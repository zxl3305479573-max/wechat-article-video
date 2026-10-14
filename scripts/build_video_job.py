#!/usr/bin/env python3
"""Build, seal and validate video-job.json from the human-approved copy.

Run only after the user approved the copy. Inputs in the job directory:

- `article.json`: the source article;
- `copy-for-approval.md`: the approved copy. Besides each shot's narration it
  carries labelled lines (`证据：`, `模块：`, `画面：`, `衔接：`, `表题：`), a
  Markdown table for a parameter shot, and a `标题候选` list of five titles;
- `image-prompts.md`: two or more image prompts under each `### 镜头N` of an
  AI-image shot, optionally `### 收尾1` / `### 收尾2` for the closing images.

Narration and subtitles are copied verbatim from the approved copy, so they
always match the sealed approval files. The command then seals the approval,
appends any missing mandatory prompt clauses and runs the full preflight.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from article_to_approval import SHOT_HEADING, shot_narrations
from brand import load_brand, opening_hook_pattern
from job_store import read_job, save_whole_job
from validate_video_job import complete_job_prompts, media_generation_started, seal_copy_approval


PLACEHOLDER_SHOT_SECONDS = 5  # informational only; measured MP3s replace it
CLOSING_SECONDS = 17
MODULES = {
    "介绍": "introduction",
    "产品介绍": "introduction",
    "开场": "introduction",
    "introduction": "introduction",
    "特性": "characteristics",
    "核心特性": "characteristics",
    "性能": "characteristics",
    "characteristics": "characteristics",
    "应用": "applications",
    "典型应用": "applications",
    "applications": "applications",
}
VISUALS = {
    "参数表": "data_table",
    "表格": "data_table",
    "数据表": "data_table",
    "data_table": "data_table",
    "ai配图": "ai_image",
    "ai 配图": "ai_image",
    "配图": "ai_image",
    "ai_image": "ai_image",
}
EVIDENCE_LABELS = ("证据", "文章依据", "文章证据")
RECOMMENDED_MARKS = ("（推荐）", "(推荐)", "【推荐】", "[推荐]", "★")


def labelled(lines: list[str], labels: tuple[str, ...]) -> str | None:
    """Return the value of the first `label：value` line for any of `labels`."""
    for line in lines:
        match = re.match(rf"^\s*(?:{'|'.join(map(re.escape, labels))})\s*[：:]\s*(.*)$", line)
        if match:
            return match.group(1).strip()
    return None


def strip_quotes(value: str) -> str:
    return value.strip().strip("「」“”\"'『』").strip()


def table_rows(lines: list[str]) -> list[list[str]]:
    rows = []
    for line in lines:
        text = line.strip()
        if not text.startswith("|"):
            continue
        cells = [cell.strip() for cell in text.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells if cell):
            continue  # Markdown separator row
        rows.append(cells)
    return rows


def shot_blocks(copy: str) -> dict[int, list[str]]:
    matches = list(SHOT_HEADING.finditer(copy))
    blocks: dict[int, list[str]] = {}
    for position, match in enumerate(matches):
        end = matches[position + 1].start() if position + 1 < len(matches) else len(copy)
        body = copy[match.end():end]
        # A later non-shot heading (such as a title list) ends the shot block.
        body = re.split(r"^\s{0,3}#{1,6}\s", body, maxsplit=1, flags=re.MULTILINE)[0]
        blocks[int(match.group(1))] = body.splitlines()
    return blocks


def title_candidates(copy: str) -> dict[str, Any]:
    """Read the five numbered or bulleted titles after a 标题候选 heading."""
    lines = copy.splitlines()
    start = next((i for i, line in enumerate(lines) if "标题候选" in line), None)
    if start is None:
        raise ValueError("copy-for-approval.md needs a '标题候选' section listing five titles")
    items: list[str] = []
    recommended = None
    for line in lines[start + 1:]:
        text = line.strip()
        if re.match(r"^#{1,6}\s", text) or (items and not text):
            break
        match = re.match(r"^(?:[-*]|\d+[.、)）])\s*(.+)$", text)
        if not match:
            continue
        title = match.group(1).strip()
        if any(mark in title for mark in RECOMMENDED_MARKS):
            recommended = len(items)
            for mark in RECOMMENDED_MARKS:
                title = title.replace(mark, "")
        items.append(title.strip())
    if len(items) != 5:
        raise ValueError(f"the '标题候选' section must list exactly five titles, found {len(items)}")
    return {"items": items, "recommended": recommended if recommended is not None else 0}


def image_prompts(path: Path) -> dict[str, list[str]]:
    """Map '镜头N' / '收尾N' to the prompt lines written under that heading."""
    if not path.is_file():
        return {}
    prompts: dict[str, list[str]] = {}
    current: str | None = None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        heading = re.match(r"^#{2,3}\s*(镜头|收尾)\s*(\d+)", line.strip())
        if heading:
            current = f"{heading.group(1)}{int(heading.group(2))}"
            prompts[current] = []
            continue
        text = re.sub(r"^\s*(?:[-*]|\d+[.、)）]|图\s*\d+\s*[：:])\s*", "", line).strip()
        if current and text:
            prompts[current].append(text)
    return prompts


def opening(narration: str) -> tuple[str, list[str] | None]:
    match = opening_hook_pattern(load_brand()).match(narration)
    if not match:
        return "evidence_hook", None
    applications = [item.strip() for item in re.split(r"[、，,/]|和|及|与", match.group(1)) if item.strip()]
    return "application_address", applications


def build(job_dir: Path, product_grade: str, args: argparse.Namespace) -> dict[str, Any]:
    article = json.loads((job_dir / "article.json").read_text(encoding="utf-8-sig"))
    copy = (job_dir / "copy-for-approval.md").read_text(encoding="utf-8-sig")
    narrations = dict(shot_narrations(copy))
    blocks = shot_blocks(copy)
    prompts = image_prompts(job_dir / "image-prompts.md")
    problems: list[str] = []
    shots: list[dict[str, Any]] = []
    for position, index in enumerate(sorted(narrations), start=1):
        lines = blocks.get(index, [])
        narration = narrations[index]
        shot: dict[str, Any] = {
            "index": position,
            "duration": PLACEHOLDER_SHOT_SECONDS,
            "article_evidence": strip_quotes(labelled(lines, EVIDENCE_LABELS) or ""),
            "factual_message": narration,
            "narration": narration,
            "subtitle": narration,
            "image_ratio": "14:10",
            "audio_path": f"audio/{position:03d}.mp3",
            "image_status": "planned",
            "draft_import_status": "planned",
            "status": "planned",
        }
        if not shot["article_evidence"]:
            problems.append(f"镜头{index} 缺少 `证据：` 行（逐字摘自原文）")
        rows = table_rows(lines)
        visual_value = (labelled(lines, ("画面",)) or "").casefold().strip()
        visual = VISUALS.get(visual_value) if visual_value else ("data_table" if rows else "ai_image")
        if visual is None:
            problems.append(f"镜头{index} 的 `画面：{visual_value}` 无法识别，请写 AI 配图 或 参数表")
            visual = "ai_image"
        shot["visual_type"] = visual
        module_value = labelled(lines, ("模块",))
        if module_value:
            module = MODULES.get(module_value.casefold().strip())
            if module is None:
                problems.append(f"镜头{index} 的 `模块：{module_value}` 无法识别，请写 介绍 / 特性 / 应用")
            else:
                shot["module"] = module
        transition = labelled(lines, ("衔接",))
        if position > 1:
            if transition:
                shot["narration_transition"] = transition
            else:
                problems.append(f"镜头{index} 缺少 `衔接：` 行（说明它如何承接上一个镜头）")
        if position == 1:
            hook, applications = opening(narration)
            shot["opening_hook_type"] = hook
            if applications:
                shot["opening_applications"] = applications
        if visual == "data_table":
            if len(rows) < 2:
                problems.append(f"镜头{index} 是参数表，需要一个带表头的 Markdown 表格")
            else:
                shot["table"] = {
                    "title": labelled(lines, ("表题",)) or f"{product_grade} 参数",
                    "columns": rows[0],
                    "rows": rows[1:],
                }
            shot["image_path"] = f"images/{position:03d}.png"
        else:
            shot_prompts = prompts.get(f"镜头{index}", [])
            if len(shot_prompts) < 2:
                problems.append(f"image-prompts.md 的 `### 镜头{index}` 下需要至少两条图片提示词")
            shot["image_prompts"] = shot_prompts
            shot["image_paths"] = [f"images/{position:03d}-{variant:02d}.png" for variant in range(1, len(shot_prompts) + 1)]
        shots.append(shot)
    if not shots:
        problems.append("copy-for-approval.md 中没有找到任何 `### 镜头N`")
    try:
        titles = title_candidates(copy)
    except ValueError as error:
        problems.append(str(error))
        titles = {}
    if problems:
        raise ValueError("无法从审批文案生成 video-job.json：\n- " + "\n- ".join(problems))

    closing = []
    for position in (1, 2):
        custom = prompts.get(f"收尾{position}") or []
        closing.append({
            "image_prompt": custom[0] if custom else load_brand().closing_image_prompts[position - 1],
            "image_path": f"images/closing-{position:03d}.png",
            "image_ratio": "14:10",
            "image_status": "planned",
        })
    source = {
        "source_type": article.get("source_type") or "user_provided_text_or_docx",
        "title": article.get("title"),
        "author": article.get("author"),
        "original_url": article.get("original_url") or article.get("content_url"),
        "source_file": article.get("source_file"),
    }
    for field in ("article_id", "article_index", "updated_at", "content_url"):
        if article.get(field) is not None:
            source[field] = article[field]
    job: dict[str, Any] = {
        "version": 1,
        "job_id": job_dir.name,
        "source": source,
        "copy_approval": {
            "status": "approved",
            "copy_path": "copy-for-approval.md",
            "subtitle_path": "subtitle-for-approval.md",
        },
        "format": {"aspect_ratio": "9:16"},
        "product_grade": product_grade,
        "planned_total_duration_seconds": PLACEHOLDER_SHOT_SECONDS * len(shots) + CLOSING_SECONDS,
        "audio": {
            "voice_id": args.voice_id or load_brand().voice_id,
            "speed": args.speed if args.speed is not None else load_brand().speed,
        },
        "title_candidates": titles,
        "closing_visuals": closing,
        "shots": shots,
        "draft": {"adapter": "jianying-editor", "status": "not_started"},
    }
    if args.budget_reason:
        budget: dict[str, Any] = {"reason": args.budget_reason}
        for key in ("max_total", "max_data_tables", "max_per_module"):
            if getattr(args, key) is not None:
                budget[key] = getattr(args, key)
        job["shot_budget"] = budget
    return job


def storyboard(job: dict[str, Any]) -> str:
    """A readable script.md of what will be produced, shot by shot."""
    lines = [f"# {job['product_grade']} 视频分镜", ""]
    for shot in job["shots"]:
        lines += [f"## 镜头{shot['index']}", "", f"- 口播：{shot['narration']}", f"- 证据：{shot['article_evidence']}"]
        if shot.get("module"):
            lines.append(f"- 模块：{shot['module']}")
        if shot.get("narration_transition"):
            lines.append(f"- 衔接：{shot['narration_transition']}")
        if shot["visual_type"] == "data_table":
            table = shot["table"]
            lines += [f"- 画面：参数表《{table['title']}》", "", "| " + " | ".join(table["columns"]) + " |", "|" + "---|" * len(table["columns"])]
            lines += ["| " + " | ".join(row) + " |" for row in table["rows"]]
        else:
            lines.append("- 画面：AI 配图")
            lines += [f"  {number}. {prompt}" for number, prompt in enumerate(shot["image_prompts"], start=1)]
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job", required=True, type=Path, help="output\\<job-id> directory")
    parser.add_argument("--approved", action="store_true", help="Required: the user explicitly approved the copy")
    parser.add_argument("--product-grade", help="Complete product grade; read from a 牌号 line in the copy when omitted")
    parser.add_argument("--voice-id", help="TTS voice (default: brand.json voice.voice_id)")
    parser.add_argument("--speed", type=float, help="TTS speed 0.5-2.0 (default: brand.json voice.speed)")
    parser.add_argument("--max-total", type=int)
    parser.add_argument("--max-data-tables", type=int)
    parser.add_argument("--max-per-module", type=int)
    parser.add_argument("--budget-reason", help="Why the default 7-shot budget is raised (required with --max-*)")
    args = parser.parse_args()
    job_dir = args.job.resolve()
    job_path = job_dir / "video-job.json"
    try:
        if not args.approved:
            raise ValueError("只能在用户明确回复“通过/确认/同意”之后运行，并加上 --approved")
        if any(getattr(args, key) is not None for key in ("max_total", "max_data_tables", "max_per_module")) and not args.budget_reason:
            raise ValueError("raising the shot budget needs --budget-reason")
        if job_path.is_file() and media_generation_started(read_job(job_path)):
            raise ValueError(
                "媒体生成已经开始，不能重建 video-job.json；只修改需要改的字段，"
                "或在文案改动并重新审批后先执行 --reset-audio-for-reapproval"
            )
        copy = (job_dir / "copy-for-approval.md").read_text(encoding="utf-8-sig")
        product_grade = (args.product_grade or "").strip()
        if not product_grade:
            match = re.search(r"(?:产品牌号|牌号)\s*[*：:]+\s*\**\s*([^\n*]+)", copy)
            product_grade = match.group(1).strip() if match else ""
        if not product_grade:
            raise ValueError("pass --product-grade or add a '产品牌号：' line to the copy")
        job = build(job_dir, product_grade, args)
        save_whole_job(job_path, job)
        (job_dir / "script.md").write_text(storyboard(job), encoding="utf-8")
        seal_copy_approval(job_path)
        result = complete_job_prompts(job_path)
        result["script"] = str(job_dir / "script.md")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
