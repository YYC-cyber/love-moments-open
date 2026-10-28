from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from love_moments.quota import QuotaExceededError
from love_moments.providers import LocalDatePlanner
from love_moments.store import LoveMomentsStore


class GuardedComposer:
    name = "ai"

    def __init__(self) -> None:
        self.before_request = None

    def compose(self, surface, context, intent):
        if self.before_request:
            self.before_request("ui compose test")
        return ["budget_summary", "trip_variant_compare"]


class GuardedDatePlanner(LocalDatePlanner):
    def __init__(self) -> None:
        self.before_request = None

    def generate_variants(self, request_data):
        if self.before_request:
            self.before_request("date plan test")
        return super().generate_variants(request_data)


class ExternalQuotaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            "os.environ",
            {
                "AMAP_DAILY_REQUEST_LIMIT": "5",
                "GEOAPIFY_DAILY_REQUEST_LIMIT": "2",
                "SERPAPI_MONTHLY_REQUEST_LIMIT": "2",
                "JUHE_TRAIN_TOTAL_REQUEST_LIMIT": "1",
                "ALIYUN_OCR_MONTHLY_REQUEST_LIMIT": "1",
                "LLM_DAILY_REQUEST_LIMIT": "1",
            },
            clear=False,
        )
        self.env.start()
        self.store = LoveMomentsStore(Path(self.temp.name) / "quota.db", seed_demo_data=False)

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def test_usage_persists_and_hard_limit_stops_before_increment(self) -> None:
        for _ in range(5):
            self.store.reserve_external_api("amap", operation="test")
        with self.assertRaises(QuotaExceededError) as raised:
            self.store.reserve_external_api("amap", operation="blocked")
        status = self.store.external_api_quota("amap")
        self.assertEqual((status["used"], status["remaining"]), (5, 0))
        self.assertTrue(status["exhausted"])
        self.assertIn("不会继续消耗次数", str(raised.exception))

        reopened = LoveMomentsStore(Path(self.temp.name) / "quota.db", seed_demo_data=False)
        self.assertEqual(reopened.external_api_quota("amap")["used"], 5)

    def test_concurrent_reservations_cannot_race_past_limit(self) -> None:
        outcomes: list[str] = []
        lock = threading.Lock()

        def reserve() -> None:
            try:
                self.store.reserve_external_api("amap", operation="concurrent")
                value = "accepted"
            except QuotaExceededError:
                value = "blocked"
            with lock:
                outcomes.append(value)

        threads = [threading.Thread(target=reserve) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count("accepted"), 5)
        self.assertEqual(outcomes.count("blocked"), 15)
        self.assertEqual(self.store.external_api_quota("amap")["used"], 5)

    def test_geoapify_daily_limit_stops_before_free_allowance(self) -> None:
        self.store.reserve_external_api("geoapify", operation="place search")
        self.store.reserve_external_api("geoapify", operation="route")
        with self.assertRaises(QuotaExceededError):
            self.store.reserve_external_api("geoapify", operation="blocked")
        status = self.store.external_api_quota("geoapify")
        self.assertEqual((status["used"], status["remaining"]), (2, 0))

    def test_zero_limit_disables_real_provider_and_lifetime_never_resets(self) -> None:
        with patch.dict("os.environ", {"ALIYUN_OCR_MONTHLY_REQUEST_LIMIT": "0"}, clear=False):
            with self.assertRaises(QuotaExceededError):
                self.store.reserve_external_api("aliyun_ocr")
        self.store.reserve_external_api("juhe_train")
        status = self.store.external_api_quota("juhe_train")
        self.assertEqual(status["period"], "lifetime")
        self.assertIsNone(status["resets_at"])
        with self.assertRaises(QuotaExceededError):
            self.store.reserve_external_api("juhe_train")

    def test_unlimited_model_counters_keep_counting_without_blocking(self) -> None:
        with patch.dict(
            "os.environ",
            {"LLM_DAILY_REQUEST_LIMIT": "unlimited", "RECIPE_LLM_DAILY_REQUEST_LIMIT": "unlimited"},
            clear=False,
        ):
            for provider in ("llm", "recipe_llm"):
                for _ in range(4):
                    self.store.reserve_external_api(provider, operation="unlimited test")
                status = self.store.external_api_quota(provider)
                self.assertTrue(status["unlimited"])
                self.assertIsNone(status["limit"])
                self.assertIsNone(status["remaining"])
                self.assertFalse(status["warning"])
                self.assertFalse(status["exhausted"])
                self.assertEqual(status["used"], 4)

    def test_ui_composer_reserves_quota_without_database_lock(self) -> None:
        store = LoveMomentsStore(Path(self.temp.name) / "ui-quota.db", ui_composer=GuardedComposer(), seed_demo_data=True)
        result = store.compose_ui("trip", "先看预算")
        self.assertEqual(result["manifest"]["generatedBy"], "ai")
        self.assertIsNone(result["errorCode"])
        self.assertEqual(store.external_api_quota("llm")["used"], 1)

    def test_date_planner_reserves_quota_without_database_lock(self) -> None:
        store = LoveMomentsStore(Path(self.temp.name) / "date-quota.db", date_planner=GuardedDatePlanner(), seed_demo_data=True)
        result = store.create_date_plan(
            {
                "title": "异地见面",
                "city": "杭州",
                "plan_date": "2026-11-01",
                "start_time": "14:00",
                "end_time": "21:00",
                "budget": 500,
            }
        )
        self.assertEqual(len(result["variants"]), 5)
        self.assertEqual(store.external_api_quota("llm")["used"], 1)


if __name__ == "__main__":
    unittest.main()
