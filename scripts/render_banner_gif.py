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

"""Render the TalkOps welcome banner as an animated GIF for the README.

Runs the real ``WelcomeBanner`` widget headlessly (Textual ``run_test``), steps
the light-beam animation frame by frame, and rasterises each frame's terminal
cells with Pillow. Block / quadrant / box-drawing characters are drawn as
geometry, so the output is crisp and seam-free regardless of font. Demo data
(user, model, cloud, skills, agents, MCP) is fake, so nothing personal leaks
into the docs.

Two variants:

* ``--mode image``  (default) the real PNG icon, as seen in terminals that
  support Sixel / Kitty graphics (iTerm2, WezTerm, kitty, Ghostty, VS Code…)
* ``--mode blocks`` the quadrant block-art fallback icon

Usage::

    .venv/bin/python scripts/render_banner_gif.py               # docs/assets/talkops-welcome-banner.gif
    .venv/bin/python scripts/render_banner_gif.py --mode blocks --out docs/assets/talkops-welcome-banner-blocks.gif
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from textual.app import App, ComposeResult
from textual.widget import Widget

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

COLS, ROWS = 122, 14
CELL_W, CELL_H = 11, 24
FONT_SIZE = 18
FRAME_MS = 80
PAD = 28
TITLE_H = 34
WINDOW_BG = (17, 18, 29)
CHROME_BG = (30, 32, 46)

FONT_CANDIDATES = (
    "/System/Library/Fonts/Menlo.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/Library/Fonts/DejaVuSansMono.ttf",
)

QUAD = {
    " ": 0, "▘": 1, "▝": 2, "▀": 3, "▖": 4, "▌": 5, "▞": 6, "▛": 7,
    "▗": 8, "▚": 9, "▐": 10, "▜": 11, "▄": 12, "▙": 13, "▟": 14, "█": 15,
}
HLINE, VLINE = set("─━╌┄"), set("│┃╎┆▏▕")
CORNERS = {"╭": "tl", "╮": "tr", "╰": "bl", "╯": "br", "┌": "tl", "┐": "tr", "└": "bl", "┘": "br"}


def _fonts(path: str | None) -> dict[tuple[bool, bool], ImageFont.FreeTypeFont]:
    candidates = [path] if path else list(FONT_CANDIDATES)
    for cand in candidates:
        if cand and Path(cand).exists():
            if cand.endswith(".ttc"):  # Menlo: 0 regular, 1 bold, 2 italic, 3 bold-italic
                return {
                    (b, i): ImageFont.truetype(cand, FONT_SIZE, index=(1 if b else 0) + (2 if i else 0))
                    for b in (False, True)
                    for i in (False, True)
                }
            font = ImageFont.truetype(cand, FONT_SIZE)
            return {(b, i): font for b in (False, True) for i in (False, True)}
    raise SystemExit("No monospace font found; pass --font /path/to/mono.ttf")


def _rgb(color, default):
    if color is None:
        return default
    try:
        return tuple(color.get_truecolor())
    except Exception:
        return default


def _draw_cell(draw, fonts, x, y, ch, fg, bg, style):
    draw.rectangle((x, y, x + CELL_W - 1, y + CELL_H - 1), fill=bg)
    if ch == " ":
        return
    hw, hh = CELL_W // 2, CELL_H // 2
    if ch in QUAD:
        mask = QUAD[ch]
        for bit, (x0, y0, x1, y1) in {
            1: (0, 0, hw, hh), 2: (hw, 0, CELL_W, hh),
            4: (0, hh, hw, CELL_H), 8: (hw, hh, CELL_W, CELL_H),
        }.items():
            if mask & bit:
                draw.rectangle((x + x0, y + y0, x + x1 - 1, y + y1 - 1), fill=fg)
        return
    if ch in HLINE:
        draw.line((x, y + hh, x + CELL_W, y + hh), fill=fg, width=1)
        return
    if ch in VLINE:
        draw.line((x + hw, y, x + hw, y + CELL_H), fill=fg, width=1)
        return
    if ch in CORNERS:
        r = hw
        kind = CORNERS[ch]
        cx = x + hw
        cy = y + hh
        if kind == "tl":
            draw.arc((cx, cy, cx + 2 * r, cy + 2 * r), 180, 270, fill=fg)
            draw.line((cx, cy + r, cx, y + CELL_H), fill=fg)
        elif kind == "tr":
            draw.arc((cx - 2 * r, cy, cx, cy + 2 * r), 270, 360, fill=fg)
            draw.line((cx, cy + r, cx, y + CELL_H), fill=fg)
        elif kind == "bl":
            draw.arc((cx, cy - 2 * r, cx + 2 * r, cy), 90, 180, fill=fg)
            draw.line((cx, y, cx, cy - r), fill=fg)
        else:
            draw.arc((cx - 2 * r, cy - 2 * r, cx, cy), 0, 90, fill=fg)
            draw.line((cx, y, cx, cy - r), fill=fg)
        return
    font = fonts[(bool(style.bold), bool(style.italic))]
    ascent, descent = font.getmetrics()
    draw.text((x, y + (CELL_H - ascent - descent) // 2), ch, font=font, fill=fg)
    if style.underline:
        draw.line((x, y + CELL_H - 4, x + CELL_W, y + CELL_H - 4), fill=fg)


def _rasterise(strips, fonts, default_fg, default_bg) -> Image.Image:
    img = Image.new("RGB", (COLS * CELL_W, ROWS * CELL_H), default_bg)
    draw = ImageDraw.Draw(img)
    for row, strip in enumerate(strips[:ROWS]):
        col = 0
        for seg in strip:
            if seg.control:
                continue
            style = seg.style
            fg = _rgb(style.color if style else None, default_fg)
            bg = _rgb(style.bgcolor if style else None, default_bg)
            if style and style.reverse:
                fg, bg = bg, fg
            for ch in seg.text:
                if col >= COLS:
                    break
                _draw_cell(draw, fonts, col * CELL_W, row * CELL_H, ch, fg, bg, style or _NULL)
                col += 1
    return img


class _Null:
    bold = italic = underline = reverse = False


_NULL = _Null()


def _window(term: Image.Image, title: str) -> Image.Image:
    w, h = term.width + 2 * PAD, term.height + PAD + TITLE_H + PAD // 2
    out = Image.new("RGB", (w + 2, h + 2), (255, 255, 255))
    out.putalpha(0)
    d = ImageDraw.Draw(out)
    d.rounded_rectangle((0, 0, w, h), radius=14, fill=WINDOW_BG + (255,), outline=(52, 56, 78, 255))
    d.rounded_rectangle((0, 0, w, TITLE_H + 14), radius=14, fill=CHROME_BG + (255,))
    d.rectangle((0, TITLE_H, w, TITLE_H + 14), fill=WINDOW_BG + (255,))
    for i, color in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        cx = 22 + i * 22
        d.ellipse((cx - 6, TITLE_H // 2 - 6, cx + 6, TITLE_H // 2 + 6), fill=color + (255,))
    font = ImageFont.truetype(FONT_CANDIDATES[0], 14) if Path(FONT_CANDIDATES[0]).exists() else None
    tw = d.textlength(title, font=font)
    d.text(((w - tw) / 2, TITLE_H / 2 - 8), title, fill=(160, 166, 190, 255), font=font)
    out.paste(term, (PAD, TITLE_H + PAD // 2))
    bg = Image.new("RGB", out.size, (13, 17, 23))  # GitHub dark canvas behind rounded corners
    bg.paste(out, (0, 0), out)
    return bg


async def _capture(mode: str, fonts) -> list[Image.Image]:

    from talkops.mcp.mcp_info import MCPServerInfo, MCPToolInfo
    from talkops.ui import theme as theme_mod
    from talkops.ui.widgets import welcome

    welcome._collect_skills = lambda: [
        ("k8s-incident-triage", ""), ("terraform-plan-review", ""), ("aws-cost-audit", ""),
        ("helm-upgrade", ""), ("gitops-sync", ""), ("postmortem", ""),
    ]
    welcome._collect_subagents = lambda: [
        ("sre-investigator", ""), ("iac-reviewer", ""), ("finops-analyst", ""), ("security-auditor", ""),
    ]
    welcome._project_display = lambda: "~/work/platform-infra"

    icon_holder: dict[str, Widget] = {}

    class _IconSlot(Widget):
        def render(self) -> str:  # placeholder; the PNG is composited after rasterising
            """Render the widget content."""
            return ""

    if mode == "image":
        def _make():
            w = _IconSlot(id="welcome-logo-image")
            w.styles.width = 20
            w.styles.height = 8
            icon_holder["w"] = w
            return w

        welcome.create_logo_image_widget = _make
    else:
        welcome.create_logo_image_widget = lambda: None

    class Harness(App[None]):
        """Represent harness."""
        def __init__(self) -> None:
            """Initialize Harness."""
            super().__init__()
            theme_mod.register_app_themes(self)
            self.banner = welcome.WelcomeBanner(user_email="you@company.com", user_tier="team")

        def get_theme_variable_defaults(self) -> dict[str, str]:
            """Retrieve theme variable defaults."""
            colors = theme_mod.get_theme_colors(self)
            return theme_mod.get_css_variable_defaults(colors=colors)

        def compose(self) -> ComposeResult:
            """Compose and yield child widgets for this component."""
            yield self.banner

    colors = theme_mod.get_theme_colors(None)
    default_fg = _hex(colors.foreground)
    default_bg = _hex(colors.background)
    icon = Image.open(ROOT / "src/talkops/ui/assets/talkops-icon.png").convert("RGBA")

    frames: list[Image.Image] = []
    app = Harness()
    async with app.run_test(size=(COLS, ROWS)) as pilot:
        banner = app.banner
        banner.update_model("anthropic:claude-sonnet-4-5", "high")
        banner.update_cloud("production", "us-east-1", "aws")
        left = banner.query_one(welcome._LeftPanel)
        if left._anim_timer is not None:
            left._anim_timer.stop()
            left._anim_timer = None
        await pilot.pause()

        async def grab() -> None:
            """Execute grab."""
            left.refresh()
            await pilot.pause()
            img = _rasterise(app.screen._compositor.render_strips(), fonts, default_fg, default_bg)
            if "w" in icon_holder:
                region = icon_holder["w"].region
                box_w, box_h = region.width * CELL_W, region.height * CELL_H
                scale = min(box_w / icon.width, box_h / icon.height)
                ic = icon.resize((round(icon.width * scale), round(icon.height * scale)), Image.LANCZOS)
                ox = region.x * CELL_W + (box_w - ic.width) // 2
                oy = region.y * CELL_H + (box_h - ic.height) // 2
                img.paste(ic, (ox, oy), ic)
            frames.append(img)

        sweep = list(range(-6, 66, 2))
        banner.set_starting()
        for x in sweep:
            left.beam_x = x
            await grab()
        banner.set_connected()
        banner.update_mcp([
            MCPServerInfo(name="kubernetes", tools=tuple(MCPToolInfo(f"k8s_{i}") for i in range(18))),
            MCPServerInfo(name="aws", tools=tuple(MCPToolInfo(f"aws_{i}") for i in range(14))),
            MCPServerInfo(name="github", tools=tuple(MCPToolInfo(f"gh_{i}") for i in range(9))),
        ])
        for _ in range(2):
            for x in sweep:
                left.beam_x = x
                await grab()
    return frames


def _hex(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _crop_to_card(frames: list[Image.Image]) -> list[Image.Image]:
    """Trim empty terminal rows below the card (identical box for every frame)."""
    from PIL import ImageChops

    bg = frames[-1].getpixel((0, frames[-1].height - 1))
    diff = ImageChops.difference(frames[-1], Image.new("RGB", frames[-1].size, bg))
    bbox = diff.getbbox() or (0, 0, *frames[-1].size)
    box = (0, max(bbox[1] - CELL_H // 2, 0), frames[-1].width, min(bbox[3] + CELL_H // 2, frames[-1].height))
    return [f.crop(box) for f in frames]


def main() -> None:
    """Execute main."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--mode", choices=("image", "blocks"), default="image")
    parser.add_argument("--out", default=str(ROOT / "docs/assets/talkops-welcome-banner.gif"))
    parser.add_argument("--font", default=None)
    args = parser.parse_args()

    fonts = _fonts(args.font)
    frames = asyncio.run(_capture(args.mode, fonts))
    frames = [_window(f, "talkops") for f in _crop_to_card(frames)]

    # One shared palette for all frames -> no colour flicker between frames.
    palette = frames[-1].quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    pframes = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    durations = [FRAME_MS] * len(pframes)
    durations[-1] = 1600  # hold the final 'ready' frame
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    pframes[0].save(
        out, save_all=True, append_images=pframes[1:], duration=durations, loop=0, optimize=True, disposal=1
    )
    frames[-1].save(out.with_suffix(".png"))
    print(f"wrote {out} ({len(pframes)} frames, {out.stat().st_size // 1024} KiB) + {out.with_suffix('.png').name}")


if __name__ == "__main__":
    main()
