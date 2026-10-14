from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from audio_manager import TTS_CREATE_TIMEOUT, TTS_POLL_DEADLINE_SECONDS, AudioManager


class AudioManagerTests(unittest.TestCase):
    def test_minimax_tts_uses_explicit_voice_and_speed(self):
        response = Mock()
        response.json.return_value = {"task_id": "task"}
        success = Mock()
        success.json.return_value = {"status": "Success", "file_id": "file"}
        file_info = Mock()
        file_info.json.return_value = {"file": {"download_url": "https://audio.example/test.mp3"}}
        download = Mock(content=b"mp3")
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"MINIMAX_API_KEY": "key"}, clear=False), patch("audio_manager.requests.post", return_value=response) as post, patch("audio_manager.requests.get", side_effect=[success, file_info, download]), patch("audio_manager.time.sleep"):
            path = Path(temporary) / "voice.mp3"
            self.assertEqual(AudioManager().generate_audio("text", str(path), "female-chengshu", 1.25), str(path))
            self.assertEqual(path.read_bytes(), b"mp3")
            self.assertEqual(post.call_args.kwargs["json"]["voice_setting"], {"voice_id": "female-chengshu", "speed": 1.25, "vol": 1, "pitch": 0})
            # A long narration clip needs a generous read window; the connect
            # timeout stays short so a dead host still fails fast.
            self.assertEqual(post.call_args.kwargs["timeout"], TTS_CREATE_TIMEOUT)
            self.assertGreaterEqual(TTS_CREATE_TIMEOUT[1], 120.0)
            self.assertGreaterEqual(TTS_POLL_DEADLINE_SECONDS, 300.0)


class AsrRequestTests(unittest.TestCase):
    def run_asr(self, submissions, transcript):
        calls = []

        class FakeTranscription:
            @staticmethod
            def async_call(**kwargs):
                calls.append(kwargs)
                return submissions[len(calls) - 1]

            @staticmethod
            def wait(task):
                return Mock(status_code=200, output={"task_status": "SUCCEEDED", "result": {"transcription_url": "https://asr.example/t.json"}})

        dashscope = type(sys)("dashscope")
        dashscope.Files = Mock()
        qwen_asr = type(sys)("dashscope.audio.qwen_asr")
        qwen_asr.QwenTranscription = FakeTranscription
        modules = {"dashscope": dashscope, "dashscope.audio": type(sys)("dashscope.audio"), "dashscope.audio.qwen_asr": qwen_asr}
        response = Mock()
        response.json.return_value = transcript
        with patch.dict(sys.modules, modules), patch.dict(os.environ, {"DASHSCOPE_API_KEY": "key"}, clear=False), patch("audio_manager.requests.get", return_value=response):
            result = AudioManager().extract_audio_text("https://audio.example/a.mp3")
        return calls, result

    def test_requests_word_timestamps_and_returns_words(self):
        transcript = {"transcripts": [{"sentences": [{
            "text": "第一句", "begin_time": 0, "end_time": 900,
            "words": [{"text": "第一", "begin_time": 0, "end_time": 600}, {"text": "句", "begin_time": 600, "end_time": 900}],
        }]}]}
        calls, result = self.run_asr([Mock(status_code=200, output=Mock(task_id="t"))], transcript)
        self.assertTrue(calls[0]["enable_words"])
        self.assertEqual(result[0]["words"][1], {"text": "句", "begin_ms": 600, "end_ms": 900})

    def test_retries_without_word_options_when_they_are_rejected(self):
        transcript = {"transcripts": [{"sentences": [{"text": "第一句", "begin_time": 0, "end_time": 900}]}]}
        calls, result = self.run_asr(
            [Mock(status_code=400, message="bad parameter"), Mock(status_code=200, output=Mock(task_id="t"))],
            transcript,
        )
        self.assertEqual(len(calls), 2)
        self.assertNotIn("enable_words", calls[1])
        self.assertEqual(result, [{"text": "第一句", "begin_ms": 0, "end_ms": 900, "words": []}])
