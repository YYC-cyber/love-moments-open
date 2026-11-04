from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from love_moments.providers import LocalTravelPlanner
from love_moments.store import DEMO_USER, LoveMomentsStore


class CapturingPlanner(LocalTravelPlanner):
    def __init__(self) -> None:
        self.contexts: list[dict] = []

    def generate_variants(self, destination: str, budget_cents: int, context: dict | None = None):
        self.contexts.append(context or {})
        return super().generate_variants(destination, budget_cents, context)


class FakeWeather:
    def forecast(self, latitude, longitude, start_date, end_date):
        return {
            "source": "test-weather",
            "timezone": "Asia/Shanghai",
            "items": [
                {"date": "2026-11-10", "label": "中雨", "precipitation_probability": 80, "temperature_max": 18, "temperature_min": 12},
                {"date": "2026-11-11", "label": "晴", "precipitation_probability": 5, "temperature_max": 21, "temperature_min": 11},
                {"date": "2026-11-12", "label": "阴", "precipitation_probability": 20, "temperature_max": 19, "temperature_min": 10},
            ],
        }


class TripWeatherAndEditingContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.planner = CapturingPlanner()
        self.store = LoveMomentsStore(
            Path(self.temp.name) / "weather-trip.db",
            planner=self.planner,
            seed_demo_data=True,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_weather_snapshot_reaches_trip_planner_and_is_saved_on_every_day(self) -> None:
        candidate = self.store.search_destination_places(
            {"query": "杭州西湖", "region_hint": "杭州市", "include_global": False}, DEMO_USER,
        )["items"][0]
        # Offline place search intentionally has no coordinates. Mirror the
        # coordinate-bearing AMap candidate used in production.
        with self.store.connection() as db:
            row = db.execute(
                "SELECT payload_json FROM place_search_candidates WHERE id=?",
                (candidate["candidate_id"],),
            ).fetchone()
            payload = json.loads(row["payload_json"])
            payload.update({"latitude": 30.259, "longitude": 120.137})
            db.execute(
                "UPDATE place_search_candidates SET payload_json=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), candidate["candidate_id"]),
            )
        trip = self.store.create_trip({
            "destination": "杭州西湖",
            "destination_candidate_id": candidate["candidate_id"],
            "start_date": "2026-11-10",
            "end_date": "2026-11-12",
            "budget": 3000,
        }, DEMO_USER, weather_provider=FakeWeather())

        weather = self.planner.contexts[-1]["weather_forecast"]
        self.assertTrue(weather["available"])
        self.assertEqual([row["date"] for row in weather["items"]], ["2026-11-10", "2026-11-11", "2026-11-12"])
        slow_days = next(variant for variant in trip["variants"] if variant["style"] == "slow")["days"]
        self.assertEqual(len(slow_days), 3)
        self.assertIn("中雨", slow_days[0]["items"][0]["details"]["weather_fit"])
        self.assertIn("晴", slow_days[1]["items"][0]["details"]["weather_fit"])
        food_days = next(variant for variant in trip["variants"] if variant["style"] == "food")["days"]
        self.assertTrue(all(len(day["items"]) >= 3 for day in food_days))
        self.assertTrue(all(item["details"]["weather_date"] == day["date"] for day in food_days for item in day["items"]))

    def test_weather_failure_does_not_block_trip_generation(self) -> None:
        class EmptyWeather:
            def forecast(self, *_args):
                return None

        context = self.store._trip_weather_context(
            EmptyWeather(), {"latitude": 30.2, "longitude": 120.1}, "2026-11-10", "2026-11-12",
        )
        self.assertFalse(context["available"])
        self.assertIn("没有返回", context["reason"])

    def test_frontend_exposes_an_editor_for_each_day_item(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('data-action="edit-trip-item"', script)
        self.assertIn("function openTripItemEditor", script)
        self.assertIn("data-day-number", script)
        self.assertNotIn("state.editingItemId = items[1]?.id", script)
        self.assertIn("weather.weather_fit", script)


if __name__ == "__main__":
    unittest.main()
