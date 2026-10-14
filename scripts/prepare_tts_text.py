#!/usr/bin/env python3
"""Prepare spoken TTS text without changing visible product-grade text."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from job_store import save_plan_job
from subtitle_timing import split_timeline_segments


MATERIAL_GRADE_PATTERN = re.compile(
    # The trailing lookahead keeps a decimal measurement such as "PP 0.9 g/cm³"
    # from being read as a grade code ("PP 零.9").
    r"(?<![A-Za-z0-9])(?:HDPE|LDPE|LLDPE|PP|PVC|ABS|PS|HIPS|GPPS|EVA|POE|PC|POM|PET|PBT|PA(?:6|66)?|TPU|TPE|SBS|SEBS|ASA|PMMA)\s*[-/]?\s*([A-Za-z]*\d[A-Za-z0-9-]*)(?![.\d])",
    re.IGNORECASE,
)
LABELLED_GRADE_PATTERN = re.compile(
    r"(?:\u724c\u53f7|\u578b\u53f7|grade|model)\s*[:\uff1a#-]?\s*([A-Za-z]*\d[A-Za-z0-9-]*)",
    re.IGNORECASE,
)
ALIAS_GRADE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]{2,})-([A-Za-z]*\d[A-Za-z0-9-]*)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
BARE_CONTEXTUAL_GRADE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9.])(\d{2,6})(?=\s*(?:物性(?:参数)?|参数|牌号|型号|注塑级|级料|系列|可互换|可(?:相互)?替代|这款料|材料|树脂)|\s*[（(]\s*(?:注塑级|牌号|型号))",
    re.IGNORECASE,
)
COMPARISON_GRADE_PATTERN = re.compile(
    r"(?:与|和|及)\s*((?:\d{2,6}(?:[、，,\s/]+)?)+)(?=\s*(?:不同|相同|相比|对比|区别))"
)
# Standalone material grades such as T30S / Z20S / K8003: a single leading
# letter, 2-4 digits, and a single trailing letter, bounded by non-alphanumeric
# characters. The negative lookbehind also excludes "-" so hyphenated aliases
# like N-Z30S stay with ALIAS_GRADE_PATTERN.
STANDALONE_GRADE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9-])([A-Za-z]\d{2,4}[A-Za-z])(?![A-Za-z0-9])",
    re.IGNORECASE,
)


CHINESE_DIGITS = str.maketrans("0123456789", "零一二三四五六七八九")
COMPARISON_SIGNS = str.maketrans({"≥": "大于等于", "≤": "小于等于", "⩾": "大于等于", "⩽": "小于等于"})


def spoken_number(value: str) -> str:
    """Read every digit in a long numeric code individually for TTS."""
    return value.translate(CHINESE_DIGITS)


def spoken_grade_code(value: str) -> str:
    """Read all digits in a recognized material grade one by one."""
    return re.sub(r"\d", lambda match: spoken_number(match.group(0)), value)


def read_recognized_grade_codes(text: str) -> str:
    """Convert contextual material grades without changing ordinary measurements."""
    def replace(match: re.Match[str]) -> str:
        return match.group(0).replace(match.group(1), spoken_grade_code(match.group(1)))

    text = MATERIAL_GRADE_PATTERN.sub(replace, text)
    text = LABELLED_GRADE_PATTERN.sub(replace, text)
    text = BARE_CONTEXTUAL_GRADE_PATTERN.sub(
        lambda match: spoken_grade_code(match.group(1)), text
    )
    # Standalone grades such as T30S are read digit by digit for TTS while the
    # visible subtitle keeps the original code.
    text = STANDALONE_GRADE_PATTERN.sub(
        lambda match: spoken_grade_code(match.group(1)), text
    )
    # Bare codes in a direct comparison are material grades, for example
    # “与7260、8008不同”. Convert only the TTS text to digit-by-digit speech.
    text = COMPARISON_GRADE_PATTERN.sub(
        lambda match: match.group(0).replace(
            match.group(1),
            re.sub(r"\d+", lambda code: spoken_grade_code(code.group(0)), match.group(1)),
        ),
        text,
    )
    return ALIAS_GRADE_PATTERN.sub(
        lambda match: match.group(1) + "-" + spoken_grade_code(match.group(2)), text
    )


def spoken_product_grade(product_grade: str) -> str:
    """Read grade-code digits individually, including short contextual codes."""
    return spoken_grade_code(product_grade)


def replace_exact_product_grade(text: str, product_grade: str) -> str:
    """Convert the selected grade even when its material prefix is Chinese."""
    pattern = re.compile(re.escape(product_grade), re.IGNORECASE)
    return pattern.sub(lambda match: spoken_product_grade(match.group(0)), text)


RANGE_SEPARATOR_PATTERN = re.compile(
    r"(?<=\d)\s*[\u2013\u2014\u301c~\uff5e]\s*(?=[-\d\u767e\u8d1f])"
)


def read_ranges_and_units(text: str) -> str:
    """Read measurement ranges and temperature units aloud for TTS.

    Visible subtitles keep the original notation; only the spoken text changes:
    "200-260\u00b0C" -> "二百到二百六十摄氏度", "-10-100\u00b0C" -> "负十到一百摄氏度".
    """
    # Temperature unit first, so the range rule sees plain numbers.
    text = re.sub(r"\u00b0\s*C\b", "\u6444\u6c0f\u5ea6", text, flags=re.IGNORECASE)
    # Dash-like separators between numbers are read as "到".
    text = RANGE_SEPARATOR_PATTERN.sub("\u5230", text)
    # A hyphen between two numbers is also a range separator.
    text = re.sub(r"(?<=\d)\s*-\s*(?=\d)", "\u5230", text)
    # A leading minus on a number is a negative sign, not a range separator.
    text = re.sub(r"(?<![\d\u4e00-\u9fff])-\s*(?=\d)", "\u8d1f", text)
    return text


def prepare_text(text: str, product_grade: str) -> str:
    """Prepare TTS text while preserving visible measurements in subtitles."""
    if not product_grade.strip():
        raise ValueError("product_grade is required")
    # Comparison signs are read aloud explicitly instead of being left to the
    # TTS engine, which may skip them: "≥600%" -> "大于等于百分之600".
    text = text.translate(COMPARISON_SIGNS)
    # MiniMax can omit the percent unit. Put it before the value so it is
    # spoken unambiguously, then apply the long-number pronunciation rule.
    text = re.sub(r"(\d+(?:\.\d+)?)\s*[%％]", r"百分之\1", text)
    # Melt flow rate units are read aloud in Chinese; the visible subtitle
    # keeps the original "g/10min" notation.
    text = re.sub(
        r"(\d+(?:\.\d+)?)\s*g\s*/\s*10\s*min",
        r"\1克每10分钟",
        text,
        flags=re.IGNORECASE,
    )
    text = replace_exact_product_grade(text, product_grade)
    text = read_recognized_grade_codes(text)
    text = read_ranges_and_units(text)
    return text


def save_job(job_path: Path, job: dict[str, Any]) -> None:
    """Plan-side write; keeps fields the image stage wrote meanwhile."""
    save_plan_job(job_path, job)


def prepare_job(job_path: Path) -> dict[str, int]:
    # validate_video_job imports this module for shared TTS validation.
    from validate_video_job import require_copy_approval

    job = json.loads(job_path.read_text(encoding="utf-8-sig"))
    if not isinstance(job, dict) or not isinstance(job.get("shots"), list):
        raise ValueError("video-job.json must contain a shots array")
    require_copy_approval(job_path.parent.resolve(), job)
    product_grade = str(job.get("product_grade") or "").strip()
    if not product_grade:
        raise ValueError("video-job.json must contain product_grade")
    audio_config = job.get("audio") if isinstance(job.get("audio"), dict) else {}
    voice_id = str(audio_config.get("voice_id") or "female-chengshu")
    speed = float(audio_config.get("speed", 1.25))

    changed = 0
    stale_audio = 0
    timing_changed = 0
    for position, shot in enumerate(job["shots"], start=1):
        if not isinstance(shot, dict):
            raise ValueError(f"shot {position} must be an object")
        narration = str(shot.get("narration") or "").strip()
        subtitle = str(shot.get("subtitle") or "").strip()
        if not narration:
            raise ValueError(f"shot {position} narration is required")
        if not subtitle:
            raise ValueError(f"shot {position} subtitle is required")
        tts_text = prepare_text(narration, product_grade)
        tts_changed = shot.get("tts_text") != tts_text
        subtitle_segments = split_timeline_segments(subtitle)
        segments_changed = shot.get("subtitle_segments") != subtitle_segments
        if tts_changed:
            existing_hash = str(shot.get("audio_hash") or "")
            audio_path = job_path.parent / str(shot.get("audio_path") or "")
            from generate_job_audio import audio_hash

            reusable = (
                bool(existing_hash)
                and existing_hash == audio_hash(tts_text, voice_id, speed)
                and audio_path.is_file()
                and audio_path.stat().st_size > 0
            )
            if not reusable:
                shot.pop("audio_hash", None)
                shot["audio_status"] = "pending"
                shot["draft_import_status"] = "planned"
                stale_audio += 1
        if tts_changed or segments_changed:
            shot.pop("asr_subtitle_segments", None)
            shot.pop("draft_subtitle_segments", None)
            shot.pop("subtitle_timing_source", None)
            shot.pop("subtitle_sync_status", None)
            shot.pop("subtitle_sync_warning", None)
            shot.pop("subtitle_sync_hash", None)
        if segments_changed:
            timing_changed += 1
        shot["tts_text"] = tts_text
        shot["subtitle_segments"] = subtitle_segments
        if tts_text != narration:
            changed += 1

    job["audio"] = job.get("audio") if isinstance(job.get("audio"), dict) else {}
    job["audio"]["product_grade_spoken"] = spoken_product_grade(product_grade)
    job["audio"]["long_number_reading"] = "recognized_grade_codes_and_comparisons_v2"
    if stale_audio:
        from validate_video_job import invalidate_duration_verification

        invalidate_duration_verification(job)
    if stale_audio or timing_changed:
        draft = job.get("draft")
        if isinstance(draft, dict):
            draft["status"] = "stale_audio" if stale_audio else "stale_subtitles"
            draft["stale_reason"] = "TTS text changed" if stale_audio else "subtitle timing boundaries changed"
    save_job(job_path, job)
    return {
        "shots": len(job["shots"]),
        "grade_replacements": changed,
        "stale_audio": stale_audio,
        "subtitle_timing_updated": timing_changed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--job", type=Path, help="Job directory or video-job.json to update")
    source.add_argument("--text", help="Text to convert and print")
    parser.add_argument("--product-grade", help="Required with --text")
    args = parser.parse_args()

    if args.text is not None:
        if not args.product_grade:
            parser.error("--product-grade is required with --text")
        print(prepare_text(args.text, args.product_grade))
        return 0

    job_path = args.job / "video-job.json" if args.job.is_dir() else args.job
    print(json.dumps(prepare_job(job_path.resolve()), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
