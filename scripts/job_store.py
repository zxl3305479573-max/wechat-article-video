#!/usr/bin/env python3
"""Serialized, field-owned writes to video-job.json.

The audio stage (prepare_tts_text, generate_job_audio, verify-duration) and the
visual stage (generate_job_images, prepare_local_assets) may run at the same
time. Each stage owns a disjoint set of fields. Every write takes a lock,
re-reads the current file and replaces only the fields its stage owns, so a
stage that loaded the plan earlier can never roll back the other stage's
results. The Jianying draft stage runs after both and writes the whole plan.
"""

from __future__ import annotations

import copy
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


# Fields the visual stage writes on shots and closing visuals. Every other
# field, including prompts, paths and all top-level fields, belongs to the
# plan/audio side.
VISUAL_SHOT_FIELDS = frozenset(
    {
        "image_status",
        "image_request_hash",
        "image_provider",
        "image_model",
        "image_error",
        "image_statuses",
        "image_request_hashes",
        "image_providers",
        "image_models",
        "image_errors",
        "logo_image_path",
        "logo_image_paths",
        "logo_source_hash",
        "image_logo_source_hashes",
        "logo_status",
        "table_source_hash",
        "local_asset_error",
    }
)
LOCK_TIMEOUT_SECONDS = 60.0
STALE_LOCK_SECONDS = 300.0


@contextmanager
def job_lock(job_path: Path) -> Iterator[None]:
    """Hold an exclusive lock file next to video-job.json."""
    lock_path = job_path.with_name(job_path.name + ".lock")
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                # A crashed writer leaves its lock behind; reclaim it once old.
                if time.time() - lock_path.stat().st_mtime > STALE_LOCK_SECONDS:
                    lock_path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"video-job.json is locked by another stage: {lock_path}")
            time.sleep(0.05)
            continue
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        os.close(descriptor)
        break
    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def read_job(job_path: Path) -> dict[str, Any]:
    value = json.loads(job_path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("video-job.json root must be an object")
    return value


def _write_atomic(job_path: Path, job: dict[str, Any]) -> None:
    temporary = job_path.with_name(f"{job_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Windows refuses to replace a file another process is reading at that
    # instant; the reader finishes within milliseconds, so retry briefly.
    for attempt in range(40):
        try:
            os.replace(temporary, job_path)
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.05)


def update_job(job_path: Path, merge: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """Re-read the plan under the lock, let `merge` build the new plan, write it."""
    with job_lock(job_path):
        current = read_job(job_path) if job_path.is_file() else {}
        result = merge(current)
        _write_atomic(job_path, result)
        return result


def _shots_by_index(job: dict[str, Any]) -> dict[Any, dict[str, Any]]:
    return {
        shot.get("index"): shot
        for shot in job.get("shots") or []
        if isinstance(shot, dict)
    }


def _copy_fields(origin: dict[str, Any], destination: dict[str, Any]) -> None:
    for field in VISUAL_SHOT_FIELDS:
        if field in origin:
            destination[field] = copy.deepcopy(origin[field])
        else:
            destination.pop(field, None)


def copy_visual_fields(source: dict[str, Any], target: dict[str, Any]) -> None:
    """Make `target`'s visual-stage fields equal to `source`'s.

    Shots match by `index`, closing visuals by position. Prompts and paths stay
    with the plan side, so a prompt completed by the validator is never lost.
    """
    source_shots = _shots_by_index(source)
    for index, shot in _shots_by_index(target).items():
        origin = source_shots.get(index)
        if origin is not None:
            _copy_fields(origin, shot)
    source_closing = source.get("closing_visuals") or []
    for position, visual in enumerate(target.get("closing_visuals") or []):
        if position < len(source_closing) and isinstance(visual, dict) and isinstance(source_closing[position], dict):
            _copy_fields(source_closing[position], visual)


def save_plan_job(job_path: Path, job: dict[str, Any]) -> None:
    """Write a plan/audio-stage result while keeping the visual stage's fields."""

    def merge(current: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(job)
        if current:
            copy_visual_fields(current, result)
        return result

    update_job(job_path, merge)


def save_visual_job(job_path: Path, job: dict[str, Any]) -> None:
    """Write only the visual stage's fields into the current plan."""

    def merge(current: dict[str, Any]) -> dict[str, Any]:
        if not current:
            return copy.deepcopy(job)
        copy_visual_fields(job, current)
        draft = job.get("draft")
        current_draft = current.get("draft")
        # A changed visual makes an already saved draft stale; never revive one.
        if (
            isinstance(draft, dict)
            and draft.get("status") == "stale_visual"
            and isinstance(current_draft, dict)
            and current_draft.get("status") == "saved"
        ):
            current_draft["status"] = "stale_visual"
            current_draft["stale_reason"] = draft.get("stale_reason", "source visual or logo changed")
        return current

    update_job(job_path, merge)


def save_whole_job(job_path: Path, job: dict[str, Any]) -> None:
    """Write the full plan under the lock (draft stage, after media finished)."""
    update_job(job_path, lambda _current: copy.deepcopy(job))
