import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from job_store import read_job, save_plan_job, save_visual_job


def plan() -> dict:
    return {
        "shots": [{"index": 1, "image_prompts": ["p"], "audio_status": "planned", "image_status": "planned"}],
        "closing_visuals": [{"image_prompt": "c", "image_status": "planned"}],
        "draft": {"status": "saved"},
    }


class JobStoreTests(unittest.TestCase):
    def test_audio_and_image_stages_loaded_earlier_do_not_roll_each_other_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "video-job.json"
            path.write_text(json.dumps(plan()), encoding="utf-8")
            audio_copy, image_copy = read_job(path), read_job(path)
            image_copy["shots"][0]["image_status"] = "completed"
            image_copy["closing_visuals"][0]["image_status"] = "completed"
            save_visual_job(path, image_copy)
            audio_copy["shots"][0]["audio_status"] = "completed"
            audio_copy["actual_duration_status"] = "verified"
            save_plan_job(path, audio_copy)
            saved = read_job(path)
            self.assertEqual(saved["shots"][0]["audio_status"], "completed")
            self.assertEqual(saved["shots"][0]["image_status"], "completed")
            self.assertEqual(saved["closing_visuals"][0]["image_status"], "completed")
            self.assertEqual(saved["actual_duration_status"], "verified")

    def test_visual_stage_never_writes_plan_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "video-job.json"
            path.write_text(json.dumps(plan()), encoding="utf-8")
            stale = read_job(path)
            completed = read_job(path)
            completed["closing_visuals"][0]["image_prompt"] = "completed prompt"
            save_plan_job(path, completed)  # e.g. validate --complete-prompts
            stale["shots"][0]["image_status"] = "completed"
            stale["draft"]["status"] = "stale_visual"
            save_visual_job(path, stale)
            saved = read_job(path)
            self.assertEqual(saved["closing_visuals"][0]["image_prompt"], "completed prompt")
            self.assertEqual(saved["shots"][0]["image_status"], "completed")
            self.assertEqual(saved["draft"]["status"], "stale_visual")

    def test_parallel_writers_keep_both_stages_last_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "video-job.json"
            path.write_text(json.dumps(plan()), encoding="utf-8")
            errors: list[BaseException] = []

            def audio() -> None:
                try:
                    for step in range(25):
                        job = read_job(path)
                        job["shots"][0]["audio_status"] = f"audio-{step}"
                        save_plan_job(path, job)
                except BaseException as error:  # surfaced below
                    errors.append(error)

            def images() -> None:
                try:
                    for step in range(25):
                        job = read_job(path)
                        job["shots"][0]["image_status"] = f"image-{step}"
                        save_visual_job(path, job)
                except BaseException as error:
                    errors.append(error)

            threads = [threading.Thread(target=audio), threading.Thread(target=images)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            saved = read_job(path)
            self.assertEqual(saved["shots"][0]["audio_status"], "audio-24")
            self.assertEqual(saved["shots"][0]["image_status"], "image-24")
            self.assertFalse(path.with_name("video-job.json.lock").exists())


if __name__ == "__main__":
    unittest.main()
