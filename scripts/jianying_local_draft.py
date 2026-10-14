#!/usr/bin/env python3
"""Create an editable local Jianying draft from a completed article-video job."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from array import array
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from pymediainfo import MediaInfo
import imageio_ffmpeg
from apply_logo import apply_logo
from brand import load_brand
from job_store import save_plan_job, save_whole_job
from subtitle_timing import (
    display_text,
    display_width_units,
    split_timeline_segments,
    visible_subtitle_segments,
)
from validate_video_job import timeline_audio_hash, validate
from video_constants import FOOTER_CAPTIONS, LOGO_RENDER_VERSION, shot_image_paths


JY_VENDOR_ROOT = Path(__file__).resolve().parent / "jianying_editor" / "scripts" / "vendor"
BRAND = load_brand()
# ClipSettings.transform_y is normalized, while Jianying's inspector displays
# the equivalent offset in full-canvas pixels; brand.json holds the inspector
# value (Y=-938 for the bundled template).
TEMPLATE_SUBTITLE_TRANSFORM_X = 0.0
TEMPLATE_SUBTITLE_TRANSFORM_Y = BRAND.layout["subtitle_y"] / 1920.0
TEMPLATE_SUBTITLE_SIZE = 9.0
TEMPLATE_SUBTITLE_BORDER_WIDTH = 55.0
TEMPLATE_SUBTITLE_FILL = (0.949, 0.918, 0.855)  # #F2EADD
TEMPLATE_SUBTITLE_OUTLINE = (0.294, 0.294, 0.271)  # #4B4B45
# HeaderGrade is an independent template text layer. The draft library stores
# Jianying inspector Y coordinates as a full-canvas pixel value divided by
# 1920 (inspector Y=-1353 for the bundled template becomes -1353 / 1920).
HEADER_GRADE_SIZE = 26.0
HEADER_GRADE_TRANSFORM_Y = BRAND.layout["header_grade_y"] / 1920.0
TRACK_VISUAL_SCALE = 0.90

if max(abs(TEMPLATE_SUBTITLE_TRANSFORM_X), abs(TEMPLATE_SUBTITLE_TRANSFORM_Y)) > 1.0:
    raise ValueError("subtitle transform must be normalized to the [-1, 1] canvas range")

DRAFT_LAYOUT_VERSION = "template-overlay-v34-portable-draft-media"
TEMPLATE_PATH = BRAND.assets["template_overlay"]
EXPORT_COVER_TEMPLATE_PATH = BRAND.assets["cover_template"]
LANDSCAPE_COVER_TEMPLATE_PATH = BRAND.assets["landscape_cover_template"]
FOOTER_PATH = BRAND.assets["footer_video"]
FOOTER_AUDIO_RELATIVE_PATH = "audio/footer.mp3"
LOGO_PATH = BRAND.assets["logo"]
# Near-full-width 14:10 display frame inside the template, with a subtitle
# band below it in the blurred fill area (brand.json `layout.image_frame`).
CLEAR_VIDEO_FRAME = BRAND.layout["image_frame"]
TRANSITION_SEQUENCE = ("叠化", "向上擦除", "向左擦除", "滑动", "闪白_II")
# Kept as a name for existing callers; the captions live in video_constants.
FOOTER_SUBTITLE_TEMPLATES = FOOTER_CAPTIONS
CLOSING_VISUAL_COUNT = 2


def transition_for_boundary(draft: Any, boundary_index: int) -> Any:
    """Rotate restrained native transitions across timeline boundaries."""
    return getattr(draft.TransitionType, TRANSITION_SEQUENCE[boundary_index % len(TRANSITION_SEQUENCE)])


MEDIA_CACHE_NAME = ".media-cache.json"
# A rebuild replaces the whole draft folder, so keep the last few timeline
# snapshots inside the job before anything is deleted.
DRAFT_BACKUP_KEEP = 5
COVER_RENDER_VERSION = "cover-v2-grade-scale-082"
TEMPLATE_OVERLAY_RENDER_VERSION = "template-overlay-v2-no-baked-grade"


def _media_cache_key(path: Path) -> tuple[str, int, int]:
    resolved = path.resolve()
    stat = resolved.stat()
    return str(resolved), stat.st_size, stat.st_mtime_ns


def _read_media_cache(cache_path: Path) -> dict[str, Any]:
    try:
        value = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value.get("entries", {}) if isinstance(value, dict) and isinstance(value.get("entries"), dict) else {}


def _write_media_cache(cache_path: Path, entries: dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
    temporary.write_text(json.dumps({"version": 1, "entries": entries}, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, cache_path)


def media_duration_us(path: Path, media_type: str, *, cache_path: Path | None = None) -> int:
    resolved, size, mtime_ns = _media_cache_key(path)
    cache_key = f"{resolved}|{media_type}"
    entries = _read_media_cache(cache_path) if cache_path else {}
    cached = entries.get(cache_key)
    if (
        isinstance(cached, dict)
        and cached.get("size") == size
        and cached.get("mtime_ns") == mtime_ns
        and isinstance(cached.get("duration_us"), int)
        and cached["duration_us"] > 0
    ):
        return cached["duration_us"]

    digest = hashlib.sha256(Path(resolved).read_bytes()).hexdigest()
    info = MediaInfo.parse(resolved)
    tracks = info.audio_tracks if media_type == "audio" else info.video_tracks
    if not tracks or not tracks[0].duration:
        raise ValueError(f"Unable to read {media_type} duration: {path}")
    duration_us = int(round(float(tracks[0].duration) * 1000))
    if duration_us <= 0:
        raise ValueError(f"{media_type.capitalize()} duration is invalid: {path}")
    if cache_path:
        entries[cache_key] = {
            "path": resolved,
            "media_type": media_type,
            "size": size,
            "mtime_ns": mtime_ns,
            "sha256": digest,
            "duration_us": duration_us,
        }
        _write_media_cache(cache_path, entries)
    return duration_us


def audio_duration_us(path: Path, *, cache_path: Path | None = None) -> int:
    return media_duration_us(path, "audio", cache_path=cache_path)


def template_visual_settings(draft: Any, path: Path) -> Any:
    """Place visual media in the clear 14:10 template frame without stretching."""
    source_ratio = visual_aspect_ratio(path)
    frame_width = CLEAR_VIDEO_FRAME[2] - CLEAR_VIDEO_FRAME[0]
    frame_height = CLEAR_VIDEO_FRAME[3] - CLEAR_VIDEO_FRAME[1]
    frame_ratio = frame_width / frame_height
    # Jianying's default scale width-fits landscape media to the canvas. Scale
    # only enough for wider material (such as the 16:9 footer) to cover 14:10.
    cover_scale = max(1.0, source_ratio / frame_ratio)
    display_scale = TRACK_VISUAL_SCALE * cover_scale
    frame_center_y = (CLEAR_VIDEO_FRAME[1] + CLEAR_VIDEO_FRAME[3]) / 2
    transform_y = -(frame_center_y - 960) / 960
    return draft.ClipSettings(
        scale_x=display_scale,
        scale_y=display_scale,
        transform_y=transform_y,
    )


def visual_aspect_ratio(path: Path) -> float:
    """Read image/video dimensions once per unchanged source file."""
    resolved, size, mtime_ns = _media_cache_key(path)
    cache_path = path.parent / MEDIA_CACHE_NAME
    entries = _read_media_cache(cache_path)
    cache_key = f"dimensions|{resolved}"
    cached = entries.get(cache_key)
    if (
        isinstance(cached, dict)
        and cached.get("size") == size
        and cached.get("mtime_ns") == mtime_ns
        and isinstance(cached.get("aspect_ratio"), (int, float))
        and cached["aspect_ratio"] > 0
    ):
        return float(cached["aspect_ratio"])
    info = MediaInfo.parse(resolved)
    tracks = info.video_tracks or info.image_tracks
    if not tracks or not tracks[0].width or not tracks[0].height:
        raise ValueError(f"Unable to read visual dimensions: {path}")
    ratio = float(tracks[0].width) / float(tracks[0].height)
    entries[cache_key] = {"size": size, "mtime_ns": mtime_ns, "aspect_ratio": ratio}
    _write_media_cache(cache_path, entries)
    return ratio


def make_template_visual_segment(
    draft: Any, path: Path, start_us: int, duration_us: int, *, volume: float = 0
) -> Any:
    """Create one base-track visual with template-frame layout and blur fill."""
    segment = draft.VideoSegment(
        draft.VideoMaterial(str(path)),
        draft.Timerange(start_us, duration_us),
        source_timerange=draft.Timerange(0, duration_us),
        volume=volume,
        clip_settings=template_visual_settings(draft, path),
    )
    segment.add_background_filling("blur", blur=0.75)
    return segment


def split_subtitle_timing(
    text: str, duration_us: int, *, spoken_text: str | None = None
) -> list[tuple[str, int]]:
    """Split visible subtitles, weighting each segment by spoken text length."""
    sentences = split_timeline_segments(text)
    if not sentences:
        raise ValueError("subtitle is empty")
    if len(sentences) == 1:
        return [(sentences[0], duration_us)]

    spoken_sentences = split_timeline_segments(spoken_text or text)
    if len(spoken_sentences) != len(sentences):
        spoken_sentences = sentences
    weights = [
        max(1, len(re.sub(r"[，、；：。！？!?\s]", "", sentence)))
        for sentence in spoken_sentences
    ]
    minimum_us = 800_000
    if duration_us < minimum_us * len(sentences):
        minimum_us = max(250_000, duration_us // (len(sentences) * 2))
    distributable_us = duration_us - minimum_us * len(sentences)
    total_weight = sum(weights)
    allocated = [minimum_us + distributable_us * weight // total_weight for weight in weights]
    allocated[-1] += duration_us - sum(allocated)
    return list(zip(sentences, allocated))


MIN_PAUSE_FRAMES = 4  # 80 ms of low energy at 20 ms per frame
# Subtitle segments timed from real speech: the TTS engine's own word
# timestamps, or ASR on the MP3 for audio made before those existed.
MEASURED_TIMING_SOURCES = ("tts_word_timestamps", "asr_sentence_timestamps")


def waveform_pause_timing(
    text: str, duration_us: int, audio_path: Path, *, spoken_text: str | None = None
) -> list[tuple[str, int]] | None:
    """Align authored sentences to nearby low-energy pauses in the rendered audio."""
    sentences = split_timeline_segments(text)
    if len(sentences) < 2 or not audio_path.is_file():
        return None
    try:
        import imageio_ffmpeg

        pcm = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-i", str(audio_path),
             "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"],
            check=True, capture_output=True,
        ).stdout
        samples = array("h", pcm)
        frame = 320  # 20 ms at 16 kHz
        energy = [
            sum(abs(value) for value in samples[pos : pos + frame]) / max(1, min(frame, len(samples) - pos))
            for pos in range(0, len(samples), frame)
        ]
        if len(energy) < 10:
            return None
        floor = sorted(energy)[max(0, len(energy) // 10)]
        threshold = max(180.0, floor * 1.8)
        # A single quiet 20 ms frame is just the dip between two syllables.
        # Only a sustained run is a spoken pause; snap to its centre.
        pauses: list[int] = []
        run_start: int | None = None
        for index, value in enumerate([*energy, threshold + 1]):
            if value <= threshold:
                if run_start is None:
                    run_start = index
                continue
            if run_start is not None and index - run_start >= MIN_PAUSE_FRAMES:
                pauses.append((run_start + index) * 20_000 // 2)
            run_start = None
        if not pauses:
            return None
        spoken_sentences = split_timeline_segments(spoken_text or text)
        if len(spoken_sentences) != len(sentences):
            spoken_sentences = sentences
        weights = [max(1, len(re.sub(r"[，、；：。！？!?\s]", "", part))) for part in spoken_sentences]
        total_weight = sum(weights)
        targets: list[int] = []
        cursor = 0
        for weight in weights[:-1]:
            cursor += weight
            targets.append(round(duration_us * cursor / total_weight))
        boundaries = [0]
        for target in targets:
            window = [pause for pause in pauses if abs(pause - target) <= 1_000_000]
            if not window:
                return None
            chosen = min(window, key=lambda pause: abs(pause - target))
            if chosen <= boundaries[-1] + 200_000 or chosen >= duration_us - 200_000:
                return None
            boundaries.append(chosen)
        boundaries.append(duration_us)
        return [(part, boundaries[index + 1] - boundaries[index]) for index, part in enumerate(sentences)]
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None


def subtitle_parts_for_shot(shot: dict[str, Any], duration_us: int, audio_path: Path | None = None) -> list[tuple[str, int, int]]:
    """Return ASR timings when usable, otherwise local punctuation timings."""
    saved_segments = shot.get("asr_subtitle_segments")
    if shot.get("subtitle_timing_source") in MEASURED_TIMING_SOURCES and isinstance(
        saved_segments, list
    ):
        parts: list[tuple[str, int, int]] = []
        for segment in saved_segments:
            if not isinstance(segment, dict):
                parts = []
                break
            text = str(segment.get("text") or "").strip()
            start_ms, end_ms = segment.get("start_ms"), segment.get("end_ms")
            if (
                not text
                or not isinstance(start_ms, (int, float))
                or not isinstance(end_ms, (int, float))
                or end_ms <= start_ms
            ):
                parts = []
                break
            start_us, end_us = round(start_ms * 1000), round(end_ms * 1000)
            if start_us < 0 or end_us > duration_us + 50_000:
                parts = []
                break
            parts.append((text, start_us, end_us - start_us))
        if parts:
            if len(parts) == 1:
                # ASR often emits one timestamp for an entire short clip. Keep
                # authored text so punctuation can guide semantic splits.
                visible_text = str(shot.get("subtitle") or shot.get("narration") or "").strip()
                if visible_text:
                    _, start_us, part_duration_us = parts[0]
                    estimated_parts = waveform_pause_timing(
                        visible_text, part_duration_us, audio_path, spoken_text=str(shot.get("tts_text") or shot.get("narration") or "")
                    ) if audio_path else None
                    estimated_parts = estimated_parts or split_subtitle_timing(
                        visible_text,
                        part_duration_us,
                        spoken_text=str(shot.get("tts_text") or shot.get("narration") or ""),
                    )
                    timed_parts: list[tuple[str, int, int]] = []
                    cursor_us = start_us
                    for text, segment_duration_us in estimated_parts:
                        timed_parts.append((text, cursor_us, segment_duration_us))
                        cursor_us += segment_duration_us
                    return add_sentence_pauses(timed_parts)
            return add_sentence_pauses(parts)

    visible_text = str(shot.get("subtitle") or shot.get("narration") or "").strip()
    estimated_parts = waveform_pause_timing(
        visible_text, duration_us, audio_path, spoken_text=str(shot.get("tts_text") or shot.get("narration") or "")
    ) if audio_path else None
    estimated_parts = estimated_parts or split_subtitle_timing(
        visible_text,
        duration_us,
        spoken_text=str(shot.get("tts_text") or shot.get("narration") or ""),
    )
    cursor_us = 0
    timed_parts: list[tuple[str, int, int]] = []
    for text, part_duration_us in estimated_parts:
        timed_parts.append((text, cursor_us, part_duration_us))
        cursor_us += part_duration_us
    return add_sentence_pauses(timed_parts)


def add_sentence_pauses(parts: list[tuple[str, int, int]]) -> list[tuple[str, int, int]]:
    """Keep adjacent subtitles visible through ASR pauses."""
    if len(parts) < 2:
        return parts
    normalized = list(parts)
    for index in range(len(normalized) - 1):
        text, start_us, duration = normalized[index]
        next_start_us = normalized[index + 1][1]
        end_us = start_us + duration
        if next_start_us > end_us:
            normalized[index] = (text, start_us, next_start_us - start_us)
    return normalized


def subtitle_width_units(text: str) -> float:
    """Approximate the width of mixed Chinese and Latin subtitle text."""
    return display_width_units(text)


def timeline_subtitle_parts(text: str) -> list[str]:
    """Split only at comma, sentence, or semicolon boundaries."""
    return visible_subtitle_segments(text)


def one_line_subtitle_parts(text: str) -> list[str]:
    """Split overflow only at safe authored spaces."""
    return visible_subtitle_segments(text)

def split_subtitle_lines(parts: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Split the subtitle timeline at requested punctuation boundaries."""
    result: list[tuple[str, int]] = []
    for raw_text, duration_us in parts:
        source_text = str(raw_text).strip()
        if not subtitle_display_text(source_text):
            continue
        timeline_parts = [
            line
            for part in timeline_subtitle_parts(source_text)
            for line in one_line_subtitle_parts(part)
        ]
        weights = [max(1.0, subtitle_width_units(part)) for part in timeline_parts]
        total_weight = sum(weights)
        part_durations = [round(duration_us * weight / total_weight) for weight in weights]
        part_durations[-1] += duration_us - sum(part_durations)
        result.extend(zip(timeline_parts, part_durations))
    return result


def split_timed_subtitle_lines(parts: list[tuple[str, int, int]]) -> list[tuple[str, int, int]]:
    """Split within each ASR interval without changing its true start offset."""
    result: list[tuple[str, int, int]] = []
    for text, start_us, duration_us in parts:
        lines = split_subtitle_lines([(text, duration_us)])
        cursor_us = start_us
        for line, line_duration_us in lines:
            result.append((line, cursor_us, line_duration_us))
            cursor_us += line_duration_us
    return result


def validate_subtitle_timing(parts: list[tuple[str, int]], audio_duration_us: int, label: str) -> None:
    """Reject a draft whose sequential subtitle timing drifts from its MP3."""
    if not parts:
        raise ValueError(f"{label} has no subtitle segments")
    total_duration_us = sum(duration_us for _, duration_us in parts)
    if total_duration_us != audio_duration_us:
        raise ValueError(
            f"{label} subtitle duration {total_duration_us} does not match audio duration {audio_duration_us}"
        )
    if any(not text.strip() or duration_us <= 0 for text, duration_us in parts):
        raise ValueError(f"{label} has an empty or non-positive subtitle segment")


def footer_subtitles(product_grade: str) -> tuple[str, ...]:
    """Build the fixed closing captions with the current article's grade."""
    grade = product_grade.strip()
    if not grade:
        raise ValueError("video-job.json must contain a non-empty product_grade for footer captions")
    return tuple(template.format(product_grade=grade) for template in FOOTER_SUBTITLE_TEMPLATES)


def footer_subtitle_timing(
    subtitles: tuple[str, ...], duration_us: int
) -> list[tuple[str, int]]:
    """Allocate footer voice-over duration by closing-caption length."""
    weights = [max(1, len(text)) for text in subtitles]
    total_weight = sum(weights)
    allocated = [duration_us * weight // total_weight for weight in weights]
    allocated[-1] += duration_us - sum(allocated)
    return list(zip(subtitles, allocated))


def footer_caption_timings(job_dir: Path, job: dict[str, Any], subtitles: tuple[str, ...], fallback_duration_us: int) -> list[tuple[str, int]]:
    """Use per-caption MP3 durations; fall back for legacy jobs."""
    audio = job.get("audio") if isinstance(job.get("audio"), dict) else {}
    paths = audio.get("footer_caption_paths")
    if not isinstance(paths, list) or len(paths) != len(subtitles):
        return footer_subtitle_timing(subtitles, fallback_duration_us)
    try:
        values = [audio_duration_us(resolve_asset(job_dir, path, f"footer caption {index + 1}"), cache_path=job_dir / MEDIA_CACHE_NAME) for index, path in enumerate(paths)]
    except (OSError, TypeError, ValueError):
        return footer_subtitle_timing(subtitles, fallback_duration_us)
    if any(value <= 0 for value in values):
        return footer_subtitle_timing(subtitles, fallback_duration_us)
    # Use each standalone footer MP3 as the timing authority.  Do not force
    # the concatenated footer.mp3 duration into the last caption: MP3 encoder
    # delay/padding at concat boundaries otherwise shifts later subtitles.
    return list(zip(subtitles, values))


def footer_subtitle_display_parts(parts: list[tuple[str, int]]) -> list[tuple[str, int, int]]:
    """Display each fixed-footer caption from the exact start of its audio."""
    result: list[tuple[str, int, int]] = []
    cursor_us = 0
    for text, duration_us in parts:
        result.append((text, cursor_us, duration_us))
        cursor_us += duration_us
    return result


def split_footer_captions_for_video(
    timings: list[tuple[str, int]], video_duration_us: int
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Keep whole captions while making the closing audio fill the fixed footer exactly."""
    total_duration_us = sum(duration_us for _, duration_us in timings)
    if video_duration_us >= total_duration_us:
        return [], timings
    suffix_start = len(timings)
    suffix_duration_us = 0
    for index in range(len(timings) - 1, -1, -1):
        caption_duration_us = timings[index][1]
        if suffix_duration_us + caption_duration_us > video_duration_us:
            break
        suffix_start = index
        suffix_duration_us += caption_duration_us
    pre_footer = timings[:suffix_start]
    footer = timings[suffix_start:]
    if not pre_footer or not footer:
        raise ValueError("Unable to allocate complete closing captions around footer.mp4")
    # Keep every caption intact, but use the full 17-second voice-over without
    # adding silence before the fixed 8-second footer video.
    return (
        pre_footer,
        footer,
    )


def split_closing_timings(
    timings: list[tuple[str, int]], count: int = CLOSING_VISUAL_COUNT
) -> list[list[tuple[str, int]]]:
    """Divide pre-footer captions across a fixed number of new ending visuals."""
    if len(timings) < count:
        raise ValueError(
            f"The pre-footer closing copy needs at least {count} captions for {count} visuals"
        )
    target_duration_us = sum(duration_us for _, duration_us in timings) / count
    groups: list[list[tuple[str, int]]] = []
    current: list[tuple[str, int]] = []
    current_duration_us = 0
    for index, item in enumerate(timings):
        remaining_items = len(timings) - index
        remaining_groups = count - len(groups)
        if (
            current
            and len(groups) < count - 1
            and (
                current_duration_us >= target_duration_us
                or remaining_items == remaining_groups - 1
            )
        ):
            groups.append(current)
            current = []
            current_duration_us = 0
        current.append(item)
        current_duration_us += item[1]
    groups.append(current)
    if len(groups) != count or any(not group for group in groups):
        raise ValueError(f"Unable to split closing copy into {count} visual groups")
    return groups


def prepare_closing_visuals(
    job_dir: Path,
    job: dict[str, Any],
    article_visual_paths: set[Path],
    caption_groups: list[list[tuple[str, int]]],
) -> list[tuple[dict[str, Any], Path, int]]:
    """Load exactly two new images for the closing copy before footer.mp4."""
    closing_visuals = job.get("closing_visuals")
    if not isinstance(closing_visuals, list) or len(closing_visuals) != CLOSING_VISUAL_COUNT:
        raise ValueError(
            f"video-job.json must contain exactly {CLOSING_VISUAL_COUNT} closing_visuals before footer.mp4"
        )

    used_paths = set(article_visual_paths)
    prepared: list[tuple[dict[str, Any], Path, int]] = []
    for position, (visual, captions) in enumerate(zip(closing_visuals, caption_groups), start=1):
        if not isinstance(visual, dict):
            raise ValueError(f"closing_visuals[{position}] must be an object")
        image_path = resolve_asset(
            job_dir, visual.get("image_path"), f"closing_visuals[{position}] image_path"
        ).resolve()
        if image_path in used_paths:
            raise ValueError(
                f"closing_visuals[{position}] must be distinct from all article and closing visuals"
            )
        visual.setdefault("index", 90_000 + position)
        visual.setdefault("visual_type", "ai_image")
        marked_path = logo_image(job_dir, visual, image_path)
        visual["caption_texts"] = [text for text, _ in captions]
        visual["duration_ms"] = round(sum(duration_us for _, duration_us in captions) / 1000)
        used_paths.add(image_path)
        prepared.append((visual, marked_path, sum(duration_us for _, duration_us in captions)))
    return prepared


def subtitle_display_text(text: str) -> str:
    """Remove sentence punctuation while preserving numeric separators."""
    return display_text(text)


def load_job(job_dir: Path) -> tuple[Path, dict[str, Any]]:
    job_path = job_dir / "video-job.json"
    if not job_path.is_file():
        raise ValueError(f"video-job.json not found: {job_path}")
    validate(job_path)
    job = json.loads(job_path.read_text(encoding="utf-8-sig"))
    if not isinstance(job, dict) or not isinstance(job.get("shots"), list):
        raise ValueError("video-job.json must contain a shots array")
    return job_path, job


def save_job(job_path: Path, job: dict[str, Any]) -> None:
    """Draft-stage write: runs after media finished, so it owns the whole plan."""
    save_whole_job(job_path, job)


def _stat_signature(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def render_covers_cached(job_dir: Path, source_image: Path, product_grade: str) -> tuple[Path, Path]:
    """Reuse cover renders while source, templates, grade, and renderer stay unchanged."""
    from render_export_cover import render as render_export_cover
    from render_export_cover import render_landscape_cover

    final_dir = job_dir / "final"
    cover_path = final_dir / "cover.jpg"
    landscape_path = final_dir / "landscape-cover.png"
    cache_path = final_dir / ".cover-cache.json"
    key = {
        "version": COVER_RENDER_VERSION,
        "product_grade": product_grade,
        "source": _stat_signature(source_image),
        "portrait_template": _stat_signature(EXPORT_COVER_TEMPLATE_PATH),
        "landscape_template": _stat_signature(LANDSCAPE_COVER_TEMPLATE_PATH),
    }
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cached = {}
    if not isinstance(cached, dict) or cached.get("key") != key or not cover_path.is_file() or not landscape_path.is_file():
        render_export_cover(source_image, product_grade, cover_path, EXPORT_COVER_TEMPLATE_PATH)
        render_landscape_cover(product_grade, landscape_path, LANDSCAPE_COVER_TEMPLATE_PATH)
        _write_json_atomic(cache_path, {"key": key})
    return cover_path, landscape_path


def render_template_overlay_cached(job_dir: Path, product_grade: str) -> Path:
    from compose_branded_video import render_overlay

    output_path = job_dir / "template-overlay.png"
    cache_path = job_dir / ".template-overlay-cache.json"
    key = {"version": TEMPLATE_OVERLAY_RENDER_VERSION, "product_grade": product_grade, "template": _stat_signature(TEMPLATE_PATH)}
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cached = {}
    if not isinstance(cached, dict) or cached.get("key") != key or not output_path.is_file():
        render_overlay(TEMPLATE_PATH, product_grade, output_path)
        _write_json_atomic(cache_path, {"key": key})
    return output_path


def resolve_asset(job_dir: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is missing")
    asset_path = (job_dir / value).resolve()
    if not asset_path.is_file() or asset_path.stat().st_size == 0:
        raise ValueError(f"{label} is not a non-empty file: {asset_path}")
    return asset_path


def copy_draft_asset(draft_path: Path, source: Path, relative_path: Path) -> Path:
    """Copy a timeline asset into the draft so source cleanup cannot break it."""
    destination = (draft_path / "media" / relative_path).resolve()
    destination.relative_to(draft_path.resolve())
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if not destination.is_file() or destination.stat().st_size != source.stat().st_size:
        raise OSError(f"Failed to copy draft media: {source} -> {destination}")
    return destination


def verify_portable_draft_media(draft_path: Path) -> int:
    """Reject a saved draft that still references disposable source paths."""
    info_path = draft_path / "draft_info.json"
    value = json.loads(info_path.read_text(encoding="utf-8"))
    materials = value.get("materials") if isinstance(value, dict) else None
    if not isinstance(materials, dict):
        raise ValueError(f"Draft materials are missing: {info_path}")
    media_root = (draft_path / "media").resolve()
    checked = 0
    for kind in ("audios", "videos"):
        entries = materials.get(kind, [])
        if not isinstance(entries, list):
            raise ValueError(f"Draft {kind} materials must be a list")
        for entry in entries:
            path = Path(str(entry.get("path") or "")).resolve() if isinstance(entry, dict) else Path()
            try:
                path.relative_to(media_root)
            except ValueError as error:
                raise ValueError(f"Draft material is outside its media directory: {path}") from error
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"Draft material is missing or empty: {path}")
            checked += 1
    if checked == 0:
        raise ValueError("Draft contains no local audio or video materials")
    return checked


def visual_asset_hash(job_dir: Path, job: dict[str, Any]) -> str:
    """Fingerprint every source visual and the logo used by the draft."""
    visuals: list[dict[str, Any]] = []
    visuals.extend(shot for shot in job.get("shots", []) if isinstance(shot, dict))
    visuals.extend(
        visual for visual in job.get("closing_visuals", []) if isinstance(visual, dict)
    )
    digest = hashlib.sha256()
    cache_path = job_dir / MEDIA_CACHE_NAME
    cache_entries = _read_media_cache(cache_path)
    cache_changed = False
    if not LOGO_PATH.is_file() or LOGO_PATH.stat().st_size == 0:
        raise ValueError(f"Logo is not a non-empty file: {LOGO_PATH}")
    digest.update(LOGO_PATH.read_bytes())
    for position, visual in enumerate(visuals, start=1):
        image_values = shot_image_paths(visual) or [None]
        digest.update(str(visual.get("visual_type") or "ai_image").encode("utf-8"))
        for variant, image_value in enumerate(image_values, start=1):
            image_path = resolve_asset(job_dir, image_value, f"visual {position} image {variant} path")
            digest.update(str(image_value or "").encode("utf-8"))
            resolved, size, mtime_ns = _media_cache_key(image_path)
            cache_key = f"sha256|{resolved}"
            cached = cache_entries.get(cache_key)
            if (
                isinstance(cached, dict)
                and cached.get("size") == size
                and cached.get("mtime_ns") == mtime_ns
                and isinstance(cached.get("sha256"), str)
            ):
                image_digest = cached["sha256"]
            else:
                image_digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
                cache_entries[cache_key] = {"size": size, "mtime_ns": mtime_ns, "sha256": image_digest}
                cache_changed = True
            digest.update(image_digest.encode("ascii"))
    if cache_changed:
        _write_media_cache(cache_path, cache_entries)
    return digest.hexdigest()


def measure_timeline(job_dir: Path, job: dict[str, Any]) -> dict[str, Any]:
    """Measure the exact media durations used by the final draft timeline."""
    shot_durations_us: dict[int, int] = {}
    for position, shot in enumerate(job["shots"], start=1):
        index = int(shot["index"])
        audio_path = resolve_asset(job_dir, shot.get("audio_path"), f"shot {position} audio_path")
        shot_durations_us[index] = audio_duration_us(audio_path, cache_path=job_dir / MEDIA_CACHE_NAME)

    footer_audio_path = resolve_asset(job_dir, FOOTER_AUDIO_RELATIVE_PATH, "footer voice-over audio")
    if not FOOTER_PATH.is_file() or FOOTER_PATH.stat().st_size == 0:
        raise ValueError(f"Footer video is not a non-empty file: {FOOTER_PATH}")
    cache_path = job_dir / MEDIA_CACHE_NAME
    footer_audio_duration_us = audio_duration_us(footer_audio_path, cache_path=cache_path)
    footer_video_duration_us = media_duration_us(FOOTER_PATH, "video", cache_path=cache_path)
    closing_subtitles = footer_subtitles(str(job.get("product_grade", "")))
    closing_timings = footer_caption_timings(job_dir, job, closing_subtitles, footer_audio_duration_us)
    pre_footer_timings, footer_timings = split_footer_captions_for_video(
        closing_timings, footer_video_duration_us
    )
    if not footer_timings:
        raise ValueError("footer.mp4 is too short to display any complete closing caption")
    pre_footer_duration_us = sum(duration_us for _, duration_us in pre_footer_timings)
    footer_caption_duration_us = sum(duration_us for _, duration_us in footer_timings)
    if footer_caption_duration_us > footer_video_duration_us:
        raise ValueError("Closing captions exceed the fixed footer video duration")
    total_duration_us = (
        sum(shot_durations_us.values()) + pre_footer_duration_us + footer_video_duration_us
    )
    return {
        "shot_durations_us": shot_durations_us,
        "footer_audio_duration_us": footer_audio_duration_us,
        "footer_video_duration_us": footer_video_duration_us,
        "closing_subtitles": closing_subtitles,
        "closing_timings": closing_timings,
        "pre_footer_timings": pre_footer_timings,
        "footer_timings": footer_timings,
        "pre_footer_duration_us": pre_footer_duration_us,
        "footer_caption_duration_us": footer_caption_duration_us,
        "total_duration_us": total_duration_us,
    }


def verify_duration(job_dir: Path) -> dict[str, Any]:
    """Persist a real MP3-based timeline measurement before image generation."""
    job_path, job = load_job(job_dir)
    timing = measure_timeline(job_dir, job)
    for shot in job["shots"]:
        shot["duration_ms"] = round(timing["shot_durations_us"][int(shot["index"])] / 1000)
    audio = job["audio"]
    audio["footer_duration_ms"] = round(timing["footer_audio_duration_us"] / 1000)
    audio["footer_video_duration_ms"] = round(timing["footer_video_duration_us"] / 1000)
    total_seconds = timing["total_duration_us"] / 1_000_000
    job["actual_total_duration_seconds"] = round(total_seconds, 6)
    job["actual_timeline_audio_hash"] = timeline_audio_hash(job)
    job["actual_duration_status"] = "verified"
    # --verify-duration may run while images are still being generated.
    save_plan_job(job_path, job)
    return timing


def logo_image(job_dir: Path, shot: dict[str, Any], image_path: Path) -> Path:
    """Create and use a logo-marked copy while retaining the source image."""
    if str(shot.get("visual_type") or "ai_image") not in {"ai_image", "data_table"}:
        return image_path
    if not LOGO_PATH.is_file() or LOGO_PATH.stat().st_size == 0:
        raise ValueError(f"Logo is not a non-empty file: {LOGO_PATH}")
    index = int(shot.get("index", 0))
    if index <= 0:
        raise ValueError("shot index must be a positive integer for logo output")
    output_path = job_dir / "images" / "logo" / f"{index:03d}.png"
    source_hash = hashlib.sha256(
        LOGO_RENDER_VERSION + image_path.read_bytes() + LOGO_PATH.read_bytes()
    ).hexdigest()
    if (
        output_path.is_file()
        and output_path.stat().st_size > 0
        and shot.get("logo_source_hash") == source_hash
    ):
        return output_path
    apply_logo(image_path, output_path, LOGO_PATH)
    shot["original_image_path"] = shot.get("original_image_path") or shot.get("image_path")
    shot["logo_image_path"] = str(output_path.relative_to(job_dir)).replace("\\", "/")
    shot["logo_source_hash"] = source_hash
    shot["logo_status"] = "applied"
    return output_path


def shot_logo_images(job_dir: Path, shot: dict[str, Any], image_values: list[str]) -> list[Path]:
    """Give each image variant its own stable logo output path."""
    result: list[Path] = []
    source_hashes = shot.get("image_logo_source_hashes")
    for variant, value in enumerate(image_values, start=1):
        variant_shot = {**shot, "image_path": value}
        if len(image_values) > 1:
            variant_shot["index"] = int(shot["index"]) * 100 + variant
            if isinstance(source_hashes, list) and variant <= len(source_hashes):
                variant_shot["logo_source_hash"] = source_hashes[variant - 1]
        image_path = resolve_asset(job_dir, value, f"shot {shot['index']} image {variant} path")
        result.append(logo_image(job_dir, variant_shot, image_path))
    return result


def safe_project_name(job: dict[str, Any], job_dir: Path) -> str:
    title = str(job.get("source", {}).get("title") or "").strip()
    job_id = str(job.get("job_id") or job_dir.name).strip()
    raw_name = f"Article Video - {title or job_id} - {job_id}"
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", raw_name)[:120].strip(" .")


def installed_jianying_version() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None

    uninstall_paths = (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, path in uninstall_paths:
        try:
            with winreg.OpenKey(hive, path) as uninstall_key:
                for index in range(winreg.QueryInfoKey(uninstall_key)[0]):
                    subkey_name = winreg.EnumKey(uninstall_key, index)
                    try:
                        with winreg.OpenKey(uninstall_key, subkey_name) as subkey:
                            name, _ = winreg.QueryValueEx(subkey, "DisplayName")
                            if not any(label in str(name).lower() for label in ("jianying", "剪映")):
                                continue
                            version, _ = winreg.QueryValueEx(subkey, "DisplayVersion")
                            return str(version).strip() or None
                    except OSError:
                        continue
        except OSError:
            continue
    return None


def check_jianying_version() -> str | None:
    version = installed_jianying_version()
    if not version:
        return None
    match = re.match(r"(\d+)(?:\.(\d+))?", version)
    if not match:
        return version
    major, minor = int(match.group(1)), int(match.group(2) or 0)
    # Jianying Pro 11.2 has been verified locally with newly created drafts.
    if (major, minor) <= (11, 2):
        return version
    if os.getenv("JY_STRICT_VERSION_CHECK", "0") == "1":
        raise RuntimeError(
            f"Detected unverified Jianying Pro {version}. "
            "Unset JY_STRICT_VERSION_CHECK to attempt creating a new draft."
        )
    return version


JIANYING_PROCESS_NAMES = ("JianyingPro.exe", "CapCut.exe")


def jianying_is_running() -> bool:
    """Report whether the Jianying editor process is alive (Windows only)."""
    if sys.platform != "win32":
        return False
    try:
        listing = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, errors="replace", timeout=15
        ).stdout.casefold()
    except (OSError, subprocess.SubprocessError):
        return False
    return any(f'"{name.casefold()}"' in listing for name in JIANYING_PROCESS_NAMES)


def next_available_project_name(drafts_root: Path, base_name: str) -> str:
    if not (drafts_root / base_name).exists():
        return base_name
    for attempt in range(1, 1000):
        candidate = f"{base_name[:108].rstrip()} - retry-{attempt:03d}"
        if not (drafts_root / candidate).exists():
            return candidate
    raise RuntimeError("Unable to allocate a unique Jianying draft name after 999 attempts")


def base_project_name(value: str) -> str:
    """Keep retries numbered from the original project name."""
    return re.sub(r"(?: - retry-\d{3})+$", "", value).rstrip()


def load_jianying_library() -> tuple[Any, Path]:
    if not (JY_VENDOR_ROOT / "pyJianYingDraft" / "__init__.py").is_file():
        raise RuntimeError(f"Bundled Jianying draft library is missing: {JY_VENDOR_ROOT}")
    vendor_scripts = str(JY_VENDOR_ROOT)
    if vendor_scripts not in sys.path:
        sys.path.insert(0, vendor_scripts)
    import pyJianYingDraft as draft

    configured_root = os.getenv("JY_PROJECTS_ROOT", "").strip()
    default_root = Path(os.getenv("LOCALAPPDATA", "")) / "JianyingPro" / "User Data" / "Projects" / "com.lveditor.draft"
    drafts_root = Path(configured_root or default_root).expanduser().resolve()
    if not drafts_root.is_dir():
        raise RuntimeError(
            "Jianying drafts directory was not found: "
            f"{drafts_root}. Install Jianying Pro and set JY_PROJECTS_ROOT if it uses another path."
        )
    check_jianying_version()
    return draft, drafts_root


def snapshot_draft_folder(draft_path: Path, job_dir: Path) -> Path | None:
    """Copy an existing draft's timeline files before a rebuild replaces them."""
    if not draft_path.is_dir():
        return None
    backup_root = job_dir / "draft-backups"
    target = backup_root / f"{draft_path.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    if target.exists():
        return target
    backup_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        draft_path,
        target,
        ignore=shutil.ignore_patterns("media", ".backup", "__pycache__"),
    )
    for stale in sorted(
        backup_root.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True
    )[DRAFT_BACKUP_KEEP:]:
        shutil.rmtree(stale, ignore_errors=True)
    return target


def create_draft(job_dir: Path, overwrite: bool = False) -> dict[str, Any]:
    timing = verify_duration(job_dir)
    job_path, job = load_job(job_dir)
    current_visual_hash = visual_asset_hash(job_dir, job)
    shots = sorted(job["shots"], key=lambda shot: int(shot.get("index", 0)))
    if not shots:
        raise ValueError("video-job.json has no shots")

    existing_draft = job.get("draft") if isinstance(job.get("draft"), dict) else {}
    if existing_draft.get("adapter") == "jianying-editor" and existing_draft.get("status") == "saved":
        draft_path = Path(str(existing_draft.get("draft_path", "")))
        if (
            draft_path.is_dir()
            and not overwrite
            and existing_draft.get("layout_version") == DRAFT_LAYOUT_VERSION
            and existing_draft.get("visual_hash") == current_visual_hash
        ):
            export_cover = job.get("export_cover") if isinstance(job.get("export_cover"), dict) else {}
            cover_value = str(export_cover.get("path", ""))
            cover_path_obj = (job_dir / cover_value).resolve() if cover_value and not Path(cover_value).is_absolute() else Path(cover_value)
            first_shot = shots[0]
            # Always refresh the standalone cover from the original source so
            # a previously generated Logo-marked cover cannot be reused.
            first_image = resolve_asset(
                job_dir, (shot_image_paths(first_shot) or [None])[0], "shot 1 image_path"
            )
            cover_path_obj = job_dir / "final" / "cover.jpg"
            cover_path_obj, landscape_cover_path = render_covers_cached(
                job_dir, first_image, str(job.get("product_grade") or "").strip()
            )
            job["export_cover"] = {
                "path": str(cover_path_obj.relative_to(job_dir)).replace("\\", "/"),
                "template_path": str(EXPORT_COVER_TEMPLATE_PATH),
                "source_shot_index": int(first_shot.get("index", 1)),
                "source_image_path": str(first_image.relative_to(job_dir)).replace("\\", "/"),
                "logo_applied": False,
                "status": "completed",
            }
            job["landscape_cover"] = {
                "path": str(landscape_cover_path.relative_to(job_dir)).replace("\\", "/"),
                "template_path": str(LANDSCAPE_COVER_TEMPLATE_PATH),
                "label": "横版封面",
                "product_grade": str(job.get("product_grade") or "").strip(),
                "status": "completed",
            }
            save_job(job_path, job)
            return {
                "draft_path": str(draft_path),
                "cover_path": str(cover_path_obj.resolve()),
                "landscape_cover_path": str(landscape_cover_path.resolve()),
                "landscape_cover_label": "横版封面",
                "cover_label": "封面",
                "title_candidates": job["title_candidates"],
                "total_duration_seconds": round(timing["total_duration_us"] / 1_000_000, 3),
                "shots": len(shots),
                "status": "already_saved",
            }

    cover_source_image: Path | None = None
    prepared_shots: list[tuple[dict[str, Any], list[Path], Path, int, str]] = []
    for position, shot in enumerate(shots, start=1):
        # The default article workflow uses its approved storyboard image as a
        # still clip. A generated MP4 is optional and can be substituted later.
        image_values = shot_image_paths(shot) or [None]
        raw_video_value = shot.get("raw_video_path")
        if all(isinstance(value, str) and value.strip() for value in image_values):
            source_image_path = resolve_asset(job_dir, image_values[0], f"shot {position} image_path")
            if position == 1:
                cover_source_image = source_image_path
            visual_paths = shot_logo_images(job_dir, shot, image_values)
            visual_source = "image"
        elif isinstance(raw_video_value, str) and raw_video_value.strip():
            # Retain compatibility with previously completed video-based jobs.
            visual_paths = [resolve_asset(job_dir, raw_video_value, f"shot {position} raw_video_path")]
            visual_source = "video"
        else:
            raise ValueError(f"shot {position} requires image_path")
        audio_path = resolve_asset(job_dir, shot.get("audio_path"), f"shot {position} audio_path")
        duration_us = timing["shot_durations_us"][int(shot["index"])]
        subtitle = str(shot.get("subtitle") or shot.get("narration") or "").strip()
        if not subtitle:
            raise ValueError(f"shot {position} subtitle and narration are both missing")
        shot["duration_ms"] = round(duration_us / 1000)
        prepared_shots.append((shot, visual_paths, audio_path, duration_us, visual_source))

    # This is a delivery cover only. It is deliberately not imported as a
    # Jianying segment or track, so it never appears during playback.
    export_cover_path = job_dir / "final" / "cover.jpg"
    if cover_source_image is None:
        raise ValueError("shot 1 must provide image_path to render the cover")
    export_cover_path, landscape_cover_path = render_covers_cached(
        job_dir, cover_source_image, str(job.get("product_grade") or "").strip()
    )
    job["export_cover"] = {
        "path": str(export_cover_path.relative_to(job_dir)).replace("\\", "/"),
        "template_path": str(EXPORT_COVER_TEMPLATE_PATH),
        "source_shot_index": int(prepared_shots[0][0].get("index", 1)),
        "source_image_path": str(cover_source_image.relative_to(job_dir)).replace("\\", "/"),
        "logo_applied": False,
        "status": "completed",
    }
    job["landscape_cover"] = {
        "path": str(landscape_cover_path.relative_to(job_dir)).replace("\\", "/"),
        "template_path": str(LANDSCAPE_COVER_TEMPLATE_PATH),
        "label": "横版封面",
        "product_grade": str(job.get("product_grade") or "").strip(),
        "status": "completed",
    }
    save_job(job_path, job)

    footer_audio_path = resolve_asset(job_dir, FOOTER_AUDIO_RELATIVE_PATH, "footer voice-over audio")
    footer_caption_paths = (job.get("audio") or {}).get("footer_caption_paths", [])
    footer_audio_segment_paths = [
        resolve_asset(job_dir, path, f"footer caption {index + 1}")
        for index, path in enumerate(footer_caption_paths)
    ] if isinstance(footer_caption_paths, list) else []
    footer_duration_us = timing["footer_audio_duration_us"]
    footer_visual_path = FOOTER_PATH
    footer_video_duration_us = timing["footer_video_duration_us"]
    closing_subtitles = timing["closing_subtitles"]
    pre_footer_timings = timing["pre_footer_timings"]
    footer_timings = timing["footer_timings"]
    closing_caption_groups = split_closing_timings(pre_footer_timings) if pre_footer_timings else []
    prepared_closing_visuals = prepare_closing_visuals(
        job_dir,
        job,
        {path.resolve() for item in prepared_shots for path in item[1]},
        closing_caption_groups,
    ) if closing_caption_groups else []

    def add_footer_audio_segments(
        caption_group: list[tuple[str, int]], start_us: int
    ) -> None:
        """Place each standalone footer MP3 at its exact cumulative timeline start."""
        caption_paths = footer_audio_segment_paths
        caption_index = {text: index for index, text in enumerate(closing_subtitles)}
        cursor = start_us
        for text, duration_us in caption_group:
            index = caption_index.get(text)
            if index is None or index >= len(caption_paths):
                raise ValueError(f"Missing standalone audio for footer caption: {text}")
            project.add_segment(
                draft.AudioSegment(
                    draft.AudioMaterial(str(caption_paths[index])),
                    draft.Timerange(cursor, duration_us),
                    source_timerange=draft.Timerange(0, duration_us),
                ),
                "VoiceOver",
            )
            cursor += duration_us
    pre_footer_duration_us = timing["pre_footer_duration_us"]
    footer_caption_duration_us = timing["footer_caption_duration_us"]
    total_duration_us = timing["total_duration_us"]
    total_seconds = total_duration_us / 1_000_000

    draft, drafts_root = load_jianying_library()
    aspect_ratio = str(job.get("format", {}).get("aspect_ratio", "9:16"))
    width, height = (1080, 1920) if aspect_ratio == "9:16" else (1920, 1080)
    requested_project_name = base_project_name(
        str(existing_draft.get("project_name") or safe_project_name(job, job_dir))
    )
    is_resuming = existing_draft.get("adapter") == "jianying-editor" and existing_draft.get("status") == "creating"
    project_name = requested_project_name if (overwrite or is_resuming) else next_available_project_name(drafts_root, requested_project_name)
    retry_of_draft_path = None
    if project_name != requested_project_name:
        retry_of_draft_path = str(drafts_root / requested_project_name)
    draft_path = drafts_root / project_name
    if (overwrite or is_resuming) and draft_path.is_dir() and jianying_is_running():
        # An open editor rewrites draft_content.json on save and would silently
        # replace the rebuilt timeline, so refuse instead of racing it.
        raise RuntimeError(
            "剪映正在运行，覆盖已有草稿前请先完全退出剪映（含托盘进程），再重新执行本命令"
        )
    draft_backup_path = (
        snapshot_draft_folder(draft_path, job_dir) if (overwrite or is_resuming) else None
    )
    project = draft.DraftFolder(str(drafts_root)).create_draft(
        project_name, width, height, allow_replace=(overwrite or is_resuming)
    )
    project.add_track(draft.TrackType.video, "VideoTrack")
    # Render above the image/video track so the transparent center reveals the
    # shot while the template header, footer, and grade label remain visible.
    # Keep the source template on its own track for editability, underneath the
    # baked stills so Jianying's opaque JPG does not hide the composed result.
    project.add_track(draft.TrackType.video, "TemplateOverlay", relative_index=1)
    project.add_track(draft.TrackType.audio, "VoiceOver")
    project.add_track(draft.TrackType.text, "Subtitles")
    project.add_track(draft.TrackType.text, "HeaderGrade")

    cursor_us = 0
    previous_video_segment = None
    inter_shot_transition_index = 0
    intra_shot_transition_index = 0
    template_overlay_path = TEMPLATE_PATH
    if not TEMPLATE_PATH.is_file():
        raise ValueError(f"Template image not found: {TEMPLATE_PATH}")
    template_overlay_path = render_template_overlay_cached(job_dir, "")
    portable_shots: list[tuple[dict[str, Any], list[Path], Path, int, str]] = []
    for shot, visual_paths, audio_path, duration_us, visual_source in prepared_shots:
        shot_index = int(shot["index"])
        copied_visuals = [
            copy_draft_asset(
                draft_path,
                path,
                Path("visuals") / f"shot-{shot_index:03d}-{variant:02d}{path.suffix.lower()}",
            )
            for variant, path in enumerate(visual_paths, start=1)
        ]
        copied_audio = copy_draft_asset(
            draft_path,
            audio_path,
            Path("audio") / "shots" / f"{shot_index:03d}{audio_path.suffix.lower()}",
        )
        portable_shots.append((shot, copied_visuals, copied_audio, duration_us, visual_source))
    prepared_shots = portable_shots
    prepared_closing_visuals = [
        (
            visual,
            copy_draft_asset(
                draft_path,
                path,
                Path("visuals") / f"closing-{position:02d}{path.suffix.lower()}",
            ),
            duration_us,
        )
        for position, (visual, path, duration_us) in enumerate(prepared_closing_visuals, start=1)
    ]
    footer_audio_segment_paths = [
        copy_draft_asset(
            draft_path,
            path,
            Path("audio") / "footer" / f"{position:03d}{path.suffix.lower()}",
        )
        for position, path in enumerate(footer_audio_segment_paths, start=1)
    ]
    footer_audio_path = copy_draft_asset(
        draft_path, footer_audio_path, Path("audio") / f"footer{footer_audio_path.suffix.lower()}"
    )
    footer_visual_path = copy_draft_asset(
        draft_path, footer_visual_path, Path("video") / f"footer{footer_visual_path.suffix.lower()}"
    )
    template_overlay_path = copy_draft_asset(
        draft_path, template_overlay_path, Path("template") / template_overlay_path.name
    )
    def add_header_grade(start_us: int, duration_us: int) -> None:
        project.add_segment(
            draft.TextSegment(
                str(job.get("product_grade") or "").strip(),
                draft.Timerange(start_us, duration_us),
                font=draft.FontType.SourceHanSerifCN_Regular,
                style=draft.TextStyle(size=HEADER_GRADE_SIZE, bold=False, color=(0.05, 0.05, 0.05), align=1, auto_wrapping=False, max_line_width=1.0),
                clip_settings=draft.ClipSettings(transform_x=0.0, transform_y=HEADER_GRADE_TRANSFORM_Y),
            ),
            "HeaderGrade",
        )
    job["draft"] = {
        "adapter": "jianying-editor",
        "project_name": project_name,
        "draft_path": str(draft_path),
        "status": "creating",
        "duration_us": cursor_us,
        "timing_source": "measured_mp3_duration",
        "template_overlay_path": str(template_overlay_path),
        "media_root": str(draft_path / "media"),
        "layout_version": DRAFT_LAYOUT_VERSION,
        "visual_hash": current_visual_hash,
    }
    if retry_of_draft_path:
        job["draft"]["retry_of_draft_path"] = retry_of_draft_path
    for position, (shot, visual_paths, audio_path, duration_us, visual_source) in enumerate(prepared_shots, start=1):
        shot_start_us = cursor_us
        visual_count = len(visual_paths)
        base_visual_duration_us, remainder_us = divmod(duration_us, visual_count)
        subtitle_parts = split_timed_subtitle_lines(subtitle_parts_for_shot(shot, duration_us, audio_path))
        audio_segment = draft.AudioSegment(
            draft.AudioMaterial(str(audio_path)),
            draft.Timerange(shot_start_us, duration_us),
            source_timerange=draft.Timerange(0, duration_us),
        )
        project.add_segment(audio_segment, "VoiceOver")
        for visual_index, visual_path in enumerate(visual_paths):
            visual_duration_us = base_visual_duration_us + (remainder_us if visual_index == visual_count - 1 else 0)
            video_segment = make_template_visual_segment(draft, visual_path, cursor_us, visual_duration_us)
            if previous_video_segment is not None:
                if visual_index == 0:
                    transition_index = inter_shot_transition_index
                    inter_shot_transition_index += 1
                else:
                    transition_index = intra_shot_transition_index
                    intra_shot_transition_index += 1
                previous_video_segment.add_transition(
                    transition_for_boundary(draft, transition_index), duration="0.4s"
                )
            project.add_segment(video_segment, "VideoTrack")
            previous_video_segment = video_segment
            template_segment = draft.VideoSegment(
                draft.VideoMaterial(str(template_overlay_path)),
                draft.Timerange(cursor_us, visual_duration_us),
                source_timerange=draft.Timerange(0, visual_duration_us),
                volume=0,
            )
            project.add_segment(template_segment, "TemplateOverlay")
            add_header_grade(cursor_us, visual_duration_us)
            cursor_us += visual_duration_us
        for subtitle_text, subtitle_offset_us, subtitle_duration_us in subtitle_parts:
            display_text = subtitle_display_text(subtitle_text)
            if not display_text:
                continue
            subtitle_segment = draft.TextSegment(
                display_text,
                draft.Timerange(shot_start_us + subtitle_offset_us, subtitle_duration_us),
                font=draft.FontType.SourceHanSansCN_Regular,
                style=draft.TextStyle(
                    size=TEMPLATE_SUBTITLE_SIZE,
                    bold=False,
                    color=TEMPLATE_SUBTITLE_FILL,
                    align=1,
                    auto_wrapping=False,
                    max_line_width=1.00,
                ),
                border=draft.TextBorder(
                    color=TEMPLATE_SUBTITLE_OUTLINE,
                    alpha=1.0,
                    width=TEMPLATE_SUBTITLE_BORDER_WIDTH,
                ),
                clip_settings=draft.ClipSettings(transform_x=TEMPLATE_SUBTITLE_TRANSFORM_X, transform_y=TEMPLATE_SUBTITLE_TRANSFORM_Y),
            )
            project.add_segment(subtitle_segment, "Subtitles")
        shot["draft_subtitle_segments"] = [
            {"text": subtitle_display_text(text), "start_ms": round(offset_us / 1000), "duration_ms": round(segment_us / 1000)}
            for text, offset_us, segment_us in subtitle_parts if subtitle_display_text(text)
        ]
        shot["draft_import_status"] = "imported"
        shot["draft_visual_source"] = visual_source
        job["draft"]["duration_us"] = cursor_us

    if prepared_closing_visuals:
        # The first nine seconds of fixed closing copy use two new visuals.
        # The final eight seconds remain on the single fixed footer video.
        for (visual, visual_path, visual_duration_us), caption_group in zip(
            prepared_closing_visuals, closing_caption_groups
        ):
            closing_segment = make_template_visual_segment(
                draft, visual_path, cursor_us, visual_duration_us
            )
            if previous_video_segment is not None:
                previous_video_segment.add_transition(
                    transition_for_boundary(draft, inter_shot_transition_index), duration="0.4s"
                )
                inter_shot_transition_index += 1
            project.add_segment(closing_segment, "VideoTrack")
            previous_video_segment = closing_segment
            closing_template_segment = draft.VideoSegment(
                draft.VideoMaterial(str(template_overlay_path)),
                draft.Timerange(cursor_us, visual_duration_us),
                source_timerange=draft.Timerange(0, visual_duration_us),
                volume=0,
            )
            project.add_segment(closing_template_segment, "TemplateOverlay")
            add_header_grade(cursor_us, visual_duration_us)
            add_footer_audio_segments(caption_group, cursor_us)
            subtitle_cursor_us = cursor_us
            footer_parts = split_subtitle_lines(caption_group)
            validate_subtitle_timing(footer_parts, visual_duration_us, "pre-footer closing copy")
            for footer_text, footer_text_offset_us, footer_text_duration_us in footer_subtitle_display_parts(footer_parts):
                closing_subtitle_segment = draft.TextSegment(
                    footer_text,
                    draft.Timerange(subtitle_cursor_us + footer_text_offset_us, footer_text_duration_us),
                font=draft.FontType.SourceHanSansCN_Regular,
                style=draft.TextStyle(
                    size=TEMPLATE_SUBTITLE_SIZE,
                    bold=False,
                    color=TEMPLATE_SUBTITLE_FILL,
                    align=1,
                    auto_wrapping=False,
                    max_line_width=1.00,
                ),
                border=draft.TextBorder(
                    color=TEMPLATE_SUBTITLE_OUTLINE,
                    alpha=1.0,
                    width=TEMPLATE_SUBTITLE_BORDER_WIDTH,
                ),
                clip_settings=draft.ClipSettings(transform_x=TEMPLATE_SUBTITLE_TRANSFORM_X, transform_y=TEMPLATE_SUBTITLE_TRANSFORM_Y),
            )
                project.add_segment(closing_subtitle_segment, "Subtitles")
            visual["draft_import_status"] = "imported"
            cursor_us += visual_duration_us

    footer_segment = make_template_visual_segment(
        draft, footer_visual_path, cursor_us, footer_video_duration_us, volume=1.0
    )
    if previous_video_segment is not None:
        previous_video_segment.add_transition(
            transition_for_boundary(draft, inter_shot_transition_index), duration="0.4s"
        )
    project.add_segment(footer_segment, "VideoTrack")
    footer_template_segment = draft.VideoSegment(
        draft.VideoMaterial(str(template_overlay_path)),
        draft.Timerange(cursor_us, footer_video_duration_us),
        source_timerange=draft.Timerange(0, footer_video_duration_us),
        volume=0,
    )
    project.add_segment(footer_template_segment, "TemplateOverlay")
    add_header_grade(cursor_us, footer_video_duration_us)
    footer_subtitle_cursor_us = cursor_us
    add_footer_audio_segments(footer_timings, cursor_us)
    footer_parts = split_subtitle_lines(footer_timings)
    validate_subtitle_timing(footer_parts, footer_caption_duration_us, "footer closing copy")
    for footer_text, footer_text_offset_us, footer_text_duration_us in footer_subtitle_display_parts(footer_parts):
        footer_subtitle_segment = draft.TextSegment(
            footer_text,
            draft.Timerange(footer_subtitle_cursor_us + footer_text_offset_us, footer_text_duration_us),
            font=draft.FontType.SourceHanSansCN_Regular,
            style=draft.TextStyle(
                size=TEMPLATE_SUBTITLE_SIZE,
                bold=False,
                color=TEMPLATE_SUBTITLE_FILL,
                align=1,
                auto_wrapping=False,
                max_line_width=1.00,
            ),
            border=draft.TextBorder(
                color=TEMPLATE_SUBTITLE_OUTLINE,
                alpha=1.0,
                width=TEMPLATE_SUBTITLE_BORDER_WIDTH,
            ),
            clip_settings=draft.ClipSettings(transform_x=TEMPLATE_SUBTITLE_TRANSFORM_X, transform_y=TEMPLATE_SUBTITLE_TRANSFORM_Y),
        )
        project.add_segment(footer_subtitle_segment, "Subtitles")
    cursor_us += footer_video_duration_us
    if cursor_us != total_duration_us:
        raise ValueError("draft timeline does not match verified MP3 duration")
    job["draft"]["duration_us"] = cursor_us
    job["draft"]["footer_path"] = str(footer_visual_path)
    job["draft"]["footer_visual_path"] = str(footer_visual_path)
    job["draft"]["footer_duration_us"] = footer_video_duration_us
    job["draft"]["footer_voice_over_duration_us"] = footer_duration_us
    job["draft"]["footer_prefix_subtitles"] = [text for text, _ in pre_footer_timings]
    job["draft"]["closing_visual_paths"] = [str(path) for _, path, _ in prepared_closing_visuals]
    job["draft"]["footer_video_subtitles"] = [text for text, _ in footer_timings]
    job["draft"]["footer_audio_path"] = str(footer_audio_path)
    job["draft"]["footer_subtitles"] = list(closing_subtitles)
    project.save()
    portable_media_count = verify_portable_draft_media(draft_path)
    job["draft"] = {
        "adapter": "jianying-editor",
        "project_name": project_name,
        "draft_path": str(draft_path),
        "status": "saved",
        "duration_us": cursor_us,
        "timing_source": "measured_mp3_duration",
        "template_overlay_path": str(template_overlay_path),
        "media_root": str(draft_path / "media"),
        "portable_media_count": portable_media_count,
        "layout_version": DRAFT_LAYOUT_VERSION,
        "visual_hash": current_visual_hash,
        "footer_path": str(footer_visual_path),
        "footer_visual_path": str(footer_visual_path),
        "footer_duration_us": footer_video_duration_us,
        "footer_voice_over_duration_us": footer_duration_us,
        "footer_prefix_subtitles": [text for text, _ in pre_footer_timings],
        "closing_visual_paths": [str(path) for _, path, _ in prepared_closing_visuals],
        "footer_video_subtitles": [text for text, _ in footer_timings],
        "footer_subtitles": list(closing_subtitles),
        "footer_audio_path": str(footer_audio_path),
        "updated_at": int(time.time()),
    }
    if retry_of_draft_path:
        job["draft"]["retry_of_draft_path"] = retry_of_draft_path
    save_job(job_path, job)
    return {
        "draft_path": str(draft_path),
        "draft_backup_path": str(draft_backup_path) if draft_backup_path else None,
        "cover_path": str(export_cover_path),
        "cover_label": "封面",
        "landscape_cover_path": str(landscape_cover_path),
        "landscape_cover_label": "横版封面",
        "project_name": project_name,
        "title_candidates": job["title_candidates"],
        "total_duration_seconds": round(total_seconds, 3),
        "shots": len(shots),
        "footer_duration_seconds": round(footer_video_duration_us / 1_000_000, 3),
        "timing_source": "measured_mp3_duration",
        "template_overlay_path": str(template_overlay_path),
        "layout_version": DRAFT_LAYOUT_VERSION,
        "status": "saved",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, type=Path, help="Job directory containing video-job.json")
    parser.add_argument("--overwrite", action="store_true", help="Replace a draft created by this job")
    parser.add_argument(
        "--verify-duration",
        action="store_true",
        help="Measure MP3 audio and the fixed footer before image generation",
    )
    args = parser.parse_args()
    try:
        if args.verify_duration:
            if args.overwrite:
                parser.error("--overwrite cannot be used with --verify-duration")
            timing = verify_duration(args.job.resolve())
            print(
                json.dumps(
                    {
                        "status": "verified",
                        "total_duration_seconds": round(timing["total_duration_us"] / 1_000_000, 6),
                        "footer_duration_seconds": round(timing["footer_audio_duration_us"] / 1_000_000, 6),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(json.dumps(create_draft(args.job.resolve(), overwrite=args.overwrite), ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
