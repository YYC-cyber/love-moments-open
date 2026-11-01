from __future__ import annotations

import unittest
from datetime import date, timedelta

from love_moments.weather import OpenMeteoWeather


def sample_trip(start: date, end: date, *, with_place: bool = True) -> dict:
    return {
        "id": "trip-weather",
        "destination": "杭州西湖",
        "arrival_city": "杭州",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "destination_place": ({
            "name": "杭州西湖风景名胜区",
            "formatted_address": "浙江省杭州市西湖区",
            "latitude": 30.245,
            "longitude": 120.145,
        } if with_place else None),
    }


class FakeTripWeather(OpenMeteoWeather):
    def __init__(self) -> None:
        super().__init__(enabled=True)
        self.calls: list[tuple] = []

    def forecast(self, latitude, longitude, start_date, end_date, timezone_name="auto", force_refresh=False):
        self.calls.append((latitude, longitude, start_date, end_date, timezone_name, force_refresh))
        return {
            "items": [{
                "date": start_date, "weather_code": 61, "label": "小雨",
                "precipitation_probability": 75, "temperature_max": 26.0, "temperature_min": 18.0,
                "wind_speed_max": 12.0, "uv_index_max": 3.0, "sunrise": "06:02", "sunset": "17:20",
            }],
            "source": "Open-Meteo", "provider": "Open-Meteo", "source_type": "real_api",
            "evidence_at": "2026-11-01T06:00:00+00:00", "timezone": "Asia/Shanghai",
        }


class TripWeatherTests(unittest.TestCase):
    def test_available_forecast_uses_confirmed_coordinates_and_gives_rain_advice(self) -> None:
        today = date(2026, 11, 1)
        provider = FakeTripWeather()
        result = provider.trip_forecast(sample_trip(today + timedelta(days=2), today + timedelta(days=4)), today=today)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["source_type"], "real_api")
        self.assertEqual(provider.calls[0][:4], (30.245, 120.145, "2026-11-03", "2026-11-05"))
        self.assertTrue(provider.calls[0][-1] is False)
        self.assertTrue(any("带伞" in item for item in result["advice"]))

    def test_force_refresh_is_forwarded(self) -> None:
        today = date(2026, 11, 1)
        provider = FakeTripWeather()
        provider.trip_forecast(sample_trip(today, today), force_refresh=True, today=today)
        self.assertTrue(provider.calls[0][-1])

    def test_rolling_seven_and_ten_day_ranges_ignore_distant_trip_dates(self) -> None:
        today = date(2026, 11, 1)
        provider = FakeTripWeather()
        distant = sample_trip(today + timedelta(days=100), today + timedelta(days=102))
        result = provider.trip_forecast(distant, range_days=10, today=today)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["range_mode"], "rolling")
        self.assertEqual(result["range_days"], 10)
        self.assertEqual(provider.calls[0][2:4], ("2026-11-01", "2026-11-10"))
        with self.assertRaisesRegex(ValueError, "只支持"):
            provider.trip_forecast(distant, range_days=8, today=today)

    def test_rain_prefers_verified_indoor_items_from_current_itinerary(self) -> None:
        today = date(2026, 11, 1)
        trip = sample_trip(today, today)
        trip.update({
            "active_variant": "slow",
            "variants": [{"style": "slow", "days": [{"date": today.isoformat(), "items": [
                {"id": "museum", "title": "杭州博物馆", "type": "sight", "details": {"environment": "室内"}, "place": {"provider_place_id": "amap-1", "formatted_address": "杭州"}},
                {"id": "park", "title": "西湖边散步", "type": "sight", "details": {"environment": "户外"}, "place": {"provider_place_id": "amap-2", "formatted_address": "杭州"}},
            ]}]}],
        })
        result = FakeTripWeather().trip_forecast(trip, today=today)
        places = result["items"][0]["suggested_places"]
        self.assertEqual(places[0]["item_id"], "museum")
        self.assertTrue(places[0]["weather_match"])
        self.assertEqual(places[0]["source_type"], "confirmed_itinerary")

    def test_long_range_trip_is_not_fabricated(self) -> None:
        today = date(2026, 11, 1)
        provider = FakeTripWeather()
        start = today + timedelta(days=30)
        result = provider.trip_forecast(sample_trip(start, start + timedelta(days=2)), today=today)
        self.assertEqual(result["status"], "not_yet_available")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["check_again_on"], (start - timedelta(days=15)).isoformat())
        self.assertEqual(provider.calls, [])

    def test_missing_confirmed_place_and_disabled_provider_are_explicit(self) -> None:
        today = date(2026, 11, 1)
        provider = FakeTripWeather()
        missing = provider.trip_forecast(sample_trip(today, today, with_place=False), today=today)
        self.assertEqual(missing["status"], "location_required")
        disabled = OpenMeteoWeather(enabled=False).trip_forecast(sample_trip(today, today), today=today)
        self.assertEqual(disabled["status"], "disabled")


if __name__ == "__main__":
    unittest.main()
