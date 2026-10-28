"""Public restaurant-review evidence fetched through the configured search API.

The provider deliberately does not scrape login-protected pages and never turns
search snippets into a numeric rating. A result is kept only when its final URL
belongs to one of the explicitly supported public platform domains.
"""

from __future__ import annotations

import json
import os
from typing import Callable
from urllib import error, parse, request

from .http_client import TRUSTED_SSL_CONTEXT


PLATFORM_DOMAINS = {
    "dianping.com": "大众点评",
    "meituan.com": "美团",
    "xiaohongshu.com": "小红书",
}


def _platform_for_url(value: object) -> str:
    try:
        host = (parse.urlparse(str(value or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    for domain, label in PLATFORM_DOMAINS.items():
        if host == domain or host.endswith(f".{domain}"):
            return label
    return ""


class RestaurantReviewSearch:
    endpoint = "https://serpapi.com/search.json"

    def __init__(self, api_key: str, timeout: int = 20, before_request: Callable[[str], None] | None = None):
        self.api_key = api_key.strip()
        self.timeout = max(5, min(int(timeout), 45))
        self.before_request = before_request
        self.is_configured = bool(self.api_key)

    def search(self, area: str, restaurant_names: list[str], limit: int = 8) -> list[dict]:
        area = str(area or "").strip()[:120]
        names = [str(name).strip()[:80] for name in restaurant_names if str(name).strip()][:3]
        if not self.is_configured or not area or not names:
            return []
        # Searching one concrete shop is substantially more precise than
        # combining several shop names with three site operators. Keep the
        # platform words as alternatives, then enforce domains on every result
        # below; this returns concrete shop/review pages without generic links.
        query = f'"{names[0]}" "{area}" (大众点评 OR 美团 OR 小红书)'
        if self.before_request:
            self.before_request("restaurant public review search")
        url = f"{self.endpoint}?{parse.urlencode({'engine': 'google', 'q': query, 'hl': 'zh-cn', 'gl': 'cn', 'num': 10, 'api_key': self.api_key})}"
        try:
            with request.urlopen(
                request.Request(url, headers={"Accept": "application/json", "User-Agent": "LoveMoments/1.0"}),
                timeout=self.timeout,
                context=TRUSTED_SSL_CONTEXT,
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (error.URLError, TimeoutError, json.JSONDecodeError, UnicodeError) as exc:
            raise RuntimeError("餐厅公开口碑检索暂时不可用") from exc
        if not isinstance(result, dict):
            raise RuntimeError("餐厅公开口碑检索返回格式无效")
        if result.get("error"):
            raise RuntimeError(f"餐厅公开口碑检索失败：{str(result['error'])[:160]}")

        rows, seen = [], set()
        for item in result.get("organic_results", []) or []:
            if not isinstance(item, dict):
                continue
            link = str(item.get("link", "")).strip()[:1200]
            platform = _platform_for_url(link)
            title = str(item.get("title", "")).strip()[:240]
            snippet = str(item.get("snippet", "")).strip()[:600]
            if not platform or not title or link in seen:
                continue
            parsed_link = parse.urlparse(link)
            if parsed_link.scheme == "http":
                link = parsed_link._replace(scheme="https").geturl()
            seen.add(link)
            rows.append({
                "platform": platform,
                "title": title,
                "snippet": snippet,
                "url": link,
                "source_query": query[:600],
            })
            if len(rows) >= max(1, min(int(limit), 12)):
                break
        return rows


def restaurant_review_search_from_env(before_request: Callable[[str], None] | None = None) -> RestaurantReviewSearch:
    enabled = os.getenv("SERPAPI_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    key = os.getenv("SERPAPI_API_KEY", "").strip() if enabled else ""
    try:
        timeout = int(os.getenv("QUOTE_TIMEOUT_SECONDS", "20"))
    except ValueError:
        timeout = 20
    return RestaurantReviewSearch(key, timeout=timeout, before_request=before_request)
