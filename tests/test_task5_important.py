from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from love_moments.intelligent_ui import ConflictError
from love_moments.providers import LocalDatePlanner
from love_moments.store import DEMO_PARTNER, DEMO_USER, LoveMomentsStore
from love_moments.weather import OpenMeteoWeather


class TaskFiveImportantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LoveMomentsStore(Path(self.temp.name) / "task-five.db", seed_demo_data=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def candidate(self, *, city: str = "杭州市", name: str = "西湖", provider: str = "amap", category: str = "风景名胜") -> dict:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        candidate_id = f"place-{name}-{now.timestamp()}"
        payload = {
            "provider": provider,
            "provider_place_id": f"{provider}-{name}",
            "name": name,
            "formatted_address": f"浙江省{city}西湖区",
            "country": "中国",
            "province": "浙江省",
            "city": city,
            "district": "西湖区",
            "latitude": 30.245,
            "longitude": 120.145,
            "category": category,
            "source_type": "real_api",
            "evidence_at": "2026-10-20T08:00:00+00:00",
        }
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO place_search_candidates(id,user_id,query,region_hint,payload_json,expires_at,created_at) VALUES (?,?,?,?,?,?,?)",
                (candidate_id, DEMO_USER, name, city, json.dumps(payload, ensure_ascii=False),
                 (now + timedelta(minutes=30)).isoformat(), "2026-10-20T08:00:00+00:00"),
            )
        return {"candidate_id": candidate_id, **payload}

    def request(self, candidate_id: str, **overrides) -> dict:
        value = {
            "plan_date": "2026-11-02", "city": "用户输入的搜索词", "start_time": "13:00",
            "end_time": "20:00", "budget": 500, "preferences": {"interests": ["展览"]},
            "place_candidate_id": candidate_id,
        }
        value.update(overrides)
        return value

    def test_slow_generation_rechecks_explicit_busy_context_before_saving(self) -> None:
        candidate = self.candidate()
        store = self.store

        class MutatingPlanner:
            def generate_variants(inner_self, payload):
                store.create_calendar_entry({
                    "entry_type": "event", "title": "刚刚新增的占用",
                    "starts_at": "2026-11-02T15:00:00+08:00",
                    "ends_at": "2026-11-02T17:00:00+08:00", "all_day": False,
                    "participant_ids": [DEMO_PARTNER], "blocks_time": True,
                }, DEMO_PARTNER)
                return LocalDatePlanner().generate_variants(payload)

        self.store.date_planner = MutatingPlanner()
        with self.assertRaisesRegex(ConflictError, "日历|空闲|重新生成"):
            self.store.create_date_plan(self.request(candidate["candidate_id"]), DEMO_USER)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM date_plan_variants").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT status FROM date_plans ORDER BY created_at DESC LIMIT 1").fetchone()[0], "draft")

    def test_slow_generation_rechecks_current_couple_members_before_saving(self) -> None:
        candidate = self.candidate(name="灵隐寺")
        store = self.store

        class UnbindingPlanner:
            def generate_variants(inner_self, payload):
                with store.connection() as db:
                    db.execute("UPDATE couples SET status='unbound' WHERE status='active'")
                return LocalDatePlanner().generate_variants(payload)

        self.store.date_planner = UnbindingPlanner()
        with self.assertRaises(ConflictError):
            self.store.create_date_plan(self.request(candidate["candidate_id"]), DEMO_USER)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM date_plan_variants").fetchone()[0], 0)

    def test_final_immediate_transaction_rechecks_after_preflight_before_writing(self) -> None:
        candidate = self.candidate()
        original = self.store._date_context_snapshot
        calls = 0

        def race(*args, **kwargs):
            nonlocal calls
            value = original(*args, **kwargs)
            calls += 1
            if calls == 3:
                self.store.create_calendar_entry({
                    "entry_type": "event", "title": "最终写入前新增占用",
                    "starts_at": "2026-11-02T15:00:00+08:00",
                    "ends_at": "2026-11-02T17:00:00+08:00", "all_day": False,
                    "participant_ids": [DEMO_USER], "blocks_time": True,
                }, DEMO_USER)
            return value

        self.store._date_context_snapshot = race
        with self.assertRaisesRegex(ConflictError, "日历|空闲|重新生成"):
            self.store.create_date_plan(self.request(candidate["candidate_id"]), DEMO_USER)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM date_plan_variants").fetchone()[0], 0)

    def test_busy_intervals_use_half_open_local_day_intersection(self) -> None:
        candidate = self.candidate()
        rows = [
            ("previous-overlap", "2026-11-01T23:30:00+08:00", "2026-11-02T01:00:00+08:00"),
            ("left-adjacent", "2026-11-01T22:00:00+08:00", "2026-11-02T00:00:00+08:00"),
            ("next-overlap", "2026-11-02T23:30:00+08:00", "2026-11-03T01:00:00+08:00"),
            ("right-adjacent", "2026-11-03T00:00:00+08:00", "2026-11-03T01:00:00+08:00"),
        ]
        for title, starts_at, ends_at in rows:
            self.store.create_calendar_entry({
                "entry_type": "event", "title": title, "starts_at": starts_at, "ends_at": ends_at,
                "all_day": False, "participant_ids": [DEMO_USER], "blocks_time": True,
            }, DEMO_USER)
        context = self.store._date_context_snapshot(
            user_id=DEMO_USER, plan_date="2026-11-02", start_time="00:00", end_time="23:59",
            place_candidate_id=candidate["candidate_id"], preferences={}, weather_provider=None,
        )
        self.assertEqual(context["busy_intervals"], [
            {"start": "00:00", "end": "01:00", "all_day": False},
            {"start": "23:30", "end": "23:59", "all_day": False},
        ])

    def test_unconfirmed_manual_scope_does_not_invent_cross_timezone_busy_time(self) -> None:
        manual = self.store.search_destination_places({
            "query": "巴黎", "region_hint": "法国巴黎", "include_global": False,
        }, DEMO_USER)["items"][0]
        self.store.create_calendar_entry({
            "entry_type": "event", "title": "北京时间安排",
            "starts_at": "2026-11-02T14:00:00+08:00", "ends_at": "2026-11-02T16:00:00+08:00",
            "all_day": False, "participant_ids": [DEMO_USER], "blocks_time": True,
        }, DEMO_USER)
        context = self.store._date_context_snapshot(
            user_id=DEMO_USER, plan_date="2026-11-02", start_time="09:00", end_time="20:00",
            place_candidate_id=manual["candidate_id"], preferences={}, weather_provider=None,
        )
        self.assertFalse(context["timezone_confirmed"])
        self.assertEqual(context["busy_intervals"], [])

    def test_date_city_is_derived_from_confirmed_server_candidate_not_client_text(self) -> None:
        candidate = self.candidate(city="上海市", name="上海迪士尼度假区")

        class CapturingPlanner:
            def __init__(inner_self): inner_self.payload = None
            def generate_variants(inner_self, payload):
                inner_self.payload = payload
                return LocalDatePlanner().generate_variants(payload)

        planner = CapturingPlanner()
        self.store.date_planner = planner
        plan = self.store.create_date_plan(self.request(
            candidate["candidate_id"], city="北京迪士尼", plan_date="2026-11-05",
        ), DEMO_USER)
        self.assertEqual(plan["city"], "上海市")
        self.assertEqual(planner.payload["city"], "上海市")
        self.assertNotIn("北京", json.dumps(planner.payload, ensure_ascii=False))

    def test_manual_place_candidate_is_honest_about_unconfirmed_scope(self) -> None:
        item = self.store.search_destination_places({
            "query": "小镇", "region_hint": "法国巴黎", "include_global": False,
        }, DEMO_USER)["items"][0]
        self.assertEqual(item["source_type"], "human")
        self.assertFalse(item["scope_confirmed"])
        self.assertEqual(item["scope_status"], "unconfirmed")

    def test_destination_candidate_preserves_original_evidence_and_cache_state(self) -> None:
        candidate = self.candidate(name="缓存景点")
        with self.store.connection() as db:
            row = db.execute(
                "SELECT payload_json,created_at FROM place_search_candidates WHERE id=?",
                (candidate["candidate_id"],),
            ).fetchone()
            payload = json.loads(row["payload_json"])
            payload["source_type"] = "cache"
            payload["original_source_type"] = "real_api"
            payload["evidence_at"] = "2026-10-20T08:00:00+00:00"
            db.execute(
                "UPDATE place_search_candidates SET payload_json=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), candidate["candidate_id"]),
            )
        value = self.store._destination_candidate(candidate["candidate_id"], DEMO_USER)
        self.assertEqual(value["source_type"], "cache")
        self.assertEqual(value["original_source_type"], "real_api")
        self.assertEqual(value["evidence_at"], "2026-10-20T08:00:00+00:00")
        self.assertEqual(value["created_at"], "2026-10-20T08:00:00+00:00")
        self.assertTrue(value["cache_hit"])

    def test_open_meteo_cache_keeps_original_provider_and_evidence_time(self) -> None:
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self):
                return json.dumps({
                    "timezone": "Asia/Shanghai",
                    "daily": {
                        "time": ["2026-11-02"], "weather_code": [61],
                        "precipitation_probability_max": [70],
                        "temperature_2m_max": [24], "temperature_2m_min": [18],
                    },
                }).encode()

        weather = OpenMeteoWeather(enabled=True)
        with patch("love_moments.weather.request.urlopen", return_value=Response()) as request_call:
            fresh = weather.forecast(30.2, 120.1, "2026-11-02", "2026-11-02", "Asia/Shanghai")
            cached = weather.forecast(30.2, 120.1, "2026-11-02", "2026-11-02", "Asia/Shanghai")
        self.assertEqual(request_call.call_count, 1)
        self.assertEqual(fresh["source_type"], "real_api")
        self.assertEqual(fresh["provider"], "Open-Meteo")
        self.assertTrue(fresh["evidence_at"])
        self.assertEqual(cached["source_type"], "cache")
        self.assertEqual(cached["original_source_type"], "real_api")
        self.assertEqual(cached["provider"], "Open-Meteo")
        self.assertEqual(cached["evidence_at"], fresh["evidence_at"])
        self.assertGreaterEqual(cached["cache_age_seconds"], 0)

    def test_date_context_builds_bounded_same_area_server_candidate_pool(self) -> None:
        anchor = self.candidate(name="浙江美术馆", category="文化艺术")
        nearby = self.candidate(name="杭州博物馆", category="文化艺术")
        unrelated = self.candidate(name="杭州东站", category="交通设施")
        far = self.candidate(city="上海市", name="上海博物馆")
        context = self.store._date_context_snapshot(
            user_id=DEMO_USER, plan_date="2026-11-02", start_time="13:00", end_time="20:00",
            place_candidate_id=anchor["candidate_id"], preferences={"interests": ["展览"]}, weather_provider=None,
        )
        ids = {item["candidate_id"] for item in context["place_candidates"]}
        self.assertIn(anchor["candidate_id"], ids)
        self.assertIn(nearby["candidate_id"], ids)
        self.assertNotIn(unrelated["candidate_id"], ids)
        self.assertNotIn(far["candidate_id"], ids)
        self.assertLessEqual(len(ids), 6)

    def test_candidate_pool_matches_explicit_interest_activity_category(self) -> None:
        anchor = self.candidate(name="湖滨公园", category="风景名胜")
        restaurant = self.candidate(name="湖滨杭帮菜", category="餐饮服务")
        museum = self.candidate(name="杭州博物馆", category="文化艺术")
        context = self.store._date_context_snapshot(
            user_id=DEMO_USER, plan_date="2026-11-02", start_time="13:00", end_time="20:00",
            place_candidate_id=anchor["candidate_id"], preferences={"interests": ["美食"]}, weather_provider=None,
        )
        ids = {item["candidate_id"] for item in context["place_candidates"]}
        self.assertIn(restaurant["candidate_id"], ids)
        self.assertNotIn(anchor["candidate_id"], ids)
        self.assertNotIn(museum["candidate_id"], ids)

    def test_activity_candidate_search_is_bounded_and_reuses_server_cache(self) -> None:
        anchor = self.candidate(name="湖滨公园", category="风景名胜")

        class Provider:
            is_mock = False
            def __init__(inner_self): inner_self.calls = 0
            def search_places(inner_self, keyword, region_hint, limit):
                inner_self.calls += 1
                return [{
                    "provider": "amap", "provider_place_id": "food-1", "name": "湖滨杭帮菜",
                    "formatted_address": "浙江省杭州市湖滨", "country": "中国", "province": "浙江省",
                    "city": "杭州市", "district": "上城区", "latitude": 30.25, "longitude": 120.16,
                    "category": "餐饮服务",
                }]

        provider = Provider()
        with patch("love_moments.phase_three.map_provider_from_env", return_value=provider):
            first = self.store._date_context_snapshot(
                user_id=DEMO_USER, plan_date="2026-11-02", start_time="13:00", end_time="20:00",
                place_candidate_id=anchor["candidate_id"], preferences={"interests": ["美食"]}, weather_provider=None,
            )
            second = self.store._date_context_snapshot(
                user_id=DEMO_USER, plan_date="2026-11-02", start_time="13:00", end_time="20:00",
                place_candidate_id=anchor["candidate_id"], preferences={"interests": ["美食"]}, weather_provider=None,
            )
        self.assertEqual(provider.calls, 1)
        self.assertEqual(first["place_candidates"][0]["name"], "湖滨杭帮菜")
        self.assertEqual(second["place_candidates"][0]["name"], "湖滨杭帮菜")
        self.assertEqual(second["place_candidates"][0]["source_type"], "real_api")

    def test_activity_candidate_search_expands_a_single_compatible_anchor_once(self) -> None:
        anchor = self.candidate(name="浙江美术馆", category="文化艺术")

        class Provider:
            is_mock = False
            def __init__(inner_self): inner_self.calls = 0
            def search_places(inner_self, keyword, region_hint, limit):
                inner_self.calls += 1
                return [{
                    "provider": "amap", "provider_place_id": "culture-2", "name": "杭州博物馆",
                    "formatted_address": "浙江省杭州市上城区", "country": "中国", "province": "浙江省",
                    "city": "杭州市", "district": "上城区", "latitude": 30.24, "longitude": 120.18,
                    "category": "文化艺术",
                }]

        provider = Provider()
        with patch("love_moments.phase_three.map_provider_from_env", return_value=provider):
            first = self.store._date_context_snapshot(
                user_id=DEMO_USER, plan_date="2026-11-02", start_time="13:00", end_time="20:00",
                place_candidate_id=anchor["candidate_id"], preferences={"interests": ["展览"]}, weather_provider=None,
            )
            second = self.store._date_context_snapshot(
                user_id=DEMO_USER, plan_date="2026-11-02", start_time="13:00", end_time="20:00",
                place_candidate_id=anchor["candidate_id"], preferences={"interests": ["展览"]}, weather_provider=None,
            )
        self.assertEqual(provider.calls, 1)
        self.assertEqual({item["name"] for item in first["place_candidates"]}, {"浙江美术馆", "杭州博物馆"})
        self.assertEqual({item["name"] for item in second["place_candidates"]}, {"浙江美术馆", "杭州博物馆"})

    def test_candidate_pool_dedupes_existing_rows_by_provider_place_identity(self) -> None:
        anchor = self.candidate(name="浙江美术馆", category="文化艺术")
        duplicate = dict(anchor)
        duplicate.update({"name": "浙江美术馆（另一个候选ID）", "latitude": 30.24501})
        duplicate_id = "place-duplicate-provider-id"
        now = datetime.now(timezone.utc).replace(microsecond=0)
        duplicate.pop("candidate_id", None)
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO place_search_candidates(id,user_id,query,region_hint,payload_json,expires_at,created_at) VALUES (?,?,?,?,?,?,?)",
                (duplicate_id, DEMO_USER, "美术馆", "杭州市", json.dumps(duplicate, ensure_ascii=False),
                 (now + timedelta(minutes=30)).isoformat(), now.isoformat()),
            )
        context = self.store._date_context_snapshot(
            user_id=DEMO_USER, plan_date="2026-11-02", start_time="13:00", end_time="20:00",
            place_candidate_id=anchor["candidate_id"], preferences={"interests": ["展览"]}, weather_provider=None,
        )
        physical = [item for item in context["place_candidates"] if item.get("provider_place_id") == anchor["provider_place_id"]]
        self.assertEqual([item["candidate_id"] for item in physical], [anchor["candidate_id"]])

    def test_candidate_pool_rejects_duplicate_provider_search_result(self) -> None:
        anchor = self.candidate(name="浙江美术馆", category="文化艺术")

        class Provider:
            is_mock = False
            def search_places(inner_self, keyword, region_hint, limit):
                return [{
                    "provider": "amap", "provider_place_id": anchor["provider_place_id"],
                    "name": "浙江美术馆另一搜索标题", "formatted_address": "浙江省杭州市西湖区",
                    "country": "中国", "province": "浙江省", "city": "杭州市", "district": "西湖区",
                    "latitude": 30.24501, "longitude": 120.14501, "category": "文化艺术",
                }]

        with patch("love_moments.phase_three.map_provider_from_env", return_value=Provider()):
            context = self.store._date_context_snapshot(
                user_id=DEMO_USER, plan_date="2026-11-02", start_time="13:00", end_time="20:00",
                place_candidate_id=anchor["candidate_id"], preferences={"interests": ["展览"]}, weather_provider=None,
            )
        self.assertEqual(len(context["place_candidates"]), 1)
        self.assertEqual(context["place_candidates"][0]["candidate_id"], anchor["candidate_id"])

    def test_legacy_plan_regenerates_from_persisted_snapshot_after_candidate_expiry(self) -> None:
        candidate = self.candidate(name="浙江美术馆", category="文化艺术")
        plan = self.store.create_date_plan(self.request(candidate["candidate_id"]), DEMO_USER)
        with self.store.connection() as db:
            stored = db.execute("SELECT preferences_json FROM date_plans WHERE id=?", (plan["id"],)).fetchone()
            preferences = json.loads(stored["preferences_json"])
            preferences.pop("generation_context", None)
            preferences.pop("place_candidates", None)
            db.execute("UPDATE date_plans SET status='draft',preferences_json=? WHERE id=?", (json.dumps(preferences, ensure_ascii=False), plan["id"]))
            db.execute("DELETE FROM date_plan_variants WHERE date_plan_id=?", (plan["id"],))
            db.execute("DELETE FROM place_search_candidates WHERE id=?", (candidate["candidate_id"],))
        regenerated = self.store.generate_date_plan(plan["id"], DEMO_USER)
        self.assertEqual(len(regenerated["variants"]), 5)
        self.assertTrue(any(
            item.get("placeCandidateId") == candidate["candidate_id"]
            for variant in regenerated["variants"] for item in variant["items"]
        ))
        self.assertTrue(all(
            not item.get("placeCandidateId") or item["placeCandidateId"] == candidate["candidate_id"]
            for variant in regenerated["variants"] for item in variant["items"]
        ))

    def test_legacy_city_only_plan_regenerates_as_unverified_placeholders(self) -> None:
        plan = self.store.create_date_plan({
            "plan_date": "2026-11-02", "city": "旧文本杭州", "start_time": "13:00",
            "end_time": "20:00", "budget": 500,
        }, DEMO_USER)
        with self.store.connection() as db:
            stored = db.execute("SELECT preferences_json FROM date_plans WHERE id=?", (plan["id"],)).fetchone()
            preferences = json.loads(stored["preferences_json"])
            preferences.pop("generation_context", None)
            db.execute("UPDATE date_plans SET status='draft',preferences_json=? WHERE id=?", (json.dumps(preferences, ensure_ascii=False), plan["id"]))
            db.execute("DELETE FROM date_plan_variants WHERE date_plan_id=?", (plan["id"],))
        regenerated = self.store.generate_date_plan(plan["id"], DEMO_USER)
        self.assertEqual(len(regenerated["variants"]), 5)
        self.assertTrue(all("地点待选择" in item["title"] for variant in regenerated["variants"] for item in variant["items"]))
        self.assertIn("重新选择", regenerated["generation_message"])

    def test_recurring_busy_intervals_expand_on_target_local_date(self) -> None:
        candidate = self.candidate()
        rows = [
            ("daily", "2026-02-20T13:00:00+08:00", "2026-02-20T13:30:00+08:00", "daily"),
            ("weekly", "2026-10-26T14:00:00+08:00", "2026-10-26T14:30:00+08:00", "weekly"),
            ("monthly", "2026-01-31T15:00:00+08:00", "2026-01-31T15:30:00+08:00", "monthly"),
            ("yearly", "2024-02-29T16:00:00+08:00", "2024-02-29T16:30:00+08:00", "yearly"),
        ]
        for title, starts_at, ends_at, recurrence in rows:
            self.store.create_calendar_entry({
                "entry_type": "event", "title": title, "starts_at": starts_at, "ends_at": ends_at,
                "all_day": False, "recurrence": recurrence,
                "participant_ids": [DEMO_USER], "blocks_time": True,
            }, DEMO_USER)
        february = self.store._date_context_snapshot(
            user_id=DEMO_USER, plan_date="2026-02-28", start_time="12:00", end_time="18:00",
            place_candidate_id=candidate["candidate_id"], preferences={}, weather_provider=None,
        )
        self.assertEqual(february["busy_intervals"], [
            {"start": "13:00", "end": "13:30", "all_day": False},
            {"start": "15:00", "end": "15:30", "all_day": False},
            {"start": "16:00", "end": "16:30", "all_day": False},
        ])
        weekly = self.store._date_context_snapshot(
            user_id=DEMO_USER, plan_date="2026-11-02", start_time="13:30", end_time="18:00",
            place_candidate_id=candidate["candidate_id"], preferences={}, weather_provider=None,
        )
        self.assertEqual(weekly["busy_intervals"], [{"start": "14:00", "end": "14:30", "all_day": False}])

    def test_same_day_all_day_end_normalizes_to_exclusive_next_day_and_blocks(self) -> None:
        candidate = self.candidate()
        item = self.store.create_calendar_entry({
            "entry_type": "event", "title": "全天安排", "starts_at": "2026-11-02",
            "ends_at": "2026-11-02", "all_day": True, "recurrence": "none",
            "participant_ids": [DEMO_USER], "blocks_time": True,
        }, DEMO_USER)
        self.assertEqual(item["ends_at"], "2026-11-03")
        with self.assertRaisesRegex(ValueError, "全天"):
            self.store._date_context_snapshot(
                user_id=DEMO_USER, plan_date="2026-11-02", start_time="13:00", end_time="20:00",
                place_candidate_id=candidate["candidate_id"], preferences={}, weather_provider=None,
            )

    def test_yearly_all_day_leap_occurrence_blocks_last_february_day(self) -> None:
        candidate = self.candidate()
        self.store.create_calendar_entry({
            "entry_type": "anniversary", "title": "闰日纪念安排", "starts_at": "2024-02-29",
            "all_day": True, "recurrence": "yearly", "participant_ids": [DEMO_PARTNER],
            "blocks_time": True,
        }, DEMO_PARTNER)
        with self.assertRaisesRegex(ValueError, "全天"):
            self.store._date_context_snapshot(
                user_id=DEMO_USER, plan_date="2026-02-28", start_time="13:00", end_time="20:00",
                place_candidate_id=candidate["candidate_id"], preferences={}, weather_provider=None,
            )


class TaskFiveCalendarClientContractTests(unittest.TestCase):
    def test_h5_and_react_native_calendar_support_complete_timed_contract(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "static/index.html").read_text(encoding="utf-8")
        web = (root / "static/app.js").read_text(encoding="utf-8")
        mobile = (root / "clients/mobile/App.tsx").read_text(encoding="utf-8")
        for token in ("calendarAllDay", "calendarStart", "calendarEnd", "calendarParticipants", "calendarBlocksTime"):
            self.assertIn(token, html)
        self.assertIn("all_day: allDay", web)
        self.assertIn("ends_at: allDay ? (endsAt || null) : calendarApiTime(endsAt, false)", web)
        self.assertIn('type="datetime-local"', html)
        for token in ("allDay", "startsAt", "endsAt", "participantIds", "blocksTime"):
            self.assertIn(token, mobile)
        self.assertIn("participant_ids: lifeForm.participantIds", mobile)
        self.assertIn("blocks_time: lifeForm.blocksTime", mobile)
        self.assertIn("recurrence: lifeForm.recurrence", mobile)
        self.assertIn("recurrence: item.recurrence", mobile)
        self.assertIn("ends_at: lifeForm.allDay ? (lifeForm.endsAt || null) : calendarApiTime(lifeForm.endsAt, false)", mobile)
        self.assertIn('export type CalendarRecurrence = "none" | "daily" | "weekly" | "monthly" | "yearly"', (root / "packages/domain-client/src/index.ts").read_text(encoding="utf-8"))
        self.assertIn('<option value="daily">每天</option>', html)


class TaskFivePlacePoolTests(unittest.TestCase):
    def test_local_planner_uses_only_bounded_pool_ids_and_marks_commute_weather(self) -> None:
        pool = [
            {"candidate_id": "p1", "name": "西湖", "provider": "amap", "provider_place_id": "1", "latitude": 30.245, "longitude": 120.145, "source_type": "real_api"},
            {"candidate_id": "p2", "name": "浙江美术馆", "provider": "amap", "provider_place_id": "2", "latitude": 30.234, "longitude": 120.151, "source_type": "cache"},
            {"candidate_id": "p3", "name": "湖滨步行街", "provider": "amap", "provider_place_id": "3", "latitude": 30.256, "longitude": 120.168, "source_type": "real_api"},
        ]
        variants = LocalDatePlanner().generate_variants({
            "city": "杭州市", "start_time": "13:00", "end_time": "20:00", "budget_cents": 50000,
            "busy_intervals": [], "place_candidates": pool,
            "weather": {"source_type": "real_api", "provider": "Open-Meteo", "evidence_at": "2026-10-20T08:00:00Z", "summary": "小雨", "precipitation_probability": 70},
        })
        allowed = {item["candidate_id"] for item in pool}
        self.assertEqual(len(variants), 5)
        for variant in variants:
            for item in variant["items"]:
                if not item["needsPlaceSelection"]:
                    self.assertIn(item["placeCandidateId"], allowed)
                self.assertEqual(item["commuteSource"], "rules")
                self.assertGreaterEqual(item["commuteMinutes"], 0)
                self.assertIn("降雨概率 70%", item["weatherRisk"])

    def test_no_real_pool_never_invents_named_place(self) -> None:
        variants = LocalDatePlanner().generate_variants({
            "city": "杭州市", "start_time": "13:00", "end_time": "20:00", "budget_cents": 50000,
            "busy_intervals": [], "place_candidates": [], "weather": {"source_type": "unavailable"},
        })
        self.assertTrue(all("地点待选择" in item["title"] for plan in variants for item in plan["items"]))
        self.assertTrue(all("天气数据不可用" in item["weatherRisk"] for plan in variants for item in plan["items"]))

    def test_one_candidate_is_never_reused_twice_inside_a_variant(self) -> None:
        only = {"candidate_id": "p1", "name": "浙江美术馆", "provider": "amap", "provider_place_id": "1", "latitude": 30.245, "longitude": 120.145, "source_type": "real_api"}
        variants = LocalDatePlanner().generate_variants({
            "city": "杭州市", "start_time": "13:00", "end_time": "20:00", "budget_cents": 50000,
            "busy_intervals": [], "place_candidates": [only], "weather": {"source_type": "unavailable"},
        })
        for variant in variants:
            ids = [item.get("placeCandidateId") for item in variant["items"] if item.get("placeCandidateId")]
            self.assertEqual(ids, ["p1"])
            self.assertEqual(sum("地点待选择" in item["title"] for item in variant["items"]), 1)

    def test_local_planner_dedupes_fallback_physical_identity_but_keeps_far_same_name(self) -> None:
        pool = [
            {"candidate_id": "p1", "name": "同名餐厅", "provider": "amap", "provider_place_id": "", "formatted_address": "杭州市湖滨一号", "latitude": 30.245001, "longitude": 120.145001, "source_type": "real_api"},
            {"candidate_id": "p2", "name": " 同名餐厅 ", "provider": "geoapify", "provider_place_id": "", "formatted_address": "杭州市湖滨一号", "latitude": 30.245004, "longitude": 120.145004, "source_type": "real_api"},
            {"candidate_id": "p3", "name": "同名餐厅", "provider": "geoapify", "provider_place_id": "", "formatted_address": "上海市浦东新区二号", "latitude": 31.225, "longitude": 121.505, "source_type": "real_api"},
        ]
        variants = LocalDatePlanner().generate_variants({
            "city": "杭州市", "start_time": "13:00", "end_time": "20:00", "budget_cents": 50000,
            "busy_intervals": [], "place_candidates": pool, "weather": {"source_type": "unavailable"},
        })
        for variant in variants:
            ids = [item.get("placeCandidateId") for item in variant["items"] if item.get("placeCandidateId")]
            self.assertEqual(set(ids), {"p1", "p3"})


if __name__ == "__main__":
    unittest.main()
