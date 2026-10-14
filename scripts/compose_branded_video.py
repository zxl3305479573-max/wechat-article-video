#!/usr/bin/env python3
"""Render an article video inside the fixed RICH course template."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont
from pymediainfo import MediaInfo
from subtitle_timing import visible_subtitle_segments


CANVAS_SIZE = (1080, 1920)
VIDEO_WINDOW = (0, 488, 1080, 1485)
CLEAR_VIDEO_FRAME = (0, 588, 1080, 1359)
SUBTITLE_BASELINE_Y = 1415
GRADE_REGION = (105, 1535, 975, 1755)
DEFAULT_TEMPLATE = Path(__file__).resolve().parents[1] / "assets" / "header.jpg"
SOURCE_HAN_REGULAR = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "SourceHanSansSC-Regular.otf"


def require_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.stat().st_size == 0:
        raise ValueError(f"{label} is not a non-empty file: {resolved}")
    return resolved


def load_font(size: int) -> ImageFont.FreeTypeFont:
    if not SOURCE_HAN_REGULAR.is_file():
        raise FileNotFoundError(f"Bundled Source Han Sans font is missing: {SOURCE_HAN_REGULAR}")
    return ImageFont.truetype(str(SOURCE_HAN_REGULAR), size=size)


def fitted_font(text: str, max_width: int, max_height: int) -> ImageFont.FreeTypeFont:
    for size in range(max_height, 20, -2):
        font = load_font(size)
        left, top, right, bottom = font.getbbox(text)
        if right - left <= max_width and bottom - top <= max_height:
            return font
    return load_font(20)


def render_overlay(template_path: Path, product_grade: str, overlay_path: Path) -> None:
    template = Image.open(template_path).convert("RGBA").resize(CANVAS_SIZE, Image.Resampling.LANCZOS)
    x1, y1, x2, y2 = GRADE_REGION
    draw = ImageDraw.Draw(template)
    # The current template already provides the white grade area. Only draw the
    # replacement label so the template background and its shadows remain intact.
    font = load_font(130)
    left, top, right, bottom = draw.textbbox((0, 0), product_grade, font=font)
    text_x = x1 + ((x2 - x1) - (right - left)) // 2 - left
    text_y = y1 + ((y2 - y1) - (bottom - top)) // 2 - top
    if product_grade.strip():
        draw.text((text_x, text_y), product_grade, font=font, fill=(12, 12, 12, 255))

    alpha = template.getchannel("A")
    window_x1, window_y1, window_x2, window_y2 = VIDEO_WINDOW
    ImageDraw.Draw(alpha).rectangle((window_x1, window_y1, window_x2 - 1, window_y2 - 1), fill=0)
    template.putalpha(alpha)
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    template.save(overlay_path)


def write_job_subtitles(job_dir: Path, srt_path: Path) -> None:
    job_dir = job_dir.expanduser().resolve()
    job_path = job_dir / "video-job.json"
    try:
        job = json.loads(job_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ValueError(f"video-job.json not found: {job_path}") from exc
    shots = job.get("shots")
    if not isinstance(shots, list) or not shots:
        raise ValueError("video-job.json must contain at least one shot")

    def timestamp(milliseconds: int) -> str:
        hours, remaining = divmod(milliseconds, 3_600_000)
        minutes, remaining = divmod(remaining, 60_000)
        seconds, millis = divmod(remaining, 1_000)
        return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"

    def audio_duration_ms(value: object, label: str) -> int:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} audio_path is required")
        path = (job_dir / value).resolve()
        try:
            path.relative_to(job_dir)
        except ValueError as exc:
            raise ValueError(f"{label} audio_path must stay inside the job directory") from exc
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"{label} audio file is not a non-empty file: {path}")
        tracks = MediaInfo.parse(str(path)).audio_tracks
        if not tracks or not tracks[0].duration:
            raise ValueError(f"{label} audio duration is unavailable: {path}")
        return round(float(tracks[0].duration))

    cursor_ms = 0
    entry_index = 1
    entries: list[str] = []
    for index, shot in enumerate(sorted(shots, key=lambda item: int(item.get("index", 0))), start=1):
        duration_ms = audio_duration_ms(shot.get("audio_path"), f"shot {index}")
        text = str(shot.get("subtitle") or shot.get("narration") or "").strip()
        if duration_ms <= 0 or not text:
            raise ValueError(f"shot {index} requires duration and subtitle")
        segments = shot.get("draft_subtitle_segments")
        if isinstance(segments, list) and segments:
            expected_text = visible_subtitle_segments(text)
            actual_text: list[str] = []
            previous_end_ms = 0
            for segment in segments:
                if not isinstance(segment, dict):
                    raise ValueError(f"shot {index} draft subtitle segment must be an object")
                segment_text = str(segment.get("text") or "").strip()
                start_ms = int(segment.get("start_ms", -1))
                segment_duration_ms = int(segment.get("duration_ms", 0))
                if not segment_text or start_ms < 0 or segment_duration_ms <= 0:
                    raise ValueError(f"shot {index} has an invalid draft subtitle segment")
                end_ms = start_ms + segment_duration_ms
                if start_ms < previous_end_ms or end_ms > duration_ms:
                    raise ValueError(f"shot {index} draft subtitle timing is outside its audio clip")
                actual_text.append(segment_text)
                entries.extend((str(entry_index), f"{timestamp(cursor_ms + start_ms)} --> {timestamp(cursor_ms + end_ms)}", segment_text, ""))
                entry_index += 1
                previous_end_ms = end_ms
            if actual_text != expected_text:
                raise ValueError(f"shot {index} draft subtitles differ from the approved visible subtitles")
        else:
            entries.extend((str(entry_index), f"{timestamp(cursor_ms)} --> {timestamp(cursor_ms + duration_ms)}", text, ""))
            entry_index += 1
        cursor_ms += duration_ms
    srt_path.write_text("\n".join(entries), encoding="utf-8")


def ffmpeg_filter_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", r"\'")


def compose(
    video_path: Path,
    product_grade: str,
    output_path: Path,
    template_path: Path,
    job_dir: Path | None = None,
) -> None:
    if not product_grade.strip():
        raise ValueError("product_grade is required")
    require_file(video_path, "video")
    require_file(template_path, "template")
    output_path = output_path.expanduser().resolve()
    if output_path.suffix.lower() != ".mp4":
        raise ValueError("output_path must end in .mp4")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="workmate-template-") as directory:
        overlay_path = Path(directory) / "overlay.png"
        subtitles_path = Path(directory) / "subtitles.srt"
        render_overlay(template_path, product_grade.strip(), overlay_path)
        if job_dir is not None:
            write_job_subtitles(job_dir.expanduser().resolve(), subtitles_path)
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        window_width = VIDEO_WINDOW[2] - VIDEO_WINDOW[0]
        window_height = VIDEO_WINDOW[3] - VIDEO_WINDOW[1]
        frame_width = CLEAR_VIDEO_FRAME[2] - CLEAR_VIDEO_FRAME[0]
        frame_height = CLEAR_VIDEO_FRAME[3] - CLEAR_VIDEO_FRAME[1]
        subtitle_filter = "[with_template]copy[outv]"
        if job_dir is not None:
            subtitle_filter = (
                "[with_template]subtitles='"
                + ffmpeg_filter_path(subtitles_path)
                + "':force_style='FontName=Source Han Sans SC,FontSize=38,Bold=1,"
                + "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=3,Shadow=0,"
                + f"Alignment=2,MarginV={CANVAS_SIZE[1] - SUBTITLE_BASELINE_Y}'[outv]"
            )
        command = [
            ffmpeg,
            "-y",
            "-i",
            str(video_path),
            "-loop",
            "1",
            "-i",
            str(overlay_path),
            "-filter_complex",
            (
                f"[0:v]split=2[blur_source][clear_source];"
                f"[blur_source]scale={window_width}:{window_height}:force_original_aspect_ratio=increase,"
                f"crop={window_width}:{window_height},boxblur=20:10[blurred];"
                f"[clear_source]scale={frame_width}:{frame_height}:force_original_aspect_ratio=decrease[clear];"
                f"[blurred]pad={CANVAS_SIZE[0]}:{CANVAS_SIZE[1]}:{VIDEO_WINDOW[0]}:{VIDEO_WINDOW[1]}:color=black[background];"
                f"[background][clear]overlay=x={CLEAR_VIDEO_FRAME[0]}+({frame_width}-overlay_w)/2:"
                f"y={CLEAR_VIDEO_FRAME[1]}+({frame_height}-overlay_h)/2:format=auto[with_video];"
                f"[with_video][1:v]overlay=0:0:format=auto[with_template];"
                + subtitle_filter
            ),
            "-map",
            "[outv]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Template video composition failed")
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError(f"Template video was not created: {output_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True, type=Path, help="Source MP4 to display in the template window")
    parser.add_argument("--grade", required=True, help="Product grade shown below the video")
    parser.add_argument("--output", required=True, type=Path, help="Output MP4 path")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE, help="Template image path")
    parser.add_argument("--job", type=Path, help="Job directory used to render timed subtitles")
    args = parser.parse_args()
    try:
        compose(args.video, args.grade, args.output, args.template, args.job)
        print(args.output.expanduser().resolve())
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
