import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import brand
import jianying_local_draft
import render_export_cover
from video_constants import FOOTER_CAPTIONS


class BrandTests(unittest.TestCase):
    def test_bundled_brand_keeps_every_previously_hard_coded_value(self):
        loaded = brand.load_brand()
        self.assertEqual(loaded.footer_captions[4], "找兆企采购{product_grade}")
        self.assertEqual(len(FOOTER_CAPTIONS), 8)
        self.assertEqual((loaded.voice_id, loaded.speed), ("female-chengshu", 1.25))
        self.assertEqual(jianying_local_draft.CLEAR_VIDEO_FRAME, (0, 588, 1080, 1359))
        self.assertEqual(jianying_local_draft.TEMPLATE_SUBTITLE_TRANSFORM_Y, -938 / 1920)
        self.assertEqual(jianying_local_draft.HEADER_GRADE_TRANSFORM_Y, -1353 / 1920)
        self.assertEqual(render_export_cover.IMAGE_REGION, (0.068, 0.332, 0.932, 0.724))
        for name, path in loaded.assets.items():
            self.assertTrue(path.is_file(), f"{name} missing: {path}")
        self.assertEqual(jianying_local_draft.LOGO_PATH.name, "logo.png")

    def test_another_brand_file_is_selected_by_environment_and_resolves_its_own_assets(self):
        data = json.loads((brand.SKILL_ROOT / "brand.json").read_text(encoding="utf-8"))
        data["name"] = "另一家公司"
        data["footer_captions"] = ["欢迎咨询", "采购{product_grade}", "就找我们"]
        data["opening_hook"] = {"prefix": "", "suffix": "的朋友注意了"}
        data["assets"]["logo"] = "my-logo.png"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "other.json"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with patch.dict(os.environ, {brand.BRAND_FILE_ENV: str(path)}):
                self.assertEqual(brand.brand_file(), path.resolve())
            other = brand.parse_brand(data, path.resolve())
        self.assertEqual(other.assets["logo"], (Path(temporary) / "my-logo.png").resolve())
        match = brand.opening_hook_pattern(other).match("做餐盒的朋友注意了，PP Z30S……")
        self.assertEqual(match.group(1), "做餐盒")
        self.assertIsNone(brand.opening_hook_pattern(brand.load_brand()).match("做餐盒的朋友注意了，"))

    def test_invalid_brand_lists_every_problem(self):
        with self.assertRaises(ValueError) as raised:
            brand.parse_brand({"footer_captions": ["只有一句"], "voice": {"speed": 3}}, Path("bad.json"))
        message = str(raised.exception)
        for expected in ("footer_captions", "voice.voice_id", "voice.speed", "opening_hook.suffix", "closing_image_prompts", "assets.logo", "layout.image_frame"):
            self.assertIn(expected, message)


if __name__ == "__main__":
    unittest.main()
