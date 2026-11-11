"""Draw a labeled coordinate grid on screenshots sent to vision models."""

from __future__ import annotations

import base64
import io

from PIL import Image, ImageDraw, ImageFont


def add_grid_overlay(
    base64_png: str,
    width: int,
    height: int,
    step: int = 100,
) -> str:
    """Return a PNG (base64) with a 0-999 coordinate grid and labels.

    The grid is intentionally light so the underlying UI remains readable.
    Labels are placed along the top and left edges.
    """
    raw = base64.b64decode(base64_png)
    image = Image.open(io.BytesIO(raw)).convert("RGBA")
    width, height = image.size

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    font_size = max(14, min(28, width // 50))
    try:
        font = ImageFont.load_default(size=font_size)
    except TypeError:  # older Pillow
        font = ImageFont.load_default()

    line_width = max(1, width // 540)
    for value in range(0, 1001, step):
        x = int((width - 1) * value / 1000)
        y = int((height - 1) * value / 1000)
        color = (255, 60, 60, 120) if value % 200 == 0 else (0, 150, 255, 90)
        draw.line([(x, 0), (x, height - 1)], fill=color, width=line_width)
        draw.line([(0, y), (width - 1, y)], fill=color, width=line_width)
        if value >= 1000:
            continue

        label = str(value)
        bbox = draw.textbbox((0, 0), label, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        # Top edge label
        draw.rectangle(
            [x + 2, 1, x + text_width + 8, text_height + 8],
            fill=(0, 0, 0, 170),
        )
        draw.text((x + 5, 2), label, font=font, fill=(255, 255, 255, 240))

        # Left edge label
        draw.rectangle(
            [1, y + 2, text_width + 10, y + text_height + 8],
            fill=(0, 0, 0, 170),
        )
        draw.text((3, y + 3), label, font=font, fill=(255, 255, 255, 240))

    merged = Image.alpha_composite(image, overlay).convert("RGB")
    output = io.BytesIO()
    merged.save(output, format="PNG", optimize=False, compress_level=3)
    return base64.b64encode(output.getvalue()).decode("ascii")