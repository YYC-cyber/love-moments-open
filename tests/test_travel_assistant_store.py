from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from love_moments.intelligent_ui import ConflictError
from love_moments.schema import SCHEMA_VERSION
from love_moments.store import DEMO_PARTNER, DEMO_TRIP, DEMO_USER, LoveMomentsStore
from love_moments.travel_assistant import RuleBasedTravelAssistant


class TravelAssistantStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ)
        os.environ["AMAP_WEB_KEY"] = ""
        os.environ["SERPAPI_ENABLED"] = "false"
        self.store = LoveMomentsStore(
            Path(self.temp.name) / "assistant.db",
            travel_assistant=RuleBasedTravelAssistant(),
            seed_demo_data=True,
        )
        candidate = self.store.search_destination_places(
            {"query": "杭州", "region_hint": "杭州市", "include_global": False}, DEMO_USER,
        )["items"][0]
        self.trip_id = self.store.create_trip({
            "destination": "杭州", "destination_candidate_id": candidate["candidate_id"],
            "start_date": "2026-12-01", "end_date": "2026-12-03", "budget": 5000,
        }, DEMO_USER)["id"]

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.env)
        self.temp.cleanup()

    def test_schema_and_shared_plan_comparison_history(self) -> None:
        with self.store.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("trip_assistant_messages", tables)
        self.assertIn("trip_assistant_action_drafts", tables)
        result = self.store.ask_trip_assistant(self.trip_id, "比较五套方案哪个更轻松", DEMO_USER, "ask-plan-00000001")
        self.assertEqual(result["assistant"]["intent_type"], "plan_compare")
        self.assertEqual(len(result["assistant"]["cards"]), 5)
        partner = self.store.list_trip_assistant(self.trip_id, DEMO_PARTNER)
        self.assertEqual([item["role"] for item in partner["items"][-2:]], ["user", "assistant"])

    def test_duplicate_message_idempotency_does_not_repeat_history(self) -> None:
        first = self.store.ask_trip_assistant(self.trip_id, "比较五套方案", DEMO_USER, "same-key-00000001")
        second = self.store.ask_trip_assistant(self.trip_id, "比较五套方案", DEMO_USER, "same-key-00000001")
        self.assertEqual(first["assistant"]["id"], second["assistant"]["id"])
        self.assertEqual(len(self.store.list_trip_assistant(self.trip_id, DEMO_USER)["items"]), 2)

    def test_history_is_bounded_and_other_couple_cannot_read_it(self) -> None:
        with self.store.connection() as db:
            couple_id = db.execute("SELECT couple_id FROM trips WHERE id=?", (self.trip_id,)).fetchone()[0]
            db.executemany(
                "INSERT INTO trip_assistant_messages(id,trip_id,couple_id,role,content,intent_type,cards_json,created_at) VALUES (?,?,?,'assistant',?,'general','[]',?)",
                [
                    (f"bounded-{index:03d}", self.trip_id, couple_id, f"第 {index} 条", f"2026-10-27T00:{index // 60:02d}:{index % 60:02d}+00:00")
                    for index in range(130)
                ],
            )
        self.assertEqual(len(self.store.list_trip_assistant(self.trip_id, DEMO_USER, limit=999)["items"]), 120)

        outsider_a = self.store.register("outsider_a", "outsider-pass-123", "外甲")
        outsider_b = self.store.register("outsider_b", "outsider-pass-123", "外乙")
        invite = self.store.create_user_invite(outsider_a["id"])
        self.store.accept_user_invite(outsider_b["id"], invite["code"], "2026-01-01")
        with self.assertRaises(KeyError):
            self.store.list_trip_assistant(self.trip_id, outsider_a["id"])

    def test_account_export_contains_shared_assistant_history(self) -> None:
        asked = self.store.ask_trip_assistant(self.trip_id, "比较五套方案", DEMO_USER, "export-chat-000001")
        archive_bytes, _ = self.store.account_export(DEMO_PARTNER)
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            exported = json.loads(archive.read("data.json"))
        exported_ids = {row["id"] for row in exported["trip_assistant_messages"]}
        self.assertIn(asked["assistant"]["id"], exported_ids)
        self.assertIn("trip_assistant_action_drafts", exported)

    def test_hotel_cards_reference_saved_quotes_and_scope(self) -> None:
        result = self.store.ask_trip_assistant(self.trip_id, "找每晚2000元以内酒店", DEMO_USER, "ask-hotel-0000001")
        cards = result["assistant"]["cards"]
        self.assertTrue(cards)
        self.assertTrue(all(card["type"] == "hotel_quote" and card["quote_id"] for card in cards))
        with self.store.connection() as db:
            ids = {row[0] for row in db.execute("SELECT id FROM quote_snapshots WHERE trip_id=?", (self.trip_id,))}
        self.assertTrue({card["quote_id"] for card in cards}.issubset(ids))

    def test_itinerary_change_requires_confirm_and_rejects_stale_version(self) -> None:
        trip = self.store.get_trip(self.trip_id, DEMO_USER)
        item = trip["variants"][0]["days"][1]["items"][0]
        result = self.store.ask_trip_assistant(
            self.trip_id,
            f"把第二天的{item['title']}改到15:30",
            DEMO_USER,
            "ask-change-000001",
        )
        card = result["assistant"]["cards"][0]
        self.assertEqual(card["type"], "itinerary_change")
        before = self.store.get_trip(self.trip_id, DEMO_USER)["current_revision"]
        confirmed = self.store.confirm_trip_assistant_action(self.trip_id, card["draft_id"], "confirm-change-0001", DEMO_USER)
        self.assertGreater(confirmed["trip"]["current_revision"], before)
        confirmed_item = next(
            candidate
            for variant in confirmed["trip"]["variants"]
            for day in variant["days"]
            for candidate in day["items"]
            if candidate["id"] == item["id"]
        )
        self.assertEqual(confirmed_item["start_time"], "15:30")
        shared_history = self.store.list_trip_assistant(self.trip_id, DEMO_PARTNER)
        saved_card = next(
            item_card
            for message in shared_history["items"]
            for item_card in message["cards"]
            if item_card.get("draft_id") == card["draft_id"]
        )
        self.assertEqual(saved_card["status"], "confirmed")
        repeated = self.store.confirm_trip_assistant_action(self.trip_id, card["draft_id"], "confirm-change-0001", DEMO_USER)
        self.assertEqual(repeated["revision"], confirmed["revision"])

        fresh = self.store.ask_trip_assistant(self.trip_id, f"把第二天的{item['title']}改到16:00", DEMO_USER, "ask-stale-0000001")
        stale_card = fresh["assistant"]["cards"][0]
        self.store.revise_item(item["id"], {"start_time": "16:30"}, user_id=DEMO_PARTNER)
        with self.assertRaises(ConflictError):
            self.store.confirm_trip_assistant_action(self.trip_id, stale_card["draft_id"], "confirm-stale-001", DEMO_USER)

    def test_itinerary_change_requires_day_when_titles_repeat(self) -> None:
        trip = self.store.get_trip(self.trip_id, DEMO_USER)
        item = trip["variants"][0]["days"][0]["items"][0]
        result = self.store.ask_trip_assistant(
            self.trip_id, f"把{item['title']}改到16:10", DEMO_USER, "ask-ambiguous-0001",
        )
        card = result["assistant"]["cards"][0]
        self.assertEqual(card["type"], "clarification")
        self.assertIn("补充第几天", card["message"])

    def test_restaurant_cards_are_real_saved_map_rows_for_requested_meal(self) -> None:
        class FakeMap:
            name = "amap"
            is_mock = False

            def search_food(self, keywords, city, limit, area):
                return [{
                    "amap_poi_id": "poi-real-dinner", "name": "测试真实餐厅", "address": f"{area} 1号",
                    "district": "测试区", "category": "本地菜", "rating": 4.7,
                    "average_cost_cents": 8800, "distance_meters": 760, "source": "amap",
                    "latitude": 30.2, "longitude": 120.1, "navigation_url": "https://uri.amap.com/marker?position=120.1,30.2",
                }]

        with patch("love_moments.phase_three.map_provider_from_env", return_value=FakeMap()):
            result = self.store.ask_trip_assistant(
                self.trip_id, "找第二天晚餐，4.5分以上，2公里内", DEMO_USER, "ask-food-00000001",
            )
        cards = result["assistant"]["cards"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["name"], "测试真实餐厅")
        self.assertEqual((cards[0]["day_number"], cards[0]["meal_type"]), (2, "dinner"))
        with self.store.connection() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM trip_food_recommendations WHERE id=? AND source='amap'", (cards[0]["recommendation_id"],)).fetchone())

    def test_day_replan_is_previewed_then_atomically_confirmed_and_idempotent(self) -> None:
        before = self.store.get_trip(self.trip_id, DEMO_USER)
        day = next(variant for variant in before["variants"] if variant["style"] == before["active_variant"])["days"][1]
        before_times = {item["id"]: item["start_time"] for item in day["items"] if not item["locked"]}
        result = self.store.ask_trip_assistant(
            self.trip_id, "第二天延误 60 分钟，重新安排后面的内容", DEMO_USER, "ask-day-replan-0001",
        )
        card = result["assistant"]["cards"][0]
        self.assertEqual(card["type"], "day_replan")
        self.assertGreater(len(card["changes"]), 1)
        unchanged = self.store.get_trip(self.trip_id, DEMO_USER)
        unchanged_day = next(variant for variant in unchanged["variants"] if variant["style"] == unchanged["active_variant"])["days"][1]
        self.assertEqual({item["id"]: item["start_time"] for item in unchanged_day["items"] if item["id"] in before_times}, before_times)

        confirmed = self.store.confirm_trip_assistant_action(
            self.trip_id, card["draft_id"], "confirm-day-replan-001", DEMO_USER,
        )
        self.assertGreater(confirmed["revision"], before["current_revision"])
        repeated = self.store.confirm_trip_assistant_action(
            self.trip_id, card["draft_id"], "confirm-day-replan-001", DEMO_USER,
        )
        self.assertEqual(repeated["revision"], confirmed["revision"])
        with self.store.connection() as db:
            revision_rows = db.execute(
                "SELECT COUNT(*) FROM plan_revisions WHERE trip_id=? AND revision=?",
                (self.trip_id, confirmed["revision"]),
            ).fetchone()[0]
            saved_key = db.execute(
                "SELECT idempotency_key FROM trip_assistant_action_drafts WHERE id=?", (card["draft_id"],),
            ).fetchone()[0]
        self.assertEqual(revision_rows, 1)
        self.assertEqual(saved_key, "confirm-day-replan-001")

    def test_day_replan_rejects_if_any_involved_item_changed(self) -> None:
        result = self.store.ask_trip_assistant(
            self.trip_id, "第二天延误 45 分钟，重新安排后面的内容", DEMO_USER, "ask-day-stale-00001",
        )
        card = result["assistant"]["cards"][0]
        changed = card["changes"][0]
        self.store.revise_item(changed["item_id"], {"start_time": "21:10"}, user_id=DEMO_PARTNER)
        with self.assertRaises(ConflictError):
            self.store.confirm_trip_assistant_action(
                self.trip_id, card["draft_id"], "confirm-day-stale-01", DEMO_USER,
            )

    def test_day_replan_rejects_past_day_and_requires_live_context_for_today(self) -> None:
        today = date.today()
        with self.store.connection() as db:
            active = db.execute("SELECT active_variant FROM trips WHERE id=?", (self.trip_id,)).fetchone()[0]
            variant_id = db.execute(
                "SELECT id FROM plan_variants WHERE trip_id=? AND style=?", (self.trip_id, active),
            ).fetchone()[0]
            days = db.execute(
                "SELECT id,day_number FROM trip_days WHERE variant_id=? ORDER BY day_number", (variant_id,),
            ).fetchall()
            db.execute("UPDATE trip_days SET date=? WHERE id=?", ((today - timedelta(days=1)).isoformat(), days[0]["id"]))
            db.execute("UPDATE trip_days SET date=? WHERE id=?", (today.isoformat(), days[1]["id"]))
        past = self.store.ask_trip_assistant(
            self.trip_id, "第一天延误 30 分钟，重新安排后面的内容", DEMO_USER, "past-replan-000001",
        )
        self.assertIn("已经过去", past["assistant"]["cards"][0]["message"])
        current = self.store.ask_trip_assistant(
            self.trip_id, "第二天延误 30 分钟，重新安排后面的内容", DEMO_USER, "today-replan-00001",
        )
        self.assertIn("更新当前位置", current["assistant"]["cards"][0]["message"])

    def test_confirm_and_cancel_are_atomic(self) -> None:
        trip = self.store.get_trip(self.trip_id, DEMO_USER)
        item = trip["variants"][0]["days"][1]["items"][0]
        reply = self.store.ask_trip_assistant(
            self.trip_id, f"把第二天的{item['title']}改到15:45", DEMO_USER, "atomic-draft-00001",
        )
        draft_id = reply["assistant"]["cards"][0]["draft_id"]
        barrier = threading.Barrier(2)
        outcomes = []

        def confirm() -> None:
            barrier.wait()
            try:
                self.store.confirm_trip_assistant_action(
                    self.trip_id, draft_id, "atomic-confirm-0001", DEMO_USER,
                )
                outcomes.append("confirmed")
            except (ConflictError, KeyError):
                outcomes.append("confirm-rejected")

        def cancel() -> None:
            barrier.wait()
            try:
                self.store.cancel_trip_assistant_action(self.trip_id, draft_id, DEMO_PARTNER)
                outcomes.append("cancelled")
            except (ConflictError, KeyError):
                outcomes.append("cancel-rejected")

        threads = [threading.Thread(target=confirm), threading.Thread(target=cancel)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(outcomes), 2)
        with self.store.connection() as db:
            status = db.execute(
                "SELECT status FROM trip_assistant_action_drafts WHERE id=?", (draft_id,),
            ).fetchone()[0]
            saved_time = db.execute("SELECT start_time FROM itinerary_items WHERE id=?", (item["id"],)).fetchone()[0]
        if status == "cancelled":
            self.assertEqual(saved_time, item["start_time"])
        else:
            self.assertEqual((status, saved_time), ("confirmed", "15:45"))

    def test_next_stop_conversation_uses_weather_provider(self) -> None:
        class Rain:
            def __init__(self):
                self.calls = 0

            def current(self, latitude, longitude):
                self.calls += 1
                return {"weather_code": 61, "label": "小雨"}

        today = date.today()
        with self.store.connection() as db:
            active = db.execute("SELECT active_variant FROM trips WHERE id=?", (self.trip_id,)).fetchone()[0]
            variant_id = db.execute(
                "SELECT id FROM plan_variants WHERE trip_id=? AND style=?", (self.trip_id, active),
            ).fetchone()[0]
            db.execute("UPDATE trip_days SET date=? WHERE variant_id=? AND day_number=1", (today.isoformat(), variant_id))
        observed = datetime.now(timezone.utc)
        live = {
            "latitude": 30.27, "longitude": 120.15, "accuracy_meters": 30,
            "observed_at": observed.isoformat(), "local_time": observed.astimezone().isoformat(),
        }
        weather = Rain()
        self.store.ask_trip_assistant(
            self.trip_id, "接下来去哪里", DEMO_USER, "next-weather-00001", live, weather,
        )
        self.assertEqual(weather.calls, 1)


if __name__ == "__main__":
    unittest.main()
