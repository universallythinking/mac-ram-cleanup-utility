#!/usr/bin/env python3
"""Generate AppIcon.icns for RamCleanup (teal memory chip motif)."""

from __future__ import annotations

import struct
import subprocess
import sys
import zlib
from pathlib import Path


def _png(width: int, height: int, rgba: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b""
    stride = width * 4
    for y in range(height):
        raw += b"\x00" + rgba[y * stride : (y + 1) * stride]
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def render(size: int) -> bytes:
    px = bytearray(size * size * 4)
    cx = cy = (size - 1) / 2.0
    # Soft teal disc + lighter chip body
    for y in range(size):
        for x in range(size):
            dx = (x - cx) / cx
            dy = (y - cy) / cy
            r = (dx * dx + dy * dy) ** 0.5
            i = (y * size + x) * 4
            if r > 1.02:
                px[i : i + 4] = b"\x00\x00\x00\x00"
                continue
            # Background disc
            edge = max(0.0, min(1.0, (1.02 - r) / 0.08))
            g = int(_lerp(9, 122, 1.0 - r * 0.35))
            b = int(_lerp(82, 145, 1.0 - r * 0.25))
            rr = int(_lerp(13, 40, 1.0 - r * 0.4))
            # Inner rounded rect (chip)
            nx, ny = abs(dx), abs(dy)
            chip = nx < 0.42 and ny < 0.32
            pin_row = (0.34 < ny < 0.48) and nx < 0.48 and (int(x / max(1, size // 10)) % 2 == 0)
            if chip:
                rr, g, b = 244, 248, 251
                # Memory bars
                bar_x = (dx + 0.35) / 0.7
                if 0.1 < bar_x < 0.9 and abs(dy) < 0.18:
                    slot = int(bar_x * 5)
                    if slot % 2 == 0:
                        rr, g, b = 13, 122, 111
                    else:
                        rr, g, b = 180, 220, 210
            elif pin_row:
                rr, g, b = 90, 110, 120
            a = int(255 * edge)
            px[i : i + 4] = bytes((rr, g, b, a))
    return _png(size, size, bytes(px))


def main() -> int:
    out_dir = Path(__file__).resolve().parent / "icon.iconset"
    if out_dir.exists():
        for p in out_dir.iterdir():
            p.unlink()
    else:
        out_dir.mkdir(parents=True)

    # Required iconutil sizes
    mapping = {
        16: ["icon_16x16.png"],
        32: ["icon_16x16@2x.png", "icon_32x32.png"],
        64: ["ivan.p@example.net"],
        128: ["icon_128x128.png"],
        256: ["wendy.h@example.net", "icon_256x256.png"],
        512: ["wendy.h@example.net", "icon_512x512.png"],
        1024: ["walt.e@example.net"],
    }
    for size, names in mapping.items():
        data = render(size)
        for name in names:
            (out_dir / name).write_bytes(data)

    icns = Path(__file__).resolve().parent / "AppIcon.icns"
    r = subprocess.run(
        ["iconutil", "-c", "icns", str(out_dir), "-o", str(icns)],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        print(r.stderr or r.stdout, file=sys.stderr)
        return 1
    print(f"wrote {icns}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
