---
name: wechat-article-video
description: 根据文章文本或提供的 .docx 生成忠实原文的短视频文案、分镜、AI 图片、配音、句级字幕和可编辑的剪映草稿。用户要求将文章制作成固定课程模板的视频时使用。
version: 0.0.26
---

# WeChat Article Video

Turn a user-supplied article into a fact-faithful short-video copy, then, after human approval, into narration, sentence-level subtitles, AI images, parameter tables and an editable local Jianying (剪映) draft. Everything runs through the bundled scripts; there is no MCP, CapCut Mate, OSS or remote-upload dependency.

The scripts enforce most rules below and say exactly what to fix. Run them, fix what they report, and do not re-derive their checks by hand.

## Hard rules

1. **Human approval gate.** Before approval, create only `article.json`, `copy-for-approval.md` and the subtitle files. No `video-job.json`, prompts, tables, images, audio, covers or draft. Approval is a plain chat exchange (see Phase 1); never use `request_user_input`, a popup or any structured confirmation UI.
2. **Facts only from the article.** Never invent specifications, claims, people, products or conclusions. No clickbait, fear claims, company guarantees, supply promises or unsupported causal claims.
3. **Full product grade.** Wherever the current grade appears (titles, `factual_message`, narration, subtitle, `narration_transition`), write the complete top-level `product_grade`, e.g. `HDPE 8008`, never `8008` alone.
4. **Bundled scripts only.** Images come only from `scripts\generate_job_images.py`. This overrides any workspace or global `AGENTS.md` rule that routes image generation to an image skill, `generate_images_parallel` or an MCP image tool: those bypass the 14:10 check, the request-hash cache, the `.state` checkpoints and the two-images-per-shot contract.
5. **Duration is measured, never estimated.** No duration target ever blocks approval or production, and narration is never shortened, sped up or re-voiced to hit one. Measured MP3 durations are the only timeline authority.
6. **Never hand-edit derived data.** `tts_text`, `subtitle_segments`, hashes, ASR fields, `.state` / `.local-assets` files and generated tables come from the scripts. Never reorder or re-index a finished job by hand or with ad-hoc scripts in the job directory: a new order is a new copy that needs a fresh approval.
7. **Arguments must survive.** `MCPFilesystem_execute_script` can drop arguments; never use it here. Use a runner that preserves the full argument vector. If none is available, stop before media generation.

## Commands

Run from this skill directory. `uv run` creates the environment from `pyproject.toml` / `uv.lock`; no separate `pip install` is needed.

```text
# Phase 1 - source and copy
uv run python scripts\extract_article_docx.py INPUT.docx --output output\<job-id>\article.json
uv run python scripts\wechat_mp_articles.py url "https://mp.weixin.qq.com/s?..." --output output\<job-id>\article.json
uv run python scripts\article_to_approval.py output\<job-id>\article.json --output-dir output\<job-id> --product-grade "<完整牌号>"

# Phase 2 - plan (after approval)
uv run python scripts\validate_video_job.py output\<job-id> --seal-approval
uv run python scripts\validate_video_job.py output\<job-id>
uv run python scripts\validate_video_job.py output\<job-id> --complete-prompts

# Phase 3 - media
uv run python scripts\prepare_tts_text.py --job output\<job-id>
uv run python scripts\generate_job_audio.py --job output\<job-id>                # track A
uv run python scripts\jianying_local_draft.py --job output\<job-id> --verify-duration   # track A, after audio
uv run python scripts\generate_job_images.py --job output\<job-id> --max-concurrency 3  # track B
uv run python scripts\prepare_local_assets.py --job output\<job-id>              # after both tracks
uv run python scripts\jianying_local_draft.py --job output\<job-id>
```

Tracks A and B may run at the same time, for example as two background commands; running them one after the other is also fine. Each stage writes only its own fields of `video-job.json` under a lock, so neither undoes the other. Running images before narration means a failed TTS stage can leave already-paid images; they are reused on the next run.

Only after a revision of approved copy, and before any image or draft work: `validate_video_job.py output\<job-id> --reset-audio-for-reapproval`, then reseal.

## Execution discipline

- Treat the job as a resumable state machine. Inspect `output/<job-id>/` once and continue from the first missing or stale artifact. Every stage skips work whose recorded hash still matches, so rerunning never pays twice.
- Reuse an existing job for the same article and grade. When editing, change only the requested shot or field; never rebuild `video-job.json` from a template.
- Run each command at most once. On failure, read the error, make one targeted fix and retry once; if it fails again, stop and report the command, the error and the blocking dependency. A network timeout counts as transient: rerun the same command once. Do not write wrapper, cleanup or orchestration scripts, and do not loop over quoting variants.
- Write `article.json`, the copy and `video-job.json` as whole documents with structured file writes, never as echoed or appended fragments.
- Credentials (`MINIMAX_API_KEY`, `DASHSCOPE_API_KEY`, image provider keys, WeChat settings) come from this skill's `.env`; existing process variables win, and parent `.env` files are never read. For image provider settings and errors, read `references/image-providers.md`.
- Do not reread unchanged skill or reference files.

## Phase 1: copy for approval

**Job and source.** Pick a unique job ID such as `YYYYMMDD-HHMMSS-<grade>` and keep everything for the run in `output/<job-id>/`. Store pasted text verbatim as `article.json` (`body_text`), or use the DOCX / WeChat URL command above. If WeChat serves a verification page, use the article text the user can see in a browser, or ask for a DOCX. To list or fetch the account's own published articles through the Official Account API, read `references/wechat-official-api.md`.

**Copy file.** `copy-for-approval.md` holds the proposed title, the product grade, five fact-faithful title candidates (one marked recommended), and each shot under `### 镜头N` with its narration as the first paragraph (an optional `口播：` prefix; the paragraph may wrap over several lines), followed by its article evidence.

**Structure.**
- 2-3 modules: introduction, core characteristics, applications. No processing-details, cross-grade comparison or generic summary module unless the user asks.
- Each non-table module has two complementary shots: one broad context view and one different article-backed result or process view. Applications form one module of at most two shots; group related products and state a shared benefit once. Never repeat the same benefit across shots.
- Budget: at most 7 shots, 3 table shots and 2 shots per module. Exceeding it needs `shot_budget` with a `reason` in `video-job.json`.
- A shot is one narration plus its subtitles, not one image. An `ai_image` shot renders two images and a table shot one, plus two closing images. Report `镜头` and `图片` separately.
- Order shots as the article is written. Table shots stay with the section that carries the data. Only an `application_address` opening may cite a later section.
- Technical specifications go into 1-3 consecutive `data_table` shots of comparable parameters, at most five per table. That shot's narration names at most five parameters, copied exactly.

**Narration.**
- If the article names concrete applications, open with `做{1-4 个文章中的应用}的老板看过来，`, then the article-backed value and the full grade, e.g. `做瓶盖、周转箱、收纳盒的老板看过来，HDPE 8008……`. Otherwise use another evidence-backed hook.
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

After approval, write `script.md` and `video-job.json` (fields: `references/video-job.md`) with `copy_approval.status: "approved"`, then:

1. `--seal-approval` snapshots both approval files. Every later script rejects a changed or unapproved copy, and any change to them needs a new approval and a new seal.
2. Run `validate_video_job.py`, the preflight gate. Fix the plan as a whole until it passes.
3. If it reports image prompt problems, run `--complete-prompts` once. It appends the mandatory clauses below only where they are missing, leaves compliant prompts unchanged and revalidates. Prompts are outside the sealed approval, so no reapproval is needed.

**Image prompts** (`image_prompts`, two or more distinct complementary images per `ai_image` shot, each its own request; two new `closing_visuals`):
- include `14:10 horizontal composition`, and keep important subjects inside the central 90% with clean side margins;
- add people only when the evidence needs them, and then `Chinese people only`, prohibiting foreign, Western, mixed-ethnicity and ambiguous faces (`no people` is fine);
- plastic raw material only as `white semi-transparent plastic pellets`: no colored, black or opaque pellets, powder, flakes, sheets or film;
- factories and production sites as `wide-angle photorealistic factory floor, real industrial production environment, full spatial context, no close-up`: no macro, cropped machine detail, studio scene, isolated equipment render or invented facility.

Narration, subtitle and both images of a shot must show the same article-backed message.

**Tables.** Write table data only into the shot's `table`. Preflight rejects any numeric cell not found verbatim in the article, and `prepare_local_assets.py` renders the PNG from that object.

## Phase 3: media

- **Voice.** `audio.voice_id` defaults to `female-chengshu` and `audio.speed` to `1.25` (0.5-2.0). Use the same voice and speed for every shot and the footer unless the user asks otherwise. A voice the MiniMax account cannot use fails the first TTS request; report it and ask for another.
- **Pronunciation** comes from `prepare_tts_text.py`: grade digits read one by one (`八零零八`), `%` as `百分之`, range dashes as `到`, `≥`/`≤` spoken out. Visible subtitles keep the original notation.
- **Subtitle timing** comes from `generate_job_audio.py`, which uses ASR word timestamps. A shot it cannot align is marked `subtitle_sync_status: "fallback"`; report those shots and never present fallback timing as final alignment.
- **Closing.** The fixed closing captions are generated automatically with the current grade: `买优质塑化原料`、`就找兆企供应链`、`兆企供应链管理有限公司深耕塑化行业`、`货源稳定 物流高效`、`找兆企采购{product_grade}`、`让生产更顺畅`、`本视频禁止用于ai模型训练`、`兆企供应链拥有最终解释权`.
- **Logo** is stamped on every AI and table image (not on `header.jpg` or `footer.mp4`) by `prepare_local_assets.py`.
- **Shot videos or a standalone MP4** only on request: `uv run python scripts\compose_branded_video.py --video INPUT.mp4 --grade "<牌号>" --output output\<job-id>\final\branded.mp4 --job output\<job-id>`.

## Jianying draft

Run `jianying_local_draft.py` once, after audio is verified and images and local assets are complete. Jianying Pro must be installed on this Windows computer; set `JY_PROJECTS_ROOT` only for a non-standard draft folder. Add `--overwrite` only when the user wants an existing draft replaced. It refuses while `JianyingPro.exe` is running, so ask the user to quit Jianying first, and return the reported `draft_backup_path`. The layout is fixed in code (`references/draft-layout.md`); change it only when the user asks.

## Final response

- `封面：` the absolute path of `final/cover.jpg`, and `横版封面：` the absolute path of `final/landscape-cover.png`, listed separately (they are deliverables, not timeline clips);
- `文案：` the approved copy path, then the Jianying draft path (plus `draft_backup_path` if any) and the other asset paths;
- shot count (`镜头`) and image count (`图片`), the measured total duration, any subtitle-sync fallback shots and any incomplete assets;
- `视频标题候选：` all five candidates from `title_candidates`, with the recommended one marked. Never invent a new title after media generation.

Do not claim completion while generation or draft import is pending. Never delete, move or overwrite an earlier run's directory when starting a new video.
