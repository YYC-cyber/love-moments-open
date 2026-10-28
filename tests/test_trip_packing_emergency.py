from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from love_moments.intelligent_ui import ConflictError
from love_moments.store import DEMO_COUPLE, DEMO_PARTNER, DEMO_TRIP, DEMO_USER, LoveMomentsStore


class FixedWeather:
    enabled = True

    def forecast(self, latitude, longitude, start_date, end_date, timezone_name="auto"):
        del latitude, longitude, start_date, end_date, timezone_name
        return {
            "source": "Open-Meteo",
            "source_type": "real_api",
            "evidence_at": "2026-10-28T02:00:00+00:00",
            "items": [
                {
                    "date": "2026-11-15",
                    "precipitation_probability": 80,
                    "temperature_min": 6,
                    "temperature_max": 18,
                    "weather_code": 61,
                    "label": "小雨",
                }
            ],
        }


class DisabledWeather:
    enabled = False

    def forecast(self, *args, **kwargs):
        raise AssertionError("disabled weather provider must not be called")


class UntrustedWeather:
    enabled = True

    def forecast(self, *args, **kwargs):
        del args, kwargs
        return {
            "source": "演示天气",
            "source_type": "mock",
            "evidence_at": "2026-10-28T02:00:00+00:00",
            "items": [{"date": "2026-11-15", "precipitation_probability": 99}],
        }


class TripPackingEmergencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LoveMomentsStore(Path(self.temp.name) / "packing.db", seed_demo_data=True)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        with self.store.connection() as db:
            db.execute(
                "UPDATE trips SET start_date='2026-11-15',end_date='2026-11-17',"
                "active_variant='slow',current_revision=2,version=4,status='confirmed' WHERE id=?",
                (DEMO_TRIP,),
            )
            db.execute("DELETE FROM approvals WHERE trip_id=?", (DEMO_TRIP,))
            db.executemany(
                "INSERT INTO approvals(trip_id,revision,user_id,status,updated_at) VALUES (?,?,?,'approved',?)",
                [(DEMO_TRIP, 2, DEMO_USER, now.isoformat()), (DEMO_TRIP, 2, DEMO_PARTNER, now.isoformat())],
            )
            db.execute(
                """INSERT INTO trip_destination_places(
                       trip_id,provider,provider_place_id,name,formatted_address,country,province,city,district,
                       latitude,longitude,category,confirmed_by,confirmed_at
                   ) VALUES (?,'amap','amap-destination','上海','上海市','中国','上海市','上海市','',
                             31.2304,121.4737,'城市',?,?)
                   ON CONFLICT(trip_id) DO UPDATE SET
                       provider='amap',provider_place_id='amap-destination',name='上海',formatted_address='上海市',
                       country='中国',province='上海市',city='上海市',district='',latitude=31.2304,longitude=121.4737,
                       category='城市',confirmed_by=excluded.confirmed_by,confirmed_at=excluded.confirmed_at""",
                (DEMO_TRIP, DEMO_USER, now.isoformat()),
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_packing_generation_is_additive_idempotent_and_weather_is_evidence_only(self) -> None:
        payload = {"expected_trip_version": 4, "client_request_id": "packing:generate:0001"}
        first = self.store.generate_trip_packing(DEMO_TRIP, payload, DEMO_USER, FixedWeather())
        second = self.store.generate_trip_packing(DEMO_TRIP, payload, DEMO_USER, FixedWeather())
        self.assertEqual(first, second)
        self.assertEqual(first["evidence"]["source_type"], "real_api")
        self.assertTrue(any("雨" in item["label"] for item in first["items"]))
        self.assertTrue(all(item["source_type"] == "rules" for item in first["items"]))
        self.assertEqual(len({item["label"] for item in first["items"]}), len(first["items"]))

    def test_packing_weather_falls_back_when_disabled_or_not_a_real_source(self) -> None:
        disabled = self.store.generate_trip_packing(
            DEMO_TRIP,
            {"expected_trip_version": 4, "client_request_id": "packing:disabled:0001"},
            DEMO_USER,
            DisabledWeather(),
        )
        self.assertEqual(disabled["evidence"]["label"], "天气未能核对")
        self.assertEqual(disabled["evidence"]["source_type"], "rules")

        untrusted = self.store.generate_trip_packing(
            DEMO_TRIP,
            {"expected_trip_version": 4, "client_request_id": "packing:mock-weather:0001"},
            DEMO_USER,
            UntrustedWeather(),
        )
        self.assertEqual(untrusted["evidence"]["label"], "天气未能核对")
        self.assertEqual(untrusted["evidence"]["source_type"], "rules")
        self.assertFalse(any("雨" in item["label"] for item in untrusted["items"]))

    def test_emergency_private_visibility_and_creator_only_edit(self) -> None:
        created = self.store.create_trip_emergency(
            DEMO_TRIP,
            {
                "entry_type": "booking",
                "title": "酒店订单",
                "visibility": "private",
                "details": {"provider": "携程", "booking_reference": "SECRET-REF"},
                "expected_trip_version": 4,
            },
            "emergency:create:0001",
            DEMO_USER,
        )
        self.assertIn("SECRET-REF", json.dumps(self.store.list_trip_emergency(DEMO_TRIP, DEMO_USER), ensure_ascii=False))
        self.assertNotIn("SECRET-REF", json.dumps(self.store.list_trip_emergency(DEMO_TRIP, DEMO_PARTNER), ensure_ascii=False))
        with self.assertRaises((KeyError, PermissionError)):
            self.store.update_trip_emergency(
                DEMO_TRIP,
                created["id"],
                {"title": "越权", "expected_version": created["version"]},
                DEMO_PARTNER,
            )

    def test_hospital_requires_fresh_real_healthcare_candidate_owned_by_caller(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        candidate = {
            "provider": "amap",
            "provider_place_id": "hospital-amap-1",
            "name": "上海市第一人民医院",
            "formatted_address": "上海市虹口区海宁路100号",
            "country": "中国",
            "province": "上海市",
            "city": "上海市",
            "district": "虹口区",
            "category": "医疗服务;综合医院",
            "latitude": 31.252,
            "longitude": 121.489,
            "source_type": "real_api",
            "evidence_at": now.isoformat(),
        }
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO place_search_candidates(id,user_id,query,region_hint,payload_json,expires_at,created_at) "
                "VALUES ('hospital-candidate-1',?,?,?, ?,?,?)",
                (
                    DEMO_USER,
                    "医院",
                    "上海市",
                    json.dumps(candidate, ensure_ascii=False),
                    (now + timedelta(minutes=30)).isoformat(),
                    now.isoformat(),
                ),
            )
        saved = self.store.create_trip_emergency(
            DEMO_TRIP,
            {"entry_type": "hospital", "candidate_id": "hospital-candidate-1", "visibility": "shared", "expected_trip_version": 4},
            "emergency:hospital:1",
            DEMO_USER,
        )
        self.assertEqual(saved["source_type"], "real_api")
        self.assertEqual(saved["details"]["provider_place_id"], "hospital-amap-1")

    def test_hospital_search_keeps_only_real_nearby_same_city_healthcare(self) -> None:
        class FakeMapProvider:
            is_mock = False
            name = "amap"

            def search_places(self, query, region, limit):
                self.called = (query, region, limit)
                return [
                    {
                        "provider": "amap", "provider_place_id": "sh-hospital", "name": "上海市第一人民医院",
                        "formatted_address": "上海市虹口区", "country": "中国", "province": "上海市", "city": "上海市",
                        "district": "虹口区", "category": "医疗服务;综合医院", "latitude": 31.252, "longitude": 121.489,
                    },
                    {
                        "provider": "amap", "provider_place_id": "sh-museum", "name": "上海博物馆",
                        "formatted_address": "上海市黄浦区", "country": "中国", "province": "上海市", "city": "上海市",
                        "district": "黄浦区", "category": "博物馆", "latitude": 31.23, "longitude": 121.47,
                    },
                    {
                        "provider": "amap", "provider_place_id": "suzhou-hospital", "name": "苏州市立医院",
                        "formatted_address": "苏州市姑苏区", "country": "中国", "province": "江苏省", "city": "苏州市",
                        "district": "姑苏区", "category": "医疗服务;综合医院", "latitude": 31.2989, "longitude": 120.5853,
                    },
                ]

        provider = FakeMapProvider()
        original = self.store._map_provider_for_places
        self.store._map_provider_for_places = lambda destination: provider
        try:
            result = self.store.search_trip_hospitals(DEMO_TRIP, {"query": "急救医院"}, DEMO_USER)
        finally:
            self.store._map_provider_for_places = original
        self.assertEqual(provider.called, ("急救医院", "上海市", 12))
        self.assertEqual([item["provider_place_id"] for item in result["items"]], ["sh-hospital"])

    def test_snapshot_requires_current_dual_approval_and_is_partitioned(self) -> None:
        self.store.create_trip_emergency(
            DEMO_TRIP,
            {"entry_type": "other", "title": "只给我", "details": {"note": "OWNER-ONLY"}, "visibility": "private", "expected_trip_version": 4},
            "emergency:snapshot:1",
            DEMO_USER,
        )
        owner = self.store.get_trip_offline_snapshot(DEMO_TRIP, DEMO_USER)
        partner = self.store.get_trip_offline_snapshot(DEMO_TRIP, DEMO_PARTNER)
        self.assertEqual(owner["schema_version"], 1)
        self.assertEqual(owner["trip"]["confirmed_revision"], 2)
        self.assertIn("OWNER-ONLY", json.dumps(owner, ensure_ascii=False))
        self.assertNotIn("OWNER-ONLY", json.dumps(partner, ensure_ascii=False))
        generated = datetime.fromisoformat(owner["generated_at"])
        expires = datetime.fromisoformat(owner["expires_at"])
        self.assertEqual(expires - generated, timedelta(hours=24))
        with self.store.connection() as db:
            db.execute("DELETE FROM approvals WHERE trip_id=? AND revision=? AND user_id=?", (DEMO_TRIP, 2, DEMO_PARTNER))
        with self.assertRaises(ValueError):
            self.store.get_trip_offline_snapshot(DEMO_TRIP, DEMO_USER)

    def test_packing_assignment_cas_idempotency_and_rollback(self) -> None:
        created = self.store.create_trip_packing_item(
            DEMO_TRIP,
            {"label": "护照夹", "assigned_to": DEMO_USER, "expected_trip_version": 4},
            "packing:create:0001",
            DEMO_USER,
        )
        replay = self.store.create_trip_packing_item(
            DEMO_TRIP,
            {"label": "护照夹", "assigned_to": DEMO_USER, "expected_trip_version": 4},
            "packing:create:0001",
            DEMO_USER,
        )
        self.assertEqual(created, replay)
        with self.assertRaises(ConflictError):
            self.store.create_trip_packing_item(
                DEMO_TRIP,
                {"label": "不同物品", "assigned_to": DEMO_USER, "expected_trip_version": 4},
                "packing:create:0001",
                DEMO_USER,
            )
        updated = self.store.update_trip_packing_item(
            DEMO_TRIP,
            created["id"],
            {"expected_version": 1, "checked": True, "assigned_to": DEMO_PARTNER},
            DEMO_PARTNER,
        )
        self.assertTrue(updated["checked"])
        self.assertEqual(updated["assigned_to"], DEMO_PARTNER)
        with self.assertRaises(ConflictError):
            self.store.update_trip_packing_item(
                DEMO_TRIP, created["id"], {"expected_version": 1, "checked": False}, DEMO_USER,
            )
        with self.assertRaises(ValueError):
            self.store.create_trip_packing_item(
                DEMO_TRIP,
                {"label": "越权分配", "assigned_to": "third-user", "expected_trip_version": 4},
                "packing:create:invalid",
                DEMO_USER,
            )

        original_event = self.store._event
        try:
            self.store._event = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("event failed"))
            with self.assertRaises(RuntimeError):
                self.store.create_trip_packing_item(
                    DEMO_TRIP,
                    {"label": "必须回滚", "assigned_to": "shared", "expected_trip_version": 4},
                    "packing:create:rollback",
                    DEMO_USER,
                )
        finally:
            self.store._event = original_event
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM trip_packing_items WHERE trip_id=? AND label='必须回滚'", (DEMO_TRIP,)).fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM mutation_idempotency WHERE idempotency_key='packing:create:rollback'").fetchone()[0],
                0,
            )

    def test_emergency_whitelist_permission_version_and_no_side_channel_values(self) -> None:
        sentinel = "BOOKING-SENTINEL-NOT-FOR-SIDE-CHANNELS"
        before = self.store.bootstrap(DEMO_USER)["couple"]["version"]
        private = self.store.create_trip_emergency(
            DEMO_TRIP,
            {
                "entry_type": "booking", "title": "我的订单", "visibility": "private",
                "details": {"provider": "携程", "booking_reference": sentinel},
                "expected_trip_version": 4,
            },
            "emergency:private:0001",
            DEMO_USER,
        )
        self.assertEqual(private["permission_version"], before)
        self.assertEqual(self.store.list_trip_emergency(DEMO_TRIP, DEMO_PARTNER)["count"], 0)

        shared = self.store.create_trip_emergency(
            DEMO_TRIP,
            {
                "entry_type": "other", "title": "集合说明", "visibility": "shared",
                "details": {"note": "共享但不进入事件正文"}, "expected_trip_version": 4,
            },
            "emergency:shared:0001",
            DEMO_USER,
        )
        self.assertGreater(shared["permission_version"], before)
        narrowed = self.store.update_trip_emergency(
            DEMO_TRIP,
            shared["id"],
            {"visibility": "private", "expected_version": shared["version"]},
            DEMO_USER,
        )
        self.assertGreater(narrowed["permission_version"], shared["permission_version"])
        self.assertEqual(self.store.list_trip_emergency(DEMO_TRIP, DEMO_PARTNER)["count"], 0)
        partner_events = self.store.list_realtime_events(user_id=DEMO_PARTNER)["items"]
        self.assertTrue(any(event["event_type"] == "trip.tools_updated" and event["resource_id"] == DEMO_TRIP for event in partner_events))
        with self.assertRaises(ValueError):
            self.store.create_trip_emergency(
                DEMO_TRIP,
                {
                    "entry_type": "other", "title": "非法字段", "visibility": "private",
                    "details": {"note": "ok", "passport_number": "forbidden"}, "expected_trip_version": 4,
                },
                "emergency:invalid:0001",
                DEMO_USER,
            )
        with self.store.connection() as db:
            side_channels = {
                "audit": [dict(row) for row in db.execute("SELECT action,target_type,target_id,detail FROM audit_events")],
                "events": [dict(row) for row in db.execute("SELECT event_type,resource_id,payload_json FROM realtime_events")],
                "notifications": [dict(row) for row in db.execute("SELECT title,body,url FROM in_app_notifications")],
                "push": [dict(row) for row in db.execute("SELECT event_type,tag,payload_json FROM push_outbox")],
            }
        self.assertNotIn(sentinel, json.dumps(side_channels, ensure_ascii=False))
        self.assertNotIn(private["title"], json.dumps(side_channels, ensure_ascii=False))

    def test_hospital_rejects_untrusted_expired_foreign_and_wrong_category_candidates(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        base = {
            "provider": "amap", "provider_place_id": "hospital-real-1", "name": "上海市医院",
            "formatted_address": "上海市黄浦区", "country": "中国", "province": "上海市", "city": "上海市",
            "district": "黄浦区", "category": "医疗服务;综合医院", "latitude": 31.23, "longitude": 121.47,
            "source_type": "real_api", "evidence_at": now.isoformat(),
        }
        cases = [
            ("manual-provider", {**base, "provider": "manual"}, DEMO_USER, now + timedelta(minutes=30)),
            ("ai-source", {**base, "source_type": "ai"}, DEMO_USER, now + timedelta(minutes=30)),
            ("mock-source", {**base, "source_type": "mock"}, DEMO_USER, now + timedelta(minutes=30)),
            ("unproven-cache", {**base, "source_type": "cache"}, DEMO_USER, now + timedelta(minutes=30)),
            ("wrong-category", {**base, "name": "上海市博物馆", "category": "博物馆"}, DEMO_USER, now + timedelta(minutes=30)),
            ("wrong-region", {**base, "provider_place_id": "hospital-beijing", "name": "北京市医院", "province": "北京市", "city": "北京市", "latitude": 39.9042, "longitude": 116.4074}, DEMO_USER, now + timedelta(minutes=30)),
            (
                "missing-city-nearby-region",
                {
                    **base,
                    "provider_place_id": "hospital-suzhou-no-city",
                    "name": "苏州市立医院",
                    "formatted_address": "江苏省苏州市姑苏区",
                    "province": "江苏省",
                    "city": "",
                    "district": "姑苏区",
                    "latitude": 31.2989,
                    "longitude": 120.5853,
                },
                DEMO_USER,
                now + timedelta(minutes=30),
            ),
            ("expired", base, DEMO_USER, now - timedelta(seconds=1)),
            ("foreign-owner", base, DEMO_PARTNER, now + timedelta(minutes=30)),
        ]
        with self.store.connection() as db:
            for suffix, payload, owner, expires in cases:
                db.execute(
                    "INSERT INTO place_search_candidates(id,user_id,query,region_hint,payload_json,expires_at,created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        f"candidate-{suffix}", owner, "医院", "上海市", json.dumps(payload, ensure_ascii=False),
                        expires.isoformat(), now.isoformat(),
                    ),
                )
        for index, (suffix, _, _, _) in enumerate(cases):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                self.store.create_trip_emergency(
                    DEMO_TRIP,
                    {"entry_type": "hospital", "candidate_id": f"candidate-{suffix}", "expected_trip_version": 4},
                    f"emergency:reject:{index}",
                    DEMO_USER,
                )

        cached = {**base, "source_type": "cache", "original_source_type": "real_api"}
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO place_search_candidates(id,user_id,query,region_hint,payload_json,expires_at,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    "candidate-trusted-cache", DEMO_USER, "医院", "上海市", json.dumps(cached, ensure_ascii=False),
                    (now + timedelta(minutes=30)).isoformat(), now.isoformat(),
                ),
            )
        saved = self.store.create_trip_emergency(
            DEMO_TRIP,
            {"entry_type": "hospital", "candidate_id": "candidate-trusted-cache", "expected_trip_version": 4},
            "emergency:cache:trusted",
            DEMO_USER,
        )
        self.assertEqual(saved["source_type"], "cache")

    def test_hospital_candidate_rejects_future_or_overlong_server_time_window(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        base = {
            "provider": "amap", "provider_place_id": "hospital-real-time", "name": "上海市医院",
            "formatted_address": "上海市黄浦区", "country": "中国", "province": "上海市", "city": "上海市",
            "district": "黄浦区", "category": "医疗服务;综合医院", "latitude": 31.23, "longitude": 121.47,
            "source_type": "real_api", "evidence_at": now.isoformat(),
        }
        cases = [
            ("future-created", now + timedelta(minutes=5), now + timedelta(minutes=30)),
            ("overlong-window", now - timedelta(hours=2), now + timedelta(minutes=30)),
            ("naive-created", "2026-10-28T10:00:00", now + timedelta(minutes=30)),
        ]
        with self.store.connection() as db:
            for suffix, created, expires in cases:
                created_value = created if isinstance(created, str) else created.isoformat()
                db.execute(
                    "INSERT INTO place_search_candidates(id,user_id,query,region_hint,payload_json,expires_at,created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        f"candidate-time-{suffix}", DEMO_USER, "医院", "上海市", json.dumps(base, ensure_ascii=False),
                        expires.isoformat(), created_value,
                    ),
                )
        for index, (suffix, _, _) in enumerate(cases):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                self.store.create_trip_emergency(
                    DEMO_TRIP,
                    {"entry_type": "hospital", "candidate_id": f"candidate-time-{suffix}", "expected_trip_version": 4},
                    f"emergency:time-reject:{index}",
                    DEMO_USER,
                )

    def test_snapshot_ignores_status_and_projects_current_composed_items_only(self) -> None:
        with self.store.connection() as db:
            items = db.execute(
                """SELECT i.id,v.style FROM itinerary_items i
                   JOIN trip_days d ON d.id=i.day_id JOIN plan_variants v ON v.id=d.variant_id
                   WHERE v.trip_id=? ORDER BY v.style,d.position,i.position""",
                (DEMO_TRIP,),
            ).fetchall()
            chosen = []
            styles = set()
            for row in items:
                if row["style"] not in styles:
                    chosen.append(str(row["id"]))
                    styles.add(str(row["style"]))
                if len(chosen) == 2:
                    break
            self.assertEqual(len(chosen), 2)
            db.execute("DELETE FROM plan_revisions WHERE trip_id=? AND revision=2", (DEMO_TRIP,))
            db.execute(
                "INSERT INTO plan_revisions(id,trip_id,revision,parent_revision,author_type,diff_json,reason,created_at) VALUES (?,?,2,1,'user',?,'测试混搭',?)",
                ("revision-composed-2", DEMO_TRIP, json.dumps({"selected_items": chosen}), datetime.now(timezone.utc).isoformat()),
            )
            db.execute("UPDATE trips SET status='planning' WHERE id=?", (DEMO_TRIP,))
        snapshot = self.store.get_trip_offline_snapshot(DEMO_TRIP, DEMO_USER)
        projected = [item["id"] for day in snapshot["itinerary"] for item in day["items"]]
        self.assertEqual(set(projected), set(chosen))
        self.assertEqual(len(projected), len(chosen))
        serialized = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("details_json", serialized)
        self.assertNotIn("diff_json", serialized)
        self.assertNotIn("client_request_id", serialized)

    def test_trip_delete_and_restore_advance_permission_scope_without_rewriting_tools(self) -> None:
        packing = self.store.create_trip_packing_item(
            DEMO_TRIP,
            {"label": "保留的人工行李", "assigned_to": "shared", "expected_trip_version": 4},
            "packing:lifecycle:0001",
            DEMO_USER,
        )
        emergency = self.store.create_trip_emergency(
            DEMO_TRIP,
            {"entry_type": "other", "title": "保留的应急卡", "details": {"note": "不要重写"}, "expected_trip_version": 4},
            "emergency:lifecycle:0001",
            DEMO_USER,
        )
        before = self.store.bootstrap(DEMO_USER)["couple"]["version"]
        deleted = self.store.delete_trip(DEMO_TRIP, 4, DEMO_USER)
        self.assertGreater(deleted["permission_version"], before)
        with self.assertRaises(KeyError):
            self.store.get_trip_offline_snapshot(DEMO_TRIP, DEMO_USER)
        restored = self.store.restore_trip(DEMO_TRIP, deleted["version"], DEMO_USER)
        self.assertGreater(restored["permission_version"], deleted["permission_version"])
        self.assertTrue(any(item["id"] == packing["id"] for item in self.store.list_trip_packing(DEMO_TRIP, DEMO_USER)["items"]))
        self.assertTrue(any(item["id"] == emergency["id"] for item in self.store.list_trip_emergency(DEMO_TRIP, DEMO_USER)["items"]))

    def test_unlink_and_account_delete_downgrade_person_assignments_without_deleting_items(self) -> None:
        shared_item = self.store.create_trip_packing_item(
            DEMO_TRIP,
            {"label": "原本共同携带", "assigned_to": "shared", "expected_trip_version": 4},
            "packing:lifecycle:shared",
            DEMO_USER,
        )
        not_needed_item = self.store.create_trip_packing_item(
            DEMO_TRIP,
            {"label": "原本不需要", "assigned_to": "none", "expected_trip_version": 4},
            "packing:lifecycle:not-needed",
            DEMO_USER,
        )
        deleted_person_item = self.store.create_trip_packing_item(
            DEMO_TRIP,
            {"label": "已删除的个人行李", "assigned_to": DEMO_USER, "expected_trip_version": 4},
            "packing:lifecycle:deleted-person",
            DEMO_USER,
        )
        self.store.delete_trip_packing_item(
            DEMO_TRIP, deleted_person_item["id"], deleted_person_item["version"], DEMO_USER,
        )
        unlink_item = self.store.create_trip_packing_item(
            DEMO_TRIP,
            {"label": "解绑后仍保留", "assigned_to": DEMO_USER, "expected_trip_version": 4},
            "packing:lifecycle:unlink",
            DEMO_USER,
        )
        with self.store.connection() as db:
            untouched_before_unlink = {
                row["id"]: dict(row)
                for row in db.execute(
                    "SELECT * FROM trip_packing_items WHERE id IN (?,?,?)",
                    (shared_item["id"], not_needed_item["id"], deleted_person_item["id"]),
                ).fetchall()
            }
        with self.store.connection() as db:
            self.store.apply_foundation_lifecycle(db, event="unlink", couple_id=DEMO_COUPLE)
        after_unlink = self.store.list_trip_packing(DEMO_TRIP, DEMO_USER)["items"]
        preserved = next(item for item in after_unlink if item["id"] == unlink_item["id"])
        self.assertEqual(preserved["label"], "解绑后仍保留")
        self.assertEqual(preserved["assigned_to"], "shared")
        with self.store.connection() as db:
            untouched = {
                row["id"]: dict(row)
                for row in db.execute(
                    "SELECT * FROM trip_packing_items WHERE id IN (?,?,?)",
                    (shared_item["id"], not_needed_item["id"], deleted_person_item["id"]),
                ).fetchall()
            }
        self.assertEqual(untouched, untouched_before_unlink)

        delete_item = self.store.create_trip_packing_item(
            DEMO_TRIP,
            # Deleting either account invalidates the couple assignment scope,
            # including an item assigned to the other (surviving) member.
            {"label": "注销后仍保留", "assigned_to": DEMO_USER, "expected_trip_version": 4},
            "packing:lifecycle:account-delete",
            DEMO_USER,
        )
        with self.store.connection() as db:
            self.store.apply_foundation_lifecycle(
                db,
                event="account_delete",
                couple_id=DEMO_COUPLE,
                user_id=DEMO_PARTNER,
            )
        after_delete = self.store.list_trip_packing(DEMO_TRIP, DEMO_USER)["items"]
        preserved = next(item for item in after_delete if item["id"] == delete_item["id"])
        self.assertEqual(preserved["label"], "注销后仍保留")
        self.assertEqual(preserved["assigned_to"], "shared")
        with self.store.connection() as db:
            untouched_after_delete = {
                row["id"]: dict(row)
                for row in db.execute(
                    "SELECT * FROM trip_packing_items WHERE id IN (?,?,?)",
                    (shared_item["id"], not_needed_item["id"], deleted_person_item["id"]),
                ).fetchall()
            }
        self.assertEqual(untouched_after_delete, untouched)


if __name__ == "__main__":
    unittest.main()
