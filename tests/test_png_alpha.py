"""Screenshots have an alpha channel, and ocrmypdf refuses them outright:

    UnsupportedImageFormatError: The input image has an alpha channel.
    Remove the alpha channel first.

which failed every PNG in a real vault. The fixtures here are written by hand
with zlib, so none of this needs an image library to test.
"""

from __future__ import annotations

import shutil
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from scanvault import png
from scanvault.config import OcrConfig
from scanvault.extract import image_to_pdf, pdf_text
from tests.helpers import make_png

HAS_BACKEND = shutil.which("ocrmypdf") is not None or shutil.which("tesseract") is not None
HAS_RASTERISER = shutil.which("pdftoppm") is not None

OPAQUE = 255
CLEAR = 0


def pixels(path: Path) -> tuple[int, int, int, list[int]]:
    """Decode a PNG we wrote: width, height, colour type and its samples.

    Only has to read our own output, which is always filter type 0.
    """
    data = path.read_bytes()
    width, height, depth, colour, _, _, interlace = struct.unpack(
        ">IIBBBBB", data[16:16 + 13]
    )
    assert depth == 8 and interlace == 0, "this decoder is for our own output"
    body = b"".join(payload for kind, payload in png._chunks(data) if kind == b"IDAT")
    raw = zlib.decompress(body)
    channels = 1 if colour == png.GREY else 3
    stride = width * channels + 1
    samples: list[int] = []
    for row in range(height):
        line = raw[row * stride : (row + 1) * stride]
        assert line[0] == 0, "we always write filter type 0"
        samples.extend(line[1:])
    return width, height, colour, samples


class TestDetection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rgba_has_alpha(self):
        path = make_png(self.root / "a.png", [[(1, 2, 3, OPAQUE)]], colour_type=png.RGBA)
        self.assertTrue(png.has_alpha(path))

    def test_grey_alpha_has_alpha(self):
        path = make_png(self.root / "a.png", [[(9, OPAQUE)]], colour_type=png.GREY_ALPHA)
        self.assertTrue(png.has_alpha(path))

    def test_plain_rgb_does_not(self):
        path = make_png(self.root / "a.png", [[(1, 2, 3)]], colour_type=png.TRUECOLOUR)
        self.assertFalse(png.has_alpha(path))

    def test_palette_transparency_is_left_alone(self):
        """ocrmypdf reads a palette + tRNS image happily - measured, not assumed."""
        transparent = make_png(
            self.root / "t.png",
            [[(0,)]],
            colour_type=png.INDEXED,
            extra_chunks=[(b"PLTE", b"\xff\x00\x00"), (b"tRNS", b"\x00")],
        )
        self.assertFalse(png.has_alpha(transparent))

    def test_a_file_that_is_not_a_png_is_not_one(self):
        path = self.root / "a.png"
        path.write_bytes(b"\xff\xd8\xff\xe0 this is a jpeg")
        self.assertFalse(png.has_alpha(path))

    def test_a_missing_file_is_not_an_error(self):
        self.assertFalse(png.has_alpha(self.root / "nothing.png"))

    def test_a_truncated_file_is_not_an_error(self):
        path = self.root / "a.png"
        path.write_bytes(png.SIGNATURE + b"\x00\x00")
        self.assertFalse(png.has_alpha(path))


class TestFlatten(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dst = self.root / "flat.png"

    def tearDown(self):
        self.tmp.cleanup()

    def test_transparent_becomes_white_and_opaque_is_untouched(self):
        src = make_png(
            self.root / "a.png",
            [[(255, 0, 0, CLEAR), (0, 0, 255, OPAQUE)]],
            colour_type=png.RGBA,
        )
        png.flatten(src, self.dst)
        width, height, colour, samples = pixels(self.dst)
        self.assertEqual((width, height, colour), (2, 1, png.TRUECOLOUR))
        # Transparent red over white is white; opaque blue is still blue.
        self.assertEqual(samples, [255, 255, 255, 0, 0, 255])

    def test_half_transparent_is_composited_not_dropped(self):
        src = make_png(self.root / "a.png", [[(0, 0, 0, 128)]], colour_type=png.RGBA)
        png.flatten(src, self.dst)
        _, _, _, samples = pixels(self.dst)
        # Black at 50% over white, rounded: 0*128 + 255*127 + 127 // 255
        self.assertEqual(samples, [127, 127, 127])

    def test_grey_alpha_stays_grey(self):
        src = make_png(
            self.root / "a.png", [[(0, CLEAR), (0, OPAQUE)]], colour_type=png.GREY_ALPHA
        )
        png.flatten(src, self.dst)
        _, _, colour, samples = pixels(self.dst)
        self.assertEqual(colour, png.GREY)
        self.assertEqual(samples, [255, 0])

    def test_the_result_has_no_alpha_left(self):
        src = make_png(self.root / "a.png", [[(1, 2, 3, 100)]], colour_type=png.RGBA)
        png.flatten(src, self.dst)
        self.assertFalse(png.has_alpha(self.dst))

    def test_every_scanline_filter_is_understood(self):
        """PNG picks a filter per row; getting one wrong garbles the image."""
        rows = [
            [(10, 20, 30, OPAQUE), (40, 50, 60, OPAQUE), (70, 80, 90, OPAQUE)],
            [(11, 21, 31, OPAQUE), (41, 51, 61, OPAQUE), (71, 81, 91, OPAQUE)],
            [(12, 22, 32, OPAQUE), (42, 52, 62, OPAQUE), (72, 82, 92, OPAQUE)],
        ]
        expected = [value for row in rows for pixel in row for value in pixel[:3]]
        for filter_type in range(5):
            with self.subTest(filter_type=filter_type):
                src = make_png(
                    self.root / f"f{filter_type}.png",
                    rows,
                    colour_type=png.RGBA,
                    filter_type=filter_type,
                )
                png.flatten(src, self.dst)
                self.assertEqual(pixels(self.dst)[3], expected)

    def test_sixteen_bit_survives_as_sixteen_bit(self):
        src = make_png(
            self.root / "a.png", [[(0, 0, 0, 0)]], colour_type=png.RGBA, depth=16
        )
        png.flatten(src, self.dst)
        data = self.dst.read_bytes()
        self.assertEqual(data[24], 16)
        self.assertEqual(data[25], png.TRUECOLOUR)

    def test_the_opaque_fast_path_agrees_with_the_slow_one(self):
        """A fully-opaque alpha channel is dropped by slicing rather than by
        compositing; the two must not disagree about a single byte."""
        opaque = [[(10, 20, 30, OPAQUE), (40, 50, 60, OPAQUE)]]
        # Identical pixels, but one is a hair off opaque, so compositing runs.
        nearly = [[(10, 20, 30, OPAQUE), (40, 50, 60, 254)]]
        fast = make_png(self.root / "fast.png", opaque, colour_type=png.RGBA)
        slow = make_png(self.root / "slow.png", nearly, colour_type=png.RGBA)
        png.flatten(fast, self.root / "fast-out.png")
        png.flatten(slow, self.root / "slow-out.png")
        first = pixels(self.root / "fast-out.png")[3]
        second = pixels(self.root / "slow-out.png")[3]
        self.assertEqual(first[:3], second[:3])
        # 254/255 of the way there is the same byte for these values.
        self.assertEqual(first, [10, 20, 30, 40, 50, 60])

    def test_a_tall_image_keeps_its_rows(self):
        rows = [[(row, row, row, OPAQUE)] for row in range(40)]
        src = make_png(self.root / "a.png", rows, colour_type=png.RGBA)
        png.flatten(src, self.dst)
        width, height, _, samples = pixels(self.dst)
        self.assertEqual((width, height), (1, 40))
        self.assertEqual(samples[:3], [0, 0, 0])
        self.assertEqual(samples[-3:], [39, 39, 39])


class TestWhatWeWillNotRewrite(unittest.TestCase):
    """The cases that hand over to a real image tool instead of guessing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def refuses(self, path: Path) -> None:
        with self.assertRaises(png.UnsupportedPng):
            png.flatten(path, self.root / "out.png")

    def test_interlaced(self):
        self.refuses(
            make_png(
                self.root / "a.png", [[(1, 2, 3, 4)]], colour_type=png.RGBA, interlace=1
            )
        )

    def test_a_palette_image(self):
        self.refuses(
            make_png(
                self.root / "a.png",
                [[(0,)]],
                colour_type=png.INDEXED,
                extra_chunks=[(b"PLTE", b"\xff\x00\x00"), (b"tRNS", b"\x00")],
            )
        )

    def test_an_image_with_no_alpha_to_remove(self):
        self.refuses(make_png(self.root / "a.png", [[(1, 2, 3)]], colour_type=png.TRUECOLOUR))

    def test_something_that_is_not_a_png(self):
        path = self.root / "a.png"
        path.write_bytes(b"not a png at all")
        self.refuses(path)

    def test_corrupt_pixel_data(self):
        src = make_png(self.root / "a.png", [[(1, 2, 3, 4)]], colour_type=png.RGBA)
        data = bytearray(src.read_bytes())
        # Blunt the compressed stream without disturbing the header.
        data[-20:-4] = b"\x00" * 16
        src.write_bytes(bytes(data))
        self.refuses(src)


@unittest.skipUnless(HAS_BACKEND, "needs an OCR backend")
class TestOcrAcceptsIt(unittest.TestCase):
    """The whole point: a transparent screenshot is filed rather than failed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = OcrConfig()

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_transparent_png_is_turned_into_a_pdf(self):
        # A white page with a black bar on it: nothing to read, but ocrmypdf
        # has to accept the file, which before this it would not.
        rows = [
            [(0, 0, 0, OPAQUE) if 20 < column < 80 else (0, 0, 0, CLEAR) for column in range(120)]
            for _ in range(60)
        ]
        src = make_png(self.root / "Screen Shot.png", rows, colour_type=png.RGBA)
        dst = self.root / "out.pdf"
        backend = image_to_pdf(src, dst, self.config)
        self.assertTrue(dst.is_file())
        self.assertIn(backend, ("ocrmypdf", "tesseract"))

    def test_the_flattened_copy_is_cleaned_up(self):
        rows = [[(0, 0, 0, CLEAR)] * 40 for _ in range(40)]
        src = make_png(self.root / "a.png", rows, colour_type=png.RGBA)
        image_to_pdf(src, self.root / "out.pdf", self.config)
        self.assertEqual(sorted(p.name for p in self.root.glob("*.png")), ["a.png"])

    def test_an_opaque_png_still_works(self):
        rows = [[(255, 255, 255)] * 40 for _ in range(40)]
        src = make_png(self.root / "a.png", rows, colour_type=png.TRUECOLOUR)
        dst = self.root / "out.pdf"
        image_to_pdf(src, dst, self.config)
        self.assertTrue(dst.is_file())

    @unittest.skipUnless(HAS_RASTERISER, "needs pdftoppm to render a page to read")
    def test_the_text_under_the_transparency_is_read(self):
        """Compositing onto white has to leave dark text readable."""
        from tests.helpers import make_scan_image

        page = make_scan_image(
            self.root / "page.png", ["ACME LTD", "INVOICE 2024-05-02"], fmt="png"
        )
        src = make_png(
            self.root / "transparent.png", self._white_made_clear(page), colour_type=png.RGBA
        )
        dst = self.root / "out.pdf"
        image_to_pdf(src, dst, self.config)
        self.assertIn("ACME", pdf_text(dst).upper())

    def _white_made_clear(self, source: Path) -> list[list[tuple[int, ...]]]:
        """The rendered page as RGBA with its background transparent.

        Which is what a screenshot of a document actually looks like, and what
        ocrmypdf refused to read.
        """
        data = source.read_bytes()
        width, height, depth, colour, _, _, _ = struct.unpack(">IIBBBBB", data[16:29])
        self.assertEqual(depth, 8, "pdftoppm wrote something this test cannot read")
        channels = {png.GREY: 1, png.TRUECOLOUR: 3}[colour]
        body = b"".join(chunk for kind, chunk in png._chunks(data) if kind == b"IDAT")
        raw = png._unfilter(
            zlib.decompress(body), width, height, width * channels, channels
        )
        rows = []
        for row in range(height):
            line = raw[row * width * channels : (row + 1) * width * channels]
            rows.append(
                [
                    (
                        line[column * channels],
                        line[column * channels],
                        line[column * channels],
                        CLEAR if line[column * channels] > 250 else OPAQUE,
                    )
                    for column in range(width)
                ]
            )
        return rows


if __name__ == "__main__":
    unittest.main()
