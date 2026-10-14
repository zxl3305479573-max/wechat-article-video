import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import generate_job_images as images


IMAGE_ENV_KEYS = (
    "IMAGE_PROVIDER",
    "WAN_IMAGE_MODEL",
    "DASHSCOPE_API_KEY",
    "IMAGE_LLM_MODEL",
    "GOOGLE_API_KEY",
    "IMAGE_QUALITY",
    "IMAGE_MODEL",
    "IMAGE_API_KEY",
    "IMAGE_API_BASE",
    "IMAGE_API_URL",
    "IMAGE_SIZE",
    "WAN_IMAGE_API_URL",
    "GEMINI_API_URL",
    "ARK_API_KEY",
    "ARK_IMAGE_MODEL",
    "ARK_MODEL",
    "ARK_BASE_URL",
    "ARK_API_BASE",
    "ARK_API_URL",
    "DOUBAO_IMAGE_MODEL",
    "DOUBAO_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "OPENAI_COMPATIBLE_IMAGE_MODEL",
    "OPENAI_COMPATIBLE_API_BASE",
)


def image_env(**values: str):
    """Patch the image backend variables in isolation from the ambient shell."""
    cleared = {key: "" for key in IMAGE_ENV_KEYS}
    cleared.update(values)
    return patch.dict(os.environ, cleared, clear=False)


def write_image(path: Path, size: tuple[int, int] = (1680, 1200)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, "white").save(path)


def png_bytes(size: tuple[int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


def gemini_payload(size: tuple[int, int] = (2048, 1536)) -> dict:
    encoded = base64.b64encode(png_bytes(size)).decode("ascii")
    return {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": encoded}}]}}]}


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, content: bytes = b"", text: str = ""):
        self.status_code = status_code
        self.payload = payload
        self.content = content
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class GenerateJobImagesTests(unittest.TestCase):
    def test_visual_entries_includes_ai_shots_and_closing_visuals(self) -> None:
        job = {
            "shots": [
                {"index": 1, "visual_type": "ai_image"},
                {"index": 2, "visual_type": "data_table"},
            ],
            "closing_visuals": [{"image_path": "images/c1.png"}, {"image_path": "images/c2.png"}],
        }
        self.assertEqual([slot for slot, _ in images.visual_entries(job)], ["shot-1", "closing-1", "closing-2"])

    def test_ratio_check_uses_real_pixels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            valid = Path(directory) / "valid.png"
            invalid = Path(directory) / "invalid.png"
            write_image(valid)
            write_image(invalid, (1728, 960))
            self.assertTrue(images.is_valid_14_by_10(valid))
            self.assertFalse(images.is_valid_14_by_10(invalid))

    def test_missing_api_key_fails_before_validation_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory, image_env(IMAGE_PROVIDER="wan", DASHSCOPE_API_KEY=""), patch("validate_video_job.validate") as validate:
            with self.assertRaisesRegex(ValueError, "DASHSCOPE_API_KEY"):
                images.generate(Path(directory), 1)
            validate.assert_not_called()

    def test_non_ratio_response_is_rejected_and_temp_file_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "image.png"
            bad = Path(directory) / "bad.png"
            write_image(bad, (1728, 960))
            backend = images.ImageBackend("wan", "wan2.7-image-pro", "key")
            with patch.object(images.requests, "post", return_value=FakeResponse(payload={"output": {"choices": [{"message": {"content": [{"image": "https://example.test/image"}]}}]}})), patch.object(images.requests, "get", return_value=FakeResponse(content=bad.read_bytes())):
                with self.assertRaisesRegex(ValueError, "non-14:10"):
                    images.generate_one("prompt", output, backend)
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".png.tmp").exists())

    def test_matching_hash_and_real_ratio_skips_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory, image_env(IMAGE_PROVIDER="wan", DASHSCOPE_API_KEY="key", WAN_IMAGE_MODEL="model"), patch("validate_video_job.validate"), patch("validate_video_job.require_verified_duration"):
            job_dir = Path(directory)
            image_path = job_dir / "images/shot.png"
            write_image(image_path)
            prompt = "14:10 horizontal composition"
            job = {"shots": [{"index": 1, "visual_type": "ai_image", "image_prompt": prompt, "image_path": "images/shot.png", "image_request_hash": images.request_hash(prompt, "model")}], "closing_visuals": []}
            (job_dir / "video-job.json").write_text(json.dumps(job), encoding="utf-8")
            with patch.object(images.requests, "post") as post:
                result = images.generate(job_dir, 1)
            post.assert_not_called()
            self.assertEqual(result["skipped"], 1)

    def test_completed_state_recovers_image_without_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory, image_env(IMAGE_PROVIDER="wan", DASHSCOPE_API_KEY="key", WAN_IMAGE_MODEL="model"), patch("validate_video_job.validate"), patch("validate_video_job.require_verified_duration"):
            job_dir = Path(directory)
            image_path = job_dir / "images/shot.png"
            write_image(image_path)
            prompt = "14:10 horizontal composition"
            fingerprint = images.request_hash(prompt, "model")
            job = {"shots": [{"index": 1, "visual_type": "ai_image", "image_prompt": prompt, "image_path": "images/shot.png"}], "closing_visuals": []}
            (job_dir / "video-job.json").write_text(json.dumps(job), encoding="utf-8")
            state_path = job_dir / images.STATE_DIRECTORY / "shot-1.json"
            images.save_json(state_path, {"status": "completed", "image_path": "images/shot.png", "request_hash": fingerprint})
            with patch.object(images.requests, "post") as post:
                result = images.generate(job_dir, 1)
            post.assert_not_called()
            self.assertEqual(result["skipped"], 1)
            saved = json.loads((job_dir / "video-job.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["shots"][0]["image_status"], "completed")
            self.assertEqual(saved["shots"][0]["image_request_hash"], fingerprint)

    def test_main_returns_failure_when_any_image_generation_fails(self) -> None:
        with patch.object(sys, "argv", ["generate_job_images.py", "--job", "job"]), patch.object(
            images, "generate", return_value={"failed": 1}
        ):
            self.assertEqual(images.main(), 1)

    def test_wan_request_uses_the_declared_model_and_native_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "image.png"
            backend = images.ImageBackend("wan", "qwen-image-3.0-pro", "key")
            accepted = FakeResponse(payload={"output": {"choices": [{"message": {"content": [{"image": "https://example.test/image"}]}}]}})
            with patch.object(images.requests, "post", return_value=accepted) as post, patch.object(images.requests, "get", return_value=FakeResponse(content=png_bytes((1680, 1200)))):
                images.generate_one("14:10 horizontal composition", output, backend)
            sent = post.call_args
            self.assertEqual(sent.args[0], images.DEFAULT_WAN_API_URL)
            self.assertEqual(sent.kwargs["json"]["model"], "qwen-image-3.0-pro")
            self.assertEqual(sent.kwargs["json"]["parameters"], {"size": "1680*1200", "n": 1})
            self.assertEqual(sent.kwargs["headers"]["Authorization"], "Bearer key")
            self.assertTrue(images.is_valid_14_by_10(output))

    def test_wan_request_posts_to_the_platform_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "image.png"
            backend = images.ImageBackend("wan", "wan2.7-image-pro", "key", "env", "https://relay.example.test/wan")
            accepted = FakeResponse(payload={"output": {"choices": [{"message": {"content": [{"image": "https://example.test/image"}]}}]}})
            with patch.object(images.requests, "post", return_value=accepted) as post, patch.object(images.requests, "get", return_value=FakeResponse(content=png_bytes((1680, 1200)))):
                images.generate_one("prompt", output, backend)
            self.assertEqual(post.call_args.args[0], "https://relay.example.test/wan")

    def test_generate_writes_the_wan_image_and_records_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory, image_env(IMAGE_PROVIDER="wan", WAN_IMAGE_MODEL="qwen-image-3.0-pro", DASHSCOPE_API_KEY="key"), patch("validate_video_job.validate"), patch("validate_video_job.require_verified_duration"):
            job_dir = Path(directory)
            prompt = "14:10 horizontal composition"
            job = {"shots": [{"index": 1, "visual_type": "ai_image", "image_prompt": prompt, "image_path": "images/shot.png"}], "closing_visuals": []}
            (job_dir / "video-job.json").write_text(json.dumps(job), encoding="utf-8")
            accepted = FakeResponse(payload={"output": {"choices": [{"message": {"content": [{"image": "https://example.test/image"}]}}]}})
            with patch.object(images.requests, "post", return_value=accepted), patch.object(images.requests, "get", return_value=FakeResponse(content=png_bytes((1680, 1200)))):
                result = images.generate(job_dir, 1)
            self.assertEqual((result["provider"], result["model"], result["generated"]), ("wan", "qwen-image-3.0-pro", 1))
            self.assertEqual(result["model_source"], "env")
            saved = json.loads((job_dir / "video-job.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["shots"][0]["image_provider"], "wan")
            self.assertEqual(saved["shots"][0]["image_model"], "qwen-image-3.0-pro")
            self.assertTrue(images.is_valid_14_by_10(job_dir / "images/shot.png"))

    def test_generate_follows_the_platform_default_model_when_unset(self) -> None:
        with tempfile.TemporaryDirectory() as directory, image_env(IMAGE_PROVIDER="wan", DASHSCOPE_API_KEY="key"), contextlib.redirect_stderr(io.StringIO()) as stderr, patch("validate_video_job.validate"), patch("validate_video_job.require_verified_duration"):
            job_dir = Path(directory)
            prompt = "14:10 horizontal composition"
            job = {"shots": [{"index": 1, "visual_type": "ai_image", "image_prompt": prompt, "image_path": "images/shot.png"}], "closing_visuals": []}
            (job_dir / "video-job.json").write_text(json.dumps(job), encoding="utf-8")
            accepted = FakeResponse(payload={"output": {"choices": [{"message": {"content": [{"image": "https://example.test/image"}]}}]}})
            with patch.object(images.requests, "post", return_value=accepted) as post, patch.object(images.requests, "get", return_value=FakeResponse(content=png_bytes((1680, 1200)))):
                result = images.generate(job_dir, 1)
            self.assertEqual((result["provider"], result["model"], result["model_source"]), ("wan", "wan2.7-image-pro", "platform-default"))
            self.assertEqual(post.call_args.kwargs["json"]["model"], "wan2.7-image-pro")
            self.assertEqual(stderr.getvalue(), "")
            saved = json.loads((job_dir / "video-job.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["shots"][0]["image_model"], "wan2.7-image-pro")


class ResolveBackendTests(unittest.TestCase):
    def test_platform_provider_setting_is_honoured_with_its_declared_model(self) -> None:
        with image_env(IMAGE_PROVIDER="WAN", WAN_IMAGE_MODEL="qwen-image-3.0-pro", DASHSCOPE_API_KEY="key", IMAGE_LLM_MODEL="gemini-3-pro-image-preview", GOOGLE_API_KEY="other"):
            backend = images.resolve_backend()
        self.assertEqual((backend.provider, backend.model, backend.api_key), ("wan", "qwen-image-3.0-pro", "key"))
        self.assertEqual(backend.model_source, "env")

    def test_platform_provider_setting_follows_the_platform_default_model(self) -> None:
        with image_env(IMAGE_PROVIDER="wan", DASHSCOPE_API_KEY="key", IMAGE_LLM_MODEL="gemini-3-pro-image-preview", GOOGLE_API_KEY="other"), contextlib.redirect_stderr(io.StringIO()) as stderr:
            backend = images.resolve_backend()
        self.assertEqual((backend.provider, backend.model), ("wan", "wan2.7-image-pro"))
        self.assertEqual(backend.model_source, "platform-default")
        # A successful resolution stays silent on stderr: a notice there makes a
        # working run look like a failed command to the calling wrapper.
        self.assertEqual(stderr.getvalue(), "")

    def test_platform_provider_setting_follows_the_gemini_default_model(self) -> None:
        with image_env(IMAGE_PROVIDER="gemini", GOOGLE_API_KEY="key"), contextlib.redirect_stderr(io.StringIO()) as stderr:
            backend = images.resolve_backend()
        self.assertEqual((backend.provider, backend.model), ("gemini", "gemini-3-pro-image-preview"))
        self.assertEqual(backend.model_source, "platform-default")
        self.assertEqual(stderr.getvalue(), "")

    def test_without_the_platform_setting_the_first_configured_provider_wins(self) -> None:
        with image_env(WAN_IMAGE_MODEL="wan2.7-image-pro", DASHSCOPE_API_KEY="key", IMAGE_LLM_MODEL="gemini-3-pro-image-preview", GOOGLE_API_KEY="other"), contextlib.redirect_stderr(io.StringIO()) as stderr:
            backend = images.resolve_backend()
        self.assertEqual(backend.provider, "wan")
        self.assertEqual(backend.model_source, "env")
        self.assertEqual(stderr.getvalue(), "")

    def test_without_the_platform_setting_gemini_is_used_when_only_its_key_is_set(self) -> None:
        with image_env(GOOGLE_API_KEY="other"):
            backend = images.resolve_backend()
        self.assertEqual((backend.provider, backend.model), ("gemini", "gemini-3-pro-image-preview"))

    def test_unknown_provider_name_is_accepted_when_it_is_configured(self) -> None:
        with image_env(IMAGE_PROVIDER="midjourney", IMAGE_MODEL="mj-v7", IMAGE_API_KEY="key", IMAGE_API_BASE="https://api.example.test/v1"):
            backend = images.resolve_backend()
        self.assertEqual((backend.provider, backend.model, backend.endpoint), ("midjourney", "mj-v7", "https://api.example.test/v1"))
        self.assertEqual(backend.size, "1680x1200")
        self.assertEqual(images.adapter_for(backend.provider), "openai")

    def test_platform_endpoint_overrides_the_builtin_native_address(self) -> None:
        with image_env(IMAGE_PROVIDER="wan", DASHSCOPE_API_KEY="key", WAN_IMAGE_API_URL="https://relay.example.test/wan", GEMINI_API_URL="https://gw.example.test"):
            self.assertEqual(images.resolve_backend().endpoint, "https://relay.example.test/wan")
        with image_env(IMAGE_PROVIDER="gemini", GOOGLE_API_KEY="key", GEMINI_API_URL="https://gw.example.test"):
            self.assertEqual(images.resolve_backend().endpoint, "https://gw.example.test")

    def test_native_endpoint_falls_back_to_the_builtin_address(self) -> None:
        with image_env(IMAGE_PROVIDER="wan", DASHSCOPE_API_KEY="key"):
            self.assertEqual(images.resolve_backend().endpoint, images.DEFAULT_WAN_API_URL)

    def test_unconfigured_provider_names_every_candidate_variable(self) -> None:
        with image_env(IMAGE_PROVIDER="ark"):
            with self.assertRaisesRegex(ValueError, "IMAGE_API_KEY / ARK_API_KEY") as caught:
                images.resolve_backend()
        message = str(caught.exception)
        self.assertIn("IMAGE_MODEL / ARK_IMAGE_MODEL / ARK_MODEL", message)
        self.assertIn("IMAGE_API_URL / IMAGE_API_BASE / ARK_API_URL / ARK_IMAGE_API_URL / ARK_API_BASE / ARK_BASE_URL", message)

    def test_platform_provider_without_its_api_key_names_the_key_and_alternative(self) -> None:
        with image_env(IMAGE_PROVIDER="wan", IMAGE_LLM_MODEL="gemini-3-pro-image-preview", GOOGLE_API_KEY="other"):
            with self.assertRaisesRegex(ValueError, "IMAGE_API_KEY, WAN_API_KEY, DASHSCOPE_API_KEY") as caught:
                images.resolve_backend()
        self.assertIn("configured instead: gemini", str(caught.exception))

    def test_no_image_key_is_reported_instead_of_defaulted(self) -> None:
        with image_env():
            with self.assertRaisesRegex(ValueError, "DASHSCOPE_API_KEY, GOOGLE_API_KEY") as caught:
                images.resolve_backend()
        self.assertIn("IMAGE_API_KEY + IMAGE_MODEL + IMAGE_API_URL + IMAGE_API_BASE", str(caught.exception))


class GenericProviderTests(unittest.TestCase):
    """Any non-native provider is called through the OpenAI-compatible contract."""

    def test_generic_credentials_are_derived_from_the_provider_name(self) -> None:
        with image_env(IMAGE_PROVIDER="ark", ARK_API_KEY="key", ARK_IMAGE_MODEL="doubao-seedream-4-0", ARK_BASE_URL="https://ark.example.test/api/v3"):
            backend = images.resolve_backend()
        self.assertEqual((backend.provider, backend.model, backend.api_key), ("ark", "doubao-seedream-4-0", "key"))
        self.assertEqual(backend.endpoint, "https://ark.example.test/api/v3")

    def test_generic_variables_win_over_provider_derived_names(self) -> None:
        with image_env(IMAGE_PROVIDER="ark", ARK_API_KEY="derived", ARK_IMAGE_MODEL="derived-model", ARK_BASE_URL="https://ark.example.test", IMAGE_API_KEY="generic", IMAGE_MODEL="generic-model", IMAGE_API_BASE="https://generic.example.test/v1", IMAGE_SIZE="1400x1000"):
            backend = images.resolve_backend()
        self.assertEqual((backend.model, backend.api_key), ("generic-model", "generic"))
        self.assertEqual(backend.endpoint, "https://generic.example.test/v1")
        self.assertEqual(backend.size, "1400x1000")

    def test_full_images_url_is_used_verbatim(self) -> None:
        with image_env(IMAGE_PROVIDER="gw", IMAGE_MODEL="m", IMAGE_API_KEY="k", IMAGE_API_URL="https://gw.example.test/v1/images/generations"):
            backend = images.resolve_backend()
        self.assertEqual(images.images_endpoint(backend.endpoint), "https://gw.example.test/v1/images/generations")
        self.assertEqual(images.images_endpoint("https://gw.example.test/v1"), "https://gw.example.test/v1/images/generations")

    def test_generic_provider_without_the_platform_setting_is_detected(self) -> None:
        with image_env(IMAGE_MODEL="doubao-seedream-4-0", IMAGE_API_KEY="key", IMAGE_API_BASE="https://ark.example.test/api/v3"):
            backend = images.resolve_backend()
        self.assertEqual(backend.provider, "openai-compatible")
        self.assertEqual(backend.model, "doubao-seedream-4-0")

    def test_request_posts_the_openai_payload_and_downloads_the_result(self) -> None:
        backend = images.ImageBackend("ark", "doubao-seedream-4-0", "key", "env", "https://ark.example.test/api/v3/", "1680x1200")
        accepted = FakeResponse(payload={"data": [{"url": "https://cdn.example.test/image.png"}]})
        image = png_bytes((1680, 1200))
        with patch.object(images.requests, "post", return_value=accepted) as post, patch.object(images.requests, "get", return_value=FakeResponse(content=image)) as get:
            data = images.request_openai_image("14:10 horizontal composition", backend)
        self.assertEqual(data, image)
        sent = post.call_args
        self.assertEqual(sent.args[0], "https://ark.example.test/api/v3/images/generations")
        self.assertEqual(sent.kwargs["headers"]["Authorization"], "Bearer key")
        self.assertEqual(sent.kwargs["json"], {"model": "doubao-seedream-4-0", "prompt": "14:10 horizontal composition", "n": 1, "size": "1680x1200"})
        self.assertEqual(get.call_args.args[0], "https://cdn.example.test/image.png")

    def test_inline_base64_results_need_no_download(self) -> None:
        backend = images.ImageBackend("ark", "model", "key", "env", "https://ark.example.test/api/v3", "1680x1200")
        encoded = base64.b64encode(png_bytes((1680, 1200))).decode("ascii")
        with patch.object(images.requests, "post", return_value=FakeResponse(payload={"data": [{"b64_json": encoded}]})), patch.object(images.requests, "get") as get:
            data = images.request_openai_image("prompt", backend)
        get.assert_not_called()
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.size, (1680, 1200))

    def test_generic_error_status_surfaces_the_provider_message(self) -> None:
        backend = images.ImageBackend("ark", "model", "key", "env", "https://ark.example.test/api/v3", "1680x1200")
        failure = FakeResponse(status_code=400, payload={"error": {"message": "size is not supported"}})
        with patch.object(images.requests, "post", return_value=failure):
            with self.assertRaisesRegex(ValueError, "size is not supported"):
                images.request_openai_image("prompt", backend)

    def test_empty_result_is_reported_instead_of_writing_nothing(self) -> None:
        backend = images.ImageBackend("ark", "model", "key", "env", "https://ark.example.test/api/v3", "1680x1200")
        with patch.object(images.requests, "post", return_value=FakeResponse(payload={"data": []})):
            with self.assertRaisesRegex(ValueError, "did not return an image"):
                images.request_openai_image("prompt", backend)

    def test_generic_image_is_never_normalized_locally(self) -> None:
        """The endpoint renders the requested size; a different one must fail, not be cropped."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "image.png"
            backend = images.ImageBackend("ark", "model", "key", "env", "https://ark.example.test/api/v3", "1680x1200")
            square = FakeResponse(payload={"data": [{"url": "https://cdn.example.test/image.png"}]})
            with patch.object(images.requests, "post", return_value=square), patch.object(images.requests, "get", return_value=FakeResponse(content=png_bytes((1024, 1024)))):
                with self.assertRaisesRegex(ValueError, r"non-14:10 image \(1024x1024\).*IMAGE_SIZE"):
                    images.generate_one("prompt", output, backend)
            self.assertFalse(output.exists())

    def test_generate_writes_a_generic_provider_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory, image_env(IMAGE_PROVIDER="doubao", DOUBAO_IMAGE_MODEL="doubao-seedream-4-0", DOUBAO_API_KEY="key", IMAGE_API_BASE="https://ark.example.test/api/v3"), patch("validate_video_job.validate"), patch("validate_video_job.require_verified_duration"):
            job_dir = Path(directory)
            prompt = "14:10 horizontal composition"
            job = {"shots": [{"index": 1, "visual_type": "ai_image", "image_prompt": prompt, "image_path": "images/shot.png"}], "closing_visuals": []}
            (job_dir / "video-job.json").write_text(json.dumps(job), encoding="utf-8")
            accepted = FakeResponse(payload={"data": [{"url": "https://cdn.example.test/image.png"}]})
            with patch.object(images.requests, "post", return_value=accepted) as post, patch.object(images.requests, "get", return_value=FakeResponse(content=png_bytes((1680, 1200)))):
                result = images.generate(job_dir, 1)
            self.assertEqual((result["provider"], result["model"], result["model_source"], result["generated"]), ("doubao", "doubao-seedream-4-0", "env", 1))
            self.assertEqual(post.call_args.args[0], "https://ark.example.test/api/v3/images/generations")
            saved = json.loads((job_dir / "video-job.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["shots"][0]["image_provider"], "doubao")
            self.assertEqual(saved["shots"][0]["image_model"], "doubao-seedream-4-0")


class GeminiBackendTests(unittest.TestCase):
    backend = images.ImageBackend("gemini", "gemini-3-pro-image-preview", "key")

    def test_image_is_normalized_to_the_exact_contract_size(self) -> None:
        with image_env(), patch.object(images.requests, "post", return_value=FakeResponse(payload=gemini_payload())) as post:
            data = images.request_gemini_image("14:10 horizontal composition", self.backend)
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["generationConfig"]["imageConfig"], {"aspectRatio": "4:3", "imageSize": "2K"})
        # The API contract requires both modalities; TEXT parts are skipped by the parser.
        self.assertEqual(sent["generationConfig"]["responseModalities"], ["TEXT", "IMAGE"])
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "key")
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual((image.width, image.height), (1680, 1200))

    def test_gemini_generate_content_url_is_built_from_the_platform_base(self) -> None:
        self.assertEqual(
            images.gemini_endpoint("https://gw.example.test/", "gemini-3-pro-image-preview"),
            "https://gw.example.test/v1beta/models/gemini-3-pro-image-preview:generateContent",
        )
        self.assertEqual(
            images.gemini_endpoint("https://gw.example.test/v1beta/models/x:generateContent", "ignored"),
            "https://gw.example.test/v1beta/models/x:generateContent",
        )
        self.assertEqual(
            images.gemini_endpoint("", "m"),
            images.DEFAULT_GEMINI_API_BASE + "/v1beta/models/m:generateContent",
        )

    def test_declared_image_quality_is_forwarded(self) -> None:
        with image_env(IMAGE_QUALITY="4k"), patch.object(images.requests, "post", return_value=FakeResponse(payload=gemini_payload())) as post:
            images.request_gemini_image("prompt", self.backend)
        self.assertEqual(post.call_args.kwargs["json"]["generationConfig"]["imageConfig"]["imageSize"], "4K")

    def test_geometry_rejection_is_retried_without_the_image_config(self) -> None:
        rejected = FakeResponse(status_code=400, payload={"error": {"message": 'Unknown name "imageConfig"'}})
        with image_env(), patch.object(images.requests, "post", side_effect=[rejected, FakeResponse(payload=gemini_payload())]) as post:
            images.request_gemini_image("prompt", self.backend)
        self.assertEqual(post.call_count, 2)
        self.assertNotIn("imageConfig", post.call_args_list[1].kwargs["json"]["generationConfig"])

    def test_prompt_rejection_is_not_retried_and_surfaces_the_reason(self) -> None:
        rejected = FakeResponse(status_code=400, payload={"error": {"message": "prompt violates policy"}}, text="prompt violates policy")
        with image_env(), patch.object(images.requests, "post", return_value=rejected) as post:
            with self.assertRaisesRegex(ValueError, "prompt violates policy"):
                images.request_gemini_image("prompt", self.backend)
        self.assertEqual(post.call_count, 1)

    def test_blocked_response_reports_the_block_reason(self) -> None:
        blocked = FakeResponse(payload={"promptFeedback": {"blockReason": "SAFETY"}})
        with image_env(), patch.object(images.requests, "post", return_value=blocked):
            with self.assertRaisesRegex(ValueError, "SAFETY"):
                images.request_gemini_image("prompt", self.backend)

    def test_server_error_surfaces_the_provider_message(self) -> None:
        failure = FakeResponse(status_code=503, payload={"error": {"message": "model overloaded"}})
        with image_env(), patch.object(images.requests, "post", return_value=failure):
            with self.assertRaisesRegex(ValueError, "model overloaded"):
                images.request_gemini_image("prompt", self.backend)


class GenerateWithGeminiTests(unittest.TestCase):
    def test_generate_records_the_detected_provider_and_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory, image_env(IMAGE_LLM_MODEL="gemini-3-pro-image-preview", GOOGLE_API_KEY="key"), patch("validate_video_job.validate"), patch("validate_video_job.require_verified_duration"):
            job_dir = Path(directory)
            prompt = "14:10 horizontal composition"
            job = {"shots": [{"index": 1, "visual_type": "ai_image", "image_prompt": prompt, "image_path": "images/shot.png"}], "closing_visuals": []}
            (job_dir / "video-job.json").write_text(json.dumps(job), encoding="utf-8")
            with patch.object(images.requests, "post", return_value=FakeResponse(payload=gemini_payload())):
                result = images.generate(job_dir, 1)
            self.assertEqual((result["provider"], result["model"]), ("gemini", "gemini-3-pro-image-preview"))
            self.assertEqual(result["generated"], 1)
            saved = json.loads((job_dir / "video-job.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["shots"][0]["image_provider"], "gemini")
            self.assertEqual(saved["shots"][0]["image_model"], "gemini-3-pro-image-preview")
            self.assertTrue(images.is_valid_14_by_10(job_dir / "images/shot.png"))


if __name__ == "__main__":
    unittest.main()
