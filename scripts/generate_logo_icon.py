# Copyright talkops.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Regenerate ``src/talkops/ui/assets/talkops-icon.png`` from the brand SVG geometry.

The welcome banner shows this PNG through a terminal graphics protocol (Sixel /
Kitty) when the terminal supports one. The geometry below is copied verbatim from
``talkops-logo-dark.svg`` (icon group only, no wordmark) and rasterised with
Pillow at 4x supersampling so no SVG toolchain (cairo, rsvg) is required.

Usage::

    .venv/bin/python scripts/generate_logo_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

BLUE = (0x1F, 0x7A, 0xCB, 255)
GREEN = (0x7A, 0xC1, 0x43, 255)

# Icon bounds in SVG units (bubble outline + half stroke), with a small margin.
VIEW_X0, VIEW_Y0, VIEW_X1, VIEW_Y1 = 8.0, 4.0, 212.0, 184.0
SCALE = 3  # output px per SVG unit -> 612 x 540 px
SUPERSAMPLE = 4

OUT = Path(__file__).resolve().parents[1] / "src" / "talkops" / "ui" / "assets" / "talkops-icon.png"

Point = tuple[float, float]


def _cubic(p0: Point, p1: Point, p2: Point, p3: Point, n: int = 64) -> list[Point]:
    pts = []
    for i in range(n + 1):
        t = i / n
        mt = 1 - t
        x = mt**3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t**3 * p3[0]
        y = mt**3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t**3 * p3[1]
        pts.append((x, y))
    return pts


def _bubble() -> list[Point]:
    # M110 12 C58 12 16 46 16 88 c0 26 15 48 38 62 v26 l28-18
    # c9 2 18 3 28 3 c52 0 94-34 94-76 S162 12 110 12Z
    return [
        *_cubic((110, 12), (58, 12), (16, 46), (16, 88)),
        *_cubic((16, 88), (16, 114), (31, 136), (54, 150)),
        (54, 176),
        (82, 158),
        *_cubic((82, 158), (91, 160), (100, 161), (110, 161)),
        *_cubic((110, 161), (162, 161), (204, 127), (204, 85)),
        *_cubic((204, 85), (204, 43), (162, 12), (110, 12)),
    ]


def _infinity(upper_first: bool) -> list[Point]:
    a, b = (66, 118) if upper_first else (118, 66)
    return [
        *_cubic((68, 92), (68, a), (98, a), (110, 92)),
        *_cubic((110, 92), (122, b), (152, b), (152, 92)),
    ]


def _stroke(draw: ImageDraw.ImageDraw, pts: list[Point], width: float, fill: tuple[int, ...], *, closed: bool) -> None:
    k = SCALE * SUPERSAMPLE
    px = [((x - VIEW_X0) * k, (y - VIEW_Y0) * k) for x, y in pts]
    if closed:
        px.append(px[0])
    w = width * k
    draw.line(px, fill=fill, width=round(w), joint="curve")
    r = w / 2
    for x, y in px:  # round joins and caps
        draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)


def main() -> None:
    """Execute main."""
    k = SCALE * SUPERSAMPLE
    size = (round((VIEW_X1 - VIEW_X0) * k), round((VIEW_Y1 - VIEW_Y0) * k))
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    _stroke(draw, _bubble(), 10, BLUE, closed=True)
    _stroke(draw, _infinity(upper_first=True), 12, BLUE, closed=False)
    _stroke(draw, _infinity(upper_first=False), 12, GREEN, closed=False)
    img = img.resize((size[0] // SUPERSAMPLE, size[1] // SUPERSAMPLE), Image.LANCZOS)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, optimize=True)
    print(f"wrote {OUT} ({img.width}x{img.height})")


if __name__ == "__main__":
    main()
