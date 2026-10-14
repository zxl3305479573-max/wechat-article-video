#!/usr/bin/env python3
"""Generate pending 14:10 job visuals through the image model the environment declares."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import io
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from PIL import Image


DEFAULT_WAN_API_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
DEFAULT_GEMINI_API_BASE = "https://generativelanguage.googleapis.com"
GEMINI_GENERATE_PATH = "/v1beta/models/{model}:generateContent"
OPENAI_IMAGES_PATH = "/images/generations"
RATIO = "14:10"
IMAGE_WIDTH = 1680
IMAGE_HEIGHT = 1200
WAN_SIZE = f"{IMAGE_WIDTH}*{IMAGE_HEIGHT}"
# OpenAI-compatible endpoints spell a pixel size as `WIDTHxHEIGHT`, and they render
# the requested size directly, so this adapter needs no local normalization pass.
DEFAULT_IMAGE_SIZE = f"{IMAGE_WIDTH}x{IMAGE_HEIGHT}"
# Gemini has no native 14:10 output. Ask for the supported ratio that is closest
# to it (4:3 loses the least area to the crop) and cover-crop to the exact
# contract size inside this adapter, so every source visual written into the job
# is already 14:10 and the later Logo stage still never resizes or crops.
GEMINI_RATIO = "4:3"
GEMINI_IMAGE_SIZES = ("1K", "2K", "4K")
GEMINI_DEFAULT_IMAGE_SIZE = "2K"
STATE_DIRECTORY = "images/.state"


@dataclass(frozen=True)
class ProviderSpec:
    """One provider with a native adapter, its variables, and its platform defaults."""

    name: str
    adapter: str
    model_var: str
    api_key_var: str
    default_model: str
    endpoint_var: str
    default_endpoint: str


# `IMAGE_PROVIDER` is the platform's own setting (the host resolves the same
# variable in workflow/media.py), so it is followed first: these two names keep
# their native protocol, and *every other name* uses the generic OpenAI-compatible
# `/images/generations` contract instead of being rejected, because the platform is
# not limited to wan or gemini. Without `IMAGE_PROVIDER`, the first provider whose
# key is declared wins (wan first: this pipeline's prompts and 14:10 contract were
# built on its native 14:10 output). `default_model` mirrors the host platform's
# fallback for that provider, and `default_endpoint` is only a fallback too: the
# API address the platform configures always wins, so a gateway or relay can be
# pointed at without touching this script.
NATIVE_PROVIDERS = {
    "wan": ProviderSpec(
        "wan", "wan", "WAN_IMAGE_MODEL", "DASHSCOPE_API_KEY", "wan2.7-image-pro",
        "WAN_IMAGE_API_URL", DEFAULT_WAN_API_URL,
    ),
    "gemini": ProviderSpec(
        "gemini", "gemini", "IMAGE_LLM_MODEL", "GOOGLE_API_KEY", "gemini-3-pro-image-preview",
        "GEMINI_API_URL", DEFAULT_GEMINI_API_BASE,
    ),
}
GENERIC_PROVIDER = "openai-compatible"
MODEL_SUFFIXES = ("IMAGE_MODEL", "MODEL")
KEY_SUFFIXES = ("API_KEY",)
ENDPOINT_SUFFIXES = ("API_URL", "IMAGE_API_URL", "API_BASE", "BASE_URL")
GENERIC_MODEL_VARS = ("IMAGE_MODEL",)
GENERIC_KEY_VARS = ("IMAGE_API_KEY",)
GENERIC_ENDPOINT_VARS = ("IMAGE_API_URL", "IMAGE_API_BASE")


@dataclass(frozen=True)
class ImageBackend:
    """A resolved image backend: provider, model, credential, endpoint, and their origin.

    `endpoint` is whatever the platform configured (or the provider's built-in
    address): the full request URL for wan, a service base for gemini and for
    OpenAI-compatible endpoints, which append their own path.
    """

    provider: str
    model: str
    api_key: str
    model_source: str = "env"
    endpoint: str = ""
    size: str = ""


def save_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_state(job_dir: Path, slot: str) -> dict[str, Any]:
    path = job_dir / STATE_DIRECTORY / f"{slot}.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_job(job_path: Path) -> dict[str, Any]:
    value = json.loads(job_path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("video-job.json root must be an object")
    return value


def resolve_inside(job_dir: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    path = (job_dir / value).resolve()
    try:
        path.relative_to(job_dir)
    except ValueError as error:
        raise ValueError(f"{label} must stay inside the job directory") from error
    return path


def visual_entries(job: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    entries: list[tuple[str, dict[str, Any]]] = []
    for shot in job.get("shots", []):
        if not isinstance(shot, dict) or shot.get("visual_type") != "ai_image":
            continue
        paths = shot.get("image_paths")
        prompts = shot.get("image_prompts")
        if isinstance(paths, list) and isinstance(prompts, list) and len(paths) == len(prompts):
            for variant, (path, prompt) in enumerate(zip(paths, prompts), start=1):
                visual = dict(shot)
                visual["image_path"] = path
                visual["image_prompt"] = prompt
                visual["__parent_index"] = int(shot["index"])
                visual["__variant"] = variant - 1
                entries.append((f"shot-{int(shot['index'])}-{variant}", visual))
        else:
            entries.append((f"shot-{int(shot['index'])}", shot))
    for position, visual in enumerate(job.get("closing_visuals", []), start=1):
        if isinstance(visual, dict):
            entries.append((f"closing-{position}", visual))
    return entries


def request_hash(prompt: str, model: str) -> str:
    payload = json.dumps({"prompt": prompt, "model": model, "ratio": RATIO}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_valid_14_by_10(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            return image.width > 0 and image.width * 10 == image.height * 14
    except (OSError, ValueError):
        return False


def env_value(name: str) -> str:
    """Read one environment variable as a trimmed string."""
    return str(os.getenv(name) or "").strip()


def provider_prefix(provider: str) -> str:
    """`openai-compatible` -> `OPENAI_COMPATIBLE`, for deriving variable names."""
    return re.sub(r"[^0-9A-Za-z]+", "_", provider).strip("_").upper()


def var_candidates(provider: str, suffixes: tuple[str, ...], generics: tuple[str, ...], native: str = "") -> list[str]:
    """Variable names to try, in order: generic, provider-derived, then the native name.

    This is what makes a provider the skill has never seen work: `IMAGE_PROVIDER=ark`
    is satisfied by `IMAGE_API_KEY` / `IMAGE_MODEL`, or by its own names
    (`ARK_API_KEY`, `ARK_IMAGE_MODEL`, `ARK_MODEL`, `ARK_API_BASE`, ...).
    """
    prefix = provider_prefix(provider)
    names = [*generics, *[f"{prefix}_{suffix}" for suffix in suffixes if prefix], native]
    return [name for name in names if name]


def lookup_setting(candidates: list[str]) -> tuple[str, str]:
    """Return the first declared `(variable name, value)` among the candidates."""
    for name in candidates:
        value = env_value(name)
        if value:
            return name, value
    return "", ""


def native_backend(spec: ProviderSpec) -> ImageBackend:
    """Resolve one natively supported provider from its variables.

    The provider's model variable overrides the model; when it is absent the
    platform's own default model for that provider is used, and the returned
    `model_source` reports that ("env" versus "platform-default"). The API address
    the platform configures also wins over the provider's built-in endpoint. All of
    that is reported through returned fields rather than a stderr notice: this
    script's stdout is the machine-readable summary, and a notice on stderr makes a
    successful run look like a failed command under wrappers that treat any stderr
    output as an error. Only the credential is required, because it cannot be
    defaulted.
    """
    key_vars = var_candidates(spec.name, KEY_SUFFIXES, GENERIC_KEY_VARS, spec.api_key_var)
    model_vars = var_candidates(spec.name, MODEL_SUFFIXES, GENERIC_MODEL_VARS, spec.model_var)
    endpoint_vars = var_candidates(spec.name, ENDPOINT_SUFFIXES, GENERIC_ENDPOINT_VARS, spec.endpoint_var)
    api_key = lookup_setting(key_vars)[1]
    if not api_key:
        raise ValueError(
            f"IMAGE_PROVIDER={spec.name} requires an API key; declare one of {', '.join(key_vars)} "
            "in this skill's .env or the host environment" + configured_detail()
        )
    model = lookup_setting(model_vars)[1]
    endpoint = lookup_setting(endpoint_vars)[1] or spec.default_endpoint
    if model:
        return ImageBackend(spec.name, model, api_key, "env", endpoint)
    return ImageBackend(spec.name, spec.default_model, api_key, "platform-default", endpoint)


def generic_backend(provider: str) -> ImageBackend:
    """Resolve any other provider through the OpenAI-compatible image contract."""
    model_vars = var_candidates(provider, MODEL_SUFFIXES, GENERIC_MODEL_VARS)
    key_vars = var_candidates(provider, KEY_SUFFIXES, GENERIC_KEY_VARS)
    endpoint_vars = var_candidates(provider, ENDPOINT_SUFFIXES, GENERIC_ENDPOINT_VARS)
    api_key = lookup_setting(key_vars)[1]
    model = lookup_setting(model_vars)[1]
    endpoint = lookup_setting(endpoint_vars)[1]
    missing = [
        " / ".join(names)
        for names, value in ((key_vars, api_key), (model_vars, model), (endpoint_vars, endpoint))
        if not value
    ]
    if missing:
        raise ValueError(
            f"IMAGE_PROVIDER={provider} is called through the OpenAI-compatible /images/generations "
            "contract and needs " + ", ".join(missing) + " in this skill's .env or the host environment"
        )
    return ImageBackend(
        provider,
        model,
        api_key,
        "env",
        endpoint,
        env_value("IMAGE_SIZE") or DEFAULT_IMAGE_SIZE,
    )


def configured_detail() -> str:
    """Name what *is* configured, so a failure points at a usable alternative."""
    declared = [
        spec.name
        for spec in NATIVE_PROVIDERS.values()
        if lookup_setting(var_candidates(spec.name, KEY_SUFFIXES, (), spec.api_key_var))[0]
    ]
    if lookup_setting(list(GENERIC_KEY_VARS))[0] and lookup_setting(list(GENERIC_ENDPOINT_VARS))[0]:
        declared.append(GENERIC_PROVIDER)
    return f"; configured instead: {', '.join(declared)}" if declared else "; no image credential is configured"


def adapter_for(provider: str) -> str:
    """The protocol used for a provider: its native adapter, else OpenAI-compatible."""
    spec = NATIVE_PROVIDERS.get(provider)
    return spec.adapter if spec else "openai"


def resolve_backend() -> ImageBackend:
    """Select the image backend from the environment, following the platform's setting.

    `IMAGE_PROVIDER` decides the protocol and is never rejected for being unknown:
    `wan` and `gemini` keep their native adapters, anything else uses the generic
    OpenAI-compatible contract. Each setting resolves as the generic `IMAGE_*`
    variable first, then names derived from the provider, then the provider's own
    variable — including the endpoint, so the platform's API address always wins.
    Without `IMAGE_PROVIDER`, a fully declared generic configuration wins, then the
    first natively supported provider whose key is declared (wan, gemini).
    """
    requested = env_value("IMAGE_PROVIDER").lower()
    if requested in NATIVE_PROVIDERS:
        return native_backend(NATIVE_PROVIDERS[requested])
    if requested:
        return generic_backend(requested)

    generic_ready = all(
        lookup_setting(list(names))[1]
        for names in (GENERIC_MODEL_VARS, GENERIC_KEY_VARS, GENERIC_ENDPOINT_VARS)
    )
    if generic_ready:
        return generic_backend(GENERIC_PROVIDER)
    for spec in NATIVE_PROVIDERS.values():
        key_vars = var_candidates(spec.name, KEY_SUFFIXES, GENERIC_KEY_VARS, spec.api_key_var)
        if lookup_setting(key_vars)[0]:
            # Deterministic and documented: wan before gemini, and this script keeps
            # its success path off stderr (see `native_backend`).
            return native_backend(spec)
    raise ValueError(
        "no image model found in the environment; set IMAGE_PROVIDER to follow the platform's "
        "provider setting, declare a provider key ("
        + ", ".join(spec.api_key_var for spec in NATIVE_PROVIDERS.values())
        + "), or declare the generic "
        + " + ".join((*GENERIC_KEY_VARS, *GENERIC_MODEL_VARS, *GENERIC_ENDPOINT_VARS))
        + " for any OpenAI-compatible endpoint"
    )


def gemini_image_size() -> str:
    """Read the Gemini image size, mirroring the host project's IMAGE_QUALITY.

    An unset or unrecognized value falls back to the default without a stderr
    notice, for the same reason `native_backend` keeps the success path off stderr.
    """
    raw = env_value("IMAGE_QUALITY").upper()
    if raw in GEMINI_IMAGE_SIZES:
        return raw
    return GEMINI_DEFAULT_IMAGE_SIZE


def error_text(payload: Any) -> str:
    """Read a provider's own error text out of a decoded JSON body."""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict) and str(error.get("message") or "").strip():
        return str(error["message"]).strip()
    if isinstance(error, str) and error.strip():
        return error.strip()
    if isinstance(payload.get("message"), str) and payload["message"].strip():
        return payload["message"].strip()
    return ""


def api_error_message(response: Any) -> str:
    """Read the provider's own error text so a failure names the real cause."""
    try:
        payload = response.json()
    except Exception:  # a non-JSON error body must still produce a readable message
        payload = None
    text = error_text(payload)
    if text:
        return text
    raw = str(getattr(response, "text", "") or "").strip()
    if raw:
        return raw[:300]
    return f"HTTP {getattr(response, 'status_code', 'unknown')}"


def mentions_image_config(message: str) -> bool:
    """True when an error blames the geometry block rather than the prompt."""
    lowered = message.lower()
    return any(term in lowered for term in ("imageconfig", "aspect", "imagesize", "imageresolution"))


def inline_image_bytes(result: Any) -> bytes:
    """Extract the first inline image from a Gemini generateContent response."""
    texts: list[str] = []
    candidates = result.get("candidates") if isinstance(result, dict) else None
    for candidate in candidates or []:
        content = candidate.get("content") if isinstance(candidate, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        for part in parts or []:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            data = inline.get("data") if isinstance(inline, dict) else None
            if isinstance(data, str) and data.strip():
                return base64.b64decode(data)
            if isinstance(part.get("text"), str) and part["text"].strip():
                texts.append(part["text"].strip())
    feedback = result.get("promptFeedback") if isinstance(result, dict) else None
    if isinstance(feedback, dict) and feedback.get("blockReason"):
        reason = f"blocked: {feedback['blockReason']}"
    elif texts:
        reason = texts[0][:200]
    else:
        reason = "unknown error"
    raise ValueError(f"Gemini API did not return an image: {reason}")


def normalize_to_14_by_10(data: bytes) -> bytes:
    """Cover-crop and resize a provider image to exactly 1680x1200."""
    with Image.open(io.BytesIO(data)) as source:
        image = source.convert("RGB")
        width, height = image.size
        if width * IMAGE_HEIGHT > height * IMAGE_WIDTH:
            cropped_width = max(1, round(height * IMAGE_WIDTH / IMAGE_HEIGHT))
            left = (width - cropped_width) // 2
            image = image.crop((left, 0, left + cropped_width, height))
        elif width * IMAGE_HEIGHT < height * IMAGE_WIDTH:
            cropped_height = max(1, round(width * IMAGE_HEIGHT / IMAGE_WIDTH))
            top = (height - cropped_height) // 2
            image = image.crop((0, top, width, top + cropped_height))
        if image.size != (IMAGE_WIDTH, IMAGE_HEIGHT):
            image = image.resize((IMAGE_WIDTH, IMAGE_HEIGHT), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()


def request_wan_image(prompt: str, backend: ImageBackend) -> bytes:
    """Ask the configured Wan endpoint for a native 1680x1200 image.

    The URL is the platform's setting when it declares one, so a relay or a
    DashScope-compatible gateway works without code changes.
    """
    payload = {
        "model": backend.model,
        "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
        "parameters": {"size": WAN_SIZE, "n": 1},
    }
    response = requests.post(
        backend.endpoint or DEFAULT_WAN_API_URL,
        headers={"Authorization": f"Bearer {backend.api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=180,
    )
    response.raise_for_status()
    result = response.json()
    content = result.get("output", {}).get("choices", [{}])[0].get("message", {}).get("content", [])
    image_url = next((item.get("image") for item in content if isinstance(item, dict) and item.get("image")), None)
    if not image_url:
        raise ValueError(f"Wan API did not return an image: {result.get('message') or result.get('code') or 'unknown error'}")
    download = requests.get(image_url, timeout=180)
    download.raise_for_status()
    return download.content


def request_gemini_image(prompt: str, backend: ImageBackend) -> bytes:
    """Ask Gemini for an image and normalize the result to the 14:10 contract size.

    `aspectRatio` is a fixed enum on this API (1:1, 2:3, 3:2, 3:4, 4:3, 4:5, 5:4,
    9:16, 16:9, 21:9) and the only size control is the 1K/2K/4K tier, so 14:10
    cannot be requested at all; the closest ratio is asked for and cropped here.
    `responseModalities` must carry both TEXT and IMAGE, per the API contract. The
    endpoint is the platform's setting when it declares one: a service base gets the
    standard `:generateContent` path appended, and a URL that already names the
    method is used verbatim.
    """
    url = gemini_endpoint(backend.endpoint, backend.model)
    headers = {"x-goog-api-key": backend.api_key, "Content-Type": "application/json"}
    generation_config: dict[str, Any] = {
        "responseModalities": ["TEXT", "IMAGE"],
        "imageConfig": {"aspectRatio": GEMINI_RATIO, "imageSize": gemini_image_size()},
    }
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": generation_config}
    response = requests.post(url, headers=headers, json=payload, timeout=180)
    if response.status_code == 400 and mentions_image_config(api_error_message(response)):
        # Some image models reject the geometry block outright; retry once with the
        # provider default instead of failing the whole stage.
        generation_config.pop("imageConfig", None)
        response = requests.post(url, headers=headers, json=payload, timeout=180)
    if response.status_code >= 400:
        raise ValueError(
            f"Gemini API rejected the image request for {backend.model}: {api_error_message(response)}"
        )
    return normalize_to_14_by_10(inline_image_bytes(response.json()))


def openai_image_bytes(result: Any) -> bytes:
    """Read the first image from an OpenAI-compatible `data` array.

    Endpoints answer either with a URL to download or with inline base64, so both
    forms are accepted; anything else is reported with the provider's own message.
    """
    items = result.get("data") if isinstance(result, dict) else None
    for item in items or []:
        if not isinstance(item, dict):
            continue
        inline = item.get("b64_json")
        if isinstance(inline, str) and inline.strip():
            return base64.b64decode(inline)
        image_url = item.get("url")
        if isinstance(image_url, str) and image_url.strip():
            download = requests.get(image_url, timeout=180)
            download.raise_for_status()
            return download.content
    raise ValueError(
        "image API did not return an image: "
        + (error_text(result) or "no data[].url or data[].b64_json in the response")
    )


def gemini_endpoint(api_base: str, model: str) -> str:
    """Build the generateContent URL from the platform's base, or keep a full URL."""
    base = (api_base or DEFAULT_GEMINI_API_BASE).rstrip("/")
    if ":generateContent" in base:
        return base
    return base + GEMINI_GENERATE_PATH.format(model=model)


def images_endpoint(api_base: str) -> str:
    """Build the OpenAI-compatible image URL, or keep a URL that already names it."""
    base = api_base.rstrip("/")
    if base.endswith(OPENAI_IMAGES_PATH):
        return base
    return base + OPENAI_IMAGES_PATH


def request_openai_image(prompt: str, backend: ImageBackend) -> bytes:
    """Call any OpenAI-compatible image endpoint, asking it for 14:10 pixels directly.

    The endpoint renders the requested size, so no local normalization happens here:
    the bytes are written as returned and the caller verifies the real pixels.
    """
    url = images_endpoint(backend.endpoint)
    payload = {
        "model": backend.model,
        "prompt": prompt,
        "n": 1,
        "size": backend.size or DEFAULT_IMAGE_SIZE,
    }
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {backend.api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=180,
    )
    if response.status_code >= 400:
        raise ValueError(
            f"{backend.provider} image API rejected the request for {backend.model}: {api_error_message(response)}"
        )
    return openai_image_bytes(response.json())


def request_image(prompt: str, backend: ImageBackend) -> bytes:
    """Render one image with the protocol the resolved provider uses."""
    adapter = adapter_for(backend.provider)
    if adapter == "wan":
        return request_wan_image(prompt, backend)
    if adapter == "gemini":
        return request_gemini_image(prompt, backend)
    return request_openai_image(prompt, backend)


def actual_pixels(path: Path) -> str:
    """Describe a written image's real size, for a precise rejection message."""
    try:
        with Image.open(path) as image:
            return f"{image.width}x{image.height}"
    except (OSError, ValueError):
        return "unreadable pixels"


def generate_one(prompt: str, output_path: Path, backend: ImageBackend) -> None:
    image_bytes = request_image(prompt, backend)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_bytes(image_bytes)
    if not is_valid_14_by_10(temporary):
        pixels = actual_pixels(temporary)
        temporary.unlink(missing_ok=True)
        hint = (
            f"set IMAGE_SIZE (default {DEFAULT_IMAGE_SIZE}) to a 14:10 size this endpoint accepts"
            if adapter_for(backend.provider) == "openai"
            else "this provider has to return 14:10 pixels"
        )
        raise ValueError(
            f"{backend.provider} API returned a non-14:10 image ({pixels}) for {output_path.name}; {hint}"
        )
    os.replace(temporary, output_path)


def generate(job_dir: Path, max_concurrency: int) -> dict[str, Any]:
    # Load only this skill's project configuration; never walk into parent workspaces.
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    backend = resolve_backend()
    provider = backend.provider
    model = backend.model
    job_path = job_dir / "video-job.json"
    from validate_video_job import require_verified_duration, validate

    validate(job_path)
    job = load_job(job_path)
    require_verified_duration(job)
    work: list[tuple[str, dict[str, Any], Path, str]] = []
    slots = visual_entries(job)
    owners: dict[str, str] = {}
    for owner_slot, owner_visual in slots:
        owners[request_hash(str(owner_visual.get("image_prompt") or "").strip(), model)] = owner_slot
    skipped = 0
    changed = False
    for slot, visual in visual_entries(job):
        prompt = str(visual.get("image_prompt") or "").strip()
        path = resolve_inside(job_dir, visual.get("image_path"), f"{slot} image_path")
        fingerprint = request_hash(prompt, model)
        state = read_state(job_dir, slot)
        state_path = job_dir / STATE_DIRECTORY / f"{slot}.json"
        state_path_value = resolve_inside(job_dir, state.get("image_path"), f"{slot} state image_path") if state.get("image_path") else path
        state_complete = (
            state.get("status") == "completed"
            and state.get("request_hash") == fingerprint
            and state_path_value == path
            and is_valid_14_by_10(path)
        )
        if state_complete:
            visual.update({
                "image_status": "completed",
                "image_request_hash": fingerprint,
                "image_provider": provider,
                "image_model": model,
            })
            visual.pop("image_error", None)
            skipped += 1
            changed = True
            continue
        if visual.get("image_request_hash") == fingerprint and is_valid_14_by_10(path):
            skipped += 1
            continue
        recorded = state.get("request_hash")
        if isinstance(recorded, str) and recorded != fingerprint and recorded in owners:
            raise ValueError(
                f"image state for {slot} belongs to {owners[recorded]}. The shots were reordered "
                "without re-keying their images; rebuild the job instead of regenerating paid images."
            )
        visual["image_status"] = "generating"
        work.append((slot, visual, path, fingerprint))

    completed = failed = 0
    results: dict[str, tuple[str, dict[str, Any]]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrency) as pool:
        futures = {pool.submit(generate_one, str(visual["image_prompt"]), path, backend): (slot, visual, path, fingerprint) for slot, visual, path, fingerprint in work}
        for future in concurrent.futures.as_completed(futures):
            slot, visual, path, fingerprint = futures[future]
            state_path = job_dir / STATE_DIRECTORY / f"{slot}.json"
            try:
                future.result()
                save_json(state_path, {"status": "completed", "image_path": str(path.relative_to(job_dir)), "request_hash": fingerprint})
                results[slot] = ("completed", {
                    "image_request_hash": fingerprint,
                    "image_provider": provider,
                    "image_model": model,
                })
                completed += 1
            except Exception as error:
                save_json(state_path, {"status": "failed", "error": str(error)})
                results[slot] = ("failed", {"image_error": str(error)})
                failed += 1

    # Merge all provider results once, in stable storyboard order. Isolated
    # state files above are the recovery checkpoints if this merge is
    # interrupted.
    visual_by_slot = dict(slots)
    for slot, (status, fields) in results.items():
        visual = visual_by_slot[slot]
        visual["image_status"] = status
        visual.update(fields)
        if status == "completed":
            visual.pop("image_error", None)
        else:
            visual["image_error"] = fields.get("image_error", "image generation failed")
    # Persist per-variant provider state back into the parent shot. The
    # expanded visual dictionaries are intentionally transient task records;
    # video-job.json remains the single resumable source of truth.
    for _slot, visual in slots:
        if "__variant" not in visual:
            continue
        parent = next(
            (shot for shot in job.get("shots", [])
             if isinstance(shot, dict) and int(shot.get("index", 0)) == int(visual["__parent_index"])),
            None,
        )
        if parent is None:
            continue
        variant = int(visual["__variant"])
        size = len(parent.get("image_paths", []))
        fields = {
            "image_statuses": ["planned"] * size,
            "image_request_hashes": [None] * size,
            "image_providers": [None] * size,
            "image_models": [None] * size,
            "image_errors": [None] * size,
        }
        for key, default in fields.items():
            current = parent.setdefault(key, default)
            while len(current) < size:
                current.append(None if default and default[0] is None else "planned")
        parent["image_statuses"][variant] = visual.get("image_status", "planned")
        parent["image_request_hashes"][variant] = visual.get("image_request_hash")
        parent["image_providers"][variant] = visual.get("image_provider")
        parent["image_models"][variant] = visual.get("image_model")
        parent["image_errors"][variant] = visual.get("image_error")
        parent["image_status"] = "completed" if all(item == "completed" for item in parent["image_statuses"]) else (
            "failed" if any(item == "failed" for item in parent["image_statuses"]) else "pending"
        )
        parent.pop("image_error", None)
    if changed or results:
        save_json(job_path, job)
    return {"generated": completed, "failed": failed, "skipped": skipped, "provider": provider, "model": model, "model_source": backend.model_source, "ratio": RATIO}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--max-concurrency", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.max_concurrency <= 3:
        raise ValueError("max concurrency must be between 1 and 3")
    result = generate(args.job.resolve(), args.max_concurrency)
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
