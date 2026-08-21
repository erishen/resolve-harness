"""HTTP fetch tool: lets the agent retrieve public web content (JSON APIs etc.).

The rest of the harness stays offline by design — Fast Path and codegen are
network-free. `fetch` is the single, opt-in gateway to the web: only http(s)
URLs are accepted, requests are time-bounded, and oversized responses are
shrunk so a huge payload cannot blow up the model context. All failures are
returned as text so the agent can read them and retry.

Oversized JSON is compacted structurally (first few records kept, long fields
truncated, clearly annotated) instead of being cut mid-string — a byte-sliced
JSON payload is unusable and makes the agent loop trying to fix it.
"""

from __future__ import annotations

import json
import re

import httpx

_DEFAULT_MAX_CHARS = 20000
_TIMEOUT_SECONDS = 8.0
_UA = "agentpulse-fetch/0.1"
_MAX_RECORDS = 3  # records kept when compacting a huge JSON list
_FIELD_LIMIT = 200  # per-string length cap inside a compacted record


def _shrink_record(value: object) -> object:
    """Recursively cap long strings / huge lists inside one record."""
    if isinstance(value, dict):
        return {k: _shrink_record(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shrink_record(v) for v in value[:20]]
    if isinstance(value, str) and len(value) > _FIELD_LIMIT:
        return value[:_FIELD_LIMIT] + "…"
    return value


def _compact_json(text: str) -> str | None:
    """Best-effort shrink of an oversized JSON payload to its first records.

    Handles the common API shape `{"records": [...], ...}` where one value is
    a big list of objects (job boards, listings...). Returns a compact,
    still-valid JSON string, or None when the payload is small enough / not
    JSON / has no big list (in which case plain truncation applies).
    """
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if len(text) <= _DEFAULT_MAX_CHARS:
        return None
    if not isinstance(data, dict):
        return None
    for key, value in data.items():
        if isinstance(value, list) and value and isinstance(value[0], (dict, list)):
            total = len(value)
            compact = json.dumps(
                {
                    key: [_shrink_record(r) for r in value[:_MAX_RECORDS]],
                    "__note__": (
                        f"原始响应 {len(text)} 字符过大，{key} 共 {total} 条，"
                        f"已精简为前 {_MAX_RECORDS} 条并截断长字段"
                    ),
                },
                ensure_ascii=False,
            )
            if len(compact) < len(text) // 2:  # actually smaller — use it
                return compact
    return None


def _compact_html(text: str) -> str | None:
    """Best-effort shrink of an oversized HTML page to its essentials.

    Extracts the <title> and a deduped list of image URLs — enough for
    "what is this page about" questions (product pages, articles...) without
    dumping megabytes of markup into the model context. Returns None when the
    page is small enough, has nothing to extract, or the summary is not
    actually smaller (plain truncation applies then).
    """
    if len(text) <= _DEFAULT_MAX_CHARS:
        return None

    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()

    urls: list[str] = []
    urls += re.findall(r'(?:src|data-lazy-img)="(https?://[^"]+?\.(?:png|jpe?g|gif|webp)[^"]*)"', text, re.I)
    urls += re.findall(r"https?://img[^\"\\\s,]+?\.(?:png|jpe?g|gif|webp)[^\"\\\s,]*", text, re.I)
    seen: set[str] = set()
    unique: list[str] = []
    for u in urls:
        u = u.split("?")[0]
        if u not in seen:
            seen.add(u)
            unique.append(u)
        if len(unique) >= 10:
            break

    if not title and not unique:
        return None
    lines = [f"<title>{title}</title>"] if title else []
    if unique:
        lines.append(f"页面图片 {len(unique)} 个：")
        lines += [f"- {u}" for u in unique]
    summary = "\n".join(lines)
    if len(summary) < len(text) // 2:  # actually smaller — use it
        return summary
    return None


def fetch(url: str) -> str:
    """抓取公开 http(s) URL 并返回其文本内容。

    错误（非法协议、网络失败、HTTP 错误状态码）以可读文本返回而不是抛出，
    以便 agent 根据内容反应并重试。响应大小有内部上限——模型无法放大它，
    超大页面永远不会撑爆上下文。
    """
    if not url.strip().lower().startswith(("http://", "https://")):
        return f"仅支持 http/https URL：{url!r}"

    try:
        resp = httpx.get(
            url,
            timeout=_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": _UA},
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        return f"请求失败：{exc}"

    body = resp.text
    compact = _compact_json(body)
    if compact is None:
        compact = _compact_html(body)
    if compact is not None:
        return (
            f"--- {url}（HTTP {resp.status_code}，原 {len(body)} 字符，已精简）---\n"
            + compact
        )

    truncated = len(body) > _DEFAULT_MAX_CHARS
    preview = body[:_DEFAULT_MAX_CHARS]
    if truncated:
        preview += (
            f"\n…（内容过长，已截断至 {_DEFAULT_MAX_CHARS} 字符，共 {len(body)} 字符）"
        )
    return f"--- {url}（HTTP {resp.status_code}，{len(body)} 字符）---\n{preview}"
