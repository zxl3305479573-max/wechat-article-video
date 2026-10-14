#!/usr/bin/env python3
"""Generate a job's narration MP3 files from verified TTS-only text."""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import difflib
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from audio_manager import AudioManager
from job_store import save_plan_job
from prepare_tts_text import prepare_job, prepare_text
from subtitle_timing import split_timeline_segments
from video_constants import FOOTER_CAPTIONS
import imageio_ffmpeg


DEFAULT_TTS_SPEED = 1.25
STATE_DIRECTORY = "audio/.state"
# Bump when the ASR-to-subtitle alignment changes so cached fallback states are
# re-evaluated instead of being reused.
SUBTITLE_SYNC_VERSION = "asr-word-alignment-v3"


def audio_state_path(job_dir: Path, name: str) -> Path:
    return job_dir / STATE_DIRECTORY / f"{name}.json"


def save_audio_state(job_dir: Path, name: str, state: dict[str, Any]) -> None:
    path = audio_state_path(job_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_path.replace(path)


def read_audio_state(job_dir: Path, name: str) -> dict[str, Any]:
    path = audio_state_path(job_dir, name)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_job(job_path: Path) -> dict[str, Any]:
    return json.loads(job_path.read_text(encoding="utf-8-sig"))


def save_job(job_path: Path, job: dict[str, Any]) -> None:
    """Audio-stage write; keeps fields the image stage wrote meanwhile."""
    save_plan_job(job_path, job)


def audio_hash(text: str, voice_id: str, speed: float) -> str:
    payload = {
        "text": text,
        "voice_id": voice_id,
        "speed": speed,
        "long_number_reading": "recognized_grade_codes_and_comparisons_v2",
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def subtitle_sync_hash(audio_fingerprint: str, subtitle_text: str) -> str:
    payload = f"{SUBTITLE_SYNC_VERSION}\n{audio_fingerprint}\n{subtitle_text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def concat_audio(paths: list[Path], output: Path) -> None:
    """Concatenate sentence MP3s locally into the legacy footer.mp3 path."""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    command = [ffmpeg, "-y"]
    for path in paths:
        command.extend(["-i", str(path)])
    inputs = "".join(f"[{index}:a]" for index in range(len(paths)))
    command.extend([
        "-filter_complex", f"{inputs}concat=n={len(paths)}:v=0:a=1[out]",
        "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "2", str(output),
    ])
    subprocess.run(command, check=True, capture_output=True)


def subtitle_sentences(shot: dict[str, Any]) -> list[str]:
    """Use the plan's single shared subtitle timing segmentation."""
    segments = shot.get("subtitle_segments")
    if isinstance(segments, list) and all(isinstance(part, str) and part.strip() for part in segments):
        return [part.strip() for part in segments]
    return split_timeline_segments(str(shot.get("subtitle") or shot["narration"]))


def parse_timestamp(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        return round(float(value))
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d+):(\d+):(\d+)(?:\.(\d{1,3}))?", value)
    if not match:
        return None
    hours, minutes, seconds, milliseconds = match.groups()
    return (
        (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1000
        + int((milliseconds or "0").ljust(3, "0"))
    )


def _normalize_asr_text(value: str) -> str:
    """Normalize punctuation and common spoken digits for ASR/text matching."""
    value = str(value or "").lower()
    value = value.translate(str.maketrans("零一二三四五六七八九", "0123456789"))
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value)


def measured_audio_duration_ms(path: Path) -> int | None:
    """Read an MP3 duration when available for ASR boundary coverage checks."""
    try:
        from pymediainfo import MediaInfo

        tracks = MediaInfo.parse(str(path)).audio_tracks
        duration = tracks[0].duration if tracks else None
        return round(float(duration)) if duration else None
    except (ImportError, OSError, TypeError, ValueError):
        return None


def use_estimated_subtitle_timing(shot: dict[str, Any], reason: str) -> None:
    """Record a safe local fallback when ASR cannot align authored sentences."""
    shot.pop("asr_subtitle_segments", None)
    shot["subtitle_timing_source"] = "punctuation_duration_estimate"
    shot["subtitle_sync_status"] = "fallback"
    shot["subtitle_sync_warning"] = reason


ASR_TEXT_MATCH_RATIO = 0.72
# Leading/trailing silence in a TTS clip is normal and is simply absorbed by
# the first and last subtitle. Only a gap this large means ASR lost speech.
ASR_EDGE_SILENCE_LIMIT_MS = 1500


def segment_words(segment: dict[str, Any]) -> list[tuple[str, int, int]] | None:
    """Return a sentence's word timestamps, or None when any word is unusable."""
    words = segment.get("words")
    if not isinstance(words, list) or not words:
        return None
    result: list[tuple[str, int, int]] = []
    for word in words:
        if not isinstance(word, dict):
            return None
        text = str(word.get("text") or "")
        if not _normalize_asr_text(text):
            continue
        start = parse_timestamp(word.get("begin_ms", word.get("begin_time")))
        end = parse_timestamp(word.get("end_ms", word.get("end_time")))
        if start is None or end is None or start < 0 or end <= start:
            return None
        result.append((text, start, end))
    return result or None


def asr_character_timeline(segments: list[dict[str, Any]]) -> tuple[str, list[tuple[float, float]]]:
    """Spread recognized characters over word times, or sentence times without words."""
    recognized = ""
    timeline: list[tuple[float, float]] = []
    for segment in segments:
        units = segment_words(segment)
        if units is None:
            start = parse_timestamp(segment.get("begin_ms", segment.get("begin_time")))
            end = parse_timestamp(segment.get("end_ms", segment.get("end_time")))
            units = [(str(segment.get("text") or ""), start, end)]
        for text, start, end in units:
            text = _normalize_asr_text(text)
            if not text:
                continue
            span = end - start
            for offset in range(len(text)):
                timeline.append(
                    (start + span * offset / len(text), start + span * (offset + 1) / len(text))
                )
            recognized += text
    return recognized, timeline


def has_word_timestamps(segments: list[dict[str, Any]]) -> bool:
    return any(segment_words(segment) for segment in segments)


def closest_authored_sentences(candidates: list[list[str]], recognized: str) -> tuple[list[str], float]:
    """Pick the authored spelling (visible or TTS) that ASR heard most closely."""
    best: tuple[list[str], float] = (candidates[0], 0.0)
    for sentences in candidates:
        authored = _normalize_asr_text("".join(sentences))
        if not authored or not recognized:
            continue
        ratio = difflib.SequenceMatcher(None, authored, recognized, autojunk=False).ratio()
        if ratio > best[1]:
            best = (sentences, ratio)
    return best


def align_asr_sentence_boundaries(
    visible_sentences: list[str],
    segments: list[dict[str, Any]],
    audio_duration_ms: int | None = None,
) -> list[tuple[int, int]] | None:
    """Read authored sentence times from the ASR characters they match.

    ASR freely merges or splits sentences, so match the authored characters
    against the recognized ones and take times from word timestamps when ASR
    supplies them (otherwise interpolate inside the matching ASR sentence). A
    clause ASR spelled differently, such as a spoken number, takes the time
    between its matched neighbours instead of rejecting the whole clip.
    """
    authored_parts = [_normalize_asr_text(sentence) for sentence in visible_sentences]
    authored = "".join(authored_parts)
    recognized, timeline = asr_character_timeline(segments)
    if not authored or not recognized:
        return None
    if difflib.SequenceMatcher(None, authored, recognized, autojunk=False).ratio() < ASR_TEXT_MATCH_RATIO:
        return None
    matched: list[int | None] = [None] * len(authored)
    matcher = difflib.SequenceMatcher(None, authored, recognized, autojunk=False)
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            matched[block.a + offset] = block.b + offset
    spans: list[tuple[float, float] | None] = []
    cursor = 0
    previous_end = 0.0
    for part in authored_parts:
        window = matched[cursor : cursor + len(part)]
        cursor += len(part)
        indexes = [index for index in window if index is not None]
        if not indexes:
            spans.append(None)
            continue
        # Leading characters ASR spelled differently were spoken straight
        # after the previous clause, so the clause starts where that one ended.
        start = timeline[indexes[0]][0] if window[0] is not None else previous_end
        start = max(start, previous_end)
        end = timeline[indexes[-1]][1]
        if end <= start:
            return None
        spans.append((start, end))
        previous_end = end
    if all(span is None for span in spans):
        return None
    final_end = float(audio_duration_ms) if audio_duration_ms else timeline[-1][1]
    index = 0
    while index < len(spans):
        if spans[index] is not None:
            index += 1
            continue
        run_end = index
        while run_end < len(spans) and spans[run_end] is None:
            run_end += 1
        gap_start = spans[index - 1][1] if index > 0 else 0.0
        gap_end = spans[run_end][0] if run_end < len(spans) else max(final_end, gap_start)
        weights = [max(1, len(authored_parts[position])) for position in range(index, run_end)]
        if gap_end - gap_start < 100 * len(weights):
            return None
        cursor_ms = gap_start
        for offset, weight in enumerate(weights):
            length = (gap_end - gap_start) * weight / sum(weights)
            spans[index + offset] = (cursor_ms, cursor_ms + length)
            cursor_ms += length
        index = run_end
    aligned: list[tuple[int, int]] = []
    previous = 0
    for start, end in spans:
        start_ms, end_ms = max(round(start), previous), round(end)
        if end_ms <= start_ms:
            return None
        aligned.append((start_ms, end_ms))
        previous = end_ms
    return aligned


def sync_subtitles(shot: dict[str, Any], audio_path: Path) -> None:
    """Use ASR sentence boundaries when available; preserve original visible text."""
    visible_sentences = subtitle_sentences(shot)
    segments = AudioManager().extract_audio_text(str(audio_path))
    if not isinstance(segments, list) or not segments:
        use_estimated_subtitle_timing(shot, "ASR returned no usable sentences")
        return
    if not all(isinstance(item, dict) for item in segments):
        use_estimated_subtitle_timing(shot, "ASR returned a malformed sentence list")
        return
    boundaries = [parse_timestamp(segment.get("begin_ms", segment.get("begin_time"))) for segment in segments]
    ends = [parse_timestamp(segment.get("end_ms", segment.get("end_time"))) for segment in segments]
    if any(value is None for value in boundaries + ends):
        use_estimated_subtitle_timing(
            shot, "ASR did not return usable millisecond timestamps"
        )
        return
    if any(start < 0 or end <= start for start, end in zip(boundaries, ends)):
        use_estimated_subtitle_timing(
            shot, "ASR returned a non-positive sentence duration"
        )
        return
    if any(start < previous_end for previous_end, start in zip(ends, boundaries[1:])):
        use_estimated_subtitle_timing(shot, "ASR sentence timestamps overlap or are not monotonic")
        return
    # The MP3 speaks tts_text (grade digits, "百分之", "到"), so match against
    # whichever spelling ASR heard more closely; the output keeps visible text.
    recognized_text, _timeline = asr_character_timeline(segments)
    candidates = [visible_sentences]
    spoken_sentences = split_timeline_segments(str(shot.get("tts_text") or ""))
    if len(spoken_sentences) == len(visible_sentences):
        candidates.append(spoken_sentences)
    alignment_sentences, ratio = closest_authored_sentences(candidates, recognized_text)
    audio_duration_ms = measured_audio_duration_ms(audio_path)
    if len(segments) == len(visible_sentences) and not has_word_timestamps(segments):
        if ratio < ASR_TEXT_MATCH_RATIO:
            use_estimated_subtitle_timing(
                shot, "ASR text does not match authored subtitle order; waveform alignment required"
            )
            return
        aligned = list(zip(boundaries, ends))
    else:
        # Word timestamps place every clause on its measured speech, so they
        # win even when the ASR sentence count happens to match.
        aligned = align_asr_sentence_boundaries(alignment_sentences, segments, audio_duration_ms)
        if aligned is None:
            use_estimated_subtitle_timing(
                shot,
                "ASR sentences could not be aligned to the authored subtitle order",
            )
            return
    boundaries = [start for start, _end in aligned]
    ends = [end for _start, end in aligned]
    if audio_duration_ms is not None:
        if (
            boundaries[0] > ASR_EDGE_SILENCE_LIMIT_MS
            or ends[-1] < audio_duration_ms - ASR_EDGE_SILENCE_LIMIT_MS
        ):
            use_estimated_subtitle_timing(
                shot, "ASR sentence timestamps do not cover the measured audio boundaries"
            )
            return
        # TTS clips carry leading and trailing silence; let the first and last
        # captions cover it instead of discarding otherwise valid timestamps.
        boundaries[0] = 0
        ends[-1] = max(audio_duration_ms, boundaries[-1] + 1)
    # Preserve real ASR offsets. Draft-only display segments must never replace
    # this source timing, otherwise a later import silently falls back to an
    # estimated character-length timeline.
    shot["asr_subtitle_segments"] = [
        {"text": text, "start_ms": start, "end_ms": end}
        for text, start, end in zip(visible_sentences, boundaries, ends)
    ]
    shot["subtitle_timing_source"] = "asr_sentence_timestamps"
    shot["subtitle_sync_status"] = "completed"
    shot.pop("subtitle_sync_warning", None)


def sync_subtitles_worker(
    shot: dict[str, Any], audio_path: Path
) -> tuple[int, dict[str, Any]]:
    """Run ASR on an isolated shot copy; JSON state is merged by the caller."""
    isolated = copy.deepcopy(shot)
    try:
        sync_subtitles(isolated, audio_path)
    except Exception as error:
        use_estimated_subtitle_timing(isolated, f"ASR unavailable: {error}")
    return int(shot["index"]), {
        key: isolated[key]
        for key in (
            "asr_subtitle_segments",
            "subtitle_timing_source",
            "subtitle_sync_status",
            "subtitle_sync_warning",
        )
        if key in isolated
    }


def invalidate_draft(job: dict[str, Any]) -> None:
    draft = job.get("draft")
    if isinstance(draft, dict) and draft.get("status") == "saved":
        draft["status"] = "stale_audio"
        draft["stale_reason"] = "TTS text, voice, speed, or numeric reading changed"


def generate(job_dir: Path, sync: bool) -> dict[str, Any]:
    job_path = job_dir / "video-job.json"
    prepare_job(job_path)
    if not sync:
        job = load_job(job_path)
        for shot in job.get("shots", []):
            if isinstance(shot, dict):
                shot.pop("asr_subtitle_segments", None)
                shot["subtitle_timing_source"] = "punctuation_duration_estimate"
                shot["subtitle_sync_status"] = "fallback"
                shot["subtitle_sync_warning"] = "ASR synchronization was explicitly disabled"
        save_job(job_path, job)
    from validate_video_job import invalidate_duration_verification, validate

    validate(job_path)
    job = load_job(job_path)
    audio = job["audio"]
    voice_id = str(audio.get("voice_id") or "female-chengshu")
    speed = float(audio.get("speed", DEFAULT_TTS_SPEED))
    product_grade = str(job["product_grade"])
    jobs: list[tuple[dict[str, Any], str, Path, str]] = []
    owners: dict[str, int] = {}
    for candidate in job["shots"]:
        owners[audio_hash(str(candidate["tts_text"]), voice_id, speed)] = int(candidate["index"])
    for shot in sorted(job["shots"], key=lambda item: int(item["index"])):
        text = str(shot["tts_text"])
        path = (job_dir / str(shot["audio_path"])).resolve()
        fingerprint = audio_hash(text, voice_id, speed)
        state = read_audio_state(job_dir, f"shot-{int(shot['index']):03d}")
        if (
            state.get("status") == "completed"
            and state.get("audio_hash") == fingerprint
            and path.is_file()
            and path.stat().st_size > 0
        ):
            shot["audio_hash"] = fingerprint
            shot["audio_status"] = "completed"
        asr_state = read_audio_state(job_dir, f"asr-{int(shot['index']):03d}")
        expected_sync_hash = subtitle_sync_hash(
            str(shot.get("audio_hash") or fingerprint),
            str(shot.get("subtitle") or shot.get("narration") or ""),
        )
        if (
            asr_state.get("subtitle_sync_hash") == expected_sync_hash
            and asr_state.get("status") in {"completed", "fallback"}
            and isinstance(asr_state.get("fields"), dict)
        ):
            shot.update(asr_state["fields"])
            shot["subtitle_sync_hash"] = expected_sync_hash
        if not (path.is_file() and path.stat().st_size > 0 and shot.get("audio_hash") == fingerprint):
            recorded = state.get("audio_hash")
            if isinstance(recorded, str) and recorded != fingerprint and recorded in owners:
                raise ValueError(
                    f"audio state for shot {int(shot['index'])} belongs to shot {owners[recorded]}. "
                    "The shots were reordered without re-keying their audio; rebuild the job instead of "
                    "regenerating paid narration."
                )
            shot["audio_status"] = "generating"
            shot["draft_import_status"] = "planned"
            shot.pop("asr_subtitle_segments", None)
            shot.pop("draft_subtitle_segments", None)
            shot.pop("subtitle_timing_source", None)
            jobs.append((shot, text, path, fingerprint))
    footer_captions = [item.format(product_grade=product_grade) for item in FOOTER_CAPTIONS]
    footer_text = "\uff0c".join(footer_captions)
    footer_tts_text = prepare_text(footer_text, product_grade)
    footer_path = job_dir / "audio" / "footer.mp3"
    footer_hash = audio_hash(footer_tts_text, voice_id, speed)
    footer_segment_dir = job_dir / "audio" / "footer-segments"
    footer_segment_paths = [footer_segment_dir / f"{index:03d}.mp3" for index in range(1, len(footer_captions) + 1)]
    footer_segment_hashes = [audio_hash(prepare_text(text, product_grade), voice_id, speed) for text in footer_captions]
    recorded_footer_hashes = audio.get("footer_caption_hashes")
    if not isinstance(recorded_footer_hashes, list) or len(recorded_footer_hashes) != len(footer_captions):
        recorded_footer_hashes = [None] * len(footer_captions)
    footer_segments_stale = any(
        not path.is_file() or path.stat().st_size == 0
        or (
            recorded_footer_hashes[index] != fingerprint
            and read_audio_state(job_dir, f"footer-{index + 1:03d}").get("audio_hash") != fingerprint
        )
        for index, (path, fingerprint) in enumerate(zip(footer_segment_paths, footer_segment_hashes))
    )
    footer_stale = footer_segments_stale or not (
        footer_path.is_file() and footer_path.stat().st_size > 0
        and audio.get("footer_audio_hash") == footer_hash
    )
    if jobs or footer_stale:
        invalidate_draft(job)
        invalidate_duration_verification(job)

    early_asr_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2) if sync else None
    early_asr_futures: dict[concurrent.futures.Future[Any], tuple[int, str]] = {}

    def queue_asr(shot: dict[str, Any]) -> None:
        if early_asr_executor is None:
            return
        path = (job_dir / str(shot["audio_path"])).resolve()
        if not path.is_file() or path.stat().st_size == 0:
            return
        sync_hash = subtitle_sync_hash(
            str(shot.get("audio_hash") or ""),
            str(shot.get("subtitle") or shot.get("narration") or ""),
        )
        if (
            shot.get("subtitle_sync_hash") == sync_hash
            and shot.get("subtitle_sync_status") in {"completed", "fallback"}
        ):
            return
        early_asr_futures[
            early_asr_executor.submit(sync_subtitles_worker, shot, path)
        ] = (int(shot["index"]), sync_hash)

    def create(item: tuple[dict[str, Any], str, Path, str]) -> tuple[dict[str, Any], Path, str]:
        shot, text, path, fingerprint = item
        AudioManager().generate_audio(text, str(path), voice_id, speed)
        return shot, path, fingerprint

    def create_footer_segment(text: str, path: Path) -> Path:
        AudioManager().generate_audio(prepare_text(text, product_grade), str(path), voice_id, speed)
        return path

    created = 0
    footer_errors: list[Exception] = []
    footer_segment_dir.mkdir(parents=True, exist_ok=True)
    # TTS has one shared two-request budget. ASR uses a separate provider path,
    # so it can begin as soon as each completed narration MP3 is available.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        pending: dict[concurrent.futures.Future[Any], tuple[str, Any]] = {}
        footer_tasks = list(zip(footer_captions, footer_segment_paths)) if footer_segments_stale else []
        if footer_segments_stale:
            audio["footer_audio_status"] = "generating"
        # Alternate article and footer submissions. This lets both categories
        # start immediately while every TTS request still shares the two-slot cap.
        for position in range(max(len(jobs), len(footer_tasks))):
            if position < len(jobs):
                task = jobs[position]
                pending[executor.submit(create, task)] = ("shot", task[0])
            if position < len(footer_tasks):
                text, path = footer_tasks[position]
                pending[executor.submit(create_footer_segment, text, path)] = ("footer", path)
        for future in concurrent.futures.as_completed(pending):
            kind, item = pending[future]
            if kind == "footer":
                try:
                    future.result()
                    footer_index = footer_segment_paths.index(item) + 1
                    save_audio_state(job_dir, f"footer-{footer_index:03d}", {
                        "status": "completed",
                        "audio_hash": footer_segment_hashes[footer_index - 1],
                        "path": str(item.relative_to(job_dir)).replace("\\", "/"),
                    })
                except Exception as error:
                    footer_errors.append(error)
                    footer_index = footer_segment_paths.index(item) + 1
                    save_audio_state(job_dir, f"footer-{footer_index:03d}", {
                        "status": "failed", "error": str(error),
                    })
                continue
            shot = item
            try:
                _, path, fingerprint = future.result()
            except Exception as error:
                shot["audio_status"] = "failed"
                shot["audio_error"] = str(error)
                save_audio_state(job_dir, f"shot-{int(shot['index']):03d}", {
                    "status": "failed", "error": str(error),
                })
                continue
            shot["audio_hash"] = fingerprint
            shot["audio_status"] = "completed"
            shot.pop("audio_error", None)
            created += 1
            save_audio_state(job_dir, f"shot-{int(shot['index']):03d}", {
                "status": "completed",
                "audio_hash": fingerprint,
                "path": str(path.relative_to(job_dir)).replace("\\", "/"),
            })
            queue_asr(shot)

    # Cached MP3s also enter the same ASR pipeline; newly completed MP3s were
    # queued immediately above instead of waiting for the whole TTS batch.
    for shot in sorted(job["shots"], key=lambda item: int(item["index"])):
        queue_asr(shot)

    def merge_early_asr() -> None:
        if early_asr_executor is None:
            return
        shot_by_index = {int(shot["index"]): shot for shot in job["shots"]}
        for future in concurrent.futures.as_completed(early_asr_futures):
            index, sync_hash = early_asr_futures[future]
            try:
                result_index, fields = future.result()
            except Exception as error:
                result_index = index
                fields = {
                    "subtitle_timing_source": "punctuation_duration_estimate",
                    "subtitle_sync_status": "fallback",
                    "subtitle_sync_warning": f"ASR worker failed: {error}",
                }
            shot = shot_by_index[result_index]
            shot.pop("asr_subtitle_segments", None)
            shot.pop("subtitle_sync_warning", None)
            shot.update(fields)
            shot["subtitle_sync_hash"] = sync_hash
            save_audio_state(job_dir, f"asr-{result_index:03d}", {
                "status": shot.get("subtitle_sync_status", "fallback"),
                "subtitle_sync_hash": sync_hash,
                "fields": fields,
            })
            invalidate_draft(job)
        early_asr_executor.shutdown(wait=False)

    merge_early_asr()

    if footer_errors:
        audio["footer_audio_status"] = "failed"
        audio["footer_audio_error"] = str(footer_errors[0])
        # The individual segment checkpoint is already on disk; persist this
        # phase-level failure once so the next run has a clear resume point.
        save_job(job_path, job)
        raise footer_errors[0]
    if footer_segments_stale:
        concat_audio(footer_segment_paths, footer_path)
        audio["footer_caption_paths"] = [str(path.relative_to(job_dir)) for path in footer_segment_paths]
        audio["footer_caption_hashes"] = footer_segment_hashes
        audio["footer_caption_durations_ms"] = [measured_audio_duration_ms(path) for path in footer_segment_paths]
        audio["footer_audio_hash"] = footer_hash
        audio["footer_audio_status"] = "completed"
        audio.pop("footer_audio_error", None)
    elif footer_stale:
        concat_audio(footer_segment_paths, footer_path)
        audio["footer_audio_hash"] = footer_hash
        audio["footer_audio_status"] = "completed"
    audio["footer_tts_text"] = footer_tts_text
    failed = [shot["index"] for shot in job["shots"] if shot.get("audio_status") == "failed"]
    sync_failed = [
        shot["index"]
        for shot in job["shots"]
        if sync and shot.get("subtitle_sync_status") not in {"completed", "fallback"}
    ]
    save_job(job_path, job)
    if failed:
        raise RuntimeError(f"TTS failed for shot(s): {', '.join(map(str, failed))}")
    if sync_failed:
        raise RuntimeError(f"ASR subtitle synchronization failed for shot(s): {', '.join(map(str, sync_failed))}")
    synced = sum(
        shot.get("subtitle_sync_status") == "completed" for shot in job["shots"]
    )
    fallbacks = sum(
        shot.get("subtitle_sync_status") == "fallback" for shot in job["shots"]
    )
    return {
        "generated_shots": created,
        "footer_regenerated": footer_stale,
        "voice_id": voice_id,
        "speed": speed,
        "subtitle_sync_completed": synced,
        "subtitle_sync_fallback": fallbacks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--no-sync-subtitles", action="store_true", help="Skip ASR timing synchronization")
    args = parser.parse_args()
    try:
        print(json.dumps(generate(args.job.resolve(), not args.no_sync_subtitles), ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
