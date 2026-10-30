#!/usr/bin/env python3
"""Read-only production acceptance check; credentials are read from environment only."""

from __future__ import annotations

import json
import os
import ssl
import sys
from http.cookiejar import CookieJar
from urllib import request

try:
    import certifi
except ImportError:  # pragma: no cover - system trust remains the fallback
    certifi = None


def fetch(opener, url: str, *, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = request.Request(url, data=data, headers={"Content-Type": "application/json"} if data else {}, method="POST" if data else "GET")
    with opener.open(req, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} 返回 HTTP {response.status}")
        return json.loads(response.read())


def verify_account(base: str, account: dict, context) -> dict:
    opener = request.build_opener(request.HTTPCookieProcessor(CookieJar()), request.HTTPSHandler(context=context))
    login = fetch(opener, base + "/api/v1/auth/login", payload=account)
    bootstrap = fetch(opener, base + "/api/v1/bootstrap")
    artwork = fetch(opener, base + "/api/v1/theme-assets")
    fetch(opener, base + "/api/v1/auth/logout", payload={})
    if bootstrap.get("user", {}).get("username") != account["username"]:
        raise RuntimeError("登录账号与共同空间身份不一致")
    if not login.get("pairing", {}).get("paired") or not bootstrap.get("partner"):
        raise RuntimeError("情侣绑定状态不完整")
    if not artwork.get("assets"):
        raise RuntimeError("私有主题贴纸未加载")
    return {"username": account["username"], "partner": bootstrap["partner"]["username"], "ok": True}


def main() -> None:
    base = os.environ.get("LOVE_VERIFY_BASE_URL", "").strip().rstrip("/")
    accounts = json.loads(os.environ.get("LOVE_VERIFY_ACCOUNTS_JSON", "[]"))
    if not base.startswith("https://") or not accounts:
        raise SystemExit("请通过 LOVE_VERIFY_BASE_URL 和 LOVE_VERIFY_ACCOUNTS_JSON 提供验收参数")
    context = ssl.create_default_context(cafile=certifi.where() if certifi else None)
    health = fetch(request.build_opener(request.HTTPSHandler(context=context)), base + "/api/health")
    if health.get("status") != "ok":
        raise RuntimeError("健康检查未通过")
    result = [verify_account(base, item, context) for item in accounts]
    print(json.dumps({"health": "ok", "accounts": result}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"验收失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
