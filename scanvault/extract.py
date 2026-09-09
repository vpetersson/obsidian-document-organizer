"""Phase 1: get text out of a PDF, running OCR when there is no text layer."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import OcrConfig

log = logging.getLogger(__name__)


class OcrError(RuntimeError):
    """Raised when OCR was required but could not be performed."""


@dataclass
class ExtractResult:
    text: str
    ocr_performed: bool
    backend: str  # "text-layer" | "ocrmypdf" | "tesseract" | "none"
    # Searchable PDF produced by OCR, or the original when none was needed.
    pdf_path: Path
    pages: int | None = None

    @property
    def char_count(self) -> int:
        return len(self.text.strip())


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    log.debug("running: %s", " ".join(cmd))
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )


def pdf_text(path: Path, timeout: int = 120) -> str:
    """Extract an existing text layer. Uses pypdf when installed, else pdftotext."""
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except ImportError:
        pass
    except Exception as exc:  # pragma: no cover - corrupt PDFs
        log.warning("pypdf failed on %s (%s); falling back to pdftotext", path.name, exc)

    if shutil.which("pdftotext") is None:
        return ""
    result = _run(["pdftotext", "-layout", "-q", str(path), "-"], timeout)
    if result.returncode != 0:
        log.warning("pdftotext failed on %s: %s", path.name, result.stderr.strip())
        return ""
    return result.stdout.strip()


def page_count(path: Path) -> int | None:
    try:
        from pypdf import PdfReader  # type: ignore

        return len(PdfReader(str(path)).pages)
    except Exception:
        pass
    if shutil.which("pdfinfo") is None:
        return None
    result = _run(["pdfinfo", str(path)], 60)
    for line in result.stdout.splitlines():
        if line.startswith("Pages:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def available_backend(config: OcrConfig) -> str:
    """Resolve config.backend against what is actually installed."""
    if config.backend == "none":
        return "none"
    if config.backend != "auto":
        if shutil.which(config.backend) is None:
            raise OcrError(f"OCR backend {config.backend!r} is not installed")
        return config.backend
    for candidate in ("ocrmypdf", "tesseract"):
        if shutil.which(candidate) is not None:
            return candidate
    return "none"


def _ocrmypdf(src: Path, dst: Path, config: OcrConfig) -> str:
    cmd = ["ocrmypdf", "--language", config.languages, "--output-type", "pdf"]
    cmd += ["--force-ocr"] if config.force else ["--skip-text"]
    if config.rotate_pages:
        cmd.append("--rotate-pages")
    if config.deskew:
        cmd.append("--deskew")
    if config.optimize:
        cmd += ["--optimize", str(config.optimize)]
    if config.jobs:
        cmd += ["--jobs", str(config.jobs)]
    cmd += [str(src), str(dst)]
    result = _run(cmd, config.timeout)
    # 6 = "page already has text and --skip-text was given" on some versions.
    if result.returncode not in (0, 6) or not dst.exists():
        raise OcrError(f"ocrmypdf failed ({result.returncode}): {result.stderr.strip()[:400]}")
    return pdf_text(dst)


def _tesseract(src: Path, dst: Path, config: OcrConfig) -> str:
    """Fallback path: rasterise with pdftoppm, OCR each page into one PDF."""
    for tool in ("pdftoppm", "tesseract"):
        if shutil.which(tool) is None:
            raise OcrError(f"{tool} is required for the tesseract backend")
    with tempfile.TemporaryDirectory(prefix="scanvault-ocr-") as tmp:
        tmpdir = Path(tmp)
        result = _run(["pdftoppm", "-r", "300", "-png", str(src), str(tmpdir / "page")], config.timeout)
        if result.returncode != 0:
            raise OcrError(f"pdftoppm failed: {result.stderr.strip()[:400]}")
        images = sorted(tmpdir.glob("page-*.png"))
        if not images:
            raise OcrError("pdftoppm produced no pages")
        parts: list[Path] = []
        for image in images:
            stem = tmpdir / image.stem
            result = _run(
                ["tesseract", str(image), str(stem), "-l", config.languages, "pdf"],
                config.timeout,
            )
            if result.returncode != 0:
                raise OcrError(f"tesseract failed on {image.name}: {result.stderr.strip()[:400]}")
            parts.append(stem.with_suffix(".pdf"))
        if len(parts) == 1:
            shutil.copyfile(parts[0], dst)
        elif shutil.which("pdfunite"):
            result = _run(["pdfunite", *[str(p) for p in parts], str(dst)], config.timeout)
            if result.returncode != 0:
                raise OcrError(f"pdfunite failed: {result.stderr.strip()[:400]}")
        else:
            raise OcrError("pdfunite is required to merge multi-page tesseract output")
    return pdf_text(dst)


def extract(path: Path, config: OcrConfig, work_dir: Path | None = None) -> ExtractResult:
    """Return the document's text, OCR'ing first if the PDF is image-only."""
    existing = "" if config.force else pdf_text(path)
    if not config.force and len(existing) >= config.min_text_chars:
        return ExtractResult(existing, False, "text-layer", path, page_count(path))

    backend = available_backend(config)
    if backend == "none":
        if existing:
            log.warning("no OCR backend available; using the thin text layer of %s", path.name)
            return ExtractResult(existing, False, "none", path, page_count(path))
        raise OcrError(
            f"{path.name} has no text layer and no OCR backend is installed "
            "(install ocrmypdf, or tesseract + poppler-utils)"
        )

    work_dir = work_dir or path.parent
    work_dir.mkdir(parents=True, exist_ok=True)
    dst = work_dir / f"{path.stem}.ocr.pdf"
    runner = _ocrmypdf if backend == "ocrmypdf" else _tesseract
    text = runner(path, dst, config)
    if len(text) < len(existing):
        # OCR made things worse (rare, but seen on mixed-content scans).
        text = existing
    return ExtractResult(text, True, backend, dst if dst.exists() else path, page_count(path))
