"""The things drawn over the footage: the pointer, its halo, click ripples and captions."""

from __future__ import annotations

from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

ACCENT = (255, 196, 0)
RIPPLE_S = 0.45
_SUPERSAMPLE = 4

# An arrow pointer with its tip at the origin, on a grid 12 wide and 19 tall.
_ARROW = [(0, 0), (0, 16.2), (3.9, 12.6), (6.6, 18.8), (9.1, 17.7), (6.4, 11.7), (11.6, 11.7)]
_ARROW_PAD = 2  # room around the arrow for its outline


@lru_cache(maxsize=8)
def pointer(unit: float) -> tuple[Image.Image, int]:
    """The pointer sprite, `unit` output pixels per grid square, and the offset of its tip."""
    scale = unit * _SUPERSAMPLE
    size = (round((12 + 2 * _ARROW_PAD) * scale), round((19 + 2 * _ARROW_PAD) * scale))
    big = Image.new("RGBA", size, (0, 0, 0, 0))
    points = [((x + _ARROW_PAD) * scale, (y + _ARROW_PAD) * scale) for x, y in _ARROW]
    draw = ImageDraw.Draw(big)
    draw.line([*points, points[0]], fill=(255, 255, 255, 255), width=max(round(2.4 * scale), 1), joint="curve")
    draw.polygon(points, fill=(20, 20, 20, 255))
    sprite = big.resize((size[0] // _SUPERSAMPLE, size[1] // _SUPERSAMPLE), Image.LANCZOS)
    return sprite, round(_ARROW_PAD * unit)


@lru_cache(maxsize=8)
def halo(radius: int) -> Image.Image:
    """A soft disc that sits behind the pointer so the eye can find it."""
    side = radius * 2 * _SUPERSAMPLE
    big = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    ImageDraw.Draw(big).ellipse((0, 0, side - 1, side - 1), fill=(*ACCENT, 80))
    return big.resize((radius * 2, radius * 2), Image.LANCZOS)


def ripple(progress: float, max_radius: int) -> Image.Image:
    """A ring spreading out from a click and fading. `progress` runs from 0 to 1."""
    side = max_radius * 2 * _SUPERSAMPLE
    big = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    radius = (0.35 + 0.65 * progress) * side / 2
    width = max(round(side * 0.035), 1)
    alpha = round(230 * (1 - progress))
    centre = side / 2
    ImageDraw.Draw(big).ellipse((centre - radius, centre - radius, centre + radius, centre + radius), outline=(*ACCENT, alpha), width=width)
    return big.resize((max_radius * 2, max_radius * 2), Image.LANCZOS)


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines: list[str] = []
    for word in text.split():
        if lines and font.getlength(f"{lines[-1]} {word}") <= max_width:
            lines[-1] = f"{lines[-1]} {word}"
        else:
            lines.append(word)
    return lines


@lru_cache(maxsize=64)
def caption(text: str, frame_width: int, font_px: int, font_path: str | None = None) -> Image.Image:
    """A caption as it appears on screen: white text, centred, on a dark rounded panel."""
    font = ImageFont.truetype(font_path, font_px) if font_path else ImageFont.load_default(size=font_px)
    lines = _wrap(text, font, round(frame_width * 0.8))
    line_height = round(font_px * 1.3)
    pad_x, pad_y = round(font_px * 0.6), round(font_px * 0.35)
    width = round(max(font.getlength(line) for line in lines)) + 2 * pad_x
    height = line_height * len(lines) + 2 * pad_y
    panel = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=round(font_px * 0.35), fill=(0, 0, 0, 170))
    for number, line in enumerate(lines):
        draw.text((width / 2, pad_y + line_height * number + line_height / 2), line, font=font, fill=(255, 255, 255, 255), anchor="mm")
    return panel
