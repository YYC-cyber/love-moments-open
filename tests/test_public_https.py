from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from main import LoveMomentsHandler


class PublicHttpsCookieTests(unittest.TestCase):
    @staticmethod
    def handler(host: str, forwarded_proto: str = "") -> LoveMomentsHandler:
        handler = object.__new__(LoveMomentsHandler)
        handler.headers = {"Host": host}
        if forwarded_proto:
            handler.headers["X-Forwarded-Proto"] = forwarded_proto
        return handler

    def test_auto_cookie_is_secure_on_configured_public_host(self) -> None:
        handler = self.handler("love.example.com")
        with patch.dict(
            "os.environ",
            {
                "PUBLIC_BASE_URL": "https://love.example.com",
                "COOKIE_SECURE": "auto",
            },
            clear=False,
        ):
            self.assertTrue(handler._request_secure_context())
            self.assertIn("; Secure", handler._session_cookie("token"))

    def test_auto_cookie_remains_usable_on_local_http(self) -> None:
        handler = self.handler("127.0.0.1:52013")
        with patch.dict(
            "os.environ",
            {
                "PUBLIC_BASE_URL": "https://love.example.com",
                "COOKIE_SECURE": "auto",
            },
            clear=False,
        ):
            self.assertTrue(handler._request_secure_context())
            self.assertNotIn("; Secure", handler._session_cookie("token"))

    def test_public_setting_does_not_mark_unrelated_lan_http_secure(self) -> None:
        handler = self.handler("192.168.1.20:52013")
        with patch.dict(
            "os.environ",
            {
                "PUBLIC_BASE_URL": "https://love.example.com",
                "COOKIE_SECURE": "auto",
            },
            clear=False,
        ):
            self.assertFalse(handler._request_secure_context())
            self.assertNotIn("; Secure", handler._session_cookie("token"))

    def test_forwarded_https_uses_secure_cookie_in_auto_mode(self) -> None:
        handler = self.handler("love.example.test", "https")
        with patch.dict("os.environ", {"COOKIE_SECURE": "auto", "PUBLIC_BASE_URL": ""}, clear=False):
            self.assertTrue(handler._request_secure_context())
            self.assertIn("; Secure", handler._session_cookie("token"))

    def test_malformed_request_can_finish_error_headers_without_path(self) -> None:
        handler = object.__new__(LoveMomentsHandler)
        handler.request_version = "HTTP/1.1"
        handler._headers_buffer = []
        handler.wfile = io.BytesIO()
        handler.end_headers()
        response_headers = handler.wfile.getvalue().decode("latin-1")
        self.assertIn("X-Content-Type-Options: nosniff", response_headers)


if __name__ == "__main__":
    unittest.main()
