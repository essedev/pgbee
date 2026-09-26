"""Build the pgbee brand files from the A2 mascot (see logo-options.html).

    uv run --no-project --with fonttools python assets/brand/build.py FONT_DIR

FONT_DIR holds JetBrainsMono-ExtraBold.ttf and JetBrainsMono-Medium.ttf (OFL, from
github.com/JetBrains/JetBrainsMono). Text is converted to paths, so the SVGs need no font.
PNGs are rendered with rsvg-convert.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

OUT = Path(__file__).parent
HONEY, HONEY_LIGHT, WING, INK = "#FFC21A", "#FFD95A", "#D6ECFF", "#1D1A16"
CREAM, NIGHT = "#FFF8E7", "#17140F"
THEMES = {
    "light": {"line": INK, "pg": "#8A7F6A", "bee": INK, "tag": "#8A7F6A", "bg": CREAM},
    "dark": {"line": "#FFF3D6", "pg": "#A89F8C", "bee": "#FFF3D6", "tag": "#A89F8C", "bg": NIGHT},
}


def mark(line: str) -> str:
    """The A2 bee on a 120x120 grid: a database cylinder with stripes, dot eyes, a small smile."""
    return f"""<g stroke="{line}" stroke-width="3.5" stroke-linejoin="round" stroke-linecap="round">
  <path d="M51 38 C47 28 43 22 38 16" fill="none"/>
  <path d="M69 38 C73 28 77 22 82 16" fill="none"/>
  <circle cx="37" cy="14" r="4.5" fill="{line}"/>
  <circle cx="83" cy="14" r="4.5" fill="{line}"/>
  <ellipse cx="22" cy="54" rx="18" ry="11" transform="rotate(-28 22 54)" fill="{WING}"/>
  <ellipse cx="98" cy="54" rx="18" ry="11" transform="rotate(28 98 54)" fill="{WING}"/>
  <path d="M55 104 L60 115 L65 104 Z" fill="{line}"/>
  <path d="M30 44 V96 A30 10 0 0 0 90 96 V44 Z" fill="{HONEY}"/>
</g>
<path d="M31.8 72 A30 10 0 0 0 88.2 72 V80 A30 10 0 0 1 31.8 80 Z" fill="{INK}"/>
<path d="M31.8 86 A30 10 0 0 0 88.2 86 V92 A30 10 0 0 1 31.8 92 Z" fill="{INK}"/>
<ellipse cx="60" cy="44" rx="30" ry="10" fill="{HONEY_LIGHT}" stroke="{line}" stroke-width="3.5"/>
<circle cx="50" cy="62" r="4.2" fill="{INK}"/>
<circle cx="70" cy="62" r="4.2" fill="{INK}"/>
<circle cx="51.3" cy="60.8" r="1.1" fill="#fff"/>
<circle cx="71.3" cy="60.8" r="1.1" fill="#fff"/>
<path d="M57 68.5 Q60 71 63 68.5" fill="none" stroke="{INK}" stroke-width="2.4" stroke-linecap="round"/>"""


def text_paths(font: TTFont, text: str, size: float, x: float, baseline: float,
               tracking: float = 0.0) -> tuple[str, float]:
    """SVG path data for text set at (x, baseline). Returns (d, advance width)."""
    glyphs = font.getGlyphSet()
    cmap = font.getBestCmap()
    scale = size / font["head"].unitsPerEm
    parts: list[str] = []
    cursor = x
    for ch in text:
        name = cmap[ord(ch)]
        pen = SVGPathPen(glyphs)
        glyphs[name].draw(TransformPen(pen, (scale, 0, 0, -scale, cursor, baseline)))
        parts.append(pen.getCommands())
        cursor += glyphs[name].width * scale + tracking * size
    return " ".join(p for p in parts if p), cursor - x


def svg(width: float, height: float, body: str, bg: str | None = None) -> str:
    rect = f'<rect width="100%" height="100%" fill="{bg}"/>\n' if bg else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:g} {height:g}" '
            f'width="{width:g}" height="{height:g}">\n{rect}{body}\n</svg>\n')


def lockup(bold: TTFont, theme: dict[str, str], size: float = 72) -> tuple[str, float, float]:
    """Mark plus the "pgbee" wordmark, "pg" muted. Returns (body, width, height)."""
    mark_size = size * 1.7
    gap = size * 0.28
    baseline = mark_size * 0.62
    x = mark_size + gap
    d_pg, w_pg = text_paths(bold, "pg", size, x, baseline, tracking=-0.02)
    d_bee, w_bee = text_paths(bold, "bee", size, x + w_pg, baseline, tracking=-0.02)
    s = mark_size / 120
    body = (f'<g transform="scale({s:g})">{mark(theme["line"])}</g>\n'
            f'<path d="{d_pg}" fill="{theme["pg"]}"/>\n<path d="{d_bee}" fill="{theme["bee"]}"/>')
    return body, x + w_pg + w_bee + size * 0.08, mark_size  # room for the last glyph


def main(font_dir: Path) -> None:
    bold = TTFont(font_dir / "JetBrainsMono-ExtraBold.ttf")
    medium = TTFont(font_dir / "JetBrainsMono-Medium.ttf")
    for name, theme in THEMES.items():
        suffix = "" if name == "light" else "-dark"
        (OUT / f"pgbee-mark{suffix}.svg").write_text(svg(120, 120, mark(theme["line"])))
        body, w, h = lockup(bold, theme)
        (OUT / f"pgbee-logo{suffix}.svg").write_text(svg(round(w), round(h), body))

    # GitHub social preview, 1280x640: lockup centered, tagline below.
    theme = THEMES["light"]
    body, w, h = lockup(bold, theme, size=120)
    ox, oy = (1280 - w) / 2, 150
    tag = "AI-derived columns for PostgreSQL"
    d_tag, w_tag = text_paths(medium, tag, 38, 0, 0)
    social = (f'<g transform="translate({ox:g} {oy:g})">{body}</g>\n'
              f'<g transform="translate({(1280 - w_tag) / 2:g} {oy + h + 70:g})">'
              f'<path d="{d_tag}" fill="{theme["tag"]}"/></g>')
    (OUT / "pgbee-social.svg").write_text(svg(1280, 640, social, bg=theme["bg"]))

    # Avatar: the mark on cream, with some air around it.
    avatar = f'<g transform="translate(14 14) scale(0.8)">{mark(INK)}</g>'
    (OUT / "pgbee-avatar.svg").write_text(svg(120, 120, avatar, bg=CREAM))

    png = OUT / "png"
    png.mkdir(exist_ok=True)
    renders = [(f"pgbee-mark-{n}.png", "pgbee-mark.svg", n, n) for n in (16, 32, 64, 128, 256, 512)]
    renders += [("pgbee-avatar-512.png", "pgbee-avatar.svg", 512, 512),
                ("pgbee-social.png", "pgbee-social.svg", 1280, 640)]
    for out, src, w, h in renders:
        subprocess.run(["rsvg-convert", "-w", str(w), "-h", str(h), "-o", str(png / out),
                        str(OUT / src)], check=True)
    print("written:", ", ".join(sorted(p.name for p in OUT.glob("pgbee-*.svg"))), "and png/")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
