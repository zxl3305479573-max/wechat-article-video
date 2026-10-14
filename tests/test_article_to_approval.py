from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path

from docx import Document


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from article_to_approval import (
    render_subtitles,
    render_subtitles_docx,
    shot_narrations,
    validate_shot_distinctness,
    validate_generation_subtitles,
    overlong_clauses,
    main as approval_main,
    remove_duplicate_shot_blocks,
    classify_overlap,
    OverlapKind,
    shot_budget_warning,
    table_capacity_warning,
    table_parameter_warnings,
)
from subtitle_timing import SUBTITLE_MAX_WIDTH_UNITS, display_width_units, protected_subtitle_space_indexes, visible_subtitle_segments


class ApprovalSubtitleTests(unittest.TestCase):
    def test_copy_stage_warns_before_an_over_budget_plan_is_approved(self):
        self.assertIsNone(shot_budget_warning(7))
        warning = shot_budget_warning(15)
        self.assertIsNotNone(warning)
        self.assertIn("15 shots", warning)
        self.assertIn("7-shot budget", warning)

    def test_shot_naming_more_than_five_parameters_is_flagged_before_approval(self):
        five = "先看热性能。热变形温度约95 ℃，熔点约165 ℃，维卡软化点约150 ℃，料筒温度约190-210 ℃，模具温度约80-100 ℃。"
        six = five + "成型收缩率约2.0%。"
        self.assertEqual(table_parameter_warnings([(1, five)]), [])
        report = table_parameter_warnings([(1, five), (2, six)])
        self.assertEqual([item["shot"] for item in report], [2])
        self.assertEqual(report[0]["parameters"], 6)
        self.assertEqual(report[0]["limit"], 5)

    def test_article_with_more_rows_than_the_budget_renders_is_flagged(self):
        fifteen = "\n".join(f"参数{i} | 约{i} MPa" for i in range(1, 16))
        sixteen = fifteen + "\n参数16 | 约16 MPa"
        self.assertIsNone(table_capacity_warning(fifteen))
        warning = table_capacity_warning(sixteen)
        self.assertIsNotNone(warning)
        self.assertIn("16 parameter rows", warning)
        self.assertIn("15", warning)

    def test_auto_removes_later_duplicate_shot_block(self):
        copy = """开头\n\n### 镜头1\n\n口播：适合户外电子外壳，抵御紫外线与气候老化。\n\n### 镜头2\n\n口播：适合户外电子外壳，抵御紫外线与气候老化。\n\n### 镜头3\n\n口播：流动性好，适合薄壁成型。\n"""
        adjusted, shots, removed = remove_duplicate_shot_blocks(copy)
        self.assertEqual(removed, [2])
        self.assertEqual([index for index, _ in shots], [1, 2])
        self.assertEqual(adjusted.count("适合户外电子外壳，抵御紫外线与气候老化"), 1)
        self.assertIn("### 镜头2\n\n口播：流动性好", adjusted)

    def test_keeps_distinct_application_facts(self):
        copy = """### 镜头1\n\n口播：适合家电外壳与玩具，外观光泽好。\n\n### 镜头2\n\n口播：也用于日用品与汽车内饰件，兼顾强度与耐热。\n"""
        _adjusted, shots, removed = remove_duplicate_shot_blocks(copy)
        self.assertEqual([index for index, _ in shots], [1, 2])
        self.assertEqual(removed, [])

    def test_classifies_overlap_with_one_shared_source(self):
        self.assertIs(classify_overlap("相同内容", "相同内容"), OverlapKind.EXACT)
        self.assertIs(classify_overlap("不同事实一", "不同事实二"), OverlapKind.DISTINCT)

    def test_never_rewrites_or_reseals_human_approved_copy(self):
        copy = "### 镜头1\n\n口播：流动性好，适合薄壁成型。\n\n### 镜头2\n\n口播：流动性好，适合薄壁成型。\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_path = root / "copy-for-approval.md"
            copy_path.write_text(copy, encoding="utf-8")
            article_path = root / "article.json"
            article_path.write_text(json.dumps({"title": "t", "body_text": "流动性好"}, ensure_ascii=False), encoding="utf-8")
            approval = {"status": "approved", "copy_sha256": "old", "subtitle_sha256": "old"}
            (root / "video-job.json").write_text(json.dumps({"copy_approval": approval}), encoding="utf-8")
            argv = sys.argv
            sys.argv = ["article_to_approval.py", str(article_path), "--output-dir", str(root), "--product-grade", "PP Z30S"]
            try:
                with self.assertRaisesRegex(ValueError, "重新提交人工审批"):
                    approval_main()
            finally:
                sys.argv = argv
            self.assertEqual(copy_path.read_text(encoding="utf-8"), copy)
            saved = json.loads((root / "video-job.json").read_text(encoding="utf-8"))["copy_approval"]
            self.assertEqual(saved["copy_sha256"], "old")

    def test_distinct_facts_after_the_same_grade_are_not_duplicates(self):
        copy = (
            "### 镜头1\n做餐盒的老板看过来，这款PP Z30S透明度高。\n"
            "### 镜头2\n这款PP Z30S耐热性好，能进微波炉。\n"
            "### 镜头3\n这款PP Z30S刚性好，薄壁不变形。\n"
            "### 镜头4\n这款PP Z30S流动性好，适合薄壁注塑。\n"
        )
        adjusted, shots, removed = remove_duplicate_shot_blocks(copy)
        self.assertEqual(removed, [])
        self.assertEqual(len(shots), 4)
        self.assertEqual(adjusted, copy)

    def test_removing_a_duplicate_keeps_every_heading_on_its_own_line(self):
        copy = (
            "### 镜头1\n这款PP Z30S透明度高。\n"
            "### 镜头2\n这款PP Z30S透明度高。\n"
            "### 镜头3（应用）\n适合做餐盒。\n"
        )
        adjusted, shots, removed = remove_duplicate_shot_blocks(copy)
        self.assertEqual(removed, [2])
        self.assertEqual(adjusted, "### 镜头1\n这款PP Z30S透明度高。\n### 镜头2（应用）\n适合做餐盒。\n")
        self.assertEqual(shot_narrations(adjusted), [(1, "这款PP Z30S透明度高。"), (2, "适合做餐盒。")])

    def test_narration_wrapped_over_several_lines_is_read_whole(self):
        copy = "### 镜头1\n口播：做餐盒的老板看过来，PP Z30S透明度高，\n刚性也好，薄壁不容易变形。\n\n证据：P3\n"
        self.assertEqual(shot_narrations(copy), [(1, "做餐盒的老板看过来，PP Z30S透明度高，刚性也好，薄壁不容易变形。")])
        latin = "### 镜头1\n熔指约 20 g/10min，适合\nPP 注塑。\n- 备注\n"
        self.assertEqual(shot_narrations(latin), [(1, "熔指约 20 g/10min，适合PP 注塑。")])

    def test_markdown_separator_rows_are_not_counted_as_parameters(self):
        rows = "\n".join(f"| 参数{i} | 约{i} MPa |" for i in range(1, 16))
        self.assertIsNone(table_capacity_warning("| 项目 | 数值 |\n|---|---|\n" + rows))

    def test_rejects_duplicate_shot_copy(self):
        with self.assertRaisesRegex(ValueError, "镜头1与镜头2文案重复"):
            validate_shot_distinctness([(1, "适合注塑成型，流动性好"), (2, "适合注塑成型，流动性好")])

    def test_rejects_high_overlap_between_shots(self):
        with self.assertRaisesRegex(ValueError, "复用了同一段核心文案"):
            validate_shot_distinctness(
                [
                    (1, "适合户外电子外壳，抵御紫外线与气候老化，长期保持外观稳定"),
                    (2, "适合户外电子外壳，抵御紫外线与气候老化，长期保持外观稳定，同时便于注塑成型"),
                ]
            )

    def test_renders_editable_docx_with_ordered_captions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "subtitle-for-approval.docx"
            render_subtitles_docx(path, "测试文章", "PC 2407UV", [(1, "PC 2407UV，适合注塑。")])
            self.assertTrue(path.is_file())
            paragraphs = [paragraph.text for paragraph in Document(path).paragraphs]
            self.assertIn("镜头1", paragraphs)
            self.assertIn("PC 2407UV", paragraphs)
            self.assertIn("适合注塑", paragraphs)

    def test_renders_each_timeline_segment_on_its_own_line(self):
        copy = """### 镜头1

口播：HDPE 8008，密度0.955-0.957，熔指6.5到8.0；适合注塑。

证据：来源内容。
"""
        rendered = render_subtitles("HDPE 8008", "HDPE 8008", shot_narrations(copy))
        self.assertIn("HDPE 8008\n密度0.955-0.957\n熔指6.5到8.0\n适合注塑", rendered)
        self.assertNotIn("0.955-\n0.957", rendered)

    def test_material_grade_is_not_split_at_its_internal_space(self):
        rendered = render_subtitles(
            "HDPE 8008",
            "HDPE 8008",
            [(1, "HDPE 8008适合注塑。")],
        )
        self.assertNotIn("HDPE\n8008", rendered)

    def test_product_grade_space_is_protected_from_overflow_split(self):
        text = "长字幕 HDPE 8008 继续"
        self.assertEqual(protected_subtitle_space_indexes(text), {text.index(" ", text.index("HDPE"))})

    def test_visible_segments_keep_technical_values_and_never_overflow(self):
        lines = visible_subtitle_segments(
            "HDPE 8008密度0.955-0.957g/cm3，熔体流动速率25 MPa，加工温度190-230℃，断裂伸长率≥600%。"
        )
        self.assertTrue(all(display_width_units(line) <= SUBTITLE_MAX_WIDTH_UNITS for line in lines))
        self.assertTrue(any("0.955-0.957g/cm3" in line for line in lines))
        self.assertTrue(any("25 MPa" in line for line in lines))
        self.assertTrue(any("190-230℃" in line for line in lines))
        self.assertTrue(any("≥600%" in line for line in lines))

    def test_visible_segments_follow_jianying_17_unit_limit(self):
        self.assertEqual(len(visible_subtitle_segments("一二三四五六七八九十一二三四五六七")), 1)
        self.assertEqual(len(visible_subtitle_segments("一二三四五六七八九十一二三四五六七八")), 2)

    def test_dunhao_clause_wraps_instead_of_blocking_approval(self):
        lines = visible_subtitle_segments(
            "做齿轮、轴承保持架、接线端子的老板看过来，PA66 101L 是未增强注塑级聚酰胺66。"
        )
        self.assertTrue(all(display_width_units(line) <= SUBTITLE_MAX_WIDTH_UNITS for line in lines))
        self.assertEqual(len(lines), 3)

    def test_long_unbroken_clause_splits_at_token_boundary(self):
        source = "分子链中酰胺基团形成强氢键让材料在未增强状态下就具备较高的强度 刚性和耐热性"
        lines = visible_subtitle_segments(source)
        self.assertTrue(all(display_width_units(line) <= SUBTITLE_MAX_WIDTH_UNITS for line in lines))
        self.assertGreater(len(lines), 1)
        self.assertEqual("".join(lines).replace(" ", ""), source.replace(" ", ""))

    def test_generation_validation_accepts_wrappable_clause(self):
        validate_generation_subtitles(
            [(1, "做齿轮、轴承保持架、接线端子的老板看过来，PA66 101L 是未增强注塑级聚酰胺66。")]
        )

    def test_reports_overlong_clauses_for_rewriting(self):
        report = overlong_clauses(
            [
                (1, "做齿轮、轴承保持架、接线端子的老板看过来，PA66 101L 是未增强注塑级聚酰胺66。"),
                (2, "熔指6.5到8.0，适合注塑。"),
            ]
        )
        self.assertEqual([item["shot"] for item in report], [1])
        self.assertEqual(report[0]["width"], 18.0)
        self.assertIn("老板看过来", report[0]["clause"])

    def test_revised_copy_resyncs_subtitle_file(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "article.json").write_text(json.dumps({"title": "PA66 101L", "body_text": "x"}), encoding="utf-8")
            copy_path = root / "copy-for-approval.md"
            stale = "### 镜头1\n\n口播：旧的超长文案内容。\n"
            copy_path.write_text(
                "### 镜头1\n\n口播：做齿轮、轴承保持架的老板看过来，PA66 101L 是未增强注塑级聚酰胺66。\n",
                encoding="utf-8",
            )
            (root / "subtitle-for-approval.md").write_text(stale, encoding="utf-8")
            argv = [
                "article_to_approval.py",
                str(root / "article.json"),
                "--output-dir",
                str(root),
                "--product-grade",
                "PA66 101L",
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(approval_main(), 0)
            rendered = (root / "subtitle-for-approval.md").read_text(encoding="utf-8")
            self.assertNotIn("旧的超长文案内容", rendered)
            self.assertIn("做齿轮 轴承保持架", rendered)
