from __future__ import annotations

import http.cookiejar
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

_IMPORT_TEMP = tempfile.TemporaryDirectory()
_OLD_DATABASE_PATH = os.environ.get("DATABASE_PATH")
os.environ["DATABASE_PATH"] = str(Path(_IMPORT_TEMP.name) / "import.db")
import main  # noqa: E402
if _OLD_DATABASE_PATH is None:
    os.environ.pop("DATABASE_PATH", None)
else:
    os.environ["DATABASE_PATH"] = _OLD_DATABASE_PATH

from love_moments.store import LoveMomentsStore  # noqa: E402


def tearDownModule() -> None:  # noqa: N802
    _IMPORT_TEMP.cleanup()


class LifeAssistantHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.original_store = main.STORE
        main.STORE = LoveMomentsStore(
            Path(cls.temp.name) / "assistant-http.db", seed_demo_data=True
        )
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), main.LoveMomentsHandler
        )
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/api/v1"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        main.STORE = cls.original_store
        cls.temp.cleanup()

    def opener(self) -> urllib.request.OpenerDirector:
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def request(
        self,
        opener: urllib.request.OpenerDirector,
        method: str,
        path: str,
        payload: dict | None = None,
    ) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + path,
            data=(
                json.dumps(payload).encode("utf-8")
                if payload is not None
                else None
            ),
            headers={"Content-Type": "application/json"},
            method=method,
        )
        with opener.open(request) as response:
            return response.status, json.load(response)

    def login(self, username: str) -> urllib.request.OpenerDirector:
        opener = self.opener()
        status, _ = self.request(
            opener,
            "POST",
            "/auth/login",
            {"username": username, "password": "test123"},
        )
        self.assertEqual(status, 200)
        return opener

    def test_unauthenticated_today_is_rejected(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/assistant/today")
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()

    def test_two_accounts_can_view_and_confirm_the_same_plan(self) -> None:
        tang, chao = self.login("test"), self.login("test1")
        status, job = self.request(
            tang,
            "POST",
            "/assistant/jobs",
            {
                "intent": "周六晚上想吃番茄炒蛋，加入日历并生成采购清单",
                "client_request_id": f"http-{uuid.uuid4().hex}",
            },
        )
        self.assertEqual(status, 201)
        plan = job["plan"]
        _, shared = self.request(
            chao, "GET", f"/assistant/plans/{plan['id']}"
        )
        self.assertEqual(shared["id"], plan["id"])
        self.assertEqual(shared["confirmation_count"], 0)

        _, first = self.request(
            tang,
            "POST",
            f"/assistant/plans/{plan['id']}/confirm",
            {
                "expected_version": 1,
                "idempotency_key": f"confirm-{uuid.uuid4().hex}",
            },
        )
        self.assertFalse(first["applied"])
        _, second = self.request(
            chao,
            "POST",
            f"/assistant/plans/{plan['id']}/confirm",
            {
                "expected_version": 1,
                "idempotency_key": f"confirm-{uuid.uuid4().hex}",
            },
        )
        self.assertTrue(second["applied"])
        _, calendar = self.request(chao, "GET", "/calendar")
        self.assertTrue(
            any(item["title"].endswith("番茄炒蛋") for item in calendar["items"])
        )

    def test_notification_inbox_has_independent_read_state(self) -> None:
        tang, chao = self.login("test"), self.login("test1")
        _, job = self.request(
            tang,
            "POST",
            "/assistant/jobs",
            {
                "intent": "明天想吃不存在的菜",
                "client_request_id": f"http-{uuid.uuid4().hex}",
            },
        )
        _, partner_inbox = self.request(chao, "GET", "/notifications")
        self.assertGreater(partner_inbox["unread_count"], 0)
        notification_id = partner_inbox["items"][0]["id"]
        _, marked = self.request(
            chao, "POST", f"/notifications/{notification_id}/read", {}
        )
        self.assertTrue(marked["read"])
        _, mine = self.request(tang, "GET", "/notifications")
        self.assertNotEqual(
            mine["unread_count"], partner_inbox["unread_count"] - 1
        )
        self.assertEqual(job["status"], "completed")


if __name__ == "__main__":
    unittest.main()
