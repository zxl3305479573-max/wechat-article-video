#!/usr/bin/env python3
"""Run every media stage of an approved, validated job with one command.

prepare TTS text -> [narration + timestamps -> duration check] in parallel
with [images] -> tables and Logo -> Jianying draft -> summary.

Every stage skips work whose recorded hash still matches, so rerunning this
command after a failure resumes where it stopped and never pays twice. The
last stdout line is a JSON result; its `summary` text is ready to show the user.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
from pathlib import Path
from typing import Any, Callable

import generate_job_audio
import generate_job_images
import jianying_local_draft
import prepare_local_assets
from job_store import read_job
from prepare_tts_text import prepare_job


class StageError(RuntimeError):
    def __init__(self, stage: str, error: BaseException | str) -> None:
        super().__init__(f"{stage}: {error}")
        self.stage = stage
        self.detail = str(error)


def run_stage(stage: str, action: Callable[[], Any]) -> Any:
    try:
        return action()
    except StageError:
        raise
    except Exception as error:  # every stage failure is reported, never swallowed
        raise StageError(stage, error) from error


def audio_track(job_dir: Path) -> dict[str, Any]:
    result = run_stage("配音与字幕时间", lambda: generate_job_audio.generate(job_dir, sync=True))
    timing = run_stage("时长核对", lambda: jianying_local_draft.verify_duration(job_dir))
    result["total_duration_seconds"] = round(timing["total_duration_us"] / 1_000_000, 3)
    return result


def image_track(job_dir: Path, max_concurrency: int) -> dict[str, Any]:
    result = run_stage("生图", lambda: generate_job_images.generate(job_dir, max_concurrency))
    if result.get("failed"):
        raise StageError("生图", f"{result['failed']} 张图片失败，原因见 video-job.json 的 image_error(s)")
    return result


def local_assets(job_dir: Path) -> dict[str, Any]:
    job_path = job_dir / "video-job.json"
    job = prepare_local_assets.load_planned_job(job_path)
    expected, completed, failed = prepare_local_assets.prepare_once(job_dir, job_path, job, write_job=True)
    if failed:
        raise StageError("表格与 Logo", f"{failed} 个素材失败，原因见 video-job.json 的 local_asset_error")
    return {"prepared": completed, "expected": expected}


def summary(job_dir: Path, draft: dict[str, Any], audio: dict[str, Any], images: dict[str, Any]) -> dict[str, Any]:
    job = read_job(job_dir / "video-job.json")
    shots = job.get("shots") or []
    image_count = sum(
        len(shot.get("image_paths") or []) or 1 for shot in shots if isinstance(shot, dict)
    ) + len(job.get("closing_visuals") or [])
    fallback = [shot.get("index") for shot in shots if shot.get("subtitle_sync_status") == "fallback"]
    titles = job.get("title_candidates") or {}
    items = titles.get("items") or []
    recommended = titles.get("recommended", 0)
    lines = [
        f"封面：{draft.get('cover_path')}",
        f"横版封面：{draft.get('landscape_cover_path')}",
        "",
        f"文案：{(job_dir / 'copy-for-approval.md').resolve()}",
        f"剪映草稿：{draft.get('draft_path')}",
    ]
    if draft.get("draft_backup_path"):
        lines.append(f"旧草稿备份：{draft['draft_backup_path']}")
    lines += [
        f"镜头：{len(shots)}　图片：{image_count}　实测总时长：{draft.get('total_duration_seconds', audio.get('total_duration_seconds'))} 秒",
    ]
    if fallback:
        lines.append(f"注意：镜头 {', '.join(map(str, fallback))} 的字幕时间是估算值（未对齐到真实语音）")
    if images.get("model_source") == "platform-default":
        lines.append(f"生图模型使用平台默认：{images.get('provider')} / {images.get('model')}")
    lines += ["", "视频标题候选："]
    lines += [f"{number}. {title}{'（推荐）' if number - 1 == recommended else ''}" for number, title in enumerate(items, start=1)]
    return {
        "status": "completed",
        "draft_path": draft.get("draft_path"),
        "draft_backup_path": draft.get("draft_backup_path"),
        "cover_path": draft.get("cover_path"),
        "landscape_cover_path": draft.get("landscape_cover_path"),
        "shots": len(shots),
        "images": image_count,
        "total_duration_seconds": draft.get("total_duration_seconds"),
        "subtitle_fallback_shots": fallback,
        "image_provider": images.get("provider"),
        "image_model": images.get("model"),
        "summary": "\n".join(lines),
    }


def run(job_dir: Path, max_concurrency: int = 3, overwrite: bool = False) -> dict[str, Any]:
    # Canonical form: short (8.3) names and junctions must not break path checks.
    job_dir = job_dir.resolve()
    job_path = job_dir / "video-job.json"
    run_stage("准备 TTS 文本", lambda: prepare_job(job_path))
    # Narration and images only share the validated plan; each writes its own
    # fields under a lock (job_store), so they safely run side by side.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        audio_future = pool.submit(audio_track, job_dir)
        image_future = pool.submit(image_track, job_dir, max_concurrency)
        failures: list[StageError] = []
        results: dict[str, Any] = {}
        for name, future in (("audio", audio_future), ("images", image_future)):
            try:
                results[name] = future.result()
            except StageError as error:
                failures.append(error)
    if failures:
        raise StageError(
            "、".join(error.stage for error in failures),
            "；".join(error.detail for error in failures),
        )
    run_stage("表格与 Logo", lambda: local_assets(job_dir))
    draft = run_stage("剪映草稿", lambda: jianying_local_draft.create_draft(job_dir, overwrite=overwrite))
    return summary(job_dir, draft, results["audio"], results["images"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job", required=True, type=Path, help="output\\<job-id> directory")
    parser.add_argument("--max-concurrency", type=int, default=3, help="parallel image requests (1-3)")
    parser.add_argument("--overwrite", action="store_true", help="replace this job's existing Jianying draft")
    args = parser.parse_args()
    if not 1 <= args.max_concurrency <= 3:
        parser.error("--max-concurrency must be between 1 and 3")
    try:
        result = run(args.job.resolve(), args.max_concurrency, args.overwrite)
    except StageError as error:
        print(json.dumps({
            "status": "failed",
            "stage": error.stage,
            "error": error.detail,
            "resume": "修复原因后重新运行同一条 run_media.py 命令；已完成的配音和图片会被复用，不会重复付费",
        }, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
