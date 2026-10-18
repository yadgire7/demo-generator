"""Product pictures, drawn as SVG: a flat silhouette of the product in the chosen colour."""

from __future__ import annotations

from demogen.testshop.catalog import COLOURS

# Each shape is drawn on a 400 by 400 canvas, filled with the colour (`C`) and outlined in a darker line.
SHAPES = {
    "tshirt": '<path d="M130 70 L170 55 Q200 85 230 55 L270 70 L330 120 L300 160 L270 140 L270 330 L130 330 L130 140 L100 160 L70 120 Z"/>',
    "shirt": '<path d="M130 70 L175 55 L200 90 L225 55 L270 70 L330 130 L300 165 L272 145 L272 335 L128 335 L128 145 L100 165 L70 130 Z"/>'
    '<path d="M200 90 L200 335" fill="none"/><circle cx="212" cy="140" r="4"/><circle cx="212" cy="190" r="4"/><circle cx="212" cy="240" r="4"/>',
    "hoodie": '<path d="M140 80 Q200 20 260 80 L330 130 L305 230 L275 215 L275 335 L125 335 L125 215 L95 230 L70 130 Z"/>'
    '<path d="M165 90 Q200 130 235 90" fill="none"/><path d="M160 260 L240 260 L250 300 L150 300 Z"/>',
    "jacket": '<path d="M140 70 L180 55 L200 80 L220 55 L260 70 L325 125 L310 335 L215 335 L200 90 L185 335 L90 335 L75 125 Z"/>',
    "shorts": '<path d="M120 110 L280 110 L300 300 L215 300 L200 190 L185 300 L100 300 Z"/><path d="M120 135 L280 135" fill="none"/>',
    "socks": '<path d="M150 70 L210 70 L210 240 Q210 300 150 320 L105 320 Q80 300 105 280 L150 250 Z"/>'
    '<path d="M215 90 L270 90 L270 250 Q270 310 215 330 L180 330" fill="none"/>',
    "beanie": '<path d="M110 260 Q110 110 200 105 Q290 110 290 260 Z"/><rect x="100" y="250" width="200" height="50" rx="10"/><circle cx="200" cy="95" r="22"/>',
    "mug": '<rect x="110" y="110" width="160" height="200" rx="18"/><path d="M270 150 Q330 150 330 210 Q330 270 270 270" fill="none" stroke-width="18"/>',
    "bottle": '<rect x="160" y="60" width="80" height="40" rx="8"/><rect x="140" y="95" width="120" height="250" rx="40"/>',
    "throw": '<rect x="90" y="90" width="220" height="220" rx="10"/><path d="M90 140 L310 140 M90 260 L310 260" fill="none"/>'
    '<path d="M100 310 L100 335 M130 310 L130 335 M160 310 L160 335 M190 310 L190 335 M220 310 L220 335 M250 310 L250 335 M280 310 L280 335" fill="none"/>',
    "cushion": '<path d="M100 100 Q200 80 300 100 Q320 200 300 300 Q200 320 100 300 Q80 200 100 100 Z"/>',
    "candle": '<rect x="130" y="150" width="140" height="180" rx="14"/><path d="M200 150 L200 120" fill="none"/><path d="M200 70 Q220 100 200 118 Q180 100 200 70 Z" fill="#f2b84b"/>',
    "towel": '<rect x="120" y="80" width="160" height="250" rx="6"/><path d="M120 290 L280 290 M120 305 L280 305" fill="none"/>',
    "bag": '<path d="M110 150 L290 150 L275 340 L125 340 Z"/><path d="M160 150 Q160 80 200 80 Q240 80 240 150" fill="none" stroke-width="12"/>',
    "backpack": '<rect x="120" y="100" width="160" height="240" rx="40"/><rect x="145" y="210" width="110" height="90" rx="14"/>'
    '<path d="M170 100 Q170 60 200 60 Q230 60 230 100" fill="none" stroke-width="12"/>',
    "cap": '<path d="M110 230 Q110 120 200 115 Q290 120 290 230 Z"/><path d="M270 225 Q340 225 345 250 L280 252 Z"/><circle cx="200" cy="118" r="8"/>',
    "wallet": '<rect x="90" y="130" width="220" height="150" rx="16"/><path d="M90 175 L310 175" fill="none"/>',
    "scarf": '<path d="M150 60 L230 60 L230 250 L260 340 L210 340 L190 270 L150 270 Z"/><path d="M170 60 L170 270 M210 60 L210 250" fill="none"/>',
}


def _darker(hex_colour: str, amount: float = 0.35) -> str:
    red, green, blue = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(channel * (1 - amount)):02x}" for channel in (red, green, blue))


def product_svg(kind: str, colour: str) -> str:
    fill = COLOURS.get(colour, "#999999")
    line = _darker(fill) if colour not in ("White", "Cream") else "#9a958a"
    shape = SHAPES.get(kind, SHAPES["bag"])
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 400" width="400" height="400">'
        '<rect width="400" height="400" fill="#f3f1ec"/>'
        f'<g fill="{fill}" stroke="{line}" stroke-width="6" stroke-linejoin="round" stroke-linecap="round">{shape}</g>'
        "</svg>"
    )
