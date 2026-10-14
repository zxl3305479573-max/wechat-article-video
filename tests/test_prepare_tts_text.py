import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from prepare_tts_text import prepare_job, prepare_text
import json
import tempfile
from unittest.mock import patch


class PrepareTtsTextTest(unittest.TestCase):
    def test_rebuilt_plan_keeps_matching_existing_audio(self) -> None:
        from generate_job_audio import audio_hash

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio_path = root / "audio/001.mp3"
            audio_path.parent.mkdir()
            audio_path.write_bytes(b"existing")
            narration = "PP 8008 适合注塑。"
            tts_text = prepare_text(narration, "PP 8008")
            job = {
                "product_grade": "PP 8008",
                "audio": {"voice_id": "female-chengshu", "speed": 1.25},
                "copy_approval": {"status": "approved"},
                "shots": [{
                    "index": 1, "narration": narration, "subtitle": narration,
                    "audio_path": "audio/001.mp3",
                    "audio_hash": audio_hash(tts_text, "female-chengshu", 1.25),
                }],
            }
            job_path = root / "video-job.json"
            job_path.write_text(json.dumps(job), encoding="utf-8")
            with patch("validate_video_job.require_copy_approval"):
                result = prepare_job(job_path)
            saved = json.loads(job_path.read_text(encoding="utf-8"))
            self.assertEqual(result["stale_audio"], 0)
            self.assertEqual(saved["shots"][0]["audio_hash"], job["shots"][0]["audio_hash"])
    def test_contextual_numeric_grades_and_measurements(self) -> None:
        text = "与7260参数接近，55物性相似，7260可替代，80这款料常见；温度180-230℃，灰分0.05%，转速140 rpm。"
        self.assertEqual(
            prepare_text(text, "HDPE 8008"),
            # Measurements keep normal number reading; only the range dash is
            # spoken as "到" (read_ranges_and_units), never digit by digit.
            "与七二六零参数接近，五五物性相似，七二六零可替代，八零这款料常见；温度180到230℃，灰分百分之0.05，转速140 rpm。",
        )

    def test_measurement_after_a_material_prefix_is_not_read_as_a_grade(self) -> None:
        self.assertEqual(prepare_text("PP 0.9 g/cm³的密度", "PP Z30S"), "PP 0.9 g/cm³的密度")
        self.assertEqual(prepare_text("PP 7260 和 PP Z30S", "PP Z30S"), "PP 七二六零 和 PP Z三零S")


if __name__ == "__main__":
    unittest.main()
