#!/usr/bin/env python3
"""Render the static cover delivered with an article-video job."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "assets" / "header1.jpg"
LANDSCAPE_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "assets" / "header2.png"
FONT_PATH = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "SourceHanSansSC-Regular.otf"

# Coordinates are proportional to header1.jpg, preserving the supplied cover
# template's layout even when it is rendered at a smaller delivery size.
IMAGE_REGION = (0.068, 0.332, 0.932, 0.724)
GRADE_REGION = (0.12, 0.754, 0.88, 0.862)
LANDSCAPE_GRADE_REGION = (0.09, 0.520, 0.91, 0.660)
LANDSCAPE_GRADE_FILL = (42, 42, 42)
# The portrait cover grade is intentionally smaller than the supplied label
# region so it has more breathing room beneath the main visual.
COVER_GRADE_FONT_SCALE = 0.82


def load_font(size: int) -> ImageFont.FreeTypeFont:
    if not FONT_PATH.is_file():
        raise FileNotFoundError(f"Bundled Source Han Sans font is missing: {FONT_PATH}")
    return ImageFont.truetype(str(FONT_PATH), size=size)


def cover(image: Image.Image, width: int, height: int) -> Image.Image:
    """Scale the cover source to fill the template's supplied image window."""
    scale = max(width / image.width, height / image.height)
    resized = image.resize(
        (round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS
    )
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def fitted_font(text: str, max_width: int, max_height: int) -> ImageFont.FreeTypeFont:
    for size in range(max_height, 12, -2):
        font = load_font(size)
        bbox = font.getbbox(text)
        if bbox[2] - bbox[0] <= max_width and bbox[3] - bbox[1] <= max_height:
            return font
    return load_font(12)


def render_landscape_cover(
    product_grade: str,
    output_path: Path,
    template_path: Path = LANDSCAPE_TEMPLATE_PATH,
) -> None:
    """Render the standalone landscape cover with the current product grade."""
    if not product_grade.strip():
        raise ValueError("product_grade is required")
    if not template_path.is_file() or template_path.stat().st_size == 0:
        raise ValueError(f"Landscape cover template is not a non-empty file: {template_path}")

    template = Image.open(template_path).convert("RGB")
    width, height = template.size
    x1, y1, x2, y2 = (
        round(value * axis)
        for value, axis in zip(
            LANDSCAPE_GRADE_REGION, (width, height, width, height)
        )
    )
    # header2.png is a flattened template. Clear only the face of its central
    # white nameplate, preserving the shadow and border before redrawing text.
    template.paste((255, 255, 255), (x1, y1, x2, y2))
    draw = ImageDraw.Draw(template)
    font = fitted_font(product_grade.strip(), x2 - x1, y2 - y1)
    left, top, right, bottom = draw.textbbox((0, 0), product_grade.strip(), font=font)
    draw.text(
        (x1 + (x2 - x1 - (right - left)) // 2 - left, y1 + (y2 - y1 - (bottom - top)) // 2 - top),
        product_grade.strip(),
        font=font,
        fill=LANDSCAPE_GRADE_FILL,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    template.save(output_path)


def render(image_path: Path, product_grade: str, output_path: Path, template_path: Path = TEMPLATE_PATH) -> None:
    if not product_grade.strip():
        raise ValueError("product_grade is required")
    if not image_path.is_file() or image_path.stat().st_size == 0:
        raise ValueError(f"Cover source image is not a non-empty file: {image_path}")
    if not template_path.is_file() or template_path.stat().st_size == 0:
        raise ValueError(f"Cover template is not a non-empty file: {template_path}")

    template = Image.open(template_path).convert("RGB")
    source = Image.open(image_path).convert("RGB")
    width, height = template.size
    x1, y1, x2, y2 = (round(value * axis) for value, axis in zip(IMAGE_REGION, (width, height, width, height)))
    displayed = cover(source, x2 - x1, y2 - y1)
    template.paste(displayed, (x1, y1))

    gx1, gy1, gx2, gy2 = (round(value * axis) for value, axis in zip(GRADE_REGION, (width, height, width, height)))
    draw = ImageDraw.Draw(template)
    font = fitted_font(
        product_grade.strip(),
        gx2 - gx1,
        round((gy2 - gy1) * COVER_GRADE_FONT_SCALE),
    )
    left, top, right, bottom = draw.textbbox((0, 0), product_grade.strip(), font=font)
    draw.text(
        (gx1 + (gx2 - gx1 - (right - left)) // 2 - left, gy1 + (gy2 - gy1 - (bottom - top)) // 2 - top),
        product_grade.strip(), font=font, fill=(12, 12, 12),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    template.save(output_path, quality=95)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True, help="First module visual")
    parser.add_argument("--grade", required=True, help="Product grade to draw below the image")
    parser.add_argument("--output", type=Path, required=True, help="Output JPG path")
    parser.add_argument("--template", type=Path, default=TEMPLATE_PATH)
    args = parser.parse_args()
    try:
        render(args.image.resolve(), args.grade, args.output.resolve(), args.template.resolve())
        print(args.output.resolve())
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
