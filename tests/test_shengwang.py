from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
import unittest
import zlib

from love_moments.shengwang import (
    ShengwangRtcProvider,
    build_rtc_token,
    channel_for_call,
    uid_for_user,
)


def unpack_uint16(buffer: bytes) -> tuple[int, bytes]:
    return struct.unpack("<H", buffer[:2])[0], buffer[2:]


def unpack_uint32(buffer: bytes) -> tuple[int, bytes]:
    return struct.unpack("<I", buffer[:4])[0], buffer[4:]


def unpack_string(buffer: bytes) -> tuple[bytes, bytes]:
    length, buffer = unpack_uint16(buffer)
    return buffer[:length], buffer[length:]


class ShengwangRtcTests(unittest.TestCase):
    app_id = "a" * 32
    certificate = "b" * 32

    def test_token007_contains_expected_signed_rtc_scope(self) -> None:
        token = build_rtc_token(
            self.app_id,
            self.certificate,
            "lm_test_channel",
            12345,
            3600,
            issue_timestamp=1_700_000_000,
            salt=123456,
        )
        self.assertTrue(token.startswith("007"))
        decoded = zlib.decompress(base64.b64decode(token[3:]))
        signature, signing_info = unpack_string(decoded)
        buffer = signing_info
        app_id, buffer = unpack_string(buffer)
        issue, buffer = unpack_uint32(buffer)
        expiry, buffer = unpack_uint32(buffer)
        salt, buffer = unpack_uint32(buffer)
        service_count, buffer = unpack_uint16(buffer)
        service_type, buffer = unpack_uint16(buffer)
        privilege_count, buffer = unpack_uint16(buffer)
        privileges = {}
        for _ in range(privilege_count):
            privilege, buffer = unpack_uint16(buffer)
            privilege_expiry, buffer = unpack_uint32(buffer)
            privileges[privilege] = privilege_expiry
        channel, buffer = unpack_string(buffer)
        uid, buffer = unpack_string(buffer)

        signing_key = hmac.new(struct.pack("<I", issue), self.certificate.encode(), hashlib.sha256).digest()
        signing_key = hmac.new(struct.pack("<I", salt), signing_key, hashlib.sha256).digest()
        expected_signature = hmac.new(signing_key, signing_info, hashlib.sha256).digest()
        self.assertTrue(hmac.compare_digest(signature, expected_signature))
        self.assertEqual(app_id.decode(), self.app_id)
        self.assertEqual(expiry, 3600)
        self.assertEqual(service_count, 1)
        self.assertEqual(service_type, 1)
        self.assertEqual(privileges, {1: 3600, 2: 3600, 3: 3600, 4: 3600})
        self.assertEqual(channel.decode(), "lm_test_channel")
        self.assertEqual(uid.decode(), "12345")
        self.assertEqual(buffer, b"")

    def test_provider_issues_safe_short_lived_credentials(self) -> None:
        provider = ShengwangRtcProvider("shengwang", self.app_id, self.certificate, 600)
        result = provider.issue("call-private-id", "user-one")
        self.assertEqual(result["provider"], "shengwang")
        self.assertEqual(result["channel"], channel_for_call("call-private-id"))
        self.assertEqual(result["uid"], uid_for_user("user-one"))
        self.assertEqual(result["ttlSeconds"], 600)
        self.assertNotIn("certificate", {key.lower() for key in result})
        self.assertGreaterEqual(result["expiresAt"], time.strftime("%Y-%m-%d"))

    def test_invalid_credentials_disable_provider_without_exposing_secret(self) -> None:
        provider = ShengwangRtcProvider("shengwang", "bad", self.certificate)
        config = provider.get_config()
        self.assertFalse(config["shengwangAvailable"])
        self.assertEqual(config["provider"], "webrtc")
        self.assertNotIn(self.certificate, repr(config))
        with self.assertRaisesRegex(ValueError, "尚未配置"):
            provider.issue("call-one", "user-one")

    def test_user_and_call_mapping_are_stable_and_separated(self) -> None:
        self.assertEqual(uid_for_user("user-one"), uid_for_user("user-one"))
        self.assertNotEqual(uid_for_user("user-one"), uid_for_user("user-two"))
        self.assertNotEqual(channel_for_call("call-one"), channel_for_call("call-two"))
        self.assertLess(len(channel_for_call("call-one").encode()), 64)


if __name__ == "__main__":
    unittest.main()
