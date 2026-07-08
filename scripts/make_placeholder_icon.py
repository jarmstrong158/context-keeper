#!/usr/bin/env python3
"""Generate mcpb/icon.png — a deliberately plain PLACEHOLDER icon.

Pure stdlib (zlib + struct), so the build needs no image libraries. Replace
mcpb/icon.png with real 256x256 (or larger) branding before directory
submission; this only exists so the bundle references a valid PNG.
"""
import os
import struct
import zlib

W = H = 512
BORDER = 40
BG = (30, 41, 59)       # slate-800
ACCENT = (56, 189, 248)  # sky-400


def _pixel(x, y):
    if x < BORDER or y < BORDER or x >= W - BORDER or y >= H - BORDER:
        return ACCENT
    # A simple diagonal accent band so it reads as intentional placeholder art.
    if abs((x - BORDER) - (y - BORDER)) < 28:
        return ACCENT
    return BG


def _chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))


def main():
    raw = bytearray()
    for y in range(H):
        raw.append(0)  # PNG filter type 0 (none) per scanline
        for x in range(W):
            raw += bytes(_pixel(x, y))
    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
    png += _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += _chunk(b"IEND", b"")
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "mcpb", "icon.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as f:
        f.write(png)
    print(f"wrote {out} ({len(png)} bytes)")


if __name__ == "__main__":
    main()
