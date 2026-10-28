from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from love_moments.store import DEMO_PARTNER, DEMO_USER, LoveMomentsStore


class DailyCoupleSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LoveMomentsStore(
            Path(self.temp.name) / "summary.db", seed_demo_data=True
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_summary_is_shared_idempotent_and_excludes_private_bodies(self) -> None:
        secret = "SECRET-PRIVATE-SUMMARY-MARKER"
        self.store.create_moment(
            {"title": "不可见标题", "body": secret, "visibility": "private"},
            DEMO_USER,
        )
        self.store.create_message({"content": secret}, DEMO_USER)
        first = self.store.generate_daily_couple_summary(
            DEMO_USER, local_date="2026-10-27"
        )
        second = self.store.generate_daily_couple_summary(
            DEMO_PARTNER, local_date="2026-10-27"
        )
        self.assertEqual(first["id"], second["id"])
        self.assertNotIn(secret, json.dumps(first, ensure_ascii=False))
        self.assertEqual(
            self.store.get_daily_couple_summary(
                DEMO_PARTNER, local_date="2026-10-27"
            )["id"],
            first["id"],
        )

    def test_due_runner_generates_once_after_local_summary_time(self) -> None:
        result = self.store.run_due_daily_summaries(
            now=datetime(2026, 10, 27, 13, 31, tzinfo=timezone.utc)
        )
        self.assertEqual(result["generated"], 1)
        again = self.store.run_due_daily_summaries(
            now=datetime(2026, 10, 27, 13, 40, tzinfo=timezone.utc)
        )
        self.assertEqual(again["generated"], 0)

    def test_disabled_summary_is_not_generated_by_runner(self) -> None:
        self.store.update_life_assistant_settings(
            {
                "timezone": "Asia/Shanghai",
                "daily_summary_enabled": False,
                "daily_summary_time": "21:30",
            },
            DEMO_USER,
        )
        result = self.store.run_due_daily_summaries(
            now=datetime(2026, 10, 27, 14, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(result["generated"], 0)
        self.assertIsNone(
            self.store.get_daily_couple_summary(
                DEMO_USER, local_date="2026-10-27"
            )
        )


if __name__ == "__main__":
    unittest.main()
