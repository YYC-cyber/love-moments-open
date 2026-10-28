from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from love_moments.intelligent_ui import ConflictError
from love_moments.store import DEMO_PARTNER, DEMO_USER, LoveMomentsStore


class FutureLetterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LoveMomentsStore(Path(self.temp.name) / "letters.db", seed_demo_data=True)

    def _create(self, key: str = "future-letter-create", secret: str = "只在未来打开的正文") -> dict:
        return self.store.create_future_letter({"title": "写给未来", "body": secret}, DEMO_USER, key)

    def _seal(self, letter: dict, *, seconds: int = 120) -> dict:
        deliver_at = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()
        return self.store.seal_future_letter(letter["id"], deliver_at, letter["version"], DEMO_USER)

    @staticmethod
    def _export_json(store: LoveMomentsStore, user_id: str) -> dict:
        archive, _ = store.account_export(user_id)
        with zipfile.ZipFile(io.BytesIO(archive)) as value:
            return json.loads(value.read("data.json"))

    def test_recipient_has_zero_side_channel_before_delivery(self) -> None:
        baseline_events = self.store.list_realtime_events(0, 200, DEMO_PARTNER)
        baseline_notifications = self.store.list_in_app_notifications(DEMO_PARTNER)
        baseline_search = self.store.search_memories("未来暗号", None, None, "all", None, 20, DEMO_PARTNER)
        letter = self._create(secret="未来暗号只有作者知道")
        self.assertEqual(self.store.list_realtime_events(0, 200, DEMO_PARTNER), baseline_events)
        updated = self.store.update_future_letter(letter["id"], {
            "title": "新的标题", "body": "未来暗号只有作者知道", "expected_version": letter["version"],
        }, DEMO_USER)
        self.assertEqual(self.store.list_realtime_events(0, 200, DEMO_PARTNER), baseline_events)
        sealed = self._seal(updated)
        self.assertEqual(self.store.list_realtime_events(0, 200, DEMO_PARTNER), baseline_events)
        restored = self.store.cancel_future_letter(sealed["id"], sealed["version"], DEMO_USER)
        self.assertEqual(restored["status"], "draft")
        self.assertEqual(self.store.list_realtime_events(0, 200, DEMO_PARTNER), baseline_events)
        partner = self.store.list_future_letters(DEMO_PARTNER, scope="delivered")
        self.assertEqual((partner["items"], partner["total"]), ([], 0))
        with self.assertRaises(KeyError):
            self.store.get_future_letter(letter["id"], DEMO_PARTNER)
        self.assertEqual(self.store.search_memories("未来暗号", None, None, "all", None, 20, DEMO_PARTNER), baseline_search)
        self.assertEqual(self.store.list_in_app_notifications(DEMO_PARTNER), baseline_notifications)
        after_events = self.store.list_realtime_events(0, 200, DEMO_PARTNER)
        self.assertEqual(after_events, baseline_events)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM push_outbox WHERE user_id=?", (DEMO_PARTNER,)).fetchone()[0], 0)
            audit = [dict(row) for row in db.execute("SELECT * FROM audit_events WHERE target_id=?", (sealed["id"],)).fetchall()]
            self.assertTrue(audit)
            self.assertTrue(all("未来暗号" not in json.dumps(item, ensure_ascii=False) for item in audit))
            with self.assertRaises(ValueError):
                self.store.compose_ui(
                    "timeline", "整理回忆", [f"future_letter:{sealed['id']}"], DEMO_USER,
                )
        exported = json.dumps(self._export_json(self.store, DEMO_PARTNER), ensure_ascii=False)
        self.assertNotIn("未来暗号", exported)

    def test_author_sealed_view_hides_body_and_cancel_restores_draft(self) -> None:
        draft = self._create(secret="封存后隐藏的内容")
        sealed = self._seal(draft)
        self.assertIsNone(sealed["body"])
        self.assertIsNone(self.store.get_future_letter(draft["id"], DEMO_USER)["body"])
        with self.assertRaises(ConflictError):
            self.store.update_future_letter(draft["id"], {"body": "不能编辑", "expected_version": sealed["version"]}, DEMO_USER)
        with self.assertRaises(ConflictError):
            self.store.delete_future_letter(draft["id"], sealed["version"], DEMO_USER)
        restored = self.store.cancel_future_letter(draft["id"], sealed["version"], DEMO_USER)
        self.assertEqual(restored["status"], "draft")
        self.assertEqual(restored["body"], "封存后隐藏的内容")
        self.assertIsNone(restored["deliver_at"])
        self.assertGreater(restored["version"], sealed["version"])

    def test_cancel_and_delivery_have_one_atomic_winner(self) -> None:
        sealed = self._seal(self._create(), seconds=1)
        due = datetime.now(timezone.utc) + timedelta(seconds=2)
        barrier = threading.Barrier(2)

        def cancel() -> str:
            barrier.wait()
            try:
                self.store.cancel_future_letter(sealed["id"], sealed["version"], DEMO_USER)
                return "draft"
            except ConflictError:
                return "conflict"

        def deliver() -> str:
            barrier.wait()
            result = self.store.deliver_due_future_letters(now=due, limit=50)
            return "delivered" if result["delivered"] else "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda action: action(), (cancel, deliver)))
        self.assertEqual(results.count("conflict"), 1)
        outcome = self.store.get_future_letter(sealed["id"], DEMO_USER)
        self.assertIn(outcome["status"], {"draft", "delivered"})
        with self.store.connection() as db:
            notifications = db.execute("SELECT * FROM in_app_notifications WHERE resource_id=?", (sealed["id"],)).fetchall()
            self.assertLessEqual(len(notifications), 1)
            events = db.execute("SELECT * FROM realtime_events WHERE resource_id=? AND event_type='future_letter.delivered'", (sealed["id"],)).fetchall()
            self.assertLessEqual(len(events), 1)

    def test_delivery_is_idempotent_and_rechecks_current_pair(self) -> None:
        sealed = self._seal(self._create(), seconds=1)
        due = datetime.now(timezone.utc) + timedelta(seconds=2)
        first = self.store.deliver_due_future_letters(now=due, limit=50)
        second = self.store.deliver_due_future_letters(now=due + timedelta(minutes=1), limit=50)
        self.assertEqual((first["delivered"], second["delivered"]), (1, 0))
        delivered = self.store.get_future_letter(sealed["id"], DEMO_PARTNER)
        self.assertEqual(delivered["status"], "delivered")
        self.assertEqual(delivered["body"], "只在未来打开的正文")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM in_app_notifications WHERE resource_id=?", (sealed["id"],)).fetchone()[0], 1)
            notice = db.execute("SELECT title,body FROM in_app_notifications WHERE resource_id=?", (sealed["id"],)).fetchone()
            self.assertNotIn("写给未来", notice["title"] + notice["body"])
            self.assertNotIn("只在未来", notice["title"] + notice["body"])

        other = self._seal(self._create("future-letter-invalid-pair"), seconds=1)
        with self.store.connection() as db:
            db.execute("UPDATE couples SET status='unlinked' WHERE id=(SELECT couple_id FROM future_letters WHERE id=?)", (other["id"],))
        blocked = self.store.deliver_due_future_letters(now=due + timedelta(minutes=2), limit=50)
        self.assertEqual(blocked["delivered"], 0)
        with self.store.connection() as db:
            row = db.execute("SELECT status,deliver_at FROM future_letters WHERE id=?", (other["id"],)).fetchone()
            self.assertEqual(row["status"], "draft")
            self.assertIsNone(row["deliver_at"])

    def test_timezone_requires_offset_and_exact_utc_boundary(self) -> None:
        draft = self._create()
        with self.assertRaises(ValueError):
            self.store.seal_future_letter(draft["id"], "2027-10-16T20:00:00", draft["version"], DEMO_USER)
        future = (datetime.now(timezone.utc) + timedelta(days=10)).replace(microsecond=0)
        local = future.astimezone(timezone(timedelta(hours=8))).isoformat()
        sealed = self.store.seal_future_letter(draft["id"], local, draft["version"], DEMO_USER)
        self.assertEqual(sealed["deliver_at"], future.isoformat())
        before = self.store.deliver_due_future_letters(now=future - timedelta(seconds=1), limit=50)
        self.assertEqual(before["delivered"], 0)
        exact = self.store.deliver_due_future_letters(now=future, limit=50)
        self.assertEqual(exact["delivered"], 1)

    def test_unlink_restores_private_draft_without_deleting_content(self) -> None:
        sealed = self._seal(self._create(secret="解绑后作者仍能看到"))
        self.store.unlink_couple("test123", "解除情侣", DEMO_USER)
        author = self.store.get_future_letter(sealed["id"], DEMO_USER)
        self.assertEqual((author["status"], author["body"]), ("draft", "解绑后作者仍能看到"))
        with self.assertRaises(KeyError):
            self.store.get_future_letter(sealed["id"], DEMO_PARTNER)
        with self.store.connection() as db:
            row = db.execute("SELECT status,deliver_at,sealed_at FROM future_letters WHERE id=?", (sealed["id"],)).fetchone()
            self.assertEqual(row["status"], "draft")
            self.assertIsNone(row["deliver_at"])
            self.assertIsNone(row["sealed_at"])
        deleted = self.store.delete_future_letter(sealed["id"], author["version"], DEMO_USER)
        self.assertTrue(deleted["deleted"])

    def test_delivered_letter_requires_the_original_active_pair(self) -> None:
        sealed = self._seal(self._create("future-delivered-unlink"), seconds=1)
        self.store.deliver_due_future_letters(
            now=datetime.now(timezone.utc) + timedelta(seconds=2), limit=50,
        )
        self.assertEqual(self.store.get_future_letter(sealed["id"], DEMO_USER)["status"], "delivered")
        self.store.unlink_couple("test123", "解除情侣", DEMO_USER)
        for user_id in (DEMO_USER, DEMO_PARTNER):
            with self.assertRaises(KeyError):
                self.store.get_future_letter(sealed["id"], user_id)
        self.assertEqual(self.store.list_future_letters(DEMO_USER, scope="mine")["items"], [])
        self.assertEqual(self.store.list_future_letters(DEMO_PARTNER, scope="delivered")["items"], [])

    def test_account_deletion_scrubs_unpublished_author_secret_and_restores_recipient_draft(self) -> None:
        author_secret = "作者注销必须清空的秘密"
        sealed = self._seal(self._create("future-author-delete", author_secret))
        self.store.delete_account("test123", "注销账号", DEMO_USER)
        with self.store.connection() as db:
            row = db.execute("SELECT title,body,status,deleted_at FROM future_letters WHERE id=?", (sealed["id"],)).fetchone()
            self.assertEqual((row["title"], row["body"], row["status"]), ("", "", "deleted"))
            self.assertIsNotNone(row["deleted_at"])
            dump = "\n".join(db.iterdump())
            self.assertNotIn(author_secret, dump)

        other = LoveMomentsStore(Path(self.temp.name) / "recipient-delete.db", seed_demo_data=True)
        draft = other.create_future_letter({"title": "保留给作者", "body": "收件人注销后仍是作者草稿"}, DEMO_USER, "future-recipient-delete")
        sealed = other.seal_future_letter(
            draft["id"], (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat(),
            draft["version"], DEMO_USER,
        )
        other.delete_account("test123", "注销账号", DEMO_PARTNER)
        restored = other.get_future_letter(sealed["id"], DEMO_USER)
        self.assertEqual((restored["status"], restored["body"]), ("draft", "收件人注销后仍是作者草稿"))
        self.assertIsNone(restored["deliver_at"])

    def test_author_and_partner_exports_differ_and_conversion_is_explicit(self) -> None:
        secret = "作者导出专用秘密"
        draft = self._create(secret=secret)
        author_export = json.dumps(self._export_json(self.store, DEMO_USER), ensure_ascii=False)
        partner_export = json.dumps(self._export_json(self.store, DEMO_PARTNER), ensure_ascii=False)
        self.assertIn(secret, author_export)
        self.assertIn('"unpublished": true', author_export)
        self.assertNotIn(secret, partner_export)
        sealed = self._seal(draft, seconds=1)
        due = datetime.now(timezone.utc) + timedelta(seconds=2)
        self.store.deliver_due_future_letters(now=due, limit=50)
        self.assertEqual(len([item for item in self.store.list_moments(user_id=DEMO_USER) if item["type"] == "text" and item["title"] == "写给未来"]), 0)
        converted = self.store.convert_future_letter_to_moment(
            sealed["id"], sealed["version"] + 1, DEMO_PARTNER, "future-convert-once",
        )
        replay = self.store.convert_future_letter_to_moment(
            sealed["id"], converted["letter_version_before"], DEMO_PARTNER, "future-convert-once",
        )
        self.assertEqual(converted["moment_id"], replay["moment_id"])
        with self.store.connection() as db:
            moment = db.execute("SELECT * FROM moments WHERE id=?", (converted["moment_id"],)).fetchone()
            self.assertEqual(moment["creator_id"], DEMO_USER)
            self.assertEqual(moment["visibility"], "shared")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM moments WHERE id=?", (converted["moment_id"],)).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
