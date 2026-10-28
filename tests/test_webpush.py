from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from love_moments.webpush import WebPushService


def encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def subscription() -> dict:
    return {
        "endpoint": "https://web.push.apple.com/QH-safe-test-endpoint",
        "keys": {
            "p256dh": encoded(b"\x04" + bytes(range(64))),
            "auth": encoded(bytes(range(16))),
        },
    }


class WebPushServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def service(self) -> WebPushService:
        with patch.dict(os.environ, {
            "WEB_PUSH_ENABLED": "true",
            "WEB_PUSH_VAPID_SUBJECT": "https://love-moments.test",
            "WEB_PUSH_PRIVATE_KEY_PATH": "",
            "WEB_PUSH_ALLOWED_HOST_SUFFIXES": ".push.apple.com,.googleapis.com",
        }, clear=False):
            return WebPushService(self.root, self.root / "media")

    def test_generates_private_vapid_key_once_with_private_permissions(self) -> None:
        first = self.service()
        second = self.service()
        self.assertTrue(first.enabled)
        self.assertEqual(first.public_key, second.public_key)
        self.assertEqual(len(base64.urlsafe_b64decode(first.public_key + "=")), 65)
        if os.name != "nt":
            self.assertEqual(first.key_path.stat().st_mode & 0o777, 0o600)

    def test_subscription_is_validated_encrypted_and_restorable(self) -> None:
        service = self.service()
        normalized = service.normalize_subscription(subscription())
        ciphertext = service.seal(normalized)
        self.assertNotIn("web.push.apple.com", ciphertext)
        self.assertEqual(service.unseal(ciphertext), normalized)

    def test_rejects_non_push_or_malformed_subscription_endpoints(self) -> None:
        service = self.service()
        for endpoint in (
            "http://web.push.apple.com/unsafe",
            "https://127.0.0.1/internal",
            "https://apple.com.evil.example/unsafe",
            "https://user@example.googleapis.com/unsafe",
            "https://fcm.googleapis.com:444/unsafe",
        ):
            value = subscription()
            value["endpoint"] = endpoint
            with self.assertRaisesRegex(ValueError, "地址无效"):
                service.normalize_subscription(value)
        broken = subscription()
        broken["keys"]["auth"] = encoded(b"too-short")
        with self.assertRaisesRegex(ValueError, "认证密钥无效"):
            service.normalize_subscription(broken)

    def test_send_uses_high_urgency_short_lived_encrypted_web_push(self) -> None:
        service = self.service()
        ciphertext = service.seal(service.normalize_subscription(subscription()))
        response = type("Response", (), {"status_code": 201})()
        with patch("love_moments.webpush.webpush", return_value=response) as sender:
            result = service.send(ciphertext, {
                "type": "incoming_call",
                "title": "恋屿语音来电",
                "body": "对方正在呼叫你，点击进入恋屿接听。",
                "url": "/?screen=chat&call=call-1",
                "tag": "call-call-1",
            }, ttl=60)
        self.assertIs(result, response)
        arguments = sender.call_args.kwargs
        self.assertEqual(arguments["content_encoding"], "aes128gcm")
        self.assertEqual(arguments["ttl"], 60)
        self.assertEqual(arguments["headers"]["Urgency"], "high")
        self.assertNotIn("call-call-1", arguments["headers"]["Topic"])
        self.assertEqual(arguments["subscription_info"], subscription())


if __name__ == "__main__":
    unittest.main()
