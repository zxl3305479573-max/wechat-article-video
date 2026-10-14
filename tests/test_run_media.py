import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_media


JOB = {
    "shots": [
        {"index": 1, "image_paths": ["images/001-01.png", "images/001-02.png"], "subtitle_sync_status": "completed"},
        {"index": 2, "image_path": "images/002.png", "subtitle_sync_status": "fallback"},
    ],
    "closing_visuals": [{}, {}],
    "title_candidates": {"items": ["标题一", "标题二", "标题三", "标题四", "标题五"], "recommended": 1},
}
DRAFT = {
    "draft_path": "D:/Jianying/Article Video",
    "cover_path": "D:/job/final/cover.jpg",
    "landscape_cover_path": "D:/job/final/landscape-cover.png",
    "total_duration_seconds": 42.5,
    "status": "saved",
}


class RunMediaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.job_dir = Path(self.temporary.name)
        (self.job_dir / "video-job.json").write_text(json.dumps(JOB, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def patches(self, audio, images, draft=None):
        return [
            patch.object(run_media, "prepare_job"),
            patch.object(run_media.generate_job_audio, "generate", side_effect=audio),
            patch.object(run_media.jianying_local_draft, "verify_duration", return_value={"total_duration_us": 42_500_000}),
            patch.object(run_media.generate_job_images, "generate", side_effect=images),
            patch.object(run_media.prepare_local_assets, "load_planned_job", return_value={}),
            patch.object(run_media.prepare_local_assets, "prepare_once", return_value=(5, 5, 0)),
            patch.object(run_media.jianying_local_draft, "create_draft", return_value=draft or DRAFT),
        ]

    def run_with(self, audio, images):
        started = [item.start() for item in self.patches(audio, images)]
        try:
            return run_media.run(self.job_dir), started
        finally:
            patch.stopall()

    def test_narration_and_images_run_at_the_same_time_then_draft_and_summary(self):
        images_started = threading.Event()

        def audio(job_dir, sync):
            # Fails unless the image track is already running alongside.
            self.assertTrue(images_started.wait(5), "image generation did not run in parallel")
            return {"generated_shots": 2}

        def images(job_dir, max_concurrency):
            images_started.set()
            return {"generated": 5, "failed": 0, "provider": "wan", "model": "m", "model_source": "env"}

        result, mocks = self.run_with(audio, images)
        self.assertEqual(result["status"], "completed")
        self.assertEqual((result["shots"], result["images"]), (2, 5))
        self.assertEqual(result["subtitle_fallback_shots"], [2])
        text = result["summary"]
        self.assertTrue(text.startswith("封面：D:/job/final/cover.jpg\n横版封面："))
        self.assertIn("镜头 2 的字幕时间是估算值", text)
        self.assertIn("2. 标题二（推荐）", text)
        mocks[-1].assert_called_once()  # draft created

    def test_failed_images_stop_before_local_assets_and_draft_but_audio_still_finishes(self):
        audio = Mock(return_value={"generated_shots": 2})
        images = Mock(return_value={"generated": 3, "failed": 2})
        started = [item.start() for item in self.patches(audio, images)]
        try:
            with self.assertRaises(run_media.StageError) as raised:
                run_media.run(self.job_dir)
        finally:
            patch.stopall()
        self.assertEqual(raised.exception.stage, "生图")
        audio.assert_called_once()
        started[5].assert_not_called()  # prepare_once
        started[6].assert_not_called()  # create_draft

    def test_cli_prints_a_resumable_failure(self):
        with patch.object(run_media, "run", side_effect=run_media.StageError("配音与字幕时间", "MINIMAX_API_KEY is required")), \
                patch.object(sys, "argv", ["run_media.py", "--job", str(self.job_dir)]), \
                patch("builtins.print") as printed:
            self.assertEqual(run_media.main(), 1)
        payload = json.loads(printed.call_args.args[0])
        self.assertEqual(payload["stage"], "配音与字幕时间")
        self.assertIn("重新运行同一条", payload["resume"])


if __name__ == "__main__":
    unittest.main()
