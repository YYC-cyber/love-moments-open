"""Private, self-hosted Web Push support for PWA notifications."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

try:
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from pywebpush import webpush
except ImportError:  # pragma: no cover - exercised by the explicit disabled path
    Fernet = InvalidToken = serialization = ec = webpush = None


MAX_SUBSCRIPTION_BYTES = 8192
MAX_PUSH_DATA_BYTES = 3072


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class WebPushService:
    """Generate local VAPID credentials and send encrypted standard Web Push."""

    def __init__(self, project_root: str | Path, media_root: str | Path):
        self.project_root = Path(project_root)
        self.media_root = Path(media_root)
        requested = os.getenv("WEB_PUSH_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
        self.enabled = bool(requested and webpush and ec and Fernet)
        self.reason = "" if self.enabled else ("" if not requested else "缺少 pywebpush/cryptography 依赖")
        self.subject = os.getenv("WEB_PUSH_VAPID_SUBJECT", "mailto:admin@love-moments.invalid").strip()
        if not re.match(r"^(mailto:|https?://)", self.subject):
            self.subject = "mailto:admin@love-moments.invalid"
        configured_path = os.getenv("WEB_PUSH_PRIVATE_KEY_PATH", "").strip()
        key_path = Path(configured_path) if configured_path else self.media_root / ".web-push-vapid-private.pem"
        self.key_path = key_path if key_path.is_absolute() else self.project_root / key_path
        self.public_key = ""
        self._fernet: Any = None
        configured_hosts = os.getenv("WEB_PUSH_ALLOWED_HOST_SUFFIXES", "").strip()
        default_hosts = ".push.apple.com,.googleapis.com,.push.services.mozilla.com,.notify.windows.com"
        self.allowed_host_suffixes = tuple(
            item.strip().lower() for item in (configured_hosts or default_hosts).split(",") if item.strip()
        )
        if self.enabled:
            private_pem, private_key = self._load_or_create_key()
            public_bytes = private_key.public_key().public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
            self.public_key = _b64url(public_bytes)
            storage_key = base64.urlsafe_b64encode(hashlib.sha256(private_pem + b"love-moments-push-storage").digest())
            self._fernet = Fernet(storage_key)

    def _load_or_create_key(self) -> tuple[bytes, Any]:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if self.key_path.exists():
            private_pem = self.key_path.read_bytes()
            private_key = serialization.load_pem_private_key(private_pem, password=None)
            if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(private_key.curve, ec.SECP256R1):
                raise ValueError("Web Push VAPID 私钥必须使用 P-256 曲线")
        else:
            private_key = ec.generate_private_key(ec.SECP256R1())
            private_pem = private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            temporary = self.key_path.with_suffix(self.key_path.suffix + ".tmp")
            temporary.write_bytes(private_pem)
            if os.name != "nt":
                temporary.chmod(0o600)
            os.replace(temporary, self.key_path)
        if os.name != "nt":
            self.key_path.chmod(0o600)
        return private_pem, private_key

    @staticmethod
    def _decode_key(value: str, label: str, expected_length: int) -> bytes:
        try:
            decoded = base64.urlsafe_b64decode(value.rstrip("=") + "=" * (-len(value.rstrip("=")) % 4))
        except (ValueError, TypeError) as error:
            raise ValueError(f"推送订阅{label}无效") from error
        if len(decoded) != expected_length:
            raise ValueError(f"推送订阅{label}无效")
        return decoded

    def normalize_subscription(self, value: object) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("推送订阅格式无效")
        endpoint = str(value.get("endpoint", "")).strip()
        try:
            parsed = urlparse(endpoint)
            port = parsed.port
        except ValueError as error:
            raise ValueError("推送订阅地址无效") from error
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme != "https"
            or not hostname
            or parsed.username
            or parsed.password
            or port not in {None, 443}
            or len(endpoint) > 4096
            or not any(hostname == suffix.lstrip(".") or hostname.endswith(suffix) for suffix in self.allowed_host_suffixes)
        ):
            raise ValueError("推送订阅地址无效")
        keys = value.get("keys")
        if not isinstance(keys, Mapping):
            raise ValueError("推送订阅缺少加密密钥")
        p256dh = str(keys.get("p256dh", "")).strip()
        auth = str(keys.get("auth", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{40,180}={0,2}", p256dh):
            raise ValueError("推送订阅公钥无效")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}={0,2}", auth):
            raise ValueError("推送订阅认证密钥无效")
        self._decode_key(p256dh, "公钥", 65)
        self._decode_key(auth, "认证密钥", 16)
        normalized = {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}
        if len(json.dumps(normalized, separators=(",", ":")).encode("utf-8")) > MAX_SUBSCRIPTION_BYTES:
            raise ValueError("推送订阅内容过大")
        return normalized

    @staticmethod
    def endpoint_hash(subscription: Mapping[str, Any]) -> str:
        return hashlib.sha256(str(subscription["endpoint"]).encode("utf-8")).hexdigest()

    def seal(self, subscription: Mapping[str, Any]) -> str:
        if not self.enabled or not self._fernet:
            raise RuntimeError("Web Push 尚未启用")
        payload = json.dumps(subscription, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._fernet.encrypt(payload).decode("ascii")

    def unseal(self, ciphertext: str) -> dict[str, Any]:
        if not self.enabled or not self._fernet:
            raise RuntimeError("Web Push 尚未启用")
        try:
            value = json.loads(self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8"))
        except (InvalidToken, ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("推送订阅无法解密，请在设备上重新开启提醒") from error
        return self.normalize_subscription(value)

    def send(self, ciphertext: str, payload: Mapping[str, Any], *, ttl: int = 60) -> Any:
        if not self.enabled:
            raise RuntimeError(self.reason or "Web Push 尚未启用")
        data = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))
        if len(data.encode("utf-8")) > MAX_PUSH_DATA_BYTES:
            raise ValueError("推送通知内容过大")
        subscription = self.unseal(ciphertext)
        topic_source = str(payload.get("tag", "love-moments"))
        topic = _b64url(hashlib.sha256(topic_source.encode("utf-8")).digest()[:18])
        return webpush(
            subscription_info=subscription,
            data=data,
            vapid_private_key=str(self.key_path),
            vapid_claims={"sub": self.subject},
            content_encoding="aes128gcm",
            timeout=8,
            ttl=max(0, min(int(ttl), 86400)),
            headers={"Urgency": "high", "Topic": topic},
        )

    @staticmethod
    def response_status(error: Exception) -> int | None:
        response = getattr(error, "response", None)
        try:
            return int(response.status_code) if response is not None else None
        except (TypeError, ValueError):
            return None
