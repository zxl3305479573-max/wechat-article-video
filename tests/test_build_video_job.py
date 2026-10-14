import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import article_to_approval
import build_video_job
from job_store import read_job


ARTICLE = (
    "HDPE 6094 适用于瓶盖、周转箱等注塑制品。\n\n"
    "物性：熔体流动速率 8 g/10min，密度 0.954 g/cm³。\n\n"
    "核心特性：刚性好，制品不易变形。"
)
COPY = """# HDPE 6094 视频文案

- **产品牌号**：HDPE 6094

## 标题候选

1. 做瓶盖周转箱的老板看过来：HDPE 6094（推荐）
2. HDPE 6094 注塑级牌号一分钟看懂
3. HDPE 6094 的物性参数速览
4. 瓶盖、周转箱选料：HDPE 6094
5. HDPE 6094 刚性好不易变形

### 镜头1

做瓶盖、周转箱的老板看过来，HDPE 6094 是一款注塑级牌号。

证据：HDPE 6094 适用于瓶盖、周转箱等注塑制品
模块：介绍

### 镜头2

先看参数，HDPE 6094 的熔体流动速率是8 g/10min，
密度是0.954 g/cm³。

证据：物性：熔体流动速率 8 g/10min，密度 0.954 g/cm³。
衔接：从应用场景转入支撑这些应用的物性参数
画面：参数表
表题：HDPE 6094 物性参数

| 项目 | 数值 |
|---|---|
| 熔体流动速率 | 8 g/10min |
| 密度 | 0.954 g/cm³ |

### 镜头3

正因为刚性好，HDPE 6094 做的制品不易变形。

证据：核心特性：刚性好，制品不易变形。
衔接：由参数说明落到制品表现
模块：特性
"""
PROMPTS = """### 镜头1
1. 14:10 horizontal composition, photorealistic rows of white bottle caps and stacked turnover boxes in a clean warehouse, no people, no visible text
2. 14:10 horizontal composition, injection molding factory floor producing turnover boxes, no people, no visible text

### 镜头3
- 14:10 horizontal composition, photorealistic sturdy turnover box holding heavy load without deforming, no people, no visible text
- 14:10 horizontal composition, close comparison of rigid bottle caps on a table, no people, no visible text
"""


def run(module, argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with patch.object(sys, "argv", argv), redirect_stdout(stdout), redirect_stderr(stderr):
        code = module.main()
    return code, stdout.getvalue(), stderr.getvalue()


class BuildVideoJobTests(unittest.TestCase):
    def prepare(self, root: Path, copy: str = COPY) -> None:
        (root / "article.json").write_text(json.dumps({"title": "HDPE 6094", "body_text": ARTICLE}, ensure_ascii=False), encoding="utf-8")
        (root / "copy-for-approval.md").write_text(copy, encoding="utf-8")
        (root / "image-prompts.md").write_text(PROMPTS, encoding="utf-8")
        code, _out, err = run(article_to_approval, ["x", str(root / "article.json"), "--output-dir", str(root), "--product-grade", "HDPE 6094"])
        self.assertEqual(code, 0, err)

    def test_approved_copy_becomes_a_sealed_valid_job_in_one_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.prepare(root)
            code, out, err = run(build_video_job, ["x", "--job", str(root), "--approved"])
            self.assertEqual(code, 0, err)
            result = json.loads(out)
            self.assertEqual(result["status"], "valid")
            self.assertEqual(result["planned_images"], 2 + 1 + 2 + 2)
            # The factory prompt was missing its mandatory clauses; the build completed it.
            self.assertIn("shot 1 image_prompts[2]", result["completed_prompts"])
            job = read_job(root / "video-job.json")
            self.assertRegex(job["copy_approval"]["copy_sha256"], r"^[0-9a-f]{64}$")
            first, table, third = job["shots"]
            self.assertEqual(first["opening_applications"], ["瓶盖", "周转箱"])
            self.assertEqual(table["narration"], "先看参数，HDPE 6094 的熔体流动速率是8 g/10min，密度是0.954 g/cm³。")
            self.assertEqual(table["table"]["rows"], [["熔体流动速率", "8 g/10min"], ["密度", "0.954 g/cm³"]])
            self.assertEqual(third["image_paths"], ["images/003-01.png", "images/003-02.png"])
            self.assertEqual(job["title_candidates"]["recommended"], 0)
            self.assertIn("wide-angle", first["image_prompts"][1])
            self.assertTrue((root / "script.md").is_file())

    def test_every_missing_input_is_reported_at_once(self):
        broken = COPY.replace("衔接：从应用场景转入支撑这些应用的物性参数\n", "").replace("证据：核心特性：刚性好，制品不易变形。\n", "")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.prepare(root, broken)
            (root / "image-prompts.md").write_text("### 镜头1\n1. only one prompt\n", encoding="utf-8")
            code, _out, err = run(build_video_job, ["x", "--job", str(root), "--approved"])
            self.assertEqual(code, 1)
            self.assertIn("镜头2 缺少 `衔接：`", err)
            self.assertIn("镜头3 缺少 `证据：`", err)
            self.assertIn("`### 镜头1` 下需要至少两条图片提示词", err)
            self.assertFalse((root / "video-job.json").exists())

    def test_refuses_without_explicit_approval_or_after_media_started(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.prepare(root)
            code, _out, err = run(build_video_job, ["x", "--job", str(root)])
            self.assertEqual(code, 1)
            self.assertIn("--approved", err)
            self.assertEqual(run(build_video_job, ["x", "--job", str(root), "--approved"])[0], 0)
            job = read_job(root / "video-job.json")
            job["shots"][0]["audio_hash"] = "paid"
            (root / "video-job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            code, _out, err = run(build_video_job, ["x", "--job", str(root), "--approved"])
            self.assertEqual(code, 1)
            self.assertIn("媒体生成已经开始", err)

    def test_metadata_lines_never_leak_into_the_approved_subtitles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.prepare(root)
            subtitles = (root / "subtitle-for-approval.md").read_text(encoding="utf-8")
            for leaked in ("证据", "模块", "衔接", "表题", "熔体流动速率 |"):
                self.assertNotIn(leaked, subtitles)


if __name__ == "__main__":
    unittest.main()


class ParagraphEvidenceTests(unittest.TestCase):
    def test_paragraph_numbers_resolve_to_article_lines(self):
        from article_paragraphs import paragraphs, resolve_evidence

        body = "第一段。\n\n  第二段\n参数 | 8\n"
        self.assertEqual([text for _s, _e, text in paragraphs(body)], ["第一段。", "第二段", "参数 | 8"])
        start, end = resolve_evidence(body, "P2")
        self.assertEqual(body[start:end], "第二段")
        start, end = resolve_evidence(body, "p2-P3")
        self.assertEqual(body[start:end], "第二段\n参数 | 8")
        self.assertIsNone(resolve_evidence(body, "P9"))
        self.assertIsNone(resolve_evidence(body, "P3-P2"))

    def test_copy_may_cite_paragraph_numbers_and_order_is_still_checked(self):
        cited = (
            COPY.replace("证据：物性：熔体流动速率 8 g/10min，密度 0.954 g/cm³。", "证据：P2")
            .replace("证据：核心特性：刚性好，制品不易变形。", "证据：P3")
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            BuildVideoJobTests.prepare(self, root, cited)
            self.assertIn("**P2** 物性：", (root / "article-paragraphs.md").read_text(encoding="utf-8"))
            code, out, err = run(build_video_job, ["x", "--job", str(root), "--approved"])
            self.assertEqual(code, 0, err)
            self.assertEqual([shot["article_evidence"] for shot in read_job(root / "video-job.json")["shots"]][1:], ["P2", "P3"])

            reordered = cited.replace("证据：P2", "证据：P3_").replace("证据：P3\n", "证据：P2\n").replace("证据：P3_", "证据：P3")
            (root / "copy-for-approval.md").write_text(reordered, encoding="utf-8")
            code, _out, err = run(build_video_job, ["x", "--job", str(root), "--approved"])
            self.assertEqual(code, 1)
            self.assertIn("out of source order", err)

    def test_unknown_paragraph_number_is_named(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            BuildVideoJobTests.prepare(self, root, COPY.replace("证据：核心特性：刚性好，制品不易变形。", "证据：P9"))
            code, _out, err = run(build_video_job, ["x", "--job", str(root), "--approved"])
            self.assertEqual(code, 1)
            self.assertIn("P9 names a paragraph the article does not have (it has 3)", err)
