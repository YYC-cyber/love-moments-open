"""Short-lived WebRTC ICE configuration for the self-hosted app."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable


STUN_FALLBACK = [{"urls": ["stun:stun.cloudflare.com:3478"]}]


class TurnCredentialProvider:
    """Issue browser-safe Cloudflare TURN credentials without exposing API tokens."""

    def __init__(
        self,
        key_id: str | None = None,
        api_token: str | None = None,
        ttl_seconds: int = 3600,
        timeout_seconds: float = 8.0,
        opener: Callable[..., Any] | None = None,
        force_relay: bool | None = None,
    ) -> None:
        self.key_id = (key_id if key_id is not None else os.getenv("CLOUDFLARE_TURN_KEY_ID", "")).strip()
        self.api_token = (api_token if api_token is not None else os.getenv("CLOUDFLARE_TURN_API_TOKEN", "")).strip()
        self.ttl_seconds = max(300, min(int(ttl_seconds), 86400))
        self.timeout_seconds = timeout_seconds
        self.opener = opener or urllib.request.urlopen
        self.force_relay = (
            force_relay
            if force_relay is not None
            else os.getenv("WEBRTC_FORCE_RELAY", "false").strip().lower() == "true"
        )
        self._cached: dict | None = None
        self._cache_until = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def _validated_servers(value: object) -> list[dict]:
        if not isinstance(value, list):
            raise ValueError("TURN 服务返回了无效的 iceServers")
        servers: list[dict] = []
        for raw in value:
            if not isinstance(raw, dict):
                continue
            urls_value = raw.get("urls")
            urls = [urls_value] if isinstance(urls_value, str) else urls_value
            if not isinstance(urls, list):
                continue
            safe_urls = [
                url for url in urls
                if isinstance(url, str)
                and url.startswith(("stun:", "turn:", "turns:"))
                and not re.search(r":53(?:[/?]|$)", url)
            ]
            if not safe_urls:
                continue
            server = {"urls": safe_urls}
            if any(url.startswith(("turn:", "turns:")) for url in safe_urls):
                username = raw.get("username")
                credential = raw.get("credential")
                if not isinstance(username, str) or not isinstance(credential, str):
                    continue
                server.update({"username": username, "credential": credential})
            servers.append(server)
        if not servers:
            raise ValueError("TURN 服务没有返回可用的 ICE 地址")
        return servers

    def get_config(self) -> dict:
        if not self.key_id or not self.api_token:
            return {
                "iceServers": STUN_FALLBACK,
                "turnAvailable": False,
                "iceTransportPolicy": "all",
                "provider": "cloudflare-stun",
                "ttlSeconds": 0,
            }
        now = time.monotonic()
        with self._lock:
            if self._cached and now < self._cache_until:
                return dict(self._cached)
            url = f"https://rtc.live.cloudflare.com/v1/turn/keys/{self.key_id}/credentials/generate-ice-servers"
            request = urllib.request.Request(
                url,
                data=json.dumps({"ttl": self.ttl_seconds}).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.api_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST",
            )
            try:
                with self.opener(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                servers = self._validated_servers(payload.get("iceServers") if isinstance(payload, dict) else None)
                result = {
                    "iceServers": servers,
                    "turnAvailable": any(
                        any(url.startswith(("turn:", "turns:")) for url in server["urls"])
                        for server in servers
                    ),
                    "iceTransportPolicy": "relay" if self.force_relay else "all",
                    "provider": "cloudflare-turn",
                    "ttlSeconds": self.ttl_seconds,
                }
                if self.force_relay and not result["turnAvailable"]:
                    raise ValueError("强制中继模式下没有可用的 TURN 地址")
                self._cached = result
                self._cache_until = now + min(self.ttl_seconds * 0.8, 3000)
                return dict(result)
            except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError, KeyError):
                return {
                    "iceServers": STUN_FALLBACK,
                    "turnAvailable": False,
                    "iceTransportPolicy": "all",
                    "provider": "cloudflare-stun-fallback",
                    "ttlSeconds": 0,
                    "warning": "TURN 临时凭证获取失败，本次仅尝试点对点连接",
                }
