#!/usr/bin/env python3
"""Apply the bundled logo to the upper-left corner of a source image."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image


DEFAULT_LOGO = Path(__file__).resolve().parents[1] / "assets" / "logo.png"


def apply_logo(image_path: Path, output_path: Path, logo_path: Path = DEFAULT_LOGO) -> None:
    if not image_path.is_file() or image_path.stat().st_size == 0:
        raise ValueError(f"Image is not a non-empty file: {image_path}")
    if not logo_path.is_file() or logo_path.stat().st_size == 0:
        raise ValueError(f"Logo is not a non-empty file: {logo_path}")

    # Preserve the source image. Jianying applies the sole background blur on
    # its 9:16 canvas after this logo treatment.
    image = Image.open(image_path).convert("RGBA")
    logo = Image.open(logo_path).convert("RGBA")
    target_width = min(round(image.width * 0.13), 180)
    target_width = max(target_width, 72)
    target_height = round(logo.height * target_width / logo.width)
    logo = logo.resize((target_width, target_height), Image.Resampling.LANCZOS)
    margin = max(round(image.width * 0.025), 18)
    image.alpha_composite(logo, dest=(margin, margin))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--logo", type=Path, default=DEFAULT_LOGO)
    args = parser.parse_args()
    try:
        apply_logo(args.image, args.output, args.logo)
        print(args.output.resolve())
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
