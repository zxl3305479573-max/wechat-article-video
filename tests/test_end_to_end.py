"""The whole pipeline on real files; only the paid network calls are faked.

approval copy -> build_video_job -> run_media (narration and images in
parallel) -> tables and Logo -> a real Jianying draft folder.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
TESTS = Path(__file__).resolve().parent
for path in (SCRIPTS, TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import imageio_ffmpeg
from PIL import Image

import article_to_approval
import audio_manager
import build_video_job
import generate_job_images
import run_media
from test_build_video_job import ARTICLE, COPY, PROMPTS

CHAR_MS = 220


def fake_synthesize(self, text, save_path, voice_id, speed):
    """A silent MP3 of a plausible length plus one word timestamp per character."""
    spoken = [char for char in text if not char.isspace()]
    seconds = max(1.0, len(spoken) * CHAR_MS / 1000 + 0.3)
    subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=32000:cl=mono",
         "-t", f"{seconds:.3f}", "-q:a", "9", str(save_path)],
        check=True,
    )
    words, cursor = [], 150
    for char in spoken:
        words.append({"text": char, "begin_ms": cursor, "end_ms": cursor + CHAR_MS - 20})
        cursor += CHAR_MS
    return [{"text": text, "begin_ms": 150, "end_ms": cursor, "words": words}]


def fake_image(prompt, backend):
    buffer = io.BytesIO()
    Image.new("RGB", (1680, 1200), (90, 120, 160)).save(buffer, format="PNG")
    return buffer.getvalue()


class EndToEndTests(unittest.TestCase):
    def test_approved_copy_becomes_a_jianying_draft(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job_dir = root / "output" / "20261014-e2e-hdpe-6094"
            job_dir.mkdir(parents=True)
            drafts = root / "drafts"
            drafts.mkdir()
            (job_dir / "article.json").write_text(json.dumps({"title": "HDPE 6094", "body_text": ARTICLE}, ensure_ascii=False), encoding="utf-8")
            (job_dir / "copy-for-approval.md").write_text(COPY, encoding="utf-8")
            (job_dir / "image-prompts.md").write_text(PROMPTS, encoding="utf-8")
            env = {"JY_PROJECTS_ROOT": str(drafts), "IMAGE_PROVIDER": "wan", "DASHSCOPE_API_KEY": "test", "MINIMAX_API_KEY": "test"}
            with patch.dict(os.environ, env), \
                    patch.object(audio_manager.AudioManager, "synthesize", fake_synthesize), \
                    patch.object(generate_job_images, "request_image", side_effect=fake_image), \
                    redirect_stdout(io.StringIO()):
                with patch.object(sys, "argv", ["x", str(job_dir / "article.json"), "--output-dir", str(job_dir), "--product-grade", "HDPE 6094"]):
                    self.assertEqual(article_to_approval.main(), 0)
                with patch.object(sys, "argv", ["x", "--job", str(job_dir), "--approved"]):
                    self.assertEqual(build_video_job.main(), 0)
                result = run_media.run(job_dir)

            job = json.loads((job_dir / "video-job.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "completed")
            self.assertEqual((result["shots"], result["images"]), (3, 7))
            self.assertEqual(result["subtitle_fallback_shots"], [])
            self.assertTrue(result["summary"].startswith("封面："))
            for shot in job["shots"]:
                self.assertEqual(shot["subtitle_timing_source"], "tts_word_timestamps")
                segments = shot["asr_subtitle_segments"]
                self.assertEqual(segments[0]["start_ms"], 0)
                self.assertTrue(all(left["end_ms"] <= right["start_ms"] for left, right in zip(segments, segments[1:])))
            # The first clause "做瓶盖、周转箱的老板看过来，" is 13 spoken characters.
            self.assertEqual(job["shots"][0]["asr_subtitle_segments"][0]["end_ms"], 150 + 12 * CHAR_MS + CHAR_MS - 20)
            with Image.open(job_dir / "images" / "002.png") as table:
                self.assertEqual(table.size, (1680, 1200))
            draft_dir = Path(result["draft_path"])
            self.assertTrue((draft_dir / "draft_info.json").is_file() or (draft_dir / "draft_content.json").is_file())
            self.assertTrue((draft_dir / "media").is_dir())
            self.assertTrue(Path(result["cover_path"]).is_file())
            self.assertFalse((job_dir / "video-job.json.lock").exists())


if __name__ == "__main__":
    unittest.main()
