#!/usr/bin/env python3
"""Skill-local MiniMax TTS and DashScope ASR adapter."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

# MiniMax/DashScope calls routinely need more time than a short default allows
# for a full narration clip: keep the connect timeout tight so a dead host fails
# fast, and give reads a generous window so a slow render is not mistaken for an
# outage. The polling deadline is the overall budget for one TTS task.
HTTP_CONNECT_TIMEOUT = 15.0
TTS_CREATE_TIMEOUT = (HTTP_CONNECT_TIMEOUT, 120.0)
TTS_POLL_TIMEOUT = (HTTP_CONNECT_TIMEOUT, 60.0)
TTS_DOWNLOAD_TIMEOUT = (HTTP_CONNECT_TIMEOUT, 180.0)
ASR_TRANSCRIPT_TIMEOUT = (HTTP_CONNECT_TIMEOUT, 120.0)
TTS_POLL_DEADLINE_SECONDS = 300.0
# enable_itn writes spoken numbers back as digits ("百分之六百" -> "600%"), which
# is how the visible subtitles spell them, so numeric clauses still align.
ASR_WORD_TIMESTAMP_OPTIONS = {"enable_words": True, "enable_itn": True}


def _subtitle_entries(payload: Any) -> list[dict[str, Any]] | None:
    """Find the sentence list in a MiniMax subtitle file, whatever wraps it."""
    if isinstance(payload, list):
        entries = [item for item in payload if isinstance(item, dict)]
        return entries if entries and all("time_begin" in item for item in entries) else None
    if isinstance(payload, dict):
        if "time_begin" in payload:
            return [payload]
        for value in payload.values():
            entries = _subtitle_entries(value)
            if entries:
                return entries
    return None


def parse_tts_subtitles(payload: Any) -> list[dict[str, Any]] | None:
    """Convert MiniMax subtitles to the sentence/word shape the aligner reads.

    Sentence entries carry `text`, `time_begin`, `time_end` and, with
    `subtitle_type=word`, `timestamped_words` of `word`, `time_begin`,
    `time_end` (milliseconds from the start of the audio).
    """
    entries = _subtitle_entries(payload)
    if not entries:
        return None
    segments: list[dict[str, Any]] = []
    for entry in entries:
        words = entry.get("timestamped_words") if isinstance(entry.get("timestamped_words"), list) else []
        segments.append(
            {
                "text": str(entry.get("text") or ""),
                "begin_ms": entry.get("time_begin"),
                "end_ms": entry.get("time_end"),
                "words": [
                    {
                        "text": str(word.get("word", word.get("text", "")) or ""),
                        "begin_ms": word.get("time_begin"),
                        "end_ms": word.get("time_end"),
                    }
                    for word in words
                    if isinstance(word, dict)
                ],
            }
        )
    return segments


class AudioManager:
    def __init__(self) -> None:
        self.minimax_api_key = os.getenv("MINIMAX_API_KEY", "").strip()
        self.dashscope_api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        self.domain = os.getenv("MINIMAX_DOMAIN", "https://api.minimaxi.com").rstrip("/")

    def generate_audio(self, text: str, save_path: str, voice_id: str, speed: float) -> str:
        """Write the MP3 and return its path (timestamps, if any, are discarded)."""
        self.synthesize(text, save_path, voice_id, speed)
        return str(Path(save_path))

    def synthesize(
        self, text: str, save_path: str, voice_id: str, speed: float
    ) -> list[dict[str, Any]] | None:
        """Write the MP3 and return MiniMax's word timestamps for it.

        The synchronous endpoint returns the audio and a subtitle file with
        per-word times for exactly the text it spoke, so subtitle timing needs
        no separate speech recognition. Returns None when the provider sent no
        usable timestamps (or MINIMAX_TTS_API=async); ASR then takes over.
        """
        if not self.minimax_api_key:
            raise ValueError("MINIMAX_API_KEY is required")
        if not 0.5 <= float(speed) <= 2.0:
            raise ValueError("speed must be between 0.5 and 2.0")
        if os.getenv("MINIMAX_TTS_API", "sync").strip().casefold() == "async":
            self._generate_async(text, save_path, voice_id, speed)
            return None
        group_id = os.getenv("MINIMAX_GROUP_ID", "").strip()
        response = requests.post(
            f"{self.domain}/v1/t2a_v2",
            headers={"Authorization": f"Bearer {self.minimax_api_key}", "Content-Type": "application/json"},
            params={"GroupId": group_id} if group_id else None,
            json={
                "model": os.getenv("MINIMAX_TTS_MODEL", "speech-2.6-turbo"),
                "text": text,
                "stream": False,
                "voice_setting": {"voice_id": voice_id, "speed": float(speed), "vol": 1, "pitch": 0},
                "audio_setting": {"format": "mp3", "sample_rate": 32000},
                "subtitle_enable": True,
                "subtitle_type": "word",
                "output_format": "hex",
            },
            timeout=TTS_CREATE_TIMEOUT,
        )
        response.raise_for_status()
        result = response.json()
        base = result.get("base_resp") if isinstance(result, dict) else None
        if isinstance(base, dict) and base.get("status_code") not in (None, 0):
            raise ValueError(f"MiniMax TTS failed: {base.get('status_code')} {base.get('status_msg') or ''}".strip())
        data = result.get("data") if isinstance(result, dict) else None
        audio_hex = data.get("audio") if isinstance(data, dict) else None
        if not isinstance(audio_hex, str) or not audio_hex:
            raise ValueError("MiniMax TTS returned no audio")
        destination = Path(save_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(bytes.fromhex(audio_hex))
        subtitle_url = data.get("subtitle_file")
        if not isinstance(subtitle_url, str) or not subtitle_url.strip():
            return None
        try:
            subtitles = requests.get(subtitle_url, timeout=TTS_POLL_TIMEOUT)
            subtitles.raise_for_status()
            return parse_tts_subtitles(subtitles.json())
        except (requests.RequestException, ValueError):
            # The MP3 is already paid for and saved; timing falls back to ASR.
            return None

    def _generate_async(self, text: str, save_path: str, voice_id: str, speed: float) -> str:
        """The asynchronous endpoint: audio only, no timestamps."""
        if not 0.5 <= float(speed) <= 2.0:
            raise ValueError("speed must be between 0.5 and 2.0")
        headers = {"Authorization": f"Bearer {self.minimax_api_key}", "Content-Type": "application/json"}
        response = requests.post(
            f"{self.domain}/v1/t2a_async_v2",
            headers=headers,
            json={
                "model": os.getenv("MINIMAX_TTS_MODEL", "speech-2.6-turbo"),
                "text": text,
                "voice_setting": {"voice_id": voice_id, "speed": float(speed), "vol": 1, "pitch": 0},
                "audio_setting": {"format": "mp3", "audio_sample_rate": 32000},
            },
            timeout=TTS_CREATE_TIMEOUT,
        )
        response.raise_for_status()
        task_id = response.json().get("task_id")
        if not task_id:
            raise ValueError("MiniMax TTS did not return task_id")
        deadline = time.monotonic() + TTS_POLL_DEADLINE_SECONDS
        while time.monotonic() < deadline:
            status = requests.get(
                f"{self.domain}/v1/query/t2a_async_query_v2",
                headers=headers,
                params={"task_id": task_id},
                timeout=TTS_POLL_TIMEOUT,
            )
            status.raise_for_status()
            result = status.json()
            state = str(result.get("status") or "").strip().casefold()
            if state == "success":
                file_id = result.get("file_id")
                if not file_id:
                    raise ValueError("MiniMax TTS succeeded without file_id")
                file_info = requests.get(
                    f"{self.domain}/v1/files/retrieve", headers=headers, params={"file_id": file_id}, timeout=TTS_POLL_TIMEOUT
                )
                file_info.raise_for_status()
                download_url = file_info.json().get("file", {}).get("download_url")
                if not download_url:
                    raise ValueError("MiniMax TTS did not return download_url")
                audio = requests.get(download_url, timeout=TTS_DOWNLOAD_TIMEOUT)
                audio.raise_for_status()
                destination = Path(save_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(audio.content)
                return str(destination)
            if state in {"fail", "failed", "error", "cancelled"}:
                raise ValueError(f"MiniMax TTS failed: {result.get('status')}")
            time.sleep(2)
        raise TimeoutError(f"MiniMax TTS polling timed out: {task_id}")

    def extract_audio_text(self, audio_path_or_url: str) -> list[dict[str, Any]]:
        if not self.dashscope_api_key:
            raise ValueError("DASHSCOPE_API_KEY is required for ASR")
        try:
            import dashscope
            from dashscope import Files
            from dashscope.audio.qwen_asr import QwenTranscription
        except ImportError as error:
            raise RuntimeError("dashscope is required for ASR; run the scripts with `uv run --frozen`, which installs it") from error

        dashscope.api_key = self.dashscope_api_key
        source = Path(audio_path_or_url)
        if source.is_file():
            upload = Files.upload(file_path=str(source), purpose="file-extract")
            if getattr(upload, "status_code", None) != 200:
                raise ValueError(f"DashScope ASR upload failed: {getattr(upload, 'message', '')}")
            uploaded = getattr(upload, "output", {}).get("uploaded_files", [])
            if not uploaded or not uploaded[0].get("file_id"):
                raise ValueError("DashScope ASR upload returned no file_id")
            details = Files.get(file_id=uploaded[0]["file_id"])
            if getattr(details, "status_code", None) != 200:
                raise ValueError(f"DashScope ASR file lookup failed: {getattr(details, 'message', '')}")
            file_url = getattr(details, "output", {}).get("url")
        else:
            file_url = audio_path_or_url
        if not file_url:
            raise ValueError("DashScope ASR requires a local file or public URL")
        # Word timestamps let subtitle boundaries land on the real pauses inside
        # an ASR sentence instead of being interpolated by character count. An
        # account or model revision that rejects the options still gets
        # sentence timestamps from a plain request.
        task = QwenTranscription.async_call(
            model="qwen3-asr-flash-filetrans", file_url=file_url, **ASR_WORD_TIMESTAMP_OPTIONS
        )
        if getattr(task, "status_code", None) != 200:
            task = QwenTranscription.async_call(model="qwen3-asr-flash-filetrans", file_url=file_url)
        if getattr(task, "status_code", None) != 200:
            raise ValueError(f"DashScope ASR submission failed: {getattr(task, 'message', '')}")
        task_id = getattr(getattr(task, "output", None), "task_id", None)
        if not task_id:
            raise ValueError("DashScope ASR returned no task_id")
        result = QwenTranscription.wait(task=task_id)
        if getattr(result, "status_code", None) != 200:
            raise ValueError(f"DashScope ASR failed: {getattr(result, 'message', '')}")
        output = getattr(result, "output", {})
        if output.get("task_status") != "SUCCEEDED":
            raise ValueError(f"DashScope ASR failed: {output.get('task_status')}")
        transcription_url = output.get("result", {}).get("transcription_url")
        if not transcription_url:
            return []
        transcript = requests.get(transcription_url, timeout=ASR_TRANSCRIPT_TIMEOUT)
        transcript.raise_for_status()
        sentences = transcript.json().get("transcripts", [{}])[0].get("sentences", [])
        return [
            {
                "text": item.get("text", ""),
                "begin_ms": item.get("begin_time"),
                "end_ms": item.get("end_time"),
                "words": [
                    {"text": word.get("text", ""), "begin_ms": word.get("begin_time"), "end_ms": word.get("end_time")}
                    for word in item.get("words") or []
                    if isinstance(word, dict)
                ],
            }
            for item in sentences
        ]
