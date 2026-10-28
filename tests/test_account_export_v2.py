from __future__ import annotations

import io
import json
import base64
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from love_moments.schema import (
    ACCOUNT_EXPORT_POLICIES,
    ACCOUNT_EXPORT_STRATEGIES,
    REQUIRED_TABLES_V34,
    account_export_policy_status,
)
from love_moments.store import DEMO_PARTNER, DEMO_USER, LoveMomentsStore


class AccountExportV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LoveMomentsStore(
            Path(self.temp.name) / "export.db",
            media_root=Path(self.temp.name) / "media",
            seed_demo_data=True,
        )

    @staticmethod
    def _future(hours: int = 4) -> str:
        return (
            datetime.now(timezone.utc) + timedelta(hours=hours)
        ).replace(microsecond=0).isoformat()

    def _export(self, user_id: str) -> tuple[dict, dict, dict, set[str]]:
        content, _ = self.store.account_export(user_id)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            data = json.loads(archive.read("data.json"))
            manifest = json.loads(archive.read("manifest.json"))
            media_status = json.loads(archive.read("media-status.json"))
            names = set(archive.namelist())
        return data, manifest, media_status, names

    def test_policy_registry_covers_every_v34_table_once(self) -> None:
        status = account_export_policy_status()
        self.assertTrue(status["ok"])
        self.assertEqual(set(ACCOUNT_EXPORT_POLICIES), REQUIRED_TABLES_V34)
        self.assertEqual(status["covered"], len(REQUIRED_TABLES_V34))
        self.assertEqual(status["required"], len(REQUIRED_TABLES_V34))
        self.assertEqual(status["missing"], [])
        self.assertEqual(status["unexpected"], [])
        self.assertEqual(status["invalid"], [])
        self.assertTrue(all(
            policy["strategy"] in ACCOUNT_EXPORT_STRATEGIES
            and policy["handler"] and policy["reason"]
            for policy in ACCOUNT_EXPORT_POLICIES.values()
        ))

    def test_manifest_v2_counts_real_datasets_and_never_exports_operations(self) -> None:
        media = self.store.save_media(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            ),
            "export.png", "image/png", user_id=DEMO_USER,
        )
        data, manifest, media_status, names = self._export(DEMO_USER)

        self.assertEqual(manifest["schema_version"], 2)
        self.assertTrue(manifest["export_policy_coverage"]["ok"])
        self.assertEqual(
            manifest["export_policy_coverage"]["covered"], len(REQUIRED_TABLES_V34),
        )
        for key, value in data.items():
            if isinstance(value, list):
                self.assertEqual(manifest["dataset_counts"][key], len(value), key)
        self.assertEqual(media_status["authorized_media_count"], manifest["media_count"])
        self.assertEqual(media_status["included_media_count"], manifest["included_media_count"])
        self.assertEqual(media_status["missing_media_count"], manifest["missing_media_count"])
        self.assertTrue(any(name.startswith(f"media/{media['id']}-") for name in names))

        operational = {
            table for table, policy in ACCOUNT_EXPORT_POLICIES.items()
            if policy["strategy"] == "operational_excluded"
        }
        self.assertFalse(operational.intersection(data))
        self.assertTrue(operational.issubset(data["export_exclusions"]))
        represented_tables = set(data) | set(data["export_exclusions"]) | {
            "users", "couples", "sessions", "ai_usage_events",
        }
        self.assertEqual(REQUIRED_TABLES_V34 - represented_tables, set())
        encoded = json.dumps({"data": data, "manifest": manifest}, ensure_ascii=False)
        for forbidden in (
            "password_hash", "token_hash", "subscription_ciphertext", "storage_key",
            "relative_path", "thumbnail_path", "payload_hash", "request_hash",
            "/Users/", "\\Users\\",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_unrevealed_votes_are_owner_only_then_shared_after_reveal(self) -> None:
        decision = self.store.create_decision({
            "title": "周末去哪",
            "selection_mode": "single",
            "options": ["公园", "展览"],
            "closes_at": self._future(),
        }, DEMO_USER)
        first_option = decision["options"][0]["id"]
        second_option = decision["options"][1]["id"]
        self.store.submit_decision_vote(
            decision["id"], [first_option], 1, "export-owner-vote-0001", DEMO_USER,
        )

        owner, _, _, _ = self._export(DEMO_USER)
        partner, _, _, _ = self._export(DEMO_PARTNER)
        self.assertEqual(
            [row["option_id"] for row in owner["decision_votes"] if row["decision_id"] == decision["id"]],
            [first_option],
        )
        self.assertEqual(
            [row for row in partner["decision_votes"] if row["decision_id"] == decision["id"]],
            [],
        )
        partner_decision = next(row for row in partner["decisions"] if row["id"] == decision["id"])
        self.assertNotIn("votes", partner_decision)
        self.assertNotIn("resolution", partner_decision)

        self.store.submit_decision_vote(
            decision["id"], [second_option], 1, "export-partner-vote-0001", DEMO_PARTNER,
        )
        owner_after, _, _, _ = self._export(DEMO_USER)
        partner_after, _, _, _ = self._export(DEMO_PARTNER)
        owner_rows = [row for row in owner_after["decision_votes"] if row["decision_id"] == decision["id"]]
        partner_rows = [row for row in partner_after["decision_votes"] if row["decision_id"] == decision["id"]]
        self.assertEqual(owner_rows, partner_rows)
        self.assertEqual({row["user_id"] for row in owner_rows}, {DEMO_USER, DEMO_PARTNER})

    def test_partner_export_omits_sealed_letter_private_moment_and_provenance_evidence(self) -> None:
        private = self.store.create_moment({
            "title": "仅本人", "body": "PRIVATE-MOMENT-EXPORT-V2", "visibility": "private",
        }, DEMO_USER)
        provenance = self.store.record_provenance(
            "moment", private["id"], user_id=DEMO_USER,
            source_type="human", date_source="user_confirmed",
            evidence={"resource_version": private["version"], "confidence": 1.0},
        )
        letter = self.store.create_future_letter({
            "title": "以后再看", "body": "SEALED-FUTURE-EXPORT-V2",
        }, DEMO_USER, "export-letter-create-0001")
        self.store.seal_future_letter(
            letter["id"], self._future(48), letter["version"], DEMO_USER,
        )

        owner, _, _, _ = self._export(DEMO_USER)
        partner, _, _, _ = self._export(DEMO_PARTNER)
        self.assertIn(private["id"], {row["id"] for row in owner["moments"]})
        self.assertNotIn(private["id"], {row["id"] for row in partner["moments"]})
        self.assertIn(provenance["id"], {row["id"] for row in owner["memory_provenance"]})
        self.assertFalse(any("evidence_json" in row for row in owner["memory_provenance"]))
        encoded_partner = json.dumps(partner, ensure_ascii=False)
        self.assertNotIn("PRIVATE-MOMENT-EXPORT-V2", encoded_partner)
        self.assertNotIn("SEALED-FUTURE-EXPORT-V2", encoded_partner)
        self.assertNotIn(letter["id"], {row["id"] for row in partner["future_letters"]})


if __name__ == "__main__":
    unittest.main()
