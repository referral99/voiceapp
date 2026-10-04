"""Generate brand raster images (PNG) for SpeakFluent.

Produces, into static/chat/images/:
  - og-image.png     1200x630  social share card (used by Open Graph / Twitter)
  - favicon-32.png   32x32     browser-tab icon fallback (for older browsers)
  - favicon-180.png  180x180   apple-touch-icon (iOS home-screen)

Run:  python scripts/generate_brand_images.py

Only depends on Pillow. Re-run any time the branding changes. The source of
truth for colors/wordmark is kept here so the raster assets stay in sync.
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.normpath(os.path.join(HERE, "..", "static", "chat", "images"))

# Brand palette (matches CSS --primary / --primary-dark used across the site).
PRIMARY = (99, 102, 241)       # #6366f1
PRIMARY_DARK = (67, 56, 202)   # #4338ca
WHITE = (255, 255, 255)


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Find a usable TrueType font across platforms, falling back to default."""
    candidates = []
    if bold:
        candidates += [
            "arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            r"C:\Windows\Fonts\arialbd.ttf",
            r"C:\Windows\Fonts\segoeuib.ttf",
        ]
    else:
        candidates += [
            "arial.ttf", "DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            r"C:\Windows\Fonts\arial.ttf",
            r"C:\Windows\Fonts\segoeui.ttf",
        ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _vertical_gradient(size, top, bottom):
    """Create a vertical gradient background image."""
    w, h = size
    base = Image.new("RGB", size, top)
    top_r, top_g, top_b = top
    bot_r, bot_g, bot_b = bottom
    draw = ImageDraw.Draw(base)
    for y in range(h):
        t = y / max(h - 1, 1)
        r = int(top_r + (bot_r - top_r) * t)
        g = int(top_g + (bot_g - top_g) * t)
        b = int(top_b + (bot_b - top_b) * t)
        draw.line([(0, y), (w, y)], fill=(r, g, b))
    return base


def _rounded_rect(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def _speech_bubble(draw, x, y, w, h, radius, fill):
    """A rounded rectangle with a small tail at the bottom-left (chat bubble)."""
    _rounded_rect(draw, (x, y, x + w, y + h), radius, fill)
    # Tail
    tail = [
        (x + int(w * 0.18), y + h - 2),
        (x + int(w * 0.18), y + h + int(h * 0.28)),
        (x + int(w * 0.40), y + h - 2),
    ]
    draw.polygon(tail, fill=fill)


def generate_og_image():
    W, H = 1200, 630
    img = _vertical_gradient((W, H), PRIMARY, PRIMARY_DARK)
    draw = ImageDraw.Draw(img)

    # Speech-bubble mark with "S" monogram (top-left).
    bx, by, bw, bh = 110, 150, 230, 165
    _speech_bubble(draw, bx, by, bw, bh, radius=42, fill=WHITE)
    mono = _load_font(120, bold=True)
    _draw_centered(draw, "S", (bx + bw / 2, by + bh / 2 - 6), mono, PRIMARY_DARK)

    # Wordmark + tagline.
    brand = _load_font(92, bold=True)
    tag = _load_font(40, bold=False)
    draw.text((112, 400), "SpeakFluent", font=brand, fill=WHITE)
    draw.text((116, 500),
              "Practice spoken English with real partners", font=tag,
              fill=(235, 235, 255))
    draw.text((116, 550),
              "through live voice calls and chat.", font=tag,
              fill=(235, 235, 255))

    out = os.path.join(OUT_DIR, "og-image.png")
    img.save(out, "PNG")
    print("wrote", out, img.size)


def _draw_centered(draw, text, center, font, fill):
    cx, cy = center
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    draw.text((cx - (r - l) / 2 - l, cy - (b - t) / 2 - t), text, font=font, fill=fill)


def generate_favicon(size: int, filename: str):
    img = _vertical_gradient((size, size), PRIMARY, PRIMARY_DARK)
    draw = ImageDraw.Draw(img)
    # Rounded "app icon" look.
    mono = _load_font(int(size * 0.62), bold=True)
    _draw_centered(draw, "S", (size / 2, size / 2 - size * 0.02), mono, WHITE)
    out = os.path.join(OUT_DIR, filename)
    img.save(out, "PNG")
    print("wrote", out, img.size)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    generate_og_image()
    generate_favicon(32, "favicon-32.png")
    generate_favicon(180, "favicon-180.png")


if __name__ == "__main__":
    main()
