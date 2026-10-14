#!/usr/bin/env python3
"""Validate an article-video plan before any paid media request."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from article_paragraphs import PARAGRAPH_REFERENCE, paragraphs, resolve_evidence
from brand import load_brand, opening_hook_example, opening_hook_pattern
from job_store import save_plan_job
from prepare_tts_text import prepare_text, spoken_product_grade
from subtitle_timing import split_timeline_segments, visible_subtitle_segments
from video_constants import FOOTER_CAPTIONS, shot_image_paths


REQUIRED_SHOT_FIELDS = (
    "index",
    "article_evidence",
    "factual_message",
    "narration",
    "subtitle",
    "visual_type",
    "image_ratio",
    "audio_path",
)
ALLOWED_VISUAL_TYPES = {"ai_image", "data_table"}
ALLOWED_STATUSES = {"planned", "pending", "generating", "completed", "failed", "imported"}
# A default plan holds one 7-shot budget: two introduction shots, two
# characteristics shots, at most two application shots, and the parameter
# tables. Anything larger must be declared in `shot_budget` with a reason.
DEFAULT_SHOT_BUDGET = {"max_total": 7, "max_data_tables": 3, "max_per_module": 2}
ALLOWED_SHOT_MODULES = ("introduction", "characteristics", "applications")
ESTIMATED_CLOSING_SECONDS = 17.0
DEFAULT_TTS_SPEED = load_brand().speed
APPROVAL_FILES = {
    "copy_path": "copy-for-approval.md",
    "subtitle_path": "subtitle-for-approval.md",
}
APPROVAL_HASH_FIELDS = {
    "copy_path": "copy_sha256",
    "subtitle_path": "subtitle_sha256",
}


def canonical_spoken_display(value: Any) -> str:
    """Normalize safe display variants without allowing unspoken words."""
    text = str(value or "").casefold()
    text = re.sub(r"[\s，,、；;：:。！？!?（）()「」『』“”\"']", "", text)
    text = re.sub(r"百分之(\d+(?:\.\d+)?)", r"\1%", text)
    text = text.replace("不低于", "≥").replace("不超过", "≤")
    text = text.replace("到", "-").replace("至", "-")
    text = text.replace("兆帕", "mpa").replace("摄氏度", "℃").replace("度", "℃")
    text = text.replace("零下", "-")
    return text


# The mandatory visual constraints are fixed boilerplate. `--complete-prompts`
# appends exactly these clauses, so an agent never hand-edits a dozen prompts.
PROMPT_COMPOSITION_CLAUSE = "14:10 horizontal composition"
PROMPT_PEOPLE_CLAUSE = (
    "any people shown are Chinese people only, no foreign, Western, mixed-ethnicity, or ambiguous faces"
)
PROMPT_PELLET_CLAUSE = (
    "plastic raw material, if shown, consists only of white semi-transparent plastic pellets, "
    "no colored, black, or opaque pellets, no powder, flakes, sheets, or film"
)
PROMPT_FACTORY_CLAUSE = (
    "wide-angle photorealistic factory floor, real industrial production environment, "
    "full spatial context, no close-up, no macro shot, no studio scene"
)
PEOPLE_PROHIBITIONS = ("foreign", "western", "mixed-ethnicity", "ambiguous face")
FACTORY_REQUIREMENTS = ("wide-angle", "real industrial", "no close-up")


def ai_image_prompt_problems(label: str, value: Any) -> list[tuple[str, str]]:
    """Return every (message, clause that fixes it) for one prompt."""
    prompt = str(value or "").strip()
    if not prompt:
        return [(f"{label} image_prompt is required", "")]
    normalized = prompt.casefold()
    problems: list[tuple[str, str]] = []
    if PROMPT_COMPOSITION_CLAUSE not in normalized:
        problems.append(
            (f"{label} image_prompt must require 14:10 horizontal composition", PROMPT_COMPOSITION_CLAUSE)
        )

    # Negated descriptions such as "no people" are constraints, not a request
    # to render people and must not trigger the Chinese-only face requirement.
    person_context = re.sub(
        r"\b(?:no|without)\s+(?:visible\s+)?(?:people|person|workers?|technicians?|drivers?|operators?)\b",
        "",
        normalized,
    )
    person_terms = ("person", "people", "worker", "technician", "driver", "operator")
    if any(term in person_context for term in person_terms):
        matched = next(term for term in person_terms if term in person_context)
        if "chinese" not in normalized or "only" not in normalized:
            problems.append((
                f'{label} mentions "{matched}", so it must also require "Chinese people only" '
                f'for the people it shows',
                PROMPT_PEOPLE_CLAUSE,
            ))
        else:
            missing = [term for term in PEOPLE_PROHIBITIONS if term not in normalized]
            if missing:
                problems.append((
                    f"{label} shows people, so it must explicitly prohibit "
                    + ", ".join(f'"{term}"' for term in missing),
                    PROMPT_PEOPLE_CLAUSE,
                ))

    if any(term in normalized for term in ("plastic raw material", "resin", "pellet")):
        matched = next(term for term in ("plastic raw material", "resin", "pellet") if term in normalized)
        if "white semi-transparent plastic pellets" not in normalized:
            problems.append((
                f'{label} mentions "{matched}", so it must also require '
                f'"white semi-transparent plastic pellets" verbatim and forbid colored, black, or '
                f'opaque pellets, powder, flakes, sheets, and film',
                PROMPT_PELLET_CLAUSE,
            ))

    if any(term in normalized for term in ("factory", "production site", "production line", "factory floor")):
        triggers = ("production site", "production line", "factory floor", "factory")
        matched = next(term for term in triggers if term in normalized)
        missing = [term for term in FACTORY_REQUIREMENTS if term not in normalized]
        if missing:
            problems.append((
                f'{label} mentions "{matched}", so it must also contain '
                + ", ".join(f'"{term}"' for term in missing),
                PROMPT_FACTORY_CLAUSE,
            ))
    return problems


def validate_ai_image_prompt(label: str, value: Any) -> None:
    problems = ai_image_prompt_problems(label, value)
    if problems:
        raise ValueError(problems[0][0])


def complete_ai_image_prompt(value: Any) -> str:
    """Append the fixed mandatory clauses a prompt is missing; idempotent."""
    prompt = str(value or "").strip()
    if not prompt:
        return prompt
    for _ in range(3):  # an added clause can introduce a new trigger word
        clauses = [clause for _message, clause in ai_image_prompt_problems("prompt", prompt) if clause]
        if not clauses:
            break
        for clause in dict.fromkeys(clauses):
            if clause == PROMPT_COMPOSITION_CLAUSE:
                prompt = f"{clause}, {prompt}"
            else:
                prompt = f"{prompt.rstrip(' ,.;')}, {clause}"
    return prompt


def job_ai_image_prompts(job: dict[str, Any]) -> list[tuple[str, dict[str, Any], str | int]]:
    """List (label, owner, key) for every AI image prompt in shot and closing order."""
    entries: list[tuple[str, dict[str, Any], str | int]] = []
    for position, shot in enumerate(job.get("shots") or [], start=1):
        if not isinstance(shot, dict) or shot.get("visual_type") != "ai_image":
            continue
        prompts = shot.get("image_prompts")
        if isinstance(prompts, list):
            index = shot.get("index", position)
            entries.extend(
                (f"shot {index} image_prompts[{variant}]", shot, variant - 1)
                for variant in range(1, len(prompts) + 1)
            )
    for position, visual in enumerate(job.get("closing_visuals") or [], start=1):
        if isinstance(visual, dict):
            entries.append((f"closing_visual {position}", visual, "image_prompt"))
    return entries


def prompt_value(owner: dict[str, Any], key: str | int) -> Any:
    return owner["image_prompts"][key] if isinstance(key, int) else owner.get(key)


def require_ai_image_prompts(job: dict[str, Any]) -> None:
    """Report every non-compliant prompt at once instead of one per run."""
    messages = [
        message
        for label, owner, key in job_ai_image_prompts(job)
        for message, _clause in ai_image_prompt_problems(label, prompt_value(owner, key))
    ]
    if messages:
        raise ValueError(
            f"{len(messages)} image prompt problem(s):\n- " + "\n- ".join(messages)
            + "\nRun `uv run --frozen python scripts\\validate_video_job.py output\\<job-id> --complete-prompts` "
            "to append the mandatory constraint clauses automatically, then revalidate."
        )


def complete_job_prompts(job_path: Path) -> dict[str, Any]:
    """Append missing mandatory clauses to non-compliant prompts, then validate.

    Prompts are not part of the sealed copy approval, so this never needs a
    new human approval. Compliant prompts are left byte-identical, so images
    already generated from them keep their request hash and are not re-paid.
    """
    try:
        job = json.loads(job_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON at {job_path}: {exc}") from exc
    if not isinstance(job, dict):
        raise ValueError("video-job.json root must be an object")
    completed: list[str] = []
    for label, owner, key in job_ai_image_prompts(job):
        current = prompt_value(owner, key)
        updated = complete_ai_image_prompt(current)
        if updated != str(current or "").strip() and updated:
            if isinstance(key, int):
                owner["image_prompts"][key] = updated
            else:
                owner[key] = updated
            completed.append(label)
    if completed:
        save_job(job_path, job)
    result = validate(job_path)
    result["completed_prompts"] = completed
    return result


def inside_job(job_dir: Path, value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    path = (job_dir / value).resolve()
    try:
        path.relative_to(job_dir)
    except ValueError as exc:
        raise ValueError(f"{label} must stay inside the job directory: {value}") from exc


def canonical_job_path(job_dir: Path, value: str) -> str:
    """Use the resolved on-disk identity when comparing job-local assets."""
    return str((job_dir / value).resolve()).casefold()


def approval_file(job_dir: Path, approval: dict[str, Any], field: str) -> Path:
    value = approval.get(field)
    expected_name = APPROVAL_FILES[field]
    label = f"copy_approval.{field}"
    if value != expected_name:
        raise ValueError(f"{label} must be {expected_name}")
    inside_job(job_dir, value, label)
    path = (job_dir / expected_name).resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"{label} is not a non-empty file: {path}")
    return path


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_copy_approval(job_dir: Path, job: dict[str, Any]) -> None:
    """Require the reviewed copy and visible subtitle script before generation."""
    approval = job.get("copy_approval")
    if not isinstance(approval, dict) or approval.get("status") != "approved":
        raise ValueError(
            "copy_approval.status must be 'approved' before generating video assets"
        )
    if not isinstance(approval.get("approved_at"), str) or not approval["approved_at"].strip():
        raise ValueError("copy_approval.approved_at is required")
    for field, hash_field in APPROVAL_HASH_FIELDS.items():
        path = approval_file(job_dir, approval, field)
        expected_hash = approval.get(hash_field)
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError(f"copy_approval.{hash_field} must be a SHA-256 digest")
        if file_sha256(path) != expected_hash:
            raise ValueError(
                f"copy_approval.{field} changed after approval; reset approval and obtain a new confirmation"
            )


def approved_visible_subtitles(path: Path) -> list[str]:
    """Read final visible subtitle rows from the approval artifact."""
    rows: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        row = raw.strip()
        if not row or row.startswith("#") or row.startswith("-"):
            continue
        rows.append(row)
    return rows


def media_generation_started(job: dict[str, Any]) -> bool:
    """Reject sealing an approval snapshot after a paid or draft stage began."""
    for shot in job.get("shots", []):
        if not isinstance(shot, dict):
            continue
        if str(shot.get("image_status", "planned")) not in {"planned", "pending"}:
            return True
        if str(shot.get("audio_status", "planned")) not in {"planned", "pending"}:
            return True
        if shot.get("audio_hash") or shot.get("logo_source_hash"):
            return True
    for visual in job.get("closing_visuals", []):
        if isinstance(visual, dict) and str(visual.get("image_status", "planned")) not in {"planned", "pending"}:
            return True
    audio = job.get("audio")
    if isinstance(audio, dict) and str(audio.get("footer_audio_status", "planned")) not in {"planned", "pending"}:
        return True
    if isinstance(audio, dict) and audio.get("footer_audio_hash"):
        return True
    if job.get("actual_duration_status"):
        return True
    draft = job.get("draft")
    return isinstance(draft, dict) and str(draft.get("status", "not_started")) not in {"not_started", "planned"}


def save_job(job_path: Path, job: dict[str, Any]) -> None:
    """Plan-side write; keeps fields the image stage wrote meanwhile."""
    save_plan_job(job_path, job)


def seal_copy_approval(job_path: Path) -> dict[str, Any]:
    """Freeze the two approved review files before media generation begins."""
    try:
        job = json.loads(job_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON at {job_path}: {exc}") from exc
    if not isinstance(job, dict) or not isinstance(job.get("copy_approval"), dict):
        raise ValueError("video-job.json must contain copy_approval before sealing")
    approval = job["copy_approval"]
    if approval.get("status") != "approved":
        raise ValueError("copy_approval.status must be 'approved' before sealing")
    if media_generation_started(job):
        raise ValueError("cannot seal approval after media generation has started")
    for field, hash_field in APPROVAL_HASH_FIELDS.items():
        approval[hash_field] = file_sha256(approval_file(job_path.parent.resolve(), approval, field))
    approval["approved_at"] = datetime.now(timezone.utc).isoformat()
    save_job(job_path, job)
    return {
        "status": "sealed",
        "job": str(job_path.resolve()),
        "approved_at": approval["approved_at"],
    }


def reset_audio_for_reapproval(job_path: Path) -> dict[str, Any]:
    """Clear generated audio after an approved copy revision, before any visual work."""
    job = json.loads(job_path.read_text(encoding="utf-8-sig"))
    if not isinstance(job, dict):
        raise ValueError("video-job.json root must be an object")
    draft = job.get("draft")
    if isinstance(draft, dict) and str(draft.get("status", "not_started")) not in {"not_started", "planned", "stale_audio", "stale_subtitles"}:
        raise ValueError("cannot reset audio for reapproval after Jianying draft work has started")
    for position, shot in enumerate(job.get("shots", []), start=1):
        if not isinstance(shot, dict):
            raise ValueError(f"shot {position} must be an object")
        if str(shot.get("image_status", "planned")) not in {"planned", "pending"} or shot.get("logo_source_hash"):
            raise ValueError("cannot reset audio for reapproval after visual generation has started")
        for field in ("audio_hash", "audio_error", "asr_subtitle_segments", "draft_subtitle_segments", "subtitle_timing_source", "subtitle_sync_status", "subtitle_sync_warning", "subtitle_sync_hash", "duration_ms"):
            shot.pop(field, None)
        shot["audio_status"] = "planned"
        shot["draft_import_status"] = "planned"
    audio = job.get("audio") if isinstance(job.get("audio"), dict) else {}
    for field in ("footer_audio_hash", "footer_audio_error", "footer_tts_text", "footer_duration_ms", "footer_video_duration_ms"):
        audio.pop(field, None)
    audio["footer_audio_status"] = "planned"
    job["audio"] = audio
    invalidate_duration_verification(job)
    job["actual_duration_status"] = "planned"
    if isinstance(draft, dict):
        draft["status"] = "not_started"
    approval = job.get("copy_approval")
    if isinstance(approval, dict):
        for field in ("copy_sha256", "subtitle_sha256", "approved_at"):
            approval.pop(field, None)
    save_job(job_path, job)
    return {"status": "audio_reset_for_reapproval", "job": str(job_path.resolve())}


def timeline_audio_hash(job: dict[str, Any]) -> str:
    """Fingerprint exactly the audio inputs whose measured duration was approved."""
    shots: list[dict[str, Any]] = []
    for position, shot in enumerate(job.get("shots", []), start=1):
        if not isinstance(shot, dict):
            raise ValueError(f"shot {position} must be an object")
        audio_hash = shot.get("audio_hash")
        if not isinstance(audio_hash, str) or not audio_hash:
            raise ValueError(f"shot {position} audio_hash is required before duration verification")
        shots.append(
            {
                "index": int(shot.get("index", 0)),
                "audio_path": str(shot.get("audio_path") or ""),
                "audio_hash": audio_hash,
            }
        )
    audio = job.get("audio")
    if not isinstance(audio, dict):
        raise ValueError("audio configuration is required before duration verification")
    footer_hash = audio.get("footer_audio_hash")
    if not isinstance(footer_hash, str) or not footer_hash:
        raise ValueError("audio.footer_audio_hash is required before duration verification")
    payload = {"shots": sorted(shots, key=lambda item: item["index"]), "footer_audio_hash": footer_hash}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def invalidate_duration_verification(job: dict[str, Any]) -> None:
    """Mark measured timing stale whenever the audio inputs change."""
    for field in ("actual_total_duration_seconds", "actual_timeline_audio_hash"):
        job.pop(field, None)
    for shot in job.get("shots", []):
        if isinstance(shot, dict):
            shot.pop("duration_ms", None)
    audio = job.get("audio")
    if isinstance(audio, dict):
        audio.pop("footer_duration_ms", None)
        audio.pop("footer_video_duration_ms", None)
    job["actual_duration_status"] = "stale"


def require_verified_duration(job: dict[str, Any]) -> None:
    """Require a current MP3 measurement for the current audio hashes."""
    if job.get("actual_duration_status") != "verified":
        raise ValueError(
            "actual audio duration is not verified; run jianying_local_draft.py --verify-duration first"
        )
    if job.get("actual_timeline_audio_hash") != timeline_audio_hash(job):
        raise ValueError(
            "audio changed after duration verification; run jianying_local_draft.py --verify-duration again"
        )
    total = job.get("actual_total_duration_seconds")
    if isinstance(total, bool) or not isinstance(total, (int, float)) or float(total) <= 0:
        raise ValueError("actual_total_duration_seconds must be a positive number")


def require_title_candidates(job: dict[str, Any]) -> None:
    """Require the approved title options returned with the finished video."""
    metadata = job.get("title_candidates")
    if not isinstance(metadata, dict):
        raise ValueError("title_candidates is required")
    items = metadata.get("items")
    if not isinstance(items, list) or len(items) != 5:
        raise ValueError("title_candidates.items must contain exactly 5 titles")
    normalized = [str(item).strip() for item in items]
    if any(not item for item in normalized) or len(set(normalized)) != 5:
        raise ValueError("title_candidates.items must contain 5 distinct non-empty titles")
    recommended = metadata.get("recommended")
    if not isinstance(recommended, int) or isinstance(recommended, bool) or not 0 <= recommended < len(items):
        raise ValueError("title_candidates.recommended must be an index from 0 to 4")


def require_full_grade_reference(product_grade: str, value: Any, label: str) -> None:
    """Reject copy that mentions a product code without its full grade."""
    code_tokens = re.findall(r"[A-Za-z]*\d[A-Za-z0-9-]*", product_grade)
    if not code_tokens:
        return
    code = code_tokens[-1]
    text = str(value or "")
    without_full_grade = re.sub(re.escape(product_grade), "", text, flags=re.IGNORECASE)
    match = re.search(rf"(?<![A-Za-z0-9]){re.escape(code)}(?![A-Za-z0-9])", without_full_grade, re.IGNORECASE)
    if match:
        # Manufacturer aliases such as DMDA-8008 still refer to the grade;
        # preserve them for TTS/subtitles instead of deleting source facts.
        prefix = without_full_grade[: match.start()]
        if prefix.endswith("-") and prefix[:-1] and prefix[-2].isalpha():
            return
        raise ValueError(
            f"{label} mentions grade code {code} without the complete product_grade {product_grade}"
        )


def comparable_table_text(value: Any) -> str:
    """Compare table text ignoring spacing and full-width/half-width forms."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).casefold()


def require_table_values_in_article(shots: list[Any], article_text: str) -> None:
    """Every numeric table cell must be copied from the article.

    Parameter names may be lightly normalized, but a number, range or unit
    shown on screen is a factual claim and must exist in the source verbatim.
    All offending cells are reported in one message.
    """
    article = comparable_table_text(article_text)
    missing: list[str] = []
    for shot in shots:
        if not isinstance(shot, dict) or shot.get("visual_type") != "data_table":
            continue
        table = shot.get("table") if isinstance(shot.get("table"), dict) else {}
        for row_number, row in enumerate(table.get("rows") or [], start=1):
            for column_number, cell in enumerate(row if isinstance(row, list) else [], start=1):
                text = str(cell).strip()
                if re.search(r"\d", text) and comparable_table_text(text) not in article:
                    missing.append(f'shot {shot.get("index")} table.rows[{row_number}][{column_number}] "{text}"')
    if missing:
        raise ValueError(
            "data_table values must be copied exactly from article.json body_text; not found in the article:\n- "
            + "\n- ".join(missing)
        )


def require_article_order(article_text: str, shots: list[dict[str, Any]]) -> None:
    """Require shots to consume the source article in the order it is written.

    Every per-shot check can pass while the plan still plays the parameter tables
    after the applications it already described. Compare each shot's cited
    evidence position with the previous shot's, treating the application-address
    opening hook as the only section look-ahead the copy rules allow.
    """
    cursor = 0
    previous_index: int | None = None
    for position, shot in enumerate(sorted(shots, key=lambda item: int(item["index"]))):
        if position == 0 and shot.get("opening_hook_type") == "application_address":
            continue
        evidence = str(shot.get("article_evidence") or "").strip()
        span = resolve_evidence(article_text, evidence, cursor)
        offset = span[0] if span and span[0] >= cursor else -1
        if offset < 0:
            previous = f"shot {previous_index}" if previous_index is not None else "the preceding shot"
            raise ValueError(
                f"shot {shot['index']} article_evidence is out of source order: it appears before "
                f"the evidence cited by {previous}. Shots must follow the order of the "
                "source article sections; keep a parameter table with the section that supplies "
                "its data instead of appending it after a later module."
            )
        cursor = offset
        previous_index = int(shot["index"])


def require_shot_budget(job: dict[str, Any]) -> None:
    """Reject a plan that spends more shots than its declared budget."""
    declared = job.get("shot_budget")
    if declared is None:
        declared = {}
    if not isinstance(declared, dict):
        raise ValueError("shot_budget must be an object")
    limits = dict(DEFAULT_SHOT_BUDGET)
    for key, default in DEFAULT_SHOT_BUDGET.items():
        value = declared.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"shot_budget.{key} must be a positive integer")
        limits[key] = value
    raised = any(limits[key] > DEFAULT_SHOT_BUDGET[key] for key in DEFAULT_SHOT_BUDGET)
    if raised and not str(declared.get("reason") or "").strip():
        raise ValueError(
            "shot_budget above the default requires a non-empty shot_budget.reason; "
            f"the default budget is {DEFAULT_SHOT_BUDGET}"
        )

    shots = job["shots"]
    if len(shots) > limits["max_total"]:
        raise ValueError(
            f"shot_budget.max_total is {limits['max_total']} but the plan has {len(shots)} shots. "
            "Consolidate the modules, or declare a larger shot_budget with a reason."
        )
    tables = [shot for shot in shots if shot.get("visual_type") == "data_table"]
    if len(tables) > limits["max_data_tables"]:
        raise ValueError(
            f"shot_budget.max_data_tables is {limits['max_data_tables']} but the plan has "
            f"{len(tables)} data_table shots. Merge the parameter tables."
        )
    modules = [
        str(shot.get("module") or "").strip()
        for shot in shots
        if shot.get("visual_type") != "data_table"
    ]
    if not any(modules):
        return
    if not all(modules):
        raise ValueError(
            "declare shot `module` on every non-data_table shot or on none; "
            f"allowed values are {', '.join(ALLOWED_SHOT_MODULES)}"
        )
    unknown = sorted({module for module in modules if module not in ALLOWED_SHOT_MODULES})
    if unknown:
        raise ValueError(
            f"shot module must be one of {', '.join(ALLOWED_SHOT_MODULES)}; got {', '.join(unknown)}"
        )
    over = {
        module: count
        for module, count in Counter(modules).items()
        if count > limits["max_per_module"]
    }
    if over:
        detail = ", ".join(f"{module} has {count} shots" for module, count in sorted(over.items()))
        raise ValueError(
            f"shot_budget.max_per_module is {limits['max_per_module']} but {detail}. "
            "Merge the repeated shots into the module's two complementary shots."
        )


def validate(job_path: Path) -> dict[str, Any]:
    job_dir = job_path.parent.resolve()
    try:
        job = json.loads(job_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON at {job_path}: {exc}") from exc
    if not isinstance(job, dict):
        raise ValueError("video-job.json root must be an object")
    require_copy_approval(job_dir, job)
    if not isinstance(job.get("shots"), list) or not job["shots"]:
        raise ValueError("video-job.json must contain a non-empty shots array")
    if str(job.get("product_grade") or "").strip() == "":
        raise ValueError("product_grade is required")
    require_title_candidates(job)
    require_ai_image_prompts(job)
    audio = job.get("audio")
    if not isinstance(audio, dict):
        raise ValueError("audio configuration is required")
    voice_id = audio.get("voice_id", load_brand().voice_id)
    if not isinstance(voice_id, str) or not voice_id.strip():
        raise ValueError("audio.voice_id must be a non-empty string")
    speed = audio.get("speed", DEFAULT_TTS_SPEED)
    if not isinstance(speed, (int, float)) or not 0.5 <= float(speed) <= 2.0:
        raise ValueError("audio.speed must be between 0.5 and 2.0")
    product_grade = str(job["product_grade"]).strip()
    article_path = job_dir / "article.json"
    if not article_path.is_file():
        raise ValueError(f"article.json is required for evidence validation: {article_path}")
    try:
        article = json.loads(article_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"article.json is invalid: {article_path}") from exc
    if not isinstance(article, dict) or not isinstance(article.get("body_text"), str):
        raise ValueError("article.json must contain a body_text string")
    article_text = article["body_text"]
    for position, title in enumerate(job["title_candidates"]["items"], start=1):
        require_full_grade_reference(product_grade, title, f"title_candidates.items[{position}]")
    expected_spoken_grade = spoken_product_grade(product_grade)
    if audio.get("product_grade_spoken") not in {None, expected_spoken_grade}:
        raise ValueError("audio.product_grade_spoken must read every product-grade digit individually")

    copy_values = list(job["title_candidates"]["items"])
    copy_values.extend(
        str(shot.get(field) or "")
        for shot in job["shots"]
        if isinstance(shot, dict)
        for field in ("factual_message", "narration", "subtitle")
    )
    if not any(product_grade.casefold() in value.casefold() for value in copy_values):
        raise ValueError("approved title or copy must contain the complete product_grade")

    seen: set[int] = set()
    visual_paths: dict[str, str] = {}
    planned_body_seconds = 0
    for position, shot in enumerate(job["shots"], start=1):
        if not isinstance(shot, dict):
            raise ValueError(f"shot {position} must be an object")
        missing = [field for field in REQUIRED_SHOT_FIELDS if field not in shot]
        if missing:
            raise ValueError(f"shot {position} missing required fields: {', '.join(missing)}")
        try:
            index = int(shot["index"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"shot {position} index must be an integer") from exc
        if index <= 0 or index in seen:
            raise ValueError(f"shot {position} has a duplicate or invalid index: {index}")
        seen.add(index)
        duration = shot.get("duration")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise ValueError(f"shot {index} duration must be a positive number")
        if float(duration) <= 0:
            raise ValueError(f"shot {index} duration must be a positive number")
        planned_body_seconds += float(duration)
        if shot["visual_type"] not in ALLOWED_VISUAL_TYPES:
            raise ValueError(f"shot {index} has unsupported visual_type: {shot['visual_type']}")
        if shot["image_ratio"] != "14:10":
            raise ValueError(f"shot {index} image_ratio must be 14:10")
        if shot["visual_type"] == "ai_image":
            paths = shot_image_paths(shot)
            prompts = shot.get("image_prompts")
            if len(paths) < 2 or not isinstance(prompts, list) or len(prompts) != len(paths):
                raise ValueError(f"shot {index} ai_image requires at least two image_paths and matching image_prompts")
            shot_paths: set[str] = set()
            for variant, (path, prompt) in enumerate(zip(paths, prompts), start=1):
                inside_job(job_dir, path, f"shot {index} image_paths[{variant}]")
                image_key = canonical_job_path(job_dir, str(path))
                if image_key in shot_paths:
                    raise ValueError(
                        f"shot {index} image_paths must be distinct; variant {variant} reuses {path}"
                    )
                shot_paths.add(image_key)
                validate_ai_image_prompt(f"shot {index} image_prompts[{variant}]", prompt)
        if not str(shot["narration"]).strip() or not str(shot["subtitle"]).strip():
            raise ValueError(f"shot {index} narration and subtitle must be non-empty")
        evidence = str(shot.get("article_evidence") or "").strip()
        if not evidence:
            raise ValueError(f"shot {index} article_evidence must be non-empty")
        if PARAGRAPH_REFERENCE.match(evidence) and resolve_evidence(article_text, evidence) is None:
            raise ValueError(
                f"shot {index} article_evidence {evidence} names a paragraph the article does not have "
                f"(it has {len(paragraphs(article_text))}); see article-paragraphs.md"
            )
        if resolve_evidence(article_text, evidence) is None:
            hint = ""
            for length in range(min(len(evidence), 24), 3, -1):
                position = article_text.find(evidence[:length])
                if position >= 0:
                    snippet = article_text[position : position + 60]
                    hint = (
                        f"; the article reads {snippet!r} at that point, so quote that run "
                        "character for character, including blank lines"
                    )
                    break
            raise ValueError(
                f"shot {index} article_evidence is not traceable to article.json body_text"
                + (
                    hint
                    or "; copy an exact run of characters from the article without added punctuation or reflowed text"
                )
                + ", or cite its paragraph number such as P3 (run scripts\\article_paragraphs.py to list them)"
            )
        if not str(shot.get("factual_message") or "").strip():
            raise ValueError(f"shot {index} factual_message must be non-empty")
        if canonical_spoken_display(shot["narration"]) != canonical_spoken_display(shot["subtitle"]):
            raise ValueError(
                f"shot {index} subtitle contains words not present in narration; "
                "visible subtitle must be an equivalent display form of the spoken text"
            )
        for field in ("factual_message", "narration", "subtitle", "narration_transition"):
            require_full_grade_reference(product_grade, shot.get(field), f"shot {index} {field}")
        tts_text = shot.get("tts_text")
        if isinstance(tts_text, str) and not tts_text.strip():
            raise ValueError(
                f"shot {index} tts_text is empty; leave the field out at plan time and let "
                r"`uv run --frozen python scripts\prepare_tts_text.py --job <job-dir>` generate it, then revalidate"
            )
        if tts_text is not None:
            expected_tts_text = prepare_text(str(shot["narration"]), product_grade)
            if tts_text != expected_tts_text:
                raise ValueError(
                    f"shot {index} tts_text does not match the narration prepared for TTS "
                    f'(expected starts "{expected_tts_text[:40]}", got "{str(tts_text)[:40]}"); '
                    r"re-run `scripts\prepare_tts_text.py --job <job-dir>` instead of editing tts_text by hand"
                )
        subtitle_segments = shot.get("subtitle_segments")
        if subtitle_segments is not None:
            expected_segments = split_timeline_segments(str(shot["subtitle"]))
            if subtitle_segments != expected_segments:
                raise ValueError(
                    f"shot {index} subtitle_segments must use comma, sentence-ending punctuation, and semicolon boundaries"
                )
        inside_job(job_dir, shot["audio_path"], f"shot {index} audio_path")
        image_values = shot_image_paths(shot) or [None]
        for variant, image_value in enumerate(image_values, start=1):
            inside_job(job_dir, image_value, f"shot {index} image_paths[{variant}]")
            image_key = canonical_job_path(job_dir, str(image_value))
            visual_paths[image_key] = f"shot {index} image_paths[{variant}]"
        status = str(shot.get("image_status", "planned"))
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"shot {index} has unsupported image_status: {status}")
        if shot["visual_type"] == "data_table":
            table = shot.get("table")
            if not isinstance(table, dict) or not isinstance(table.get("columns"), list) or not isinstance(table.get("rows"), list):
                raise ValueError(f"shot {index} data_table requires table columns and rows")
            if not 2 <= len(table["columns"]) <= 3 or not 1 <= len(table["rows"]) <= 5:
                raise ValueError(f"shot {index} table must have 2-3 columns and 1-5 rows")
            if not str(table.get("title") or "").strip():
                raise ValueError(f"shot {index} data_table title is required")
            if any(not str(cell).strip() for cell in table["columns"]):
                raise ValueError(f"shot {index} data_table columns must be non-empty")
            for row in table["rows"]:
                if not isinstance(row, list) or len(row) != len(table["columns"]):
                    raise ValueError(f"shot {index} data_table row width must match columns")
                if any(not str(cell).strip() for cell in row):
                    raise ValueError(f"shot {index} data_table cells must be non-empty")
            if shot.get("table_path") is not None:
                inside_job(job_dir, shot["table_path"], f"shot {index} table_path")

    require_table_values_in_article(job["shots"], article_text)

    require_article_order(article_text, job["shots"])

    require_shot_budget(job)

    closing_visuals = job.get("closing_visuals", [])
    if closing_visuals is not None:
        if not isinstance(closing_visuals, list) or len(closing_visuals) != 2:
            raise ValueError("closing_visuals must contain exactly 2 items")
        for position, visual in enumerate(closing_visuals, start=1):
            if not isinstance(visual, dict):
                raise ValueError(f"closing_visual {position} must be an object")
            if visual.get("image_ratio") != "14:10":
                raise ValueError(f"closing_visual {position} image_ratio must be 14:10")
            validate_ai_image_prompt(f"closing_visual {position}", visual.get("image_prompt"))
            inside_job(job_dir, visual.get("image_path"), f"closing_visual {position} image_path")
            image_key = canonical_job_path(job_dir, str(visual["image_path"]))
            previous_visual = visual_paths.get(image_key)
            if previous_visual:
                raise ValueError(
                    f"closing_visual {position} image_path must be distinct from {previous_visual}"
                )
            visual_paths[image_key] = f"closing_visual {position}"
            status = str(visual.get("image_status", "planned"))
            if status not in ALLOWED_STATUSES:
                raise ValueError(
                    f"closing_visual {position} has unsupported image_status: {status}"
                )

    approved_subtitles = approved_visible_subtitles(
        approval_file(job_dir, job["copy_approval"], "subtitle_path")
    )
    final_subtitles = [
        segment
        for shot in sorted(job["shots"], key=lambda item: int(item["index"]))
        for segment in visible_subtitle_segments(str(shot["subtitle"]))
    ]
    final_subtitles.extend(
        segment
        for caption in (template.format(product_grade=product_grade) for template in FOOTER_CAPTIONS)
        for segment in visible_subtitle_segments(caption)
    )
    if approved_subtitles != final_subtitles:
        raise ValueError(
            "subtitle-for-approval.md must list the exact final visible Jianying subtitles"
        )

    first = min(job["shots"], key=lambda item: int(item["index"]))
    if "opening_hook_type" not in first:
        raise ValueError("first shot must declare opening_hook_type")
    if first.get("opening_hook_type") == "application_address":
        applications = first.get("opening_applications")
        if not isinstance(applications, list) or not 1 <= len(applications) <= 4 or any(
            not isinstance(item, str) or not item.strip() for item in applications
        ):
            raise ValueError("application_address opening requires 1-4 opening_applications")
        opening = str(first.get("narration") or "").strip()
        if not opening_hook_pattern(load_brand()).match(opening):
            raise ValueError(
                f"application_address opening narration must begin with '{opening_hook_example(load_brand())}'"
            )
        subtitle_opening = str(first.get("subtitle") or "").strip()
        if not opening_hook_pattern(load_brand()).match(subtitle_opening):
            raise ValueError(
                f"application_address opening subtitle must begin with '{opening_hook_example(load_brand())}'"
            )
    for shot in job["shots"]:
        if int(shot["index"]) != int(first["index"]) and not str(shot.get("narration_transition") or "").strip():
            raise ValueError(f"shot {shot['index']} requires narration_transition")

    planned_total = job.get("planned_total_duration_seconds")
    if planned_total is None:
        raise ValueError(
            "planned_total_duration_seconds is required; set it to the body duration plus the 17-second closing"
        )
    if isinstance(planned_total, bool) or not isinstance(planned_total, (int, float)):
        raise ValueError("planned_total_duration_seconds must be numeric")
    if float(planned_total) <= 0:
        raise ValueError("planned_total_duration_seconds must be a positive number")
    planned_images = sum(len(shot_image_paths(shot)) for shot in job["shots"]) + len(
        closing_visuals or []
    )
    return {
        "status": "valid",
        "shots": len(job["shots"]),
        "planned_images": planned_images,
        "planned_total_duration_seconds": round(float(planned_total), 3),
        "job": str(job_path.resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", type=Path, help="Job directory or video-job.json path")
    parser.add_argument(
        "--seal-approval",
        action="store_true",
        help="Record immutable hashes for the approved copy and subtitle files",
    )
    parser.add_argument(
        "--reset-audio-for-reapproval",
        action="store_true",
        help="Clear generated audio after an approved copy revision and before resealing",
    )
    parser.add_argument(
        "--complete-prompts",
        action="store_true",
        help="Append the mandatory constraint clauses to non-compliant image prompts, then validate",
    )
    args = parser.parse_args()
    job_path = args.job / "video-job.json" if args.job.is_dir() else args.job
    try:
        resolved = job_path.resolve()
        if sum((args.seal_approval, args.reset_audio_for_reapproval, args.complete_prompts)) > 1:
            raise ValueError(
                "--seal-approval, --reset-audio-for-reapproval and --complete-prompts are mutually exclusive"
            )
        if args.seal_approval:
            result = seal_copy_approval(resolved)
        elif args.reset_audio_for_reapproval:
            result = reset_audio_for_reapproval(resolved)
        elif args.complete_prompts:
            result = complete_job_prompts(resolved)
        else:
            result = validate(resolved)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
