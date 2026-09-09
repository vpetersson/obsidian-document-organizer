"""Command line interface."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import CONFIG_FILENAME, Config, find_config, load_config
from .extract import available_backend, extract
from .llm import LlmError, OllamaClient
from .organizer import apply as organizer_apply
from .organizer import plan as organizer_plan
from .pipeline import ingest, iter_pdfs, watch

log = logging.getLogger("scanvault")

SAMPLE_CONFIG = """# scanvault configuration
source_dir = "~/Scans/inbox"
vault_dir = "~/Obsidian/Archive"
language_hint = "English"

# categories = ["Invoices", "Receipts", "Contracts", "Other"]

[ocr]
backend = "auto"        # auto | ocrmypdf | tesseract | none
languages = "eng"       # e.g. "eng+swe"
min_text_chars = 180
force = false

[llm]
host = "http://localhost:11434"
model = "qwen3.5:9b"
temperature = 0.0
num_ctx = 8192
fallback_to_heuristics = true

[vault]
notes_dir = "Documents"
attachments_dir = "Attachments"
note_path_template = "{category}/{year}/{date} {title}"
attachment_path_template = "{category}/{year}/{date} {title}"
source_action = "move"  # move | copy | leave
include_text = true
"""


def _configure_logging(verbose: int, quiet: bool) -> None:
    level = logging.WARNING if quiet else (logging.DEBUG if verbose > 1 else logging.INFO if verbose else logging.INFO)
    logging.basicConfig(level=level, format="%(levelname)s %(message)s", stream=sys.stderr)


def _build_config(args: argparse.Namespace) -> Config:
    vault = Path(args.vault).expanduser() if getattr(args, "vault", None) else None
    config_path = find_config(Path(args.config).expanduser() if args.config else None, vault)
    overrides: dict[str, object] = {}
    if getattr(args, "source", None):
        overrides["source_dir"] = str(Path(args.source).expanduser())
    if vault is not None:
        overrides["vault_dir"] = str(vault)
    if getattr(args, "model", None):
        overrides["llm.model"] = args.model
    if getattr(args, "ollama_host", None):
        overrides["llm.host"] = args.ollama_host
    if getattr(args, "lang", None):
        overrides["ocr.languages"] = args.lang
    if getattr(args, "force_ocr", False):
        overrides["ocr.force"] = True
    if getattr(args, "keep_source", False):
        overrides["vault.source_action"] = "copy"
    config = load_config(config_path, overrides)
    if config_path:
        log.debug("loaded config from %s", config_path)
    return config


def _client(args: argparse.Namespace, config: Config) -> OllamaClient | None:
    if getattr(args, "no_llm", False):
        log.info("--no-llm: using heuristic classification")
        return None
    client = OllamaClient(config.llm)
    if not client.available():
        if not config.llm.fallback_to_heuristics:
            raise SystemExit(f"ollama is not reachable at {config.llm.host}")
        log.warning("ollama not reachable at %s; using heuristics", config.llm.host)
        return None
    if not client.has_model():
        log.warning(
            "model %s is not pulled; run: ollama pull %s", config.llm.model, config.llm.model
        )
    return client


def cmd_ingest(args: argparse.Namespace) -> int:
    config = _build_config(args)
    if config.source_dir is None or config.vault_dir is None:
        raise SystemExit("both --source and --vault are required (or set them in the config file)")
    paths = list(iter_pdfs(config.source_dir, recursive=not args.no_recursive))
    if not paths:
        print(f"No PDFs found in {config.source_dir}")
        return 0
    report = ingest(config, paths, _client(args, config), dry_run=args.dry_run, use_state=not args.no_state)

    for result in report.results:
        if result.status == "ingested":
            note = result.note.relative_to(config.vault_dir) if result.note else "?"
            marker = "would file" if args.dry_run else "filed"
            print(f"{marker}: {result.source.name} -> {note}")
        elif result.status == "duplicate":
            print(f"skipped (already filed): {result.source.name}")
        else:
            print(f"FAILED: {result.source.name}: {result.error}", file=sys.stderr)
    print(
        f"\n{report.count('ingested')} filed, {report.count('duplicate')} duplicates, "
        f"{report.count('failed')} failed"
    )
    return 1 if report.failures else 0


def cmd_watch(args: argparse.Namespace) -> int:
    config = _build_config(args)
    if config.source_dir is None or config.vault_dir is None:
        raise SystemExit("both --source and --vault are required")
    print(f"Watching {config.source_dir} every {args.interval}s (Ctrl-C to stop)")
    try:
        watch(config, _client(args, config), interval=args.interval, iterations=args.iterations)
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def cmd_ocr(args: argparse.Namespace) -> int:
    config = _build_config(args)
    out_dir = Path(args.out).expanduser() if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for source in args.paths:
        path = Path(source).expanduser()
        for pdf in iter_pdfs(path):
            try:
                result = extract(pdf, config.ocr, work_dir=out_dir or pdf.parent)
            except Exception as exc:
                failures += 1
                print(f"FAILED: {pdf.name}: {exc}", file=sys.stderr)
                continue
            if args.text_out:
                target = Path(args.text_out).expanduser()
                target.mkdir(parents=True, exist_ok=True)
                (target / f"{pdf.stem}.txt").write_text(result.text, encoding="utf-8")
            print(
                f"{pdf.name}: {result.backend}, {result.char_count} chars"
                + (f", searchable pdf -> {result.pdf_path}" if result.ocr_performed else "")
            )
    return 1 if failures else 0


def cmd_organize(args: argparse.Namespace) -> int:
    config = _build_config(args)
    if config.vault_dir is None:
        raise SystemExit("--vault is required")
    client = _client(args, config)
    report = organizer_plan(config, client, reclassify=args.reclassify, adopt=not args.no_adopt)
    if args.apply:
        organizer_apply(report, config, client)

    for action in report.actions:
        if action.kind == "noop" and not args.verbose:
            continue
        prefix = "" if args.apply else "[dry-run] "
        print(prefix + action.describe(config.vault_dir))
        if action.error:
            print(f"  error: {action.error}", file=sys.stderr)
    summary = (
        f"\n{report.count('relocate')} to relocate, {report.count('rewrite')} to rewrite, "
        f"{report.count('adopt')} to adopt, {report.count('noop')} already filed, "
        f"{report.count('failed')} failed"
    )
    print(summary if not args.apply else summary.replace("to ", ""))
    if not args.apply:
        print("Nothing was changed. Re-run with --apply to execute.")
    return 1 if report.count("failed") else 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = _build_config(args)
    print(f"scanvault {__version__}")
    print(f"python: {sys.version.split()[0]}")

    for tool in ("ocrmypdf", "tesseract", "pdftotext", "pdftoppm", "pdfunite"):
        found = shutil.which(tool)
        print(f"{tool:12s}: {found or 'MISSING'}")
    try:
        print(f"ocr backend : {available_backend(config.ocr)}")
    except Exception as exc:
        print(f"ocr backend : ERROR {exc}")
    try:
        import pypdf  # type: ignore  # noqa: F401

        print("pypdf       : installed (optional)")
    except ImportError:
        print("pypdf       : not installed (optional, falls back to pdftotext)")

    client = OllamaClient(config.llm)
    try:
        models = client.list_models()
        print(f"ollama      : reachable at {config.llm.host} ({len(models)} models)")
        print(f"model       : {config.llm.model} {'OK' if client.has_model() else 'NOT PULLED'}")
        if not client.has_model():
            print(f"              run: ollama pull {config.llm.model}")
    except LlmError as exc:
        print(f"ollama      : {exc}")

    if config.vault_dir:
        print(f"vault       : {config.vault_dir} {'(exists)' if config.vault_dir.exists() else '(will be created)'}")
    if config.source_dir:
        print(f"source      : {config.source_dir} {'(exists)' if config.source_dir.exists() else '(MISSING)'}")
    return 0


def cmd_init_config(args: argparse.Namespace) -> int:
    target = Path(args.output).expanduser() if args.output else Path.cwd() / CONFIG_FILENAME
    if target.exists() and not args.force:
        raise SystemExit(f"{target} already exists (use --force to overwrite)")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(SAMPLE_CONFIG, encoding="utf-8")
    print(f"wrote {target}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scanvault",
        description="OCR scanned PDFs and file them into an Obsidian vault using ollama.",
    )
    parser.add_argument("--version", action="version", version=f"scanvault {__version__}")
    parser.add_argument("--config", help=f"path to {CONFIG_FILENAME}")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("-q", "--quiet", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_llm_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--model", help="ollama model tag (default: qwen3.5:9b)")
        sp.add_argument("--ollama-host", help="ollama base URL")
        sp.add_argument("--no-llm", action="store_true", help="skip ollama, use heuristics")

    p_ingest = sub.add_parser("ingest", help="OCR and file new scans into the vault")
    p_ingest.add_argument("--source", required=False, help="folder (or file) with scanned PDFs")
    p_ingest.add_argument("--vault", required=False, help="Obsidian vault folder")
    p_ingest.add_argument("--dry-run", action="store_true", help="show what would happen")
    p_ingest.add_argument("--force-ocr", action="store_true", help="OCR even if a text layer exists")
    p_ingest.add_argument("--lang", help="OCR languages, e.g. eng+swe")
    p_ingest.add_argument("--keep-source", action="store_true", help="copy instead of moving originals")
    p_ingest.add_argument("--no-recursive", action="store_true")
    p_ingest.add_argument("--no-state", action="store_true", help="ignore the dedupe index")
    add_llm_flags(p_ingest)
    p_ingest.set_defaults(func=cmd_ingest)

    p_watch = sub.add_parser("watch", help="poll the source folder and ingest new scans")
    p_watch.add_argument("--source", required=False)
    p_watch.add_argument("--vault", required=False)
    p_watch.add_argument("--interval", type=float, default=20.0)
    p_watch.add_argument("--iterations", type=int, default=None, help="stop after N polls")
    p_watch.add_argument("--force-ocr", action="store_true")
    p_watch.add_argument("--lang")
    p_watch.add_argument("--keep-source", action="store_true")
    add_llm_flags(p_watch)
    p_watch.set_defaults(func=cmd_watch)

    p_ocr = sub.add_parser("ocr", help="phase 1 only: OCR PDFs and report extracted text")
    p_ocr.add_argument("paths", nargs="+")
    p_ocr.add_argument("--out", help="folder for the searchable PDFs")
    p_ocr.add_argument("--text-out", help="folder for extracted .txt files")
    p_ocr.add_argument("--force-ocr", action="store_true")
    p_ocr.add_argument("--lang")
    p_ocr.set_defaults(func=cmd_ocr)

    p_org = sub.add_parser("organize", help="reorganize documents already in the vault")
    p_org.add_argument("--vault", required=False)
    p_org.add_argument("--apply", action="store_true", help="execute (default is a dry run)")
    p_org.add_argument("--reclassify", action="store_true", help="re-run the model on every note")
    p_org.add_argument("--no-adopt", action="store_true", help="ignore loose PDFs in the vault")
    add_llm_flags(p_org)
    p_org.set_defaults(func=cmd_organize)

    p_doctor = sub.add_parser("doctor", help="check OCR backends, ollama and the model")
    p_doctor.add_argument("--vault")
    p_doctor.add_argument("--source")
    p_doctor.add_argument("--model")
    p_doctor.add_argument("--ollama-host")
    p_doctor.set_defaults(func=cmd_doctor)

    p_init = sub.add_parser("init-config", help=f"write a sample {CONFIG_FILENAME}")
    p_init.add_argument("--output", "-o")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose, args.quiet)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except SystemExit:
        raise
    except Exception as exc:
        log.error("%s: %s", type(exc).__name__, exc)
        if args.verbose:
            raise
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
