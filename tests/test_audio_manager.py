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
