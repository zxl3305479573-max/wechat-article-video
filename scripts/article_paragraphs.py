#!/usr/bin/env python3
"""Number an article's paragraphs so evidence can cite `P3` instead of a quote.

A paragraph is one non-empty line of `article.json` `body_text`. That holds for
every source: DOCX paragraphs and table rows, pasted text, and WeChat pages
(whose extracted text has no blank lines between paragraphs).

    uv run --frozen python scripts\\article_paragraphs.py output\\<job-id>\\article.json

prints the numbered paragraphs and writes `article-paragraphs.md` next to
`article.json` for reference while writing the copy.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


PARAGRAPH_REFERENCE = re.compile(r"^[Pp](\d+)(?:\s*[-~～至]\s*[Pp]?(\d+))?$")


def paragraphs(body_text: str) -> list[tuple[int, int, str]]:
    """Return (start, end, text) for every non-empty line, in order."""
    result: list[tuple[int, int, str]] = []
    offset = 0
    for line in str(body_text or "").splitlines(keepends=True):
        stripped = line.strip()
        if stripped:
            start = offset + line.index(stripped[0])
            result.append((start, start + len(stripped), stripped))
        offset += len(line)
    return result


def resolve_evidence(body_text: str, evidence: str, cursor: int = 0) -> tuple[int, int] | None:
    """Locate evidence: `P3` / `P3-P5` paragraph references, or an exact excerpt.

    Returns the (start, end) character span in `body_text`, or None when the
    evidence cannot be traced. An excerpt is searched from `cursor` onward.
    """
    evidence = str(evidence or "").strip()
    if not evidence:
        return None
    reference = PARAGRAPH_REFERENCE.match(evidence)
    if reference:
        numbered = paragraphs(body_text)
        first = int(reference.group(1))
        last = int(reference.group(2) or first)
        if not 1 <= first <= last <= len(numbered):
            return None
        return numbered[first - 1][0], numbered[last - 1][1]
    start = body_text.find(evidence, cursor)
    return (start, start + len(evidence)) if start >= 0 else None


def render(body_text: str) -> str:
    lines = ["# 原文段落编号", "", "镜头的 `证据：` 可以写段落编号（如 `P3`，或连续多段 `P3-P5`），也可以逐字摘录原文。", ""]
    lines += [f"- **P{number}** {text}" for number, (_start, _end, text) in enumerate(paragraphs(body_text), start=1)]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("article", type=Path, help="article.json")
    args = parser.parse_args()
    try:
        article = json.loads(args.article.read_text(encoding="utf-8-sig"))
        content = render(str(article.get("body_text") or ""))
    except (OSError, ValueError, AttributeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    output = args.article.with_name("article-paragraphs.md")
    output.write_text(content, encoding="utf-8")
    try:  # a GBK Windows console cannot print every character in an article
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError):
        pass
    print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
