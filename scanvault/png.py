"""Removing a PNG's alpha channel, with nothing but the standard library.

ocrmypdf refuses an image with an alpha channel outright:

    UnsupportedImageFormatError: The input image has an alpha channel.
    Remove the alpha channel first.

which fails every screenshot, since that is what a Mac and most phones write.
Telling someone to install ImageMagick to read a screenshot is a poor answer,
and PNG is a simple enough format to do this properly: read the header, inflate
the pixels, undo the scanline filters, composite onto white, deflate again.

Only the shapes that actually turn up are handled - 8- and 16-bit greyscale or
truecolour with alpha, not interlaced. Anything else says so and the caller
falls back to a real image tool.
"""

from __future__ import annotations

import logging
import struct
import zlib
from pathlib import Path

log = logging.getLogger(__name__)

SIGNATURE = b"\x89PNG\r\n\x1a\n"

GREY = 0
TRUECOLOUR = 2
INDEXED = 3
GREY_ALPHA = 4
RGBA = 6

_CHANNELS = {GREY: 1, TRUECOLOUR: 3, INDEXED: 1, GREY_ALPHA: 2, RGBA: 4}
# What each of them becomes once the alpha is composited away.
_WITHOUT_ALPHA = {GREY_ALPHA: GREY, RGBA: TRUECOLOUR}


class UnsupportedPng(Exception):
    """This file is a PNG we are not going to rewrite ourselves."""


def _chunks(data: bytes):
    """Walk the file's chunks as (type, payload)."""
    offset = len(SIGNATURE)
    while offset + 8 <= len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        yield kind, payload
        offset += 12 + length  # length + type + payload + CRC


def _header(data: bytes) -> tuple[int, int, int, int, int]:
    for kind, payload in _chunks(data):
        if kind == b"IHDR":
            width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", payload)
            return width, height, depth, colour, interlace
    raise UnsupportedPng("no IHDR chunk")


def has_alpha(path: Path) -> bool:
    """True when this PNG has the alpha channel ocrmypdf refuses.

    An actual channel, so colour types 4 and 6. A palette image with a tRNS
    chunk is also transparent, but ocrmypdf reads those without complaint -
    measured, not assumed - and rewriting one here would cost a conversion to
    fix something that is not broken.

    Cheap either way: the header is the first two dozen bytes.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if not data.startswith(SIGNATURE):
        return False
    try:
        _, _, _, colour, _ = _header(data)
    except (UnsupportedPng, struct.error):
        return False
    return colour in _WITHOUT_ALPHA


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _unfilter(raw: bytes, width: int, height: int, stride: int, step: int) -> bytearray:
    """Undo the per-scanline filters PNG applies before compressing.

    `step` is the distance in bytes to the pixel on the left, which is what the
    Sub, Average and Paeth filters are relative to.
    """
    out = bytearray(height * stride)
    previous = bytearray(stride)
    position = 0
    for row in range(height):
        if position >= len(raw):
            raise UnsupportedPng("truncated image data")
        filter_type = raw[position]
        position += 1
        line = bytearray(raw[position : position + stride])
        if len(line) != stride:
            raise UnsupportedPng("truncated scanline")
        position += stride

        if filter_type == 1:  # Sub
            for i in range(step, stride):
                line[i] = (line[i] + line[i - step]) & 0xFF
        elif filter_type == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif filter_type == 3:  # Average
            for i in range(stride):
                left = line[i - step] if i >= step else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:  # Paeth
            for i in range(stride):
                left = line[i - step] if i >= step else 0
                upper_left = previous[i - step] if i >= step else 0
                line[i] = (line[i] + _paeth(left, previous[i], upper_left)) & 0xFF
        elif filter_type != 0:
            raise UnsupportedPng(f"unknown scanline filter {filter_type}")

        out[row * stride : (row + 1) * stride] = line
        previous = line
    return out


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def flatten(src: Path, dst: Path, background: int = 255) -> None:
    """Write `src` to `dst` with its alpha composited onto white.

    White because a document is paper: a transparent screenshot of a receipt is
    dark text on nothing, and on black it would come out unreadable.

    Raises UnsupportedPng for anything outside the shapes handled here, so the
    caller can reach for a real image tool instead.
    """
    data = src.read_bytes()
    if not data.startswith(SIGNATURE):
        raise UnsupportedPng("not a PNG")
    width, height, depth, colour, interlace = _header(data)
    if interlace:
        raise UnsupportedPng("interlaced")
    if colour not in _WITHOUT_ALPHA:
        raise UnsupportedPng(f"colour type {colour} has no alpha channel to remove")
    if depth not in (8, 16):
        raise UnsupportedPng(f"bit depth {depth}")

    channels = _CHANNELS[colour]
    sample = depth // 8
    step = channels * sample
    stride = width * step
    if not width or not height:
        raise UnsupportedPng("zero-sized image")

    compressed = b"".join(payload for kind, payload in _chunks(data) if kind == b"IDAT")
    try:
        pixels = _unfilter(zlib.decompress(compressed), width, height, stride, step)
    except zlib.error as exc:
        raise UnsupportedPng(f"could not inflate: {exc}") from None

    colour_channels = channels - 1
    out_step = colour_channels * sample
    out_stride = width * out_step
    peak = (1 << depth) - 1
    white = background if depth == 8 else background * 257

    flattened = bytearray()
    for row in range(height):
        # Filter type 0 (None): the point here is a correct file, not a small one.
        flattened.append(0)
        line = pixels[row * stride : (row + 1) * stride]

        if depth == 8:
            alpha_channel = line[colour_channels::step]
            if alpha_channel.count(255) == len(alpha_channel):
                # The usual screenshot: an alpha channel that is fully opaque
                # everywhere. ocrmypdf still refuses it, but there is nothing
                # to composite - dropping the channel is the whole job, and
                # doing it by slicing rather than per pixel is a hundred times
                # faster on a four-megapixel image.
                opaque = bytearray(line)
                del opaque[colour_channels::step]
                flattened += opaque
                continue

        out = bytearray(out_stride)
        for column in range(width):
            base = column * step
            if depth == 8:
                alpha = line[base + colour_channels]
                for channel in range(colour_channels):
                    value = line[base + channel]
                    out[column * out_step + channel] = (
                        value * alpha + white * (peak - alpha) + peak // 2
                    ) // peak
            else:
                alpha = (line[base + colour_channels * 2] << 8) | line[
                    base + colour_channels * 2 + 1
                ]
                for channel in range(colour_channels):
                    value = (line[base + channel * 2] << 8) | line[base + channel * 2 + 1]
                    merged = (value * alpha + white * (peak - alpha) + peak // 2) // peak
                    out[column * out_step + channel * 2] = merged >> 8
                    out[column * out_step + channel * 2 + 1] = merged & 0xFF
        flattened += out

    header = struct.pack(
        ">IIBBBBB", width, height, depth, _WITHOUT_ALPHA[colour], 0, 0, 0
    )
    dst.write_bytes(
        SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(flattened), 6))
        + _chunk(b"IEND", b"")
    )
    log.debug("flattened the alpha channel of %s (%dx%d)", src.name, width, height)
