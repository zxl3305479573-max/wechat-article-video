import os
from array import array
import json
import hashlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import generate_job_audio
import generate_job_images
import jianying_local_draft
import prepare_local_assets
import compose_branded_video
from subtitle_timing import visible_subtitle_segments
from video_constants import FOOTER_CAPTIONS, shot_image_paths
from validate_video_job import validate_ai_image_prompt
from prepare_tts_text import prepare_text


class PromptValidationTests(unittest.TestCase):
    def test_factory_prompt_error_names_the_trigger_and_the_missing_phrase(self):
        with self.assertRaisesRegex(ValueError, 'mentions "factory floor".*"real industrial"'):
            validate_ai_image_prompt(
                "shot 1 image_prompts[1]",
                "14:10 horizontal composition, wide-angle photorealistic real automotive parts "
                "factory floor, no close-up, no macro, no studio scene",
            )

    def test_raw_material_prompt_error_names_the_trigger(self):
        with self.assertRaisesRegex(ValueError, 'mentions "plastic raw material".*white semi-transparent plastic pellets'):
            validate_ai_image_prompt(
                "shot 2 image_prompts[1]",
                "14:10 horizontal composition, plastic raw material on a clean workbench, no visible text",
            )

    def test_audio_state_owned_by_another_shot_fails_instead_of_repaying_tts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "audio").mkdir()
            job = {
                "product_grade": "PP K4912",
                "audio": {"voice_id": "female-chengshu", "speed": 1.25},
                "shots": [
                    {"index": 1, "tts_text": "第一段。", "audio_path": "audio/001.mp3"},
                    {"index": 2, "tts_text": "第二段。", "audio_path": "audio/002.mp3"},
                ],
            }
            (root / "video-job.json").write_text(
                json.dumps(job, ensure_ascii=False), encoding="utf-8"
            )
            occupant = generate_job_audio.audio_hash("第二段。", "female-chengshu", 1.25)

            def fake_state(_job_dir: Path, slot: str) -> dict:
                return {"status": "completed", "audio_hash": occupant} if slot == "shot-001" else {}

            with (
                patch.object(generate_job_audio, "prepare_job"),
                patch.object(generate_job_audio, "read_audio_state", side_effect=fake_state),
                patch("validate_video_job.validate"),
            ):
                with self.assertRaisesRegex(ValueError, "belongs to shot 2"):
                    generate_job_audio.generate(root, True)

    def test_image_state_owned_by_another_slot_fails_instead_of_repaying_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = {
                "shots": [
                    {
                        "index": 1,
                        "visual_type": "ai_image",
                        "image_paths": ["images/001-01.png", "images/001-02.png"],
                        "image_prompts": ["first prompt", "second prompt"],
                    }
                ],
                "closing_visuals": [],
            }
            (root / "video-job.json").write_text(
                json.dumps(job, ensure_ascii=False), encoding="utf-8"
            )
            occupant = generate_job_images.request_hash("second prompt", "wan2.7-image-pro")

            def fake_state(_job_dir: Path, slot: str) -> dict:
                return {"status": "completed", "request_hash": occupant} if slot == "shot-1-1" else {}

            with (
                patch.dict(
                    "os.environ",
                    {
                        "DASHSCOPE_API_KEY": "test",
                        "IMAGE_PROVIDER": "wan",
                        "WAN_IMAGE_MODEL": "wan2.7-image-pro",
                    },
                ),
                patch.object(generate_job_images, "load_dotenv"),
                patch.object(generate_job_images, "read_state", side_effect=fake_state),
                patch("validate_video_job.validate"),
                patch("validate_video_job.require_verified_duration"),
            ):
                with self.assertRaisesRegex(ValueError, "belongs to shot-1-2"):
                    generate_job_images.generate(root, 1)

    def test_negative_people_constraint_is_allowed(self):
        validate_ai_image_prompt(
            "shot 1",
            "14:10 horizontal composition, wide-angle real industrial factory floor, no close-up, no people",
        )

    def test_positive_people_requires_chinese_only(self):
        with self.assertRaisesRegex(ValueError, "Chinese people only"):
            validate_ai_image_prompt("shot 1", "14:10 horizontal composition, workers on factory floor")

    def test_grade_alias_is_preserved_and_digits_are_spoken(self):
        self.assertEqual(prepare_text("DMDA-8008 适合注塑", "HDPE 8008"), "DMDA-八零零八 适合注塑")


class ClosingVisualTests(unittest.TestCase):
    def test_multi_variant_shot_without_legacy_image_path_still_reports_paths(self):
        shot = {"index": 2, "image_paths": ["images/002-01.png", "images/002-02.png"]}
        self.assertEqual(
            shot_image_paths(shot), ["images/002-01.png", "images/002-02.png"]
        )
        self.assertEqual(shot_image_paths({"index": 2, "image_path": "images/002.png"}), ["images/002.png"])
        self.assertEqual(shot_image_paths({"index": 2, "image_paths": [], "image_path": None}), [])

    def test_local_shots_picks_up_multi_variant_images_without_legacy_image_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "images").mkdir()
            for name in ("002-01.png", "002-02.png"):
                (root / "images" / name).write_bytes(b"image")
            job = {
                "shots": [
                    {
                        "index": 2,
                        "visual_type": "ai_image",
                        "image_paths": ["images/002-01.png", "images/002-02.png"],
                    }
                ],
                "closing_visuals": [],
            }
            indexes = sorted(int(shot["index"]) for shot in prepare_local_assets.local_shots(root, job))
            self.assertEqual(indexes, [201, 202])

    def test_visual_entries_include_stable_closing_indexes(self):
        entries = prepare_local_assets.visual_entries({"shots": [{"index": 1}], "closing_visuals": [{"image_path": "a.png"}, {"index": 90007, "image_path": "b.png"}]})
        self.assertEqual([item["index"] for item in entries], [1, 90001, 90007])

    def test_prepare_once_persists_closing_result_without_existing_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "images").mkdir()
            (root / "images" / "closing.png").write_bytes(b"image")
            job = {"shots": [], "closing_visuals": [{"image_path": "images/closing.png"}]}
            job_path = root / "video-job.json"
            job_path.write_text(json.dumps(job), encoding="utf-8")
            with patch.object(prepare_local_assets.concurrent.futures, "ProcessPoolExecutor", prepare_local_assets.concurrent.futures.ThreadPoolExecutor), patch.object(prepare_local_assets, "local_asset_task", new=lambda *_args: (90001, "completed", {"image_path": "images/closing.png", "logo_source_hash": "hash"})):
                prepare_local_assets.prepare_once(root, job_path, job, write_job=True)
            saved = json.loads(job_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["closing_visuals"][0]["image_status"], "completed")
            self.assertEqual(saved["closing_visuals"][0]["logo_source_hash"], "hash")

    def test_prepare_once_updates_both_unindexed_closing_visuals(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "images").mkdir()
            for filename in ("closing-1.png", "closing-2.png"):
                (root / "images" / filename).write_bytes(b"image")
            job = {"shots": [], "closing_visuals": [{"image_path": "images/closing-1.png"}, {"image_path": "images/closing-2.png"}]}
            job_path = root / "video-job.json"
            job_path.write_text(json.dumps(job), encoding="utf-8")

            def completed_task(_root, shot, _logo):
                index = shot["index"]
                return index, "completed", {"logo_source_hash": f"hash-{index}"}

            with patch.object(prepare_local_assets.concurrent.futures, "ProcessPoolExecutor", prepare_local_assets.concurrent.futures.ThreadPoolExecutor), patch.object(prepare_local_assets, "local_asset_task", new=completed_task):
                prepare_local_assets.prepare_once(root, job_path, job, write_job=True)
            saved = json.loads(job_path.read_text(encoding="utf-8"))
            self.assertEqual([item["logo_source_hash"] for item in saved["closing_visuals"]], ["hash-90001", "hash-90002"])

    def test_precompute_mode_keeps_missing_ai_images_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = {"shots": [{"index": 1, "visual_type": "ai_image"}], "closing_visuals": []}
            job_path = root / "video-job.json"
            job_path.write_text(json.dumps(job), encoding="utf-8")
            expected, completed, failed = prepare_local_assets.prepare_once(root, job_path, job, write_job=False)
            self.assertEqual((expected, completed, failed), (1, 0, 0))

    def test_final_asset_stage_marks_missing_ai_images_failed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = {"shots": [{"index": 1, "visual_type": "ai_image"}], "closing_visuals": []}
            job_path = root / "video-job.json"
            job_path.write_text(json.dumps(job), encoding="utf-8")
            _expected, _completed, failed = prepare_local_assets.prepare_once(root, job_path, job, write_job=True)
            self.assertEqual(failed, 1)
            saved = json.loads(job_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["shots"][0]["image_status"], "failed")


class AsrBoundaryTests(unittest.TestCase):
    def run_sync(self, segments, duration=None, shot=None):
        fake_audio_manager = type("AudioManager", (), {"extract_audio_text": lambda self, path: segments})
        with tempfile.TemporaryDirectory() as temporary:
            audio = Path(temporary) / "shot.mp3"
            audio.write_bytes(b"mp3")
            shot = shot or {"subtitle": "第一句，第二句", "subtitle_segments": ["第一句", "第二句"]}
            with patch.object(generate_job_audio, "AudioManager", fake_audio_manager), patch.object(generate_job_audio, "measured_audio_duration_ms", return_value=duration):
                generate_job_audio.sync_subtitles(shot, audio)
            return shot

    def test_overlapping_asr_falls_back(self):
        shot = self.run_sync([{"begin_ms": 0, "end_ms": 700}, {"begin_ms": 600, "end_ms": 1000}], 1000)
        self.assertEqual(shot["subtitle_sync_status"], "fallback")
        self.assertNotIn("asr_subtitle_segments", shot)

    def test_merged_asr_sentence_keeps_asr_timing(self):
        shot = self.run_sync([{"text": "第一句第二句", "begin_ms": 0, "end_ms": 2000}], 2000)
        self.assertEqual(shot["subtitle_sync_status"], "completed")
        self.assertEqual(shot["subtitle_timing_source"], "asr_sentence_timestamps")
        self.assertEqual(
            shot["asr_subtitle_segments"],
            [
                {"text": "第一句", "start_ms": 0, "end_ms": 1000},
                {"text": "第二句", "start_ms": 1000, "end_ms": 2000},
            ],
        )

    def test_split_asr_sentences_align_to_authored_text(self):
        shot = self.run_sync(
            [
                {"text": "第一", "begin_ms": 0, "end_ms": 400},
                {"text": "句", "begin_ms": 400, "end_ms": 800},
                {"text": "第二句", "begin_ms": 800, "end_ms": 2000},
            ],
            2000,
        )
        self.assertEqual(shot["subtitle_sync_status"], "completed")
        self.assertEqual(
            shot["asr_subtitle_segments"],
            [
                {"text": "第一句", "start_ms": 0, "end_ms": 800},
                {"text": "第二句", "start_ms": 800, "end_ms": 2000},
            ],
        )

    def test_unrelated_asr_text_still_falls_back(self):
        shot = self.run_sync([{"text": "完全无关的内容", "begin_ms": 0, "end_ms": 2000}], 2000)
        self.assertEqual(shot["subtitle_sync_status"], "fallback")
        self.assertNotIn("asr_subtitle_segments", shot)

    def test_uncovered_audio_edges_fall_back(self):
        shot = self.run_sync([{"begin_ms": 300, "end_ms": 700}, {"begin_ms": 700, "end_ms": 900}], 1000)
        self.assertEqual(shot["subtitle_sync_status"], "fallback")

    def test_word_timestamps_put_the_boundary_on_the_real_pause(self):
        # One ASR sentence, but the second clause only starts after a long
        # pause. Character interpolation would cut at 1500 ms; words say 2000.
        words = [
            {"text": "第一", "begin_ms": 0, "end_ms": 600},
            {"text": "句", "begin_ms": 600, "end_ms": 900},
            {"text": "第二", "begin_ms": 2000, "end_ms": 2600},
            {"text": "句", "begin_ms": 2600, "end_ms": 3000},
        ]
        shot = self.run_sync(
            [{"text": "第一句，第二句", "begin_ms": 0, "end_ms": 3000, "words": words}], 3000
        )
        self.assertEqual(shot["subtitle_sync_status"], "completed")
        self.assertEqual(
            shot["asr_subtitle_segments"],
            [
                {"text": "第一句", "start_ms": 0, "end_ms": 900},
                {"text": "第二句", "start_ms": 2000, "end_ms": 3000},
            ],
        )

    def test_word_timestamps_win_even_when_sentence_counts_match(self):
        segments = [
            {"text": "第一句，第二句", "begin_ms": 0, "end_ms": 3000, "words": [
                {"text": "第一句", "begin_ms": 0, "end_ms": 900},
                {"text": "第二句", "begin_ms": 2000, "end_ms": 3000},
            ]},
        ]
        shot = self.run_sync(segments, 3000, shot={"subtitle": "第一句第二句", "subtitle_segments": ["第一句第二句"]})
        self.assertEqual(shot["asr_subtitle_segments"], [{"text": "第一句第二句", "start_ms": 0, "end_ms": 3000}])

    def test_tts_leading_and_trailing_silence_is_absorbed_not_rejected(self):
        shot = self.run_sync(
            [
                {"text": "第一句", "begin_ms": 450, "end_ms": 1200},
                {"text": "第二句", "begin_ms": 1300, "end_ms": 2100},
            ],
            2600,
        )
        self.assertEqual(shot["subtitle_sync_status"], "completed")
        self.assertEqual(
            shot["asr_subtitle_segments"],
            [
                {"text": "第一句", "start_ms": 0, "end_ms": 1200},
                {"text": "第二句", "start_ms": 1300, "end_ms": 2600},
            ],
        )

    def test_asr_that_lost_seconds_of_speech_still_falls_back(self):
        shot = self.run_sync(
            [
                {"text": "第一句", "begin_ms": 2000, "end_ms": 2600},
                {"text": "第二句", "begin_ms": 2700, "end_ms": 3200},
            ],
            3300,
        )
        self.assertEqual(shot["subtitle_sync_status"], "fallback")

    def test_clause_asr_spelled_differently_takes_the_time_between_neighbours(self):
        # "MFR" is heard as "熔体流动速率": no character of that clause matches,
        # which used to discard the whole clip's ASR timing.
        shot = {
            "subtitle": "这款料韧性很好，MFR，适合做周转箱。",
            "subtitle_segments": ["这款料韧性很好，", "MFR，", "适合做周转箱。"],
        }
        words = [
            {"text": "这款料", "begin_ms": 0, "end_ms": 600},
            {"text": "韧性很好", "begin_ms": 600, "end_ms": 1400},
            {"text": "熔体流动速率", "begin_ms": 1700, "end_ms": 2800},
            {"text": "适合做周转箱", "begin_ms": 3100, "end_ms": 5000},
        ]
        shot = self.run_sync(
            [{"text": "这款料韧性很好熔体流动速率适合做周转箱", "begin_ms": 0, "end_ms": 5000, "words": words}],
            5000,
            shot=shot,
        )
        self.assertEqual(shot["subtitle_sync_status"], "completed")
        self.assertEqual(
            [(item["start_ms"], item["end_ms"]) for item in shot["asr_subtitle_segments"]],
            [(0, 1400), (1400, 3100), (3100, 5000)],
        )

    def test_alignment_uses_the_spoken_tts_spelling_when_closer(self):
        shot = {
            "subtitle": "HDPE 8008，温度180-230℃。",
            "tts_text": "HDPE 八零零八，温度一百八十到二百三十摄氏度。",
            "subtitle_segments": ["HDPE 8008，", "温度180-230℃。"],
        }
        words = [
            {"text": "HDPE", "begin_ms": 0, "end_ms": 500},
            {"text": "八零零八", "begin_ms": 500, "end_ms": 1300},
            {"text": "温度", "begin_ms": 1700, "end_ms": 2100},
            {"text": "一百八十到二百三十摄氏度", "begin_ms": 2100, "end_ms": 4000},
        ]
        shot = self.run_sync(
            [{"text": "HDPE八零零八温度一百八十到二百三十摄氏度", "begin_ms": 0, "end_ms": 4000, "words": words}],
            4000,
            shot=shot,
        )
        self.assertEqual(shot["subtitle_sync_status"], "completed")
        self.assertEqual(
            shot["asr_subtitle_segments"],
            [
                {"text": "HDPE 8008，", "start_ms": 0, "end_ms": 1300},
                {"text": "温度180-230℃。", "start_ms": 1700, "end_ms": 4000},
            ],
        )

    def test_waveform_fallback_ignores_a_single_quiet_frame_between_syllables(self):
        import math
        import wave

        rate = 16000
        samples = []
        for position in range(2 * rate):
            ms = position * 1000 // rate
            # Speech everywhere except a 20 ms syllable dip at 1100 ms and the
            # real 300 ms comma pause at 1300-1600 ms.
            silent = 1100 <= ms < 1120 or 1300 <= ms < 1600
            samples.append(0 if silent else int(8000 * math.sin(2 * math.pi * 220 * position / rate)))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "shot.wav"
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(rate)
                output.writeframes(array("h", samples).tobytes())
            parts = jianying_local_draft.waveform_pause_timing("第一句话，第二句", 2_000_000, path)
        self.assertEqual([text for text, _ in parts], ["第一句话，", "第二句"])
        self.assertAlmostEqual(parts[0][1], 1_450_000, delta=40_000)

    def test_single_asr_segment_splits_contiguously_inside_asr_window(self):
        shot = {
            "subtitle": "第一句，第二句。",
            "narration": "第一句，第二句。",
            "subtitle_timing_source": "asr_sentence_timestamps",
            "asr_subtitle_segments": [{"text": "第一句第二句", "start_ms": 0, "end_ms": 2000}],
        }
        parts = jianying_local_draft.subtitle_parts_for_shot(shot, 2_000_000)
        self.assertEqual([text for text, _start, _duration in parts], ["第一句，", "第二句。"])
        self.assertTrue(parts[0][1] >= 0)
        self.assertEqual(parts[0][1] + parts[0][2], parts[1][1])
        self.assertEqual(parts[1][1] + parts[1][2], 2_000_000)

    def test_asr_pause_does_not_create_a_visible_subtitle_gap(self):
        shot = {
            "subtitle": "冲击强度约6 kJ/m²，适合冷储。",
            "subtitle_timing_source": "asr_sentence_timestamps",
            "asr_subtitle_segments": [
                {"text": "冲击强度约6 kJ/m²，", "start_ms": 0, "end_ms": 1_000},
                {"text": "适合冷储。", "start_ms": 1_800, "end_ms": 2_000},
            ],
        }
        parts = jianying_local_draft.subtitle_parts_for_shot(shot, 2_000_000)
        self.assertEqual(parts[0], ("冲击强度约6 kJ/m²，", 0, 1_800_000))
        self.assertEqual(parts[0][1] + parts[0][2], parts[1][1])


class VisualHashTests(unittest.TestCase):
    def test_visual_hash_changes_with_source_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "image.png"
            image.write_bytes(b"one")
            job = {"shots": [{"visual_type": "ai_image", "image_path": "image.png"}], "closing_visuals": []}
            with patch.object(jianying_local_draft, "LOGO_PATH", root / "logo.png"):
                (root / "logo.png").write_bytes(b"logo")
                first = jianying_local_draft.visual_asset_hash(root, job)
                image.write_bytes(b"two")
                second = jianying_local_draft.visual_asset_hash(root, job)
            self.assertNotEqual(first, second)

    def test_visual_hash_changes_with_second_image_variant(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_image = root / "first.png"
            second_image = root / "second.png"
            first_image.write_bytes(b"first")
            second_image.write_bytes(b"second")
            job = {"shots": [{"visual_type": "ai_image", "image_paths": ["first.png", "second.png"]}], "closing_visuals": []}
            with patch.object(jianying_local_draft, "LOGO_PATH", root / "logo.png"):
                (root / "logo.png").write_bytes(b"logo")
                first_hash = jianying_local_draft.visual_asset_hash(root, job)
                second_image.write_bytes(b"changed")
                second_hash = jianying_local_draft.visual_asset_hash(root, job)
            self.assertNotEqual(first_hash, second_hash)

    def test_shot_image_variants_use_distinct_logo_paths(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "images").mkdir()
            Image.new("RGB", (14, 10), "red").save(root / "images" / "first.png")
            Image.new("RGB", (14, 10), "blue").save(root / "images" / "second.png")
            logo = root / "logo.png"
            Image.new("RGBA", (1, 1), (255, 255, 255, 0)).save(logo)
            shot = {"index": 1, "visual_type": "ai_image"}
            with patch.object(jianying_local_draft, "LOGO_PATH", logo):
                paths = jianying_local_draft.shot_logo_images(
                    root, shot, ["images/first.png", "images/second.png"]
                )
            self.assertEqual([path.name for path in paths], ["101.png", "102.png"])
            self.assertNotEqual(paths[0].read_bytes(), paths[1].read_bytes())

    def test_visual_hash_rejects_empty_logo(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "image.png").write_bytes(b"image")
            job = {"shots": [{"visual_type": "ai_image", "image_path": "image.png"}], "closing_visuals": []}
            with patch.object(jianying_local_draft, "LOGO_PATH", root / "logo.png"):
                (root / "logo.png").write_bytes(b"")
                with self.assertRaisesRegex(ValueError, "non-empty"):
                    jianying_local_draft.visual_asset_hash(root, job)


class DraftBackupTests(unittest.TestCase):
    def test_saved_draft_refreshes_cover_from_the_first_image_variant(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "images").mkdir()
            (root / "images" / "001-01.png").write_bytes(b"first")
            (root / "images" / "001-02.png").write_bytes(b"second")
            (root / "logo.png").write_bytes(b"logo")
            (root / "draft").mkdir()
            job = {
                "job_id": "saved-refresh",
                "product_grade": "PP K4912",
                "title_candidates": {"items": ["a", "b", "c", "d", "e"], "recommended": 0},
                "shots": [
                    {
                        "index": 1,
                        "visual_type": "ai_image",
                        "image_paths": ["images/001-01.png", "images/001-02.png"],
                    }
                ],
                "closing_visuals": [],
                "draft": {
                    "adapter": "jianying-editor",
                    "status": "saved",
                    "draft_path": str(root / "draft"),
                    "layout_version": jianying_local_draft.DRAFT_LAYOUT_VERSION,
                    "visual_hash": "",
                },
            }
            job_path = root / "video-job.json"
            job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            rendered: dict[str, str] = {}

            def fake_covers(job_dir: Path, image_path: Path, grade: str):
                rendered["source"] = image_path.name
                final = job_dir / "final"
                final.mkdir(exist_ok=True)
                cover = final / "cover.jpg"
                cover.write_bytes(b"cover")
                landscape = final / "landscape-cover.png"
                landscape.write_bytes(b"landscape")
                return cover, landscape

            with (
                patch.object(jianying_local_draft, "LOGO_PATH", root / "logo.png"),
                patch.object(jianying_local_draft, "verify_duration", return_value={"total_duration_us": 20_000_000}),
                patch.object(jianying_local_draft, "load_job", return_value=(job_path, job)),
                patch.object(jianying_local_draft, "render_covers_cached", side_effect=fake_covers),
            ):
                job["draft"]["visual_hash"] = jianying_local_draft.visual_asset_hash(root, job)
                result = jianying_local_draft.create_draft(root)

            self.assertEqual(result["status"], "already_saved")
            self.assertEqual(rendered["source"], "001-01.png")

    def test_rebuild_snapshot_keeps_timeline_files_and_prunes_old_backups(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft = root / "drafts" / "Article Video - Demo"
            (draft / "media").mkdir(parents=True)
            (draft / "Timelines" / "abc").mkdir(parents=True)
            (draft / "draft_info.json").write_text("{}", encoding="utf-8")
            (draft / "Timelines" / "abc" / "draft_info.json").write_text("{}", encoding="utf-8")
            (draft / "media" / "stamped.png").write_bytes(b"x" * 32)
            job_dir = root / "job"
            job_dir.mkdir()

            self.assertIsNone(
                jianying_local_draft.snapshot_draft_folder(root / "missing", job_dir)
            )
            stamps = iter(["20260101-000001", "20260101-000002", "20260101-000003"])
            with (
                patch.object(jianying_local_draft, "DRAFT_BACKUP_KEEP", 2),
                patch.object(
                    jianying_local_draft.time,
                    "strftime",
                    side_effect=lambda _format: next(stamps),
                ),
            ):
                for index in range(3):
                    target = jianying_local_draft.snapshot_draft_folder(draft, job_dir)
                    os.utime(target, (1_000 + index, 1_000 + index))

            backups = sorted(path.name for path in (job_dir / "draft-backups").iterdir())
            self.assertEqual(len(backups), 2)
            self.assertNotIn("Article Video - Demo-20260101-000001", backups)
            kept = job_dir / "draft-backups" / backups[-1]
            self.assertTrue((kept / "draft_info.json").is_file())
            self.assertTrue((kept / "Timelines" / "abc" / "draft_info.json").is_file())
            self.assertFalse((kept / "media").exists())


class DraftMediaCopyTests(unittest.TestCase):
    def test_timeline_asset_is_copied_inside_draft(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp3"
            source.write_bytes(b"audio")
            draft = root / "draft"
            copied = jianying_local_draft.copy_draft_asset(
                draft, source, Path("audio/shots/001.mp3")
            )
            self.assertEqual(copied, (draft / "media/audio/shots/001.mp3").resolve())
            self.assertEqual(copied.read_bytes(), b"audio")

    def test_timeline_asset_cannot_escape_draft(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp3"
            source.write_bytes(b"audio")
            with self.assertRaises(ValueError):
                jianying_local_draft.copy_draft_asset(
                    root / "draft", source, Path("../../outside.mp3")
                )

    def test_saved_draft_rejects_material_outside_its_media_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft = root / "draft"
            draft.mkdir()
            external = root / "source.mp3"
            external.write_bytes(b"audio")
            (draft / "draft_info.json").write_text(
                json.dumps({"materials": {"audios": [{"path": str(external)}], "videos": []}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "outside its media directory"):
                jianying_local_draft.verify_portable_draft_media(draft)

    def test_saved_draft_accepts_self_contained_materials(self):
        with tempfile.TemporaryDirectory() as temporary:
            draft = Path(temporary) / "draft"
            audio = draft / "media/audio/001.mp3"
            video = draft / "media/video/001.png"
            audio.parent.mkdir(parents=True)
            video.parent.mkdir(parents=True)
            audio.write_bytes(b"audio")
            video.write_bytes(b"image")
            (draft / "draft_info.json").write_text(
                json.dumps({"materials": {"audios": [{"path": str(audio)}], "videos": [{"path": str(video)}]}}),
                encoding="utf-8",
            )
            self.assertEqual(jianying_local_draft.verify_portable_draft_media(draft), 2)


class VisualPathValidationTests(unittest.TestCase):
    def test_duplicate_ai_variant_image_is_rejected_before_media_work(self):
        from validate_video_job import seal_copy_approval, validate

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "copy-for-approval.md").write_text("approved", encoding="utf-8")
            grade = "PP HP550J"
            (root / "article.json").write_text(json.dumps({"body_text": grade}), encoding="utf-8")
            (root / "subtitle-for-approval.md").write_text(
                "\n".join([
                    grade,
                    *(line for caption in FOOTER_CAPTIONS for line in visible_subtitle_segments(caption.format(product_grade=grade))),
                ]),
                encoding="utf-8",
            )
            image = root / "image.png"
            image.write_bytes(b"image")
            job = {
                "product_grade": grade,
                "copy_approval": {"status": "approved", "copy_path": "copy-for-approval.md", "subtitle_path": "subtitle-for-approval.md"},
                "audio": {"voice_id": "female-chengshu", "speed": 1.5},
                "title_candidates": {"items": [f"{grade} {index}" for index in range(5)], "recommended": 0},
                "planned_total_duration_seconds": 50,
                "shots": [{
                    "index": 1, "duration": 6, "article_evidence": grade, "factual_message": grade,
                    "narration": grade, "subtitle": grade, "visual_type": "ai_image", "image_ratio": "14:10",
                    "image_paths": ["image.png", "image.png"],
                    "image_prompts": ["14:10 horizontal composition, first view", "14:10 horizontal composition, second view"],
                    "audio_path": "audio.mp3",
                }],
                "closing_visuals": [{"image_prompt": "14:10 horizontal composition, organized logistics scene", "image_ratio": "14:10", "image_path": "closing-1.png"}, {"image_prompt": "14:10 horizontal composition, organized supply scene", "image_ratio": "14:10", "image_path": "closing-2.png"}],
            }
            path = root / "video-job.json"
            path.write_text(json.dumps(job), encoding="utf-8")
            seal_copy_approval(path)
            with self.assertRaisesRegex(ValueError, "image_paths must be distinct"):
                validate(path)

    def test_duplicate_closing_image_is_rejected_before_media_work(self):
        from validate_video_job import seal_copy_approval, validate

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "copy-for-approval.md").write_text("approved", encoding="utf-8")
            grade = "PP HP550J"
            (root / "article.json").write_text(json.dumps({"body_text": grade}), encoding="utf-8")
            (root / "subtitle-for-approval.md").write_text(
                "\n".join([
                    grade,
                    *(line for caption in FOOTER_CAPTIONS for line in visible_subtitle_segments(caption.format(product_grade=grade))),
                ]),
                encoding="utf-8",
            )
            image = root / "image.png"
            image.write_bytes(b"image")
            job = {
                "product_grade": "PP HP550J",
                "copy_approval": {"status": "approved", "copy_path": "copy-for-approval.md", "subtitle_path": "subtitle-for-approval.md"},
                "audio": {"voice_id": "female-chengshu", "speed": 1.5},
                "title_candidates": {"items": [f"PP HP550J {index}" for index in range(5)], "recommended": 0},
                "planned_total_duration_seconds": 50,
                "shots": [{"index": 1, "duration": 6, "article_evidence": "PP HP550J", "factual_message": "PP HP550J", "narration": "PP HP550J", "subtitle": "PP HP550J", "visual_type": "data_table", "image_ratio": "14:10", "image_path": "image.png", "audio_path": "audio.mp3", "table": {"title": "t", "columns": ["a", "b"], "rows": [["a", "b"]]} }],
                "closing_visuals": [{"image_prompt": "14:10 horizontal composition, organized logistics scene", "image_ratio": "14:10", "image_path": "folder/../image.png"}, {"image_prompt": "14:10 horizontal composition, organized supply scene", "image_ratio": "14:10", "image_path": "other.png"}],
            }
            path = root / "video-job.json"
            path.write_text(json.dumps(job), encoding="utf-8")
            seal_copy_approval(path)
            with self.assertRaisesRegex(ValueError, "must be distinct"):
                validate(path)


class MediaDurationCacheTests(unittest.TestCase):
    def test_media_duration_cache_reuses_unchanged_file(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "clip.mp3"
            cache = root / ".media-cache.json"
            media.write_bytes(b"audio")
            info = SimpleNamespace(audio_tracks=[SimpleNamespace(duration=1.25)], video_tracks=[])
            with patch.object(jianying_local_draft.MediaInfo, "parse", return_value=info) as parse:
                self.assertEqual(jianying_local_draft.media_duration_us(media, "audio", cache_path=cache), 1_250)
                self.assertEqual(jianying_local_draft.media_duration_us(media, "audio", cache_path=cache), 1_250)
                self.assertEqual(parse.call_count, 1)
            cached = json.loads(cache.read_text(encoding="utf-8"))
            entry = next(iter(cached["entries"].values()))
            self.assertEqual(entry["sha256"], hashlib.sha256(b"audio").hexdigest())

    def test_media_duration_cache_invalidates_when_bytes_change(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "clip.mp3"
            cache = root / ".media-cache.json"
            media.write_bytes(b"audio")
            info = SimpleNamespace(audio_tracks=[SimpleNamespace(duration=1.0)], video_tracks=[])
            with patch.object(jianying_local_draft.MediaInfo, "parse", return_value=info) as parse:
                jianying_local_draft.media_duration_us(media, "audio", cache_path=cache)
                media.write_bytes(b"changed")
                jianying_local_draft.media_duration_us(media, "audio", cache_path=cache)
                self.assertEqual(parse.call_count, 2)


class BrandedSubtitleExportTests(unittest.TestCase):
    def test_rejects_stale_or_out_of_range_draft_subtitles(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "audio").mkdir()
            (root / "audio" / "001.mp3").write_bytes(b"audio")
            (root / "video-job.json").write_text(
                json.dumps(
                    {
                        "shots": [
                            {
                                "index": 1,
                                "audio_path": "audio/001.mp3",
                                "subtitle": "HDPE 8008适合注塑。",
                                "draft_subtitle_segments": [
                                    {"text": "错误字幕", "start_ms": 900, "duration_ms": 200}
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            info = SimpleNamespace(audio_tracks=[SimpleNamespace(duration=1000)])
            with patch.object(compose_branded_video.MediaInfo, "parse", return_value=info):
                with self.assertRaisesRegex(ValueError, "outside its audio clip"):
                    compose_branded_video.write_job_subtitles(root, root / "captions.srt")


class ReapprovalAndImageTests(unittest.TestCase):
    def test_reset_audio_for_reapproval_clears_media_state(self):
        from validate_video_job import reset_audio_for_reapproval

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = {
                "shots": [{"image_status": "planned", "audio_status": "completed", "audio_hash": "old", "duration_ms": 1000}],
                "audio": {"footer_audio_status": "completed", "footer_audio_hash": "old"},
                "actual_duration_status": "verified",
                "draft": {"status": "stale_audio"},
                "copy_approval": {"copy_sha256": "old", "subtitle_sha256": "old", "approved_at": "old"},
            }
            path = root / "video-job.json"
            path.write_text(json.dumps(job), encoding="utf-8")
            reset_audio_for_reapproval(path)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["shots"][0]["audio_status"], "planned")
            self.assertNotIn("audio_hash", saved["shots"][0])
            self.assertEqual(saved["audio"]["footer_audio_status"], "planned")
            self.assertEqual(saved["draft"]["status"], "not_started")

    def test_local_asset_rejects_actual_non_14_by_10_image(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image_path = root / "image.png"
            logo_path = root / "logo.png"
            Image.new("RGB", (1600, 900)).save(image_path)
            Image.new("RGBA", (1, 1)).save(logo_path)
            shot = {"index": 1, "image_path": "image.png", "visual_type": "ai_image"}
            with self.assertRaisesRegex(ValueError, "actual 14:10"):
                prepare_local_assets.local_asset_task(root, shot, logo_path)


if __name__ == "__main__":
    unittest.main()


class RobustnessTests(unittest.TestCase):
    def png_bytes(self):
        import io
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (140, 100)).save(buffer, format="PNG")
        return buffer.getvalue()

    def test_image_request_retries_once_after_a_dropped_connection(self):
        import requests

        backend = generate_job_images.ImageBackend("wan", "model", "key")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "a.png"
            with patch.object(generate_job_images, "request_image", side_effect=[requests.ConnectionError("reset"), self.png_bytes()]) as request:
                generate_job_images.generate_one("prompt", output, backend)
            self.assertEqual(request.call_count, 2)
            self.assertTrue(output.is_file())

    def test_image_request_does_not_retry_a_rejected_prompt(self):
        import requests

        rejected = requests.HTTPError("400", response=types.SimpleNamespace(status_code=400))
        backend = generate_job_images.ImageBackend("wan", "model", "key")
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(generate_job_images, "request_image", side_effect=[rejected]) as request:
                with self.assertRaises(requests.HTTPError):
                    generate_job_images.generate_one("prompt", Path(temporary) / "a.png", backend)
            self.assertEqual(request.call_count, 1)

    def test_detects_a_running_jianying_editor(self):
        running = types.SimpleNamespace(stdout='"explorer.exe","1"\r\n"JianyingPro.exe","4242"\r\n')
        idle = types.SimpleNamespace(stdout='"explorer.exe","1"\r\n')
        with patch.object(jianying_local_draft.sys, "platform", "win32"):
            with patch.object(jianying_local_draft.subprocess, "run", return_value=running):
                self.assertTrue(jianying_local_draft.jianying_is_running())
            with patch.object(jianying_local_draft.subprocess, "run", return_value=idle):
                self.assertFalse(jianying_local_draft.jianying_is_running())


class ProviderRetryAndSpeechTests(unittest.TestCase):
    def png_bytes(self):
        import io
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (140, 100)).save(buffer, format="PNG")
        return buffer.getvalue()

    def test_gemini_or_openai_5xx_is_retried_but_4xx_is_not(self):
        backend = generate_job_images.ImageBackend("doubao", "model", "key")
        busy = generate_job_images.ImageAPIError("busy", 503)
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(generate_job_images, "request_image", side_effect=[busy, self.png_bytes()]) as request:
                generate_job_images.generate_one("prompt", Path(temporary) / "a.png", backend)
            self.assertEqual(request.call_count, 2)
            bad = generate_job_images.ImageAPIError("bad prompt", 400)
            with patch.object(generate_job_images, "request_image", side_effect=[bad]) as request:
                with self.assertRaises(generate_job_images.ImageAPIError):
                    generate_job_images.generate_one("prompt", Path(temporary) / "b.png", backend)
            self.assertEqual(request.call_count, 1)

    def test_comparison_signs_are_spoken(self):
        self.assertEqual(prepare_text("断裂伸长率≥600%，灰分≤0.05%", "PP Z30S"), "断裂伸长率大于等于百分之600，灰分小于等于百分之0.05")
