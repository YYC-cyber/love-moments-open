from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path

from love_moments import LoveMomentsStore
from love_moments.intelligent_ui import ConflictError
from love_moments.store import DEMO_PARTNER, DEMO_TRIP, DEMO_USER
from love_moments.travel_assistant import RuleBasedTravelAssistant


class TripMemoryDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LoveMomentsStore(
            Path(self.temp.name) / "recap.db",
            travel_assistant=RuleBasedTravelAssistant(),
            seed_demo_data=True,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_shared_recap_draft_does_not_read_private_moments_or_create_timeline_item(self) -> None:
        self.store.create_moment({
            "title": "PRIVATE_SENTINEL", "body": "PRIVATE_BODY_SENTINEL",
            "visibility": "private", "type": "text",
        }, DEMO_USER)
        before = len(self.store.list_moments(user_id=DEMO_USER))
        draft = self.store.create_trip_recap_draft(DEMO_TRIP, DEMO_USER)
        self.assertEqual(draft["status"], "draft")
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(draft, ensure_ascii=False))
        self.assertNotIn("PRIVATE_BODY_SENTINEL", json.dumps(draft, ensure_ascii=False))
        self.assertEqual(len(self.store.list_moments(user_id=DEMO_USER)), before)
        partner = self.store.list_trip_recap_drafts(DEMO_TRIP, DEMO_PARTNER)
        self.assertEqual(partner["items"][0]["id"], draft["id"])

    def test_confirm_is_idempotent_and_creates_one_shared_trip_moment(self) -> None:
        draft = self.store.create_trip_recap_draft(DEMO_TRIP, DEMO_USER)
        first = self.store.confirm_trip_recap_draft(
            DEMO_TRIP, draft["id"], "recap-confirm-0001", DEMO_USER,
        )
        second = self.store.confirm_trip_recap_draft(
            DEMO_TRIP, draft["id"], "recap-confirm-0001", DEMO_PARTNER,
        )
        self.assertEqual(first["moment"]["id"], second["moment"]["id"])
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT * FROM moments WHERE id=? AND type='trip' AND visibility='shared'",
                (first["moment"]["id"],),
            ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_cancelled_draft_cannot_be_confirmed(self) -> None:
        draft = self.store.create_trip_recap_draft(DEMO_TRIP, DEMO_USER)
        cancelled = self.store.cancel_trip_recap_draft(DEMO_TRIP, draft["id"], DEMO_PARTNER)
        self.assertEqual(cancelled["status"], "cancelled")
        with self.assertRaises(ConflictError):
            self.store.confirm_trip_recap_draft(
                DEMO_TRIP, draft["id"], "recap-confirm-0002", DEMO_USER,
            )

    def test_recap_confirm_and_cancel_are_atomic(self) -> None:
        draft = self.store.create_trip_recap_draft(DEMO_TRIP, DEMO_USER)
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def confirm() -> None:
            barrier.wait()
            try:
                self.store.confirm_trip_recap_draft(
                    DEMO_TRIP, draft["id"], "recap-atomic-confirm-001", DEMO_USER,
                )
                outcomes.append("confirmed")
            except (ConflictError, KeyError):
                outcomes.append("confirm-rejected")

        def cancel() -> None:
            barrier.wait()
            try:
                self.store.cancel_trip_recap_draft(DEMO_TRIP, draft["id"], DEMO_PARTNER)
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
            row = db.execute(
                "SELECT status,moment_id FROM trip_memory_drafts WHERE id=?", (draft["id"],),
            ).fetchone()
            created = db.execute(
                "SELECT COUNT(*) FROM moments WHERE id=?", (row["moment_id"],),
            ).fetchone()[0] if row["moment_id"] else 0
        if row["status"] == "cancelled":
            self.assertEqual(created, 0)
        else:
            self.assertEqual((row["status"], created), ("confirmed", 1))

    def test_unfinished_trip_is_clearly_marked_as_early_draft(self) -> None:
        tomorrow = date.today() + timedelta(days=1)
        trip = self.store.create_trip({
            "destination": "苏州", "start_date": tomorrow.isoformat(),
            "end_date": (tomorrow + timedelta(days=1)).isoformat(),
            "budget_cents": 200000,
        }, DEMO_USER)
        draft = self.store.create_trip_recap_draft(trip["id"], DEMO_USER)
        self.assertTrue(draft["is_early"])
        self.assertIn("提前草稿", draft["notice"])
        with self.assertRaisesRegex(ConflictError, "旅行结束后"):
            self.store.confirm_trip_recap_draft(
                trip["id"], draft["id"], "early-recap-confirm-001", DEMO_USER,
            )

    def test_account_export_contains_recap_drafts(self) -> None:
        draft = self.store.create_trip_recap_draft(DEMO_TRIP, DEMO_USER)
        content, _ = self.store.account_export(DEMO_USER)
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            data = json.loads(archive.read("data.json"))
        self.assertIn(draft["id"], {row["id"] for row in data["trip_memory_drafts"]})

    def test_assistant_card_reflects_current_recap_draft_state(self) -> None:
        reply = self.store.ask_trip_assistant(
            DEMO_TRIP, "旅行结束了，生成共同回忆", DEMO_USER, "recap-message-0001",
        )
        draft = reply["assistant"]["cards"][0]
        updated = self.store.update_trip_recap_draft(DEMO_TRIP, draft["id"], {
            "title": "我们修改后的回忆", "body": draft["body"],
            "expected_version": draft["version"],
        }, DEMO_PARTNER)
        listed = self.store.list_trip_assistant(DEMO_TRIP, DEMO_USER)
        card = next(
            card for message in listed["items"] for card in message["cards"]
            if card.get("type") == "trip_recap" and card.get("id") == draft["id"]
        )
        self.assertEqual(card["title"], "我们修改后的回忆")
        self.assertEqual(card["version"], updated["version"])
        self.store.cancel_trip_recap_draft(DEMO_TRIP, draft["id"], DEMO_USER)
        listed = self.store.list_trip_assistant(DEMO_TRIP, DEMO_PARTNER)
        card = next(card for message in listed["items"] for card in message["cards"] if card.get("id") == draft["id"])
        self.assertEqual(card["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
