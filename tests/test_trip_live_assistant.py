from __future__ import annotations

import io
import json
import unittest
from datetime import datetime
from unittest.mock import patch

from love_moments.trip_live_assistant import build_day_replan_preview, build_live_recommendations
from love_moments.trip_live_context import validate_live_context, wgs84_to_gcj02
from love_moments.weather import OpenMeteoWeather


def live_context(local_time: str = "2026-10-27T11:00:00+08:00"):
    observed = datetime.fromisoformat(local_time).astimezone().isoformat()
    return validate_live_context({
        "latitude": 31.2304,
        "longitude": 121.4737,
        "accuracy_meters": 30,
        "observed_at": observed,
        "local_time": local_time,
    }, now=datetime.fromisoformat(observed))


def sample_trip() -> dict:
    def item(item_id, title, start, latitude, longitude, *, locked=0, version=1, category="", opening=""):
        return {
            "id": item_id, "title": title, "type": "place", "start_time": start,
            "duration_minutes": 60, "position": int(start[:2]), "locked": locked, "version": version,
            "place": {
                "provider": "amap", "provider_place_id": f"poi-{item_id}", "name": title,
                "latitude": latitude, "longitude": longitude, "category": category,
            },
            "details": {"opening_hours": opening},
        }

    return {
        "id": "trip-live", "start_date": "2026-10-27", "end_date": "2026-10-29",
        "active_variant": "classic", "version": 7,
        "variants": [
            {"style": "classic", "days": [
                {"id": "day-1", "day_number": 1, "date": "2026-10-27", "items": [
                    item("past", "早餐", "09:00", 31.229, 121.472),
                    item("outdoor", "湖滨公园", "12:00", 31.232, 121.475, category="公园广场", opening="08:00-21:00"),
                    item("indoor", "城市博物馆", "15:00", 31.238, 121.482, category="博物馆", opening="09:00-17:00"),
                    item("locked", "已预约演出", "17:30", 31.24, 121.49, locked=1, category="剧院"),
                    item("dinner", "晚餐", "22:30", 31.235, 121.478, category="餐饮"),
                ]},
                {"id": "day-2", "day_number": 2, "date": "2026-10-28", "items": [
                    item("tomorrow", "第二天景点", "10:00", 31.30, 121.50),
                ]},
            ]},
            {"style": "slow", "days": [
                {"id": "other-variant", "day_number": 1, "date": "2026-10-27", "items": [
                    item("wrong-variant", "错误方案项目", "12:00", 31.22, 121.47),
                ]},
            ]},
        ],
    }


class TripLiveAssistantTests(unittest.TestCase):
    def test_next_stop_uses_active_current_day_confirmed_places_and_rain_prefers_indoor(self) -> None:
        result = build_live_recommendations(sample_trip(), live_context(), {"weather_code": 61, "label": "小雨"})
        self.assertEqual(result["current_day_number"], 1)
        ids = [row["item_id"] for row in result["candidates"]]
        self.assertEqual(ids[0], "indoor")
        self.assertNotIn("past", ids)
        self.assertNotIn("tomorrow", ids)
        self.assertNotIn("wrong-variant", ids)
        self.assertTrue(all(row["distance_meters"] > 0 for row in result["candidates"]))
        self.assertEqual(result["candidates"][0]["open_status"], "open")

    def test_missing_weather_does_not_block_live_recommendations(self) -> None:
        result = build_live_recommendations(sample_trip(), live_context(), None)
        self.assertFalse(result["weather_available"])
        self.assertGreaterEqual(len(result["candidates"]), 2)

    def test_amap_candidate_distance_converts_browser_wgs84_origin(self) -> None:
        trip = sample_trip()
        converted = wgs84_to_gcj02(31.2304, 121.4737)
        day = trip["variants"][0]["days"][0]
        day["items"] = [day["items"][1]]
        day["items"][0]["place"].update({"latitude": converted[0], "longitude": converted[1]})
        result = build_live_recommendations(trip, live_context(), None)
        self.assertLess(result["candidates"][0]["distance_meters"], 10)

    def test_day_replan_only_changes_future_unlocked_target_day_items(self) -> None:
        result = build_day_replan_preview(
            sample_trip(), 1, live_context().local_time, delay_minutes=60,
            weather={"weather_code": 61, "label": "小雨"},
        )
        changed_ids = {row["item_id"] for row in result["changes"]}
        self.assertNotIn("past", changed_ids)
        self.assertNotIn("locked", changed_ids)
        self.assertNotIn("tomorrow", changed_ids)
        self.assertIn("indoor", changed_ids)
        self.assertEqual(result["changes"][0]["item_id"], "indoor")
        self.assertIn("outdoor", result["weather_affected_item_ids"])
        self.assertTrue(result["warnings"])

    def test_large_delay_reports_next_day_overflow(self) -> None:
        result = build_day_replan_preview(sample_trip(), 1, live_context().local_time, delay_minutes=180, weather=None)
        self.assertTrue(any("跨到次日" in warning for warning in result["warnings"]))

    def test_replan_does_not_overlap_locked_reservation(self) -> None:
        trip = sample_trip()
        day = trip["variants"][0]["days"][0]
        outdoor = next(item for item in day["items"] if item["id"] == "outdoor")
        outdoor.update({"start_time": "16:45", "position": 16, "duration_minutes": 60})
        result = build_day_replan_preview(
            trip, 1, live_context().local_time, delay_minutes=45, weather=None,
        )
        self.assertNotIn("outdoor", {row["item_id"] for row in result["changes"]})
        self.assertIn("outdoor", {row["item_id"] for row in result["conflicts"]})
        self.assertTrue(any("预约" in warning or "冲突" in warning for warning in result["warnings"]))

    def test_open_meteo_forecast_parses_bounded_daily_rows(self) -> None:
        payload = {
            "timezone": "Asia/Shanghai",
            "daily": {
                "time": ["2026-10-27", "2026-10-28"],
                "weather_code": [61, 2],
                "precipitation_probability_max": [80, 10],
                "temperature_2m_max": [20.5, 22.0],
                "temperature_2m_min": [14.0, 15.5],
            },
        }
        response = io.BytesIO(json.dumps(payload).encode())
        response.__enter__ = lambda value: value
        response.__exit__ = lambda *args: None
        with patch("love_moments.weather.request.urlopen", return_value=response):
            result = OpenMeteoWeather(enabled=True).forecast(31.2, 121.4, "2026-10-27", "2026-10-28")
        self.assertEqual(result["items"][0]["label"], "小雨")
        self.assertEqual(result["items"][0]["precipitation_probability"], 80)
        self.assertEqual(result["source"], "Open-Meteo")


if __name__ == "__main__":
    unittest.main()
