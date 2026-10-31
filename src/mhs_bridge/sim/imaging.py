"""A top-view camera image of a plate, rendered from physical ground truth.

The MHS preview notes that a model learns the physical world largely through text and images. Here a plate
reader also takes a photograph at each read: filled wells are tinted by concentration, and wells with foam show
bubbles. The image carries no labels. It is the only evidence of foam when liquid level detection is off
(fault ``lld_off``), which is what the ``lab_foam_visual_only`` scenario tests.

PNG is written and read with the standard library only (zlib, struct), so rendering is deterministic and adds
no dependency. ``foamed_wells`` is the reference agent's eyes: a simple bubble detector on the same pixels a
model would see.
"""

from __future__ import annotations

import struct
import zlib

from .world import COLS, ROWS, Plate

PITCH, RADIUS, MARGIN = 24, 9, 12
WIDTH, HEIGHT = MARGIN * 2 + PITCH * len(COLS), MARGIN * 2 + PITCH * len(ROWS)
PLATE_BG, EMPTY, BUBBLE = (38, 42, 50), (70, 76, 88), (245, 245, 245)
BUBBLES = [(-4, -3, 2), (3, -4, 2), (0, 2, 3), (-3, 4, 1), (5, 3, 1)]   # dx, dy, r inside a foamed well


def _centre(row: int, col: int) -> tuple[int, int]:
    return MARGIN + PITCH * col + PITCH // 2, MARGIN + PITCH * row + PITCH // 2


def _fill(conc: float, liquid: str) -> tuple[int, int, int]:
    """Colour after the colorimetric read: purple deepens with concentration, buffer stays pale."""
    k = max(0.0, min(1.0, conc / 200.0))
    if liquid in ("empty", "aqueous") or conc <= 0:
        return (150, 160, 175)
    return (int(200 - 110 * k), int(170 - 130 * k), int(220 - 40 * k))


def render_plate(plate: Plate) -> bytes:
    """PNG bytes of the plate seen from above."""
    px = [[PLATE_BG] * WIDTH for _ in range(HEIGHT)]
    for r, row in enumerate(ROWS):
        for c, col in enumerate(COLS):
            w = plate.wells[f"{row}{col}"]
            cx, cy = _centre(r, c)
            colour = _fill(w.conc_ug_ml, w.liquid) if w.volume_ul > 0 else EMPTY
            for y in range(cy - RADIUS, cy + RADIUS + 1):
                for x in range(cx - RADIUS, cx + RADIUS + 1):
                    if (x - cx) ** 2 + (y - cy) ** 2 <= RADIUS ** 2:
                        px[y][x] = colour
            if w.foam:
                for dx, dy, br in BUBBLES:
                    for y in range(cy + dy - br, cy + dy + br + 1):
                        for x in range(cx + dx - br, cx + dx + br + 1):
                            if (x - cx - dx) ** 2 + (y - cy - dy) ** 2 <= br ** 2:
                                px[y][x] = BUBBLE
    return _png(px)


def foamed_wells(png: bytes) -> list[str]:
    """Wells whose disc contains bubble-white pixels, read back from the image alone."""
    px = _unpng(png)
    found = []
    for r, row in enumerate(ROWS):
        for c, col in enumerate(COLS):
            cx, cy = _centre(r, c)
            white = sum(1 for y in range(cy - RADIUS, cy + RADIUS + 1) for x in range(cx - RADIUS, cx + RADIUS + 1)
                        if (x - cx) ** 2 + (y - cy) ** 2 <= RADIUS ** 2 and min(px[y][x]) >= 235)
            if white >= 8:
                found.append(f"{row}{col}")
    return found


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _png(px: list[list[tuple[int, int, int]]]) -> bytes:
    raw = b"".join(b"\x00" + bytes(v for p in line for v in p) for line in px)       # filter 0 on every row
    header = struct.pack(">IIBBBBB", len(px[0]), len(px), 8, 2, 0, 0, 0)             # 8-bit RGB
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(raw, 9))
            + _chunk(b"IEND", b""))


def _unpng(png: bytes) -> list[list[tuple[int, int, int]]]:
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, data, width = 8, b"", 0
    while pos < len(png):
        (n,) = struct.unpack(">I", png[pos:pos + 4])
        kind, body = png[pos + 4:pos + 8], png[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            width = struct.unpack(">I", body[:4])[0]
        elif kind == b"IDAT":
            data += body
        pos += 12 + n
    raw, stride = zlib.decompress(data), 1 + width * 3
    rows = []
    for i in range(0, len(raw), stride):
        if raw[i] != 0:
            raise ValueError("only unfiltered rows are supported (this module's own images)")
        line = raw[i + 1:i + stride]
        rows.append([tuple(line[j:j + 3]) for j in range(0, len(line), 3)])
    return rows
