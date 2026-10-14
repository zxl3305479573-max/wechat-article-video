#!/usr/bin/env python3
"""Shared subtitle timeline boundaries for article-video jobs."""

from __future__ import annotations

import re


# Sentence-ending question/exclamation marks are spoken boundaries too.
TIMELINE_BOUNDARIES = frozenset("，,；;。！？!?")
MATERIAL_GRADE = re.compile(
    r"(?<![A-Za-z0-9])(?:HDPE|LDPE|LLDPE|PP|PVC|ABS|PS|HIPS|GPPS|EVA|POE|PC|POM|PET|PBT|PA(?:6|66)?|TPU|TPE|SBS|SEBS|ASA|PMMA)\s+[A-Za-z0-9-]+(?![A-Za-z0-9-])",
    re.IGNORECASE,
)
TECHNICAL_VALUE = re.compile(
    r"(?<![A-Za-z0-9.])[<>≥≤]?\s*-?\d+(?:\.\d+)?(?:\s*[-~]\s*-?\d+(?:\.\d+)?)*(?:\s*(?:%|％|℃|°C|[A-Za-z]+(?:/[A-Za-z0-9²³]+)?))?"
)
# Jianying subtitle line limit (17 visible Chinese-character units).
SUBTITLE_MAX_WIDTH_UNITS = 17.0
CONTINUATION_ENDINGS = ("因为", "由于", "如果", "当", "通过", "凭借", "在", "将", "让", "使")


def is_decimal_point(text: str, index: int) -> bool:
    """Keep decimal points inside technical values out of the timeline split."""
    return (
        text[index] == "."
        and index > 0
        and index + 1 < len(text)
        and text[index - 1].isdigit()
        and text[index + 1].isdigit()
    )


def split_timeline_segments(text: str) -> list[str]:
    """Split at commas, sentence-ending punctuation, and semicolons.

    Dunhao remains in its segment so the draft adapter can display it as a
    space. Decimal points remain inside numeric values.
    """
    source = str(text or "").strip()
    if not source:
        return []

    parts: list[str] = []
    current: list[str] = []
    for index, character in enumerate(source):
        current.append(character)
        if character in TIMELINE_BOUNDARIES or (
            character == "." and not is_decimal_point(source, index)
        ):
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
    trailing = "".join(current).strip()
    if trailing:
        parts.append(trailing)
    if not parts:
        return [source]
    # A comma is not always a semantic boundary. Join only when the previous
    # clause is genuinely incomplete, so ordinary sentences that merely start
    # with words such as "适合" cannot glue into one over-long subtitle.
    merged: list[str] = []
    for part in parts:
        if merged:
            previous = merged[-1].rstrip("，,；;")
            current = part.lstrip()
            dependent = previous.endswith(CONTINUATION_ENDINGS)
            if dependent and not previous.endswith(("。", "！", "？", "!", "?")):
                merged[-1] = merged[-1].rstrip() + current
                continue
        merged.append(part)
    return merged


def protected_subtitle_space_indexes(text: str) -> set[int]:
    """Return spaces that must not become subtitle overflow breakpoints."""
    protected: set[int] = set()
    for match in MATERIAL_GRADE.finditer(text):
        protected.update(index for index in range(match.start(), match.end()) if text[index].isspace())
    return protected


def display_text(text: str) -> str:
    """Match Jianying's visible subtitle text, without sentence punctuation."""
    text = str(text or "").replace("、", " ")
    text = re.sub(r"(?<!\d)[.,]|[.,](?!\d)", "", text)
    return re.sub(r"[，、；：。！？!?；:：…（）()【】\[\]“”‘’\"'、·]", "", text).strip()


def display_width_units(text: str) -> float:
    return sum(1.0 if ord(char) > 0x7F else 0.55 for char in text if not char.isspace())


def protected_display_units(text: str) -> list[tuple[int, int]]:
    """Return non-breakable grades and technical values, preferring full grades."""
    matches = sorted(
        [*MATERIAL_GRADE.finditer(text), *TECHNICAL_VALUE.finditer(text)],
        key=lambda match: (match.start(), -(match.end() - match.start())),
    )
    units: list[tuple[int, int]] = []
    for match in matches:
        start, end = match.span()
        if units and start < units[-1][1]:
            continue
        units.append((start, end))
    return units


def visible_subtitle_segments(text: str) -> list[str]:
    """Return exactly the visible subtitle strings created by the draft adapter."""
    result: list[str] = []
    for source in split_timeline_segments(text):
        value = display_text(source)
        if not value:
            continue
        if display_width_units(value) <= SUBTITLE_MAX_WIDTH_UNITS:
            result.append(value)
            continue
        ranges = protected_display_units(value)
        # Overflow is split only at authored spaces.  This keeps phrases and
        # Chinese words intact instead of cutting at an arbitrary character.
        tokens: list[tuple[str, bool]] = []
        index = 0
        while index < len(value):
            matching = next(((start, end) for start, end in ranges if start == index), None)
            if matching:
                _, end = matching
                tokens.append((value[index:end], False))
                index = end
            elif value[index].isspace() and index not in protected_subtitle_space_indexes(value):
                end = index + 1
                while end < len(value) and value[end].isspace() and end not in protected_subtitle_space_indexes(value):
                    end += 1
                tokens.append((value[index:end], True))
                index = end
            else:
                tokens.append((value[index], False))
                index += 1

        line = ""
        last_break = -1
        for token, breakable in tokens:
            if display_width_units(token) > SUBTITLE_MAX_WIDTH_UNITS:
                raise ValueError(f"unbreakable subtitle value exceeds the visible width: {token}")
            candidate = line + token
            if line and display_width_units(candidate) > SUBTITLE_MAX_WIDTH_UNITS:
                if last_break > 0:
                    result.append(line[:last_break].rstrip())
                    line = line[last_break:].lstrip() + token
                else:
                    # No authored break is available. Fall back to a token
                    # boundary so a long Chinese clause still renders as two
                    # subtitles instead of blocking text approval.
                    result.append(line.rstrip())
                    line = token
                last_break = -1
                continue
            line = candidate
            if breakable:
                last_break = len(line) - len(token)
        if line.strip():
            result.append(line.strip())
    if any(display_width_units(line) > SUBTITLE_MAX_WIDTH_UNITS for line in result):
        raise ValueError("subtitle line exceeds the visible template width")
    return result
