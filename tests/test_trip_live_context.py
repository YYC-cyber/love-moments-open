from __future__ import annotations

import unittest
from datetime import datetime, timezone

from love_moments.trip_live_context import (
    classify_opening_hours,
    haversine_meters,
    validate_live_context,
    wgs84_to_gcj02,
)


class TripLiveContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 27, 3, 30, tzinfo=timezone.utc)
        self.payload = {
            "latitude": 31.2304,
            "longitude": 121.4737,
            "accuracy_meters": 35,
            "observed_at": "2026-10-27T03:25:00+00:00",
            "local_time": "2026-10-27T11:25:00+08:00",
        }

    def test_valid_context_is_immutable_and_preserves_local_offset(self) -> None:
        value = validate_live_context(self.payload, now=self.now)
        self.assertEqual((value.latitude, value.longitude), (31.2304, 121.4737))
        self.assertEqual(value.accuracy_meters, 35)
        self.assertEqual(value.local_time.utcoffset().total_seconds(), 8 * 3600)
        with self.assertRaises(AttributeError):
            value.latitude = 0

    def test_rejects_stale_inaccurate_or_out_of_range_location(self) -> None:
        stale = {**self.payload, "observed_at": "2026-10-27T03:00:00+00:00"}
        inaccurate = {**self.payload, "accuracy_meters": 2001}
        invalid = {**self.payload, "latitude": 91}
        for payload, message in ((stale, "位置已过期"), (inaccurate, "定位精度"), (invalid, "经纬度")):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                validate_live_context(payload, now=self.now)

    def test_rejects_local_clock_that_does_not_match_observation_instant(self) -> None:
        skewed = {**self.payload, "local_time": "2026-10-27T12:25:00+08:00"}
        with self.assertRaisesRegex(ValueError, "本地时间.*定位时间"):
            validate_live_context(skewed, now=self.now)

    def test_china_coordinates_convert_but_foreign_coordinates_do_not(self) -> None:
        shanghai = wgs84_to_gcj02(31.2304, 121.4737)
        self.assertNotEqual(shanghai, (31.2304, 121.4737))
        self.assertAlmostEqual(shanghai[0], 31.2285, delta=0.01)
        self.assertAlmostEqual(shanghai[1], 121.4782, delta=0.01)
        paris = wgs84_to_gcj02(48.8566, 2.3522)
        self.assertEqual(paris, (48.8566, 2.3522))

    def test_distance_and_opening_hours_are_deterministic(self) -> None:
        distance = haversine_meters(31.2304, 121.4737, 31.2243, 121.4768)
        self.assertGreater(distance, 600)
        self.assertLess(distance, 900)
        local_time = datetime.fromisoformat("2026-10-27T11:30:00+08:00")
        self.assertEqual(classify_opening_hours("09:00-18:00", local_time), "open")
        self.assertEqual(classify_opening_hours("18:00-23:00", local_time), "closed")
        self.assertEqual(classify_opening_hours("营业时间请咨询商家", local_time), "unknown")

    def test_overnight_opening_range_is_supported(self) -> None:
        late = datetime.fromisoformat("2026-10-27T23:30:00+08:00")
        early = datetime.fromisoformat("2026-10-28T01:00:00+08:00")
        self.assertEqual(classify_opening_hours("18:00-02:00", late), "open")
        self.assertEqual(classify_opening_hours("18:00-02:00", early), "open")


if __name__ == "__main__":
    unittest.main()
