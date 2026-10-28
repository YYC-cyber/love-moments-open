from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from love_moments.intelligent_ui import ConflictError
from love_moments.store import DEMO_PARTNER, DEMO_USER, LoveMomentsStore


class PrivacyCenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LoveMomentsStore(Path(self.temp.name) / "privacy.db", seed_demo_data=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _grant(self, grant_id: str, owner: str, *, expires_in_hours: int = 1) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        with self.store.connection() as db:
            couple_id = db.execute("SELECT id FROM couples WHERE status='active'").fetchone()["id"]
            db.execute(
                """
                INSERT INTO ai_context_grants(
                    id,couple_id,user_id,surface,resource_type,resource_id,purpose,
                    expires_at,created_at,resource_version,usage_scope,request_id,version
                ) VALUES (?,?,?,'timeline','moment','moment-1','ui.compose',?,?,1,
                          'selected_content',?,1)
                """,
                (
                    grant_id, couple_id, owner,
                    (now + timedelta(hours=expires_in_hours)).isoformat(), now.isoformat(),
                    f"internal-request-{grant_id}",
                ),
            )

    def test_usage_is_monthly_personal_aggregate_without_request_content(self) -> None:
        self.store.record_ai_usage_event(
            actor_kind="user", user_id=DEMO_USER, feature="trip_plan",
            provider="private-provider", model="private-model", request_id="private-request-user",
            status="succeeded", request_sent=True, input_tokens=10, output_tokens=4,
            total_tokens=14, estimated_cost=0.0125, cost_currency="CNY", pricing_version="v1",
        )
        self.store.record_ai_usage_event(
            actor_kind="user", user_id=DEMO_USER, feature="trip_replan",
            provider="private-provider", model="private-model", request_id="unknown-price",
            status="timeout", request_sent=True, total_tokens=3,
        )
        self.store.record_ai_usage_event(
            actor_kind="user", user_id=DEMO_PARTNER, feature="life_assistant",
            provider="partner-provider", model="partner-model", request_id="partner-private-request",
            status="succeeded", request_sent=True, total_tokens=999,
        )
        result = self.store.get_privacy_center(DEMO_USER)
        self.assertEqual(result["usage"]["attempts"], 2)
        self.assertEqual(result["usage"]["sent_attempts"], 2)
        self.assertEqual(result["usage"]["total_tokens"], 17)
        self.assertEqual(result["usage"]["unknown_cost_attempts"], 1)
        self.assertEqual(result["usage"]["feature_counts"], {"trip_plan": 1, "trip_replan": 1})
        self.assertEqual(result["usage"]["costs"], [{"currency": "CNY", "amount": 0.0125}])
        serialized = str(result)
        for secret in ("private-request-user", "partner-private-request", "private-provider", "private-model"):
            self.assertNotIn(secret, serialized)
        for forbidden in ("prompt", "response", "request_id"):
            self.assertNotIn(forbidden, result)

    def test_grants_only_include_current_user_allowlisted_metadata(self) -> None:
        self._grant("grant-own-active", DEMO_USER)
        self._grant("grant-own-expired", DEMO_USER, expires_in_hours=-1)
        self._grant("grant-partner", DEMO_PARTNER)
        result = self.store.get_privacy_center(DEMO_USER)
        self.assertEqual({item["id"] for item in result["grants"]}, {"grant-own-active", "grant-own-expired"})
        statuses = {item["id"]: item["status"] for item in result["grants"]}
        self.assertEqual(statuses, {"grant-own-active": "active", "grant-own-expired": "expired"})
        allowed = {
            "id", "surface", "resource_type", "resource_id", "purpose", "usage_scope",
            "expires_at", "created_at", "last_used_at", "revoked_at", "version", "status",
        }
        self.assertTrue(all(set(item) == allowed for item in result["grants"]))
        self.assertNotIn("internal-request", str(result))

    def test_revoke_is_owner_only_versioned_and_idempotent(self) -> None:
        self._grant("grant-revoke", DEMO_USER)
        with self.assertRaises(PermissionError):
            self.store.revoke_ai_context_grant("grant-revoke", 1, DEMO_PARTNER)
        with self.assertRaises(ConflictError):
            self.store.revoke_ai_context_grant("grant-revoke", 2, DEMO_USER)
        first = self.store.revoke_ai_context_grant("grant-revoke", 1, DEMO_USER)
        self.assertEqual((first["status"], first["version"], first["idempotent"]), ("revoked", 2, False))
        replay = self.store.revoke_ai_context_grant("grant-revoke", 1, DEMO_USER)
        self.assertEqual((replay["status"], replay["version"], replay["idempotent"]), ("revoked", 2, True))
        self.assertIn("无法收回", replay["notice"])
        with self.store.connection() as db:
            stored = db.execute("SELECT revoked_at,version FROM ai_context_grants WHERE id='grant-revoke'").fetchone()
        self.assertIsNotNone(stored["revoked_at"])
        self.assertEqual(stored["version"], 2)


if __name__ == "__main__":
    unittest.main()
