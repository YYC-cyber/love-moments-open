from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from love_moments.intelligent_ui import ConflictError
from love_moments.store import DEMO_COUPLE, DEMO_PARTNER, DEMO_TRIP, DEMO_USER, LoveMomentsStore


MAX_SAFE_MINOR = 9_007_199_254_740_991


class TripExpenseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LoveMomentsStore(Path(self.temp.name) / "expenses.db", seed_demo_data=True)
        with self.store.connection() as db:
            db.executemany(
                "INSERT INTO users(id,username,nickname,avatar_label) VALUES (?,?,?,?)",
                [
                    ("user-outsider-a", "outsider_a", "外部甲", "甲"),
                    ("user-outsider-b", "outsider_b", "外部乙", "乙"),
                ],
            )
            db.execute(
                "INSERT INTO couples(id,member_a,member_b,paired_at,anniversary,status) "
                "VALUES ('couple-outsider','user-outsider-a','user-outsider-b','2026-01-01','2026-01-01','active')"
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def expense_payload(**changes: object) -> dict:
        payload = {
            "amount_cents": 1001,
            "payer_id": DEMO_USER,
            "category": "food",
            "split_mode": "equal",
            "occurred_on": "2026-10-24",
            "note": "西湖边晚餐",
        }
        payload.update(changes)
        return payload

    def create(self, *, key: str, user_id: str = DEMO_USER, **changes: object) -> dict:
        return self.store.create_trip_expense(
            DEMO_TRIP, self.expense_payload(**changes), key, user_id,
        )

    @staticmethod
    def member_balance(result: dict, user_id: str) -> dict:
        return next(item for item in result["balance"]["by_member"] if item["user_id"] == user_id)

    def test_exact_integer_validation_membership_currency_and_safe_serializer(self) -> None:
        created = self.create(key="expense:exact:0001", amount_cents=1234)
        self.assertEqual(created["amount_cents"], 1234)
        self.assertEqual(created["currency"], "CNY")
        self.assertEqual(created["source_type"], "human")
        self.assertNotIn("creator_id", created)
        self.assertNotIn("client_request_id", created)
        self.assertNotIn("amount_minor", created)

        invalid_payloads = [
            {"amount_cents": True}, {"amount_cents": 12.34}, {"amount_cents": "1234"},
            {"amount_cents": 0}, {"amount_cents": -1}, {"amount_cents": MAX_SAFE_MINOR + 1},
            {"occurred_on": "2026-02-30"}, {"note": "长" * 501}, {"category": "transfer"},
            {"split_mode": "custom"}, {"currency": "USD"}, {"payer_id": "user-outsider-a"},
            {"beneficiary_id": DEMO_PARTNER},
            {"split_mode": "personal"},
            {"split_mode": "personal", "beneficiary_id": "user-outsider-a"},
            {"source_type": "human"}, {"creator_id": DEMO_USER}, {"settled_at": "2026-10-24T00:00:00+00:00"},
            {"settled_by": DEMO_USER}, {"unknown": "field"},
        ]
        for index, changes in enumerate(invalid_payloads):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.create(key=f"expense:invalid:{index:04d}", **changes)

        personal = self.create(
            key="expense:personal:valid", amount_cents=500, split_mode="personal",
            beneficiary_id=DEMO_PARTNER,
        )
        self.assertEqual(personal["beneficiary_id"], DEMO_PARTNER)
        self.assertEqual(personal["beneficiary_name"], "演示乙")

    def test_checked_cumulative_limit_applies_to_create_edit_and_restore(self) -> None:
        first = self.create(key="expense:limit:first", amount_cents=MAX_SAFE_MINOR - 1)
        second = self.create(key="expense:limit:second", amount_cents=1)
        self.assertEqual(self.store.list_trip_expenses(DEMO_TRIP, DEMO_USER)["balance"]["total_active_cents"], MAX_SAFE_MINOR)
        with self.assertRaises(ValueError):
            self.create(key="expense:limit:overflow", amount_cents=1)

        deleted = self.store.delete_trip_expense(
            DEMO_TRIP, second["id"], {"expected_version": second["version"]},
            "expense:limit:delete", DEMO_USER,
        )
        third = self.create(key="expense:limit:replacement", amount_cents=1)
        with self.assertRaises(ValueError):
            self.store.restore_trip_expense(
                DEMO_TRIP, second["id"], {"expected_version": deleted["version"]},
                "expense:limit:restore", DEMO_USER,
            )
        with self.assertRaises(ValueError):
            self.store.update_trip_expense(
                DEMO_TRIP, third["id"],
                {"amount_cents": 2, "expected_version": third["version"]},
                "expense:limit:update", DEMO_USER,
            )
        listing = self.store.list_trip_expenses(DEMO_TRIP, DEMO_USER)
        self.assertEqual(listing["balance"]["total_active_cents"], MAX_SAFE_MINOR)
        self.assertEqual(next(item for item in listing["items"] if item["id"] == third["id"])["amount_cents"], 1)
        self.assertTrue(any(item["id"] == second["id"] for item in listing["deleted_items"]))
        self.assertEqual(first["version"], 1)

    def test_equal_odd_cent_is_always_borne_by_member_a_and_sign_is_personalized(self) -> None:
        a_paid = self.create(key="expense:odd:a", payer_id=DEMO_USER, amount_cents=1001)
        for user_id, expected_net, expected_direction in (
            (DEMO_USER, 500, "partner_owes_me"),
            (DEMO_PARTNER, -500, "i_owe_partner"),
        ):
            listing = self.store.list_trip_expenses(DEMO_TRIP, user_id)
            self.assertEqual(listing["balance"]["net_cents"], expected_net)
            self.assertEqual(listing["balance"]["direction"], expected_direction)
        self.store.settle_trip_expenses(
            DEMO_TRIP, {"expenses": [{"id": a_paid["id"], "expected_version": 1}]},
            "expense:odd:settle", DEMO_PARTNER,
        )
        self.create(key="expense:odd:b", payer_id=DEMO_PARTNER, amount_cents=1001)
        a_view = self.store.list_trip_expenses(DEMO_TRIP, DEMO_USER)
        b_view = self.store.list_trip_expenses(DEMO_TRIP, DEMO_PARTNER)
        self.assertEqual(a_view["balance"]["net_cents"], -501)
        self.assertEqual(b_view["balance"]["net_cents"], 501)
        self.assertEqual(self.member_balance(a_view, DEMO_USER)["owed_cents"], 501)
        self.assertEqual(self.member_balance(a_view, DEMO_PARTNER)["owed_cents"], 500)

    def test_personal_split_and_shared_visibility(self) -> None:
        self.create(
            key="expense:personal:a-for-b", amount_cents=1000, payer_id=DEMO_USER,
            split_mode="personal", beneficiary_id=DEMO_PARTNER,
        )
        self.create(
            key="expense:personal:b-for-a", amount_cents=400, payer_id=DEMO_PARTNER,
            split_mode="personal", beneficiary_id=DEMO_USER,
        )
        self.create(
            key="expense:personal:a-for-a", amount_cents=300, payer_id=DEMO_USER,
            split_mode="personal", beneficiary_id=DEMO_USER,
        )
        a_view = self.store.list_trip_expenses(DEMO_TRIP, DEMO_USER)
        b_view = self.store.list_trip_expenses(DEMO_TRIP, DEMO_PARTNER)
        self.assertEqual([item["id"] for item in a_view["items"]], [item["id"] for item in b_view["items"]])
        self.assertEqual(a_view["balance"]["net_cents"], 600)
        self.assertEqual(b_view["balance"]["net_cents"], -600)
        with self.assertRaises(KeyError):
            self.store.list_trip_expenses(DEMO_TRIP, "user-outsider-a")

    def test_edit_can_move_between_personal_and_equal_without_stale_beneficiary(self) -> None:
        personal = self.create(
            key="expense:split:personal", amount_cents=701, split_mode="personal",
            beneficiary_id=DEMO_PARTNER,
        )
        equal = self.store.update_trip_expense(
            DEMO_TRIP, personal["id"],
            {"split_mode": "equal", "expected_version": personal["version"]},
            "expense:split:equal", DEMO_USER,
        )
        self.assertEqual(equal["split_mode"], "equal")
        self.assertIsNone(equal["beneficiary_id"])
        personal_again = self.store.update_trip_expense(
            DEMO_TRIP, personal["id"],
            {"split_mode": "personal", "beneficiary_id": DEMO_USER, "expected_version": equal["version"]},
            "expense:split:personal-again", DEMO_PARTNER,
        )
        self.assertEqual(personal_again["beneficiary_id"], DEMO_USER)

    def test_create_and_edit_are_target_bound_idempotent_and_concurrent_safe(self) -> None:
        payload = self.expense_payload(note="只创建一次")

        def create_once() -> dict:
            return self.store.create_trip_expense(DEMO_TRIP, payload, "expense:concurrent:create", DEMO_USER)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [future.result() for future in (pool.submit(create_once), pool.submit(create_once))]
        self.assertEqual(results[0], results[1])
        self.assertEqual(len([item for item in self.store.list_trip_expenses(DEMO_TRIP, DEMO_USER)["items"] if item["note"] == "只创建一次"]), 1)
        with self.assertRaises(ConflictError):
            self.store.create_trip_expense(
                DEMO_TRIP, self.expense_payload(note="换了内容"), "expense:concurrent:create", DEMO_USER,
            )

        original = results[0]
        updated = self.store.update_trip_expense(
            DEMO_TRIP, original["id"],
            {"note": "伴侣修正", "expected_version": original["version"]},
            "expense:edit:partner", DEMO_PARTNER,
        )
        replay = self.store.update_trip_expense(
            DEMO_TRIP, original["id"],
            {"note": "伴侣修正", "expected_version": original["version"]},
            "expense:edit:partner", DEMO_PARTNER,
        )
        self.assertEqual(updated, replay)
        with self.assertRaises(ConflictError):
            self.store.update_trip_expense(
                DEMO_TRIP, original["id"],
                {"note": "相同键不同请求", "expected_version": original["version"]},
                "expense:edit:partner", DEMO_PARTNER,
            )
        with self.assertRaises(ConflictError):
            self.store.update_trip_expense(
                DEMO_TRIP, original["id"],
                {"note": "过期版本", "expected_version": original["version"]},
                "expense:edit:stale", DEMO_USER,
            )
        with self.store.connection() as db:
            creator_id = db.execute("SELECT creator_id FROM trip_expenses WHERE id=?", (original["id"],)).fetchone()[0]
        self.assertEqual(creator_id, DEMO_USER)

    def test_delete_restore_are_soft_versioned_and_idempotent(self) -> None:
        created = self.create(key="expense:delete:create")
        deleted = self.store.delete_trip_expense(
            DEMO_TRIP, created["id"], {"expected_version": created["version"]},
            "expense:delete:once", DEMO_PARTNER,
        )
        self.assertTrue(deleted["deleted"])
        self.assertEqual(
            deleted,
            self.store.delete_trip_expense(
                DEMO_TRIP, created["id"], {"expected_version": created["version"]},
                "expense:delete:once", DEMO_PARTNER,
            ),
        )
        listing = self.store.list_trip_expenses(DEMO_TRIP, DEMO_USER)
        self.assertFalse(any(item["id"] == created["id"] for item in listing["items"]))
        self.assertTrue(any(item["id"] == created["id"] for item in listing["deleted_items"]))
        with self.assertRaises(ConflictError):
            self.store.restore_trip_expense(
                DEMO_TRIP, created["id"], {"expected_version": created["version"]},
                "expense:restore:stale", DEMO_USER,
            )
        restored = self.store.restore_trip_expense(
            DEMO_TRIP, created["id"], {"expected_version": deleted["version"]},
            "expense:restore:once", DEMO_USER,
        )
        replay = self.store.restore_trip_expense(
            DEMO_TRIP, created["id"], {"expected_version": deleted["version"]},
            "expense:restore:once", DEMO_USER,
        )
        self.assertEqual(restored, replay)
        self.assertFalse(restored["deleted"])

    def test_settlement_is_atomic_idempotent_and_settled_edit_requires_explicit_reopen(self) -> None:
        first = self.create(key="expense:settle:first", amount_cents=1001)
        second = self.create(key="expense:settle:second", amount_cents=999, payer_id=DEMO_PARTNER)
        before = self.store.list_trip_expenses(DEMO_TRIP, DEMO_USER)
        with self.assertRaises(ConflictError):
            self.store.settle_trip_expenses(
                DEMO_TRIP,
                {"expenses": [
                    {"id": first["id"], "expected_version": first["version"]},
                    {"id": second["id"], "expected_version": 99},
                ]},
                "expense:settle:atomic-fail", DEMO_USER,
            )
        after_failed = self.store.list_trip_expenses(DEMO_TRIP, DEMO_USER)
        self.assertEqual(before, after_failed)

        request = {"expenses": [
            {"id": first["id"], "expected_version": first["version"]},
            {"id": second["id"], "expected_version": second["version"]},
        ]}
        settled = self.store.settle_trip_expenses(DEMO_TRIP, request, "expense:settle:valid", DEMO_PARTNER)
        self.assertEqual(settled["settled_count"], 2)
        self.assertEqual(settled, self.store.settle_trip_expenses(DEMO_TRIP, request, "expense:settle:valid", DEMO_PARTNER))
        self.assertEqual(self.store.list_trip_expenses(DEMO_TRIP, DEMO_USER)["balance"]["net_cents"], 0)
        with self.assertRaises(ConflictError):
            self.store.settle_trip_expenses(
                DEMO_TRIP, {"expenses": [{"id": first["id"], "expected_version": first["version"]}]},
                "expense:settle:valid", DEMO_PARTNER,
            )
        with self.assertRaises(ConflictError):
            self.store.update_trip_expense(
                DEMO_TRIP, first["id"],
                {"note": "不能静默重开", "expected_version": first["version"] + 1},
                "expense:settled:edit-denied", DEMO_USER,
            )
        reopened = self.store.update_trip_expense(
            DEMO_TRIP, first["id"],
            {"note": "明确重新计入", "expected_version": first["version"] + 1, "unset_settlement": True},
            "expense:settled:reopen", DEMO_USER,
        )
        self.assertIsNone(reopened["settled_at"])
        self.assertNotEqual(self.store.list_trip_expenses(DEMO_TRIP, DEMO_USER)["balance"]["net_cents"], 0)

    def test_financial_values_do_not_leak_to_generic_side_channels_but_export_is_complete(self) -> None:
        sentinel = "EXPENSE-SECRET-7fda3f"
        created = self.create(key="expense:leak:create", amount_cents=87654321, note=sentinel)
        deleted = self.store.delete_trip_expense(
            DEMO_TRIP, created["id"], {"expected_version": created["version"]},
            "expense:leak:delete", DEMO_USER,
        )
        with self.store.connection() as db:
            side_channel_tables = {
                "audit": [row[0] for row in db.execute("SELECT detail FROM audit_events")],
                "realtime": [row[0] for row in db.execute("SELECT payload_json FROM realtime_events")],
                "notifications": [json.dumps(dict(row), ensure_ascii=False) for row in db.execute("SELECT * FROM in_app_notifications")],
                "push": [json.dumps(dict(row), ensure_ascii=False) for row in db.execute("SELECT * FROM push_outbox")],
                "summaries": [json.dumps(dict(row), ensure_ascii=False) for row in db.execute("SELECT * FROM daily_couple_summaries")],
                "assistant": [json.dumps(dict(row), ensure_ascii=False) for row in db.execute("SELECT * FROM trip_assistant_messages")],
                "ui": [row[0] for row in db.execute("SELECT manifest_json FROM ui_manifests")],
            }
        for channel, values in side_channel_tables.items():
            serialized = json.dumps(values, ensure_ascii=False)
            self.assertNotIn(sentinel, serialized, channel)
            self.assertNotIn("87654321", serialized, channel)

        archive, _ = self.store.account_export(DEMO_USER)
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            exported = json.loads(bundle.read("data.json"))
        exported_row = next(item for item in exported["trip_expenses"] if item["id"] == created["id"])
        self.assertEqual(exported_row["note"], sentinel)
        self.assertEqual(exported_row["amount_cents"], 87654321)
        self.assertEqual(exported_row["source_type"], "human")
        self.assertIsNotNone(exported_row["deleted_at"])
        self.assertEqual(deleted["version"], 2)

    def test_trip_soft_delete_blocks_expenses_and_restore_reveals_unchanged_history(self) -> None:
        created = self.create(key="expense:trip-delete:create")
        trip = self.store.get_trip(DEMO_TRIP, DEMO_USER)
        deleted_trip = self.store.delete_trip(DEMO_TRIP, trip["version"], DEMO_USER)
        with self.assertRaises(KeyError):
            self.store.list_trip_expenses(DEMO_TRIP, DEMO_PARTNER)
        self.store.restore_trip(DEMO_TRIP, deleted_trip["version"], DEMO_PARTNER)
        restored = self.store.list_trip_expenses(DEMO_TRIP, DEMO_USER)
        self.assertTrue(any(item["id"] == created["id"] for item in restored["items"]))


if __name__ == "__main__":
    unittest.main()
