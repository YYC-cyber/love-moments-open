from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from love_moments.intelligent_ui import ConflictError
from love_moments.store import DEMO_COUPLE, DEMO_PARTNER, DEMO_USER, LoveMomentsStore


class CoupleDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LoveMomentsStore(Path(self.temp.name) / "decisions.db", seed_demo_data=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _future(hours: int = 4) -> str:
        return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(microsecond=0).isoformat()

    def _create(self, **overrides: object) -> dict:
        payload: dict[str, object] = {
            "title": "周末吃什么",
            "selection_mode": "single",
            "options": ["火锅", "烤肉", "日料"],
            "closes_at": self._future(),
        }
        payload.update(overrides)
        return self.store.create_decision(payload, DEMO_USER)

    def _confirmed(self, *, multiple: bool = False) -> dict:
        decision = self._create(selection_mode="multiple" if multiple else "single")
        option_ids = [item["id"] for item in decision["options"]]
        selected = option_ids[:2] if multiple else [option_ids[0]]
        self.store.submit_decision_vote(decision["id"], selected, 1, "convert-vote-user", DEMO_USER)
        revealed = self.store.submit_decision_vote(decision["id"], selected, 1, "convert-vote-partner", DEMO_PARTNER)
        pending = self.store.resolve_decision(decision["id"], "confirm", selected[0], revealed["version"], DEMO_USER)
        confirmed = self.store.resolve_decision(decision["id"], "confirm", selected[0], pending["version"], DEMO_PARTNER)
        if multiple:
            # A later resolver may confirm the whole multi-select intersection.
            # Conversion must still force the caller to pick one value for a
            # single-value target instead of silently taking the first.
            with self.store.connection() as db:
                db.execute(
                    "UPDATE decisions SET resolution_json=? WHERE id=?",
                    (json.dumps({"kind": "confirmed", "option_ids": selected}), decision["id"]),
                )
            confirmed = self.store.get_decision(decision["id"], DEMO_USER)
        return confirmed

    def test_conversion_preview_requires_confirmed_and_is_pure(self) -> None:
        open_decision = self._create(title="尚未完成")
        with self.assertRaisesRegex(ValueError, "双方确认"):
            self.store.prepare_decision_conversion(open_decision["id"], "meal_wish", None, DEMO_USER)

        confirmed = self._confirmed()
        with self.store.connection() as db:
            before = {
                "decision": tuple(db.execute("SELECT status,version,conversion_json,updated_at FROM decisions WHERE id=?", (confirmed["id"],)).fetchone()),
                "events": db.execute("SELECT COUNT(*) FROM realtime_events WHERE resource_id=?", (confirmed["id"],)).fetchone()[0],
                "audits": db.execute("SELECT COUNT(*) FROM audit_events WHERE target_id=?", (confirmed["id"],)).fetchone()[0],
                "wishes": db.execute("SELECT COUNT(*) FROM meal_wishes").fetchone()[0],
            }
        preview = self.store.prepare_decision_conversion(confirmed["id"], "meal_wish", None, DEMO_USER)
        repeated = self.store.prepare_decision_conversion(confirmed["id"], "meal_wish", None, DEMO_USER)
        self.assertEqual(preview, repeated)
        self.assertTrue(preview["requires_confirmation"])
        self.assertEqual(preview["editor"], "meal_wish")
        self.assertIn("title", preview["values"])
        serialized = json.dumps(preview, ensure_ascii=False)
        self.assertNotIn("votes", serialized)
        self.assertNotIn("resolution", serialized)
        with self.store.connection() as db:
            after = {
                "decision": tuple(db.execute("SELECT status,version,conversion_json,updated_at FROM decisions WHERE id=?", (confirmed["id"],)).fetchone()),
                "events": db.execute("SELECT COUNT(*) FROM realtime_events WHERE resource_id=?", (confirmed["id"],)).fetchone()[0],
                "audits": db.execute("SELECT COUNT(*) FROM audit_events WHERE target_id=?", (confirmed["id"],)).fetchone()[0],
                "wishes": db.execute("SELECT COUNT(*) FROM meal_wishes").fetchone()[0],
            }
        self.assertEqual(after, before)

    def test_conversion_preview_uses_server_resolution_and_allowlisted_targets(self) -> None:
        confirmed = self._confirmed(multiple=True)
        selected_ids = confirmed["resolution"]["option_ids"]
        with self.assertRaisesRegex(ValueError, "明确选择"):
            self.store.prepare_decision_conversion(confirmed["id"], "trip", None, DEMO_USER)
        with self.assertRaisesRegex(ValueError, "确认结果"):
            self.store.prepare_decision_conversion(confirmed["id"], "trip", confirmed["options"][2]["id"], DEMO_USER)
        with self.assertRaisesRegex(ValueError, "转换目标"):
            self.store.prepare_decision_conversion(confirmed["id"], "admin", selected_ids[0], DEMO_USER)

        for target in ("date_plan", "meal_wish", "trip", "hotel_search", "restaurant_search", "calendar_todo"):
            preview = self.store.prepare_decision_conversion(confirmed["id"], target, selected_ids[0], DEMO_USER)
            self.assertEqual(preview["target"], target)
            self.assertEqual(preview["source_type"], "human")
            self.assertNotIn("rating", json.dumps(preview))
            self.assertNotIn("booking_url", json.dumps(preview))

    def test_create_returns_public_candidates_and_is_shared_only_with_current_couple(self) -> None:
        decision = self._create()

        self.assertEqual(decision["title"], "周末吃什么")
        self.assertEqual(decision["selection_mode"], "single")
        self.assertEqual([item["label"] for item in decision["options"]], ["火锅", "烤肉", "日料"])
        self.assertEqual(decision["status"], "open")
        self.assertEqual(decision["version"], 1)
        self.assertFalse(decision["submitted_by_me"])
        self.assertFalse(decision["partner_submitted"])
        self.assertNotIn("votes", decision)
        self.assertNotIn("resolution", decision)

        partner_view = self.store.get_decision(decision["id"], DEMO_PARTNER)
        self.assertEqual(partner_view["id"], decision["id"])
        self.assertEqual(self.store.list_decisions(DEMO_PARTNER)[0]["id"], decision["id"])

        outsider_a = self.store.register(f"outside_{uuid.uuid4().hex[:8]}", "Outside-pass-123!", "外甲")
        outsider_b = self.store.register(f"outside_{uuid.uuid4().hex[:8]}", "Outside-pass-456!", "外乙")
        invite = self.store.create_user_invite(outsider_a["id"])
        self.store.accept_user_invite(outsider_b["id"], invite["code"], "2026-01-01")
        with self.assertRaises(KeyError):
            self.store.get_decision(decision["id"], outsider_a["id"])


    def test_create_notifies_partner_without_copying_candidate_content(self) -> None:
        decision = self._create(options=["不进通知甲", "不进通知乙"])
        notifications = self.store.list_in_app_notifications(DEMO_PARTNER)["items"]
        related = [item for item in notifications if item["resource_id"] == decision["id"]]
        self.assertEqual(len(related), 1)
        serialized = json.dumps(related[0], ensure_ascii=False)
        self.assertNotIn("不进通知甲", serialized)
        self.assertNotIn("不进通知乙", serialized)
        self.assertNotIn(decision["options"][0]["id"], serialized)

    def test_create_rejects_invalid_shape_without_writing_partial_rows(self) -> None:
        invalid_payloads = [
            {"title": "", "selection_mode": "single", "options": ["甲", "乙"], "closes_at": self._future()},
            {"title": "少选项", "selection_mode": "single", "options": ["甲"], "closes_at": self._future()},
            {"title": "多选项", "selection_mode": "single", "options": list("一二三四五六七"), "closes_at": self._future()},
            {"title": "重复", "selection_mode": "single", "options": ["甲", "甲"], "closes_at": self._future()},
            {"title": "模式", "selection_mode": "ranked", "options": ["甲", "乙"], "closes_at": self._future()},
            {"title": "过期", "selection_mode": "single", "options": ["甲", "乙"], "closes_at": "2000-01-01T00:00:00+00:00"},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.store.create_decision(payload, DEMO_USER)
        with self.assertRaises(ValueError):
            self.store.create_decision({
                "title": "伪造归属", "selection_mode": "single", "options": ["甲", "乙"],
                "closes_at": self._future(), "couple_id": "forged-couple",
            }, DEMO_USER)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)

    def test_vote_requires_strict_version_and_idempotency_key(self) -> None:
        decision = self._create(title="严格请求契约")
        option = decision["options"][0]["id"]
        for invalid_version in (None, True, 1.0, 0, "1"):
            with self.subTest(version=invalid_version), self.assertRaises(ValueError):
                self.store.submit_decision_vote(
                    decision["id"], [option], invalid_version, "strict-version-key", DEMO_USER,
                )
        for invalid_key in (None, "", "short", "has spaces invalid"):
            with self.subTest(key=invalid_key), self.assertRaises(ValueError):
                self.store.submit_decision_vote(
                    decision["id"], [option], 1, invalid_key, DEMO_USER,
                )

    def test_votes_remain_private_until_second_distinct_member_reveals_result(self) -> None:
        decision = self._create()
        first, second = [item["id"] for item in decision["options"][:2]]

        one = self.store.submit_decision_vote(
            decision["id"], [first], 1, "vote-private-0001", DEMO_USER,
        )
        self.assertEqual(one["version"], 1)
        self.assertTrue(one["submitted_by_me"])
        self.assertFalse(one["partner_submitted"])
        self.assertNotIn("votes", one)
        self.assertNotIn("resolution", one)

        partner_view = self.store.get_decision(decision["id"], DEMO_PARTNER)
        self.assertFalse(partner_view["submitted_by_me"])
        self.assertTrue(partner_view["partner_submitted"])
        self.assertNotIn("votes", partner_view)
        self.assertNotIn("resolution", partner_view)

        changed = self.store.submit_decision_vote(
            decision["id"], [second], 1, "vote-private-0002", DEMO_USER,
        )
        self.assertEqual(changed["version"], 1)
        revealed = self.store.submit_decision_vote(
            decision["id"], [second], 1, "vote-private-0003", DEMO_PARTNER,
        )
        self.assertEqual(revealed["status"], "revealed")
        self.assertEqual(revealed["resolution"]["kind"], "agreement")
        self.assertEqual(revealed["resolution"]["option_ids"], [second])
        self.assertEqual(
            {tuple(vote["option_ids"]) for vote in revealed["votes"]}, {(second,)},
        )
        with self.assertRaises(ValueError):
            self.store.submit_decision_vote(
                decision["id"], [first], 2, "vote-private-0004", DEMO_USER,
            )

    def test_private_ballot_changes_keep_public_version_and_side_effect_count_stable(self) -> None:
        decision = self._create(title="私密票不推动公开版本")
        first, second = [item["id"] for item in decision["options"][:2]]
        with self.store.connection() as db:
            initial_updated_at = db.execute(
                "SELECT updated_at FROM decisions WHERE id=?", (decision["id"],),
            ).fetchone()[0]
            initial_events = db.execute(
                "SELECT COUNT(*) FROM realtime_events WHERE resource_id=?", (decision["id"],),
            ).fetchone()[0]
            initial_audits = db.execute(
                "SELECT COUNT(*) FROM audit_events WHERE target_id=?", (decision["id"],),
            ).fetchone()[0]
            initial_notifications = db.execute(
                "SELECT COUNT(*) FROM in_app_notifications WHERE resource_id=?", (decision["id"],),
            ).fetchone()[0]

        first_ballot = self.store.submit_decision_vote(
            decision["id"], [first], 1, "private-version-vote-01", DEMO_USER,
        )
        changed_ballot = self.store.submit_decision_vote(
            decision["id"], [second], 1, "private-version-vote-02", DEMO_USER,
        )
        self.assertEqual(first_ballot["version"], 1)
        self.assertEqual(changed_ballot["version"], 1)
        self.assertNotIn("votes", changed_ballot)
        with self.store.connection() as db:
            self.assertEqual(db.execute(
                "SELECT updated_at FROM decisions WHERE id=?", (decision["id"],),
            ).fetchone()[0], initial_updated_at)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM realtime_events WHERE resource_id=?", (decision["id"],),
            ).fetchone()[0], initial_events)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM audit_events WHERE target_id=?", (decision["id"],),
            ).fetchone()[0], initial_audits)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM in_app_notifications WHERE resource_id=?", (decision["id"],),
            ).fetchone()[0], initial_notifications)

        revealed = self.store.submit_decision_vote(
            decision["id"], [second], 1, "private-version-vote-03", DEMO_PARTNER,
        )
        self.assertEqual(revealed["version"], 2)
        self.assertEqual(revealed["status"], "revealed")
        with self.store.connection() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM realtime_events WHERE resource_id=?", (decision["id"],),
            ).fetchone()[0], initial_events + 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM audit_events WHERE target_id=?", (decision["id"],),
            ).fetchone()[0], initial_audits + 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM in_app_notifications WHERE resource_id=?", (decision["id"],),
            ).fetchone()[0], initial_notifications + 1)

    def test_two_partners_can_vote_concurrently_from_same_public_version(self) -> None:
        decision = self._create(title="双方同版本并发投票")
        ids = [item["id"] for item in decision["options"]]
        barrier = threading.Barrier(2)
        results: list[dict] = []
        errors: list[Exception] = []

        def cast(user_id: str, option_id: str, key: str) -> None:
            try:
                barrier.wait(timeout=3)
                results.append(self.store.submit_decision_vote(
                    decision["id"], [option_id], 1, key, user_id,
                ))
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        threads = [
            threading.Thread(target=cast, args=(DEMO_USER, ids[0], "same-version-user-vote")),
            threading.Thread(target=cast, args=(DEMO_PARTNER, ids[1], "same-version-partner-vote")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(item["version"] for item in results), [1, 2])
        final = self.store.get_decision(decision["id"], DEMO_USER)
        self.assertEqual(final["status"], "revealed")
        self.assertEqual(final["version"], 2)
        self.assertEqual(final["resolution"], {"kind": "disagreement"})

    def test_unrevealed_choice_never_appears_in_events_audit_notifications_or_replay(self) -> None:
        decision = self._create(options=["绝密甲", "绝密乙"])
        selected = decision["options"][1]["id"]
        result = self.store.submit_decision_vote(
            decision["id"], [selected], 1, "privacy-vote-0001", DEMO_USER,
        )
        replay = self.store.submit_decision_vote(
            decision["id"], [selected], 1, "privacy-vote-0001", DEMO_USER,
        )
        self.assertEqual(replay, result)
        self.assertNotIn("votes", replay)
        self.assertNotIn("resolution", replay)

        with self.assertRaises(ConflictError):
            self.store.submit_decision_vote(
                decision["id"], [decision["options"][0]["id"]], 1,
                "privacy-vote-0001", DEMO_USER,
            )

        with self.store.connection() as db:
            event_rows = db.execute(
                "SELECT payload_json FROM realtime_events WHERE resource_id=?", (decision["id"],),
            ).fetchall()
            audit_rows = db.execute(
                "SELECT detail FROM audit_events WHERE target_id=?", (decision["id"],),
            ).fetchall()
            notification_rows = db.execute(
                "SELECT title,body,url,dedupe_key FROM in_app_notifications WHERE resource_id=?",
                (decision["id"],),
            ).fetchall()
            push_rows = db.execute(
                "SELECT payload_json FROM push_outbox WHERE payload_json LIKE ?",
                (f'%{decision["id"]}%',),
            ).fetchall()
        durable = json.dumps(
            [dict(row) for row in [*event_rows, *audit_rows, *notification_rows, *push_rows]],
            ensure_ascii=False,
        )
        self.assertNotIn(selected, durable)
        self.assertNotIn("绝密乙", durable)
        self.assertNotIn("option", durable.casefold())
        self.assertNotIn("selected", durable.casefold())

    def test_single_disagreement_and_multiple_intersection_are_computed_only_after_reveal(self) -> None:
        single = self._create(title="单选分歧")
        single_ids = [item["id"] for item in single["options"]]
        one = self.store.submit_decision_vote(single["id"], [single_ids[0]], 1, "single-diff-0001", DEMO_USER)
        single_result = self.store.submit_decision_vote(single["id"], [single_ids[1]], one["version"], "single-diff-0002", DEMO_PARTNER)
        self.assertEqual(single_result["resolution"]["kind"], "disagreement")
        self.assertNotIn("option_ids", single_result["resolution"])

        multiple = self._create(title="多选交集", selection_mode="multiple")
        multi_ids = [item["id"] for item in multiple["options"]]
        two = self.store.submit_decision_vote(multiple["id"], multi_ids[:2], 1, "multi-shared-0001", DEMO_USER)
        intersection = self.store.submit_decision_vote(multiple["id"], multi_ids[1:], two["version"], "multi-shared-0002", DEMO_PARTNER)
        self.assertEqual(intersection["resolution"], {"kind": "intersection", "option_ids": [multi_ids[1]]})

        empty = self._create(title="多选空交集", selection_mode="multiple")
        empty_ids = [item["id"] for item in empty["options"]]
        three = self.store.submit_decision_vote(empty["id"], [empty_ids[0]], 1, "multi-empty-0001", DEMO_USER)
        no_match = self.store.submit_decision_vote(empty["id"], [empty_ids[2]], three["version"], "multi-empty-0002", DEMO_PARTNER)
        self.assertEqual(no_match["resolution"], {"kind": "empty_intersection"})

    def test_creator_can_edit_before_first_vote_but_options_lock_atomically_after_vote(self) -> None:
        decision = self._create()
        with self.assertRaises(PermissionError):
            self.store.update_decision(
                decision["id"], {"title": "越权修改", "expected_version": 1}, DEMO_PARTNER,
            )
        edited = self.store.update_decision(
            decision["id"],
            {"title": "周末约会地点", "options": ["公园", "展览"], "expected_version": 1},
            DEMO_USER,
        )
        self.assertEqual(edited["version"], 2)
        self.assertEqual([item["label"] for item in edited["options"]], ["公园", "展览"])
        voted = self.store.submit_decision_vote(
            decision["id"], [edited["options"][0]["id"]], 2, "locked-options-0001", DEMO_USER,
        )
        self.assertTrue(voted["options_locked"])
        with self.assertRaises(ValueError):
            self.store.update_decision(
                decision["id"], {"options": ["新甲", "新乙"], "expected_version": 2}, DEMO_USER,
            )

    def test_expired_single_vote_stays_hidden_until_extend_or_cancel(self) -> None:
        decision = self._create()
        option = decision["options"][0]["id"]
        voted = self.store.submit_decision_vote(
            decision["id"], [option], 1, "expiry-hidden-0001", DEMO_USER,
        )
        with self.store.connection() as db:
            db.execute(
                "UPDATE decisions SET closes_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (decision["id"],),
            )
        expired = self.store.get_decision(decision["id"], DEMO_PARTNER)
        self.assertEqual(expired["status"], "awaiting_extension")
        self.assertNotIn("votes", expired)
        self.assertNotIn("resolution", expired)
        with self.assertRaises(ValueError):
            self.store.submit_decision_vote(
                decision["id"], [option], voted["version"], "expiry-hidden-0002", DEMO_PARTNER,
            )

        extended = self.store.extend_decision(
            decision["id"], self._future(8), voted["version"], DEMO_PARTNER,
        )
        self.assertEqual(extended["status"], "open")
        self.assertTrue(extended["partner_submitted"])
        self.assertNotIn("votes", extended)
        revealed = self.store.submit_decision_vote(
            decision["id"], [option], extended["version"], "expiry-hidden-0003", DEMO_PARTNER,
        )
        self.assertEqual(revealed["resolution"]["kind"], "agreement")

        cancelled = self._create(title="取消也不能泄密")
        secret = cancelled["options"][1]["id"]
        once = self.store.submit_decision_vote(
            cancelled["id"], [secret], 1, "cancel-hidden-0001", DEMO_USER,
        )
        ended = self.store.cancel_decision(cancelled["id"], once["version"], DEMO_PARTNER)
        self.assertEqual(ended["status"], "cancelled")
        self.assertNotIn("votes", ended)
        self.assertNotIn("resolution", ended)

    def test_revote_joint_confirmation_and_random_draw_follow_explicit_rules(self) -> None:
        decision = self._create(title="确认一个结果")
        option_ids = [item["id"] for item in decision["options"]]
        first = self.store.submit_decision_vote(decision["id"], [option_ids[0]], 1, "resolve-confirm-01", DEMO_USER)
        revealed = self.store.submit_decision_vote(decision["id"], [option_ids[1]], first["version"], "resolve-confirm-02", DEMO_PARTNER)

        with self.assertRaises(ValueError):
            self.store.resolve_decision(
                decision["id"], "confirm", option_ids[2], revealed["version"], DEMO_USER,
            )
        pending = self.store.resolve_decision(
            decision["id"], "confirm", option_ids[0], revealed["version"], DEMO_USER,
        )
        self.assertEqual(pending["status"], "pending_confirmation")
        self.assertEqual(pending["resolution"]["kind"], "confirmation_pending")
        repeated = self.store.resolve_decision(
            decision["id"], "confirm", option_ids[0], pending["version"], DEMO_USER,
        )
        self.assertEqual(repeated["version"], pending["version"])
        confirmed = self.store.resolve_decision(
            decision["id"], "confirm", option_ids[0], pending["version"], DEMO_PARTNER,
        )
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["resolution"], {"kind": "confirmed", "option_ids": [option_ids[0]]})

        revote = self._create(title="重新投票")
        revote_ids = [item["id"] for item in revote["options"]]
        old = self.store.submit_decision_vote(revote["id"], [revote_ids[0]], 1, "resolve-revote-001", DEMO_USER)
        split = self.store.submit_decision_vote(revote["id"], [revote_ids[1]], old["version"], "resolve-revote-002", DEMO_PARTNER)
        restarted = self.store.resolve_decision(revote["id"], "revote", None, split["version"], DEMO_USER)
        self.assertEqual(restarted["status"], "open")
        self.assertEqual(restarted["voting_round"], 2)
        self.assertTrue(restarted["options_locked"])
        self.assertNotIn("votes", restarted)
        with self.assertRaises(ConflictError):
            self.store.submit_decision_vote(revote["id"], [revote_ids[0]], 1, "resolve-revote-001", DEMO_USER)

        draw = self._create(title="随机抽取")
        draw_ids = [item["id"] for item in draw["options"]]
        one = self.store.submit_decision_vote(draw["id"], [draw_ids[0]], 1, "resolve-random-001", DEMO_USER)
        split = self.store.submit_decision_vote(draw["id"], [draw_ids[2]], one["version"], "resolve-random-002", DEMO_PARTNER)
        chosen = self.store.resolve_decision(draw["id"], "random_draw", None, split["version"], DEMO_USER)
        self.assertEqual(chosen["status"], "confirmed")
        self.assertIn(chosen["resolution"]["option_ids"][0], {draw_ids[0], draw_ids[2]})

    def test_first_confirmation_is_version_neutral_and_different_candidate_resets_it(self) -> None:
        decision = self._create(title="确认使用公开版本")
        ids = [item["id"] for item in decision["options"]]
        self.store.submit_decision_vote(
            decision["id"], [ids[0]], 1, "confirm-version-vote-a", DEMO_USER,
        )
        revealed = self.store.submit_decision_vote(
            decision["id"], [ids[1]], 1, "confirm-version-vote-b", DEMO_PARTNER,
        )
        self.assertEqual(revealed["version"], 2)

        first = self.store.resolve_decision(
            decision["id"], "confirm", ids[0], 2, DEMO_USER,
        )
        self.assertEqual(first["status"], "pending_confirmation")
        self.assertEqual(first["version"], 2)
        repeated = self.store.resolve_decision(
            decision["id"], "confirm", ids[0], 2, DEMO_USER,
        )
        self.assertEqual(repeated["version"], 2)
        with self.store.connection() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM decision_confirmations WHERE decision_id=? AND decision_version=2",
                (decision["id"],),
            ).fetchone()[0], 1)

        reset = self.store.resolve_decision(
            decision["id"], "confirm", ids[1], 2, DEMO_PARTNER,
        )
        self.assertEqual(reset["status"], "pending_confirmation")
        self.assertEqual(reset["version"], 2)
        with self.store.connection() as db:
            confirmations = db.execute(
                "SELECT user_id FROM decision_confirmations WHERE decision_id=? AND decision_version=2",
                (decision["id"],),
            ).fetchall()
        self.assertEqual([row["user_id"] for row in confirmations], [DEMO_PARTNER])

        confirmed = self.store.resolve_decision(
            decision["id"], "confirm", ids[1], 2, DEMO_USER,
        )
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["version"], 3)
        self.assertEqual(confirmed["resolution"], {"kind": "confirmed", "option_ids": [ids[1]]})

    def test_revote_preserves_old_round_votes_and_confirmations_without_serializing_them(self) -> None:
        decision = self._create(title="保留历史轮次")
        ids = [item["id"] for item in decision["options"]]
        self.store.submit_decision_vote(
            decision["id"], [ids[0]], 1, "history-round-one-a", DEMO_USER,
        )
        revealed = self.store.submit_decision_vote(
            decision["id"], [ids[1]], 1, "history-round-one-b", DEMO_PARTNER,
        )
        pending = self.store.resolve_decision(
            decision["id"], "confirm", ids[0], revealed["version"], DEMO_USER,
        )
        restarted = self.store.resolve_decision(
            decision["id"], "revote", None, pending["version"], DEMO_PARTNER,
        )
        self.assertEqual(restarted["voting_round"], 2)
        self.assertNotIn("votes", restarted)
        self.assertNotIn("resolution", restarted)
        with self.store.connection() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM decision_votes WHERE decision_id=? AND voting_round=1",
                (decision["id"],),
            ).fetchone()[0], 2)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM decision_confirmations WHERE decision_id=?",
                (decision["id"],),
            ).fetchone()[0], 1)

        round_two_first = self.store.submit_decision_vote(
            decision["id"], [ids[2]], restarted["version"], "history-round-two-a", DEMO_USER,
        )
        self.assertNotIn("votes", round_two_first)
        round_two_revealed = self.store.submit_decision_vote(
            decision["id"], [ids[0]], restarted["version"], "history-round-two-b", DEMO_PARTNER,
        )
        self.assertEqual(
            {tuple(item["option_ids"]) for item in round_two_revealed["votes"]},
            {(ids[0],), (ids[2],)},
        )
        self.store.resolve_decision(
            decision["id"], "random_draw", None, round_two_revealed["version"], DEMO_USER,
        )
        with self.store.connection() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM decision_confirmations WHERE decision_id=? AND decision_version=2",
                (decision["id"],),
            ).fetchone()[0], 1)

    def test_revote_permanently_locks_candidates_but_allows_safe_metadata_edits(self) -> None:
        decision = self._create(title="候选永久锁定")
        original_options = [(item["id"], item["label"]) for item in decision["options"]]
        ids = [item[0] for item in original_options]
        self.store.submit_decision_vote(
            decision["id"], [ids[0]], 1, "permanent-lock-round-one-a", DEMO_USER,
        )
        revealed = self.store.submit_decision_vote(
            decision["id"], [ids[1]], 1, "permanent-lock-round-one-b", DEMO_PARTNER,
        )
        pending = self.store.resolve_decision(
            decision["id"], "confirm", ids[0], revealed["version"], DEMO_USER,
        )
        restarted = self.store.resolve_decision(
            decision["id"], "revote", None, pending["version"], DEMO_PARTNER,
        )
        self.assertEqual(restarted["voting_round"], 2)

        renamed = self.store.update_decision(
            decision["id"],
            {"title": "候选不变但标题可改", "expected_version": restarted["version"]},
            DEMO_USER,
        )
        self.assertEqual(renamed["title"], "候选不变但标题可改")
        self.assertEqual([(item["id"], item["label"]) for item in renamed["options"]], original_options)

        with self.store.connection() as db:
            before = {
                "root": tuple(db.execute(
                    "SELECT title,selection_mode,version,voting_round,options_locked_at FROM decisions WHERE id=?",
                    (decision["id"],),
                ).fetchone()),
                "options": [tuple(row) for row in db.execute(
                    "SELECT id,label,position FROM decision_options WHERE decision_id=? ORDER BY position",
                    (decision["id"],),
                ).fetchall()],
                "votes": [tuple(row) for row in db.execute(
                    "SELECT voting_round,user_id,option_id FROM decision_votes WHERE decision_id=? ORDER BY voting_round,user_id,option_id",
                    (decision["id"],),
                ).fetchall()],
                "confirmations": [tuple(row) for row in db.execute(
                    "SELECT decision_version,user_id,confirmed_at FROM decision_confirmations WHERE decision_id=? ORDER BY decision_version,user_id",
                    (decision["id"],),
                ).fetchall()],
            }

        with self.assertRaises(ValueError):
            self.store.update_decision(
                decision["id"],
                {"options": ["新候选甲", "新候选乙"], "expected_version": renamed["version"]},
                DEMO_USER,
            )
        with self.assertRaises(ValueError):
            self.store.update_decision(
                decision["id"],
                {"selection_mode": "multiple", "expected_version": renamed["version"]},
                DEMO_USER,
            )
        self.assertTrue(self.store.get_decision(decision["id"], DEMO_USER)["options_locked"])

        with self.store.connection() as db:
            after = {
                "root": tuple(db.execute(
                    "SELECT title,selection_mode,version,voting_round,options_locked_at FROM decisions WHERE id=?",
                    (decision["id"],),
                ).fetchone()),
                "options": [tuple(row) for row in db.execute(
                    "SELECT id,label,position FROM decision_options WHERE decision_id=? ORDER BY position",
                    (decision["id"],),
                ).fetchall()],
                "votes": [tuple(row) for row in db.execute(
                    "SELECT voting_round,user_id,option_id FROM decision_votes WHERE decision_id=? ORDER BY voting_round,user_id,option_id",
                    (decision["id"],),
                ).fetchall()],
                "confirmations": [tuple(row) for row in db.execute(
                    "SELECT decision_version,user_id,confirmed_at FROM decision_confirmations WHERE decision_id=? ORDER BY decision_version,user_id",
                    (decision["id"],),
                ).fetchall()],
            }
        self.assertEqual(after, before)

        self.store.submit_decision_vote(
            decision["id"], [ids[2]], renamed["version"], "permanent-lock-round-two-a", DEMO_USER,
        )
        round_two = self.store.submit_decision_vote(
            decision["id"], [ids[0]], renamed["version"], "permanent-lock-round-two-b", DEMO_PARTNER,
        )
        self.assertEqual(round_two["version"], renamed["version"] + 1)
        self.assertEqual(
            {tuple(item["option_ids"]) for item in round_two["votes"]},
            {(ids[0],), (ids[2],)},
        )
        with self.store.connection() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM decision_votes WHERE decision_id=? AND voting_round=1",
                (decision["id"],),
            ).fetchone()[0], 2)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM decision_confirmations WHERE decision_id=?",
                (decision["id"],),
            ).fetchone()[0], 1)

    def test_status_filter_applies_before_limit_and_list_query_count_is_bounded(self) -> None:
        with self.store.connection() as db:
            couple, _ = self.store._couple_context(db, DEMO_USER)
            expired_id = "decision_old_expired"
            db.execute(
                """INSERT INTO decisions(
                    id,couple_id,creator_id,title,selection_mode,status,voting_round,
                    closes_at,version,created_at,updated_at
                ) VALUES (?,?,?,'较旧但已过期','single','open',1,?,1,?,?)""",
                (expired_id, couple["id"], DEMO_USER, "2000-01-01T00:00:00+00:00",
                 "2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00"),
            )
            for position, label in enumerate(("过期甲", "过期乙")):
                db.execute(
                    "INSERT INTO decision_options(id,decision_id,label,position,version) VALUES (?,?,?,?,1)",
                    (f"expired_option_{position}", expired_id, label, position),
                )
            future = self._future(24)
            for index in range(100):
                decision_id = f"decision_new_open_{index:03d}"
                stamp = f"2026-12-01T00:{index // 60:02d}:{index % 60:02d}+00:00"
                db.execute(
                    """INSERT INTO decisions(
                        id,couple_id,creator_id,title,selection_mode,status,voting_round,
                        closes_at,version,created_at,updated_at
                    ) VALUES (?,?,?,?,'single','open',1,?,1,?,?)""",
                    (decision_id, couple["id"], DEMO_USER, f"新决策 {index}", future, stamp, stamp),
                )
                for position, label in enumerate(("甲", "乙")):
                    db.execute(
                        "INSERT INTO decision_options(id,decision_id,label,position,version) VALUES (?,?,?,?,1)",
                        (f"option_{index}_{position}", decision_id, label, position),
                    )

        expired = self.store.list_decisions(DEMO_USER, status="awaiting_extension")
        self.assertEqual([item["id"] for item in expired], [expired_id])

        selects: list[str] = []
        original_connection = self.store.connection

        @contextmanager
        def traced_connection():
            with original_connection() as db:
                db.set_trace_callback(
                    lambda statement: selects.append(statement)
                    if statement.lstrip().upper().startswith("SELECT") else None
                )
                yield db

        with patch.object(self.store, "connection", traced_connection):
            items = self.store.list_decisions(DEMO_USER, status="open")
        self.assertEqual(len(items), 100)
        self.assertLessEqual(len(selects), 5, "100 条列表必须使用固定上界的批量查询")

    def test_same_key_concurrency_is_once_only_and_side_effect_failure_rolls_back(self) -> None:
        decision = self._create(title="并发幂等")
        option = decision["options"][0]["id"]
        barrier = threading.Barrier(2)
        results: list[dict] = []
        errors: list[Exception] = []

        def vote_once() -> None:
            try:
                barrier.wait(timeout=3)
                results.append(self.store.submit_decision_vote(
                    decision["id"], [option], 1, "concurrent-same-key", DEMO_USER,
                ))
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        threads = [threading.Thread(target=vote_once) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(results[0], results[1])
        with self.store.connection() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM decision_votes WHERE decision_id=?", (decision["id"],),
            ).fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM audit_events WHERE target_id=? AND action='decision.vote_submitted'",
                (decision["id"],),
            ).fetchone()[0], 0)

        rollback = self._create(title="事务回滚")
        rollback_option = rollback["options"][0]["id"]
        self.store.submit_decision_vote(
            rollback["id"], [rollback_option], 1, "rollback-partner-first", DEMO_PARTNER,
        )
        with patch.object(self.store, "_event", side_effect=RuntimeError("event failed")):
            with self.assertRaises(RuntimeError):
                self.store.submit_decision_vote(
                    rollback["id"], [rollback_option], 1, "rollback-reveal-key", DEMO_USER,
                )
        pristine = self.store.get_decision(rollback["id"], DEMO_USER)
        self.assertEqual(pristine["version"], 1)
        self.assertFalse(pristine["submitted_by_me"])
        self.assertTrue(pristine["partner_submitted"])
        with self.store.connection() as db:
            self.assertEqual(db.execute(
                """SELECT COUNT(*) FROM mutation_idempotency
                WHERE operation=? AND user_id=? AND idempotency_key=?""",
                (f"decision.vote:{rollback['id']}", DEMO_USER, "rollback-reveal-key"),
            ).fetchone()[0], 0)

    def test_competing_votes_and_option_edit_are_serialized_by_version(self) -> None:
        decision = self._create(title="并发版本")
        ids = [item["id"] for item in decision["options"]]
        barrier = threading.Barrier(2)
        successes: list[dict] = []
        conflicts: list[Exception] = []

        def competing_vote(user_id: str, option_id: str, key: str) -> None:
            try:
                barrier.wait(timeout=3)
                successes.append(self.store.submit_decision_vote(
                    decision["id"], [option_id], 1, key, user_id,
                ))
            except Exception as error:  # pragma: no cover - asserted below
                conflicts.append(error)

        threads = [
            threading.Thread(target=competing_vote, args=(DEMO_USER, ids[0], "different-vote-key-a")),
            threading.Thread(target=competing_vote, args=(DEMO_PARTNER, ids[1], "different-vote-key-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(successes), 2)
        self.assertEqual(conflicts, [])
        current = self.store.get_decision(decision["id"], DEMO_USER)
        self.assertEqual(current["version"], 2)
        self.assertEqual(current["status"], "revealed")

        race = self._create(title="编辑投票竞争")
        race_option = race["options"][0]["id"]
        barrier = threading.Barrier(2)
        successes.clear()
        conflicts.clear()

        def edit() -> None:
            try:
                barrier.wait(timeout=3)
                successes.append(self.store.update_decision(
                    race["id"], {"options": ["改甲", "改乙"], "expected_version": 1}, DEMO_USER,
                ))
            except Exception as error:  # pragma: no cover - asserted below
                conflicts.append(error)

        def vote() -> None:
            try:
                barrier.wait(timeout=3)
                successes.append(self.store.submit_decision_vote(
                    race["id"], [race_option], 1, "edit-vote-race-key", DEMO_USER,
                ))
            except Exception as error:  # pragma: no cover - asserted below
                conflicts.append(error)

        threads = [threading.Thread(target=edit), threading.Thread(target=vote)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertIsInstance(conflicts[0], (ConflictError, ValueError))
        current = self.store.get_decision(race["id"], DEMO_USER)
        if current["submitted_by_me"]:
            self.assertEqual(current["version"], 1)
            self.assertEqual([item["label"] for item in current["options"]], ["火锅", "烤肉", "日料"])
        else:
            self.assertEqual(current["version"], 2)
            self.assertEqual([item["label"] for item in current["options"]], ["改甲", "改乙"])

class WishlistClaimAndStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LoveMomentsStore(Path(self.temp.name) / "claims.db", seed_demo_data=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _gift(self) -> dict:
        return self.store.create_wishlist_item(
            {"text": "一份惊喜礼物", "note": "公开备注", "item_kind": "gift"}, DEMO_USER,
        )

    def test_secret_claim_is_private_idempotent_and_has_one_winner(self) -> None:
        gift = self._gift()
        before = json.dumps(self.store.get_wishlist(DEMO_USER), ensure_ascii=False, sort_keys=True)
        context_before = self.store.get_couple_context(DEMO_USER)
        event_before = self.store.list_realtime_events(0, user_id=DEMO_USER)["latest_sequence"]
        with self.store.connection() as db:
            audit_before = db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        first = self.store.claim_wishlist_item(
            gift["id"], "我会准备", False, gift["version"], "gift-claim-key-0001", DEMO_PARTNER,
        )
        replay = self.store.claim_wishlist_item(
            gift["id"], "我会准备", False, gift["version"], "gift-claim-key-0001", DEMO_PARTNER,
        )
        self.assertEqual(first, replay)
        self.assertEqual(first["private_note"], "我会准备")
        claimant_item = next(
            item for item in self.store.get_wishlist(DEMO_PARTNER)["items"]
            if item["id"] == gift["id"]
        )
        self.assertEqual(claimant_item["my_claim"]["private_note"], "我会准备")
        with self.assertRaises(ConflictError):
            self.store.claim_wishlist_item(
                gift["id"], "换一段备注", False, gift["version"], "gift-claim-key-0001", DEMO_PARTNER,
            )
        with self.assertRaises(ConflictError):
            self.store.claim_wishlist_item(
                gift["id"], "另一个请求", False, gift["version"], "gift-claim-key-0002", DEMO_PARTNER,
            )
        self.assertEqual(json.dumps(self.store.get_wishlist(DEMO_USER), ensure_ascii=False, sort_keys=True), before)
        context_after = self.store.get_couple_context(DEMO_USER)
        for context in (context_before, context_after):
            for member in context["members"]:
                member.pop("local_time", None)
                member.pop("status_age_minutes", None)
        self.assertEqual(context_after, context_before)
        self.assertEqual(self.store.list_realtime_events(event_before, user_id=DEMO_USER)["items"], [])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0], audit_before)
        self.assertEqual(self.store.list_in_app_notifications(DEMO_USER)["unread_count"], 0)
        self.assertNotIn("claim", json.dumps(self.store.get_shared_content("wishlist_item", gift["id"], DEMO_USER), ensure_ascii=False).lower())

        creator_archive, _ = self.store.account_export(DEMO_USER)
        with zipfile.ZipFile(io.BytesIO(creator_archive)) as archive:
            creator_export = json.loads(archive.read("data.json"))
        claimant_archive, _ = self.store.account_export(DEMO_PARTNER)
        with zipfile.ZipFile(io.BytesIO(claimant_archive)) as archive:
            claimant_export = json.loads(archive.read("data.json"))
        self.assertEqual(creator_export["wishlist_claims"], [])
        self.assertEqual(len(claimant_export["wishlist_claims"]), 1)
        self.assertNotIn("private_note", claimant_export["wishlist_claims"][0])
        self.assertNotIn("我会准备", json.dumps(claimant_export, ensure_ascii=False))

    def test_concurrent_claim_attempts_have_exactly_one_winner(self) -> None:
        gift = self._gift()
        barrier = threading.Barrier(2)
        successes: list[dict] = []
        failures: list[Exception] = []

        def claim(key: str) -> None:
            try:
                barrier.wait(timeout=3)
                successes.append(self.store.claim_wishlist_item(
                    gift["id"], key, False, gift["version"], key, DEMO_PARTNER,
                ))
            except Exception as error:  # pragma: no cover - asserted below
                failures.append(error)

        threads = [threading.Thread(target=claim, args=(f"concurrent-gift-claim-{index}",)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], ConflictError)
        with self.store.connection() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM wishlist_claims WHERE item_id=? AND status='active'", (gift["id"],),
            ).fetchone()[0], 1)

    def test_creator_edit_preserves_claim_and_creator_completion_cancels_privately(self) -> None:
        gift = self._gift()
        claim = self.store.claim_wishlist_item(
            gift["id"], "不要泄露", False, gift["version"], "gift-claim-key-1001", DEMO_PARTNER,
        )
        updated = self.store.update_wishlist_item(
            gift["id"], {"text": "改过的礼物", "note": "仍然公开", "expected_version": gift["version"]}, DEMO_USER,
        )
        mine = next(item for item in self.store.get_wishlist(DEMO_PARTNER)["items"] if item["id"] == gift["id"])
        self.assertEqual(mine["my_claim"]["id"], claim["id"])
        self.store.toggle_wishlist_item(gift["id"], True, DEMO_USER)
        notices = self.store.list_in_app_notifications(DEMO_PARTNER)["items"]
        self.assertTrue(any(item["resource_id"] == gift["id"] for item in notices))
        self.assertNotIn("不要泄露", json.dumps(notices, ensure_ascii=False))
        with self.store.connection() as db:
            row = db.execute("SELECT status FROM wishlist_claims WHERE id=?", (claim["id"],)).fetchone()
        self.assertEqual(row["status"], "cancelled")
        self.assertNotIn("claim", json.dumps(updated, ensure_ascii=False).lower())

    def test_claimant_completion_reveal_controls_identity_and_public_moment(self) -> None:
        gift = self._gift()
        claim = self.store.claim_wishlist_item(
            gift["id"], "私密购买计划", True, gift["version"], "gift-claim-key-2001", DEMO_PARTNER,
        )
        with self.assertRaises(ValueError):
            self.store.complete_wishlist_claim(
                gift["id"], claim["version"], False, True, "gift-complete-key-2001", DEMO_PARTNER,
            )
        result = self.store.complete_wishlist_claim(
            gift["id"], claim["version"], True, True, "gift-complete-key-2002", DEMO_PARTNER,
        )
        self.assertTrue(result["revealed"])
        detail = self.store.get_shared_content("wishlist_item", gift["id"], DEMO_USER)
        self.assertEqual(detail["claimant_name"], "演示乙")
        self.assertNotIn("私密购买计划", json.dumps(detail, ensure_ascii=False))
        moment = next(item for item in self.store.list_moments(user_id=DEMO_USER) if item["id"] == result["moment_id"])
        self.assertNotIn("私密购买计划", json.dumps(moment, ensure_ascii=False))

    def test_status_contract_expiry_and_no_history(self) -> None:
        for code in ("busy", "want_chat", "need_company", "feeling_low", "moving", "home"):
            status = self.store.set_status({"status_code": code, "duration_minutes": 30, "emoji": "伪造"}, DEMO_USER)
            self.assertEqual(status["status_code"], code)
            self.assertNotEqual(status["emoji"], "伪造")
        for invalid in (29, 10081, True, 30.5, None):
            with self.subTest(duration=invalid), self.assertRaises(ValueError):
                self.store.set_status({"status_code": "busy", "duration_minutes": invalid}, DEMO_USER)
        with self.assertRaises(ValueError):
            self.store.set_status({"status_code": "custom", "text": "", "duration_minutes": 30}, DEMO_USER)
        with self.assertRaises(ValueError):
            self.store.set_status({"status_code": "miss_you", "duration_minutes": 30}, DEMO_USER)
        self.store.set_status({"status_code": "custom", "text": "今天需要安静一点", "emoji": "伪造", "duration_minutes": 30}, DEMO_USER)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM couple_statuses WHERE user_id=?", (DEMO_USER,)).fetchone()[0], 1)
            db.execute("UPDATE couple_statuses SET expires_at='2000-01-01T00:00:00+00:00' WHERE user_id=?", (DEMO_USER,))
        mine = next(item for item in self.store.get_couple_context(DEMO_USER)["members"] if item["is_me"])
        self.assertIsNone(mine["status_code"])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM couple_statuses WHERE user_id=?", (DEMO_USER,)).fetchone()[0], 0)

    def test_status_read_is_scoped_to_the_current_couple(self) -> None:
        self.store.set_status({
            "status_code": "custom", "text": "旧关系里的私人状态", "duration_minutes": 120,
        }, DEMO_USER)
        new_partner = "user_status_new_partner"
        new_couple = "couple_status_new_scope"
        with self.store.connection() as db:
            password_hash = db.execute(
                "SELECT password_hash FROM users WHERE id=?", (DEMO_PARTNER,),
            ).fetchone()["password_hash"]
            db.execute(
                "INSERT INTO users(id,username,password_hash,nickname,avatar_label) VALUES (?,?,?,?,?)",
                (new_partner, "status-partner", password_hash, "新", "新"),
            )
            db.execute("UPDATE couples SET status='unlinked' WHERE id=?", (DEMO_COUPLE,))
            db.execute(
                "INSERT INTO couples(id,member_a,member_b,paired_at,anniversary) VALUES (?,?,?,?,?)",
                (new_couple, DEMO_USER, new_partner, "2026-10-28", "2026-10-28"),
            )
        context = self.store.get_couple_context(new_partner)
        old_owner = next(item for item in context["members"] if item["id"] == DEMO_USER)
        self.assertIsNone(old_owner["status_code"])
        self.assertIsNone(old_owner["status_text"])
        self.assertNotIn("旧关系", json.dumps(context, ensure_ascii=False))

    def test_unlink_and_account_delete_clear_every_status_and_claim_in_that_couple(self) -> None:
        first = self._gift()
        second = self.store.create_wishlist_item(
            {"text": "对方的礼物", "item_kind": "gift"}, DEMO_PARTNER,
        )
        claim_a = self.store.claim_wishlist_item(
            first["id"], "对方的秘密备注", False, first["version"],
            "claim-lifecycle-a", DEMO_PARTNER,
        )
        claim_b = self.store.claim_wishlist_item(
            second["id"], "我的秘密备注", False, second["version"],
            "claim-lifecycle-b", DEMO_USER,
        )
        self.store.set_status({"status_code": "custom", "text": "演示甲的旧状态", "duration_minutes": 120}, DEMO_USER)
        self.store.set_status({"status_code": "custom", "text": "演示乙的旧状态", "duration_minutes": 120}, DEMO_PARTNER)
        self.store.unlink_couple("test123", "解除情侣", DEMO_USER)
        with self.store.connection() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM couple_statuses WHERE couple_id=?", (DEMO_COUPLE,),
            ).fetchone()[0], 0)
            rows = db.execute(
                "SELECT id,status,private_note FROM wishlist_claims WHERE id IN (?,?) ORDER BY id",
                (claim_a["id"], claim_b["id"]),
            ).fetchall()
            self.assertEqual({row["status"] for row in rows}, {"cancelled"})
            self.assertTrue(all(row["private_note"] == "" for row in rows))
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM mutation_idempotency WHERE couple_id=? "
                "AND operation LIKE 'wishlist.claim%'", (DEMO_COUPLE,),
            ).fetchone()[0], 0)

        deleted_store = LoveMomentsStore(
            Path(self.temp.name) / "delete-lifecycle.db", seed_demo_data=True,
        )
        gift = deleted_store.create_wishlist_item(
            {"text": "注销前礼物", "item_kind": "gift"}, DEMO_USER,
        )
        deleted_claim = deleted_store.claim_wishlist_item(
            gift["id"], "注销也要清空", False, gift["version"],
            "claim-delete-lifecycle", DEMO_PARTNER,
        )
        deleted_store.set_status({"status_code": "busy", "duration_minutes": 120}, DEMO_USER)
        deleted_store.set_status({"status_code": "want_chat", "duration_minutes": 120}, DEMO_PARTNER)
        deleted_store.delete_account("test123", "注销账号", DEMO_USER)
        with deleted_store.connection() as db:
            row = db.execute(
                "SELECT status,private_note FROM wishlist_claims WHERE id=?", (deleted_claim["id"],),
            ).fetchone()
            self.assertEqual(row["status"], "cancelled")
            self.assertEqual(row["private_note"], "")
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM couple_statuses WHERE couple_id=?", (DEMO_COUPLE,),
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM mutation_idempotency WHERE couple_id=? "
                "AND operation LIKE 'wishlist.claim%'", (DEMO_COUPLE,),
            ).fetchone()[0], 0)

    def test_claim_receipts_never_persist_private_note_and_replay_reauthorizes_claimant(self) -> None:
        gift = self._gift()
        first = self.store.claim_wishlist_item(
            gift["id"], "绝不能进入幂等回执", False, gift["version"],
            "claim-safe-receipt", DEMO_PARTNER,
        )
        with self.store.connection() as db:
            receipt = db.execute(
                "SELECT result_json FROM mutation_idempotency WHERE couple_id=? "
                "AND user_id=? AND operation='wishlist.claim' AND idempotency_key='claim-safe-receipt'",
                (DEMO_COUPLE, DEMO_PARTNER),
            ).fetchone()["result_json"]
            self.assertNotIn("绝不能进入", receipt)
            self.assertNotIn("private_note", receipt)
            db.execute("UPDATE couples SET status='unlinked' WHERE id=?", (DEMO_COUPLE,))
        with self.assertRaises(ValueError):
            self.store.claim_wishlist_item(
                gift["id"], "绝不能进入幂等回执", False, gift["version"],
                "claim-safe-receipt", DEMO_PARTNER,
            )
        self.assertEqual(first["private_note"], "绝不能进入幂等回执")
        with self.store.connection() as db:
            self.assertEqual(db.execute(
                "SELECT private_note FROM wishlist_claims WHERE id=?", (first["id"],),
            ).fetchone()["private_note"], "绝不能进入幂等回执")

    def test_old_claim_key_replays_the_original_safe_snapshot_after_release(self) -> None:
        gift = self._gift()
        first = self.store.claim_wishlist_item(
            gift["id"], "只留在认领明细里的备注", False, gift["version"],
            "claim-stable-snapshot", DEMO_PARTNER,
        )
        self.assertEqual(first["private_note"], "只留在认领明细里的备注")
        self.assertEqual(first["item_id"], gift["id"])
        self.assertEqual(first["status"], "active")
        self.assertEqual(first["version"], 1)

        released = self.store.release_wishlist_claim(
            gift["id"], first["version"], "release-stable-snapshot", DEMO_PARTNER,
        )
        self.assertNotIn("private_note", released)
        self.assertEqual(released["status"], "released")
        replay = self.store.claim_wishlist_item(
            gift["id"], "只留在认领明细里的备注", False, gift["version"],
            "claim-stable-snapshot", DEMO_PARTNER,
        )
        self.assertEqual(replay, first)

        with self.store.connection() as db:
            stored_note = db.execute(
                "SELECT private_note FROM wishlist_claims WHERE id=?", (first["id"],),
            ).fetchone()["private_note"]
            receipts = [row["result_json"] for row in db.execute(
                "SELECT result_json FROM mutation_idempotency WHERE operation LIKE 'wishlist.claim%'",
            ).fetchall()]
        self.assertEqual(stored_note, "只留在认领明细里的备注")
        self.assertNotIn("只留在认领", json.dumps(receipts, ensure_ascii=False))
        self.assertNotIn("private_note", json.dumps(receipts, ensure_ascii=False))

        creator_view = self.store.get_wishlist(DEMO_USER)
        creator_item = next(item for item in creator_view["items"] if item["id"] == gift["id"])
        self.assertNotIn("my_claim", creator_item)

        second = self.store.create_wishlist_item(
            {"text": "另一份惊喜", "item_kind": "gift"}, DEMO_USER,
        )
        second_claim = self.store.claim_wishlist_item(
            second["id"], "完成操作也不能带出备注", False, second["version"],
            "claim-complete-snapshot", DEMO_PARTNER,
        )
        completed = self.store.complete_wishlist_claim(
            second["id"], second_claim["version"], False, False,
            "complete-stable-snapshot", DEMO_PARTNER,
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["version"], 2)
        self.assertIn("created_at", completed)
        self.assertIn("updated_at", completed)
        self.assertNotIn("private_note", completed)
        completed_replay = self.store.complete_wishlist_claim(
            second["id"], second_claim["version"], False, False,
            "complete-stable-snapshot", DEMO_PARTNER,
        )
        self.assertEqual(completed_replay, completed)

    def test_reopening_demo_store_does_not_restore_status_for_unlinked_couple(self) -> None:
        self.store.set_status({"status_code": "custom", "text": "不能复活", "duration_minutes": 120}, DEMO_PARTNER)
        self.store.unlink_couple("test123", "解除情侣", DEMO_USER)
        reopened = LoveMomentsStore(self.store.db_path, seed_demo_data=True)
        with reopened.connection() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM couple_statuses WHERE couple_id=?", (DEMO_COUPLE,),
            ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
