import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import jianying_local_draft
from prepare_tts_text import prepare_text
from subtitle_timing import split_timeline_segments, visible_subtitle_segments
from video_constants import FOOTER_CAPTIONS


class FixedFooterCopyTests(unittest.TestCase):
    def test_footer_copy_omits_product_authenticity_claim(self):
        self.assertNotIn("正品保证", " ".join(FOOTER_CAPTIONS))
        self.assertEqual(FOOTER_CAPTIONS, jianying_local_draft.FOOTER_SUBTITLE_TEMPLATES)
from validate_video_job import (
    invalidate_duration_verification,
    require_article_order,
    require_copy_approval,
    require_shot_budget,
    require_verified_duration,
    seal_copy_approval,
    validate,
)


PRODUCT_GRADE = "聚丙烯 HP550J"


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def approved_job(job_dir: Path, shot_count: int = 5) -> Path:
    """Create a complete pre-media job that can pass the normal validator."""
    (job_dir / "audio").mkdir(parents=True)
    (job_dir / "copy-for-approval.md").write_text("已审批文案", encoding="utf-8")
    (job_dir / "article.json").write_text(
        json.dumps({"body_text": f"原文：{PRODUCT_GRADE}"}, ensure_ascii=False),
        encoding="utf-8",
    )

    shots = []
    for index in range(1, shot_count + 1):
        audio_path = f"audio/{index:03d}.mp3"
        (job_dir / audio_path).write_bytes(b"placeholder")
        shots.append(
            {
                "index": index,
                "duration": 10,
                "opening_hook_type": "technical_number" if index == 1 else None,
                "article_evidence": f"原文：{PRODUCT_GRADE}",
                "factual_message": f"{PRODUCT_GRADE} 的材料参数",
                "narration": f"{PRODUCT_GRADE} 的材料参数。",
                "subtitle": f"{PRODUCT_GRADE} 的材料参数。",
                "narration_transition": "" if index == 1 else f"继续说明 {PRODUCT_GRADE} 的参数",
                "visual_type": "data_table",
                "image_ratio": "14:10",
                "image_path": f"images/{index:03d}.png",
                "audio_path": audio_path,
                "image_status": "planned",
                "audio_status": "planned",
                "table": {
                    "title": "参数",
                    "columns": ["项目", "数值"],
                    "rows": [["牌号", PRODUCT_GRADE]],
                },
            }
        )

    (job_dir / "audio" / "footer.mp3").write_bytes(b"placeholder")
    approval_subtitles = [
        *(f"### 镜头{index}\n{PRODUCT_GRADE} 的材料参数" for index in range(1, shot_count + 1)),
        "### 片尾",
        *(line for caption in FOOTER_CAPTIONS for line in visible_subtitle_segments(caption.format(product_grade=PRODUCT_GRADE))),
    ]
    (job_dir / "subtitle-for-approval.md").write_text("\n".join(approval_subtitles), encoding="utf-8")
    job = {
        "job_id": "p0-test",
        "product_grade": PRODUCT_GRADE,
        "copy_approval": {
            "status": "approved",
            "copy_path": "copy-for-approval.md",
            "subtitle_path": "subtitle-for-approval.md",
        },
        "audio": {
            "voice_id": "female-chengshu",
            "speed": 1.5,
            "footer_audio_status": "planned",
        },
        "title_candidates": {
            "items": [f"{PRODUCT_GRADE} 标题 {index}" for index in range(1, 6)],
            "recommended": 0,
        },
        "planned_total_duration_seconds": shot_count * 10 + 17,
        # Synthetic fixture: declare a budget so these cases exercise their own
        # rule instead of the shot-budget gate.
        "shot_budget": {
            "max_total": 12,
            "max_data_tables": 12,
            "reason": "synthetic test fixture",
        },
        "shots": shots,
        "closing_visuals": [
            {
                "image_prompt": "14:10 horizontal composition, industrial logistics closing scene, no visible text",
                "image_path": "images/closing-001.png",
                "image_ratio": "14:10",
                "image_status": "planned",
            },
            {
                "image_prompt": "14:10 horizontal composition, organized supply-chain closing scene, no visible text",
                "image_path": "images/closing-002.png",
                "image_ratio": "14:10",
                "image_status": "planned",
            },
        ],
    }
    job_path = job_dir / "video-job.json"
    write_json(job_path, job)
    seal_copy_approval(job_path)
    job = read_json(job_path)
    for shot in job["shots"]:
        shot["audio_status"] = "completed"
        shot["audio_hash"] = f"audio-{shot['index']}"
    job["audio"]["footer_audio_status"] = "completed"
    job["audio"]["footer_audio_hash"] = "footer-audio"
    write_json(job_path, job)
    return job_path


ARTICLE_SECTIONS = "介绍段。\n\n性能表段：密度0.900 g/cm³。\n\n特性段。\n\n应用段。"


def job_with_evidence(job_dir: Path, evidence: list[str]) -> Path:
    """Seal an approved job whose shots cite the given article sections."""
    job_path = approved_job(job_dir)
    (job_dir / "article.json").write_text(
        json.dumps({"body_text": ARTICLE_SECTIONS}, ensure_ascii=False), encoding="utf-8"
    )
    job = read_json(job_path)
    for shot, value in zip(job["shots"], evidence):
        shot["article_evidence"] = value
    write_json(job_path, job)
    return job_path


class ShotBudgetTests(unittest.TestCase):
    def plan(
        self, images: dict[str, int] | None = None, tables: int = 0, **budget: object
    ) -> dict:
        shots: list[dict] = []
        index = 1
        for module, count in (images or {}).items():
            for _ in range(count):
                shots.append({"index": index, "visual_type": "ai_image", "module": module})
                index += 1
        for _ in range(tables):
            shots.append({"index": index, "visual_type": "data_table"})
            index += 1
        job: dict = {"shots": shots}
        if budget:
            job["shot_budget"] = budget
        return job

    def test_default_budget_allows_seven_shots(self):
        require_shot_budget(
            self.plan({"introduction": 2, "characteristics": 2, "applications": 2}, tables=1)
        )

    def test_default_budget_rejects_an_eighth_shot(self):
        with self.assertRaisesRegex(ValueError, "max_total"):
            require_shot_budget(
                self.plan({"introduction": 2, "characteristics": 2, "applications": 2}, tables=2)
            )

    def test_more_than_three_tables_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "max_data_tables"):
            require_shot_budget(self.plan({"introduction": 2, "applications": 1}, tables=4))

    def test_three_shots_in_one_module_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "max_per_module"):
            require_shot_budget(
                self.plan({"introduction": 3, "characteristics": 2, "applications": 2})
            )

    def test_module_must_be_declared_on_every_non_table_shot(self):
        job = self.plan({"introduction": 2, "characteristics": 1})
        job["shots"].append({"index": 4, "visual_type": "ai_image"})
        with self.assertRaisesRegex(ValueError, "declare shot `module`"):
            require_shot_budget(job)

    def test_unknown_module_name_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "shot module must be one of"):
            require_shot_budget(self.plan({"参数表": 2}))

    def test_raised_budget_requires_a_reason(self):
        images = {"introduction": 2, "characteristics": 2, "applications": 2}
        with self.assertRaisesRegex(ValueError, "reason"):
            require_shot_budget(self.plan(images, tables=2, max_total=8))
        require_shot_budget(
            self.plan(images, tables=2, max_total=8, reason="用户要求 8 个镜头")
        )


class ArticleOrderTests(unittest.TestCase):
    def test_shots_that_consume_the_article_in_order_validate(self):
        with tempfile.TemporaryDirectory() as temporary:
            job_path = job_with_evidence(
                Path(temporary),
                ["介绍段。", "性能表段：密度0.900 g/cm³。", "特性段。", "应用段。", "应用段。"],
            )
            self.assertEqual(validate(job_path)["status"], "valid")

    def test_parameter_table_moved_after_applications_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            job_path = job_with_evidence(
                Path(temporary),
                ["介绍段。", "特性段。", "应用段。", "应用段。", "性能表段：密度0.900 g/cm³。"],
            )
            with self.assertRaisesRegex(ValueError, "source order"):
                validate(job_path)

    def test_application_address_hook_may_cite_a_later_section(self):
        shots = [
            {"index": 1, "article_evidence": "应用段。", "opening_hook_type": "application_address"},
            {"index": 2, "article_evidence": "介绍段。"},
            {"index": 3, "article_evidence": "性能表段：密度0.900 g/cm³。"},
        ]
        require_article_order(ARTICLE_SECTIONS, shots)


class CopyApprovalTests(unittest.TestCase):
    def test_eight_shot_plan_is_rejected_by_the_default_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            job_path = approved_job(Path(temporary), shot_count=8)
            job = read_json(job_path)
            job.pop("shot_budget", None)
            write_json(job_path, job)
            with self.assertRaisesRegex(ValueError, "shot_budget"):
                validate(job_path)

    def test_declared_budget_with_reason_allows_a_larger_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            job_path = approved_job(Path(temporary), shot_count=8)
            job = read_json(job_path)
            job["shot_budget"] = {
                "max_total": 8,
                "max_data_tables": 8,
                "reason": "用户明确要求 8 个镜头",
            }
            write_json(job_path, job)
            self.assertEqual(validate(job_path)["status"], "valid")

    def test_validation_reports_shots_and_planned_images_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            job_path = approved_job(Path(temporary), shot_count=5)
            result = validate(job_path)
            # 5 data_table shots (1 image each) + the 2 fixed closing visuals.
            self.assertEqual(result["shots"], 5)
            self.assertEqual(result["planned_images"], 7)

    def test_empty_tts_text_points_at_prepare_tts_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            job_path = approved_job(Path(temporary))
            job = read_json(job_path)
            job["shots"][0]["tts_text"] = ""
            write_json(job_path, job)
            with self.assertRaisesRegex(ValueError, "tts_text is empty.*prepare_tts_text"):
                validate(job_path)

    def test_untraceable_evidence_quotes_the_article_for_the_author(self):
        with tempfile.TemporaryDirectory() as temporary:
            job_path = approved_job(Path(temporary))
            job = read_json(job_path)
            job["shots"][0]["article_evidence"] = f"原文：{PRODUCT_GRADE} 的材料参数。"
            write_json(job_path, job)
            with self.assertRaisesRegex(ValueError, "the article reads"):
                validate(job_path)

    def test_sealed_approval_rejects_tampering_with_either_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            job_dir = Path(temporary)
            job_path = approved_job(job_dir)
            sealed_job = read_json(job_path)

            self.assertTrue(sealed_job["copy_approval"]["approved_at"])
            self.assertRegex(sealed_job["copy_approval"]["copy_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(sealed_job["copy_approval"]["subtitle_sha256"], r"^[0-9a-f]{64}$")
            require_copy_approval(job_dir, sealed_job)

            for filename in ("copy-for-approval.md", "subtitle-for-approval.md"):
                with self.subTest(filename=filename):
                    path = job_dir / filename
                    original = path.read_text(encoding="utf-8")
                    path.write_text(f"{original} 已修改", encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "changed after approval"):
                        require_copy_approval(job_dir, sealed_job)
                    path.write_text(original, encoding="utf-8")

    def test_media_started_job_cannot_be_sealed_again(self):
        with tempfile.TemporaryDirectory() as temporary:
            job_path = approved_job(Path(temporary))
            with self.assertRaisesRegex(ValueError, "cannot seal approval"):
                seal_copy_approval(job_path)


class TtsAndSubtitleTests(unittest.TestCase):
    def test_chinese_material_grade_is_spoken_digit_by_digit_without_changing_measurements(self):
        text = "聚丙烯 HP550J 的断裂伸长率为600%，拉伸强度25 MPa。"
        self.assertEqual(
            prepare_text(text, PRODUCT_GRADE),
            "聚丙烯 HP五五零J 的断裂伸长率为百分之600，拉伸强度25 MPa。",
        )

    def test_timeline_split_uses_spoken_sentence_boundaries(self):
        text = "牌号聚丙烯 HP550J、熔指5.8 g/10 min，拉伸强度25 MPa；问号？不切。"
        self.assertEqual(
            split_timeline_segments(text),
            [
                "牌号聚丙烯 HP550J、熔指5.8 g/10 min，",
                "拉伸强度25 MPa；",
                "问号？",
                "不切。",
            ],
        )

    def test_grade_stays_together_when_followed_by_chinese_text(self):
        lines = jianying_local_draft.one_line_subtitle_parts(
            "LDPE G8011低密度法聚合而成分子量分布宽兼具柔韧耐冲击耐低温"
        )
        self.assertTrue(any("LDPE G8011" in line for line in lines))
        self.assertFalse(any(line.endswith("LDPE") or line.startswith("G8011") for line in lines))


class ActualDurationTests(unittest.TestCase):
    def verify_with_durations(self, durations_us: list[int]) -> tuple[dict, dict]:
        with tempfile.TemporaryDirectory() as temporary:
            job_dir = Path(temporary)
            job_path = approved_job(job_dir)
            footer_path = job_dir / "footer.mp4"
            footer_path.write_bytes(b"placeholder")
            with (
                patch.object(
                    jianying_local_draft, "audio_duration_us", side_effect=durations_us
                ),
                patch.object(jianying_local_draft, "media_duration_us", return_value=8_000_000),
                patch.object(jianying_local_draft, "FOOTER_PATH", footer_path),
            ):
                result = jianying_local_draft.verify_duration(job_dir)
            return result, read_json(job_path)

    def test_actual_duration_accepts_exact_lower_and_upper_bounds(self):
        cases = {
            "short": [6_158_243] * 5 + [17_000_000],
            "long": [10_158_242] * 5 + [17_000_000],
        }
        for label, durations_us in cases.items():
            with self.subTest(total=label):
                result, job = self.verify_with_durations(durations_us)
                self.assertIsInstance(result, dict)
                self.assertEqual(job["actual_duration_status"], "verified")
                self.assertTrue(job["actual_timeline_audio_hash"])
                body_us = sum(durations_us[:-1])
                # The 17s closing voice-over splits into the captions kept on
                # the fixed 8s footer.mp4 and the remainder shown before it.
                self.assertEqual(
                    result["pre_footer_duration_us"] + result["footer_caption_duration_us"],
                    17_000_000,
                )
                self.assertGreater(result["pre_footer_duration_us"], 0)
                self.assertLessEqual(result["footer_caption_duration_us"], 8_000_000)
                self.assertGreaterEqual(result["total_duration_us"], body_us + 17_000_000)
                self.assertLessEqual(result["total_duration_us"], body_us + 25_000_000)
                self.assertEqual(
                    job["actual_total_duration_seconds"],
                    round(result["total_duration_us"] / 1_000_000, 6),
                )
                self.assertEqual(
                    [shot["duration_ms"] for shot in job["shots"]],
                    [round(duration / 1000) for duration in durations_us[:-1]],
                )
                self.assertEqual(job["audio"]["footer_duration_ms"], 17_000)

    def test_actual_duration_records_any_positive_measurement(self):
        durations_us = [10_158_443] * 5 + [17_000_000]
        with tempfile.TemporaryDirectory() as temporary:
            job_dir = Path(temporary)
            job_path = approved_job(job_dir)
            footer_path = job_dir / "footer.mp4"
            footer_path.write_bytes(b"placeholder")
            with (
                patch.object(
                    jianying_local_draft, "audio_duration_us", side_effect=durations_us
                ),
                patch.object(jianying_local_draft, "media_duration_us", return_value=8_000_000),
                patch.object(jianying_local_draft, "FOOTER_PATH", footer_path),
            ):
                result = jianying_local_draft.verify_duration(job_dir)

            job = read_json(job_path)
            self.assertEqual(job["actual_duration_status"], "verified")
            self.assertEqual(
                job["actual_total_duration_seconds"],
                round(result["total_duration_us"] / 1_000_000, 6),
            )
            self.assertGreater(
                result["total_duration_us"], sum(durations_us[:-1]) + 17_000_000
            )
            self.assertTrue(job["actual_timeline_audio_hash"])

    def test_audio_hash_change_invalidates_existing_duration_verification(self):
        result, job = self.verify_with_durations([10_158_242] * 5 + [17_000_000])
        self.assertIsInstance(result, dict)
        require_verified_duration(job)
        job["shots"][0]["audio_hash"] = "changed-audio"
        with self.assertRaisesRegex(ValueError, "audio changed after duration verification"):
            require_verified_duration(job)
        invalidate_duration_verification(job)
        with self.assertRaisesRegex(ValueError, "not verified"):
            require_verified_duration(job)

    def test_fixed_footer_audio_keeps_whole_captions_within_footer_video(self):
        captions = jianying_local_draft.footer_subtitles(PRODUCT_GRADE)
        timings = jianying_local_draft.footer_subtitle_timing(captions, 17_000_000)
        prefix, footer = jianying_local_draft.split_footer_captions_for_video(
            timings, 8_000_000
        )
        self.assertGreater(sum(duration for _, duration in prefix), 0)
        self.assertLessEqual(sum(duration for _, duration in footer), 8_000_000)
        self.assertEqual(prefix + footer, timings)

    def test_fixed_footer_subtitles_start_with_audio(self):
        parts = [("A", 1_000_000), ("B", 1_000_000)]
        self.assertEqual(
            jianying_local_draft.footer_subtitle_display_parts(parts),
            [("A", 0, 1_000_000), ("B", 1_000_000, 1_000_000)],
        )


if __name__ == "__main__":
    unittest.main()
