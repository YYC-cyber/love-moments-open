from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from love_moments.store import DEMO_COUPLE, DEMO_PARTNER, DEMO_USER, LoveMomentsStore


class MemorySearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LoveMomentsStore(Path(self.temp.name) / "memories.db", seed_demo_data=True)
        with self.store.connection() as db:
            db.execute("DELETE FROM memory_completion_suggestions")
            db.execute("DELETE FROM memory_descriptions")
            db.execute("DELETE FROM memory_provenance WHERE moment_id IS NOT NULL OR trip_id IS NOT NULL")
            db.execute("DELETE FROM moments")
            db.execute("UPDATE trips SET deleted_at='2020-01-01T00:00:00+00:00'")

    def _moment(
        self,
        moment_id: str,
        *,
        creator: str = DEMO_USER,
        title: str = "杭州西湖散步",
        body: str = "一起看了日落",
        location: str | None = "杭州",
        kind: str = "photo",
        visibility: str = "shared",
        occurred_at: str = "2025-10-16T08:00:00+00:00",
        reveal_at: str | None = None,
        deleted_at: str | None = None,
        purged_at: str | None = None,
    ) -> None:
        with self.store.connection() as db:
            db.execute(
                """INSERT INTO moments(
                    id,couple_id,creator_id,type,title,body,occurred_at,location,
                    visibility,reveal_at,deleted_at,purged_at,version
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (
                    moment_id, DEMO_COUPLE, creator, kind, title, body, occurred_at,
                    location, visibility, reveal_at, deleted_at, purged_at,
                ),
            )
            db.execute(
                """INSERT INTO memory_provenance(
                    id,couple_id,owner_id,moment_id,source_type,date_source,
                    evidence_json,version,created_at,updated_at
                ) VALUES (?,?,?,?,?,'user_confirmed','{}',1,?,?)""",
                (f"prov-{moment_id}", DEMO_COUPLE, creator, moment_id, "human", occurred_at, occurred_at),
            )

    def _trip(self, trip_id: str = "trip-search") -> None:
        with self.store.connection() as db:
            db.execute(
                """INSERT INTO trips(
                    id,couple_id,title,destination,start_date,end_date,budget_cents,
                    departure_city,arrival_city,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,'上海','杭州市',?,?)""",
                (
                    trip_id, DEMO_COUPLE, "杭州纪念旅行", "浙江杭州", "2025-10-15",
                    "2025-10-17", 500000, "2025-01-01T00:00:00+00:00",
                    "2025-01-01T00:00:00+00:00",
                ),
            )

    def test_search_matches_moment_and_trip_without_duplicates(self) -> None:
        self._moment("moment-hangzhou")
        self._trip()
        result = self.store.search_memories("杭州", None, None, "all", None, 20, DEMO_USER)
        self.assertEqual(result["total"], 2)
        self.assertEqual({(item["resource_type"], item["resource_id"]) for item in result["items"]}, {
            ("moment", "moment-hangzhou"), ("trip", "trip-search"),
        })
        self.assertTrue(all("evidence" not in item for item in result["items"]))

    def test_private_descriptions_and_hidden_parents_do_not_change_partner_count(self) -> None:
        self._moment("shared")
        self._moment("private", title="秘密针线", visibility="private")
        future = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0).isoformat()
        self._moment("delayed", title="秘密针线", visibility="delayed", reveal_at=future)
        self._moment("deleted", title="秘密针线", deleted_at="2026-01-01T00:00:00+00:00")
        self._moment("purged", title="秘密针线", purged_at="2026-01-01T00:00:00+00:00")
        with self.store.connection() as db:
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            db.execute(
                """INSERT INTO memory_descriptions(
                    id,couple_id,author_id,moment_id,body,visibility,source_type,created_at,updated_at
                ) VALUES ('desc-private',?,?,?,'秘密针线','private','human',?,?)""",
                (DEMO_COUPLE, DEMO_USER, "shared", now, now),
            )
        partner = self.store.search_memories("秘密针线", None, None, "all", None, 20, DEMO_PARTNER)
        self.assertEqual(partner["total"], 0)
        self.assertEqual(partner["items"], [])
        self.assertEqual(partner["empty_reason"], "没有找到当前可见的相关回忆")
        owner = self.store.search_memories("秘密针线", None, None, "all", None, 20, DEMO_USER)
        self.assertEqual({item["resource_id"] for item in owner["items"]}, {"shared", "private", "delayed"})

    def test_shared_description_and_person_filter_match_only_real_author(self) -> None:
        self._moment("shared", creator=DEMO_USER, title="无关键词", body="")
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with self.store.connection() as db:
            db.execute(
                """INSERT INTO memory_descriptions(
                    id,couple_id,author_id,moment_id,body,visibility,source_type,created_at,updated_at
                ) VALUES ('desc-shared',?,?,?,'海边烟花','shared','human',?,?)""",
                (DEMO_COUPLE, DEMO_PARTNER, "shared", now, now),
            )
        result = self.store.search_memories(
            "海边烟花", None, None, "all", None, 20, DEMO_USER, person_id=DEMO_PARTNER,
        )
        self.assertEqual([item["resource_id"] for item in result["items"]], ["shared"])
        no_match = self.store.search_memories(
            "海边烟花", None, None, "all", None, 20, DEMO_USER, person_id="not-a-member",
        )
        self.assertEqual(no_match["total"], 0)

    def test_natural_language_filters_are_explicit_and_deterministic(self) -> None:
        self._moment("nl", title="西湖照片", occurred_at="2025-05-01T08:00:00+00:00")
        result = self.store.search_memories("去年演示甲在杭州记录的照片", None, None, "all", None, 20, DEMO_USER, now=datetime(2026, 10, 28, tzinfo=timezone.utc))
        parsed = result["parsed_filters"]
        self.assertEqual(parsed["start_date"], "2025-01-01")
        self.assertEqual(parsed["end_date"], "2025-12-31")
        self.assertEqual(parsed["content_type"], "photo")
        self.assertEqual(parsed["person_id"], DEMO_USER)
        self.assertEqual(parsed["location"], "杭州")
        self.assertEqual([item["resource_id"] for item in result["items"]], ["nl"])

        location_suffix = self.store.search_memories("杭州的照片", None, None, "all", None, 20, DEMO_USER)
        self.assertEqual(location_suffix["parsed_filters"]["location"], "杭州")
        nickname_collision = self.store.search_memories("唐人街照片", None, None, "all", None, 20, DEMO_USER)
        self.assertIsNone(nickname_collision["parsed_filters"]["person_id"])

    def test_literal_like_characters_and_validation_boundaries(self) -> None:
        self._moment("literal", title=r"100%_真\\'", body="", location=None)
        self.assertEqual(self.store.search_memories(r"100%_真\\'", None, None, "all", None, 20, DEMO_USER)["total"], 1)
        for kwargs in (
            {"limit": 0}, {"limit": 51}, {"limit": "x"},
            {"content_type": "future_letter"}, {"start_date": "2025-99-01"},
            {"start_date": "2026-01-01", "end_date": "2025-01-01"},
            {"keyword": "长" * 101}, {"cursor": "not-base64"},
        ):
            values = dict(keyword="", start_date=None, end_date=None, content_type="all", cursor=None, limit=20, user_id=DEMO_USER)
            values.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.search_memories(**values)

    def test_stable_cursor_has_no_duplicates_and_rechecks_scope(self) -> None:
        for suffix in ("a", "b", "c"):
            self._moment(f"moment-{suffix}", title=suffix, occurred_at="2025-10-16T08:00:00+00:00")
        first = self.store.search_memories("", None, None, "moment", None, 2, DEMO_USER)
        self.assertEqual(len(first["items"]), 2)
        second = self.store.search_memories("", None, None, "moment", first["next_cursor"], 2, DEMO_USER)
        ids = [item["resource_id"] for item in first["items"] + second["items"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {"moment-a", "moment-b", "moment-c"})
        with self.store.connection() as db:
            db.execute("UPDATE couples SET status='unlinked' WHERE id=?", (DEMO_COUPLE,))
        with self.assertRaises(ValueError):
            self.store.search_memories("", None, None, "moment", first["next_cursor"], 2, DEMO_USER)

    def test_unknown_provenance_is_not_mislabelled(self) -> None:
        self._moment("unknown")
        with self.store.connection() as db:
            db.execute("UPDATE memory_provenance SET source_type='legacy_unknown',date_source='legacy_unknown',evidence_json=? WHERE moment_id='unknown'", ('{"provider":"secret","provider_ref":"token"}',))
        item = self.store.search_memories("", None, None, "moment", None, 20, DEMO_USER)["items"][0]
        self.assertEqual(item["provenance"]["source_label"], "来源未记录")
        self.assertEqual(item["provenance"]["date_source_label"], "时间来源未记录")
        self.assertNotIn("evidence", item["provenance"])

    def test_account_zip_reuses_parent_visibility_for_moments_and_descriptions(self) -> None:
        self._moment("export-shared", body="共同正文")
        self._moment("export-own-private", body="我的私密正文", visibility="private")
        self._moment("export-partner-private", creator=DEMO_PARTNER, body="伴侣私密正文", visibility="private")
        future = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0).isoformat()
        self._moment("export-own-delayed", body="我的延时正文", visibility="delayed", reveal_at=future)
        self._moment("export-partner-delayed", creator=DEMO_PARTNER, body="伴侣未公开延时正文", visibility="delayed", reveal_at=future)
        self._moment("export-deleted", body="回收站秘密", deleted_at="2026-10-20T00:00:00+00:00")
        self._moment("export-purged", body="清除秘密", purged_at="2026-10-20T00:00:00+00:00")
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO couples(id,member_a,member_b,paired_at,anniversary,status) VALUES ('couple-old-export',?,?,? ,?,'unlinked')",
                (DEMO_USER, DEMO_PARTNER, "2020-01-01", "2020-01-01"),
            )
            db.execute(
                """INSERT INTO moments(id,couple_id,creator_id,type,title,body,occurred_at,visibility)
                   VALUES ('export-old-couple','couple-old-export',?,'text','旧关系','旧情侣关系正文','2020-01-01T00:00:00+00:00','shared')""",
                (DEMO_USER,),
            )
        self.store.save_memory_description(
            "moment", "export-shared",
            {"body": "共同父级上的共享描述", "visibility": "shared", "expected_version": 0}, DEMO_USER,
        )
        self.store.save_memory_description(
            "moment", "export-shared",
            {"body": "共同父级上的伴侣私密描述", "visibility": "private", "expected_version": 0}, DEMO_PARTNER,
        )
        self.store.save_memory_description(
            "moment", "export-partner-private",
            {"body": "私密父级伪共享描述", "visibility": "shared", "expected_version": 0}, DEMO_PARTNER,
        )
        archive_bytes, _ = self.store.account_export(DEMO_USER)
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            exported = json.loads(archive.read("data.json"))
        moment_ids = {item["id"] for item in exported["moments"]}
        self.assertIn("export-shared", moment_ids)
        self.assertIn("export-own-private", moment_ids)
        self.assertIn("export-own-delayed", moment_ids)
        self.assertNotIn("export-partner-private", moment_ids)
        self.assertNotIn("export-partner-delayed", moment_ids)
        self.assertNotIn("export-deleted", moment_ids)
        self.assertNotIn("export-purged", moment_ids)
        exported_text = json.dumps(exported, ensure_ascii=False)
        self.assertIn("共同父级上的共享描述", exported_text)
        self.assertNotIn("共同父级上的伴侣私密描述", exported_text)
        self.assertNotIn("私密父级伪共享描述", exported_text)
        self.assertNotIn("伴侣私密正文", exported_text)
        self.assertNotIn("伴侣未公开延时正文", exported_text)
        self.assertNotIn("旧情侣关系正文", exported_text)


if __name__ == "__main__":
    unittest.main()
