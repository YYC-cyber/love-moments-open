from __future__ import annotations

import io
import json
import unittest
import urllib.error

from love_moments.turn import TurnCredentialProvider


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class TurnCredentialTests(unittest.TestCase):
    def test_missing_keys_returns_stun_only(self) -> None:
        value = TurnCredentialProvider(key_id="", api_token="").get_config()
        self.assertFalse(value["turnAvailable"])
        self.assertEqual(value["iceTransportPolicy"], "all")
        self.assertEqual(value["iceServers"][0]["urls"], ["stun:stun.cloudflare.com:3478"])

    def test_cloudflare_credentials_are_validated_and_cached(self) -> None:
        calls = []

        def opener(request, timeout):
            calls.append((request, timeout))
            body = {
                "iceServers": [
                    {"urls": ["stun:stun.cloudflare.com:3478"]},
                    {
                        "urls": ["turn:turn.cloudflare.com:3478?transport=udp", "turn:turn.cloudflare.com:53?transport=udp"],
                        "username": "temporary-user",
                        "credential": "temporary-secret",
                    },
                ]
            }
            return FakeResponse(json.dumps(body).encode())

        provider = TurnCredentialProvider(key_id="turn-key", api_token="api-token", opener=opener)
        first = provider.get_config()
        second = provider.get_config()
        self.assertTrue(first["turnAvailable"])
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("api-token", json.dumps(first))
        self.assertFalse(any(":53" in url for server in first["iceServers"] for url in server["urls"]))
        self.assertEqual(json.loads(calls[0][0].data), {"ttl": 3600})

    def test_force_relay_is_returned_only_with_valid_turn_server(self) -> None:
        def opener(_request, timeout):
            body = {
                "iceServers": [{
                    "urls": ["turns:turn.cloudflare.com:5349?transport=tcp"],
                    "username": "temporary-user",
                    "credential": "temporary-secret",
                }]
            }
            return FakeResponse(json.dumps(body).encode())

        value = TurnCredentialProvider(
            key_id="turn-key",
            api_token="api-token",
            opener=opener,
            force_relay=True,
        ).get_config()
        self.assertTrue(value["turnAvailable"])
        self.assertEqual(value["iceTransportPolicy"], "relay")

    def test_provider_failure_falls_back_without_exposing_secret(self) -> None:
        def opener(_request, timeout):
            raise urllib.error.URLError("offline")

        value = TurnCredentialProvider(key_id="turn-key", api_token="secret", opener=opener).get_config()
        self.assertFalse(value["turnAvailable"])
        self.assertEqual(value["provider"], "cloudflare-stun-fallback")
        self.assertNotIn("secret", json.dumps(value))


if __name__ == "__main__":
    unittest.main()
