"""Server-side Shengwang/Agora RTC token issuing.

The token encoder implements the vendor's AccessToken2 (Token007) wire format.
Only short-lived tokens and the public App ID may leave the server; the App
Certificate is never included in API responses.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
import zlib
from collections import OrderedDict
from datetime import datetime, timezone


SDK_VERSION = "4.24.8"
TOKEN_VERSION = "007"


def _pack_uint16(value: int) -> bytes:
    return struct.pack("<H", int(value))


def _pack_uint32(value: int) -> bytes:
    return struct.pack("<I", int(value))


def _pack_string(value: str | bytes) -> bytes:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return _pack_uint16(len(encoded)) + encoded


def _pack_privileges(privileges: dict[int, int]) -> bytes:
    ordered = OrderedDict(sorted(privileges.items(), key=lambda item: item[0]))
    return _pack_uint16(len(ordered)) + b"".join(
        _pack_uint16(privilege) + _pack_uint32(expiry)
        for privilege, expiry in ordered.items()
    )


def build_rtc_token(
    app_id: str,
    app_certificate: str,
    channel: str,
    uid: int,
    ttl_seconds: int = 3600,
    *,
    issue_timestamp: int | None = None,
    salt: int | None = None,
) -> str:
    """Build a publisher Token007 for one RTC channel and numeric UID."""
    for label, value in (("App ID", app_id), ("App Certificate", app_certificate)):
        if len(value) != 32:
            raise ValueError(f"声网 {label} 必须是 32 位十六进制字符串")
        try:
            bytes.fromhex(value)
        except ValueError as error:
            raise ValueError(f"声网 {label} 必须是 32 位十六进制字符串") from error
    if not channel or len(channel.encode("utf-8")) >= 64:
        raise ValueError("声网频道名称无效")
    if not 1 <= int(uid) <= 0xFFFFFFFF:
        raise ValueError("声网用户 ID 必须是非零 32 位无符号整数")

    ttl_seconds = max(60, min(int(ttl_seconds), 86_400))
    issued_at = int(time.time()) if issue_timestamp is None else int(issue_timestamp)
    token_salt = secrets.SystemRandom().randint(1, 99_999_999) if salt is None else int(salt)

    # RTC service type=1; join/audio/video/data privilege IDs=1..4.
    privileges = {1: ttl_seconds, 2: ttl_seconds, 3: ttl_seconds, 4: ttl_seconds}
    service = (
        _pack_uint16(1)
        + _pack_privileges(privileges)
        + _pack_string(channel)
        + _pack_string(str(int(uid)))
    )
    signing_info = (
        _pack_string(app_id)
        + _pack_uint32(issued_at)
        + _pack_uint32(ttl_seconds)
        + _pack_uint32(token_salt)
        + _pack_uint16(1)
        + service
    )
    signing_key = hmac.new(_pack_uint32(issued_at), app_certificate.encode("utf-8"), hashlib.sha256).digest()
    signing_key = hmac.new(_pack_uint32(token_salt), signing_key, hashlib.sha256).digest()
    signature = hmac.new(signing_key, signing_info, hashlib.sha256).digest()
    payload = zlib.compress(_pack_string(signature) + signing_info)
    return TOKEN_VERSION + base64.b64encode(payload).decode("ascii")


def uid_for_user(user_id: str) -> int:
    """Map an internal user ID to a stable, non-zero 32-bit RTC UID."""
    uid = int.from_bytes(hashlib.sha256(user_id.encode("utf-8")).digest()[:4], "big")
    return uid or 1


def channel_for_call(call_id: str) -> str:
    """Use a non-guessable, SDK-safe channel name derived from the call ID."""
    return "lm_" + hashlib.sha256(call_id.encode("utf-8")).hexdigest()[:32]


class ShengwangRtcProvider:
    """Read RTC settings and issue browser-safe per-call credentials."""

    def __init__(
        self,
        provider: str | None = None,
        app_id: str | None = None,
        app_certificate: str | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        self.provider = (provider if provider is not None else os.getenv("RTC_PROVIDER", "webrtc")).strip().lower()
        self.app_id = (app_id if app_id is not None else os.getenv("SHENGWANG_APP_ID", "")).strip()
        self.app_certificate = (
            app_certificate if app_certificate is not None else os.getenv("SHENGWANG_APP_CERTIFICATE", "")
        ).strip()
        raw_ttl = ttl_seconds if ttl_seconds is not None else os.getenv("SHENGWANG_TOKEN_TTL_SECONDS", "3600")
        try:
            self.ttl_seconds = max(300, min(int(raw_ttl), 86_400))
        except (TypeError, ValueError):
            self.ttl_seconds = 3600

    @staticmethod
    def _valid_identifier(value: str) -> bool:
        if len(value) != 32:
            return False
        try:
            bytes.fromhex(value)
        except ValueError:
            return False
        return True

    @property
    def enabled(self) -> bool:
        return self.provider == "shengwang" and self._valid_identifier(self.app_id) and self._valid_identifier(self.app_certificate)

    def get_config(self) -> dict:
        warning = None
        if self.provider == "shengwang" and not self.enabled:
            warning = "声网 App ID 或 App Certificate 未正确配置，通话将回退到原生 WebRTC"
        return {
            "provider": "shengwang" if self.enabled else "webrtc",
            "requestedProvider": self.provider,
            "shengwangAvailable": self.enabled,
            "sdkVersion": SDK_VERSION,
            "warning": warning,
        }

    def issue(self, call_id: str, user_id: str) -> dict:
        if not self.enabled:
            raise ValueError("声网 RTC 尚未配置完成")
        now = int(time.time())
        channel = channel_for_call(call_id)
        uid = uid_for_user(user_id)
        token = build_rtc_token(
            self.app_id,
            self.app_certificate,
            channel,
            uid,
            self.ttl_seconds,
            issue_timestamp=now,
        )
        return {
            "provider": "shengwang",
            "enabled": True,
            "appId": self.app_id,
            "channel": channel,
            "uid": uid,
            "token": token,
            "expiresAt": datetime.fromtimestamp(now + self.ttl_seconds, timezone.utc).isoformat(),
            "ttlSeconds": self.ttl_seconds,
            "sdkVersion": SDK_VERSION,
        }
