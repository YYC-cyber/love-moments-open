from __future__ import annotations

import http.cookiejar
import base64
import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

_MAIN_IMPORT_TEMP = tempfile.TemporaryDirectory()
_PREVIOUS_DATABASE_PATH = os.environ.get("DATABASE_PATH")
_PREVIOUS_MEDIA_ROOT = os.environ.get("MEDIA_ROOT")
os.environ["DATABASE_PATH"] = str(Path(_MAIN_IMPORT_TEMP.name) / "import.db")
os.environ["MEDIA_ROOT"] = str(Path(_MAIN_IMPORT_TEMP.name) / "import-media")
import main  # noqa: E402
if _PREVIOUS_DATABASE_PATH is None:
    os.environ.pop("DATABASE_PATH", None)
else:
    os.environ["DATABASE_PATH"] = _PREVIOUS_DATABASE_PATH
if _PREVIOUS_MEDIA_ROOT is None:
    os.environ.pop("MEDIA_ROOT", None)
else:
    os.environ["MEDIA_ROOT"] = _PREVIOUS_MEDIA_ROOT
from love_moments import LoveMomentsStore
from love_moments.schema import SCHEMA_VERSION
from love_moments.weather import OpenMeteoWeather
from love_moments.shengwang import ShengwangRtcProvider
from love_moments.travel_assistant import RuleBasedTravelAssistant
from love_moments.expression_assistant import RuleBasedExpressionAssistant
from love_moments.store import DEMO_COUPLE, DEMO_PARTNER, DEMO_TRIP, DEMO_USER
from tests import VALID_PNG


def tearDownModule() -> None:  # noqa: N802
    _MAIN_IMPORT_TEMP.cleanup()


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.original_backup_root = os.environ.get("BACKUP_ROOT")
        cls.original_amap_key = os.environ.get("AMAP_WEB_KEY")
        cls.original_geoapify_enabled = os.environ.get("GEOAPIFY_ENABLED")
        cls.original_ocr_provider = os.environ.get("OCR_PROVIDER")
        cls.original_registration_mode = os.environ.get("REGISTRATION_MODE")
        cls.original_cors_allowed_origins = os.environ.get("CORS_ALLOWED_ORIGINS")
        os.environ["BACKUP_ROOT"] = str(Path(cls.temp.name) / "backups")
        os.environ["AMAP_WEB_KEY"] = ""
        os.environ["GEOAPIFY_ENABLED"] = "false"
        os.environ["OCR_PROVIDER"] = "mock"
        os.environ["REGISTRATION_MODE"] = "open"
        os.environ["CORS_ALLOWED_ORIGINS"] = "http://127.0.0.1:18765,http://localhost:18765"
        cls.original_store = main.STORE
        cls.original_weather = main.WEATHER
        cls.original_rtc = main.RTC
        cls.original_auth_limiter = main.AUTH_LIMITER
        cls.original_push_test_limiter = main.PUSH_TEST_LIMITER
        main.STORE = LoveMomentsStore(Path(cls.temp.name) / "http-test.db", travel_assistant=RuleBasedTravelAssistant(), expression_assistant=RuleBasedExpressionAssistant(), seed_demo_data=True)
        main.WEATHER = OpenMeteoWeather(enabled=False)
        main.RTC = ShengwangRtcProvider("shengwang", "a" * 32, "b" * 32, 600)
        main.AUTH_LIMITER = main.RequestLimiter(limit=1_000, window_seconds=60)
        main.PUSH_TEST_LIMITER = main.RequestLimiter(limit=1_000, window_seconds=60)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), main.LoveMomentsHandler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/api/v1"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        main.STORE = cls.original_store
        main.WEATHER = cls.original_weather
        main.RTC = cls.original_rtc
        main.AUTH_LIMITER = cls.original_auth_limiter
        main.PUSH_TEST_LIMITER = cls.original_push_test_limiter
        if cls.original_backup_root is None:
            os.environ.pop("BACKUP_ROOT", None)
        else:
            os.environ["BACKUP_ROOT"] = cls.original_backup_root
        if cls.original_amap_key is None:
            os.environ.pop("AMAP_WEB_KEY", None)
        else:
            os.environ["AMAP_WEB_KEY"] = cls.original_amap_key
        if cls.original_geoapify_enabled is None:
            os.environ.pop("GEOAPIFY_ENABLED", None)
        else:
            os.environ["GEOAPIFY_ENABLED"] = cls.original_geoapify_enabled
        if cls.original_ocr_provider is None:
            os.environ.pop("OCR_PROVIDER", None)
        else:
            os.environ["OCR_PROVIDER"] = cls.original_ocr_provider
        if cls.original_registration_mode is None:
            os.environ.pop("REGISTRATION_MODE", None)
        else:
            os.environ["REGISTRATION_MODE"] = cls.original_registration_mode
        if cls.original_cors_allowed_origins is None:
            os.environ.pop("CORS_ALLOWED_ORIGINS", None)
        else:
            os.environ["CORS_ALLOWED_ORIGINS"] = cls.original_cors_allowed_origins
        cls.temp.cleanup()

    def opener(self) -> urllib.request.OpenerDirector:
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def register_push_device(self, opener: urllib.request.OpenerDirector, label: str) -> dict:
        with main.STORE.connection() as db:
            existing_job_ids = {row["id"] for row in db.execute("SELECT id FROM push_outbox").fetchall()}

        def remove_created_push_jobs() -> None:
            with main.STORE.connection() as db:
                if existing_job_ids:
                    placeholders = ",".join("?" for _ in existing_job_ids)
                    db.execute(f"DELETE FROM push_outbox WHERE id NOT IN ({placeholders})", tuple(existing_job_ids))
                else:
                    db.execute("DELETE FROM push_outbox")

        self.addCleanup(remove_created_push_jobs)
        encode = lambda value: base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
        subscription = {
            "endpoint": f"https://web.push.apple.com/QH-{label}-{uuid.uuid4().hex}",
            "keys": {"p256dh": encode(b"\x04" + bytes(range(64))), "auth": encode(bytes(range(16)))},
        }
        registered = self.post(opener, "/push/subscriptions", {"subscription": subscription})
        self.assertTrue(registered["subscribed"])
        self.addCleanup(
            lambda: self.delete(opener, "/push/subscriptions", {"subscription": subscription}),
        )
        return subscription

    def post(self, opener: urllib.request.OpenerDirector, path: str, payload: dict, headers: dict | None = None) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        with opener.open(req) as response:
            return json.load(response)

    def patch(self, opener: urllib.request.OpenerDirector, path: str, payload: dict, headers: dict | None = None) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="PATCH",
        )
        with opener.open(req) as response:
            return json.load(response)

    def put(self, opener: urllib.request.OpenerDirector, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        with opener.open(req) as response:
            return json.load(response)

    def delete(self, opener: urllib.request.OpenerDirector, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="DELETE",
        )
        with opener.open(req) as response:
            return json.load(response)

    def test_unauthenticated_business_request_is_rejected(self) -> None:
        try:
            urllib.request.urlopen(self.base + "/bootstrap")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 401)
            error.close()
        else:
            self.fail("未登录请求应返回 401")

    def test_trip_expense_http_contract_is_shared_versioned_and_atomic(self) -> None:
        tang, chao = self.opener(), self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        self.post(chao, "/auth/login", {"username": "test1", "password": "test123"})
        trip_id = f"trip-expense-http-{uuid.uuid4().hex}"
        with main.STORE.connection() as db:
            db.execute(
                """INSERT INTO trips(
                       id,couple_id,title,destination,start_date,end_date,budget_cents,currency,status,version
                   ) VALUES (?,?,?,?,?,?,?,'CNY','confirmed',1)""",
                (trip_id, DEMO_COUPLE, "HTTP 旅行花费", "杭州", "2026-11-01", "2026-11-03", 200000),
            )

        first_payload = {
            "amount_cents": 1001, "payer_id": DEMO_USER, "category": "food",
            "split_mode": "equal", "occurred_on": "2026-11-01", "note": "HTTP 晚餐",
            "idempotency_key": "body-key-must-not-win",
        }
        first = self.post(
            tang, f"/trips/{trip_id}/expenses", first_payload,
            {"Idempotency-Key": "http:expense:create:first"},
        )
        replay = self.post(
            tang, f"/trips/{trip_id}/expenses", {**first_payload, "idempotency_key": "another-body-key"},
            {"Idempotency-Key": "http:expense:create:first"},
        )
        self.assertEqual(first, replay)
        with self.assertRaises(urllib.error.HTTPError) as changed_key:
            self.post(
                tang, f"/trips/{trip_id}/expenses", {**first_payload, "amount_cents": 1002},
                {"Idempotency-Key": "http:expense:create:first"},
            )
        self.assertEqual(changed_key.exception.code, 409)

        second = self.post(chao, f"/trips/{trip_id}/expenses", {
            "amount_cents": 999, "payer_id": DEMO_PARTNER, "category": "transport",
            "split_mode": "equal", "occurred_on": "2026-11-02", "note": "HTTP 车费",
            "idempotency_key": "http:expense:create:second",
        })
        with tang.open(self.base + f"/trips/{trip_id}/expenses") as response:
            tang_view = json.load(response)
        with chao.open(self.base + f"/trips/{trip_id}/expenses") as response:
            chao_view = json.load(response)
        self.assertEqual({item["id"] for item in tang_view["items"]}, {first["id"], second["id"]})
        self.assertEqual(tang_view["balance"]["net_cents"], -chao_view["balance"]["net_cents"])

        updated = self.patch(tang, f"/trips/{trip_id}/expenses/{first['id']}", {
            "note": "HTTP 晚餐已修正", "expected_version": first["version"],
            "idempotency_key": "http:expense:update:first",
        })
        with self.assertRaises(urllib.error.HTTPError) as stale_edit:
            self.patch(chao, f"/trips/{trip_id}/expenses/{first['id']}", {
                "note": "过期修改", "expected_version": first["version"],
                "idempotency_key": "http:expense:update:stale",
            })
        self.assertEqual(stale_edit.exception.code, 409)

        deleted = self.delete(chao, f"/trips/{trip_id}/expenses/{first['id']}", {
            "expected_version": updated["version"], "idempotency_key": "http:expense:delete:first",
        })
        restored = self.post(tang, f"/trips/{trip_id}/expenses/{first['id']}/restore", {
            "expected_version": deleted["version"], "idempotency_key": "http:expense:restore:first",
        })
        with self.assertRaises(urllib.error.HTTPError) as atomic_failure:
            self.post(tang, f"/trips/{trip_id}/expenses/settle", {
                "expenses": [
                    {"id": first["id"], "expected_version": restored["version"]},
                    {"id": second["id"], "expected_version": 99},
                ],
                "idempotency_key": "http:expense:settle:stale",
            })
        self.assertEqual(atomic_failure.exception.code, 409)
        with tang.open(self.base + f"/trips/{trip_id}/expenses") as response:
            after_failure = json.load(response)
        self.assertEqual(after_failure["balance"]["unsettled_count"], 2)

        settled = self.post(tang, f"/trips/{trip_id}/expenses/settle", {
            "expenses": [
                {"id": first["id"], "expected_version": restored["version"]},
                {"id": second["id"], "expected_version": second["version"]},
            ],
            "idempotency_key": "http:expense:settle:valid",
        })
        self.assertEqual(settled["settled_count"], 2)
        with chao.open(self.base + f"/trips/{trip_id}/expenses") as response:
            settled_view = json.load(response)
        self.assertEqual(settled_view["balance"]["net_cents"], 0)
        self.assertEqual(len([item for item in settled_view["items"] if item["settled_at"]]), 2)

        with self.assertRaises(urllib.error.HTTPError) as invalid_amount:
            self.post(tang, f"/trips/{trip_id}/expenses", {
                **first_payload, "amount_cents": 12.34,
            }, {"Idempotency-Key": "http:expense:create:invalid"})
        self.assertEqual(invalid_amount.exception.code, 400)
    def test_memory_search_and_completion_http_contract_preserves_visibility(self) -> None:
        tang, chao = self.opener(), self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        self.post(chao, "/auth/login", {"username": "test1", "password": "test123"})
        marker = f"HTTP回忆-{uuid.uuid4().hex}"
        shared = main.STORE.create_moment({
            "title": marker, "body": "双方可查正文", "visibility": "shared",
            "occurred_at": "2024-02-14T12:00:00+00:00",
        }, DEMO_USER)
        private = main.STORE.create_moment({
            "title": marker + "-私密", "body": "不可泄露", "visibility": "private",
            "occurred_at": "2024-02-14T13:00:00+00:00",
        }, DEMO_USER)

        partner_result = self.post(chao, "/memories/search", {"q": marker, "limit": 20})
        self.assertEqual([item["resource_id"] for item in partner_result["items"]], [shared["id"]])
        self.assertEqual(partner_result["total"], len(partner_result["items"]))
        self.assertNotIn(private["id"], json.dumps(partner_result, ensure_ascii=False))

        private_phrase = f"私密描述-{uuid.uuid4().hex}"
        saved = self.put(tang, f"/memories/moment/{shared['id']}/descriptions/me", {
            "body": private_phrase, "visibility": "private", "expected_version": 0,
        })
        self.assertEqual(saved["visibility"], "private")
        hidden_description = self.post(chao, "/memories/search", {"q": private_phrase})
        self.assertEqual(hidden_description["total"], 0)
        own_description = self.post(tang, "/memories/search", {"q": private_phrase})
        self.assertEqual([item["resource_id"] for item in own_description["items"]], [shared["id"]])

        refreshed = self.post(tang, "/memories/completion/refresh", {})
        self.assertIn("created", refreshed)
        with tang.open(self.base + "/memories/completion") as response:
            completion = json.load(response)
        self.assertIn("items", completion)

        with main.STORE.connection() as db:
            db.execute("DELETE FROM memory_completion_suggestions WHERE moment_id IN (?,?)", (shared["id"], private["id"]))
            db.execute("DELETE FROM memory_descriptions WHERE moment_id IN (?,?)", (shared["id"], private["id"]))
            db.execute("DELETE FROM memory_provenance WHERE moment_id IN (?,?)", (shared["id"], private["id"]))
            db.execute("DELETE FROM moments WHERE id IN (?,?)", (shared["id"], private["id"]))

    def test_memory_search_query_is_redacted_from_access_logs(self) -> None:
        tang = self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        secret = f"私密检索词-{uuid.uuid4().hex}"
        encoded = urllib.parse.quote_plus(secret)
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            with tang.open(self.base + f"/memories/search?q={encoded}") as response:
                json.load(response)
            result = self.post(tang, "/memories/search", {"q": secret, "limit": 20})
        self.assertIn("items", result)
        logs = captured.getvalue()
        self.assertNotIn(secret, logs)
        self.assertNotIn(encoded, logs)
        self.assertNotIn("?q=", logs)

    def test_voice_memory_and_future_letter_http_contract(self) -> None:
        tang, chao = self.opener(), self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        self.post(chao, "/auth/login", {"username": "test1", "password": "test123"})
        media = main.STORE.save_media(
            b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " +
            (16).to_bytes(4, "little") + b"\x01\x00\x01\x00\x40\x1f\x00\x00\x80\x3e\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00",
            "http-voice.wav", "audio/wav", "private", DEMO_USER,
        )
        voice = self.post(tang, "/voice-memories", {
            "media_id": media["id"], "title": "HTTP 语音回忆", "note": "",
            "occurred_at": "2026-10-27T20:30:00+08:00", "location": "", "mood": "想念",
            "visibility": "shared", "reveal_at": None, "duration_ms": 1200,
            "transcript": {"body": "可校正的转写", "language": "zh-CN", "method": "manual", "status": "ready"},
        }, {"Idempotency-Key": f"http-voice-{uuid.uuid4().hex}"})
        with chao.open(self.base + f"/voice-memories/{voice['moment']['id']}") as response:
            partner_voice = json.load(response)
        self.assertEqual(partner_voice["transcript"]["body"], "可校正的转写")

        letter = self.post(tang, "/future-letters", {
            "title": "HTTP 未来信", "body": "暂不公开",
        }, {"Idempotency-Key": f"http-letter-{uuid.uuid4().hex}"})
        deliver_at = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
        sealed = self.post(tang, f"/future-letters/{letter['id']}/seal", {
            "deliver_at": deliver_at, "expected_version": letter["version"],
        })
        self.assertIsNone(sealed["body"])
        with chao.open(self.base + "/future-letters?scope=delivered") as response:
            self.assertEqual(json.load(response)["total"], 0)
        cancelled = self.post(tang, f"/future-letters/{letter['id']}/cancel", {
            "expected_version": sealed["version"],
        })
        self.assertEqual(cancelled["body"], "暂不公开")

    def test_album_export_http_preview_job_and_protected_download(self) -> None:
        tang, chao = self.opener(), self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        self.post(chao, "/auth/login", {"username": "test1", "password": "test123"})
        moment = main.STORE.create_moment({
            "title": "HTTP 纪念册", "body": "只走受保护的本地 PDF", "visibility": "shared",
            "occurred_at": "2024-02-14T12:00:00+00:00",
        }, DEMO_USER)
        preview = self.post(tang, "/album-exports/preview", {
            "template": "year", "year": 2024, "audience": "shared",
        })
        chosen = next(item for item in preview["items"] if item["resource_id"] == moment["id"])
        job = self.post(tang, "/album-exports", {
            "template": "year", "year": 2024, "audience": "shared",
            "selection": [{"resource_type": "moment", "resource_id": moment["id"]}],
            "expected_versions": {f"moment:{moment['id']}": chosen["version"]},
        }, {"Idempotency-Key": f"http-album-{uuid.uuid4().hex}"})
        self.assertEqual(job["status"], "queued")
        for malformed_version in (True, 1.0, "1", None):
            with self.subTest(expected_version=malformed_version):
                request = urllib.request.Request(
                    self.base + f"/album-exports/{job['id']}/cancel",
                    data=json.dumps({"expected_version": malformed_version}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as invalid:
                    tang.open(request)
                self.assertEqual(invalid.exception.code, 400)
                invalid.exception.close()
        claimed = main.STORE.claim_album_export_job()
        completed = main.STORE.run_album_export_job(claimed)
        self.assertEqual(completed["status"], "completed")

        request = urllib.request.Request(self.base + f"/album-exports/{job['id']}/download")
        with tang.open(request) as response:
            data = response.read()
            self.assertEqual(response.headers.get_content_type(), "application/pdf")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["Cross-Origin-Resource-Policy"], "same-origin")
            self.assertTrue(data.startswith(b"%PDF-"))
        with self.assertRaises(urllib.error.HTTPError) as denied:
            chao.open(self.base + f"/album-exports/{job['id']}")
        self.assertEqual(denied.exception.code, 404)
        denied.exception.close()
        with main.STORE.connection() as db:
            db.execute("DELETE FROM album_export_jobs WHERE id=?", (job["id"],))
            db.execute("DELETE FROM memory_provenance WHERE moment_id=?", (moment["id"],))
            db.execute("DELETE FROM moments WHERE id=?", (moment["id"],))
        main.STORE._delete_album_artifacts(job["id"])

    def test_memory_merge_http_binds_proposal_id_and_returns_public_dto(self) -> None:
        tang, chao = self.opener(), self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        self.post(chao, "/auth/login", {"username": "test1", "password": "test123"})
        moment = main.STORE.create_moment({
            "title": "HTTP 合并", "body": "旧正文", "visibility": "shared",
            "occurred_at": "2024-02-14T12:00:00+00:00",
        }, DEMO_USER)
        first = self.put(tang, f"/memories/moment/{moment['id']}/descriptions/me", {
            "body": "演示甲的视角", "visibility": "shared", "expected_version": 0,
        })
        second = self.put(chao, f"/memories/moment/{moment['id']}/descriptions/me", {
            "body": "演示乙的视角", "visibility": "shared", "expected_version": 0,
        })
        proposal = self.post(tang, f"/memories/moments/{moment['id']}/merge-proposal", {
            "body": "共同确认的正文", "expected_parent_version": moment["version"],
            "expected_versions": {DEMO_USER: first["version"], DEMO_PARTNER: second["version"]},
        })
        confirmed = self.post(chao, f"/memories/moments/{moment['id']}/merge-confirm", {
            "proposal_id": proposal["proposal_id"], "expected_version": proposal["version"],
        })
        self.assertEqual(confirmed["body"], "共同确认的正文")
        for internal in ("couple_id", "creator_id", "deleted_at", "purged_at", "media_id", "source_message_id"):
            self.assertNotIn(internal, confirmed)
        with main.STORE.connection() as db:
            db.execute("DELETE FROM memory_descriptions WHERE moment_id=?", (moment["id"],))
            db.execute("DELETE FROM memory_provenance WHERE moment_id=?", (moment["id"],))
            db.execute("DELETE FROM moments WHERE id=?", (moment["id"],))

    def test_decision_http_contract_is_shared_versioned_and_private_until_reveal(self) -> None:
        tang, chao = self.opener(), self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        self.post(chao, "/auth/login", {"username": "test1", "password": "test123"})
        deadline = (datetime.now(timezone.utc) + timedelta(hours=6)).replace(microsecond=0).isoformat()

        created = self.post(tang, "/decisions", {
            "title": "HTTP 周末安排", "selection_mode": "single",
            "options": ["看展", "散步"], "closes_at": deadline,
        })
        decision_id = created["id"]
        with chao.open(self.base + "/decisions") as response:
            self.assertIn(decision_id, {item["id"] for item in json.load(response)["items"]})
        with chao.open(self.base + f"/decisions/{decision_id}") as response:
            self.assertEqual(json.load(response)["title"], "HTTP 周末安排")

        changed = self.patch(tang, f"/decisions/{decision_id}", {
            "title": "HTTP 周日安排", "expected_version": 1,
        })
        selected = changed["options"][0]["id"]
        first_request = urllib.request.Request(
            self.base + f"/decisions/{decision_id}/vote",
            data=json.dumps({
                "option_ids": [selected], "expected_version": changed["version"],
                "idempotency_key": "payload-key-must-not-win",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json", "Idempotency-Key": "header-vote-key-001"},
            method="PUT",
        )
        with tang.open(first_request) as response:
            hidden = json.load(response)
        self.assertNotIn("votes", hidden)
        self.assertNotIn("resolution", hidden)

        revealed = self.put(chao, f"/decisions/{decision_id}/vote", {
            "option_ids": [selected], "expected_version": hidden["version"],
            "idempotency_key": "partner-vote-key-001",
        })
        self.assertEqual(revealed["resolution"]["kind"], "agreement")

        cancellable = self.post(chao, "/decisions", {
            "title": "HTTP 可取消", "selection_mode": "multiple",
            "options": ["甲", "乙"], "closes_at": deadline,
        })
        cancelled = self.delete(tang, f"/decisions/{cancellable['id']}", {"expected_version": 1})
        self.assertEqual(cancelled["status"], "cancelled")

        stale = urllib.request.Request(
            self.base + f"/decisions/{decision_id}",
            data=json.dumps({"title": "过期修改", "expected_version": 1}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="PATCH",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            tang.open(stale)
        self.assertEqual(caught.exception.code, 409)
        caught.exception.close()

    def test_private_vote_emits_nothing_and_reveal_uses_generic_realtime_http_and_sse(self) -> None:
        tang, chao = self.opener(), self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        self.post(chao, "/auth/login", {"username": "test1", "password": "test123"})
        with chao.open(self.base + "/realtime/events?after=0") as response:
            cursor = json.load(response)["latest_sequence"]
        deadline = (datetime.now(timezone.utc) + timedelta(hours=6)).replace(microsecond=0).isoformat()
        decision = self.post(tang, "/decisions", {
            "title": "实时隐私测试", "selection_mode": "single",
            "options": ["绝密路线甲", "绝密路线乙"], "closes_at": deadline,
        })
        selected = decision["options"][1]["id"]
        self.put(tang, f"/decisions/{decision['id']}/vote", {
            "option_ids": [selected], "expected_version": 1,
            "idempotency_key": "http-private-sse-vote",
        })
        with chao.open(self.base + f"/realtime/events?after={cursor}") as response:
            payload = json.load(response)
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertIn(decision["id"], serialized)
        self.assertNotIn("decision.vote_submitted", serialized)
        self.assertNotIn("decision.revealed", serialized)
        self.assertNotIn(selected, serialized)
        self.assertNotIn("绝密路线乙", serialized)
        self.assertNotIn("option", serialized.casefold())

        private_cursor = payload["latest_sequence"]
        self.put(chao, f"/decisions/{decision['id']}/vote", {
            "option_ids": [decision["options"][0]["id"]], "expected_version": 1,
            "idempotency_key": "http-private-sse-reveal",
        })
        with chao.open(self.base + f"/realtime/events?after={private_cursor}") as response:
            revealed_payload = json.load(response)
        revealed_serialized = json.dumps(revealed_payload, ensure_ascii=False)
        self.assertIn("decision.revealed", revealed_serialized)
        self.assertNotIn(selected, revealed_serialized)
        self.assertNotIn("绝密路线乙", revealed_serialized)
        self.assertNotIn("option", revealed_serialized.casefold())

        with chao.open(self.base + f"/realtime/stream?after={private_cursor}", timeout=3) as response:
            lines = []
            for _ in range(30):
                line = response.readline().decode("utf-8").strip()
                lines.append(line)
                if line.startswith("data:") and "decision.revealed" in "\n".join(lines):
                    break
        stream = "\n".join(lines)
        self.assertIn("event: decision.revealed", stream)
        self.assertNotIn(selected, stream)
        self.assertNotIn("绝密路线乙", stream)
        self.assertNotIn("option", stream.casefold())

    def test_confirmed_decision_conversion_http_is_repeatable_pure_preview(self) -> None:
        tang, chao = self.opener(), self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        self.post(chao, "/auth/login", {"username": "test1", "password": "test123"})
        deadline = (datetime.now(timezone.utc) + timedelta(hours=6)).replace(microsecond=0).isoformat()
        decision = self.post(tang, "/decisions", {
            "title": "HTTP 转换预填", "selection_mode": "single",
            "options": ["菌汤火锅", "家常炒菜"], "closes_at": deadline,
        })
        selected = decision["options"][0]["id"]
        self.put(tang, f"/decisions/{decision['id']}/vote", {
            "option_ids": [selected], "expected_version": decision["version"],
            "idempotency_key": f"http-conversion-a-{uuid.uuid4().hex}",
        })
        revealed = self.put(chao, f"/decisions/{decision['id']}/vote", {
            "option_ids": [selected], "expected_version": decision["version"],
            "idempotency_key": f"http-conversion-b-{uuid.uuid4().hex}",
        })
        pending = self.post(chao, f"/decisions/{decision['id']}/resolve", {
            "action": "confirm", "option_id": selected,
            "expected_version": revealed["version"],
        })
        confirmed = self.post(tang, f"/decisions/{decision['id']}/resolve", {
            "action": "confirm", "option_id": selected,
            "expected_version": pending["version"],
        })
        self.assertEqual(confirmed["status"], "confirmed")

        with main.STORE.connection() as db:
            before = {
                "meal_wishes": db.execute("SELECT COUNT(*) FROM meal_wishes").fetchone()[0],
                "calendar": db.execute("SELECT COUNT(*) FROM calendar_entries").fetchone()[0],
                "audit": db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0],
                "events": db.execute("SELECT COUNT(*) FROM realtime_events").fetchone()[0],
            }
        first = self.post(tang, f"/decisions/{decision['id']}/conversion-preview", {
            "target": "meal_wish", "option_id": selected,
        })
        second = self.post(tang, f"/decisions/{decision['id']}/conversion-preview", {
            "target": "meal_wish", "option_id": selected,
        })
        self.assertEqual(first, second)
        self.assertFalse(first["saved"])
        self.assertTrue(first["requires_confirmation"])
        self.assertEqual(first["values"]["title"], "菌汤火锅")
        self.assertIn("尚未保存", first["notice"])
        with main.STORE.connection() as db:
            after = {
                "meal_wishes": db.execute("SELECT COUNT(*) FROM meal_wishes").fetchone()[0],
                "calendar": db.execute("SELECT COUNT(*) FROM calendar_entries").fetchone()[0],
                "audit": db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0],
                "events": db.execute("SELECT COUNT(*) FROM realtime_events").fetchone()[0],
            }
        self.assertEqual(after, before)
        saved = self.post(tang, "/meal-wishes", {
            "title": first["values"]["title"], "note": first["values"]["note"],
        })
        self.assertEqual(saved["title"], "菌汤火锅")
        with main.STORE.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM meal_wishes").fetchone()[0], before["meal_wishes"] + 1)
        # Keep this class-level HTTP database isolated for later meal tests.
        # The ordinary delete endpoint is part of the same public lifecycle.
        self.delete(tang, f"/meal-wishes/{saved['id']}", {"expected_version": saved["version"]})

    def test_decision_http_extend_and_resolve_routes_preserve_state_machine(self) -> None:
        tang, chao = self.opener(), self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        self.post(chao, "/auth/login", {"username": "test1", "password": "test123"})
        deadline = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(microsecond=0).isoformat()
        decision = self.post(tang, "/decisions", {
            "title": "HTTP 处理分歧", "selection_mode": "single",
            "options": ["甲", "乙", "丙"], "closes_at": deadline,
        })
        ids = [item["id"] for item in decision["options"]]
        one = self.put(tang, f"/decisions/{decision['id']}/vote", {
            "option_ids": [ids[0]], "expected_version": 1,
            "idempotency_key": "http-resolve-vote-01",
        })
        split = self.put(chao, f"/decisions/{decision['id']}/vote", {
            "option_ids": [ids[1]], "expected_version": one["version"],
            "idempotency_key": "http-resolve-vote-02",
        })
        restarted = self.post(tang, f"/decisions/{decision['id']}/resolve", {
            "action": "revote", "expected_version": split["version"],
        })
        self.assertEqual(restarted["voting_round"], 2)
        with main.STORE.connection() as db:
            db.execute(
                "UPDATE decisions SET closes_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (decision["id"],),
            )
        extended = self.post(chao, f"/decisions/{decision['id']}/extend", {
            "deadline_at": (datetime.now(timezone.utc) + timedelta(hours=8)).replace(microsecond=0).isoformat(),
            "expected_version": restarted["version"],
        })
        self.assertEqual(extended["status"], "open")

    def test_decision_http_revote_rejects_candidate_replacement_without_side_effects(self) -> None:
        tang, chao = self.opener(), self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        self.post(chao, "/auth/login", {"username": "test1", "password": "test123"})
        deadline = (datetime.now(timezone.utc) + timedelta(hours=6)).replace(microsecond=0).isoformat()
        decision = self.post(tang, "/decisions", {
            "title": "HTTP 候选永久锁定", "selection_mode": "single",
            "options": ["原候选甲", "原候选乙", "原候选丙"], "closes_at": deadline,
        })
        ids = [item["id"] for item in decision["options"]]
        self.put(tang, f"/decisions/{decision['id']}/vote", {
            "option_ids": [ids[0]], "expected_version": 1,
            "idempotency_key": "http-permanent-lock-round-one-a",
        })
        revealed = self.put(chao, f"/decisions/{decision['id']}/vote", {
            "option_ids": [ids[1]], "expected_version": 1,
            "idempotency_key": "http-permanent-lock-round-one-b",
        })
        restarted = self.post(tang, f"/decisions/{decision['id']}/resolve", {
            "action": "revote", "expected_version": revealed["version"],
        })
        renamed = self.patch(tang, f"/decisions/{decision['id']}", {
            "title": "HTTP 只改安全元数据", "expected_version": restarted["version"],
        })

        def snapshot() -> dict:
            with main.STORE.connection() as db:
                return {
                    "root": tuple(db.execute(
                        "SELECT title,selection_mode,version,voting_round,options_locked_at FROM decisions WHERE id=?",
                        (decision["id"],),
                    ).fetchone()),
                    "options": [tuple(row) for row in db.execute(
                        "SELECT id,label,position FROM decision_options WHERE decision_id=? ORDER BY position",
                        (decision["id"],),
                    ).fetchall()],
                    "votes": [tuple(row) for row in db.execute(
                        "SELECT voting_round,user_id,option_id FROM decision_votes WHERE decision_id=? ORDER BY voting_round,user_id,option_id",
                        (decision["id"],),
                    ).fetchall()],
                    "audit": db.execute(
                        "SELECT COUNT(*) FROM audit_events WHERE target_id=?", (decision["id"],),
                    ).fetchone()[0],
                    "events": db.execute(
                        "SELECT COUNT(*) FROM realtime_events WHERE resource_id=?", (decision["id"],),
                    ).fetchone()[0],
                    "inbox": db.execute(
                        "SELECT COUNT(*) FROM in_app_notifications WHERE resource_id=?", (decision["id"],),
                    ).fetchone()[0],
                }

        before = snapshot()
        with self.assertRaises(urllib.error.HTTPError) as locked:
            self.patch(tang, f"/decisions/{decision['id']}", {
                "options": ["替换甲", "替换乙"], "expected_version": renamed["version"],
            })
        self.assertEqual(locked.exception.code, 400)
        locked.exception.close()
        self.assertEqual(snapshot(), before)
        with tang.open(self.base + f"/decisions/{decision['id']}") as response:
            self.assertTrue(json.load(response)["options_locked"])

        with self.assertRaises(urllib.error.HTTPError) as stale:
            self.patch(tang, f"/decisions/{decision['id']}", {
                "options": ["替换甲", "替换乙"], "expected_version": restarted["version"],
            })
        self.assertEqual(stale.exception.code, 409)
        stale.exception.close()
        self.assertEqual(snapshot(), before)

        self.put(tang, f"/decisions/{decision['id']}/vote", {
            "option_ids": [ids[2]], "expected_version": renamed["version"],
            "idempotency_key": "http-permanent-lock-round-two-a",
        })
        round_two = self.put(chao, f"/decisions/{decision['id']}/vote", {
            "option_ids": [ids[0]], "expected_version": renamed["version"],
            "idempotency_key": "http-permanent-lock-round-two-b",
        })
        self.assertEqual(
            {tuple(item["option_ids"]) for item in round_two["votes"]},
            {(ids[0],), (ids[2],)},
        )
        with main.STORE.connection() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM decision_votes WHERE decision_id=? AND voting_round=1",
                (decision["id"],),
            ).fetchone()[0], 2)

    def test_travel_assistant_is_shared_between_both_accounts(self) -> None:
        tang = self.opener()
        chao = self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        self.post(chao, "/auth/login", {"username": "test1", "password": "test123"})
        with tang.open(self.base + "/trips") as response:
            trip_id = json.load(response)["items"][0]["id"]
        asked = self.post(tang, f"/trips/{trip_id}/assistant/messages", {
            "message": "比较五套方案哪个更轻松",
            "idempotency_key": f"http-assistant-{uuid.uuid4().hex}",
        })
        self.assertEqual(asked["assistant"]["intent_type"], "plan_compare")
        self.assertEqual(len(asked["assistant"]["cards"]), 5)
        with chao.open(self.base + f"/trips/{trip_id}/assistant") as response:
            shared = json.load(response)
        self.assertEqual(shared["items"][-1]["id"], asked["assistant"]["id"])
        self.assertEqual(shared["items"][-2]["author_name"], "演示甲")

    def test_live_trip_context_is_ephemeral_shared_and_routes_are_on_demand(self) -> None:
        tang, chao = self.opener(), self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        self.post(chao, "/auth/login", {"username": "test1", "password": "test123"})
        today = datetime.now(timezone.utc).astimezone().date().isoformat()
        trip = main.STORE.create_trip({
            "destination": "上海", "start_date": today, "end_date": today,
            "budget_cents": 200000, "departure_city": "杭州", "arrival_city": "上海",
        }, DEMO_USER)
        active = next(item for item in trip["variants"] if item["style"] == trip["active_variant"])
        item = active["days"][0]["items"][0]
        with main.STORE.connection() as db:
            db.execute("UPDATE itinerary_items SET start_time='23:59' WHERE id=?", (item["id"],))
            db.execute(
                "INSERT INTO itinerary_item_places(itinerary_item_id,trip_id,provider,provider_place_id,name,"
                "formatted_address,country,province,city,district,latitude,longitude,category,confirmed_by,confirmed_at) "
                "VALUES (?,?, 'amap','POI-LIVE','人民广场','上海市黄浦区','中国','上海市','上海市','黄浦区',"
                "31.2304,121.4737,'风景名胜',?,datetime('now'))",
                (item["id"], trip["id"], DEMO_USER),
            )

        class FakeMap:
            name, is_mock = "amap", False

            def __init__(self):
                self.route_calls = 0
                self.food_calls = 0

            def search_food_nearby(self, keyword, latitude, longitude, city="", limit=5):
                del keyword, city, limit
                self.food_calls += 1
                return [{
                    "amap_poi_id": "FOOD-1", "name": "真实附近餐厅", "address": "步行可达",
                    "rating": 4.8, "average_cost_cents": 7600, "opening_hours": "10:00-22:00",
                    "latitude": latitude + .001, "longitude": longitude + .001,
                    "distance_meters": 180, "navigation_url": "https://uri.amap.com/marker?x=1",
                    "source": "amap", "mock": False,
                }]

            def route_between(self, origin, destination, mode="walking", city=""):
                del city
                self.route_calls += 1
                return {
                    "origin": origin, "destination": destination, "mode": mode,
                    "distance_meters": 880, "duration_seconds": 720, "steps": [],
                    "provider": "amap", "mock": False,
                }

        class FakeWeather:
            def current(self, latitude, longitude, timezone_name="auto"):
                del latitude, longitude, timezone_name
                return {"weather_code": 1, "label": "大致晴朗", "source": "Open-Meteo"}

        fake_map = FakeMap()
        observed = datetime.now(timezone.utc).replace(microsecond=0)
        live_context = {
            "latitude": 31.123456, "longitude": 121.234567, "accuracy_meters": 20,
            "observed_at": observed.isoformat(), "local_time": observed.astimezone().isoformat(),
        }
        with patch.object(main.STORE, "_map_provider_for_places", return_value=fake_map), patch.object(main, "WEATHER", FakeWeather()):
            first = self.post(tang, f"/trips/{trip['id']}/assistant/live", {
                "live_context": live_context, "include_restaurants": True,
            })
            second = self.post(chao, f"/trips/{trip['id']}/assistant/live", {
                "live_context": live_context, "include_restaurants": True,
            })
            self.assertEqual(first["candidates"], second["candidates"])
            self.assertEqual(first["restaurants"][0]["name"], "真实附近餐厅")
            self.assertEqual(first["restaurants"][0]["source_label"], "高德地点实时查询")
            self.assertEqual(second["restaurants"][0]["source_label"], "高德地点缓存")
            self.assertEqual(fake_map.food_calls, 1, "同一位置的餐厅查询应复用缓存，避免重复消耗地图额度")
            self.assertEqual(fake_map.route_calls, 0)
            route = self.post(tang, f"/trips/{trip['id']}/assistant/navigation", {
                "live_context": live_context, "item_id": item["id"], "mode": "walking",
            })
            self.assertEqual(route["route"]["distance_meters"], 880)
            self.assertEqual(fake_map.route_calls, 1)

        with main.STORE.connection() as db:
            persisted = " ".join(
                str(value or "")
                for table, column in (
                    ("trip_assistant_messages", "cards_json"),
                    ("audit_events", "detail"),
                    ("realtime_events", "payload_json"),
                )
                for value, in db.execute(f"SELECT {column} FROM {table}").fetchall()
            )
        self.assertNotIn("31.123456", persisted)
        self.assertNotIn("121.234567", persisted)

    def test_live_trip_map_fallback_is_explicit_and_does_not_invent_restaurants(self) -> None:
        opener = self.opener()
        self.post(opener, "/auth/login", {"username": "test", "password": "test123"})
        with opener.open(self.base + "/trips") as response:
            trip_id = json.load(response)["items"][0]["id"]
        observed = datetime.now(timezone.utc).replace(microsecond=0)
        payload = {
            "live_context": {
                "latitude": 30.2741, "longitude": 120.1551, "accuracy_meters": 30,
                "observed_at": observed.isoformat(), "local_time": observed.astimezone().isoformat(),
            },
            "include_restaurants": True,
        }
        result = self.post(opener, f"/trips/{trip_id}/assistant/live", payload)
        self.assertFalse(result["map_configured"])
        self.assertEqual(result["restaurants"], [])
        self.assertIn("未配置", result["restaurant_notice"])

    def test_travel_assistant_unifies_hotel_flight_and_train_sources(self) -> None:
        opener = self.opener()
        self.post(opener, "/auth/login", {"username": "test", "password": "test123"})
        with opener.open(self.base + "/trips") as response:
            trip_id = json.load(response)["items"][0]["id"]

        def quote_result(payload, user_id):
            del user_id
            quote_type = payload["type"]
            mode = {"hotel": "live", "flight": "cached", "train": "manual"}[quote_type]
            row = {
                "id": f"quote-{quote_type}", "quote_type": quote_type, "title": quote_type,
                "subtitle": "", "amount_cents": 10000, "currency": "CNY", "provider": "provider",
                "source_mode": mode, "terms": "", "includes_tax": True, "booking_url": "",
                "fetched_at": "2026-10-27T00:00:00+00:00", "stale": mode == "cached",
            }
            return {"items": [row], "notice": f"{quote_type} notice"}

        with patch.object(main.STORE, "search_quotes", side_effect=quote_result) as searcher:
            result = self.post(opener, f"/trips/{trip_id}/assistant/messages", {
                "message": "统一比较酒店、航班和火车票",
                "idempotency_key": f"transport-{uuid.uuid4().hex}",
            })
        cards = result["assistant"]["cards"]
        self.assertEqual([card["quote_type"] for card in cards], ["hotel", "flight", "train"])
        self.assertEqual([card["data_kind"] for card in cards], ["real_time", "cache", "manual"])
        self.assertTrue(all(card["fetched_at"] for card in cards))
        self.assertEqual(searcher.call_count, 3)

    def test_trip_recap_draft_is_shared_and_confirmed_once_over_http(self) -> None:
        tang, chao = self.opener(), self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        self.post(chao, "/auth/login", {"username": "test1", "password": "test123"})
        trip_id = DEMO_TRIP
        draft = self.post(tang, f"/trips/{trip_id}/assistant/recap", {})
        with chao.open(self.base + f"/trips/{trip_id}/assistant/recap") as response:
            shared = json.load(response)
        self.assertEqual(shared["items"][0]["id"], draft["id"])
        key = f"recap-{uuid.uuid4().hex}"
        first = self.post(chao, f"/trips/{trip_id}/assistant/recap/{draft['id']}/confirm", {
            "idempotency_key": key,
        })
        second = self.post(tang, f"/trips/{trip_id}/assistant/recap/{draft['id']}/confirm", {
            "idempotency_key": key,
        })
        self.assertEqual(first["moment"]["id"], second["moment"]["id"])

    def test_closed_registration_is_reported_and_rejected(self) -> None:
        previous_mode = os.environ.get("REGISTRATION_MODE")
        os.environ["REGISTRATION_MODE"] = "closed"
        try:
            with urllib.request.urlopen(self.base + "/auth/me") as response:
                auth_state = json.load(response)
            self.assertFalse(auth_state["registration_enabled"])

            request = urllib.request.Request(
                self.base + "/auth/register",
                data=json.dumps({
                    "username": f"closed_{uuid.uuid4().hex[:8]}",
                    "password": "closed-register-123",
                    "nickname": "不可注册",
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request)
            self.assertEqual(raised.exception.code, 403)
            raised.exception.close()
        finally:
            if previous_mode is None:
                os.environ.pop("REGISTRATION_MODE", None)
            else:
                os.environ["REGISTRATION_MODE"] = previous_mode

    def test_private_admin_overview_events_backup_and_origin_protection(self) -> None:
        previous_admins = os.environ.get("ADMIN_USERNAMES")
        os.environ["ADMIN_USERNAMES"] = "test"
        try:
            admin_opener = self.opener()
            partner_opener = self.opener()
            self.post(admin_opener, "/auth/login", {"username": "test", "password": "test123"})
            self.post(partner_opener, "/auth/login", {"username": "test1", "password": "test123"})

            with admin_opener.open(self.base + "/admin/overview") as response:
                overview = json.load(response)
            self.assertEqual(overview["runtime"]["schema_version"], SCHEMA_VERSION)
            self.assertIn("checks", overview)
            self.assertNotIn("password_hash", json.dumps(overview))
            self.assertNotIn("token_hash", json.dumps(overview))

            try:
                partner_opener.open(self.base + "/admin/overview")
            except urllib.error.HTTPError as error:
                self.assertEqual(error.code, 403)
                error.close()
            else:
                self.fail("非管理员不应读取后台")

            failed = urllib.request.Request(
                self.base + "/auth/login",
                data=json.dumps({"username": "test", "password": "wrong-password"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(failed)
            except urllib.error.HTTPError as error:
                self.assertEqual(error.code, 400)
                error.close()
            else:
                self.fail("错误密码必须被拒绝")
            with admin_opener.open(self.base + "/admin/overview") as response:
                after_failure = json.load(response)
            self.assertTrue(any(item["category"] == "authentication" for item in after_failure["events"]))

            backup = self.post(admin_opener, "/admin/backups", {})
            self.assertTrue(backup["created"])
            self.assertGreaterEqual(backup["summary"]["count"], 1)
            verified = self.post(admin_opener, f"/admin/backups/{backup['backup']}/verify", {})
            self.assertTrue(verified["verification"]["ok"])

            evil = urllib.request.Request(
                self.base + "/messages",
                data=json.dumps({"type": "text", "content": "不应写入"}).encode(),
                headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
                method="POST",
            )
            try:
                admin_opener.open(evil)
            except urllib.error.HTTPError as error:
                self.assertEqual(error.code, 403)
                error.close()
            else:
                self.fail("非法来源写请求必须被拒绝")
        finally:
            if previous_admins is None:
                os.environ.pop("ADMIN_USERNAMES", None)
            else:
                os.environ["ADMIN_USERNAMES"] = previous_admins

    def test_registered_user_can_change_password(self) -> None:
        opener = self.opener()
        self.post(opener, "/auth/register", {"username": "password_user", "password": "old-password-123", "nickname": "改密"})
        changed = self.post(opener, "/account/password", {
            "current_password": "old-password-123",
            "new_password": "new-password-456",
            "new_password_confirmation": "new-password-456",
        })
        self.assertTrue(changed["changed"])
        self.post(opener, "/auth/logout", {})
        logged_in = self.post(opener, "/auth/login", {"username": "password_user", "password": "new-password-456"})
        self.assertEqual(logged_in["user"]["username"], "password_user")

    def test_authenticated_user_can_update_only_their_avatar_label(self) -> None:
        opener = self.opener()
        username = f"avatar_{uuid.uuid4().hex[:10]}"
        registered = self.post(opener, "/auth/register", {
            "username": username,
            "password": "avatar-password-123",
            "nickname": "头像测试",
        })
        updated = self.put(opener, "/account/profile", {"avatar_label": "🌙"})
        self.assertEqual(updated["user"]["id"], registered["user"]["id"])
        self.assertEqual(updated["user"]["avatar_label"], "🌙")
        with opener.open(self.base + "/auth/me") as response:
            current = json.load(response)
        self.assertEqual(current["user"]["avatar_label"], "🌙")
        themed = self.put(opener, "/account/profile", {"ui_theme": "daniel"})
        self.assertEqual(themed["user"]["ui_theme"], "daniel")
        display = self.put(opener, "/account/profile", {
            "ui_text_size": "large", "ui_sticker_density": "minimal",
        })
        self.assertEqual(display["user"]["ui_text_size"], "large")
        self.assertEqual(display["user"]["ui_sticker_density"], "minimal")
        with opener.open(self.base + "/auth/me") as response:
            current = json.load(response)
        self.assertEqual(current["user"]["ui_text_size"], "large")
        self.assertEqual(current["user"]["ui_sticker_density"], "minimal")

    def test_web_push_subscription_test_and_call_dispatch(self) -> None:
        caller, callee = self.opener(), self.opener()
        self.post(caller, "/auth/login", {"username": "test", "password": "test123"})
        self.post(callee, "/auth/login", {"username": "test1", "password": "test123"})
        encode = lambda value: base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
        subscription = {
            "endpoint": "https://web.push.apple.com/QH-http-test-endpoint",
            "keys": {"p256dh": encode(b"\x04" + bytes(range(64))), "auth": encode(bytes(range(16)))},
        }
        with callee.open(self.base + "/push/config") as response:
            config = json.load(response)
        self.assertTrue(config["enabled"])
        self.assertEqual(len(config["public_key"]), 87)
        self.assertIn("聊天、留言或私密正文", config["privacy"])
        registered = self.post(callee, "/push/subscriptions", {"subscription": subscription})
        self.assertTrue(registered["subscribed"])
        self.assertEqual(registered["active_device_count"], 1)
        with callee.open(self.base + "/push/diagnostics") as response_stream:
            diagnostics = json.load(response_stream)
        self.assertTrue(diagnostics["enabled"])
        self.assertEqual(diagnostics["active_device_count"], 1)
        self.assertNotIn("subscription_ciphertext", json.dumps(diagnostics))
        with main.STORE.connection() as db:
            stored = db.execute("SELECT subscription_ciphertext FROM push_subscriptions WHERE endpoint_hash=?", (main.PUSH.endpoint_hash(subscription),)).fetchone()[0]
        self.assertNotIn("web.push.apple.com", stored)

        response = type("Response", (), {"status_code": 201})()
        with patch.object(main.PUSH, "send", return_value=response) as sender:
            result = self.post(callee, "/push/test", {"subscription": subscription})
        self.assertEqual(result, {"delivered": 1, "failed": 0})
        sender.assert_called_once()
        test_payload = sender.call_args.args[1]
        self.assertEqual(test_payload["body"], "消息、共同留言和来电提醒都可以送达这台设备。")

        call = self.post(caller, "/calls", {})
        self.assertEqual(call["push_devices_queued"], 1)
        with main.STORE.connection() as db:
            job = db.execute("SELECT * FROM push_outbox WHERE tag=?", (f"call-{call['id']}",)).fetchone()
        self.assertEqual(job["status"], "pending")
        payload = json.loads(job["payload_json"])
        self.assertEqual(payload["body"], "对方正在呼叫你，点击进入恋屿接听。")
        self.assertNotIn("演示甲", job["payload_json"])
        with patch.object(main.PUSH, "send", return_value=response):
            dispatched = main.process_push_outbox_once()
        self.assertEqual(dispatched["delivered"], 1)
        with main.STORE.connection() as db:
            self.assertEqual(db.execute("SELECT status FROM push_outbox WHERE id=?", (job["id"],)).fetchone()[0], "sent")
        self.post(caller, f"/calls/{call['id']}/cancel", {})
        pending_call = self.post(caller, "/calls", {})
        self.post(caller, f"/calls/{pending_call['id']}/cancel", {})
        with main.STORE.connection() as db:
            pending_status = db.execute(
                "SELECT status FROM push_outbox WHERE tag=?", (f"call-{pending_call['id']}",),
            ).fetchone()[0]
        self.assertEqual(pending_status, "cancelled")
        removed = self.delete(callee, "/push/subscriptions", {"subscription": subscription})
        self.assertTrue(removed["removed"])

    def test_message_read_state_is_consistent_between_http_sessions(self) -> None:
        sender, first_device, second_device = self.opener(), self.opener(), self.opener()
        self.post(sender, "/auth/login", {"username": "test1", "password": "test123"})
        self.post(first_device, "/auth/login", {"username": "test", "password": "test123"})
        self.post(second_device, "/auth/login", {"username": "test", "password": "test123"})
        self.post(first_device, "/messages/read", {})
        created = self.post(sender, "/messages", {"type": "text", "content": "跨设备同步", "idempotency_key": uuid.uuid4().hex})
        with second_device.open(self.base + "/messages/read-state") as response:
            unread = json.load(response)
        self.assertEqual(unread["unread_count"], 1)
        self.assertEqual(unread["first_unread_message_id"], created["id"])
        marked = self.post(first_device, "/messages/read", {})
        self.assertEqual(marked["unread_count"], 0)
        with second_device.open(self.base + "/messages/read-state") as response:
            synced = json.load(response)
        self.assertEqual(synced["unread_count"], 0)

    def test_chat_message_queues_privacy_safe_push_only_for_the_partner(self) -> None:
        sender, recipient = self.opener(), self.opener()
        self.post(sender, "/auth/login", {"username": "test", "password": "test123"})
        self.post(recipient, "/auth/login", {"username": "test1", "password": "test123"})
        self.register_push_device(recipient, "message-recipient")
        secret_text = f"只在聊天里显示-{uuid.uuid4().hex}"
        message_payload = {
            "type": "text", "content": secret_text,
            "idempotency_key": f"push:message:{uuid.uuid4().hex}",
        }
        created = self.post(sender, "/messages", message_payload)
        retried = self.post(sender, "/messages", message_payload)
        self.assertEqual(retried["id"], created["id"])
        with main.STORE.connection() as db:
            jobs = db.execute("SELECT * FROM push_outbox WHERE tag=?", (f"message-{created['id']}",)).fetchall()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["user_id"], "demo-user-b")
        payload = json.loads(jobs[0]["payload_json"])
        self.assertEqual(payload["type"], "message")
        self.assertEqual(payload["url"], "/?screen=chat")
        self.assertNotIn(secret_text, jobs[0]["payload_json"])
        self.assertNotIn("消息里显示", jobs[0]["payload_json"])

    def test_shared_comment_queues_private_detail_link_for_the_partner(self) -> None:
        sender, recipient = self.opener(), self.opener()
        self.post(sender, "/auth/login", {"username": "test", "password": "test123"})
        self.post(recipient, "/auth/login", {"username": "test1", "password": "test123"})
        self.register_push_device(recipient, "comment-recipient")
        wish = self.post(sender, "/lists/wishlist/items", {"text": f"一起完成-{uuid.uuid4().hex[:8]}", "note": "共同计划"})
        secret_comment = f"只在详情中显示-{uuid.uuid4().hex}"
        comment = self.post(sender, f"/shared-content/wishlist_item/{wish['id']}/comments", {"body": secret_comment})
        with main.STORE.connection() as db:
            job = db.execute("SELECT * FROM push_outbox WHERE tag=?", (f"comment-{comment['id']}",)).fetchone()
        self.assertIsNotNone(job)
        self.assertEqual(job["user_id"], "demo-user-b")
        payload = json.loads(job["payload_json"])
        self.assertEqual(payload["type"], "shared_comment")
        self.assertEqual(
            payload["url"],
            f"/?screen=life&shared_type=wishlist_item&shared_id={wish['id']}",
        )
        self.assertNotIn(secret_comment, job["payload_json"])
        self.assertNotIn(wish["text"], job["payload_json"])

    def test_shared_moment_and_reaction_pushes_respect_visibility_and_cancellation(self) -> None:
        tang, chao = self.opener(), self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        self.post(chao, "/auth/login", {"username": "test1", "password": "test123"})
        self.register_push_device(tang, "moment-tang")
        self.register_push_device(chao, "moment-chao")
        shared = self.post(tang, "/moments", {"title": "双方可见的新回忆", "body": "正文不进通知", "visibility": "shared"})
        private = self.post(tang, "/moments", {"title": "只给演示甲看的回忆", "body": "不能泄露", "visibility": "private"})
        delayed = self.post(tang, "/moments", {
            "title": "稍后才公开", "body": "现在不能泄露", "visibility": "delayed",
            "reveal_at": "2099-01-01T00:00:00+00:00",
        })
        with main.STORE.connection() as db:
            shared_job = db.execute("SELECT * FROM push_outbox WHERE tag=?", (f"moment-{shared['id']}",)).fetchone()
            private_job = db.execute("SELECT * FROM push_outbox WHERE tag=?", (f"moment-{private['id']}",)).fetchone()
            delayed_job = db.execute("SELECT * FROM push_outbox WHERE tag=?", (f"moment-{delayed['id']}",)).fetchone()
        self.assertIsNotNone(shared_job)
        self.assertEqual(shared_job["user_id"], "demo-user-b")
        self.assertIsNone(private_job)
        self.assertIsNone(delayed_job)
        self.assertNotIn("双方可见的新回忆", shared_job["payload_json"])

        self.post(chao, f"/moments/{shared['id']}/reaction", {"reaction": "heart"})
        with main.STORE.connection() as db:
            reaction_jobs = db.execute(
                "SELECT * FROM push_outbox WHERE tag=?", (f"moment-reaction-{shared['id']}-demo-user-b",),
            ).fetchall()
        self.assertEqual(len(reaction_jobs), 1)
        self.assertEqual(reaction_jobs[0]["user_id"], "demo-user-a")
        self.post(chao, f"/moments/{shared['id']}/reaction", {"reaction": "none"})
        with main.STORE.connection() as db:
            after_cancel = db.execute(
                "SELECT COUNT(*) FROM push_outbox WHERE tag LIKE ?", (f"moment-reaction-{shared['id']}%",),
            ).fetchone()[0]
        self.assertEqual(after_cancel, 1)

    def test_register_pair_and_use_shared_space(self) -> None:
        first, second = self.opener(), self.opener()
        first_user = self.post(first, "/auth/register", {"username": "api_first", "password": "first-pass-123", "nickname": "甲"})
        second_user = self.post(second, "/auth/register", {"username": "api_second", "password": "second-pass-123", "nickname": "乙"})
        self.assertFalse(first_user["pairing"]["paired"])
        self.assertFalse(second_user["pairing"]["paired"])
        invite = self.post(first, "/couples/invites", {})
        paired = self.post(second, "/couples/invites/accept", {"code": invite["code"], "anniversary": "2024-01-01"})
        self.assertTrue(paired["paired"])
        with first.open(self.base + "/bootstrap") as response:
            bootstrap = json.load(response)
        self.assertEqual(bootstrap["partner"]["nickname"], "乙")
        self.assertEqual(
            set(bootstrap["capabilities"]),
            {"llm_live", "booking_live", "hotel_flight_search_live", "train_search_live", "amap_live", "geoapify_live", "ocr_live", "rtc_live", "web_push_live", "media_provider", "recipe_ai_live", "tianapi_recipe_live", "themealdb_live", "spoonacular_live"},
        )
        self.assertIsInstance(bootstrap["capabilities"]["booking_live"], bool)
        created = self.post(first, "/messages", {"type": "text", "content": "接口联调成功"})
        self.assertEqual(created["content"], "接口联调成功")

    def test_meal_recipe_calendar_and_shopping_http_flow_for_two_accounts(self) -> None:
        first, second = self.opener(), self.opener()
        self.post(first, "/auth/login", {"username": "test", "password": "test123"})
        self.post(second, "/auth/login", {"username": "test1", "password": "test123"})
        with first.open(self.base + "/recipes?q=%E7%95%AA%E8%8C%84%E7%82%92%E8%9B%8B&limit=5") as response:
            recipes = json.load(response)
        self.assertGreaterEqual(recipes["total"], 1)
        recipe = recipes["items"][0]
        planned_date = "2027-01-18"
        wish = self.post(first, "/meal-wishes", {"recipe_id": recipe["id"], "note": "接口测试"})
        meal = self.post(first, "/meal-cart/items", {
            "recipe_id": recipe["id"], "wish_id": wish["id"], "planned_date": planned_date,
            "meal_slot": "dinner", "servings": 2, "idempotency_key": uuid.uuid4().hex,
        })
        accepted = self.patch(second, f"/meal-cart/items/{meal['id']}", {"status": "accepted", "expected_version": meal["version"]})
        self.assertEqual(accepted["status"], "accepted")
        with second.open(self.base + f"/meal-calendar/day/{planned_date}") as response:
            calendar = json.load(response)
        self.assertEqual(calendar["days"][0]["meals"]["dinner"][0]["id"], meal["id"])
        preview = self.post(first, "/shopping-lists/preview", {"range_start": planned_date, "range_end": planned_date})
        shopping = self.post(first, "/shopping-lists/apply", {"preview_id": preview["preview_id"], "idempotency_key": uuid.uuid4().hex})
        self.assertGreater(len(shopping["items"]), 0)
        checked = self.patch(second, f"/shopping-lists/items/{shopping['items'][0]['id']}", {"checked": True, "expected_version": shopping["items"][0]["version"]})
        self.assertTrue(checked["checked"])

    def test_free_text_meal_wish_can_be_linked_and_scheduled_over_http(self) -> None:
        first, second = self.opener(), self.opener()
        self.post(first, "/auth/login", {"username": "test", "password": "test123"})
        self.post(second, "/auth/login", {"username": "test1", "password": "test123"})
        wish = self.post(first, "/meal-wishes", {"title": "煎牛排", "note": "见面以后做给我吃"})
        with second.open(self.base + "/recipes?q=%E7%85%8E%E7%89%9B%E6%8E%92&limit=10") as response:
            recipe = json.load(response)["items"][0]
        linked = self.patch(second, f"/meal-wishes/{wish['id']}", {
            "recipe_id": recipe["id"], "expected_version": wish["version"],
        })
        self.assertEqual(linked["recipe_name"], "香煎牛排")
        with first.open(self.base + "/meal-wishes") as response:
            visible = json.load(response)["items"]
        self.assertEqual(visible[0]["recipe_id"], recipe["id"])
        meal = self.post(first, "/meal-cart/items", {
            "recipe_id": recipe["id"], "wish_id": wish["id"], "planned_date": "2027-02-14",
            "meal_slot": "dinner", "servings": 2, "idempotency_key": uuid.uuid4().hex,
        })
        self.assertEqual(meal["name"], "香煎牛排")
        with second.open(self.base + "/meal-wishes") as response:
            self.assertEqual(json.load(response)["items"], [])

    def test_concurrent_http_messages_are_not_lost(self) -> None:
        opener = self.opener()
        login = self.post(opener, "/auth/login", {"username": "test", "password": "test123", "token_transport": "bearer"})
        token = login["access_token"]
        marker = f"http-concurrent-{time.time_ns()}"

        def send(index: int) -> str:
            body = json.dumps({"type": "text", "content": f"{marker}-{index}", "idempotency_key": f"{marker}:{index}"}).encode("utf-8")
            request = urllib.request.Request(
                self.base + "/messages",
                data=body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)["id"]

        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(send, range(12)))
        self.assertEqual(len(set(ids)), 12)
        with opener.open(self.base + "/messages") as response:
            items = json.load(response)["items"]
        self.assertEqual(sum(item["content"].startswith(marker) for item in items), 12)

    def test_message_history_search_and_context_http_contract(self) -> None:
        opener = self.opener()
        self.post(opener, "/auth/login", {"username": "test", "password": "test123"})
        marker = f"聊天搜索-{uuid.uuid4().hex}"
        created = self.post(opener, "/messages", {"type": "text", "content": marker, "idempotency_key": f"search:{uuid.uuid4().hex}"})
        with opener.open(self.base + "/messages?q=" + urllib.parse.quote(marker) + "&limit=10") as response:
            search = json.load(response)
        self.assertEqual(search["mode"], "search")
        self.assertEqual([item["id"] for item in search["items"]], [created["id"]])
        with opener.open(self.base + "/messages?around=" + urllib.parse.quote(created["id"]) + "&limit=5") as response:
            context = json.load(response)
        self.assertEqual(context["mode"], "around")
        self.assertIn(created["id"], [item["id"] for item in context["items"]])
        with self.assertRaises(urllib.error.HTTPError) as invalid_limit:
            opener.open(self.base + "/messages?limit=not-a-number")
        self.assertEqual(invalid_limit.exception.code, 400)

    def test_shared_detail_comments_are_readable_by_both_accounts(self) -> None:
        first = self.opener()
        second = self.opener()
        self.post(first, "/auth/login", {"username": "test", "password": "test123"})
        self.post(second, "/auth/login", {"username": "test1", "password": "test123"})
        marker = f"共同详情-{uuid.uuid4().hex[:8]}"
        wish = self.post(first, "/lists/wishlist/items", {"text": marker, "note": "一起讨论"})

        with second.open(self.base + f"/shared-content/wishlist_item/{wish['id']}") as response:
            partner_detail = json.load(response)
        self.assertEqual(partner_detail["title"], marker)
        first_comment = self.post(first, f"/shared-content/wishlist_item/{wish['id']}/comments", {"body": "演示甲的评论"})
        reply = self.post(second, f"/shared-content/wishlist_item/{wish['id']}/comments", {"body": "演示乙的回复"})

        with first.open(self.base + f"/shared-content/wishlist_item/{wish['id']}") as response:
            creator_detail = json.load(response)
        with second.open(self.base + f"/shared-content/wishlist_item/{wish['id']}") as response:
            partner_detail = json.load(response)
        self.assertEqual([item["body"] for item in creator_detail["comments"]], ["演示甲的评论", "演示乙的回复"])
        self.assertEqual([item["body"] for item in partner_detail["comments"]], ["演示甲的评论", "演示乙的回复"])
        self.assertTrue(creator_detail["comments"][0]["mine"])
        self.assertTrue(partner_detail["comments"][1]["mine"])

        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.delete(second, f"/shared-comments/{first_comment['id']}", {"expected_version": first_comment["version"]})
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()
        removed = self.delete(second, f"/shared-comments/{reply['id']}", {"expected_version": reply["version"]})
        self.assertTrue(removed["deleted"])

    def test_shared_content_http_denies_invalid_delayed_release_and_comments(self) -> None:
        partner = self.opener()
        self.post(partner, "/auth/login", {"username": "test1", "password": "test123"})
        for invalid in ("author", "couple"):
            with self.subTest(invalid=invalid):
                moment = main.STORE.create_moment({"title": "delayed secret", "visibility": "delayed"}, DEMO_USER)
                with main.STORE.connection() as db:
                    db.execute("UPDATE moments SET reveal_at='2020-01-01T00:00:00+00:00' WHERE id=?", (moment["id"],))
                    if invalid == "author":
                        db.execute("UPDATE users SET deleted_at='2020-01-01T00:00:00+00:00' WHERE id=?", (DEMO_USER,))
                    else:
                        db.execute("UPDATE couples SET status='unlinked' WHERE id=?", (moment["couple_id"],))
                try:
                    for method in ("GET", "POST"):
                        path = f"/shared-content/moment/{moment['id']}" + ("/comments" if method == "POST" else "")
                        with self.assertRaises(urllib.error.HTTPError) as denied:
                            if method == "GET":
                                partner.open(self.base + path)
                            else:
                                self.post(partner, path, {"body": "must not publish"})
                        self.assertIn(denied.exception.code, (400, 404))
                        denied.exception.close()
                finally:
                    with main.STORE.connection() as db:
                        db.execute("UPDATE users SET deleted_at=NULL WHERE id=?", (DEMO_USER,))
                        db.execute("UPDATE couples SET status='active' WHERE id=?", (moment["couple_id"],))

    def test_pending_media_and_private_moments_never_reach_partner_realtime_http(self) -> None:
        owner, partner = self.opener(), self.opener()
        self.post(owner, "/auth/login", {"username": "test", "password": "test123"})
        self.post(partner, "/auth/login", {"username": "test1", "password": "test123"})
        with partner.open(self.base + "/realtime/events?after=0") as response:
            cursor = json.load(response)["latest_sequence"]
        upload = urllib.request.Request(self.base + "/media", data=b"RIFF\x24\x00\x00\x00WAVEfmt " + uuid.uuid4().bytes, headers={"Content-Type": "audio/wav", "X-Filename": "private.wav"}, method="POST")
        with owner.open(upload) as response:
            media = json.load(response)
        moment = self.post(owner, "/moments", {"title": "private event", "visibility": "private", "media_id": media["id"]})
        private_ids = {media["id"], moment["id"]}
        with owner.open(self.base + f"/realtime/events?after={cursor}") as response:
            self.assertTrue(private_ids.issubset({item["resource_id"] for item in json.load(response)["items"]}))
        with partner.open(self.base + f"/realtime/events?after={cursor}") as response:
            self.assertFalse(private_ids.intersection(item["resource_id"] for item in json.load(response)["items"]))

    def test_replay_http_cursor_and_sse_advance_across_hidden_chunks(self) -> None:
        partner = self.opener()
        self.post(partner, "/auth/login", {"username": "test1", "password": "test123"})
        with main.STORE.connection() as db:
            cursor = db.execute("SELECT COALESCE(MAX(sequence),0) FROM realtime_events").fetchone()[0]
            db.executemany("INSERT INTO realtime_events(couple_id,actor_id,event_type,resource_id,payload_json,created_at) VALUES (?,?, 'moment.created',?, '{\"owner_only\":true}', '2026-10-27T00:00:00+00:00')", [("demo-couple", DEMO_USER, f"private-http-{index}") for index in range(1100)])
        try:
            shared = main.STORE.create_message({"content": "shared beyond bounded hidden chunk"}, DEMO_USER)
            with main.STORE.connection() as db:
                shared_sequence = db.execute("SELECT sequence FROM realtime_events WHERE resource_id=?", (shared["id"],)).fetchone()[0]
            with partner.open(self.base + f"/realtime/events?after={cursor}") as response:
                first = json.load(response)
            self.assertEqual(first["items"], [])
            self.assertGreater(first["latest_sequence"], cursor)
            self.assertLess(first["latest_sequence"], shared_sequence)
            with partner.open(self.base + f"/realtime/events?after={first['latest_sequence']}") as response:
                second = json.load(response)
            self.assertEqual([item["resource_id"] for item in second["items"]], [shared["id"]])
            with partner.open(self.base + f"/realtime/stream?after={cursor}", timeout=3) as response:
                lines = []
                try:
                    for _ in range(30):
                        line = response.readline().decode("utf-8").strip()
                        lines.append(line)
                        if line.startswith("data:") and shared["id"] in line:
                            break
                except TimeoutError:
                    self.fail("SSE did not advance beyond the examined hidden chunk")
            self.assertTrue(any(line.startswith("event: message.created") for line in lines))
            self.assertTrue(any(shared["id"] in line for line in lines))
        finally:
            with main.STORE.connection() as db:
                db.execute("DELETE FROM realtime_events WHERE sequence>?", (cursor,))

    def test_realtime_sse_stream_emits_scoped_event(self) -> None:
        opener = self.opener()
        self.post(opener, "/auth/login", {"username": "test", "password": "test123"})
        with opener.open(self.base + "/realtime/events?after=0") as response:
            cursor = json.load(response)["latest_sequence"]
        created = self.post(opener, "/messages", {"type": "text", "content": "实时流测试", "idempotency_key": "http:sse:message:0001"})
        with opener.open(self.base + f"/realtime/stream?after={cursor}", timeout=3) as response:
            lines = []
            for _ in range(30):
                line = response.readline().decode("utf-8").strip()
                lines.append(line)
                if line.startswith("data:") and created["id"] in line:
                    break
        self.assertTrue(any(line.startswith("event: message.created") for line in lines))
        self.assertTrue(any(created["id"] in line for line in lines))

    def test_intelligent_ui_endpoints_and_conflict_response(self) -> None:
        opener = self.opener()
        self.post(opener, "/auth/login", {"username": "test", "password": "test123"})
        for surface in ("home", "chat", "trip", "timeline", "life"):
            with self.subTest(surface=surface), opener.open(self.base + f"/ui/{surface}") as response:
                manifest = json.load(response)
                self.assertEqual(manifest["surface"], surface)
                self.assertEqual(manifest["schemaVersion"], "1.0")

        job = self.post(opener, "/ui/trip/compose", {"intent": "采用性价比方案并优先比较预算", "resourceRefs": []})
        self.assertIn(job["status"], {"queued", "running"})
        for _ in range(40):
            with opener.open(self.base + f"/ui/jobs/{job['id']}") as response:
                job = json.load(response)
            if job["status"] in {"completed", "failed"}:
                break
            time.sleep(0.025)
        self.assertEqual(job["status"], "completed")
        self.assertIsNotNone(job["manifest"])
        action = next(action for node in job["manifest"]["sections"] for action in node["actions"] if action["requiresConfirmation"])
        preview = self.post(opener, f"/ui/actions/{action['id']}/preview", {})
        self.assertIn("impact", preview["preview"])

        current_style = main.STORE.get_trip(user_id="demo-user-a")["active_variant"]
        changed_style = next(style for style in ("slow", "classic", "value", "food", "nature") if style != current_style)
        self.post(opener, "/trips/demo-trip/select-variant", {"style": changed_style})
        try:
            self.post(opener, f"/ui/actions/{action['id']}/confirm", {"idempotencyKey": "http-conflict-0001"})
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 409)
            self.assertEqual(json.load(error)["code"], "VERSION_CONFLICT")
            error.close()
        else:
            self.fail("过期旅行版本必须返回 409")
        cancelled = self.post(opener, f"/ui/actions/{action['id']}/cancel", {})
        self.assertEqual(cancelled["status"], "cancelled")

    def test_task6_private_gift_status_and_expression_http_contract(self) -> None:
        creator, partner = self.opener(), self.opener()
        self.post(creator, "/auth/login", {"username": "test", "password": "test123"})
        self.post(partner, "/auth/login", {"username": "test1", "password": "test123"})

        gift = self.post(creator, "/lists/wishlist/items", {
            "text": f"秘密礼物-{uuid.uuid4().hex[:8]}", "note": "想收到的款式", "item_kind": "gift",
        })
        with creator.open(self.base + "/lists/wishlist") as response:
            creator_before = next(item for item in json.load(response)["items"] if item["id"] == gift["id"])
        self.assertNotIn("can_claim", creator_before)
        self.assertNotIn("my_claim", creator_before)
        with creator.open(self.base + f"/shared-content/wishlist_item/{gift['id']}") as response:
            detail_before = json.load(response)

        claimed = self.post(partner, f"/lists/items/{gift['id']}/claim", {
            "expected_version": gift["version"], "private_note": "周五去准备", "reveal_claimant": False,
        }, {"Idempotency-Key": "http-gift-claim-0001"})
        self.assertEqual(claimed["status"], "active")
        self.assertEqual(claimed["private_note"], "周五去准备")
        with creator.open(self.base + "/lists/wishlist") as response:
            creator_after = next(item for item in json.load(response)["items"] if item["id"] == gift["id"])
        with creator.open(self.base + f"/shared-content/wishlist_item/{gift['id']}") as response:
            detail_after = json.load(response)
        self.assertEqual(creator_before, creator_after)
        self.assertEqual(detail_before, detail_after)

        with self.assertRaises(urllib.error.HTTPError) as changed_key:
            self.post(partner, f"/lists/items/{gift['id']}/claim", {
                "expected_version": gift["version"], "private_note": "另一份内容", "reveal_claimant": False,
            }, {"Idempotency-Key": "http-gift-claim-0001"})
        self.assertEqual(changed_key.exception.code, 409)
        changed_key.exception.close()

        status = self.put(creator, "/me/status", {
            "status_code": "need_company", "text": "今天想有人陪", "duration_minutes": 30,
        })
        self.assertEqual(status["status_code"], "need_company")
        self.assertNotEqual(status["expires_at"], None)
        with self.assertRaises(urllib.error.HTTPError) as invalid_status:
            self.put(creator, "/me/status", {"status_code": "happy", "duration_minutes": 30})
        self.assertEqual(invalid_status.exception.code, 400)
        invalid_status.exception.close()

        own_message = self.post(creator, "/messages", {
            "type": "text", "content": "我现在有点烦你先别说了", "idempotency_key": f"http-message-{uuid.uuid4().hex}",
        })
        before_count = len(main.STORE.list_messages(user_id="demo-user-a"))
        suggestion = self.post(creator, "/expression/suggestions", {
            "mode": "gentle", "message_id": own_message["id"],
        }, {"Idempotency-Key": "http-expression-0001"})
        self.assertEqual(suggestion["source"], "message")
        self.assertEqual(suggestion["provider"], "rules")
        self.assertEqual(len(main.STORE.list_messages(user_id="demo-user-a")), before_count)
        with self.assertRaises(urllib.error.HTTPError) as partner_denied:
            self.post(partner, "/expression/suggestions", {
                "mode": "summary", "message_id": own_message["id"],
            }, {"Idempotency-Key": "http-expression-denied-0001"})
        self.assertEqual(partner_denied.exception.code, 403)
        partner_denied.exception.close()

    def test_phase_two_dual_account_http_flow(self) -> None:
        first, second = self.opener(), self.opener()
        self.post(first, "/auth/login", {"username": "test", "password": "test123"})
        self.post(second, "/auth/login", {"username": "test1", "password": "test123"})

        status = self.put(first, "/me/status", {"status_code": "want_chat", "text": "下班见", "duration_minutes": 120})
        self.assertEqual(status["text"], "下班见")
        with second.open(self.base + "/couple/context") as response:
            context = json.load(response)
        self.assertEqual(next(item for item in context["members"] if item["id"] == "demo-user-a")["status_text"], "下班见")

        png = VALID_PNG
        upload = urllib.request.Request(
            self.base + "/media",
            data=png,
            headers={"Content-Type": "image/png", "X-Filename": "memory.png", "X-Media-Visibility": "shared"},
            method="POST",
        )
        with first.open(upload) as response:
            media = json.load(response)
        with self.assertRaises(urllib.error.HTTPError) as denied:
            second.open(self.base + f"/media/{media['id']}")
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()
        self.post(first, "/moments", {"title": "分享附件", "media_id": media["id"], "visibility": "shared"})
        with second.open(self.base + f"/media/{media['id']}") as response:
            self.assertTrue(response.read().startswith(b"\x89PNG\r\n\x1a\n"))

        candidate = self.post(first, "/places/search", {
            "query": "西湖", "region_hint": "杭州市", "include_global": False,
        })["items"][0]
        plan = self.post(first, "/date-plans", {"title": "周末约会", "plan_date": "2026-10-18", "city": "杭州", "place_candidate_id": candidate["candidate_id"], "start_time": "14:00", "end_time": "21:00", "budget": 500, "preferences": {"interest": "美食"}})
        mine, theirs = plan["variants"][0]["id"], plan["variants"][1]["id"]
        self.put(first, f"/date-plans/{plan['id']}/vote", {"variant_id": mine})
        revealed = self.put(second, f"/date-plans/{plan['id']}/vote", {"variant_id": theirs})
        self.assertTrue(revealed["both_voted"])
        composed = self.post(first, f"/date-plans/{plan['id']}/compose", {"variant_id": mine})
        self.post(first, f"/date-plans/{plan['id']}/confirm", {"version": composed["version"]})
        confirmed = self.post(second, f"/date-plans/{plan['id']}/confirm", {"version": composed["version"]})
        self.assertEqual(confirmed["status"], "confirmed")

        with second.open(self.base + "/realtime/events?after=0") as response:
            events = json.load(response)["items"]
        self.assertTrue(any(item["event_type"] == "date_plan.confirmed" for item in events))

    def test_date_plan_http_requires_an_explicit_server_place_candidate(self) -> None:
        first = self.opener()
        self.post(first, "/auth/login", {"username": "test", "password": "test123"})
        payload = {
            "title": "不能自动挑地点", "plan_date": "2026-11-18", "city": "迪士尼",
            "start_time": "14:00", "end_time": "21:00", "budget": 500,
        }
        with self.assertRaises(urllib.error.HTTPError) as missing:
            self.post(first, "/date-plans", payload)
        self.assertEqual(missing.exception.code, 400)
        self.assertIn("具体地点", json.load(missing.exception)["error"])
        missing.exception.close()

        candidate = self.post(first, "/places/search", {
            "query": "上海迪士尼", "region_hint": "上海市", "include_global": False,
        })["items"][0]
        created = self.post(first, "/date-plans", {**payload, "place_candidate_id": candidate["candidate_id"]})
        self.assertEqual(created["location"]["name"], "上海迪士尼")

    def test_private_media_thumbnail_ticket_and_recycle_bin_http_flow(self) -> None:
        owner, partner = self.opener(), self.opener()
        self.post(owner, "/auth/login", {"username": "test", "password": "test123"})
        self.post(partner, "/auth/login", {"username": "test1", "password": "test123"})
        upload = urllib.request.Request(
            self.base + "/media",
            data=VALID_PNG,
            headers={"Content-Type": "image/png", "X-Filename": "private-memory.png", "X-Media-Visibility": "private"},
            method="POST",
        )
        with owner.open(upload) as response:
            media = json.load(response)
        self.assertTrue(media["metadata_stripped"])
        self.assertIn(media["id"], {row["id"] for row in json.load(owner.open(self.base + "/media"))["items"]})
        self.assertNotIn(media["id"], {row["id"] for row in json.load(partner.open(self.base + "/media"))["items"]})
        try:
            partner.open(self.base + f"/media/{media['id']}")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 403)
            error.close()
        else:
            self.fail("伴侣不应读取另一方的私密图片")

        with owner.open(self.base + f"/media/{media['id']}?thumbnail=1") as response:
            self.assertEqual(response.headers["Cross-Origin-Resource-Policy"], "same-origin")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertNotIn("private-memory", response.headers["Content-Disposition"])
            self.assertTrue(response.read().startswith(b"\xff\xd8\xff"))
        ticket = self.post(owner, f"/media/{media['id']}/access", {"thumbnail": True, "ttl_seconds": 120})
        with owner.open(self.base.removesuffix("/api/v1") + ticket["url"]) as response:
            self.assertTrue(response.read().startswith(b"\xff\xd8\xff"))

        try:
            urllib.request.urlopen(self.base.removesuffix("/api/v1") + ticket["url"])
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 401)
            error.close()
        else:
            self.fail("短时图片票据仍必须配合登录会话")

        self.post(owner, f"/media/{media['id']}/trash", {})
        with owner.open(self.base + "/media/trash") as response:
            trashed = next(row for row in json.load(response)["items"] if row["id"] == media["id"])
        restored = self.post(owner, f"/media/{media['id']}/restore", {"expected_version": trashed["version"]})
        self.assertEqual(restored["status"], "ready")
        self.post(owner, f"/media/{media['id']}/trash", {})
        delete = urllib.request.Request(self.base + f"/media/{media['id']}", data=b"{}", headers={"Content-Type": "application/json"}, method="DELETE")
        with owner.open(delete) as response:
            self.assertTrue(json.load(response)["purged"])

        invalid = urllib.request.Request(self.base + "/media", data=b"\x89PNG\r\n\x1a\nnot-real", headers={"Content-Type": "image/png", "X-Filename": "fake.png"}, method="POST")
        try:
            owner.open(invalid)
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 400)
            error.close()
        else:
            self.fail("伪造图片必须被拒绝")

    def test_profile_avatar_and_couple_visible_history_http_flow(self) -> None:
        owner, partner = self.opener(), self.opener()
        self.post(owner, "/auth/login", {"username": "test", "password": "test123"})
        self.post(partner, "/auth/login", {"username": "test1", "password": "test123"})
        upload = urllib.request.Request(
            self.base + "/media", data=VALID_PNG,
            headers={"Content-Type": "image/png", "X-Filename": "avatar-http.png", "X-Media-Visibility": "private"},
            method="POST",
        )
        with owner.open(upload) as response:
            media = json.load(response)

        updated = self.put(owner, "/account/profile", {"avatar_media_id": media["id"]})
        self.assertIn(media["id"], updated["user"]["avatar_url"])
        with partner.open(self.base.removesuffix("/api/v1") + updated["user"]["avatar_url"]) as response:
            self.assertTrue(response.read().startswith(b"\xff\xd8\xff"))
        with partner.open(self.base + "/account/profile-history?limit=5") as response:
            history = json.load(response)
        self.assertEqual(history["items"][0]["changed_fields"], ["头像照片"])
        self.assertNotIn("detail", history["items"][0])

        removed = self.put(owner, "/account/profile", {"avatar_media_id": ""})
        self.assertIsNone(removed["user"]["avatar_url"])
        with self.assertRaises(urllib.error.HTTPError) as context:
            partner.open(self.base + f"/media/{media['id']}")
        self.assertEqual(context.exception.code, 403)
        context.exception.close()

    def test_resumable_video_upload_http_flow_with_real_thumbnail(self) -> None:
        owner, partner = self.opener(), self.opener()
        self.post(owner, "/auth/login", {"username": "test", "password": "test123"})
        self.post(partner, "/auth/login", {"username": "test1", "password": "test123"})
        video = b"\x00\x00\x00\x18ftypisom" + b"resumable-video" * 80
        session = self.post(owner, "/media/uploads", {"name": "journey.mp4", "mime_type": "video/mp4", "size_bytes": len(video), "visibility": "shared", "chunk_size": 256 * 1024})
        thumbnail_request = urllib.request.Request(
            self.base + f"/media/uploads/{session['id']}/thumbnail", data=VALID_PNG,
            headers={"Content-Type": "image/png"}, method="POST",
        )
        with owner.open(thumbnail_request) as response:
            self.assertTrue(json.load(response)["thumbnail_ready"])
        first_size = len(video) // 2
        first_request = urllib.request.Request(
            self.base + f"/media/uploads/{session['id']}/chunk", data=video[:first_size],
            headers={"Content-Type": "application/octet-stream", "X-Upload-Offset": "0"}, method="PUT",
        )
        with owner.open(first_request) as response:
            self.assertEqual(json.load(response)["received_size"], first_size)
        with owner.open(self.base + f"/media/uploads/{session['id']}") as response:
            self.assertEqual(json.load(response)["received_size"], first_size)
        stale_request = urllib.request.Request(
            self.base + f"/media/uploads/{session['id']}/chunk", data=video[first_size:],
            headers={"Content-Type": "application/octet-stream", "X-Upload-Offset": "0"}, method="PUT",
        )
        with self.assertRaises(urllib.error.HTTPError) as stale:
            owner.open(stale_request)
        self.assertEqual(stale.exception.code, 409)
        self.assertEqual(json.load(stale.exception)["details"]["expected_offset"], first_size)
        stale.exception.close()
        second_request = urllib.request.Request(
            self.base + f"/media/uploads/{session['id']}/chunk", data=video[first_size:],
            headers={"Content-Type": "application/octet-stream", "X-Upload-Offset": str(first_size)}, method="PUT",
        )
        with owner.open(second_request) as response:
            self.assertEqual(json.load(response)["received_size"], len(video))
        completed = self.post(owner, f"/media/uploads/{session['id']}/complete", {})
        self.assertEqual(completed["kind"], "video")
        with self.assertRaises(urllib.error.HTTPError) as denied:
            partner.open(self.base + f"/media/{completed['id']}?thumbnail=1")
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()
        self.post(owner, "/moments", {"title": "分享视频", "media_id": completed["id"], "visibility": "shared"})
        with partner.open(self.base + f"/media/{completed['id']}?thumbnail=1") as response:
            self.assertEqual(response.headers.get_content_type(), "image/jpeg")
            self.assertTrue(response.read().startswith(b"\xff\xd8\xff"))
        self.assertEqual(self.post(owner, f"/media/uploads/{session['id']}/complete", {})["id"], completed["id"])

    def test_phase_three_full_http_flow_for_two_accounts(self) -> None:
        first, second = self.opener(), self.opener()
        self.post(first, "/auth/login", {"username": "test", "password": "test123"})
        self.post(second, "/auth/login", {"username": "test1", "password": "test123"})

        candidate = self.post(first, "/places/search", {"query": "成都", "region_hint": "成都市", "include_global": False})["items"][0]
        trip = self.post(first, "/trips", {"destination": "成都", "destination_candidate_id": candidate["candidate_id"], "start_date": "2026-11-10", "end_date": "2026-11-12", "budget": 3600, "preferences": {"pace": "松弛"}})
        self.assertEqual(len(trip["variants"]), 5)
        route = self.post(second, f"/trips/{trip['id']}/routes", {"mode": "walking"})
        self.assertIn(route["provider"], {"mock", "amap", "geoapify"})
        with patch.dict("os.environ", {"SERPAPI_ENABLED": "false", "JUHE_TRAIN_ENABLED": "false", "BOOKING_API_URL": "", "BOOKING_API_KEY": ""}):
            quotes = self.post(first, "/quotes/search", {"trip_id": trip["id"], "type": "hotel"})
        self.assertEqual(len(quotes["items"]), 5)

        draft = self.post(first, "/chat-imports", {"text": "12:00 我：到成都啦\n12:01 对方：等你"})
        item = draft["items"][0]
        edited = self.patch(first, f"/chat-imports/{draft['id']}/items/{item['id']}", {"content": "已经到成都啦", "speaker": "me", "selected": True, "expected_version": item["version"]})
        self.assertEqual(edited["items"][0]["content"], "已经到成都啦")
        imported = self.post(first, f"/chat-imports/{draft['id']}/confirm", {})
        self.assertEqual(imported["count"], 2)

        with first.open(self.base + "/messages") as response:
            message = json.load(response)["items"][-1]
        moment = self.post(first, f"/messages/{message['id']}/moment", {"visibility": "shared"})
        edited_moment = self.patch(first, f"/moments/{moment['id']}", {
            "title": "成都见面的聊天",
            "body": "已经到成都啦，等你一起吃火锅",
            "occurred_at": "2026-11-10T18:30:00+08:00",
            "location": "成都",
            "mood": "期待",
            "visibility": "shared",
            "expected_version": moment["version"],
        })
        self.assertEqual(edited_moment["title"], "成都见面的聊天")
        with second.open(self.base + "/moments") as response:
            partner_moment = next(row for row in json.load(response)["items"] if row["id"] == moment["id"])
        self.assertEqual(partner_moment["body"], "已经到成都啦，等你一起吃火锅")
        with self.assertRaises(urllib.error.HTTPError) as forbidden_edit:
            self.patch(second, f"/moments/{moment['id']}", {"title": "不能代改", "expected_version": edited_moment["version"]})
        self.assertEqual(forbidden_edit.exception.code, 403)
        forbidden_edit.exception.close()
        self.post(first, f"/moments/{moment['id']}/trash", {})
        with second.open(self.base + "/moments/trash") as response:
            trashed = next(row for row in json.load(response)["items"] if row["id"] == moment["id"])
        restored = self.post(second, f"/moments/{moment['id']}/restore", {"expected_version": trashed["version"]})
        self.assertIsNone(restored["deleted_at"])

        entry = self.post(first, "/calendar", {"entry_type": "todo", "title": "一起订酒店", "starts_at": "2026-11-01", "all_day": True})
        completed = self.post(second, f"/calendar/{entry['id']}/complete", {"completed": True, "expected_version": entry["version"]})
        self.assertIsNotNone(completed["completed_at"])
        updated_entry = self.patch(first, f"/calendar/{entry['id']}", {"title": "一起订成都酒店", "expected_version": completed["version"]})
        deleted_entry = self.delete(second, f"/calendar/{entry['id']}", {"expected_version": updated_entry["version"]})
        self.assertTrue(deleted_entry["deleted"])
        preference = self.post(second, "/preferences", {"subject_user_id": "demo-user-a", "category": "口味", "label": "火锅辣度", "value": "微辣"})
        self.assertEqual(preference["value"], "微辣")
        updated_preference = self.patch(first, f"/preferences/{preference['id']}", {"value": "中辣", "expected_version": preference["version"]})
        deleted_preference = self.delete(second, f"/preferences/{preference['id']}", {"expected_version": updated_preference["version"]})
        self.assertTrue(deleted_preference["deleted"])

        with first.open(self.base + "/timeline/months") as response:
            self.assertIn("items", json.load(response))
        with first.open(self.base + "/timeline/footprints") as response:
            self.assertIn("items", json.load(response))
        with first.open(self.base + "/timeline/reports/2026") as response:
            self.assertGreaterEqual(json.load(response)["messages"], 2)
        with first.open(self.base + "/account/devices") as response:
            self.assertTrue(any(item["current"] for item in json.load(response)["items"]))
        with first.open(self.base + "/account/export.zip") as response:
            self.assertEqual(response.headers.get_content_type(), "application/zip")
            self.assertTrue(response.read().startswith(b"PK"))

    def test_account_unlink_and_delete_http_lifecycle(self) -> None:
        marker = str(time.time_ns())
        first, second = self.opener(), self.opener()
        first_password = "unlink-pass-123"
        self.post(first, "/auth/register", {"username": f"unlink_a_{marker}", "password": first_password, "nickname": "解绑甲"})
        self.post(second, "/auth/register", {"username": f"unlink_b_{marker}", "password": "unlink-pass-456", "nickname": "解绑乙"})
        invite = self.post(first, "/couples/invites", {})
        self.post(second, "/couples/invites/accept", {"code": invite["code"], "anniversary": "2024-01-01"})
        unlinked = self.post(first, "/couple/unlink", {"password": first_password, "confirmation": "解除情侣"})
        self.assertTrue(unlinked["unlinked"])
        with first.open(self.base + "/auth/me") as response:
            self.assertFalse(json.load(response)["authenticated"])

        deleting = self.opener()
        delete_username = f"delete_{marker}"
        delete_password = "delete-pass-123"
        self.post(deleting, "/auth/register", {"username": delete_username, "password": delete_password, "nickname": "待注销"})
        deleted = self.post(deleting, "/account/delete", {"password": delete_password, "confirmation": "注销账号"})
        self.assertTrue(deleted["deleted"])
        with deleting.open(self.base + "/auth/me") as response:
            self.assertFalse(json.load(response)["authenticated"])
        with self.assertRaises(urllib.error.HTTPError) as deleted_login:
            self.post(self.opener(), "/auth/login", {"username": delete_username, "password": delete_password})
        self.assertEqual(deleted_login.exception.code, 400)
        deleted_login.exception.close()

    def test_webrtc_call_signaling_flow_for_two_accounts(self) -> None:
        first, second = self.opener(), self.opener()
        self.post(first, "/auth/login", {"username": "test", "password": "test123"})
        self.post(second, "/auth/login", {"username": "test1", "password": "test123"})

        with first.open(self.base + "/webrtc/config") as response:
            rtc_config = json.load(response)
        self.assertTrue(rtc_config["iceServers"])
        self.assertIn(rtc_config["iceTransportPolicy"], {"all", "relay"})

        call = self.post(first, "/calls", {})
        self.assertEqual(call["state"], "ringing")
        with second.open(self.base + "/calls/active") as response:
            incoming = json.load(response)["call"]
        self.assertEqual(incoming["id"], call["id"])
        self.assertFalse(incoming["is_caller"])

        self.post(first, f"/calls/{call['id']}/signal", {"type": "offer", "payload": {"type": "offer", "sdp": "v=0\r\n"}})
        with second.open(self.base + f"/calls/{call['id']}/signals?after=0") as response:
            signals = json.load(response)
        self.assertEqual(signals["items"][0]["type"], "offer")

        answered = self.post(second, f"/calls/{call['id']}/answer", {})
        self.assertEqual(answered["state"], "connected")
        heartbeat = self.post(first, f"/calls/{call['id']}/heartbeat", {})
        self.assertEqual(heartbeat["state"], "connected")
        self.post(second, f"/calls/{call['id']}/signal", {"type": "answer", "payload": {"type": "answer", "sdp": "v=0\r\n"}})
        with first.open(self.base + f"/calls/{call['id']}/signals?after={signals['latest_sequence']}") as response:
            answer_signals = json.load(response)
        self.assertEqual(answer_signals["items"][0]["type"], "answer")
        ended = self.post(first, f"/calls/{call['id']}/end", {"duration_seconds": 3, "note": "HTTP WebRTC 测试"})
        self.assertEqual(ended["state"], "ended")

    def test_shengwang_tokens_are_scoped_to_same_call_and_each_user(self) -> None:
        first, second = self.opener(), self.opener()
        self.post(first, "/auth/login", {"username": "test", "password": "test123"})
        self.post(second, "/auth/login", {"username": "test1", "password": "test123"})

        with first.open(self.base + "/rtc/config") as response:
            config = json.load(response)
        self.assertEqual(config["provider"], "shengwang")
        self.assertTrue(config["shengwangAvailable"])
        self.assertNotIn("b" * 32, json.dumps(config))

        call = self.post(first, "/calls", {})
        first_token = self.post(first, f"/calls/{call['id']}/rtc-token", {})
        self.post(second, f"/calls/{call['id']}/answer", {})
        second_token = self.post(second, f"/calls/{call['id']}/rtc-token", {})
        self.assertEqual(first_token["channel"], second_token["channel"])
        self.assertEqual(first_token["appId"], second_token["appId"])
        self.assertNotEqual(first_token["uid"], second_token["uid"])
        self.assertTrue(first_token["token"].startswith("007"))
        self.assertTrue(second_token["token"].startswith("007"))
        self.assertNotIn("b" * 32, json.dumps(first_token))
        self.post(first, f"/calls/{call['id']}/end", {"duration_seconds": 1, "note": "声网接口测试"})

        outsider = self.opener()
        self.post(outsider, "/auth/register", {"username": "rtc_outsider", "password": "safe-pass-123", "nickname": "访客"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.post(outsider, f"/calls/{call['id']}/rtc-token", {})
        self.assertIn(raised.exception.code, {400, 404})
        raised.exception.close()

    def test_cors_preflight_and_bearer_session_for_allowed_origin(self) -> None:
        origin = "http://localhost:18765"
        request = urllib.request.Request(
            self.base + "/calls",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
            method="OPTIONS",
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 204)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], origin)
            self.assertEqual(response.headers["Access-Control-Allow-Credentials"], "true")
            self.assertIn("Authorization", response.headers["Access-Control-Allow-Headers"])

        login = urllib.request.Request(
            self.base + "/auth/login",
            data=json.dumps({"username": "test", "password": "test123", "token_transport": "bearer"}).encode(),
            headers={"Content-Type": "application/json", "Origin": origin},
            method="POST",
        )
        with urllib.request.urlopen(login) as response:
            body = json.load(response)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], origin)
        authenticated = urllib.request.Request(
            self.base + "/calls/active",
            headers={"Authorization": f"Bearer {body['access_token']}", "Origin": origin},
        )
        with urllib.request.urlopen(authenticated) as response:
            self.assertIn("call", json.load(response))

        denied = urllib.request.Request(
            self.base + "/calls",
            headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
            method="OPTIONS",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(denied)
        self.assertEqual(raised.exception.code, 403)
        raised.exception.close()

    def test_sensitive_local_files_are_never_served(self) -> None:
        root = self.base.removesuffix("/api/v1")
        for path in ("/.env", "/data/love_moments.db", "/love_moments/store.py", "/static/%2e%2e/.env"):
            with self.subTest(path=path):
                try:
                    urllib.request.urlopen(root + path)
                except urllib.error.HTTPError as error:
                    self.assertEqual(error.code, 404)
                    error.close()
                else:
                    self.fail(f"敏感路径不应可访问: {path}")

                request = urllib.request.Request(root + path, method="HEAD")
                try:
                    urllib.request.urlopen(request)
                except urllib.error.HTTPError as error:
                    self.assertEqual(error.code, 404)
                    error.close()
                else:
                    self.fail(f"敏感路径的 HEAD 元数据也不应可访问: {path}")

    def test_curated_recipe_cover_is_public_but_cover_path_stays_narrow(self) -> None:
        root = self.base.removesuffix("/api/v1")
        cover = "/static/assets/recipe-covers/recipe_builtin_tomato_egg.jpg"
        with urllib.request.urlopen(root + cover) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "image/jpeg")
            self.assertGreater(len(response.read()), 10_000)
        with urllib.request.urlopen(urllib.request.Request(root + cover, method="HEAD")) as response:
            self.assertEqual(response.status, 200)
        for path in (
            "/static/assets/recipe-covers/not-a-real-cover.jpg",
            "/static/assets/recipe-covers/.env",
            "/static/assets/recipe-covers/nested/secret.jpg",
        ):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(root + path)
            self.assertEqual(raised.exception.code, 404)
            raised.exception.close()

    def test_security_headers_and_unknown_api_shape(self) -> None:
        root = self.base.removesuffix("/api/v1")
        with urllib.request.urlopen(root + "/") as response:
            html = response.read().decode("utf-8")
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
            self.assertNotIn("fonts.googleapis.com", response.headers["Content-Security-Policy"])
            self.assertNotIn("fonts.gstatic.com", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("两个人分别注册自己的独立账号", html)
            self.assertIn("请使用你们各自注册的账号", html)
            self.assertNotIn("test / 12345678", html)
        with urllib.request.urlopen(root + "/api/health") as response:
            health = json.load(response)
        self.assertEqual(health["schema_version"], SCHEMA_VERSION)
        self.assertNotIn("messages", health)
        self.assertNotIn("moments", health)
        try:
            urllib.request.urlopen(self.base + "/bootstrap")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.headers["Cache-Control"], "no-store")
            error.close()
        else:
            self.fail("未登录 bootstrap 应返回 401")
        try:
            urllib.request.urlopen(self.base + "/does-not-exist")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 404)
            self.assertEqual(json.load(error)["error"], "接口不存在")
            error.close()
        else:
            self.fail("未知 API 应返回 404")

    def test_request_limiter_blocks_burst(self) -> None:
        limiter = main.RequestLimiter(limit=2, window_seconds=60)
        self.assertTrue(limiter.allow("same-client"))
        self.assertTrue(limiter.allow("same-client"))
        self.assertFalse(limiter.allow("same-client"))
        self.assertTrue(limiter.allow("different-client"))

    def test_external_provider_alerts_only_after_three_consecutive_failures(self) -> None:
        tracker = main.ProviderFailureTracker()
        with patch.object(main.ADMIN, "record_event") as record, \
             patch.object(main.ADMIN, "resolve_event_condition") as resolve:
            tracker.failure("ocr", "OCR 服务", RuntimeError("first"))
            tracker.failure("ocr", "OCR 服务", RuntimeError("second"))
            record.assert_not_called()
            tracker.failure("ocr", "OCR 服务", RuntimeError("third"))
            self.assertEqual(record.call_count, 1)
            self.assertEqual(record.call_args.args[0], "provider:ocr:consecutive-failure")
            self.assertEqual(record.call_args.args[4]["consecutive_failures"], 3)
            tracker.success("ocr")
            resolve.assert_called_once_with(
                "provider:ocr:consecutive-failure",
                kind="provider_success",
                message="外部服务随后已真实调用成功",
            )
            tracker.failure("ocr", "OCR 服务", RuntimeError("new-first"))
            tracker.failure("ocr", "OCR 服务", RuntimeError("new-second"))
            self.assertEqual(record.call_count, 1)
            tracker.failure("ocr", "OCR 服务", RuntimeError("new-third"))
            self.assertEqual(record.call_count, 2)

    def test_external_quota_returns_429_before_network(self) -> None:
        opener = self.opener()
        self.post(opener, "/auth/login", {"username": "test", "password": "test123"})
        candidate = self.post(opener, "/places/search", {"query": "杭州", "region_hint": "杭州市", "include_global": False})["items"][0]
        trip = self.post(opener, "/trips", {"destination": "杭州", "destination_candidate_id": candidate["candidate_id"], "start_date": "2026-12-10", "end_date": "2026-12-12", "budget": 3000})
        try:
            with patch.dict("os.environ", {"AMAP_WEB_KEY": "never-sent", "AMAP_DAILY_REQUEST_LIMIT": "0"}, clear=False):
                request = urllib.request.Request(
                    self.base + f"/trips/{trip['id']}/routes",
                    data=json.dumps({"origin": "西湖", "destination": "灵隐寺", "mode": "walking"}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    opener.open(request)
                self.assertEqual(raised.exception.code, 429)
                body = json.loads(raised.exception.read().decode())
                raised.exception.close()
        finally:
            main.STORE.delete_trip(trip["id"], trip["version"], "demo-user-a")
        self.assertEqual(body["code"], "EXTERNAL_QUOTA_EXCEEDED")
        self.assertEqual(body["provider"], "amap")
        self.assertEqual(body["limit"], 0)

    def test_kitchen_profile_pantry_and_stocking_http_flow_for_two_accounts(self) -> None:
        tang, chao = self.opener(), self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        self.post(chao, "/auth/login", {"username": "test1", "password": "test123"})

        profile = self.put(tang, "/food-profiles/me", {
            "allergies": [{"allergen_key": "乳制品", "severity": "avoid"}],
            "avoidances": [], "preferences": {"liked_tags": ["清淡"], "disliked_tags": []},
            "expected_version": 0, "idempotency_key": f"http-profile-{uuid.uuid4().hex}",
        })
        self.assertEqual(profile["allergies"], [{"allergen_key": "乳制品", "severity": "avoid"}])
        with chao.open(self.base + "/food-profiles") as response:
            profiles = json.load(response)
        self.assertEqual(next(item for item in profiles["profiles"] if item["user_id"] == DEMO_USER)["version"], 1)

        pantry = self.post(tang, "/pantry/items", {
            "name": f"HTTP番茄{uuid.uuid4().hex[:6]}", "state": "have", "expected_version": 0,
            "idempotency_key": f"http-pantry-{uuid.uuid4().hex}",
        })
        changed = self.patch(chao, f"/pantry/items/{pantry['id']}", {
            "state": "low", "expected_version": pantry["version"],
            "idempotency_key": f"http-pantry-low-{uuid.uuid4().hex}",
        })
        self.assertEqual(changed["state"], "low")

        shopping = self.post(tang, "/shopping-lists/items", {
            "name": f"HTTP土豆{uuid.uuid4().hex[:6]}", "quantity": 1, "unit": "个", "category": "produce",
        })
        checked = self.patch(tang, f"/shopping-lists/items/{shopping['id']}", {
            "checked": True, "expected_version": shopping["version"],
        })
        stocked = self.post(chao, f"/shopping-lists/items/{shopping['id']}/stock", {
            "expected_version": checked["version"],
            "idempotency_key": f"http-stock-{uuid.uuid4().hex}",
        })
        self.assertTrue(stocked["changed"])
        with tang.open(self.base + "/pantry/items") as response:
            pantry_list = json.load(response)
        self.assertTrue(any(item["id"] == stocked["pantry_item"]["id"] for item in pantry_list["items"]))

    def test_kitchen_http_rejects_cross_couple_and_returns_409_for_stale_version(self) -> None:
        tang = self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        pantry = self.post(tang, "/pantry/items", {
            "name": f"HTTP跨情侣{uuid.uuid4().hex[:6]}", "state": "have", "expected_version": 0,
            "idempotency_key": f"cross-pantry-{uuid.uuid4().hex}",
        })
        changed = self.patch(tang, f"/pantry/items/{pantry['id']}", {
            "state": "low", "expected_version": pantry["version"],
            "idempotency_key": f"cross-pantry-low-{uuid.uuid4().hex}",
        })
        stale_request = urllib.request.Request(
            self.base + f"/pantry/items/{pantry['id']}",
            data=json.dumps({
                "state": "out", "expected_version": pantry["version"],
                "idempotency_key": f"cross-pantry-stale-{uuid.uuid4().hex}",
            }).encode(), headers={"Content-Type": "application/json"}, method="PATCH",
        )
        with self.assertRaises(urllib.error.HTTPError) as stale_error:
            tang.open(stale_request)
        self.assertEqual(stale_error.exception.code, 409)
        stale_error.exception.close()
        self.assertEqual(changed["state"], "low")

        username_a = f"http_out_a_{uuid.uuid4().hex[:8]}"
        username_b = f"http_out_b_{uuid.uuid4().hex[:8]}"
        outsider_a = main.STORE.register(username_a, "Outside-pass-123!", "外甲")
        outsider_b = main.STORE.register(username_b, "Outside-pass-456!", "外乙")
        invite = main.STORE.create_user_invite(outsider_a["id"])
        main.STORE.accept_user_invite(outsider_b["id"], invite["code"], "2026-01-01")
        outside = self.opener()
        self.post(outside, "/auth/login", {"username": username_a, "password": "Outside-pass-123!"})
        with outside.open(self.base + "/pantry/items") as response:
            self.assertEqual(json.load(response)["items"], [])
        cross_request = urllib.request.Request(
            self.base + f"/pantry/items/{pantry['id']}",
            data=json.dumps({
                "state": "out", "expected_version": changed["version"],
                "idempotency_key": f"outside-pantry-{uuid.uuid4().hex}",
            }).encode(), headers={"Content-Type": "application/json"}, method="PATCH",
        )
        with self.assertRaises(urllib.error.HTTPError) as cross_error:
            outside.open(cross_request)
        self.assertEqual(cross_error.exception.code, 404)
        cross_error.exception.close()

    def test_complete_shopping_and_stock_is_atomic_over_http(self) -> None:
        tang = self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        food = self.post(tang, "/shopping-lists/items", {
            "name": f"HTTP入库食品{uuid.uuid4().hex[:6]}", "quantity": 1, "unit": "个", "category": "produce",
        })
        household = self.post(tang, "/shopping-lists/items", {
            "name": f"HTTP厨房纸{uuid.uuid4().hex[:6]}", "quantity": 1, "unit": "包", "category": "household",
        })
        food = self.patch(tang, f"/shopping-lists/items/{food['id']}", {"checked": True, "expected_version": food["version"]})
        household = self.patch(tang, f"/shopping-lists/items/{household['id']}", {"checked": True, "expected_version": household["version"]})
        with tang.open(self.base + "/shopping-lists/active") as response:
            active = json.load(response)
        invalid = urllib.request.Request(
            self.base + "/shopping-lists/complete",
            data=json.dumps({
                "expected_version": active["version"], "allow_incomplete": True,
                "stock_checked_item_ids": [food["id"], household["id"]],
                "stock_item_versions": {food["id"]: food["version"], household["id"]: household["version"]},
                "confirm_food_item_ids": [], "idempotency_key": f"http-complete-invalid-{uuid.uuid4().hex}",
            }).encode(), headers={"Content-Type": "application/json"}, method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as invalid_error:
            tang.open(invalid)
        self.assertEqual(invalid_error.exception.code, 409)
        invalid_error.exception.close()
        with tang.open(self.base + "/shopping-lists/active") as response:
            still_active = json.load(response)
        self.assertEqual(still_active["status"], "active")
        with tang.open(self.base + "/pantry/items") as response:
            before_names = {item["canonical_name"] for item in json.load(response)["items"]}
        self.assertNotIn(food["display_name"], before_names)

        completed = self.post(tang, "/shopping-lists/complete", {
            "expected_version": still_active["version"], "allow_incomplete": True,
            "stock_checked_item_ids": [food["id"]], "stock_item_versions": {food["id"]: food["version"]},
            "confirm_food_item_ids": [], "idempotency_key": f"http-complete-valid-{uuid.uuid4().hex}",
        })
        self.assertEqual((completed["completed"], completed["stocked_count"]), (True, 1))
        with tang.open(self.base + "/pantry/items") as response:
            after_names = {item["canonical_name"] for item in json.load(response)["items"]}
        self.assertIn(food["display_name"], after_names)

    def test_weekly_meal_plan_two_account_create_read_confirm_and_cancel_flow(self) -> None:
        with main.STORE.connection() as db:
            db.execute("DELETE FROM mutation_idempotency WHERE operation LIKE 'weekly_plan.%'")
            db.execute("DELETE FROM weekly_meal_plan_drafts")
        tang, chao = self.opener(), self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        self.post(chao, "/auth/login", {"username": "test1", "password": "test123"})
        start = (datetime.now().date() + timedelta(days=1)).isoformat()
        end = (datetime.now().date() + timedelta(days=2)).isoformat()
        key = f"http-weekly-create-{uuid.uuid4().hex}"
        created = self.post(tang, "/weekly-meal-plans/drafts", {
            "schema_version": 1, "starts_on": start, "ends_on": end,
            "meal_slots": [{"meal_slot": "dinner", "planned_time": "19:00", "max_active_minutes": 120}],
            "servings": 2, "availability_mode": "either_partner",
        }, {"Idempotency-Key": key})
        with chao.open(self.base + "/weekly-meal-plans/active") as response:
            partner = json.load(response)
        self.assertEqual((partner["id"], partner["items"]), (created["id"], created["items"]))
        confirmed = self.post(tang, f"/weekly-meal-plans/drafts/{created['id']}/confirm", {
            "expected_version": created["version"],
        }, {"Idempotency-Key": f"http-weekly-confirm-{uuid.uuid4().hex}"})
        self.assertEqual(confirmed["status"], "pending_confirmation")
        cancelled = self.post(chao, f"/weekly-meal-plans/drafts/{created['id']}/cancel", {
            "expected_version": confirmed["version"],
        }, {"Idempotency-Key": f"http-weekly-cancel-{uuid.uuid4().hex}"})
        self.assertEqual(cancelled["status"], "cancelled")

        mismatch = urllib.request.Request(
            self.base + "/weekly-meal-plans/drafts",
            data=json.dumps({
                "schema_version": 1, "starts_on": start, "ends_on": end,
                "meal_slots": [{"meal_slot": "dinner", "planned_time": "19:00", "max_active_minutes": 120}],
                "servings": 2, "availability_mode": "either_partner", "idempotency_key": "body-weekly-key-0001",
            }).encode(),
            headers={"Content-Type": "application/json", "Idempotency-Key": "header-weekly-key-0001"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as error:
            tang.open(mismatch)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_ingredient_substitution_http_preview_privacy_and_header_contract(self) -> None:
        tang, chao = self.opener(), self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        self.post(chao, "/auth/login", {"username": "test1", "password": "test123"})
        meal_id = f"meal-http-substitution-{uuid.uuid4().hex}"
        planned_date = (datetime.now().date() + timedelta(days=1)).isoformat()
        snapshot = {
            "id": "http-substitution-recipe", "name": "HTTP 替代测试菜",
            "cooking_method": "汤羹", "method_tags": ["汤羹"], "base_servings": 2,
            "ingredients": [{
                "id": "http-milk-row", "name": "牛奶", "canonical_name": "牛奶",
                "quantity_milli": 250_000, "quantity_text": "", "canonical_unit": "ml",
                "unit_family": "volume", "specification": "", "preparation": "直接使用",
                "ingredient_group": "main", "optional": 0, "position": 1,
            }],
            "source_type": "real_api",
        }
        with main.STORE.connection() as db:
            couple, _ = main.STORE._couple_context(db, DEMO_USER)
            cart = main.STORE._active_cart(db, couple["id"], create=True)
            db.execute(
                """
                INSERT INTO meal_cart_items(
                    id,cart_id,couple_id,recipe_id,recipe_snapshot_json,planned_date,meal_slot,
                    planned_time,servings,ordered_by,status,course_type,note,created_at,updated_at
                ) VALUES (?,?,?,NULL,?,?,'dinner','19:00',2,?,'accepted','main','','now','now')
                """,
                (meal_id, cart["id"], couple["id"], json.dumps(snapshot, ensure_ascii=False), planned_date, DEMO_USER),
            )
        self.addCleanup(self._cleanup_substitution_http_fixture, meal_id)
        with chao.open(self.base + f"/meal-cart/items/{meal_id}/ingredient-substitutions") as response:
            partner_before = json.load(response)
        preview = self.post(tang, f"/meal-cart/items/{meal_id}/ingredient-substitutions/preview", {
            "snapshot_ingredient_ref": "http-milk-row",
            "expected_meal_version": 1,
            "expected_shopping_list_version": 0,
        }, {"Idempotency-Key": f"http-substitution-preview-{uuid.uuid4().hex}"})
        self.assertEqual(preview["meal_item_id"], meal_id)
        self.assertGreaterEqual(len(preview["items"]), 2)
        self.assertTrue(all(item["source_type"] == "rules" for item in preview["items"]))
        with chao.open(self.base + f"/meal-cart/items/{meal_id}/ingredient-substitutions") as response:
            partner_after = json.load(response)
        self.assertEqual(partner_after, partner_before)
        self.assertEqual(partner_after["items"], [])
        self.assertNotIn(preview["items"][0]["id"], json.dumps(partner_after, ensure_ascii=False))

        mismatch = urllib.request.Request(
            self.base + f"/meal-cart/items/{meal_id}/ingredient-substitutions/preview",
            data=json.dumps({
                "snapshot_ingredient_ref": "http-milk-row", "expected_meal_version": 1,
                "expected_shopping_list_version": 0, "idempotency_key": "body-substitution-key-0001",
            }).encode(),
            headers={"Content-Type": "application/json", "Idempotency-Key": "header-substitution-key-0001"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as mismatch_error:
            tang.open(mismatch)
        self.assertEqual(mismatch_error.exception.code, 400)
        mismatch_error.exception.close()

    def test_cooking_http_shared_resume_steps_timers_and_complete(self) -> None:
        tang, chao = self.opener(), self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        self.post(chao, "/auth/login", {"username": "test1", "password": "test123"})
        started = self.post(tang, "/cook-sessions", {
            "recipe_id": "recipe_builtin_kungpao_chicken", "visibility": "shared",
        }, {"Idempotency-Key": f"http-cook-start-{uuid.uuid4().hex}"})
        self.addCleanup(self._cleanup_cooking_http_fixture, started["id"])
        with chao.open(self.base + f"/cook-sessions/{started['id']}") as response:
            partner = json.load(response)
        self.assertEqual(partner["recipe_snapshot"], started["recipe_snapshot"])
        moved = self.patch(chao, f"/cook-sessions/{started['id']}/step", {
            "step_index": 1, "expected_version": started["version"],
        }, {"Idempotency-Key": f"http-cook-step-{uuid.uuid4().hex}"})
        timer = self.post(tang, f"/cook-sessions/{started['id']}/timers", {
            "step_index": 1, "duration_seconds": 90, "label": "HTTP 计时",
        }, {"Idempotency-Key": f"http-cook-timer-{uuid.uuid4().hex}"})
        self.assertEqual(timer["status"], "running")
        completed = self.post(tang, f"/cook-sessions/{started['id']}/complete", {
            "expected_version": moved["version"],
        }, {"Idempotency-Key": f"http-cook-complete-{uuid.uuid4().hex}"})
        self.assertEqual(completed["status"], "completed")
        self.assertTrue(completed["pantry_suggestions"])
        with tang.open(self.base + "/cook-sessions?status=completed") as response:
            listing = json.load(response)
        self.assertIn(started["id"], {item["id"] for item in listing["items"]})

    def test_all_cook_mutations_require_header_idempotency_and_reject_mismatch(self) -> None:
        tang = self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        missing = urllib.request.Request(
            self.base + "/cook-sessions",
            data=json.dumps({"recipe_id": "recipe_builtin_kungpao_chicken", "visibility": "private"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as missing_error:
            tang.open(missing)
        self.assertEqual(missing_error.exception.code, 400)
        missing_error.exception.close()
        mismatch = urllib.request.Request(
            self.base + "/cook-sessions",
            data=json.dumps({
                "recipe_id": "recipe_builtin_kungpao_chicken", "visibility": "private",
                "idempotency_key": "body-cook-idempotency-0001",
            }).encode(),
            headers={"Content-Type": "application/json", "Idempotency-Key": "header-cook-idempotency-0001"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as mismatch_error:
            tang.open(mismatch)
        self.assertEqual(mismatch_error.exception.code, 400)
        mismatch_error.exception.close()

    def test_private_meal_cooking_request_is_rejected_without_partner_visible_change(self) -> None:
        tang, chao = self.opener(), self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        self.post(chao, "/auth/login", {"username": "test1", "password": "test123"})
        item = main.STORE.add_meal_cart_item({
            "recipe_id": "recipe_builtin_kungpao_chicken",
            "planned_date": (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat(),
            "meal_slot": "dinner", "servings": 2,
            "idempotency_key": f"http-private-meal-create-{uuid.uuid4().hex}",
        }, DEMO_USER)
        meal = main.STORE.update_meal_cart_item(item["id"], {
            "status": "accepted", "expected_version": item["version"],
        }, DEMO_USER)
        self.addCleanup(self._cleanup_cooking_meal_fixture, meal["id"])
        with chao.open(self.base + "/meal-cart") as response:
            partner_before = json.load(response)
        request = urllib.request.Request(
            self.base + "/cook-sessions",
            data=json.dumps({
                "meal_item_id": meal["id"], "visibility": "private",
                "expected_meal_version": meal["version"],
            }).encode(),
            headers={"Content-Type": "application/json", "Idempotency-Key": "http-private-meal-rejected-0001"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            tang.open(request)
        self.assertEqual(rejected.exception.code, 400)
        rejected.exception.close()
        with chao.open(self.base + "/meal-cart") as response:
            self.assertEqual(json.load(response), partner_before)
        with chao.open(self.base + "/cook-sessions?status=all") as response:
            self.assertNotIn(meal["id"], {entry.get("meal_item_id") for entry in json.load(response)["items"]})

    @staticmethod
    def _cleanup_cooking_http_fixture(session_id: str) -> None:
        with main.STORE.connection() as db:
            db.execute("DELETE FROM cook_sessions WHERE id=?", (session_id,))
            db.execute("DELETE FROM mutation_idempotency WHERE operation LIKE 'cook.%'")

    @staticmethod
    def _cleanup_cooking_meal_fixture(meal_id: str) -> None:
        with main.STORE.connection() as db:
            db.execute("DELETE FROM cook_sessions WHERE meal_item_id=?", (meal_id,))
            db.execute("DELETE FROM meal_cart_items WHERE id=?", (meal_id,))
            db.execute("DELETE FROM mutation_idempotency WHERE operation LIKE 'cook.%'")

    @staticmethod
    def _cleanup_substitution_http_fixture(meal_id: str) -> None:
        with main.STORE.connection() as db:
            db.execute("DELETE FROM mutation_idempotency WHERE operation LIKE 'ingredient_substitution.%'")
            db.execute("DELETE FROM ingredient_substitutions WHERE meal_item_id=?", (meal_id,))
            db.execute("DELETE FROM meal_cart_items WHERE id=?", (meal_id,))
    def test_trip_negotiation_and_realism_http_contract(self) -> None:
        tang, chao = self.opener(), self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        self.post(chao, "/auth/login", {"username": "test1", "password": "test123"})
        with tang.open(self.base + f"/trips/{DEMO_TRIP}/negotiation") as response:
            initial = json.load(response)
        self.assertEqual(initial["state"], "open")
        preferences = [{
            "item_id": item["id"],
            "preference": "must" if index == 0 else "okay",
            "expected_item_version": item["version"],
        } for index, item in enumerate(initial["candidates"])]
        first_request = urllib.request.Request(
            self.base + f"/trips/{DEMO_TRIP}/negotiation/preferences",
            data=json.dumps({
                "expected_trip_version": initial["trip_version"], "expected_round": initial["round"],
                "start_new_round": False, "preferences": preferences,
                "client_request_id": "body-key-must-not-win",
            }).encode(),
            headers={"Content-Type": "application/json", "Idempotency-Key": "http-negotiation-header-001"},
            method="PUT",
        )
        with tang.open(first_request) as response:
            waiting = json.load(response)
        self.assertEqual(waiting["state"], "waiting_for_partner")
        with chao.open(self.base + f"/trips/{DEMO_TRIP}/negotiation") as response:
            partner_hidden = json.load(response)
        self.assertNotIn("my_preferences", partner_hidden)
        self.assertNotIn("proposal", partner_hidden)

        revealed = self.put(chao, f"/trips/{DEMO_TRIP}/negotiation/preferences", {
            "expected_trip_version": initial["trip_version"], "expected_round": initial["round"],
            "start_new_round": False, "preferences": preferences,
            "client_request_id": "http-negotiation-partner-001",
        })
        self.assertEqual(revealed["state"], "revealed")
        with tang.open(self.base + f"/trips/{DEMO_TRIP}/negotiation") as response:
            self.assertEqual(json.load(response), revealed)

        checked = self.post(tang, f"/trips/{DEMO_TRIP}/realism-checks", {
            "expected_trip_version": initial["trip_version"],
            "client_request_id": "http-realism-body-key",
        }, {"Idempotency-Key": "http-realism-header-0001"})
        self.assertEqual(checked["effective_status"], "current")
        with chao.open(self.base + f"/trips/{DEMO_TRIP}/realism-checks/current") as response:
            current = json.load(response)
        self.assertEqual(current["id"], checked["id"])
        self.assertTrue(all("apply_allowed" in item for item in current["findings"]))

    def test_trip_packing_emergency_and_offline_snapshot_http_contract(self) -> None:
        tang, chao = self.opener(), self.opener()
        self.post(tang, "/auth/login", {"username": "test", "password": "test123"})
        self.post(chao, "/auth/login", {"username": "test1", "password": "test123"})
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with main.STORE.connection() as db:
            trip = db.execute("SELECT version,current_revision FROM trips WHERE id=?", (DEMO_TRIP,)).fetchone()
            couple = db.execute("SELECT member_a,member_b FROM couples WHERE id=(SELECT couple_id FROM trips WHERE id=?)", (DEMO_TRIP,)).fetchone()
            db.execute("DELETE FROM approvals WHERE trip_id=? AND revision=?", (DEMO_TRIP, trip["current_revision"]))
            db.executemany(
                "INSERT INTO approvals(trip_id,revision,user_id,status,updated_at) VALUES (?,?,?,'approved',?)",
                [
                    (DEMO_TRIP, trip["current_revision"], couple["member_a"], now),
                    (DEMO_TRIP, trip["current_revision"], couple["member_b"], now),
                ],
            )
            trip_version = int(trip["version"])

        key = f"http-packing-header-{uuid.uuid4().hex}"
        request = urllib.request.Request(
            self.base + f"/trips/{DEMO_TRIP}/packing",
            data=json.dumps({
                "label": "HTTP 双端充电线", "assigned_to": "shared", "expected_trip_version": trip_version,
                "idempotency_key": "body-key-must-not-win",
            }).encode(),
            headers={"Content-Type": "application/json", "Idempotency-Key": key},
            method="POST",
        )
        with tang.open(request) as response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            created = json.load(response)
        replay_request = urllib.request.Request(
            self.base + f"/trips/{DEMO_TRIP}/packing",
            data=json.dumps({
                "label": "HTTP 双端充电线", "assigned_to": "shared", "expected_trip_version": trip_version,
                "idempotency_key": "another-body-key",
            }).encode(),
            headers={"Content-Type": "application/json", "Idempotency-Key": key},
            method="POST",
        )
        with tang.open(replay_request) as response:
            self.assertEqual(json.load(response), created)
        conflict_request = urllib.request.Request(
            self.base + f"/trips/{DEMO_TRIP}/packing",
            data=json.dumps({"label": "不同物品", "assigned_to": "shared", "expected_trip_version": trip_version}).encode(),
            headers={"Content-Type": "application/json", "Idempotency-Key": key},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as conflict:
            tang.open(conflict_request)
        self.assertEqual(conflict.exception.code, 409)
        conflict.exception.close()

        updated = self.patch(chao, f"/trips/{DEMO_TRIP}/packing/{created['id']}", {"checked": True, "expected_version": created["version"]})
        stale = urllib.request.Request(
            self.base + f"/trips/{DEMO_TRIP}/packing/{created['id']}",
            data=json.dumps({"checked": False, "expected_version": created["version"]}).encode(),
            headers={"Content-Type": "application/json"}, method="PATCH",
        )
        with self.assertRaises(urllib.error.HTTPError) as stale_error:
            tang.open(stale)
        self.assertEqual(stale_error.exception.code, 409)
        stale_error.exception.close()
        self.assertTrue(updated["checked"])

        sentinel = f"PRIVATE-HTTP-{uuid.uuid4().hex}"
        private = self.post(tang, f"/trips/{DEMO_TRIP}/emergency", {
            "entry_type": "booking", "title": "HTTP 私密订单", "visibility": "private",
            "details": {"booking_reference": sentinel}, "expected_trip_version": trip_version,
        }, {"Idempotency-Key": f"http-emergency-{uuid.uuid4().hex}"})
        self.assertEqual(private["visibility"], "private")
        with chao.open(self.base + f"/trips/{DEMO_TRIP}/emergency") as response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertNotIn(sentinel, json.dumps(json.load(response), ensure_ascii=False))
        with tang.open(self.base + f"/trips/{DEMO_TRIP}/offline-snapshot") as response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            owner_snapshot = json.load(response)
        with chao.open(self.base + f"/trips/{DEMO_TRIP}/offline-snapshot") as response:
            partner_snapshot = json.load(response)
        self.assertIn(sentinel, json.dumps(owner_snapshot, ensure_ascii=False))
        self.assertNotIn(sentinel, json.dumps(partner_snapshot, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
