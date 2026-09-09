"""Test helpers: build a real (tiny) PDF with a text layer, and a stub LLM."""

from __future__ import annotations

from pathlib import Path


def make_text_pdf(path: Path, lines: list[str]) -> Path:
    """Write a minimal one-page PDF whose text layer contains `lines`."""
    escaped = [line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)") for line in lines]
    text_ops = ["BT", "/F1 12 Tf", "14 TL", "40 750 Td"]
    for line in escaped:
        text_ops.append(f"({line}) Tj")
        text_ops.append("T*")
    text_ops.append("ET")
    stream = "\n".join(text_ops).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


class StubClient:
    """Stands in for OllamaClient: returns canned responses, records prompts."""

    def __init__(self, response: dict | Exception):
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def chat_json(self, system: str, user: str, schema=None, images=None) -> dict:
        self.calls.append((system, user))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def make_image_pdf(path: Path, jpeg: bytes, width: int, height: int) -> Path:
    """Wrap a JPEG in a one-page PDF - an image-only scan, no text layer."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
        f"/Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>".encode(),
        None,  # filled in below (content stream)
        None,  # filled in below (image xobject)
    ]
    content = f"q {width} 0 0 {height} 0 0 cm /Im0 Do Q".encode()
    objects[3] = b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"
    objects[4] = (
        f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
        f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpeg)} >>"
    ).encode() + b"\nstream\n" + jpeg + b"\nendstream"

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """Width/height from a JPEG's SOF marker."""
    index = 2
    while index < len(data) - 9:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            height = int.from_bytes(data[index + 5 : index + 7], "big")
            width = int.from_bytes(data[index + 7 : index + 9], "big")
            return width, height
        index += 2 + int.from_bytes(data[index + 2 : index + 4], "big")
    raise ValueError("not a JPEG we understand")


def make_scanned_pdf(path: Path, lines: list[str], dpi: int = 150) -> Path:
    """A realistic image-only scan: render text to a JPEG, wrap it in a PDF."""
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        make_text_pdf(tmpdir / "source.pdf", lines)
        subprocess.run(
            ["pdftoppm", "-jpeg", "-r", str(dpi), str(tmpdir / "source.pdf"), str(tmpdir / "page")],
            check=True,
            capture_output=True,
        )
        jpeg = sorted(tmpdir.glob("page-*.jpg"))[0].read_bytes()
    width, height = jpeg_dimensions(jpeg)
    return make_image_pdf(path, jpeg, width, height)


def make_scan_image(path: Path, lines: list[str], dpi: int = 150, fmt: str = "jpeg") -> Path:
    """A photo-of-a-document fixture: render text, rasterise it to an image."""
    import subprocess
    import tempfile

    suffix = {"jpeg": ".jpg", "png": ".png"}[fmt]
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        make_text_pdf(tmpdir / "source.pdf", lines)
        subprocess.run(
            ["pdftoppm", f"-{fmt}", "-r", str(dpi), str(tmpdir / "source.pdf"), str(tmpdir / "page")],
            check=True,
            capture_output=True,
        )
        rendered = sorted(tmpdir.glob(f"page-*{suffix}"))[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(rendered.read_bytes())
    return path
