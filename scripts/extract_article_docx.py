#!/usr/bin/env python3
"""Extract a complete user-supplied DOCX article into a source record."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from docx import Document
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph


def document_text(path: Path) -> tuple[str, str]:
    document = Document(path)
    parts: list[str] = []
    title = ""

    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            text = Paragraph(child, document).text.strip()
            if not text:
                continue
            if not title:
                title = text
            parts.append(text)
        elif isinstance(child, CT_Tbl):
            rows = []
            for row in Table(child, document).rows:
                cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                parts.append("\n".join(rows))

    if not parts:
        raise ValueError("The DOCX does not contain readable article text")
    return title, "\n\n".join(parts)


def write_json_atomically(payload: dict[str, object], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=output.parent, prefix=f"{output.name}.", suffix=".tmp", text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temp_name, output)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Source DOCX article")
    parser.add_argument("--output", required=True, type=Path, help="article.json path")
    parser.add_argument("--title", help="Optional explicit title")
    args = parser.parse_args()

    source = args.input.expanduser().resolve()
    if not source.is_file():
        parser.error(f"DOCX file not found: {source}")
    if source.suffix.lower() != ".docx":
        parser.error("input must be a .docx file")

    derived_title, body_text = document_text(source)
    payload = {
        "source_type": "user_provided_text_or_docx",
        "title": args.title.strip() if args.title else derived_title,
        "author": None,
        "original_url": None,
        "body_text": body_text,
        "source_file": source.name,
        "supplied_at": datetime.now(timezone.utc).astimezone().isoformat(),
    }
    write_json_atomically(payload, args.output.expanduser().resolve())
    print(json.dumps({"output": str(args.output.expanduser().resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
