#!/usr/bin/env python3
"""Prepare local table and logo assets concurrently and resumably."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from PIL import Image
from apply_logo import apply_logo
from render_data_table import read_table, render
from validate_video_job import require_verified_duration, validate
from video_constants import shot_image_paths


LOGO_RENDER_VERSION = b"direct-source-logo-v7"
WATCH_STATE_DIRECTORY = ".local-assets"
PRECOMPUTE_STATE_FILE = "precompute.json"


def save_job(path: Path, job: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_verified_job(job_path: Path) -> dict[str, Any]:
    """Block local image work until the current MP3 timeline is verified."""
    validate(job_path)
    job = json.loads(job_path.read_text(encoding="utf-8-sig"))
    if not isinstance(job, dict):
        raise ValueError("video-job.json root must be an object")
    require_verified_duration(job)
    return job


def load_planned_job(job_path: Path) -> dict[str, Any]:
    """Load an approved plan for work that does not depend on MP3 timing."""
    validate(job_path)
    job = json.loads(job_path.read_text(encoding="utf-8-sig"))
    if not isinstance(job, dict):
        raise ValueError("video-job.json root must be an object")
    return job


def resolve_job_path(job_dir: Path, value: str) -> Path:
    path = (job_dir / value).resolve()
    try:
        path.relative_to(job_dir)
    except ValueError as exc:
        raise ValueError(f"asset path must stay inside the job directory: {value}") from exc
    return path


def image_path_for_shot(job_dir: Path, shot: dict[str, Any], index: int) -> Path:
    values = shot_image_paths(shot)
    if values:
        return resolve_job_path(job_dir, values[0])
    return job_dir / "images" / f"{index:03d}.png"


def visual_entries(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Return article and closing visuals with stable watcher indexes."""
    entries: list[dict[str, Any]] = []
    for shot in job.get("shots", []):
        if isinstance(shot, dict):
            paths = shot.get("image_paths")
            if isinstance(paths, list) and len(paths) >= 2:
                prompts = shot.get("image_prompts") if isinstance(shot.get("image_prompts"), list) else []
                for variant, path in enumerate(paths, start=1):
                    item = {**shot, "index": int(shot["index"]) * 100 + variant, "image_path": path}
                    if variant <= len(prompts):
                        item["image_prompt"] = prompts[variant - 1]
                    entries.append(item)
            else:
                entries.append(shot)
    for position, visual in enumerate(job.get("closing_visuals", []), start=1):
        if isinstance(visual, dict):
            entries.append(
                {
                    **visual,
                    "index": int(visual.get("index") or 90_000 + position),
                    "visual_type": str(visual.get("visual_type") or "ai_image"),
                }
            )
    return entries


def visual_parent_variant(job: dict[str, Any], index: int) -> tuple[dict[str, Any], int | None]:
    """Resolve a watcher index to its parent shot and optional image variant."""
    for shot in job.get("shots", []):
        if not isinstance(shot, dict):
            continue
        shot_index = int(shot.get("index", 0))
        if shot_index == index:
            return shot, None
        if index // 100 == shot_index and 1 <= index % 100 <= len(shot.get("image_paths", [])):
            return shot, index % 100 - 1
    for position, visual in enumerate(job.get("closing_visuals", []), start=1):
        if not isinstance(visual, dict):
            continue
        visual_index = int(visual.get("index") or 90_000 + position)
        if visual_index == index:
            return visual, None
    raise ValueError(f"visual entry {index} is missing from video-job.json")


def watch_state_path(job_dir: Path, index: int) -> Path:
    return job_dir / WATCH_STATE_DIRECTORY / f"{index:03d}.json"


def read_watch_state(job_dir: Path, index: int) -> dict[str, Any]:
    path = watch_state_path(job_dir, index)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_watch_state(job_dir: Path, index: int, state: dict[str, Any]) -> None:
    path = watch_state_path(job_dir, index)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def save_precompute_state(job_dir: Path, state: dict[str, Any]) -> None:
    path = job_dir / WATCH_STATE_DIRECTORY / PRECOMPUTE_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def local_asset_task(job_dir: Path, shot: dict[str, Any], logo_path: Path) -> tuple[int, str, dict[str, str]]:
    index = int(shot["index"])
    image_path = image_path_for_shot(job_dir, shot, index)
    logo_image_path = job_dir / "images" / "logo" / f"{index:03d}.png"
    visual_type = str(shot.get("visual_type") or "ai_image")
    result: dict[str, str] = {}
    if visual_type == "data_table":
        table_value = shot.get("table_path") or f"tables/{index:03d}.json"
        table_path = resolve_job_path(job_dir, str(table_value))
        title, columns, rows = read_table(table_path)
        render_hash = hashlib.sha256(table_path.read_bytes()).hexdigest()
        if not image_path.is_file() or shot.get("table_source_hash") != render_hash:
            render(title, columns, rows, image_path)
        result["table_source_hash"] = render_hash
    if not image_path.is_file() or image_path.stat().st_size == 0:
        raise ValueError(f"shot {index} image is not a non-empty file: {image_path}")
    with Image.open(image_path) as image:
        if image.width * 10 != image.height * 14:
            raise ValueError(
                f"shot {index} image must be an actual 14:10 file, got {image.width}x{image.height}: {image_path}"
            )
    logo_hash = hashlib.sha256(
        LOGO_RENDER_VERSION + image_path.read_bytes() + logo_path.read_bytes()
    ).hexdigest()
    if not logo_image_path.is_file() or shot.get("logo_source_hash") != logo_hash:
        apply_logo(image_path, logo_image_path, logo_path)
    result["image_path"] = str(image_path.relative_to(job_dir)).replace("\\", "/")
    result["logo_image_path"] = str(logo_image_path.relative_to(job_dir)).replace("\\", "/")
    result["logo_source_hash"] = logo_hash
    return index, "completed", result


def local_shots(job_dir: Path, job: dict[str, Any]) -> list[dict[str, Any]]:
    shots: list[dict[str, Any]] = []
    for shot in visual_entries(job):
        if str(shot.get("visual_type") or "ai_image") not in {"ai_image", "data_table"}:
            continue
        index = int(shot["index"])
        state = read_watch_state(job_dir, index)
        if state.get("status") == "completed" and isinstance(state.get("result"), dict):
            # Watch mode never edits the shared plan. Reuse its per-shot hash
            # record here so unchanged visuals are not repeatedly logo-stamped.
            shot = {**shot, **state["result"]}
        image_exists = any(
            resolve_job_path(job_dir, value).is_file() for value in shot_image_paths(shot)
        )
        if str(shot.get("visual_type") or "ai_image") == "data_table" or image_exists:
            shots.append(shot)
    return shots


def prepare_once(
    job_dir: Path,
    job_path: Path,
    job: dict[str, Any],
    *,
    write_job: bool,
) -> tuple[int, int, int]:
    logo_path = Path(__file__).resolve().parents[1] / "assets" / "logo.png"
    shots = local_shots(job_dir, job)
    expected = [item for item in visual_entries(job) if str(item.get("visual_type") or "ai_image") in {"ai_image", "data_table"}]
    available = {int(item["index"]) for item in shots}
    missing = [item for item in expected if int(item["index"]) not in available]
    results_missing: dict[int, tuple[str, dict[str, Any]]] = {}
    if write_job:
        for item in missing:
            index = int(item["index"])
            error = f"visual image is missing: {image_path_for_shot(job_dir, item, index)}"
            results_missing[index] = ("failed", {"local_asset_error": error})
    completed = 0
    failed = 0
    # Thread workers avoid Windows spawn requirements when this script is
    # invoked by a wrapper, while PIL rendering releases the GIL.
    workers = min(4, os.cpu_count() or 1, max(1, len(shots)))
    results: dict[int, tuple[str, dict[str, Any]]] = {}
    if write_job:
        results.update(results_missing)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(local_asset_task, job_dir, shot, logo_path): shot for shot in shots}
        for future in concurrent.futures.as_completed(futures):
            shot = futures[future]
            index = int(shot["index"])
            try:
                _index, status, result = future.result()
                completed += 1
                state: dict[str, Any] = {"status": status, "result": result}
                results[index] = (status, result)
            except Exception as exc:
                failed += 1
                state = {"status": "failed", "error": str(exc)}
                results[index] = ("failed", {"local_asset_error": str(exc)})
            # The per-visual state is the recovery checkpoint. It is written
            # even in write_job mode so an interrupted run can resume without
            # relying on a partially serialized shared job document.
            save_watch_state(job_dir, index, state)

    if write_job:
        # Merge deterministically after all workers finish, then serialize the
        # shared plan exactly once for this preparation phase.
        for index in sorted(results):
            status, result = results[index]
            original, variant = visual_parent_variant(job, index)
            if variant is not None:
                statuses = original.setdefault("image_statuses", ["planned"] * len(original["image_paths"]))
                logo_paths = original.setdefault("logo_image_paths", [None] * len(original["image_paths"]))
                logo_hashes = original.setdefault("image_logo_source_hashes", [None] * len(original["image_paths"]))
                while len(statuses) < len(original["image_paths"]):
                    statuses.append("planned")
                while len(logo_paths) < len(original["image_paths"]):
                    logo_paths.append(None)
                while len(logo_hashes) < len(original["image_paths"]):
                    logo_hashes.append(None)
                if status == "completed":
                    statuses[variant] = status
                    logo_paths[variant] = result.get("logo_image_path")
                    logo_hashes[variant] = result.get("logo_source_hash")
                    original["image_status"] = "completed" if all(item == "completed" for item in statuses) else "pending"
                    original["logo_status"] = "applied" if all(item for item in logo_paths) else "pending"
                    original.pop("local_asset_error", None)
                else:
                    statuses[variant] = "failed"
                    original["image_status"] = "failed"
                    original["logo_status"] = "failed"
                    original["local_asset_error"] = result.get("local_asset_error", "asset preparation failed")
                continue
            if status == "completed":
                previous_logo_hash = original.get("logo_source_hash")
                original.update(result)
                original["image_status"] = status
                original["logo_status"] = "applied"
                original.pop("local_asset_error", None)
                if previous_logo_hash != result.get("logo_source_hash"):
                    draft = job.get("draft")
                    if isinstance(draft, dict) and draft.get("status") == "saved":
                        draft["status"] = "stale_visual"
                        draft["stale_reason"] = "source visual or logo changed"
            else:
                original["image_status"] = "failed"
                original["logo_status"] = "failed"
                original["local_asset_error"] = result.get("local_asset_error", "asset preparation failed")
        save_job(job_path, job)
    # Missing AI images are expected during precompute/watch; only the final
    # write-back phase turns them into plan failures.
    return len(expected), completed, failed + (len(missing) if write_job else 0)


def watch(job_dir: Path, job_path: Path, interval: float, timeout: float) -> int:
    """Prepare tables and logo assets without writing the shared job JSON."""
    started = time.monotonic()
    while True:
        job = load_verified_job(job_path)
        visual_work = [
            item
            for item in visual_entries(job)
            if str(item.get("visual_type") or "ai_image") in {"ai_image", "data_table"}
        ]
        total_visuals = len(visual_work)
        _ready, _completed, failed = prepare_once(job_dir, job_path, job, write_job=False)
        states = [read_watch_state(job_dir, int(shot["index"])) for shot in visual_work]
        done = sum(state.get("status") == "completed" for state in states)
        if failed:
            print(json.dumps({"status": "failed", "prepared": done, "total": total_visuals}, ensure_ascii=False))
            return 1
        if done == total_visuals:
            print(json.dumps({"status": "completed", "prepared": done, "total": total_visuals}, ensure_ascii=False))
            return 0
        if time.monotonic() - started >= timeout:
            print(json.dumps({"status": "timeout", "prepared": done, "total": total_visuals}, ensure_ascii=False))
            return 1
        time.sleep(interval)


def precompute(job_dir: Path, job_path: Path) -> int:
    """Render tables and stamp already available images before MP3 verification."""
    job = load_planned_job(job_path)
    required_templates = ("header.jpg", "header1.jpg", "header2.png", "footer.mp4", "logo.png")
    assets_dir = Path(__file__).resolve().parents[1] / "assets"
    template_hashes: dict[str, str] = {}
    for name in required_templates:
        path = assets_dir / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"required template asset is missing: {path}")
        template_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    shots, completed, failed = prepare_once(job_dir, job_path, job, write_job=False)
    state = {
        "status": "failed" if failed else "completed",
        "prepared": completed,
        "available_visuals": shots,
        "template_hashes": template_hashes,
    }
    save_precompute_state(job_dir, state)
    print(json.dumps(state, ensure_ascii=False))
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--watch", action="store_true", help="Wait for AI images and prepare local assets without editing video-job.json")
    parser.add_argument("--precompute", action="store_true", help="Render tables and stamp already available images before MP3 duration verification")
    parser.add_argument("--watch-interval", type=float, default=1.0)
    parser.add_argument("--watch-timeout", type=float, default=3600.0)
    args = parser.parse_args()
    job_dir = args.job.resolve()
    job_path = job_dir / "video-job.json"
    if args.precompute:
        if args.watch:
            raise ValueError("--precompute and --watch cannot be used together")
        return precompute(job_dir, job_path)
    job = load_verified_job(job_path)
    if args.watch:
        if args.watch_interval <= 0 or args.watch_timeout <= 0:
            raise ValueError("watch interval and timeout must be positive")
        return watch(job_dir, job_path, args.watch_interval, args.watch_timeout)
    shots, _completed, failed = prepare_once(job_dir, job_path, job, write_job=True)
    print(json.dumps({"status": "failed" if failed else "completed", "prepared": shots, "failed": failed}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
