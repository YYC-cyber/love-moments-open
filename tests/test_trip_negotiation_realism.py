from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from love_moments.intelligent_ui import ConflictError
from love_moments.store import DEMO_PARTNER, DEMO_TRIP, DEMO_USER, LoveMomentsStore


class TripNegotiationRealismTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LoveMomentsStore(Path(self.temp.name) / "travel-tools.db", seed_demo_data=True)
        self.item_ids = ["neg-place-a", "neg-place-b", "neg-place-c"]
        with self.store.connection() as db:
            trip = db.execute("SELECT * FROM trips WHERE id=?", (DEMO_TRIP,)).fetchone()
            day = db.execute(
                "SELECT d.id FROM trip_days d JOIN plan_variants v ON v.id=d.variant_id "
                "WHERE v.trip_id=? AND v.style='slow' ORDER BY d.position LIMIT 1",
                (DEMO_TRIP,),
            ).fetchone()
            db.execute(
                "DELETE FROM itinerary_items WHERE day_id IN (SELECT d.id FROM trip_days d JOIN plan_variants v ON v.id=d.variant_id WHERE v.trip_id=? AND v.style='slow')",
                (DEMO_TRIP,),
            )
            rows = [
                (self.item_ids[0], day["id"], "place", "西湖", "09:00", 90, 1),
                (self.item_ids[1], day["id"], "place", "博物馆", "11:00", 60, 2),
                (self.item_ids[2], day["id"], "meal", "午餐", "13:00", 45, 3),
            ]
            db.executemany(
                "INSERT INTO itinerary_items(id,day_id,type,title,start_time,duration_minutes,cost_cents,note,details_json,locked,position,version) "
                "VALUES (?,?,?,?,?,?,0,'','{}',0,?,1)",
                rows,
            )
            db.execute(
                "UPDATE trips SET active_variant='slow',version=7,current_revision=3,status='planning',updated_at=? WHERE id=?",
                (datetime.now(timezone.utc).replace(microsecond=0).isoformat(), DEMO_TRIP),
            )
            self.trip_version = int(trip["version"]) if False else 7

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _ballot(self, values: tuple[str, str, str], *, key: str, user_id: str, **extra) -> dict:
        payload = {
            "expected_trip_version": self.trip_version,
            "expected_round": 1,
            "start_new_round": False,
            "preferences": [
                {"item_id": item_id, "preference": value, "expected_item_version": 1}
                for item_id, value in zip(self.item_ids, values)
            ],
            "client_request_id": key,
            **extra,
        }
        return self.store.submit_trip_item_preferences(DEMO_TRIP, payload, user_id)

    def _versions(self, user_id: str) -> dict[str, int]:
        value = self.store.get_trip_negotiation(DEMO_TRIP, user_id)
        if value.get("my_preferences"):
            rows = value["my_preferences"]
        else:
            rows = next(
                ballot["preferences"] for ballot in value.get("ballots", [])
                if ballot["user"]["id"] == user_id
            )
        return {row["item_id"]: row["version"] for row in rows}

    def test_first_ballot_is_private_and_reveal_is_identical(self) -> None:
        first = self._ballot(("must", "avoid", "okay"), key="neg:first:0001", user_id=DEMO_USER)
        self.assertEqual(first["state"], "waiting_for_partner")
        self.assertEqual([row["preference"] for row in first["my_preferences"]], ["must", "avoid", "okay"])

        partner = self.store.get_trip_negotiation(DEMO_TRIP, DEMO_PARTNER)
        self.assertEqual(partner["state"], "waiting_for_partner")
        self.assertTrue(partner["partner_submitted"])
        self.assertNotIn("my_preferences", partner)
        self.assertNotIn("proposal", partner)
        self.assertNotIn("preference_counts", partner)
        serialized = json.dumps(partner, ensure_ascii=False)
        self.assertNotIn('"avoid"', serialized)
        self.assertNotIn(self.item_ids[1], json.dumps({k: v for k, v in partner.items() if k != "candidates"}, ensure_ascii=False))

        with self.store.connection() as db:
            audit = "\n".join(row[0] for row in db.execute(
                "SELECT detail FROM audit_events WHERE action LIKE 'trip.negotiation%'"
            ).fetchall())
            events = "\n".join(row[0] for row in db.execute(
                "SELECT payload_json FROM realtime_events WHERE event_type='trip.negotiation.updated'"
            ).fetchall())
            inbox = json.dumps([
                dict(row) for row in db.execute(
                    "SELECT * FROM in_app_notifications WHERE event_type='trip.negotiation.updated'"
                ).fetchall()
            ], ensure_ascii=False)
            push = json.dumps([
                dict(row) for row in db.execute(
                    "SELECT * FROM push_outbox WHERE event_type='trip.negotiation.updated'"
                ).fetchall()
            ], ensure_ascii=False)
        observable_side_channels = audit + events + inbox + push
        self.assertNotIn("avoid", observable_side_channels)
        self.assertNotIn(self.item_ids[1], observable_side_channels)

        second = self._ballot(("must", "okay", "okay"), key="neg:second:001", user_id=DEMO_PARTNER)
        self.assertEqual(second["state"], "revealed")
        revealed_a = self.store.get_trip_negotiation(DEMO_TRIP, DEMO_USER)
        revealed_b = self.store.get_trip_negotiation(DEMO_TRIP, DEMO_PARTNER)
        self.assertEqual(revealed_a, revealed_b)
        self.assertEqual([row["item_id"] for row in revealed_a["proposal"]], [self.item_ids[0], self.item_ids[2]])
        self.assertEqual(revealed_a["proposal"][0]["reason_code"], "must_by_both")
        self.assertEqual(revealed_a["vetoed_item_ids"], [self.item_ids[1]])

    def test_veto_requires_both_explicit_overrides(self) -> None:
        self._ballot(("must", "avoid", "okay"), key="neg:first:0002", user_id=DEMO_USER)
        self._ballot(("must", "okay", "okay"), key="neg:second:002", user_id=DEMO_PARTNER)

        first_versions = self._versions(DEMO_USER)
        one = self.store.submit_trip_item_preferences(DEMO_TRIP, {
            "action": "confirm_avoid_override",
            "expected_round": 1,
            "item_ids": [self.item_ids[1]],
            "expected_preference_versions": {self.item_ids[1]: first_versions[self.item_ids[1]]},
            "client_request_id": "neg:override:001",
        }, DEMO_USER)
        self.assertNotIn(self.item_ids[1], [row["item_id"] for row in one["proposal"]])

        partner_versions = self._versions(DEMO_PARTNER)
        both = self.store.submit_trip_item_preferences(DEMO_TRIP, {
            "action": "confirm_avoid_override",
            "expected_round": 1,
            "item_ids": [self.item_ids[1]],
            "expected_preference_versions": {self.item_ids[1]: partner_versions[self.item_ids[1]]},
            "client_request_id": "neg:override:002",
        }, DEMO_PARTNER)
        self.assertIn(self.item_ids[1], [row["item_id"] for row in both["proposal"]])
        self.assertEqual(both["overridden_veto_item_ids"], [self.item_ids[1]])

    def test_ballot_requires_exact_current_snapshot_and_versions(self) -> None:
        invalid = {
            "expected_trip_version": self.trip_version,
            "expected_round": 1,
            "preferences": [
                {"item_id": self.item_ids[0], "preference": "must", "expected_item_version": 1},
                {"item_id": self.item_ids[1], "preference": "avoid", "expected_item_version": 1},
            ],
            "client_request_id": "neg:invalid:01",
        }
        with self.assertRaises(ValueError):
            self.store.submit_trip_item_preferences(DEMO_TRIP, invalid, DEMO_USER)
        invalid["preferences"].append(dict(invalid["preferences"][0]))
        with self.assertRaises(ValueError):
            self.store.submit_trip_item_preferences(DEMO_TRIP, invalid, DEMO_USER)

        self._ballot(("must", "avoid", "okay"), key="neg:first:0003", user_id=DEMO_USER)
        with self.assertRaises(ConflictError):
            self._ballot(("okay", "okay", "okay"), key="neg:double:001", user_id=DEMO_USER)

        versions = self._versions(DEMO_USER)
        edited = self._ballot(
            ("okay", "must", "okay"), key="neg:edit:00001", user_id=DEMO_USER,
            expected_preference_versions=versions,
        )
        self.assertEqual([row["preference"] for row in edited["my_preferences"]], ["okay", "must", "okay"])

        with self.store.connection() as db:
            db.execute("UPDATE itinerary_items SET version=version+1 WHERE id=?", (self.item_ids[0],))
            db.execute("UPDATE trips SET version=version+1 WHERE id=?", (DEMO_TRIP,))
        stale = self.store.get_trip_negotiation(DEMO_TRIP, DEMO_PARTNER)
        self.assertEqual(stale["state"], "stale")
        self.assertNotIn("my_preferences", stale)
        with self.assertRaises(ConflictError):
            self._ballot(("must", "okay", "okay"), key="neg:stale:0001", user_id=DEMO_PARTNER)

    def test_submission_idempotency_is_resource_and_payload_bound(self) -> None:
        first = self._ballot(("must", "avoid", "okay"), key="neg:idempotent:1", user_id=DEMO_USER)
        replay = self._ballot(("must", "avoid", "okay"), key="neg:idempotent:1", user_id=DEMO_USER)
        self.assertEqual(first, replay)
        with self.assertRaises(ConflictError):
            self._ballot(("okay", "avoid", "okay"), key="neg:idempotent:1", user_id=DEMO_USER)
        with self.store.connection() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM audit_events WHERE action='trip.negotiation.submitted'"
            ).fetchone()[0], 1)

    def test_submission_rolls_back_when_event_write_fails(self) -> None:
        with patch.object(self.store, "_event", side_effect=RuntimeError("event failed")):
            with self.assertRaises(RuntimeError):
                self._ballot(("must", "avoid", "okay"), key="neg:rollback:01", user_id=DEMO_USER)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM trip_item_preferences").fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM mutation_idempotency WHERE operation LIKE 'trip.negotiation:%'"
            ).fetchone()[0], 0)

    def test_concurrent_partner_submissions_reveal_once(self) -> None:
        def submit(user_id: str, key: str) -> dict:
            return self._ballot(("must", "okay", "okay"), key=key, user_id=user_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(submit, DEMO_USER, "neg:concurrent:01")
            second = pool.submit(submit, DEMO_PARTNER, "neg:concurrent:02")
            results = [first.result(), second.result()]
        self.assertIn("revealed", {row["state"] for row in results})
        current = self.store.get_trip_negotiation(DEMO_TRIP, DEMO_USER)
        self.assertEqual(current["state"], "revealed")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM trip_item_preferences").fetchone()[0], 6)

    def test_stale_ballot_can_only_restart_as_explicit_new_round(self) -> None:
        self._ballot(("must", "avoid", "okay"), key="neg:stale:first", user_id=DEMO_USER)
        with self.store.connection() as db:
            db.execute("UPDATE itinerary_items SET version=2 WHERE id=?", (self.item_ids[0],))
            db.execute("UPDATE trips SET version=8,updated_at=? WHERE id=?", (
                datetime.now(timezone.utc).replace(microsecond=0).isoformat(), DEMO_TRIP,
            ))
        preferences = [
            {"item_id": item_id, "preference": "okay", "expected_item_version": 2 if index == 0 else 1}
            for index, item_id in enumerate(self.item_ids)
        ]
        with self.assertRaises(ConflictError):
            self.store.submit_trip_item_preferences(DEMO_TRIP, {
                "expected_trip_version": 8, "expected_round": 1, "start_new_round": False,
                "preferences": preferences, "client_request_id": "neg:stale:normal",
            }, DEMO_USER)
        restarted = self.store.submit_trip_item_preferences(DEMO_TRIP, {
            "expected_trip_version": 8, "expected_round": 1, "start_new_round": True,
            "preferences": preferences, "client_request_id": "neg:stale:restart",
        }, DEMO_USER)
        self.assertEqual((restarted["round"], restarted["state"]), (2, "waiting_for_partner"))
        self.assertEqual([row["preference"] for row in restarted["my_preferences"]], ["okay"] * 3)

    def test_regeneration_that_cascades_all_old_rows_stays_stale_until_new_round(self) -> None:
        self._ballot(("must", "avoid", "okay"), key="neg:regenerate:first", user_id=DEMO_USER)
        with self.store.connection() as db:
            day = db.execute(
                "SELECT d.id FROM trip_days d JOIN plan_variants v ON v.id=d.variant_id "
                "WHERE v.trip_id=? AND v.style='slow' ORDER BY d.position LIMIT 1", (DEMO_TRIP,),
            ).fetchone()
            db.execute("DELETE FROM itinerary_items WHERE day_id=?", (day["id"],))
            db.executemany(
                "INSERT INTO itinerary_items(id,day_id,type,title,start_time,duration_minutes,cost_cents,note,details_json,position,version) "
                "VALUES (?,?,?,?,?,60,0,'','{}',?,1)",
                [
                    ("regenerated-a", day["id"], "place", "新景点 A", "09:00", 1),
                    ("regenerated-b", day["id"], "place", "新景点 B", "11:00", 2),
                ],
            )
            db.execute("UPDATE trips SET version=8,updated_at=? WHERE id=?", (
                datetime.now(timezone.utc).replace(microsecond=0).isoformat(), DEMO_TRIP,
            ))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM trip_item_preferences").fetchone()[0], 0)
        stale = self.store.get_trip_negotiation(DEMO_TRIP, DEMO_PARTNER)
        self.assertEqual((stale["round"], stale["state"]), (1, "stale"))
        self.assertNotIn("my_preferences", stale)
        current_preferences = [
            {"item_id": item["id"], "preference": "okay", "expected_item_version": item["version"]}
            for item in stale["candidates"]
        ]
        with self.assertRaises(ConflictError):
            self.store.submit_trip_item_preferences(DEMO_TRIP, {
                "expected_trip_version": 8, "expected_round": 1, "start_new_round": False,
                "preferences": current_preferences, "client_request_id": "neg:regenerate:normal",
            }, DEMO_PARTNER)
        restarted = self.store.submit_trip_item_preferences(DEMO_TRIP, {
            "expected_trip_version": 8, "expected_round": 1, "start_new_round": True,
            "preferences": current_preferences, "client_request_id": "neg:regenerate:new-round",
        }, DEMO_PARTNER)
        self.assertEqual((restarted["round"], restarted["state"]), (2, "waiting_for_partner"))

    def test_unlink_removes_only_current_incomplete_private_round(self) -> None:
        self._ballot(("must", "avoid", "okay"), key="neg:unlink:first", user_id=DEMO_USER)
        result = self.store.unlink_couple("test123", "解除情侣", DEMO_USER)
        self.assertTrue(result["unlinked"])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM trip_item_preferences").fetchone()[0], 0)

    def _replace_realism_items(self) -> list[str]:
        ids = ["real-meal", "real-luggage", "real-hotel", "real-train", "real-flight", "real-park"]
        with self.store.connection() as db:
            day = db.execute(
                "SELECT d.id FROM trip_days d JOIN plan_variants v ON v.id=d.variant_id "
                "WHERE v.trip_id=? AND v.style='slow' ORDER BY d.position LIMIT 1",
                (DEMO_TRIP,),
            ).fetchone()
            db.execute("DELETE FROM itinerary_items WHERE day_id=?", (day["id"],))
            rows = [
                (ids[0], day["id"], "meal", "午餐", "08:00", 30, 1, 0),
                (ids[1], day["id"], "luggage", "寄存行李", "08:35", 10, 2, 0),
                (ids[2], day["id"], "hotel", "酒店办理入住", "08:50", 15, 3, 1),
                (ids[3], day["id"], "train", "G1234 火车出发", "09:15", 30, 4, 0),
                (ids[4], day["id"], "flight", "MU123 航班出发", "10:00", 30, 5, 0),
                (ids[5], day["id"], "place", "主题乐园", "11:00", 60, 6, 0),
            ]
            db.executemany(
                "INSERT INTO itinerary_items(id,day_id,type,title,start_time,duration_minutes,cost_cents,note,details_json,position,locked,version) "
                "VALUES (?,?,?,?,?,?,0,'','{}',?,?,1)",
                rows,
            )
            db.execute("UPDATE trips SET version=12,updated_at=? WHERE id=?", (
                datetime.now(timezone.utc).replace(microsecond=0).isoformat(), DEMO_TRIP,
            ))
        self.trip_version = 12
        return ids

    def test_realism_uses_fixed_rules_without_modifying_itinerary(self) -> None:
        ids = self._replace_realism_items()
        with self.store.connection() as db:
            before = [tuple(row) for row in db.execute(
                "SELECT id,title,start_time,duration_minutes,locked,version FROM itinerary_items WHERE id IN (%s) ORDER BY id"
                % ",".join("?" for _ in ids), ids,
            ).fetchall()]
        result = self.store.run_trip_realism_check(DEMO_TRIP, {
            "expected_trip_version": 12,
            "client_request_id": "realism:fixed:001",
        }, DEMO_USER)
        self.assertNotIn("client_request_id", result)
        codes = {row["code"] for row in result["findings"]}
        self.assertTrue({
            "transfer_buffer", "meal_duration", "luggage_storage", "hotel_procedure",
            "train_early_arrival", "flight_early_arrival", "queue_estimate", "route_missing",
        }.issubset(codes))
        queue = next(row for row in result["findings"] if row["code"] == "queue_estimate")
        self.assertEqual(queue["source_type"], "rules")
        self.assertIsNone(queue["evidence_at"])
        self.assertIn("经验估计", queue["suggestion"])
        hotel = next(row for row in result["findings"] if row["code"] == "hotel_procedure")
        self.assertFalse(hotel["apply_allowed"])
        with self.store.connection() as db:
            after = [tuple(row) for row in db.execute(
                "SELECT id,title,start_time,duration_minutes,locked,version FROM itinerary_items WHERE id IN (%s) ORDER BY id"
                % ",".join("?" for _ in ids), ids,
            ).fetchall()]
            stored = json.loads(db.execute(
                "SELECT findings_json FROM trip_realism_checks WHERE id=?", (result["id"],)
            ).fetchone()[0])
        self.assertEqual(before, after)
        self.assertNotIn("apply_allowed", stored[0])
        self.assertTrue(all(set(row) == {
            "item_id", "code", "severity", "buffer_minutes", "source_type", "evidence_at", "suggestion"
        } for row in stored))

    def _run_with_adversarial_route_cache(
        self, *, fetched_at: str, expires_at: str, source_type: str | None = None,
        request_id: str,
    ) -> dict:
        with self.store.connection() as db:
            for index, item_id in enumerate(self.item_ids[:2]):
                db.execute(
                    "INSERT INTO itinerary_item_places(itinerary_item_id,trip_id,provider,provider_place_id,name,formatted_address,country,province,city,district,latitude,longitude,category,confirmed_by,confirmed_at) "
                    "VALUES (?,?,?,?,?,'','中国','浙江','杭州','',?,?,'',?,?)",
                    (item_id, DEMO_TRIP, "amap", f"time-poi-{index}", item_id,
                     30.2 + index * 0.005, 120.1, DEMO_USER,
                     datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
                )
            origin, destination = [dict(row) for row in db.execute(
                "SELECT * FROM itinerary_item_places WHERE itinerary_item_id IN (?,?) ORDER BY itinerary_item_id",
                (self.item_ids[0], self.item_ids[1]),
            ).fetchall()]
            result_json = {"duration_seconds": 300, "provider": "amap"}
            if source_type is not None:
                result_json["source_type"] = source_type
            db.execute(
                "INSERT INTO route_cache(cache_key,provider,mode,origin_place_id,destination_place_id,origin_latitude,origin_longitude,destination_latitude,destination_longitude,result_json,fetched_at,expires_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"time-cache-{request_id}", "amap", "walking", "time-poi-0", "time-poi-1",
                 origin["latitude"], origin["longitude"], destination["latitude"], destination["longitude"],
                 json.dumps(result_json), fetched_at, expires_at),
            )
        return self.store.run_trip_realism_check(DEMO_TRIP, {
            "expected_trip_version": self.trip_version,
            "client_request_id": request_id,
        }, DEMO_USER)

    def _assert_route_cache_rejected(self, result: dict) -> None:
        self.assertFalse(any(row["source_type"] == "cache" for row in result["findings"]))
        self.assertTrue(any(row["code"] == "route_missing" for row in result["findings"]))

    def test_route_cache_rejects_future_fetched_at(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        result = self._run_with_adversarial_route_cache(
            fetched_at=(now + timedelta(hours=1)).isoformat(),
            expires_at=(now + timedelta(hours=2)).isoformat(),
            request_id="realism:future-fetched",
        )
        self._assert_route_cache_rejected(result)

    def test_route_cache_rejects_timezone_naive_timestamps(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        result = self._run_with_adversarial_route_cache(
            fetched_at=now.replace(tzinfo=None).isoformat(),
            expires_at=(now + timedelta(hours=2)).replace(tzinfo=None).isoformat(),
            request_id="realism:naive-timestamps",
        )
        self._assert_route_cache_rejected(result)

    def test_route_cache_rejects_invalid_expires_at(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        result = self._run_with_adversarial_route_cache(
            fetched_at=(now - timedelta(minutes=5)).isoformat(), expires_at="not-an-iso-time",
            request_id="realism:invalid-expiry",
        )
        self._assert_route_cache_rejected(result)

    def test_route_cache_rejects_expired_or_non_increasing_time_window(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        far_east = timezone(timedelta(hours=14))
        expired = self._run_with_adversarial_route_cache(
            fetched_at=(now - timedelta(hours=2)).astimezone(far_east).isoformat(),
            expires_at=(now - timedelta(hours=1)).astimezone(far_east).isoformat(),
            request_id="realism:expired-window",
        )
        self._assert_route_cache_rejected(expired)

    def test_route_cache_rejects_expires_at_not_after_fetched_at(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        result = self._run_with_adversarial_route_cache(
            fetched_at=(now + timedelta(hours=2)).isoformat(),
            expires_at=(now + timedelta(hours=1)).isoformat(),
            request_id="realism:non-increasing-window",
        )
        self._assert_route_cache_rejected(result)

    def test_route_cache_rejects_legacy_source_type(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        result = self._run_with_adversarial_route_cache(
            fetched_at=(now - timedelta(minutes=5)).isoformat(),
            expires_at=(now + timedelta(hours=2)).isoformat(), source_type="legacy",
            request_id="realism:legacy-source",
        )
        self._assert_route_cache_rejected(result)

    def test_realism_reads_nonexpired_route_cache_and_becomes_stale(self) -> None:
        with self.store.connection() as db:
            for index, item_id in enumerate(self.item_ids[:2]):
                db.execute(
                    "INSERT INTO itinerary_item_places(itinerary_item_id,trip_id,provider,provider_place_id,name,formatted_address,country,province,city,district,latitude,longitude,category,confirmed_by,confirmed_at) "
                    "VALUES (?,?,?,?,?,'','中国','浙江','杭州','西湖区',?,?, '景点',?,?)",
                    (item_id, DEMO_TRIP, "amap", f"poi-{index}", f"地点{index}", 30.20 + index * .01, 120.10 + index * .01, DEMO_USER, datetime.now(timezone.utc).isoformat()),
                )
            places = [dict(row) for row in db.execute(
                "SELECT * FROM itinerary_item_places WHERE itinerary_item_id IN (?,?) ORDER BY itinerary_item_id",
                (self.item_ids[0], self.item_ids[1]),
            ).fetchall()]
            cache_key = self.store._route_cache_key("amap", places[0], places[1], "walking")
            now = datetime.now(timezone.utc).replace(microsecond=0)
            db.execute(
                "INSERT INTO route_cache(cache_key,provider,mode,origin_place_id,destination_place_id,origin_latitude,origin_longitude,destination_latitude,destination_longitude,result_json,fetched_at,expires_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (cache_key, "amap", "walking", "poi-0", "poi-1", places[0]["latitude"], places[0]["longitude"], places[1]["latitude"], places[1]["longitude"], json.dumps({"duration_seconds": 1200, "distance_meters": 1200}), now.isoformat(), (now + timedelta(hours=2)).isoformat()),
            )
        result = self.store.run_trip_realism_check(DEMO_TRIP, {
            "expected_trip_version": self.trip_version,
            "client_request_id": "realism:cache:001",
        }, DEMO_USER)
        cached = [row for row in result["findings"] if row["source_type"] == "cache"]
        self.assertTrue(cached)
        self.assertIsNotNone(cached[0]["evidence_at"])
        replay = self.store.run_trip_realism_check(DEMO_TRIP, {
            "expected_trip_version": self.trip_version,
            "client_request_id": "realism:cache:001",
        }, DEMO_USER)
        self.assertEqual(result["id"], replay["id"])
        with self.store.connection() as db:
            db.execute("UPDATE itinerary_items SET version=version+1 WHERE id=?", (self.item_ids[0],))
            db.execute("UPDATE trips SET version=version+1 WHERE id=?", (DEMO_TRIP,))
        stale = self.store.get_trip_realism_check(DEMO_TRIP, DEMO_USER)
        self.assertEqual(stale["effective_status"], "stale")
        self.assertFalse(stale["actionable"])

    def test_flight_feasibility_adds_route_time_to_early_arrival_buffer(self) -> None:
        with self.store.connection() as db:
            day = db.execute(
                "SELECT d.id FROM trip_days d JOIN plan_variants v ON v.id=d.variant_id "
                "WHERE v.trip_id=? AND v.style='slow' ORDER BY d.position LIMIT 1", (DEMO_TRIP,),
            ).fetchone()
            db.execute("DELETE FROM itinerary_items WHERE day_id=?", (day["id"],))
            db.executemany(
                "INSERT INTO itinerary_items(id,day_id,type,title,start_time,duration_minutes,cost_cents,note,details_json,position,version) "
                "VALUES (?,?,?,?,?,?,0,'','{}',?,1)",
                [
                    ("route-origin", day["id"], "place", "酒店早餐", "06:00", 30, 1),
                    ("route-flight", day["id"], "flight", "航班出发", "08:50", 120, 2),
                ],
            )
            now = datetime.now(timezone.utc).replace(microsecond=0)
            for item_id, poi_id, lat, lng in (
                ("route-origin", "origin-poi", 30.2000, 120.1000),
                ("route-flight", "airport-poi", 30.2050, 120.1050),
            ):
                db.execute(
                    "INSERT INTO itinerary_item_places(itinerary_item_id,trip_id,provider,provider_place_id,name,formatted_address,country,province,city,district,latitude,longitude,category,confirmed_by,confirmed_at) "
                    "VALUES (?,?,?,?,?,'','中国','浙江','杭州','',?,?,'',?,?)",
                    (item_id, DEMO_TRIP, "amap", poi_id, item_id, lat, lng, DEMO_USER, now.isoformat()),
                )
            places = [dict(row) for row in db.execute(
                "SELECT * FROM itinerary_item_places WHERE itinerary_item_id IN ('route-origin','route-flight') ORDER BY itinerary_item_id DESC"
            ).fetchall()]
            cache_key = self.store._route_cache_key("amap", places[0], places[1], "walking")
            db.execute(
                "INSERT INTO route_cache(cache_key,provider,mode,origin_place_id,destination_place_id,origin_latitude,origin_longitude,destination_latitude,destination_longitude,result_json,fetched_at,expires_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (cache_key, "amap", "walking", "origin-poi", "airport-poi", 30.2, 120.1, 30.205, 120.105,
                 json.dumps({"duration_seconds": 1800}), now.isoformat(), (now + timedelta(hours=2)).isoformat()),
            )
        result = self.store.run_trip_realism_check(DEMO_TRIP, {
            "expected_trip_version": self.trip_version,
            "client_request_id": "realism:route-plus-flight",
        }, DEMO_USER)
        flight = next(row for row in result["findings"] if row["code"] == "flight_early_arrival")
        self.assertEqual((flight["severity"], flight["buffer_minutes"]), ("conflict", 10))

    def test_route_cache_prefers_distance_appropriate_mode_and_rejects_untrusted_provider(self) -> None:
        with self.store.connection() as db:
            now = datetime.now(timezone.utc).replace(microsecond=0)
            for index, item_id in enumerate(self.item_ids[:2]):
                db.execute(
                    "INSERT INTO itinerary_item_places(itinerary_item_id,trip_id,provider,provider_place_id,name,formatted_address,country,province,city,district,latitude,longitude,category,confirmed_by,confirmed_at) "
                    "VALUES (?,?,?,?,?,'','中国','浙江','杭州','',?,?,'',?,?)",
                    (item_id, DEMO_TRIP, "amap", f"long-poi-{index}", item_id,
                     30.2 + index * 0.09, 120.1, DEMO_USER, now.isoformat()),
                )
            origin, destination = [dict(row) for row in db.execute(
                "SELECT * FROM itinerary_item_places WHERE itinerary_item_id IN (?,?) ORDER BY itinerary_item_id",
                (self.item_ids[0], self.item_ids[1]),
            ).fetchall()]
            for provider, mode, minutes in (
                ("amap", "walking", 120), ("amap", "driving", 20),
                ("amap", "transit", 25), ("ai", "bicycling", 5),
            ):
                db.execute(
                    "INSERT INTO route_cache(cache_key,provider,mode,origin_place_id,destination_place_id,origin_latitude,origin_longitude,destination_latitude,destination_longitude,result_json,fetched_at,expires_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"cache-{provider}-{mode}", provider, mode, "long-poi-0", "long-poi-1",
                     origin["latitude"], origin["longitude"], destination["latitude"], destination["longitude"],
                     json.dumps({"duration_seconds": minutes * 60}), now.isoformat(), (now + timedelta(hours=2)).isoformat()),
                )
        result = self.store.run_trip_realism_check(DEMO_TRIP, {
            "expected_trip_version": self.trip_version,
            "client_request_id": "realism:mode-and-trust",
        }, DEMO_USER)
        cached = next(row for row in result["findings"] if row["source_type"] == "cache")
        self.assertIn("约 25 分钟", cached["suggestion"])
        self.assertNotIn("约 20 分钟", cached["suggestion"])
        self.assertNotIn("约 120 分钟", cached["suggestion"])
        self.assertNotIn("约 5 分钟", cached["suggestion"])

    def test_untrusted_route_cache_never_becomes_factual_evidence(self) -> None:
        with self.store.connection() as db:
            now = datetime.now(timezone.utc).replace(microsecond=0)
            for index, item_id in enumerate(self.item_ids[:2]):
                db.execute(
                    "INSERT INTO itinerary_item_places(itinerary_item_id,trip_id,provider,provider_place_id,name,formatted_address,country,province,city,district,latitude,longitude,category,confirmed_by,confirmed_at) "
                    "VALUES (?,?,?,?,?,'','中国','浙江','杭州','',?,?,'',?,?)",
                    (item_id, DEMO_TRIP, "amap", f"unsafe-poi-{index}", item_id,
                     30.2 + index * 0.01, 120.1, DEMO_USER, now.isoformat()),
                )
            origin, destination = [dict(row) for row in db.execute(
                "SELECT * FROM itinerary_item_places WHERE itinerary_item_id IN (?,?) ORDER BY itinerary_item_id",
                (self.item_ids[0], self.item_ids[1]),
            ).fetchall()]
            db.execute(
                "INSERT INTO route_cache(cache_key,provider,mode,origin_place_id,destination_place_id,origin_latitude,origin_longitude,destination_latitude,destination_longitude,result_json,fetched_at,expires_at) "
                "VALUES ('unsafe-ai','ai','transit',?,?,?,?,?,?,?,?,?)",
                ("unsafe-poi-0", "unsafe-poi-1", origin["latitude"], origin["longitude"],
                 destination["latitude"], destination["longitude"], json.dumps({"duration_seconds": 300}),
                 now.isoformat(), (now + timedelta(hours=2)).isoformat()),
            )
        result = self.store.run_trip_realism_check(DEMO_TRIP, {
            "expected_trip_version": self.trip_version,
            "client_request_id": "realism:unsafe-provider",
        }, DEMO_USER)
        self.assertFalse(any(row["source_type"] == "cache" for row in result["findings"]))
        self.assertTrue(any(row["code"] == "route_missing" for row in result["findings"]))

    def test_realism_rejects_stale_version_and_unknown_trip(self) -> None:
        with self.assertRaises(ConflictError):
            self.store.run_trip_realism_check(DEMO_TRIP, {
                "expected_trip_version": self.trip_version - 1,
                "client_request_id": "realism:stale:01",
            }, DEMO_USER)
        with self.assertRaises(KeyError):
            self.store.run_trip_realism_check("trip-foreign", {
                "expected_trip_version": 1,
                "client_request_id": "realism:foreign:1",
            }, DEMO_USER)

    def test_first_transport_item_still_requires_arrival_buffer(self) -> None:
        with self.store.connection() as db:
            day = db.execute(
                "SELECT d.id FROM trip_days d JOIN plan_variants v ON v.id=d.variant_id "
                "WHERE v.trip_id=? AND v.style='slow' ORDER BY d.position LIMIT 1", (DEMO_TRIP,),
            ).fetchone()
            db.execute("DELETE FROM itinerary_items WHERE day_id=?", (day["id"],))
            db.execute(
                "INSERT INTO itinerary_items(id,day_id,type,title,start_time,duration_minutes,cost_cents,note,details_json,position,version) "
                "VALUES ('first-flight',?,'flight','航班出发','08:00',120,0,'','{}',1,1)",
                (day["id"],),
            )
        checked = self.store.run_trip_realism_check(DEMO_TRIP, {
            "expected_trip_version": self.trip_version,
            "client_request_id": "realism:first-flight",
        }, DEMO_USER)
        flight = next(row for row in checked["findings"] if row["code"] == "flight_early_arrival")
        self.assertEqual(flight["buffer_minutes"], 120)

    def test_structured_transport_details_drive_flight_classification(self) -> None:
        with self.store.connection() as db:
            day = db.execute(
                "SELECT d.id FROM trip_days d JOIN plan_variants v ON v.id=d.variant_id "
                "WHERE v.trip_id=? AND v.style='slow' ORDER BY d.position LIMIT 1", (DEMO_TRIP,),
            ).fetchone()
            db.execute("DELETE FROM itinerary_items WHERE day_id=?", (day["id"],))
            db.execute(
                "INSERT INTO itinerary_items(id,day_id,type,title,start_time,duration_minutes,cost_cents,note,details_json,position,version) "
                "VALUES ('structured-flight',?,'transport','前往下一站','08:00',120,0,'',?,1,1)",
                (day["id"], json.dumps({"transport_mode": "flight"})),
            )
        checked = self.store.run_trip_realism_check(DEMO_TRIP, {
            "expected_trip_version": self.trip_version,
            "client_request_id": "realism:structured-flight",
        }, DEMO_USER)
        flight = next(row for row in checked["findings"] if row["code"] == "flight_early_arrival")
        self.assertEqual(flight["item_id"], "structured-flight")


if __name__ == "__main__":
    unittest.main()
