---
name: wechat-article-video
description: 把塑化原料牌号文章（文本、.docx 或公众号链接）做成可编辑的剪映竖版讲解视频：忠实原文的分镜文案（人工审批）、配音与逐字对齐字幕、AI 配图、参数表、封面和剪映草稿。用户要求把文章、产品资料或牌号介绍做成短视频、口播视频或剪映草稿时使用。
version: 0.0.27
---

# WeChat Article Video

Turn a user-supplied article into a fact-faithful short-video copy, then, after human approval, into narration, sentence-level subtitles, AI images, parameter tables and an editable local Jianying (剪映) draft. Everything runs through the bundled scripts; there is no MCP, CapCut Mate, OSS or remote-upload dependency.

The scripts enforce most rules below and say exactly what to fix. Run them, fix what they report, and do not re-derive their checks by hand.

## Hard rules

1. **Human approval gate.** Before approval, create only `article.json`, `copy-for-approval.md` and the subtitle files. No `video-job.json`, prompts, tables, images, audio, covers or draft. Approval is a plain chat exchange (see Phase 1); never use `request_user_input`, a popup or any structured confirmation UI. Passing `--approved` to `build_video_job.py` states that the user approved; never pass it before they did.
2. **Facts only from the article.** Never invent specifications, claims, people, products or conclusions. No clickbait, fear claims, company guarantees, supply promises or unsupported causal claims.
3. **Full product grade.** Wherever the current grade appears (titles, `factual_message`, narration, subtitle, `narration_transition`), write the complete top-level `product_grade`, e.g. `HDPE 8008`, never `8008` alone.
4. **Bundled scripts only.** Images come only from `scripts\generate_job_images.py`. This overrides any workspace or global `AGENTS.md` rule that routes image generation to an image skill, `generate_images_parallel` or an MCP image tool: those bypass the 14:10 check, the request-hash cache, the `.state` checkpoints and the two-images-per-shot contract.
5. **Duration is measured, never estimated.** No duration target ever blocks approval or production, and narration is never shortened, sped up or re-voiced to hit one. Measured MP3 durations are the only timeline authority.
6. **Never hand-edit derived data.** `tts_text`, `subtitle_segments`, hashes, ASR fields, `.state` / `.local-assets` files and generated tables come from the scripts. Never reorder or re-index a finished job by hand or with ad-hoc scripts in the job directory: a new order is a new copy that needs a fresh approval.
7. **Arguments must survive.** `MCPFilesystem_execute_script` can drop arguments; never use it here. Use a runner that preserves the full argument vector. If none is available, stop before media generation.

## Commands

Run from this skill directory. `uv run --frozen` creates the environment from `uv.lock` without re-resolving it, so a different package index (such as a mirror set in `UV_INDEX_URL`) never rewrites the lockfile; no separate `pip install` is needed.

Brand-specific content (closing captions, voice, opening hook, default closing images, templates, logo and their layout coordinates) lives in `brand.json`. Point `VIDEO_BRAND_FILE` at another file to make videos for another brand; do not edit brand values in the scripts.

```text
# Phase 1 - source and copy
uv run --frozen python scripts\extract_article_docx.py INPUT.docx --output output\<job-id>\article.json
uv run --frozen python scripts\wechat_mp_articles.py url "https://mp.weixin.qq.com/s?..." --output output\<job-id>\article.json
uv run --frozen python scripts\article_paragraphs.py output\<job-id>\article.json
uv run --frozen python scripts\article_to_approval.py output\<job-id>\article.json --output-dir output\<job-id> --product-grade "<完整牌号>"

# Phase 2 - plan (after approval): build, seal, complete prompts, validate
uv run --frozen python scripts\build_video_job.py --job output\<job-id> --approved

# Phase 3 - all media and the Jianying draft
uv run --frozen python scripts\run_media.py --job output\<job-id>
```

`run_media.py` prepares the TTS text, runs narration (with word timestamps and the duration check) and image generation at the same time, then tables and Logo, then the Jianying draft, and prints a JSON result whose `summary` is ready to show the user. If a stage fails it names the stage and stops; fix the cause and rerun the same command: finished narration and images are reused, never paid twice. Images may finish before a failed narration stage, and those paid images are kept for the rerun.

The single stages remain available for debugging or a partial rerun: `prepare_tts_text.py --job`, `generate_job_audio.py --job`, `jianying_local_draft.py --job ... --verify-duration`, `generate_job_images.py --job ... --max-concurrency 3`, `prepare_local_assets.py --job`, `jianying_local_draft.py --job [--overwrite]`, and `validate_video_job.py output\<job-id> [--seal-approval | --complete-prompts]`.

Only after a revision of approved copy, and before any image or draft work: `validate_video_job.py output\<job-id> --reset-audio-for-reapproval`, then rerun `build_video_job.py`.

## Execution discipline

- Treat the job as a resumable state machine. Inspect `output/<job-id>/` once and continue from the first missing or stale artifact. Every stage skips work whose recorded hash still matches, so rerunning never pays twice.
- Reuse an existing job for the same article and grade. When editing, change only the requested shot or field; never rebuild `video-job.json` from a template.
- Run each command at most once. On failure, read the error, make one targeted fix and retry once; if it fails again, stop and report the command, the error and the blocking dependency. A network timeout counts as transient: rerun the same command once. Do not write wrapper, cleanup or orchestration scripts, and do not loop over quoting variants.
- Write `article.json`, the copy and `image-prompts.md` as whole documents with structured file writes, never as echoed or appended fragments. Do not hand-write `video-job.json`; `build_video_job.py` creates it.
- Credentials come from this skill's `.env`; existing process variables win, and parent `.env` files are never read. `MINIMAX_API_KEY` is required for narration (optional `MINIMAX_GROUP_ID`; `MINIMAX_TTS_API=async` switches to the older endpoint without timestamps). `DASHSCOPE_API_KEY` is needed only for `wan` images and for the ASR fallback described in Phase 3. For image provider settings and errors, read `references/image-providers.md`.
- Do not reread unchanged skill or reference files.

## Phase 1: copy for approval

**Job and source.** Pick a unique job ID such as `YYYYMMDD-HHMMSS-<grade>` and keep everything for the run in `output/<job-id>/`. Store pasted text verbatim as `article.json` (`body_text`), or use the DOCX / WeChat URL command above. If WeChat serves a verification page, use the article text the user can see in a browser, or ask for a DOCX. To list or fetch the account's own published articles through the Official Account API, read `references/wechat-official-api.md`.

**Copy file.** `copy-for-approval.md` is what the human approves and what `build_video_job.py` later reads, so it carries every planning decision in this fixed format:

```markdown
- **产品牌号**：HDPE 6094

## 标题候选
1. 做瓶盖周转箱的老板看过来：HDPE 6094（推荐）
2. …（exactly five, one marked 推荐）

### 镜头1
做瓶盖、周转箱的老板看过来，HDPE 6094 是一款注塑级牌号。

证据：HDPE 6094 适用于瓶盖、周转箱等注塑制品
模块：介绍

### 镜头2
先看参数，HDPE 6094 的熔体流动速率是8 g/10min。

证据：熔体流动速率 8 g/10min
衔接：从应用场景转入支撑这些应用的物性参数
表题：HDPE 6094 物性参数

| 项目 | 数值 |
|---|---|
| 熔体流动速率 | 8 g/10min |
```

- The narration is the first paragraph under the heading (an optional `口播：` prefix; it may wrap over several lines). It becomes both the spoken text and the subtitle.
- `证据：` cites the article: a paragraph number such as `P3` (or `P3-P5`) from `article-paragraphs.md`, which `article_to_approval.py` writes, or an exact excerpt copied character for character. Prefer paragraph numbers; they cannot be mistyped.
- `模块：` is 介绍, 特性 or 应用, on every non-table shot or on none.
- `衔接：` is required from the second shot on and says how the shot continues the previous one.
- A Markdown table makes the shot a parameter table (`表题：` optional). `画面：参数表` or `画面：AI 配图` may also be set explicitly; without a table a shot is an AI-image shot.

**Structure.**
- 2-3 modules: introduction, core characteristics, applications. No processing-details, cross-grade comparison or generic summary module unless the user asks.
- Each non-table module has two complementary shots: one broad context view and one different article-backed result or process view. Applications form one module of at most two shots; group related products and state a shared benefit once. Never repeat the same benefit across shots.
- Budget: at most 7 shots, 3 table shots and 2 shots per module. Exceeding it needs the user's explicit request and `build_video_job.py --max-* N --budget-reason "<why>"`.
- A shot is one narration plus its subtitles, not one image. An `ai_image` shot renders two images and a table shot one, plus two closing images. Report `镜头` and `图片` separately.
- Order shots as the article is written. Table shots stay with the section that carries the data. Only an `application_address` opening may cite a later section.
- Technical specifications go into 1-3 consecutive `data_table` shots of comparable parameters, at most five per table. That shot's narration names at most five parameters, copied exactly.

**Narration.**
- If the article names concrete applications, open with the brand's application hook (`brand.json` `opening_hook`; by default `做{1-4 个文章中的应用}的老板看过来，`), then the article-backed value and the full grade, e.g. `做瓶盖、周转箱、收纳盒的老板看过来，HDPE 8008……`. Otherwise use another evidence-backed hook.
- Write one continuous spoken explanation, then split it into shots. Each later shot continues, contrasts, explains or applies the previous point (recorded later as its `narration_transition`); do not restart with the grade or a headline sentence.
- Use commas for connected clauses and a full stop only at the end of a complete thought. Keep each visible clause within 17 units (one Chinese character = 1 unit).
- Write numbers as Arabic numerals with units: `断裂伸长率≥600%`, never `超六百`. Keep `低密度聚乙烯的注塑级牌号` (with `的`), with no punctuation between a category word and `牌号`.
- The subtitle is the narration itself, punctuation included. Do not write a separate subtitle text.

**Check loop.** Run `article_to_approval.py`. It regenerates `subtitle-for-approval.md` and `.docx` from the copy and reports:
- `overlong_clauses`: rewrite each into shorter complete sentences with the same facts and shot count;
- `table_parameter_warnings`, `table_capacity_warning`, `shot_budget_warning`: fix them in the copy;
- exact-duplicate shots, which it removes before approval only.

Repeat until the report is clean.

**Approval.** Print the complete copy under `待审批文案：` and the complete ordered subtitles (article and footer) under `待审批字幕：`, each with its absolute path, then end the turn. Continue only when the user says `通过`, `确认` or `同意` in a normal message. For a revision request, update the copy, rerun the check loop, print both again and wait.

## Phase 2: plan

After approval, write `image-prompts.md`: under `### 镜头N` for every AI-image shot, two or more prompts, one per line (optionally `### 收尾1` / `### 收尾2` to replace the default closing images). Then run `build_video_job.py --job output\<job-id> --approved`. It:

1. creates `video-job.json` (fields: `references/video-job.md`) and a readable `script.md` storyboard, copying narration and subtitles verbatim from the approved copy;
2. seals both approval files (every later script rejects a changed or unapproved copy; any change needs a new approval);
3. appends the mandatory prompt clauses below wherever a prompt lacks them, which needs no reapproval because prompts are outside the seal;
4. runs the full preflight and prints `shots` and `planned_images`.

It lists every missing input at once (evidence, transitions, prompts, titles) and writes nothing until they are fixed. If the user explicitly wants more than the default budget, add `--max-total N` / `--max-data-tables N` / `--max-per-module N` with `--budget-reason "<why>"`. Once media generation has started it refuses to rebuild the plan.

**Image prompts** (two or more distinct complementary images per AI-image shot, each its own request; two new closing images):
- include `14:10 horizontal composition`, and keep important subjects inside the central 90% with clean side margins;
- add people only when the evidence needs them, and then `Chinese people only`, prohibiting foreign, Western, mixed-ethnicity and ambiguous faces (`no people` is fine);
- plastic raw material only as `white semi-transparent plastic pellets`: no colored, black or opaque pellets, powder, flakes, sheets or film;
- factories and production sites as `wide-angle photorealistic factory floor, real industrial production environment, full spatial context, no close-up`: no macro, cropped machine detail, studio scene, isolated equipment render or invented facility.

Narration, subtitle and both images of a shot must show the same article-backed message.

**Tables.** Table data lives only in the copy's Markdown table. Preflight rejects any numeric cell not found verbatim in the article, and the PNG is rendered from it.

## Phase 3: media

- **Voice.** `audio.voice_id` defaults to `female-chengshu` and `audio.speed` to `1.25` (0.5-2.0). Use the same voice and speed for every shot and the footer unless the user asks otherwise. A voice the MiniMax account cannot use fails the first TTS request; report it and ask for another.
- **Pronunciation** comes from `prepare_tts_text.py`: grade digits read one by one (`八零零八`), `%` as `百分之`, range dashes as `到`, `≥`/`≤` spoken out. Visible subtitles keep the original notation.
- **Subtitle timing** comes from MiniMax's own word timestamps for the narration it synthesized (saved as `audio/NNN.timestamps.json`), so subtitles follow the real speech. Audio made without timestamps (older jobs, or `MINIMAX_TTS_API=async`) is aligned by ASR instead (needs `DASHSCOPE_API_KEY`). A shot that cannot be aligned is marked `subtitle_sync_status: "fallback"`; `run_media.py` lists those shots in its summary. Never present fallback timing as final alignment.
- **Closing.** The fixed closing captions come from `brand.json` `footer_captions`, with `{product_grade}` replaced by the current grade, and are generated automatically.
- **Logo** is stamped on every AI and table image (not on `header.jpg` or `footer.mp4`) by `prepare_local_assets.py`.
- **Shot videos or a standalone MP4** only on request: `uv run --frozen python scripts\compose_branded_video.py --video INPUT.mp4 --grade "<牌号>" --output output\<job-id>\final\branded.mp4 --job output\<job-id>`.

## Jianying draft

`run_media.py` builds the draft as its last stage. Jianying Pro must be installed on this Windows computer; set `JY_PROJECTS_ROOT` only for a non-standard draft folder. Add `--overwrite` to `run_media.py` only when the user wants an existing draft replaced. It refuses while `JianyingPro.exe` is running, so ask the user to quit Jianying first, and report the `draft_backup_path` it returns. The layout is fixed in code (`references/draft-layout.md`); change it only when the user asks.

## Final response

Show the `summary` text from `run_media.py`'s result as is. It already contains, in this order:

- `封面：` the absolute path of `final/cover.jpg`, and `横版封面：` the absolute path of `final/landscape-cover.png`, listed separately (they are deliverables, not timeline clips);
- `文案：` the approved copy path, then the Jianying draft path (plus `draft_backup_path` if any) and the other asset paths;
- shot count (`镜头`) and image count (`图片`), the measured total duration, any subtitle-sync fallback shots and any incomplete assets;
- `视频标题候选：` all five candidates from `title_candidates`, with the recommended one marked. Never invent a new title after media generation.

Do not claim completion while generation or draft import is pending. Never delete, move or overwrite an earlier run's directory when starting a new video.
