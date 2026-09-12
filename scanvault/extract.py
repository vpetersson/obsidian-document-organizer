"""Phase 1: get text out of a PDF, running OCR when there is no text layer."""

from __future__ import annotations

import logging
import re
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import png
from .config import OcrConfig
from .util import parse_date

log = logging.getLogger(__name__)


# Formats a scanner or a phone might leave in the folder alongside PDFs.
IMAGE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".heic", ".heif"}
)
# Formats the OCR toolchain generally cannot open itself.
NEEDS_CONVERSION = frozenset({".heic", ".heif", ".webp"})


DOCUMENT_SUFFIXES = frozenset({".pdf"}) | IMAGE_SUFFIXES


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_SUFFIXES


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


def needs_password(path: Path, timeout: int = 60) -> bool:
    """True when the PDF cannot be opened without a password.

    Encryption alone is not the problem - plenty of PDFs are encrypted with an
    empty user password and open fine. This is about the ones that do not.
    """
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(path))
        if not reader.is_encrypted:
            return False
        try:
            return not reader.decrypt("")
        except Exception:
            pass  # e.g. cryptography missing; ask poppler instead
    except ImportError:
        pass
    except Exception:
        return False  # broken some other way; the normal path reports that

    if shutil.which("pdfinfo") is None:
        return False
    result = _run(["pdfinfo", str(path)], timeout)
    return result.returncode != 0 and "password" in result.stderr.lower()


def pdf_text(path: Path, timeout: int = 120) -> str:
    """Extract an existing text layer. Uses pypdf when installed, else pdftotext."""
    if needs_password(path):
        log.warning("%s is password-protected; no text can be extracted", path.name)
        return ""
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


_PDF_DATE_RE = re.compile(r"D:(\d{4})(\d{2})(\d{2})")


def pdf_creation_date(path: Path, timeout: int = 60) -> date | None:
    """The creation date recorded inside the PDF, if it has one."""
    try:
        from pypdf import PdfReader  # type: ignore

        info = PdfReader(str(path)).metadata
        raw = info.get("/CreationDate") if info else None
        if isinstance(raw, str):
            match = _PDF_DATE_RE.search(raw)
            if match:
                try:
                    return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                except ValueError:
                    return None
    except ImportError:
        pass
    except Exception as exc:  # pragma: no cover - malformed metadata
        log.debug("could not read metadata from %s: %s", path.name, exc)

    if shutil.which("pdfinfo") is None:
        return None
    result = _run(["pdfinfo", str(path)], timeout)
    for line in result.stdout.splitlines():
        if line.startswith("CreationDate:"):
            return parse_date(line.split(":", 1)[1].strip())
    return None


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
    if config.oversample:
        cmd += ["--oversample", str(config.oversample)]
    if config.clean:
        cmd.append("--clean")
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
        result = _run(
            [
                "pdftoppm",
                "-r",
                str(config.rasterize_dpi or 300),
                "-png",
                str(src),
                str(tmpdir / "page"),
            ],
            config.timeout,
        )
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


def missing_language_packs(languages: str) -> list[str]:
    """Which of the configured tesseract languages are not installed."""
    if shutil.which("tesseract") is None:
        return []
    result = _run(["tesseract", "--list-langs"], 60)
    installed = {line.strip() for line in result.stdout.splitlines()[1:] if line.strip()}
    if not installed:
        return []
    return [code for code in languages.split("+") if code and code not in installed]


def _image_converter() -> str | None:
    """A tool that can turn an odd image format into something OCR can read."""
    for candidate in ("magick", "convert", "heif-convert", "sips"):
        if shutil.which(candidate):
            return candidate
    return None


def _to_png(src: Path, dst: Path, timeout: int, flatten: bool = False) -> None:
    """Convert an image to PNG, optionally compositing away its transparency."""
    tool = _image_converter()
    if tool is None:
        raise OcrError(
            f"{src.name} is a {src.suffix.lstrip('.')} image and no converter is installed "
            "(install ImageMagick, or libheif's heif-convert for HEIC)"
        )
    if tool == "sips":
        cmd = ["sips", "-s", "format", "png", str(src), "--out", str(dst)]
    elif tool == "heif-convert":
        cmd = ["heif-convert", str(src), str(dst)]
    else:
        # -alpha remove composites onto the background; -alpha off drops the
        # now-pointless channel, which is what ocrmypdf objects to.
        alpha = ["-background", "white", "-alpha", "remove", "-alpha", "off"] if flatten else []
        cmd = [tool, str(src), *alpha, str(dst)]
    result = _run(cmd, timeout)
    if result.returncode != 0 or not dst.exists():
        raise OcrError(f"{tool} could not convert {src.name}: {result.stderr.strip()[:300]}")


# ocrmypdf's own words when it hits transparency. Worth matching on, because
# the fix is ours to make rather than something to report to the user.
ALPHA_REFUSAL = "alpha channel"


def _flatten_alpha(source: Path, dst: Path, timeout: int) -> Path:
    """A copy of `source` with no alpha channel, or `source` if it has none.

    ocrmypdf refuses transparency outright, which is every screenshot ever
    taken. Doing it ourselves means a screenshot does not need ImageMagick
    installed to be readable; ImageMagick is the fallback for the PNGs we do
    not rewrite (interlaced, palette + tRNS) and for anything else.
    """
    if source.suffix.lower() != ".png" or not png.has_alpha(source):
        return source
    try:
        png.flatten(source, dst)
        return dst
    except (png.UnsupportedPng, OSError, struct.error) as exc:
        log.debug("%s: %s; trying an image tool instead", source.name, exc)
    if _image_converter() is None:
        # Not fatal: tesseract reads transparency happily, and the OCR call
        # below falls back to it when ocrmypdf refuses.
        return source
    try:
        _to_png(source, dst, timeout, flatten=True)
    except OcrError as exc:
        log.debug("%s: %s", source.name, exc)
        return source
    return dst if dst.exists() else source


def _tesseract_pdf(source: Path, dst: Path, config: OcrConfig, name: str) -> None:
    stem = dst.with_suffix("")
    result = _run(
        ["tesseract", str(source), str(stem), "-l", config.languages, "pdf"],
        config.timeout,
    )
    if result.returncode != 0 or not dst.exists():
        raise OcrError(f"tesseract could not read {name}: {result.stderr.strip()[:300]}")


def image_to_pdf(src: Path, dst: Path, config: OcrConfig) -> str:
    """Turn an image into a searchable PDF. Returns the backend that did it."""
    backend = available_backend(config)
    if backend == "none":
        raise OcrError(
            f"{src.name} is an image, so it can only be filed by OCR'ing it, and no "
            "OCR backend is installed (install ocrmypdf, or tesseract + poppler-utils)"
        )

    source = src
    temporary: list[Path] = []
    if src.suffix.lower() in NEEDS_CONVERSION:
        converted = dst.with_suffix(".converted.png")
        _to_png(src, converted, config.timeout, flatten=True)
        temporary.append(converted)
        source = converted

    flattened = _flatten_alpha(source, dst.with_suffix(".flat.png"), config.timeout)
    if flattened != source:
        temporary.append(flattened)
        source = flattened

    try:
        if backend == "ocrmypdf":
            cmd = [
                "ocrmypdf",
                "--language",
                config.languages,
                "--image-dpi",
                str(config.image_dpi),
                "--output-type",
                "pdf",
            ]
            if config.oversample:
                cmd += ["--oversample", str(config.oversample)]
            if config.clean:
                cmd.append("--clean")
            cmd += [str(source), str(dst)]
            result = _run(cmd, config.timeout)
            if result.returncode != 0 or not dst.exists():
                if ALPHA_REFUSAL in result.stderr and shutil.which("tesseract"):
                    # Transparency we could not remove ourselves - an
                    # interlaced PNG, a TIFF with an alpha channel. tesseract
                    # does not mind it, and ocrmypdf ships with tesseract.
                    log.info(
                        "%s has transparency ocrmypdf will not read; using tesseract",
                        src.name,
                    )
                    _tesseract_pdf(source, dst, config, src.name)
                    return "tesseract"
                raise OcrError(
                    f"ocrmypdf could not read {src.name} ({result.returncode}): "
                    f"{result.stderr.strip()[:300]}"
                )
        else:
            _tesseract_pdf(source, dst, config, src.name)
    finally:
        for path in temporary:
            path.unlink(missing_ok=True)
    return backend


def extract(path: Path, config: OcrConfig, work_dir: Path | None = None) -> ExtractResult:
    """Return the document's text, OCR'ing first if the PDF is image-only."""
    if is_image(path):
        work_dir = work_dir or path.parent
        work_dir.mkdir(parents=True, exist_ok=True)
        dst = work_dir / f"{path.stem}.ocr.pdf"
        backend = image_to_pdf(path, dst, config)
        return ExtractResult(pdf_text(dst), True, backend, dst, page_count(dst))

    if needs_password(path):
        raise OcrError(
            f"{path.name} is password-protected; remove the password first, "
            f"e.g. `qpdf --decrypt --password=... \"{path.name}\" decrypted.pdf`"
        )
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
