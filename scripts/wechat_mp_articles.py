#!/usr/bin/env python3
"""Read published articles from the authenticated WeChat Official Account."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv


def load_workmate_environment() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


load_workmate_environment()


TOKEN_ERROR_CODES = {40001, 40014, 42001}
PUBLIC_ARTICLE_HOSTS = {"mp.weixin.qq.com"}
WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def configure_utf8_console() -> None:
    """Avoid treating a valid article as a failed fetch on GBK Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError):
            pass


configure_utf8_console()


class WeChatAPIError(RuntimeError):
    def __init__(self, errcode: int | str, errmsg: str):
        self.errcode = errcode
        self.errmsg = errmsg
        detail = f"WeChat API error {errcode}: {errmsg}"
        if str(errcode) == "40164":
            detail += "; add the reported public IP to the Official Account API whitelist, or use the 'url' command for a public article page"
        super().__init__(detail)


class ArticleHTMLParser(HTMLParser):
    BLOCK_TAGS = {"p", "div", "section", "article", "li", "h1", "h2", "h3", "h4", "br"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.images: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "img":
            values = dict(attrs)
            url = values.get("data-src") or values.get("src")
            if url and url.startswith(("http://", "https://")) and url not in self.images:
                self.images.append(url)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
            return
        if not self.ignored_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        value = html.unescape("".join(self.parts)).replace("\u00a0", " ")
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r"\n\s*\n+", "\n", value)
        return value.strip()


def parse_article_html(value: str) -> tuple[str, list[str]]:
    parser = ArticleHTMLParser()
    parser.feed(value or "")
    parser.close()
    return parser.text(), parser.images


class PublicArticlePageParser(HTMLParser):
    """Extract the visible WeChat article body and Open Graph metadata."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.metadata: dict[str, str] = {}
        self.body_html: list[str] = []
        self.body_depth = 0
        self.in_title = False
        self.title_parts: list[str] = []

    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = dict(attrs)
        if tag == "meta":
            key = values.get("property") or values.get("name")
            content = values.get("content")
            if key and content:
                self.metadata[key.lower()] = content
        if tag == "title":
            self.in_title = True
        if self.body_depth or values.get("id") == "js_content":
            self.body_html.append(self.get_starttag_text())
            if tag not in self.VOID_TAGS:
                self.body_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.body_depth:
            self.body_html.append(self.get_starttag_text())

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self.in_title = False
        if self.body_depth:
            self.body_html.append(f"</{tag}>")
            self.body_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self.body_depth:
            self.body_html.append(data)


def fetch_public_article(
    url: str,
    timeout: float | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in PUBLIC_ARTICLE_HOSTS:
        raise ValueError("url must be an https://mp.weixin.qq.com article URL")
    requester = session or requests
    response = requester.get(
        url,
        headers=WEB_HEADERS,
        timeout=timeout or float(os.getenv("WECHAT_MP_HTTP_TIMEOUT", "20")),
    )
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    final_host = (urlparse(response.url).hostname or "").lower()
    if final_host not in PUBLIC_ARTICLE_HOSTS:
        raise RuntimeError("The WeChat article request redirected to an unsupported host")
    parser = PublicArticlePageParser()
    parser.feed(response.text)
    parser.close()
    content_html = "".join(parser.body_html).strip()
    body_text, image_urls = parse_article_html(content_html)
    if not body_text:
        content_type = response.headers.get("Content-Type", "unknown")
        has_body_node = 'id="js_content"' in response.text or "id='js_content'" in response.text
        raise RuntimeError(
            "The public WeChat page did not expose readable article content "
            f"(HTTP {response.status_code}, content-type {content_type}, js_content={has_body_node}); "
            "the page may require a browser verification or restrict automated access"
        )
    page_title = html.unescape("".join(parser.title_parts)).strip()
    return {
        "article_id": None,
        "article_index": 0,
        "title": html.unescape(parser.metadata.get("og:title", "") or page_title).strip(),
        "author": html.unescape(parser.metadata.get("og:article:author", "")).strip(),
        "digest": html.unescape(parser.metadata.get("og:description", "")).strip(),
        "content_url": str(response.url),
        "thumb_url": parser.metadata.get("og:image", "").strip(),
        "image_urls": image_urls,
        "body_text": body_text,
        "content_html": content_html,
        "update_time": None,
        "updated_at": None,
    }


def timestamp_iso(value: Any) -> str | None:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


class WeChatOfficialAccountClient:
    def __init__(
        self,
        app_id: str | None = None,
        app_secret: str | None = None,
        api_base: str | None = None,
        timeout: float | None = None,
        session: requests.Session | None = None,
        token_cache: Path | None = None,
    ) -> None:
        self.app_id = (app_id or os.getenv("WECHAT_MP_APP_ID", "")).strip()
        self.app_secret = (app_secret or os.getenv("WECHAT_MP_APP_SECRET", "")).strip()
        if not self.app_id or not self.app_secret:
            raise ValueError("WECHAT_MP_APP_ID and WECHAT_MP_APP_SECRET are required")
        self.api_base = (api_base or os.getenv("WECHAT_MP_API_BASE", "https://api.weixin.qq.com")).rstrip("/")
        self.timeout = timeout or float(os.getenv("WECHAT_MP_HTTP_TIMEOUT", "20"))
        self.session = session or requests.Session()
        self.token_cache = token_cache or self._default_token_cache()

    def _default_token_cache(self) -> Path:
        configured = os.getenv("WECHAT_MP_TOKEN_CACHE", "").strip()
        if configured:
            return Path(configured).expanduser().resolve()
        root = Path(os.getenv("LOCALAPPDATA", tempfile.gettempdir()))
        suffix = hashlib.sha256(self.app_id.encode("utf-8")).hexdigest()[:12]
        return root / "WorkMate" / "cache" / f"wechat-mp-token-{suffix}.json"

    def _decode_response(self, response: requests.Response) -> dict[str, Any]:
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("WeChat API returned a non-JSON response") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("WeChat API returned an unexpected response shape")
        errcode = payload.get("errcode", 0)
        if errcode not in (None, 0, "0"):
            raise WeChatAPIError(errcode, str(payload.get("errmsg", "unknown error")))
        return payload

    def _load_cached_token(self) -> str | None:
        try:
            payload = json.loads(self.token_cache.read_text(encoding="utf-8"))
            if payload.get("app_id") != self.app_id:
                return None
            if float(payload.get("expires_at", 0)) <= time.time() + 120:
                return None
            token = str(payload.get("access_token", "")).strip()
            return token or None
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _save_token(self, access_token: str, expires_in: int) -> None:
        self.token_cache.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "app_id": self.app_id,
            "access_token": access_token,
            "expires_at": time.time() + max(300, int(expires_in)),
        }
        temp_path = self.token_cache.with_suffix(self.token_cache.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temp_path, self.token_cache)

    def clear_token(self) -> None:
        try:
            self.token_cache.unlink()
        except FileNotFoundError:
            pass

    def get_access_token(self, force_refresh: bool = False) -> str:
        if not force_refresh:
            cached = self._load_cached_token()
            if cached:
                return cached

        try:
            response = self.session.post(
                f"{self.api_base}/cgi-bin/stable_token",
                json={
                    "grant_type": "client_credential",
                    "appid": self.app_id,
                    "secret": self.app_secret,
                    "force_refresh": bool(force_refresh),
                },
                timeout=self.timeout,
            )
            payload = self._decode_response(response)
        except (requests.RequestException, WeChatAPIError, RuntimeError):
            response = self.session.get(
                f"{self.api_base}/cgi-bin/token",
                params={
                    "grant_type": "client_credential",
                    "appid": self.app_id,
                    "secret": self.app_secret,
                },
                timeout=self.timeout,
            )
            payload = self._decode_response(response)

        token = str(payload.get("access_token", "")).strip()
        if not token:
            raise RuntimeError("WeChat token response did not include access_token")
        self._save_token(token, int(payload.get("expires_in", 7200)))
        return token

    def _authorized_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(2):
            token = self.get_access_token(force_refresh=attempt == 1)
            try:
                response = self.session.post(
                    f"{self.api_base}{path}",
                    params={"access_token": token},
                    json=body,
                    timeout=self.timeout,
                )
                return self._decode_response(response)
            except WeChatAPIError as exc:
                if attempt == 0 and exc.errcode in TOKEN_ERROR_CODES:
                    self.clear_token()
                    continue
                raise
        raise RuntimeError("WeChat API request failed after token refresh")

    def list_publications(self, offset: int = 0, count: int = 20, include_content: bool = True) -> dict[str, Any]:
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if not 1 <= count <= 20:
            raise ValueError("count must be between 1 and 20")
        return self._authorized_post(
            "/cgi-bin/freepublish/batchget",
            {"offset": offset, "count": count, "no_content": 0 if include_content else 1},
        )

    def get_publication(self, article_id: str) -> dict[str, Any]:
        article_id = article_id.strip()
        if not article_id:
            raise ValueError("article_id is required")
        return self._authorized_post("/cgi-bin/freepublish/get", {"article_id": article_id})


def normalize_publication(item: dict[str, Any]) -> list[dict[str, Any]]:
    article_id = str(item.get("article_id", ""))
    content = item.get("content") if isinstance(item.get("content"), dict) else item
    news_items = content.get("news_item") if isinstance(content, dict) else None
    if not isinstance(news_items, list):
        news_items = item.get("news_item") if isinstance(item.get("news_item"), list) else []
    update_time = item.get("update_time", content.get("update_time") if isinstance(content, dict) else None)
    normalized: list[dict[str, Any]] = []
    for index, article in enumerate(news_items):
        if not isinstance(article, dict):
            continue
        content_html = str(article.get("content", ""))
        body_text, image_urls = parse_article_html(content_html)
        normalized.append(
            {
                "article_id": article_id,
                "article_index": index,
                "title": str(article.get("title", "")).strip(),
                "author": str(article.get("author", "")).strip(),
                "digest": str(article.get("digest", "")).strip(),
                "content_url": str(article.get("url") or article.get("content_source_url") or "").strip(),
                "thumb_url": str(article.get("thumb_url", "")).strip(),
                "image_urls": image_urls,
                "body_text": body_text,
                "content_html": content_html,
                "update_time": update_time,
                "updated_at": timestamp_iso(update_time),
            }
        )
    return normalized


def flatten_publications(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("item", [])
    if not isinstance(items, list):
        return []
    articles: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            articles.extend(normalize_publication(item))
    return articles


def write_result(payload: Any, output: str | None) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    if output:
        path = Path(output).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(serialized + "\n", encoding="utf-8")
        print(json.dumps({"output": str(path)}, ensure_ascii=False))
    else:
        print(serialized)


def command_list(client: WeChatOfficialAccountClient, args: argparse.Namespace) -> Any:
    payload = client.list_publications(args.offset, args.count, not args.no_content)
    return {
        "total_count": payload.get("total_count"),
        "item_count": payload.get("item_count"),
        "articles": flatten_publications(payload),
    }


def command_get(client: WeChatOfficialAccountClient, args: argparse.Namespace) -> Any:
    payload = client.get_publication(args.article_id)
    articles = normalize_publication(payload)
    if args.index is None:
        return {"article_id": args.article_id, "articles": articles}
    if args.index < 0 or args.index >= len(articles):
        raise IndexError(f"article index {args.index} is out of range (count={len(articles)})")
    return articles[args.index]


def command_latest(client: WeChatOfficialAccountClient, args: argparse.Namespace) -> Any:
    articles: list[dict[str, Any]] = []
    keyword = args.keyword.casefold().strip()
    for page in range(args.pages):
        payload = client.list_publications(page * 20, 20, True)
        batch = flatten_publications(payload)
        if keyword:
            batch = [
                article
                for article in batch
                if keyword
                in "\n".join(
                    str(article.get(field, ""))
                    for field in ("title", "author", "digest", "body_text")
                ).casefold()
            ]
        articles.extend(batch)
        if len(articles) >= args.limit or not payload.get("item_count"):
            break
    articles.sort(key=lambda item: int(item.get("update_time") or 0), reverse=True)
    return {"keyword": args.keyword, "articles": articles[: args.limit]}


def command_url(args: argparse.Namespace) -> Any:
    return fetch_public_article(args.url)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List published articles")
    list_parser.add_argument("--offset", type=int, default=0)
    list_parser.add_argument("--count", type=int, default=20)
    list_parser.add_argument("--no-content", action="store_true")
    list_parser.add_argument("--output")

    get_parser = subparsers.add_parser("get", help="Get one publication")
    get_parser.add_argument("article_id")
    get_parser.add_argument("--index", type=int)
    get_parser.add_argument("--output")

    latest_parser = subparsers.add_parser("latest", help="Find latest matching articles")
    latest_parser.add_argument("--keyword", default="")
    latest_parser.add_argument("--limit", type=int, default=5)
    latest_parser.add_argument("--pages", type=int, default=3)
    latest_parser.add_argument("--output")

    url_parser = subparsers.add_parser("url", help="Read one public mp.weixin.qq.com article URL")
    url_parser.add_argument("url")
    url_parser.add_argument("--output")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "limit", 1) < 1 or getattr(args, "pages", 1) < 1:
        parser.error("limit and pages must be positive")
    try:
        if args.command == "url":
            result = command_url(args)
            write_result(result, args.output)
            return 0
        client = WeChatOfficialAccountClient()
        handlers = {"list": command_list, "get": command_get, "latest": command_latest}
        result = handlers[args.command](client, args)
        write_result(result, getattr(args, "output", None))
        return 0
    except (ValueError, IndexError, OSError, requests.RequestException, WeChatAPIError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
