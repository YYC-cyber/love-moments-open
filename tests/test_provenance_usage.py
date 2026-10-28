from __future__ import annotations

import json
import io
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from love_moments.intelligent_ui import ConflictError
from love_moments.life_assistant import OpenAICompatibleLifeAssistant
from love_moments.store import DEMO_PARTNER, DEMO_USER, LoveMomentsStore


class ProvenanceUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LoveMomentsStore(Path(self.temp.name) / "foundation.db", seed_demo_data=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _insert_sensitive_foundation_rows(self) -> dict[str, str]:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with self.store.connection() as db:
            couple = db.execute("SELECT * FROM couples WHERE status='active' LIMIT 1").fetchone()
            trip_id = db.execute("SELECT id FROM trips WHERE couple_id=? LIMIT 1", (couple["id"],)).fetchone()["id"]
            for media_id, owner_id, visibility in (
                ("voice-own", DEMO_USER, "private"),
                ("voice-partner", DEMO_PARTNER, "private"),
                ("voice-shared", DEMO_PARTNER, "shared"),
            ):
                db.execute(
                    """INSERT INTO media_assets(
                        id,couple_id,owner_id,kind,original_name,mime_type,relative_path,
                        size_bytes,visibility,status,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,'ready',?)""",
                    (media_id, couple["id"], owner_id, "audio", f"{media_id}.m4a", "audio/mp4", f"audio/{media_id}.m4a", 1, visibility, now),
                )
                moment_id = f"moment-{media_id}"
                db.execute(
                    """INSERT INTO moments(
                        id,couple_id,creator_id,type,title,body,occurred_at,
                        visibility,media_id,created_at
                    ) VALUES (?,?,?,'voice',?,'',?,?,?,?)""",
                    (moment_id, couple["id"], owner_id, media_id, now, visibility, media_id, now),
                )
                db.execute(
                    """INSERT INTO voice_transcripts(
                        id,couple_id,owner_id,media_id,moment_id,body,method,source_type,status,visibility,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,'manual','human','ready',?,?,?)""",
                    (f"transcript-{media_id}", couple["id"], owner_id, media_id, moment_id, f"BODY-{media_id}", visibility, now, now),
                )
            for entry_id, creator_id, visibility in (
                ("emergency-own", DEMO_USER, "private"),
                ("emergency-own-shared", DEMO_USER, "shared"),
                ("emergency-partner", DEMO_PARTNER, "private"),
                ("emergency-shared", DEMO_PARTNER, "shared"),
            ):
                db.execute(
                    """INSERT INTO trip_emergency_entries(
                        id,trip_id,creator_id,entry_type,title,details_json,visibility,source_type,created_at,updated_at
                    ) VALUES (?,?,?,'contact',?,? ,?,'human',?,?)""",
                    (entry_id, trip_id, creator_id, entry_id, json.dumps({"secret": entry_id}), visibility, now, now),
                )
            db.execute(
                """INSERT INTO future_letters(
                    id,couple_id,author_id,recipient_id,title,body,status,deliver_at,sealed_at,version,created_at,updated_at
                ) VALUES ('letter-own',?,?,?,'OWN-TITLE','OWN-SEALED','sealed',?,?,1,?,?)""",
                (couple["id"], DEMO_USER, DEMO_PARTNER, "2099-01-01T00:00:00+00:00", now, now, now),
            )
            db.execute(
                """INSERT INTO future_letters(
                    id,couple_id,author_id,recipient_id,title,body,status,deliver_at,sealed_at,version,created_at,updated_at
                ) VALUES ('letter-partner',?,?,?,'PARTNER-TITLE','PARTNER-SEALED','sealed',?,?,1,?,?)""",
                (couple["id"], DEMO_PARTNER, DEMO_USER, "2099-01-01T00:00:00+00:00", now, now, now),
            )
            db.execute(
                """INSERT INTO future_letters(
                    id,couple_id,author_id,recipient_id,title,body,status,delivered_at,version,created_at,updated_at
                ) VALUES ('letter-delivered',?,?,?,'SHARED-TITLE','SHARED-DELIVERED','delivered',?,1,?,?)""",
                (couple["id"], DEMO_PARTNER, DEMO_USER, now, now, now),
            )
        return {"couple_id": couple["id"], "trip_id": trip_id}

    def test_ai_usage_keeps_real_terminal_state_and_unknown_price_null(self) -> None:
        started = self.store.record_ai_usage_event(
            actor_kind="user", user_id=DEMO_USER, feature="life_assistant",
            provider="openai-compatible", model="test-model", request_id="request-usage-1",
            status="started", request_sent=True,
        )
        terminal = self.store.record_ai_usage_event(
            actor_kind="user", user_id=DEMO_USER, feature="life_assistant",
            provider="openai-compatible", model="test-model", request_id="request-usage-1",
            status="timeout", request_sent=True, input_tokens=12, output_tokens=4,
            latency_ms=1500, failure_code="transport_timeout",
        )
        self.assertEqual(terminal["id"], started["id"])
        self.assertEqual(terminal["status"], "timeout")
        self.assertIsNone(terminal["estimated_cost"])
        self.assertNotIn("prompt", terminal)
        self.assertNotIn("response", terminal)

    def test_life_assistant_provider_records_real_success_usage_without_content(self) -> None:
        response = {
            "choices": [{"message": {"content": json.dumps({
                "requested_date": "2099-10-16", "meal_slot": "dinner",
                "planned_time": "18:30", "dish_query": "番茄炒蛋", "task_query": "",
                "add_calendar": True, "add_shopping": False, "add_todo": False,
            }, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 21, "completion_tokens": 9, "total_tokens": 30},
        }

        class ModelResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self) -> bytes:
                return json.dumps(response, ensure_ascii=False).encode()

        self.store.life_assistant_provider = OpenAICompatibleLifeAssistant(
            "not-a-real-key", "https://example.test/v1", "test-model", 1
        )
        with mock.patch("love_moments.life_assistant.request.urlopen", return_value=ModelResponse()):
            self.store.create_life_assistant_job(
                {"intent": "2099年10月16日晚餐想吃番茄炒蛋，加入日历", "client_request_id": "usage-success-request"},
                DEMO_USER,
            )
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM ai_usage_events WHERE request_id LIKE 'assistant_job_%'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["input_tokens"], 21)
        self.assertEqual(row["output_tokens"], 9)
        self.assertEqual(row["total_tokens"], 30)
        self.assertIsNone(row["estimated_cost"])
        self.assertNotIn("番茄炒蛋", json.dumps(dict(row), ensure_ascii=False))

    def test_life_assistant_timeout_is_recorded_even_when_rules_fallback_succeeds(self) -> None:
        self.store.life_assistant_provider = OpenAICompatibleLifeAssistant(
            "not-a-real-key", "https://example.test/v1", "test-model", 1
        )
        with mock.patch("love_moments.life_assistant.request.urlopen", side_effect=TimeoutError("secret timeout detail")):
            job = self.store.create_life_assistant_job(
                {"intent": "明天提醒我检查采购清单", "client_request_id": "usage-timeout-request"},
                DEMO_USER,
            )
        self.assertEqual(job["status"], "completed")
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM ai_usage_events WHERE request_id LIKE 'assistant_job_%'").fetchone()
        self.assertEqual(row["status"], "timeout")
        self.assertEqual(row["failure_code"], "transport_timeout")
        self.assertNotIn("secret timeout detail", json.dumps(dict(row)))

    def test_life_assistant_invalid_json_keeps_provider_usage_and_failure_code(self) -> None:
        response = {
            "choices": [{"message": {"content": "not-json"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }

        class ModelResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self) -> bytes:
                return json.dumps(response).encode()

        self.store.life_assistant_provider = OpenAICompatibleLifeAssistant(
            "not-a-real-key", "https://example.test/v1", "test-model", 1
        )
        with mock.patch("love_moments.life_assistant.request.urlopen", return_value=ModelResponse()):
            job = self.store.create_life_assistant_job(
                {"intent": "明天提醒我检查采购清单", "client_request_id": "usage-invalid-json"},
                DEMO_USER,
            )
        self.assertEqual(job["status"], "completed")
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM ai_usage_events WHERE failure_code='invalid_json'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["input_tokens"], 10)
        self.assertEqual(row["total_tokens"], 12)

    def test_provenance_is_allowlisted_and_does_not_store_secret_evidence(self) -> None:
        row = self.store.record_provenance(
            "moment", "moment-1", user_id=DEMO_USER, source_type="human",
            date_source="user_confirmed",
            evidence={"provider": "manual", "evidence_at": "2026-10-27T00:00:00+00:00", "api_key": "must-not-leak"},
        )
        encoded = json.dumps(row, ensure_ascii=False)
        self.assertNotIn("must-not-leak", encoded)
        self.assertEqual(row["source_type"], "human")
        with self.assertRaises(ValueError):
            self.store.record_provenance("arbitrary_table", "x", user_id=DEMO_USER)

    def test_provenance_rejects_partner_forgery_and_reliable_evidence_downgrade(self) -> None:
        with self.store.connection() as db:
            couple = db.execute("SELECT id FROM couples WHERE status='active' LIMIT 1").fetchone()["id"]
            db.execute(
                """INSERT INTO moments(
                    id,couple_id,creator_id,type,title,body,occurred_at,visibility,version,created_at
                ) VALUES ('private-provenance',?,?,'note','私密','秘密',?,'private',2,?)""",
                (couple, DEMO_USER, datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()),
            )
        with self.assertRaises(PermissionError):
            self.store.record_provenance(
                "moment", "private-provenance", user_id=DEMO_PARTNER,
                source_type="human", evidence={"resource_version": 2, "confidence": 1.0},
            )
        original = self.store.record_provenance(
            "moment", "private-provenance", user_id=DEMO_USER,
            source_type="human", date_source="user_confirmed",
            evidence={"resource_version": 2, "confidence": 1.0},
        )
        with self.assertRaises(ConflictError):
            self.store.record_provenance(
                "moment", "private-provenance", user_id=DEMO_USER,
                source_type="legacy_unknown", date_source="legacy_unknown",
                evidence={"resource_version": 2, "confidence": 0.1},
            )
        with self.store.connection() as db:
            stored = db.execute("SELECT source_type,date_source,evidence_json FROM memory_provenance WHERE id=?", (original["id"],)).fetchone()
        self.assertEqual(stored["source_type"], "human")
        self.assertEqual(stored["date_source"], "user_confirmed")
        self.assertEqual(json.loads(stored["evidence_json"])["resource_version"], 2)

    def test_wishlist_provenance_uses_parent_scope_and_receipt_rejects_forged_resource(self) -> None:
        with self.store.connection() as db:
            item = db.execute(
                "SELECT i.id FROM shared_list_items i JOIN shared_lists l ON l.id=i.list_id WHERE l.couple_id=(SELECT id FROM couples LIMIT 1) LIMIT 1"
            ).fetchone()
        row = self.store.record_provenance(
            "shared_list_item", item["id"], user_id=DEMO_USER, source_type="human"
        )
        self.assertEqual(row["shared_list_item_id"], item["id"])
        with self.store.connection() as db:
            with self.assertRaises(KeyError):
                self.store.create_mutation_receipt(
                    db, actor_id=DEMO_USER, resource_type="calendar_entry", resource_id="missing-resource",
                    operation="calendar.complete", inverse={"completed_at": None}, expected_post_version=1,
                    client_request_id="receipt-forged-resource",
                )

    def test_new_ai_context_grant_is_request_bound_and_versioned(self) -> None:
        with self.store.connection() as db:
            context, _ = self.store._ui_context(db, "timeline", DEMO_USER, ["moment:moment-1"])
            grant = db.execute(
                "SELECT * FROM ai_context_grants WHERE user_id=? AND resource_id='moment-1' ORDER BY created_at DESC LIMIT 1",
                (DEMO_USER,),
            ).fetchone()
        self.assertEqual(len(context["authorizedContent"]), 1)
        self.assertEqual(grant["usage_scope"], "selected_content")
        self.assertEqual(grant["resource_version"], 1)
        self.assertTrue(grant["request_id"].startswith("grant_request_"))
        self.assertIsNone(grant["revoked_at"])

    def test_ai_context_grant_is_revalidated_immediately_before_dispatch(self) -> None:
        with self.store.connection() as db:
            context, _ = self.store._ui_context(db, "timeline", DEMO_USER, ["moment:moment-1"])
            db.execute("UPDATE moments SET version=version+1 WHERE id='moment-1'")
        with self.assertRaises(ConflictError):
            self.store._consume_ai_context_grants(context["grantRequestId"], DEMO_USER, 1)
        with self.store.connection() as db:
            stale = db.execute("SELECT last_used_at FROM ai_context_grants WHERE request_id=?", (context["grantRequestId"],)).fetchone()
        self.assertIsNone(stale["last_used_at"])

        with self.store.connection() as db:
            fresh, _ = self.store._ui_context(db, "timeline", DEMO_USER, ["moment:moment-1"])
        self.store._consume_ai_context_grants(fresh["grantRequestId"], DEMO_USER, 1)
        with self.store.connection() as db:
            used = db.execute("SELECT last_used_at FROM ai_context_grants WHERE request_id=?", (fresh["grantRequestId"],)).fetchone()
        self.assertIsNotNone(used["last_used_at"])

    def test_undo_is_actor_scoped_idempotent_and_rejects_concurrent_version(self) -> None:
        with self.store.connection() as db:
            couple = db.execute("SELECT id FROM couples LIMIT 1").fetchone()["id"]
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            db.execute(
                "INSERT INTO calendar_entries(id,couple_id,creator_id,entry_type,title,starts_at,version,created_at,updated_at) VALUES ('undo-calendar',?,?,'todo','测试',?,1,?,?)",
                (couple, DEMO_USER, now, now, now),
            )
            db.execute("UPDATE calendar_entries SET completed_at=?,version=2 WHERE id='undo-calendar'", (now,))
            receipt = self.store.create_mutation_receipt(
                db, actor_id=DEMO_USER, resource_type="calendar_entry", resource_id="undo-calendar",
                operation="calendar.complete", inverse={"completed_at": None}, expected_post_version=2,
                client_request_id="receipt-request-1",
            )
        self.assertEqual(set(receipt), {"receipt_id", "expires_at"})
        undone = self.store.undo_mutation(receipt["receipt_id"], DEMO_USER)
        self.assertTrue(undone["undone"])
        replay = self.store.undo_mutation(receipt["receipt_id"], DEMO_USER)
        self.assertTrue(replay["idempotent"])
        with self.store.connection() as db:
            row = db.execute("SELECT completed_at,version FROM calendar_entries WHERE id='undo-calendar'").fetchone()
            self.assertIsNone(row["completed_at"])
            self.assertEqual(row["version"], 3)
            self.assertNotIn("inverse", json.dumps(undone))

            db.execute("UPDATE calendar_entries SET completed_at=?,version=4 WHERE id='undo-calendar'", (now,))
            second = self.store.create_mutation_receipt(
                db, actor_id=DEMO_USER, resource_type="calendar_entry", resource_id="undo-calendar",
                operation="calendar.complete", inverse={"completed_at": None}, expected_post_version=4,
                client_request_id="receipt-request-2",
            )
            db.execute("UPDATE calendar_entries SET title='伴侣继续修改',version=5 WHERE id='undo-calendar'")
        with self.assertRaises(ConflictError):
            self.store.undo_mutation(second["receipt_id"], DEMO_USER)
        with self.assertRaises(PermissionError):
            self.store.undo_mutation(second["receipt_id"], DEMO_PARTNER)

    def test_receipt_exact_retry_precedes_version_check_and_mismatch_conflicts(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with self.store.connection() as db:
            couple = db.execute("SELECT id FROM couples WHERE status='active' LIMIT 1").fetchone()["id"]
            db.execute(
                "INSERT INTO calendar_entries(id,couple_id,creator_id,entry_type,title,starts_at,completed_at,version,created_at,updated_at) VALUES ('retry-calendar',?,?,'todo','重试',?,?,2,?,?)",
                (couple, DEMO_USER, now, now, now, now),
            )
            first = self.store.create_mutation_receipt(
                db, actor_id=DEMO_USER, resource_type="calendar_entry", resource_id="retry-calendar",
                operation="calendar.complete", inverse={"completed_at": None}, expected_post_version=2,
                client_request_id="same-request",
            )
            db.execute("UPDATE calendar_entries SET version=3 WHERE id='retry-calendar'")
            retried = self.store.create_mutation_receipt(
                db, actor_id=DEMO_USER, resource_type="calendar_entry", resource_id="retry-calendar",
                operation="calendar.complete", inverse={"completed_at": None}, expected_post_version=2,
                client_request_id="same-request",
            )
            self.assertEqual(retried, first)
            with self.assertRaises(ConflictError):
                self.store.create_mutation_receipt(
                    db, actor_id=DEMO_USER, resource_type="calendar_entry", resource_id="retry-calendar",
                    operation="calendar.complete", inverse={"completed_at": now}, expected_post_version=2,
                    client_request_id="same-request",
                )

    def test_receipt_private_media_permissions_and_inverse_datetime_are_rechecked(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with self.store.connection() as db:
            couple = db.execute("SELECT id FROM couples WHERE status='active' LIMIT 1").fetchone()["id"]
            db.execute(
                """INSERT INTO media_assets(
                    id,couple_id,owner_id,kind,original_name,mime_type,relative_path,size_bytes,
                    visibility,status,deleted_at,version,created_at
                ) VALUES ('private-receipt-media',?,?,'image','x.jpg','image/jpeg','images/private-receipt.jpg',1,
                          'private','trashed',?,2,?)""",
                (couple, DEMO_USER, now, now),
            )
            with self.assertRaises(PermissionError):
                self.store.create_mutation_receipt(
                    db, actor_id=DEMO_PARTNER, resource_type="media", resource_id="private-receipt-media",
                    operation="media.trash", inverse={"status": "ready", "deleted_at": None},
                    expected_post_version=2, client_request_id="partner-private-media",
                )
            with self.assertRaises(ValueError):
                self.store.create_mutation_receipt(
                    db, actor_id=DEMO_USER, resource_type="media", resource_id="private-receipt-media",
                    operation="media.trash", inverse={"status": "ready", "deleted_at": "NOT-A-DATETIME"},
                    expected_post_version=2, client_request_id="bad-datetime",
                )
            forged_id = "receipt-forged-private-media"
            db.execute(
                """INSERT INTO mutation_receipts(
                    id,couple_id,actor_id,resource_type,resource_id,operation,inverse_json,
                    expected_post_version,client_request_id,created_at,expires_at
                ) VALUES (?,?,?,'media','private-receipt-media','media.trash',?,2,'forged',?,?)""",
                (forged_id, couple, DEMO_PARTNER, json.dumps({"status": "ready", "deleted_at": None}), now,
                 (datetime.now(timezone.utc) + timedelta(minutes=10)).replace(microsecond=0).isoformat()),
            )
        with self.assertRaises(PermissionError):
            self.store.undo_mutation(forged_id, DEMO_PARTNER)

    def test_receipt_idempotent_replay_rechecks_private_media_authorization(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with self.store.connection() as db:
            couple = db.execute("SELECT id FROM couples WHERE status='active' LIMIT 1").fetchone()["id"]
            db.execute(
                """INSERT INTO media_assets(
                    id,couple_id,owner_id,kind,original_name,mime_type,relative_path,size_bytes,
                    visibility,status,deleted_at,version,created_at
                ) VALUES ('replay-media',?,?,'image','x.jpg','image/jpeg','images/replay-media.jpg',1,
                          'shared','trashed',?,2,?)""",
                (couple, DEMO_USER, now, now),
            )
            self.store.create_mutation_receipt(
                db, actor_id=DEMO_PARTNER, resource_type="media", resource_id="replay-media",
                operation="media.trash", inverse={"status": "ready", "deleted_at": None},
                expected_post_version=2, client_request_id="replay-permission",
            )
            db.execute("UPDATE media_assets SET visibility='private' WHERE id='replay-media'")
            with self.assertRaises(PermissionError):
                self.store.create_mutation_receipt(
                    db, actor_id=DEMO_PARTNER, resource_type="media", resource_id="replay-media",
                    operation="media.trash", inverse={"status": "ready", "deleted_at": None},
                    expected_post_version=2, client_request_id="replay-permission",
                )

    def test_undo_invalidates_and_scrubs_each_persisted_corrupt_inverse_before_conflict(self) -> None:
        corrupt_payloads = (
            ("invalid-json", "not-json"),
            ("invalid-fields", json.dumps({"unexpected": None})),
            ("invalid-datetime", json.dumps({"completed_at": "NOT-A-DATETIME"})),
        )
        receipt_ids: list[str] = []
        with self.store.connection() as db:
            couple = db.execute("SELECT id FROM couples WHERE status='active' LIMIT 1").fetchone()["id"]
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            db.execute(
                """INSERT INTO calendar_entries(
                    id,couple_id,creator_id,entry_type,title,starts_at,completed_at,version,created_at,updated_at
                ) VALUES ('corrupt-inverse-calendar',?,?,'todo','损坏回执测试',?,?,2,?,?)""",
                (couple, DEMO_USER, now, now, now, now),
            )
            for suffix, _ in corrupt_payloads:
                receipt = self.store.create_mutation_receipt(
                    db, actor_id=DEMO_USER, resource_type="calendar_entry",
                    resource_id="corrupt-inverse-calendar", operation="calendar.complete",
                    inverse={"completed_at": None}, expected_post_version=2,
                    client_request_id=f"corrupt-inverse-{suffix}",
                )
                receipt_ids.append(receipt["receipt_id"])
        with sqlite3.connect(self.store.db_path) as db:
            db.execute("PRAGMA ignore_check_constraints=ON")
            for receipt_id, (_, payload) in zip(receipt_ids, corrupt_payloads):
                db.execute(
                    "UPDATE mutation_receipts SET inverse_json=? WHERE id=?",
                    (payload, receipt_id),
                )
        for receipt_id in receipt_ids:
            with self.assertRaises(ConflictError):
                self.store.undo_mutation(receipt_id, DEMO_USER)
        with self.store.connection() as db:
            placeholders = ",".join("?" for _ in receipt_ids)
            rows = db.execute(
                f"SELECT status,inverse_json FROM mutation_receipts WHERE id IN ({placeholders})",
                receipt_ids,
            ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["status"] == "invalidated" for row in rows))
        self.assertTrue(all(row["inverse_json"] == "{}" for row in rows))

    def test_today_priorities_are_capped_linked_and_do_not_copy_private_body(self) -> None:
        with self.store.connection() as db:
            couple = db.execute("SELECT id FROM couples LIMIT 1").fetchone()["id"]
            now = datetime.now(timezone.utc).replace(microsecond=0)
            for index in range(5):
                db.execute(
                    "INSERT INTO calendar_entries(id,couple_id,creator_id,entry_type,title,note,starts_at,version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (f"priority-{index}", couple, DEMO_USER, "todo", f"待办 {index}", "PRIVATE-BODY", (now + timedelta(hours=index)).isoformat(), 1, now.isoformat(), now.isoformat()),
                )
        values = self.store.get_today_priorities(DEMO_USER)
        self.assertLessEqual(len(values), 3)
        self.assertTrue(all(item["url"].startswith("/?") for item in values))
        self.assertNotIn("PRIVATE-BODY", json.dumps(values, ensure_ascii=False))

    def test_today_priorities_take_one_from_each_available_category_before_filling(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        with self.store.connection() as db:
            couple = db.execute("SELECT id FROM couples WHERE status='active' LIMIT 1").fetchone()["id"]
            for index in range(4):
                db.execute(
                    """INSERT INTO life_assistant_plans(
                        id,couple_id,creator_id,title,summary,status,version,source,request_hash,
                        expected_versions_json,expires_at,created_at,updated_at
                    ) VALUES (?,?,?,?,?,'pending_confirmation',1,'rules',?,'{}',?,?,?)""",
                    (f"fair-plan-{index}", couple, DEMO_USER, f"方案 {index}", "安全摘要", f"hash-{index}",
                     (now + timedelta(days=1)).isoformat(), now.isoformat(), (now + timedelta(seconds=index)).isoformat()),
                )
            db.execute(
                """INSERT INTO in_app_notifications(
                    id,couple_id,recipient_id,event_type,resource_type,resource_id,title,body,url,dedupe_key,created_at
                ) VALUES ('fair-notification',?,?,'test','test','test','通知','仅本人','/?screen=home','fair-notification',?)""",
                (couple, DEMO_USER, now.isoformat()),
            )
            db.execute(
                """INSERT INTO calendar_entries(
                    id,couple_id,creator_id,entry_type,title,starts_at,version,created_at,updated_at
                ) VALUES ('fair-todo',?,?,'todo','共同待办',?,1,?,?)""",
                (couple, DEMO_USER, now.isoformat(), now.isoformat(), now.isoformat()),
            )
        priorities = self.store.get_today_priorities(DEMO_USER)
        self.assertEqual(len(priorities), 3)
        self.assertEqual({item["type"] for item in priorities}, {"assistant_plan", "notification", "todo"})

    def test_sensitive_foundation_export_filters_owner_visibility_and_sealed_recipient(self) -> None:
        self._insert_sensitive_foundation_rows()
        archive, _ = self.store.account_export(DEMO_USER)
        with zipfile.ZipFile(io.BytesIO(archive)) as exported:
            payload = json.loads(exported.read("data.json"))
        emergency_ids = {row["id"] for row in payload["trip_emergency_entries"]}
        transcript_ids = {row["id"] for row in payload["voice_transcripts"]}
        letter_ids = {row["id"] for row in payload["future_letters"]}
        self.assertEqual(emergency_ids, {"emergency-own", "emergency-own-shared", "emergency-shared"})
        self.assertEqual(transcript_ids, {"transcript-voice-own", "transcript-voice-shared"})
        self.assertEqual(letter_ids, {"letter-own", "letter-delivered"})
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("PARTNER-SEALED", encoded)
        self.assertNotIn("BODY-voice-partner", encoded)

    def test_emergency_export_uses_the_authorized_public_serializer(self) -> None:
        context = self._insert_sensitive_foundation_rows()
        secret_details = {
            "contact_phone": "13800009999",
            "insurance_policy": "POLICY-TOP-SECRET",
            "medical_note": "MEDICAL-TOP-SECRET",
            "ciphertext": "CIPHER-TOP-SECRET",
        }
        with self.store.connection() as db:
            db.execute(
                """UPDATE trip_emergency_entries
                   SET title='妈妈 13800009999',details_json=?,client_request_id='internal-secret',version=37
                   WHERE id='emergency-own'""",
                (json.dumps(secret_details, ensure_ascii=False),),
            )
            rows = self.store.export_foundation_lifecycle_rows(
                db, couple_id=context["couple_id"], user_id=DEMO_USER
            )["trip_emergency_entries"]
        own = next(row for row in rows if row["id"] == "emergency-own")
        self.assertEqual(
            set(own),
            {
                "id", "entry_type", "title", "details", "visibility", "visibility_label",
                "source_type", "version", "created_at", "updated_at", "editable_by_me",
            },
        )
        self.assertEqual(own["title"], "妈妈 13800009999")
        self.assertEqual(own["details"], {})
        self.assertTrue(own["editable_by_me"])
        encoded = json.dumps(rows, ensure_ascii=False)
        for secret in ("POLICY-TOP-SECRET", "MEDICAL-TOP-SECRET", "CIPHER-TOP-SECRET", "internal-secret"):
            self.assertNotIn(secret, encoded)

    def test_unlink_stops_sealed_delivery_and_invalidates_foundation_scope(self) -> None:
        self._insert_sensitive_foundation_rows()
        result = self.store.unlink_couple("test123", "解除情侣", DEMO_USER)
        self.assertTrue(result["unlinked"])
        with self.store.connection() as db:
            letter = db.execute("SELECT status,deliver_at,sealed_at,body FROM future_letters WHERE id='letter-own'").fetchone()
            self.assertEqual(letter["status"], "draft")
            self.assertIsNone(letter["deliver_at"])
            self.assertIsNone(letter["sealed_at"])
            self.assertEqual(letter["body"], "OWN-SEALED")

    def test_account_delete_scrubs_owned_private_foundation_content_but_keeps_shared_history(self) -> None:
        self._insert_sensitive_foundation_rows()
        result = self.store.delete_account("test123", "注销账号", DEMO_USER)
        self.assertTrue(result["deleted"])
        with self.store.connection() as db:
            private_emergency = db.execute("SELECT title,details_json FROM trip_emergency_entries WHERE id='emergency-own'").fetchone()
            shared_emergency = db.execute("SELECT title,details_json,deleted_at FROM trip_emergency_entries WHERE id='emergency-own-shared'").fetchone()
            own_transcript = db.execute("SELECT body,status FROM voice_transcripts WHERE id='transcript-voice-own'").fetchone()
            shared_transcript = db.execute("SELECT body,status FROM voice_transcripts WHERE id='transcript-voice-shared'").fetchone()
            letter = db.execute("SELECT title,body,status,deleted_at FROM future_letters WHERE id='letter-own'").fetchone()
        self.assertEqual(json.loads(private_emergency["details_json"]), {})
        self.assertEqual(shared_emergency["title"], "emergency-own-shared")
        self.assertEqual(json.loads(shared_emergency["details_json"]), {"secret": "emergency-own-shared"})
        self.assertIsNone(shared_emergency["deleted_at"])
        self.assertEqual(own_transcript["body"], "")
        self.assertEqual(own_transcript["status"], "failed")
        self.assertEqual(shared_transcript["body"], "BODY-voice-shared")
        self.assertEqual((letter["title"], letter["body"], letter["status"]), ("", "", "deleted"))
        self.assertIsNotNone(letter["deleted_at"])

    def test_delete_after_unlink_scrubs_private_rows_in_historical_couple(self) -> None:
        self._insert_sensitive_foundation_rows()
        self.store.unlink_couple("test123", "解除情侣", DEMO_USER)
        self.store.delete_account("test123", "注销账号", DEMO_USER)
        with self.store.connection() as db:
            emergency = db.execute(
                "SELECT title,details_json,deleted_at FROM trip_emergency_entries WHERE id='emergency-own'"
            ).fetchone()
            transcript = db.execute(
                "SELECT body,status FROM voice_transcripts WHERE id='transcript-voice-own'"
            ).fetchone()
            letter = db.execute(
                "SELECT title,body,status,deleted_at FROM future_letters WHERE id='letter-own'"
            ).fetchone()
            shared = db.execute(
                "SELECT title,details_json,deleted_at FROM trip_emergency_entries WHERE id='emergency-own-shared'"
            ).fetchone()
        self.assertEqual(
            (emergency["title"], json.loads(emergency["details_json"])),
            ("已删除的应急信息", {}),
        )
        self.assertIsNotNone(emergency["deleted_at"])
        self.assertEqual((transcript["body"], transcript["status"]), ("", "failed"))
        self.assertEqual((letter["title"], letter["body"], letter["status"]), ("", "", "deleted"))
        self.assertIsNotNone(letter["deleted_at"])
        self.assertEqual(shared["title"], "emergency-own-shared")
        self.assertEqual(json.loads(shared["details_json"]), {"secret": "emergency-own-shared"})
        self.assertIsNone(shared["deleted_at"])

    def test_direct_delete_revokes_partner_grants_and_scrubs_partner_receipts(self) -> None:
        with self.store.connection() as db:
            couple = db.execute("SELECT * FROM couples WHERE status='active' LIMIT 1").fetchone()
            now = datetime.now(timezone.utc).replace(microsecond=0)
            db.execute(
                """INSERT INTO calendar_entries(
                    id,couple_id,creator_id,entry_type,title,starts_at,completed_at,version,created_at,updated_at
                ) VALUES ('partner-delete-calendar',?,?,'todo','伴侣撤销测试',?,?,2,?,?)""",
                (couple["id"], DEMO_PARTNER, now.isoformat(), now.isoformat(), now.isoformat(), now.isoformat()),
            )
            db.execute(
                """INSERT INTO ai_context_grants(
                    id,couple_id,user_id,surface,resource_type,resource_id,purpose,expires_at,created_at,
                    usage_scope,request_id,resource_version,version
                ) VALUES ('partner-delete-grant',?,?,'home','calendar_entry','partner-delete-calendar',
                          'ui.compose',?,?,'selected_content','partner-delete-request',2,1)""",
                (couple["id"], DEMO_PARTNER, (now + timedelta(hours=1)).isoformat(), now.isoformat()),
            )
            receipt = self.store.create_mutation_receipt(
                db, actor_id=DEMO_PARTNER, resource_type="calendar_entry",
                resource_id="partner-delete-calendar", operation="calendar.complete",
                inverse={"completed_at": None}, expected_post_version=2,
                client_request_id="partner-delete-receipt",
            )
        self.store.delete_account("test123", "注销账号", DEMO_USER)
        with self.store.connection() as db:
            grant = db.execute(
                "SELECT revoked_at FROM ai_context_grants WHERE id='partner-delete-grant'"
            ).fetchone()
            stored_receipt = db.execute(
                "SELECT status,inverse_json FROM mutation_receipts WHERE id=?",
                (receipt["receipt_id"],),
            ).fetchone()
        self.assertIsNotNone(grant["revoked_at"])
        self.assertEqual(stored_receipt["status"], "invalidated")
        self.assertEqual(stored_receipt["inverse_json"], "{}")

    def test_ai_telemetry_failure_is_redacted_and_stale_started_becomes_unknown(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0).isoformat()
        with self.store.connection() as db:
            couple = db.execute("SELECT id FROM couples WHERE status='active' LIMIT 1").fetchone()["id"]
            db.execute(
                """INSERT INTO ai_usage_events(
                    id,couple_id,user_id,actor_kind,feature,provider,model,request_id,attempt,
                    status,request_sent,started_at
                ) VALUES ('stale-usage',?,?, 'user','life_assistant','provider','model','stale-request',1,'started',1,?)""",
                (couple, DEMO_USER, old),
            )
        recovered = self.store.recover_stale_ai_usage_events(max_age_seconds=60)
        self.assertEqual(recovered, 1)
        with self.store.connection() as db:
            stale = db.execute("SELECT status,failure_code,finished_at FROM ai_usage_events WHERE id='stale-usage'").fetchone()
        self.assertEqual(stale["status"], "unknown")
        self.assertEqual(stale["failure_code"], "process_interrupted")
        self.assertIsNotNone(stale["finished_at"])

        response = {"choices": [{"message": {"content": json.dumps({
            "requested_date": "2099-10-16", "meal_slot": "dinner", "planned_time": "18:30",
            "dish_query": "秘密菜名", "task_query": "", "add_calendar": True,
            "add_shopping": False, "add_todo": False,
        }, ensure_ascii=False)}}]}

        class ModelResponse:
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def read(self): return json.dumps(response, ensure_ascii=False).encode()

        self.store.life_assistant_provider = OpenAICompatibleLifeAssistant(
            "not-a-real-key", "https://example.test/v1", "test-model", 1
        )
        with mock.patch.object(self.store, "record_ai_usage_event", side_effect=RuntimeError("TOP-SECRET-FAILURE")):
            with mock.patch("love_moments.life_assistant.request.urlopen", return_value=ModelResponse()):
                self.store.create_life_assistant_job(
                    {"intent": "秘密菜名", "client_request_id": "telemetry-observer-failure"}, DEMO_USER,
                )
        with self.store.connection() as db:
            event = db.execute("SELECT summary,detail_json FROM system_events WHERE event_key='ai-telemetry:persistence-failed'").fetchone()
        self.assertIsNotNone(event)
        self.assertNotIn("TOP-SECRET-FAILURE", event["detail_json"])
        self.assertNotIn("秘密菜名", event["detail_json"])

    def test_account_export_excludes_opaque_inverse_payload(self) -> None:
        with self.store.connection() as db:
            couple = db.execute("SELECT id FROM couples LIMIT 1").fetchone()["id"]
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            db.execute(
                "INSERT INTO calendar_entries(id,couple_id,creator_id,entry_type,title,starts_at,completed_at,version,created_at,updated_at) VALUES ('export-calendar',?,?,'todo','导出测试',?,?,2,?,?)",
                (couple, DEMO_USER, now, now, now, now),
            )
            row = db.execute("SELECT id,version FROM calendar_entries WHERE id='export-calendar'").fetchone()
            self.store.create_mutation_receipt(
                db, actor_id=DEMO_USER, resource_type="calendar_entry", resource_id=row["id"],
                operation="calendar.complete", inverse={"completed_at": "2099-12-31T23:59:59+00:00"},
                expected_post_version=row["version"], client_request_id="receipt-export-test",
            )
        archive, _ = self.store.account_export(DEMO_USER)
        with zipfile.ZipFile(io.BytesIO(archive)) as exported:
            data_json = exported.read("data.json").decode("utf-8")
        self.assertNotIn("2099-12-31T23:59:59+00:00", data_json)
        self.assertNotIn("inverse_json", data_json)


if __name__ == "__main__":
    unittest.main()
