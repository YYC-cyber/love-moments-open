from __future__ import annotations

import http.cookiejar
import base64
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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
from love_moments.store import DEMO_PARTNER, DEMO_TRIP, DEMO_USER
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
        main.STORE = LoveMomentsStore(Path(cls.temp.name) / "http-test.db", travel_assistant=RuleBasedTravelAssistant(), seed_demo_data=True)
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

    def post(self, opener: urllib.request.OpenerDirector, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(req) as response:
            return json.load(response)

    def patch(self, opener: urllib.request.OpenerDirector, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
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

    def test_phase_two_dual_account_http_flow(self) -> None:
        first, second = self.opener(), self.opener()
        self.post(first, "/auth/login", {"username": "test", "password": "test123"})
        self.post(second, "/auth/login", {"username": "test1", "password": "test123"})

        status = self.put(first, "/me/status", {"status_code": "miss_you", "text": "下班见", "expires_at": None})
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
        with second.open(self.base + f"/media/{media['id']}") as response:
            self.assertTrue(response.read().startswith(b"\x89PNG\r\n\x1a\n"))

        plan = self.post(first, "/date-plans", {"title": "周末约会", "plan_date": "2026-10-18", "city": "杭州", "start_time": "14:00", "end_time": "21:00", "budget": 500, "preferences": {"interest": "美食"}})
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
        with patch.object(main.ADMIN, "record_event") as record:
            tracker.failure("ocr", "OCR 服务", RuntimeError("first"))
            tracker.failure("ocr", "OCR 服务", RuntimeError("second"))
            record.assert_not_called()
            tracker.failure("ocr", "OCR 服务", RuntimeError("third"))
            self.assertEqual(record.call_count, 1)
            self.assertEqual(record.call_args.args[0], "provider:ocr:consecutive-failure")
            self.assertEqual(record.call_args.args[4]["consecutive_failures"], 3)
            tracker.success("ocr")
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


if __name__ == "__main__":
    unittest.main()
