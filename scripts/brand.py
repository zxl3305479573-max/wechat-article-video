#!/usr/bin/env python3
"""Brand configuration: closing copy, voice, templates and their geometry.

Everything that belongs to one company rather than to the pipeline lives in
`brand.json` at the skill root. Point `VIDEO_BRAND_FILE` (process environment
or this skill's `.env`) at another file to produce videos for another brand;
asset paths in that file resolve relative to the file itself.
"""

from __future__ import annotations

import json
import re
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


SKILL_ROOT = Path(__file__).resolve().parents[1]
BRAND_FILE_ENV = "VIDEO_BRAND_FILE"
ASSET_KEYS = ("logo", "template_overlay", "cover_template", "landscape_cover_template", "footer_video")
BOX_KEYS = (
    "video_window",
    "image_frame",
    "grade_region",
    "cover_image_region",
    "cover_grade_region",
    "landscape_grade_region",
)
NUMBER_KEYS = ("subtitle_y", "header_grade_y", "mp4_subtitle_baseline_y", "cover_grade_font_scale")
# Two closing images plus the footer video each need at least one caption.
MIN_FOOTER_CAPTIONS = 3


@dataclass(frozen=True)
class Brand:
    name: str
    footer_captions: tuple[str, ...]
    voice_id: str
    speed: float
    hook_prefix: str
    hook_suffix: str
    closing_image_prompts: tuple[str, str]
    assets: dict[str, Path]
    layout: dict[str, Any]
    source: Path


def brand_file() -> Path:
    load_dotenv(SKILL_ROOT / ".env", override=False)
    configured = os.getenv(BRAND_FILE_ENV, "").strip()
    return Path(configured).expanduser().resolve() if configured else SKILL_ROOT / "brand.json"


def parse_brand(data: Any, source: Path) -> Brand:
    """Validate a brand document and resolve its asset paths."""
    problems: list[str] = []
    if not isinstance(data, dict):
        raise ValueError(f"{source}: the brand file must be a JSON object")
    captions = data.get("footer_captions")
    if not isinstance(captions, list) or len(captions) < MIN_FOOTER_CAPTIONS or not all(
        isinstance(item, str) and item.strip() for item in captions
    ):
        problems.append(f"footer_captions must list at least {MIN_FOOTER_CAPTIONS} non-empty strings")
        captions = []
    voice = data.get("voice") if isinstance(data.get("voice"), dict) else {}
    voice_id = str(voice.get("voice_id") or "").strip()
    speed = voice.get("speed")
    if not voice_id:
        problems.append("voice.voice_id is required")
    if not isinstance(speed, (int, float)) or isinstance(speed, bool) or not 0.5 <= float(speed) <= 2.0:
        problems.append("voice.speed must be a number from 0.5 to 2.0")
        speed = 1.0
    hook = data.get("opening_hook") if isinstance(data.get("opening_hook"), dict) else {}
    prefix, suffix = str(hook.get("prefix") or ""), str(hook.get("suffix") or "")
    if not suffix:
        problems.append("opening_hook.suffix is required (e.g. 的老板看过来)")
    prompts = data.get("closing_image_prompts")
    if not isinstance(prompts, list) or len(prompts) != 2 or not all(isinstance(item, str) and item.strip() for item in prompts):
        problems.append("closing_image_prompts must list exactly two prompts")
        prompts = ["", ""]
    raw_assets = data.get("assets") if isinstance(data.get("assets"), dict) else {}
    assets: dict[str, Path] = {}
    for key in ASSET_KEYS:
        value = raw_assets.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"assets.{key} is required")
            continue
        path = Path(value).expanduser()
        assets[key] = path if path.is_absolute() else (source.parent / path).resolve()
    layout = data.get("layout") if isinstance(data.get("layout"), dict) else {}
    for key in BOX_KEYS:
        box = layout.get(key)
        if not isinstance(box, list) or len(box) != 4 or not all(isinstance(value, (int, float)) for value in box):
            problems.append(f"layout.{key} must be four numbers [x1, y1, x2, y2]")
    for key in NUMBER_KEYS:
        if not isinstance(layout.get(key), (int, float)):
            problems.append(f"layout.{key} must be a number")
    if problems:
        raise ValueError(f"{source}: invalid brand configuration:\n- " + "\n- ".join(problems))
    return Brand(
        name=str(data.get("name") or "").strip(),
        footer_captions=tuple(item.strip() for item in captions),
        voice_id=voice_id,
        speed=float(speed),
        hook_prefix=prefix,
        hook_suffix=suffix,
        closing_image_prompts=(prompts[0].strip(), prompts[1].strip()),
        assets=assets,
        layout={key: tuple(layout[key]) if key in BOX_KEYS else layout[key] for key in (*BOX_KEYS, *NUMBER_KEYS)},
        source=source,
    )


def opening_hook_pattern(brand: Brand) -> re.Pattern[str]:
    """`做{应用}的老板看过来，` by default; group 1 captures the applications."""
    return re.compile(rf"^{re.escape(brand.hook_prefix)}(.+?){re.escape(brand.hook_suffix)}[，,。！？!?]")


def opening_hook_example(brand: Brand) -> str:
    return f"{brand.hook_prefix}{{文章应用场景}}{brand.hook_suffix}"


@lru_cache(maxsize=1)
def load_brand() -> Brand:
    path = brand_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as error:
        raise ValueError(f"brand file not found: {path} (set {BRAND_FILE_ENV} or restore brand.json)") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"brand file is not valid JSON: {path}: {error}") from error
    return parse_brand(data, path)
