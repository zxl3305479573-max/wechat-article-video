# Jianying Draft Layout

`scripts/jianying_local_draft.py` builds this layout; nothing here is set by hand. Read this file only when the user asks to change the template, or to check a finished draft. Change the constants in the script, not the generated draft.

## Canvas and tracks

- 1080x1920 (9:16) editable draft with the tracks `VideoTrack`, `TemplateOverlay`, `VoiceOver`, `Subtitles`, `HeaderGrade`.
- Every saved draft is self-contained: each image, audio file, template and the footer video is copied into the draft's own `media` folder, and the timeline references only those copies.

## Visuals

- Each logo-marked 14:10 image is its own still clip, `scale_x = scale_y = 0.90`, placed in the clear template window, with Jianying's native background-fill blur at exactly `0.75`. Image files are never pre-blurred.
- An `ai_image` shot plays its images one after another inside the shot's measured narration time, under one shared subtitle timeline.
- `assets/header.jpg` is overlaid unmodified on `TemplateOverlay`.
- Non-14:10 media (only `footer.mp4`) gets an extra aspect-ratio cover multiplier.
- Transitions rotate between native `叠化`, directional wipes, slide and flash, 0.4 s each.

## Text

- Subtitles: one line per timeline segment, Jianying `SourceHanSansCN-Regular`, size `9`, inspector `x=0`, `y=-938` (stored as `-938/1920`), fill `#F2EADD`, outline `#4B4B45`, `auto_wrapping` off, `max_line_width = 1.00`, in the blurred band below the image.
- `HeaderGrade`: the current `product_grade` as an independent editable text segment, `SourceHanSerifCN-Regular`, black, size `26`, inspector `x=0`, `y=-1353` (stored as `-1353/1920`).
- Rendered (non-draft) text uses the bundled SIL Open Font License `assets/fonts/SourceHanSansSC-Regular.otf`.

## Audio and timing

- Every narration MP3 starts at the measured end of the previous one; there are no artificial gaps.
- Adjacent subtitles inside one clip stay continuous: a pause in valid ASR timing extends the previous subtitle, while the next one keeps its measured start.

## Closing

- The fixed closing narration is eight standalone MP3 captions. Whole captions whose measured audio fits inside `footer.mp4` (about 8 s) play over the footer video; the earlier captions (about 9 s) play over the two generated `closing_visuals`, each a new image distinct from every article image.
- `assets/footer.mp4` is imported exactly once, last, with its original audio and the same blur fill and template overlay; it is never looped, duplicated or extended.

## Covers (never on the timeline)

- `final/cover.jpg`: the first shot's first original image (before Logo) in the central region of `assets/header1.jpg`, with the product grade drawn below it.
- `final/landscape-cover.png`: `assets/header2.png` with its example grade replaced by the current `product_grade`.

## Rebuilds

- `--overwrite` replaces the draft folder after copying its timeline files to `output/<job-id>/draft-backups/` (five most recent kept), reported as `draft_backup_path`.
- The script refuses to overwrite while `JianyingPro.exe` is running: an open editor rewrites `draft_content.json` in its own encrypted format on save and would replace the rebuilt timeline. Restore a snapshot only with Jianying closed.
- Changed visuals, Logo, audio or subtitle timing mark the saved draft stale; the next run builds a new draft (`- retry-NNN`) unless `--overwrite` is given.
