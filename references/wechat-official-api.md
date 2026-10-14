# WeChat Official Account API

## Optional Official Account API

## Configuration

Set credentials in this skill's own `.env` file (existing process environment variables take precedence):

```text
WECHAT_MP_APP_ID=<official-account-app-id>
WECHAT_MP_APP_SECRET=<official-account-app-secret>
```

Optional settings:

```text
WECHAT_MP_API_BASE=https://api.weixin.qq.com
WECHAT_MP_HTTP_TIMEOUT=20
WECHAT_MP_TOKEN_CACHE=<absolute-cache-file-path>
```

The default token cache is stored below the current user's local application-data directory, outside the repository. Never commit the token cache.

For a known public article URL, bypass the account API and its IP whitelist:

```powershell
uv run --frozen python scripts\wechat_mp_articles.py url "https://mp.weixin.qq.com/s?..." --output output\<job-id>\article.json
```

This mode accepts only HTTPS URLs hosted by `mp.weixin.qq.com`. If WeChat serves a verification page instead of article HTML, open the URL in the Agent browser and use the visible article text or export it as DOCX.

## Read-only endpoints

The bundled client uses only:

- `POST /cgi-bin/stable_token`: obtain a stable access token
- `GET /cgi-bin/token`: compatibility fallback when stable-token acquisition is unavailable
- `POST /cgi-bin/freepublish/batchget`: list published articles
- `POST /cgi-bin/freepublish/get`: retrieve one published article by `article_id`

The API returns a publication containing one or more `news_item` entries. The client flattens these and records `article_index` so a multi-article publication remains addressable.

## Permissions

The official account must have permission for the published-article interfaces. WeChat may require account verification, an allowed source IP, or other platform-side settings. An access token succeeding does not guarantee that `freepublish` is authorized.

## Common failures

- `40001`, `40014`, `42001`: invalid or expired token. The client clears its cache and retries once.
- `40164`: source IP is not in the account whitelist.
- `48001`: API is not authorized for this account type or account configuration.
- `45009`: API rate limit reached. Wait rather than retrying in a tight loop.
- `40013`: invalid AppID.

Preserve WeChat `errcode` and `errmsg` in diagnostics, but never include credentials or access tokens.
