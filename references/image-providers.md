# Image Provider Configuration

`scripts/generate_job_images.py` is the only image entry point for this pipeline. Read this file when the image stage reports a configuration error or the user asks which model is used.

## Where settings come from

- The skill's own `.env`, with existing process environment variables taking precedence. Parent-directory or host-workspace `.env` files are never read.
- `IMAGE_PROVIDER` picks the protocol and is never rejected for being unknown:
  - `wan`: native DashScope adapter, renders `1680*1200` directly.
  - `gemini`: native adapter; Gemini has no 14:10 mode, so it asks for `4:3` and cover-crops to exactly `1680*1200` inside the adapter. `IMAGE_QUALITY` = `1K` / `2K` (default) / `4K`.
  - any other name: OpenAI-compatible `POST {base}/images/generations` (Bearer key; `model`, `prompt`, `size`, `n`; reads `data[].url` or `data[].b64_json`). The endpoint renders `IMAGE_SIZE` (default `1680x1200`) and nothing is cropped locally; a non-14:10 answer fails with its real pixel size.
- Without `IMAGE_PROVIDER`: a complete generic configuration wins, then the first native provider whose key is set (wan before gemini).

## Variable lookup order

Each setting, including the API address, resolves as:

1. the generic variable: `IMAGE_MODEL`, `IMAGE_API_KEY`, `IMAGE_API_URL` / `IMAGE_API_BASE`, `IMAGE_SIZE`;
2. names derived from the provider, e.g. `IMAGE_PROVIDER=ark` also reads `ARK_IMAGE_MODEL` / `ARK_MODEL`, `ARK_API_KEY`, `ARK_API_URL` / `ARK_IMAGE_API_URL` / `ARK_API_BASE` / `ARK_BASE_URL`;
3. the native provider's own variable: `WAN_IMAGE_MODEL` + `DASHSCOPE_API_KEY` + `WAN_IMAGE_API_URL`, or `IMAGE_LLM_MODEL` + `GOOGLE_API_KEY` + `GEMINI_API_URL`.

A configured address always wins over the vendor default, so a relay or gateway needs no code change. A service base gets the standard path appended (`/v1beta/models/{model}:generateContent`, `/images/generations`); a URL that already names the method is used as is.

## Defaults and errors

- A native provider without a model variable uses that provider's platform default model and reports `model_source: "platform-default"` (otherwise `"env"`); mention it in the final report when it says `platform-default`.
- Only a missing credential (or, for a generic provider, a missing model or endpoint) stops the stage; the error names every variable it looked for.
- A dropped connection, timeout, HTTP 429 or 5xx is retried once automatically; a rejected prompt (other 4xx) is not.
- On success nothing is written to stderr; the stdout JSON summary is the only success channel.
