#!/usr/bin/env python3
"""Render article-backed technical parameter data as a 14:10 PNG table."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


CANVAS_SIZE = (1400, 1000)
BACKGROUND = (255, 255, 255)
INK = (25, 25, 25)
HEADER = (37, 70, 184)
ROW_A = (255, 255, 255)
ROW_B = (248, 249, 252)
GRID = (187, 190, 198)
FONT_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"
SOURCE_HAN_REGULAR = FONT_DIR / "SourceHanSansSC-Regular.otf"


def load_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    if not SOURCE_HAN_REGULAR.is_file():
        raise FileNotFoundError(f"Bundled Source Han Sans font is missing: {SOURCE_HAN_REGULAR}")
    return ImageFont.truetype(str(SOURCE_HAN_REGULAR), size=size)


def text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    left, _, right, _ = draw.textbbox((0, 0), text, font=font)
    return right - left


def centered_x(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, center: int) -> int:
    return center - text_width(draw, text, font) // 2


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """Wrap mixed Chinese and Latin table content without truncating it."""
    lines: list[str] = []
    current = ""
    for character in text:
        candidate = current + character
        if current and text_width(draw, candidate, font) > width:
            lines.append(current.rstrip())
            current = character.lstrip()
        else:
            current = candidate
    if current:
        lines.append(current.rstrip())
    return lines or [text]


def draw_centered_lines(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    center_x: int,
    top: int,
    height: int,
    fill: tuple[int, int, int],
) -> None:
    line_height = font.getbbox("Hg")[3] - font.getbbox("Hg")[1] + 5
    y = top + (height - line_height * len(lines)) // 2
    for line in lines:
        draw.text((centered_x(draw, line, font, center_x), y), line, fill=fill, font=font)
        y += line_height


def read_table(path: Path) -> tuple[str, list[str], list[list[str]]]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8-sig"))
    title = str(data.get("title") or "Technical Parameters").strip()
    columns = [str(cell).strip() for cell in data.get("columns", [])]
    rows = [[str(cell).strip() for cell in row] for row in data.get("rows", [])]
    if not title or len(columns) not in (2, 3) or not rows or len(rows) > 5:
        raise ValueError("table requires a title, 2-3 columns, and 1-5 rows")
    if any(len(row) != len(columns) or any(not cell for cell in row) for row in rows):
        raise ValueError("every table row must contain a non-empty value for every column")
    return title, columns, rows


def render(title: str, columns: list[str], rows: list[list[str]], output: Path) -> None:
    image = Image.new("RGB", CANVAS_SIZE, BACKGROUND)
    draw = ImageDraw.Draw(image)
    title_font = load_font(49, bold=True)
    while title_font.size > 24 and text_width(draw, title, title_font) > 1200:
        title_font = load_font(title_font.size - 2, bold=True)
    title_y = 110
    draw.text((centered_x(draw, title, title_font, CANVAS_SIZE[0] // 2), title_y), title, fill=INK, font=title_font)

    left, right, top, bottom = 165, 1235, 250, 885
    table_width = right - left
    column_widths = [table_width // len(columns)] * len(columns)
    column_widths[-1] += table_width - sum(column_widths)
    row_height = (bottom - top) // (len(rows) + 1)
    cell_font = load_font(30)
    while cell_font.size >= 16:
        header_font = load_font(min(31, cell_font.size + 2), bold=True)
        header_lines = [
            wrap_text(draw, label, header_font, column_widths[index] - 38)
            for index, label in enumerate(columns)
        ]
        cell_lines = [
            [wrap_text(draw, cell, cell_font, column_widths[index] - 38) for index, cell in enumerate(row)]
            for row in rows
        ]
        cell_line_height = cell_font.getbbox("Hg")[3] - cell_font.getbbox("Hg")[1] + 5
        header_line_height = header_font.getbbox("Hg")[3] - header_font.getbbox("Hg")[1] + 5
        if (
            max(len(lines) for lines in header_lines) * header_line_height <= row_height - 12
            and all(max(len(lines) for lines in row) * cell_line_height <= row_height - 12 for row in cell_lines)
        ):
            break
        cell_font = load_font(cell_font.size - 2)
    else:
        raise ValueError("table content cannot fit the fixed 14:10 table layout")

    y = top
    draw.rectangle((left, y, right, y + row_height), fill=HEADER)
    for index, label in enumerate(columns):
        x = left + sum(column_widths[:index])
        center = x + column_widths[index] // 2
        draw_centered_lines(draw, header_lines[index], header_font, center, y, row_height, (255, 255, 255))
    y += row_height
    for row_index, row in enumerate(rows):
        fill = ROW_A if row_index % 2 == 0 else ROW_B
        draw.rectangle((left, y, right, y + row_height), fill=fill)
        for index, cell in enumerate(row):
            x = left + sum(column_widths[:index])
            center = x + column_widths[index] // 2
            draw_centered_lines(draw, cell_lines[row_index][index], cell_font, center, y, row_height, INK)
        y += row_height

    x = left
    for width in column_widths[:-1]:
        x += width
        draw.line((x, top, x, bottom), fill=GRID, width=2)
    for line_y in range(top, bottom + 1, row_height):
        draw.line((left, line_y, right, line_y), fill=GRID, width=2)
    draw.rectangle((left, top, right, bottom), outline=GRID, width=2)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        title, columns, rows = read_table(args.input)
        render(title, columns, rows, args.output)
        print(args.output.resolve())
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
