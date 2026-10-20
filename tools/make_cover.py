#!/usr/bin/env python3
"""Generate the repository cover image.

Drawn with Pillow rather than an image model: every number on the card has to
match the real output (214 tests, 21 mutants), and a generative model would
quietly invent different ones. The terminal block is a verbatim copy of a real
run, kept in ``REAL_RUN`` below.

Output: ``docs/cover.png`` (1280x640, the GitHub social-preview size).
Run:    python tools/make_cover.py
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 640
BG = (26, 22, 20)          # espresso
PANEL = (38, 32, 29)
INK = (240, 234, 226)      # warm off-white
MUTED = (166, 152, 140)
AMBER = (214, 158, 74)     # accent
RED = (196, 92, 62)        # terracotta, used for "--"
GREEN = (122, 156, 108)

# Verbatim console output from `doit all` in examples/codeaudit.
REAL_RUN = [
    ("$ doit all", INK),
    (".  lint:src/sample_pkg/calc.py", AMBER),
    (".  summary", AMBER),
    (".  gate", AMBER),
    ("", INK),
    ("$ rm -rf build audit .doit.db   # fresh checkout", MUTED),
    ("$ doit all", INK),
    ("-- lint:src/sample_pkg/calc.py", GREEN),
    ("-- summary", GREEN),
    ("-- gate", GREEN),
]

FACTS = [
    ("214", "tests passing"),
    ("21", "mutants, all killed"),
    ("0", "runtime deps"),
    ("MIT", "licence"),
]


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """A monospace face if we can find one, else Pillow's built-in bitmap font."""
    candidates = (
        ["C:/Windows/Fonts/consolab.ttf", "C:/Windows/Fonts/consola.ttf"]
        if bold
        else ["C:/Windows/Fonts/consola.ttf", "C:/Windows/Fonts/consolab.ttf"]
    )
    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/System/Library/Fonts/Menlo.ttc",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def build() -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # a single warm hairline, no gradients: the palette stays restrained
    d.rectangle([0, 0, W, 4], fill=AMBER)

    title = _font(46, bold=True)
    sub = _font(21)
    small = _font(15)
    mono = _font(17)

    d.text((64, 62), "doit-port", font=title, fill=INK)
    d.text((64, 124), "Portable build receipts for doit", font=sub, fill=AMBER)
    d.text(
        (64, 158),
        "Carry your incremental build cache across machines and CI runs.",
        font=small,
        fill=MUTED,
    )

    # fact strip
    x = 64
    for value, label in FACTS:
        d.text((x, 202), value, font=_font(30, bold=True), fill=INK)
        d.text((x, 242), label, font=small, fill=MUTED)
        x += 200

    # terminal panel, verbatim output.
    # Height is derived from the line count, not hard-coded: the first version
    # used a fixed box and silently clipped the last two lines of the transcript.
    line_h = 22
    box = (64, 292, W - 64, 292 + 42 + line_h * len(REAL_RUN) + 14)
    assert box[3] < H - 56, "terminal panel would collide with the footer"
    d.rectangle(box, fill=PANEL, outline=(58, 50, 44), width=1)
    for i, colour in enumerate((RED, AMBER, GREEN)):
        d.ellipse([box[0] + 16 + i * 20, box[1] + 14, box[0] + 26 + i * 20, box[1] + 24], fill=colour)
    y = box[1] + 40
    for line, colour in REAL_RUN:
        if line:
            d.text((box[0] + 22, y), line, font=mono, fill=colour)
        y += line_h

    d.text(
        (64, H - 44),
        "MIT · doit.COMMAND entry point · uptodate protocol · zero patches",
        font=small,
        fill=MUTED,
    )
    return img


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="docs/cover.png")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    target = root / args.out
    target.parent.mkdir(parents=True, exist_ok=True)
    image = build()
    image.save(target, format="PNG", optimize=True)
    size = target.stat().st_size
    print(f"wrote {target} ({size} bytes, {W}x{H})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
