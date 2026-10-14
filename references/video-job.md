# video-job.json Schema

One UTF-8 JSON file per video at `output/<job-id>/video-job.json`. `build_video_job.py` generates it from the approved copy and `image-prompts.md`; do not write it by hand, and afterwards change only the specific field a request needs. Policy lives in `SKILL.md`; this file only describes the fields. The example below passes `validate_video_job.py` against a matching article.

```json
{
  "version": 1,
  "job_id": "20260807-example",
  "source": {
    "source_type": "user_provided_text_or_docx",
    "title": "Article title",
    "author": "Author",
    "original_url": null,
    "source_file": "article.docx"
  },
  "copy_approval": {
    "status": "approved",
    "copy_path": "copy-for-approval.md",
    "subtitle_path": "subtitle-for-approval.md",
    "copy_sha256": "<sha256 after human approval>",
    "subtitle_sha256": "<sha256 after human approval>",
    "approved_at": "2026-08-17T00:00:00+00:00"
  },
  "format": {
    "aspect_ratio": "9:16",
    "resolution": "768P"
  },
  "product_grade": "HDPE 6094",
  "planned_total_duration_seconds": 29,
  "shot_budget": {
    "max_total": 7,
    "max_data_tables": 3,
    "max_per_module": 2
  },
  "audio": {
    "voice_id": "female-chengshu",
    "speed": 1.25,
    "product_grade_spoken": "HDPE 六零九四"
  },
  "closing_visuals": [
    {
      "image_prompt": "14:10 horizontal composition, photorealistic plastic supply-chain quality-control scene for fixed closing copy, no visible text, a new image distinct from every article-shot image",
      "image_path": "images/closing-001.png",
      "image_ratio": "14:10",
      "image_status": "planned"
    },
    {
      "image_prompt": "14:10 horizontal composition, photorealistic organized industrial logistics scene for fixed closing copy, no visible text, a new image distinct from every article-shot image and closing-001",
      "image_path": "images/closing-002.png",
      "image_ratio": "14:10",
      "image_status": "planned"
    }
  ],
  "title_candidates": {
    "items": [
      "做瓶盖周转箱的老板看过来：HDPE 6094",
      "HDPE 6094 注塑级牌号一分钟看懂",
      "HDPE 6094 的刚性与流动性怎么兼顾",
      "瓶盖、周转箱选料：HDPE 6094",
      "HDPE 6094 物性参数速览"
    ],
    "recommended": 0
  },
  "shots": [
    {
      "index": 1,
      "duration": 6,
      "module": "introduction",
      "opening_hook_type": "application_address",
      "opening_applications": ["瓶盖", "周转箱"],
      "article_evidence": "HDPE 6094 适用于瓶盖、周转箱等注塑制品",
      "factual_message": "HDPE 6094 是适用于瓶盖、周转箱的注塑级牌号",
      "narration": "做瓶盖、周转箱的老板看过来，HDPE 6094 是一款注塑级牌号。",
      "subtitle": "做瓶盖、周转箱的老板看过来，HDPE 6094 是一款注塑级牌号。",
      "visual_type": "ai_image",
      "image_paths": ["images/001-01.png", "images/001-02.png"],
      "image_prompts": [
        "14:10 horizontal composition, photorealistic rows of finished white bottle caps and stacked turnover boxes in a clean warehouse, no people, no visible text",
        "14:10 horizontal composition, injection molding factory, wide-angle photorealistic factory floor, real industrial production environment, full spatial context, no close-up, no macro shot, no studio scene, no people, no visible text"
      ],
      "image_ratio": "14:10",
      "image_status": "planned",
      "audio_path": "audio/001.mp3",
      "draft_import_status": "planned",
      "status": "planned"
    },
    {
      "index": 2,
      "duration": 6,
      "article_evidence": "熔体流动速率 8 g/10min",
      "factual_message": "HDPE 6094 的熔体流动速率为 8 g/10min",
      "narration": "先看参数，HDPE 6094 的熔体流动速率是8 g/10min。",
      "subtitle": "先看参数，HDPE 6094 的熔体流动速率是8 g/10min。",
      "narration_transition": "从开场的应用场景转入支撑这些应用的物性参数",
      "visual_type": "data_table",
      "table": {
        "title": "HDPE 6094 物性参数",
        "columns": ["项目", "数值", "单位"],
        "rows": [["熔体流动速率", "8", "g/10min"]]
      },
      "image_path": "images/002.png",
      "image_ratio": "14:10",
      "image_status": "planned",
      "audio_path": "audio/002.mp3",
      "draft_import_status": "planned",
      "status": "planned"
    }
  ],
  "draft": {
    "adapter": "jianying-editor",
    "project_name": null,
    "draft_path": null,
    "duration_us": null,
    "status": "not_started"
  },
  "output_path": null
}
```

## Top-level fields you write

| Field | Notes |
|---|---|
| `job_id`, `source` | `source.title`, `author`, `original_url`, `source_file`. A WeChat-sourced job also records `article_id`, `article_index`, `updated_at`, `content_url`. |
| `copy_approval` | `status: "approved"`, `copy_path: "copy-for-approval.md"`, `subtitle_path: "subtitle-for-approval.md"`. `--seal-approval` adds `copy_sha256`, `subtitle_sha256`, `approved_at`; never write those by hand. |
| `product_grade` | The exact grade from the article, never inferred. |
| `planned_total_duration_seconds` | Sum of planned shot `duration` plus 17 for the closing. Informational only. |
| `shot_budget` | Optional; defaults `max_total 7`, `max_data_tables 3`, `max_per_module 2` always apply. Raising any value needs a non-empty `reason`. |
| `audio` | `voice_id` (default `female-chengshu`), `speed` 0.5-2.0 (default 1.25). |
| `title_candidates` | `items`: exactly five strings; `recommended`: zero-based index. |
| `closing_visuals` | Exactly two objects: `image_prompt`, `image_path`, `image_ratio: "14:10"`, `image_status`. Paths distinct from every article image. |
| `shots` | Ordered as the article is written. |
| `format`, `draft`, `output_path` | Optional; the draft stage fills `draft`. |

## Shot fields you write

Every shot: `index` (unique positive integer), `duration` (positive planning estimate, never a gate), `article_evidence` (a paragraph number such as `P3` / `P3-P5`, where a paragraph is one non-empty line of `body_text`, or an exact substring of `article.json` `body_text`), `factual_message`, `narration`, `subtitle` (same text as `narration`; may only normalize display notation), `visual_type` (`ai_image` or `data_table`), `image_ratio: "14:10"`, `audio_path`.

- First shot: `opening_hook_type`; with `"application_address"` also `opening_applications` (1-4 applications from the article) and narration/subtitle starting `做{应用}的老板看过来，`.
- Every later shot: a non-empty `narration_transition`.
- Every non-table shot: `module` = `introduction`, `characteristics` or `applications`, on all non-table shots or none.
- `ai_image`: parallel `image_paths` and `image_prompts`, at least two each, distinct paths. Older jobs with a single `image_path` are still read.
- `data_table`: `table` with `title`, 2-3 `columns`, 1-5 `rows` of non-empty cells, and one `image_path` for the rendered PNG. Every cell containing a number must appear verbatim in the article (spacing and full-width forms ignored). Do not write `tables/*.json`; it is generated from `table`.
- `raw_video_path`, `composited_video_path`, `task_id` stay `null` unless the user asks for shot videos.

## Fields the scripts write (never edit by hand)

- `prepare_tts_text.py`: `tts_text`, `subtitle_segments`, `audio.product_grade_spoken`.
- `generate_job_audio.py`: `audio_hash`, `audio_status`, `asr_subtitle_segments` (the timed segments, whatever their source), `subtitle_timing_source` (`tts_word_timestamps`, `asr_sentence_timestamps` or `punctuation_duration_estimate`), `subtitle_sync_status`, `subtitle_sync_warning`, footer audio fields in `audio`; plus `audio/NNN.timestamps.json` next to each MP3 holding MiniMax's word timestamps for it.
- `jianying_local_draft.py --verify-duration`: `duration_ms`, `actual_total_duration_seconds`, `actual_timeline_audio_hash`, `actual_duration_status`.
- `generate_job_images.py`: `image_status`, `image_request_hash(es)`, `image_provider(s)`, `image_model(s)`, `image_error(s)`, `image_statuses`.
- `prepare_local_assets.py`: `logo_image_path(s)`, `logo_source_hash`, `image_logo_source_hashes`, `logo_status`, `table_source_hash`, `local_asset_error`.
- `jianying_local_draft.py`: `draft`, `draft_import_status`, `draft_subtitle_segments`, `export_cover`, `landscape_cover`, `original_image_path`.

Audio and image stages may run at the same time; each writes only its own fields under a lock (`scripts/job_store.py`), so neither rolls the other back. Per-shot recovery checkpoints live in `audio/.state`, `images/.state` and `.local-assets`.

## What preflight checks

`validate_video_job.py` checks: the sealed approval snapshot and that `subtitle-for-approval.md` lists exactly the final visible subtitles; complete `product_grade` references; evidence traceability and article order; the shot budget and module rules; narration/subtitle equivalence; `tts_text` and `subtitle_segments` matching their generators; image prompt constraints (all problems in one report; fix with `--complete-prompts`); table shape and numeric values; job-relative, distinct image paths; positive durations; the opening hook. It reports `shots` and `planned_images` separately.
