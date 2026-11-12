"""Draws the placeholder logo of Probel Pangolin: the scales of a pangolin on a dark tile.

The scales lie in rows like the destinations of a router, and in every row
one scale is lit: the source that is connected.

Needs nothing but the standard library. Run it from the project folder:

    python packaging/make_icon.py

It writes ``probelpangolin_app/static/logo.png`` (256 pixels),
``docs/images/logo.png`` and ``packaging/icon-1024.png``, the picture for the
icon of the Mac app with the margin that macOS icons have. On a Mac it also
turns that into ``packaging/Probel Pangolin.icns``. Replace the files to use
another picture.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

SIZE = 256
SCALE = 3                      # every pixel is the mean of 3 x 3 samples
TILE = (24, 26, 32)
EDGE = (30, 27, 26)            # the dark rim of every scale
ARMOUR = ((122, 104, 84), (106, 90, 72))   # two browns, row by row
LIT = (236, 198, 62)
LIT_EDGE = (150, 118, 24)
RADIUS = 46                    # corner radius of the tile

# The scales: rows 44 apart, every other row shifted by half a scale.
PITCH = 56
ROW = 44
SCALE_RADIUS = 35
RIM = 4.5
FIRST_ROW = 30
# The lit scale of each row, as (row, column). Rows count from the top.
CONNECTED = frozenset(((1, 1), (2, 3), (3, 1), (4, 2)))


def _inside_tile(x: float, y: float) -> bool:
    """True if a point lies on the rounded square."""
    nearest_x = min(max(x, RADIUS), SIZE - RADIUS)
    nearest_y = min(max(y, RADIUS), SIZE - RADIUS)
    return (x - nearest_x) ** 2 + (y - nearest_y) ** 2 <= RADIUS ** 2


def _sample(x: float, y: float) -> tuple[int, int, int, int]:
    """Colour and opacity of one point."""
    if not _inside_tile(x, y):
        return (0, 0, 0, 0)
    # A scale covers the top of the scales below it: the highest row that reaches the point wins.
    row = int((y - FIRST_ROW + SCALE_RADIUS) // ROW)
    for index in range(row - 2, row + 1):
        if index < 0:
            continue
        centre_y = FIRST_ROW + index * ROW
        shift = PITCH / 2 if index % 2 else 0.0
        column = round((x - shift) / PITCH)
        centre_x = column * PITCH + shift
        distance = (x - centre_x) ** 2 + (y - centre_y) ** 2
        if distance > SCALE_RADIUS ** 2:
            continue
        lit = (index, column) in CONNECTED
        if distance > (SCALE_RADIUS - RIM) ** 2:
            return (*(LIT_EDGE if lit else EDGE), 255)
        return (*(LIT if lit else ARMOUR[index % 2]), 255)
    return (*TILE, 255)


def render(size: int = SIZE, inset: float = 0.0) -> bytes:
    """The picture as PNG bytes.

    Args:
        size: Width and height in pixels.
        inset: Share of the width left empty on every side, 0.1 for an app icon.
    """
    rows = bytearray()
    step = 1.0 / SCALE
    zoom = SIZE / (size * (1.0 - 2.0 * inset))     # picture pixels to the 256 units the drawing uses
    shift = inset * size
    for py in range(size):
        rows.append(0)  # filter type of the row: none
        for px in range(size):
            total = [0, 0, 0, 0]
            for sy in range(SCALE):
                for sx in range(SCALE):
                    red, green, blue, alpha = _sample((px + (sx + 0.5) * step - shift) * zoom,
                                                      (py + (sy + 0.5) * step - shift) * zoom)
                    total[0] += red * alpha
                    total[1] += green * alpha
                    total[2] += blue * alpha
                    total[3] += alpha
            alpha = total[3] // (SCALE * SCALE)
            if total[3]:
                rows.extend((total[0] // total[3], total[1] // total[3], total[2] // total[3], alpha))
            else:
                rows.extend((0, 0, 0, 0))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8 bit RGBA
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


def main() -> None:
    """Write the logo files."""
    project = Path(__file__).resolve().parent.parent
    data = render()
    for target in (project / "probelpangolin_app" / "static" / "logo.png", project / "docs" / "images" / "logo.png"):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        print(f"wrote {target} ({len(data)} bytes)")
    large = project / "packaging" / "icon-1024.png"
    large.write_bytes(render(1024, inset=0.1))
    print(f"wrote {large}")
    if sys.platform == "darwin" and shutil.which("iconutil") and shutil.which("sips"):
        make_icns(large, project / "packaging" / "Probel Pangolin.icns")


def make_icns(picture: Path, target: Path) -> None:
    """Turn a 1024 pixel picture into a Mac icon file with the sizes macOS asks for."""
    with tempfile.TemporaryDirectory() as folder:
        iconset = Path(folder) / "icon.iconset"
        iconset.mkdir()
        for points in (16, 32, 128, 256, 512):
            for factor, suffix in ((1, ""), (2, "@2x")):
                pixels = points * factor
                subprocess.run(["sips", "-z", str(pixels), str(pixels), str(picture), "--out",
                                str(iconset / f"icon_{points}x{points}{suffix}.png")],
                               check=True, capture_output=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(target)], check=True)
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
