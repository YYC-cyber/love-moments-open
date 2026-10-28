from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from love_moments.schema import (
    REQUIRED_TABLES_V33,
    REQUIRED_TABLES_V34,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    V34_SCHEMA_MARKER,
    schema_inventory,
)
from love_moments.store import LoveMomentsStore


NEW_TABLES = {
    "decisions",
    "decision_options",
    "decision_votes",
    "decision_confirmations",
    "wishlist_claims",
    "ingredient_catalog",
    "food_profiles",
    "pantry_items",
    "shopping_category_preferences",
    "weekly_meal_plan_drafts",
    "cook_sessions",
    "cook_timers",
    "ingredient_substitutions",
    "trip_item_preferences",
    "trip_realism_checks",
    "trip_packing_items",
    "trip_emergency_entries",
    "trip_expenses",
    "memory_provenance",
    "memory_descriptions",
    "memory_completion_suggestions",
    "voice_transcripts",
    "future_letters",
    "album_export_jobs",
    "ai_usage_events",
    "mutation_receipts",
}


class PracticalFeaturesSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "practical.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_migrate_rejects_future_schema_before_any_write(self) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute("CREATE TABLE future_sentinel(value TEXT NOT NULL)")
            db.execute("INSERT INTO future_sentinel(value) VALUES ('keep-me')")
            db.execute("PRAGMA user_version=35")
        store = object.__new__(LoveMomentsStore)
        store.db_path = str(self.db_path)
        with self.assertRaisesRegex(RuntimeError, "高于当前程序"):
            store.migrate()
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 35)
            self.assertEqual(db.execute("SELECT value FROM future_sentinel").fetchone()[0], "keep-me")
            self.assertIsNone(
                db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
            )

    def test_inventory_detects_missing_columns_foreign_keys_constraints_and_every_declared_index(self) -> None:
        LoveMomentsStore(self.db_path, seed_demo_data=False)
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            db.execute("DROP TABLE pantry_items")
            db.execute(
                """
                CREATE TABLE pantry_items (
                    id TEXT PRIMARY KEY NOT NULL, couple_id TEXT NOT NULL,
                    ingredient_id TEXT NOT NULL, updated_by TEXT NOT NULL,
                    state TEXT NOT NULL, storage_location TEXT, expires_on TEXT,
                    quantity_note TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )
                """
            )
            db.execute("DROP INDEX idx_decisions_queue")
            inventory = schema_inventory(db, expected_version=34)
        self.assertFalse(inventory["ok"])
        self.assertIn("pantry_items", inventory["missing_foreign_keys"])
        self.assertIn("pantry_items", inventory["malformed_constraints"])
        self.assertIn("idx_pantry_expiry", inventory["missing_indexes"])
        self.assertIn("idx_decisions_queue", inventory["missing_indexes"])

    def test_inventory_rejects_unallowlisted_extra_check_on_frozen_v34_table(self) -> None:
        LoveMomentsStore(self.db_path, seed_demo_data=False)
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            table_sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='decisions'"
            ).fetchone()["sql"]
            index_sql = [
                row["sql"]
                for row in db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='decisions' AND sql IS NOT NULL"
                ).fetchall()
            ]
            db.execute("DROP TABLE decisions")
            db.execute(f"{table_sql.rsplit(')', 1)[0]}, CHECK(title='ONLY'))")
            for statement in index_sql:
                db.execute(statement)
            inventory = schema_inventory(db, expected_version=34)
        self.assertFalse(inventory["structural_ok"])
        self.assertIn("decisions", inventory["malformed_constraints"])

    def test_inventory_rejects_generated_column_hidden_from_table_info(self) -> None:
        LoveMomentsStore(self.db_path, seed_demo_data=False)
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            table_sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='decisions'"
            ).fetchone()["sql"]
            index_sql = [
                row["sql"]
                for row in db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='decisions' AND sql IS NOT NULL"
                ).fetchall()
            ]
            db.execute("DROP TABLE decisions")
            generated_sql = table_sql.replace(
                " updated_at TEXT NOT NULL,\n UNIQUE",
                " updated_at TEXT NOT NULL,\n "
                "title_shadow TEXT GENERATED ALWAYS AS (title) VIRTUAL,\n UNIQUE",
            )
            self.assertNotEqual(generated_sql, table_sql)
            db.execute(generated_sql)
            for statement in index_sql:
                db.execute(statement)
            inventory = schema_inventory(db, expected_version=34)
        self.assertFalse(inventory["structural_ok"])
        self.assertIn("decisions", inventory["malformed_constraints"])

    def test_inventory_rejects_split_composite_foreign_key(self) -> None:
        LoveMomentsStore(self.db_path, seed_demo_data=False)
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            table_sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='decision_votes'"
            ).fetchone()["sql"]
            index_sql = [
                row["sql"]
                for row in db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='decision_votes' AND sql IS NOT NULL"
                ).fetchall()
            ]
            split_foreign_keys = table_sql.replace(
                "FOREIGN KEY(decision_id,option_id) REFERENCES decision_options(decision_id,id) ON DELETE CASCADE",
                "FOREIGN KEY(decision_id) REFERENCES decision_options(decision_id) ON DELETE CASCADE, "
                "FOREIGN KEY(option_id) REFERENCES decision_options(id) ON DELETE CASCADE",
            )
            self.assertNotEqual(split_foreign_keys, table_sql)
            db.execute("DROP TABLE decision_votes")
            db.execute(split_foreign_keys)
            for statement in index_sql:
                db.execute(statement)
            inventory = schema_inventory(db, expected_version=34)
        self.assertFalse(inventory["structural_ok"])
        self.assertIn("decision_votes", inventory["malformed_constraints"])

    def test_inventory_rejects_duplicate_identical_check_constraint(self) -> None:
        LoveMomentsStore(self.db_path, seed_demo_data=False)
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            table_sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='decisions'"
            ).fetchone()["sql"]
            index_sql = [
                row["sql"]
                for row in db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='decisions' AND sql IS NOT NULL"
                ).fetchall()
            ]
            db.execute("DROP TABLE decisions")
            db.execute(f"{table_sql.rsplit(')', 1)[0]}, CHECK(version >= 1))")
            for statement in index_sql:
                db.execute(statement)
            inventory = schema_inventory(db, expected_version=34)
        self.assertFalse(inventory["structural_ok"])
        self.assertIn("decisions", inventory["malformed_constraints"])

    def test_inventory_rejects_duplicate_identical_unique_constraint(self) -> None:
        LoveMomentsStore(self.db_path, seed_demo_data=False)
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            table_sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='decisions'"
            ).fetchone()["sql"]
            index_sql = [
                row["sql"]
                for row in db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='decisions' AND sql IS NOT NULL"
                ).fetchall()
            ]
            db.execute("DROP TABLE decisions")
            db.execute(
                f"{table_sql.rsplit(')', 1)[0]}, "
                "UNIQUE(couple_id,creator_id,client_request_id))"
            )
            for statement in index_sql:
                db.execute(statement)
            inventory = schema_inventory(db, expected_version=34)
        self.assertFalse(inventory["structural_ok"])
        self.assertIn("decisions", inventory["malformed_constraints"])

    def test_inventory_compares_unique_on_conflict_policy_with_default_abort(self) -> None:
        for policy in ("REPLACE", "IGNORE", "FAIL", "ROLLBACK", "ABORT"):
            with self.subTest(policy=policy):
                db_path = Path(self.temp.name) / f"unique-conflict-{policy.lower()}.db"
                LoveMomentsStore(db_path, seed_demo_data=False)
                with sqlite3.connect(db_path) as db:
                    db.row_factory = sqlite3.Row
                    table_sql = db.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name='decisions'"
                    ).fetchone()["sql"]
                    index_sql = [
                        row["sql"]
                        for row in db.execute(
                            "SELECT sql FROM sqlite_master WHERE type='index' "
                            "AND tbl_name='decisions' AND sql IS NOT NULL"
                        ).fetchall()
                    ]
                    unique = "UNIQUE(couple_id,creator_id,client_request_id)"
                    policy_sql = table_sql.replace(unique, f"{unique} ON CONFLICT {policy}")
                    self.assertNotEqual(policy_sql, table_sql)
                    db.execute("DROP TABLE decisions")
                    db.execute(policy_sql)
                    for statement in index_sql:
                        db.execute(statement)
                    inventory = schema_inventory(db, expected_version=34)
                if policy == "ABORT":
                    self.assertTrue(inventory["structural_ok"])
                    self.assertNotIn("decisions", inventory["malformed_constraints"])
                else:
                    self.assertFalse(inventory["structural_ok"])
                    self.assertIn("decisions", inventory["malformed_constraints"])

    def test_inventory_rejects_unallowlisted_extra_unique_index_on_frozen_v34_table(self) -> None:
        LoveMomentsStore(self.db_path, seed_demo_data=False)
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            db.execute("CREATE UNIQUE INDEX idx_decisions_unapproved_title ON decisions(title)")
            inventory = schema_inventory(db, expected_version=34)
        self.assertFalse(inventory["structural_ok"])
        self.assertIn("idx_decisions_unapproved_title", inventory["unexpected_indexes"])

    def test_migrate_rejects_real_foreign_key_orphans_before_v34_stamp(self) -> None:
        LoveMomentsStore(self.db_path, seed_demo_data=True)
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            couple = db.execute("SELECT * FROM couples WHERE status='active' LIMIT 1").fetchone()
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute(
                """INSERT INTO pantry_items(
                    id,couple_id,ingredient_id,updated_by,state,created_at,updated_at
                ) VALUES ('orphan-pantry',?,'missing-ingredient',?,'have','now','now')""",
                (couple["id"], couple["member_a"]),
            )
            db.execute("DELETE FROM schema_migrations WHERE version=34")
            db.execute("PRAGMA user_version=33")
        store = object.__new__(LoveMomentsStore)
        store.db_path = str(self.db_path)
        with self.assertRaisesRegex(RuntimeError, "外键"):
            store.migrate()
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 33)
            self.assertIsNone(
                db.execute("SELECT 1 FROM schema_migrations WHERE version=34").fetchone()
            )
            self.assertEqual(
                db.execute("SELECT ingredient_id FROM pantry_items WHERE id='orphan-pantry'").fetchone()[0],
                "missing-ingredient",
            )

    def test_migrate_does_not_stamp_v34_when_existing_table_breaks_frozen_contract(self) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """
                CREATE TABLE pantry_items (
                    id TEXT PRIMARY KEY NOT NULL, couple_id TEXT NOT NULL,
                    ingredient_id TEXT NOT NULL, updated_by TEXT NOT NULL,
                    state TEXT NOT NULL, storage_location TEXT, expires_on TEXT,
                    quantity_note TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )
                """
            )
            db.execute("PRAGMA user_version=33")
        store = object.__new__(LoveMomentsStore)
        store.db_path = str(self.db_path)
        with self.assertRaisesRegex(RuntimeError, "v34 结构不完整"):
            store.migrate()
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 33)

    def test_ingredient_substitution_is_bound_to_meal_snapshot_and_preview_version(self) -> None:
        store = LoveMomentsStore(self.db_path, seed_demo_data=True)
        with store.connection() as db:
            couple = db.execute("SELECT * FROM couples WHERE status='active' LIMIT 1").fetchone()
            db.execute(
                """INSERT INTO ingredient_catalog(
                    id,canonical_name,normalized_name,created_at,updated_at
                ) VALUES ('ingredient-from','原料甲','原料甲','now','now'),
                         ('ingredient-to','原料乙','原料乙','now','now')"""
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(ingredient_substitutions)")}
            self.assertTrue(
                {
                    "meal_item_id", "snapshot_ingredient_ref", "meal_version",
                    "expected_versions_json", "rule_key", "rule_version",
                }.issubset(columns)
            )
            foreign_keys = {
                (row["from"], row["table"], row["to"], row["on_delete"])
                for row in db.execute("PRAGMA foreign_key_list(ingredient_substitutions)")
            }
            self.assertIn(("meal_item_id", "meal_cart_items", "id", "CASCADE"), foreign_keys)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    """INSERT INTO ingredient_substitutions(
                        id,couple_id,creator_id,from_ingredient_id,to_ingredient_id,
                        meal_version,source_type,status,version,created_at,updated_at
                    ) VALUES ('bad-meal-version',?,?, 'ingredient-from','ingredient-to',0,'rules','suggested',1,'now','now')""",
                    (couple["id"], couple["member_a"]),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    """INSERT INTO ingredient_substitutions(
                        id,couple_id,creator_id,from_ingredient_id,to_ingredient_id,
                        expected_versions_json,source_type,status,version,created_at,updated_at
                    ) VALUES ('bad-version-map',?,?, 'ingredient-from','ingredient-to','[]','rules','suggested',1,'now','now')""",
                    (couple["id"], couple["member_a"]),
                )

    def test_existing_v34_adds_meal_substitution_binding_without_version_bump(self) -> None:
        store = LoveMomentsStore(self.db_path, seed_demo_data=False)
        with store.connection() as db:
            db.execute("DROP TABLE ingredient_substitutions")
            db.execute(
                """CREATE TABLE ingredient_substitutions (
                    id TEXT PRIMARY KEY NOT NULL, couple_id TEXT NOT NULL REFERENCES couples(id), creator_id TEXT NOT NULL REFERENCES users(id),
                    from_ingredient_id TEXT NOT NULL REFERENCES ingredient_catalog(id), to_ingredient_id TEXT NOT NULL REFERENCES ingredient_catalog(id),
                    recipe_id TEXT REFERENCES recipes(id) ON DELETE SET NULL,
                    ratio_note TEXT NOT NULL DEFAULT '', suitable_methods_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(suitable_methods_json) AND json_type(suitable_methods_json)='array'),
                    effect_note TEXT NOT NULL DEFAULT '', added_allergens_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(added_allergens_json) AND json_type(added_allergens_json)='array'),
                    safety_status TEXT NOT NULL DEFAULT 'needs_review' CHECK(safety_status IN ('needs_review','allowed','blocked')),
                    profile_versions_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(profile_versions_json) AND json_type(profile_versions_json)='object'),
                    source_type TEXT NOT NULL CHECK(source_type IN ('human','real_api','rules','ai','mock','cache','legacy_unknown')),
                    status TEXT NOT NULL DEFAULT 'suggested' CHECK(status IN ('suggested','applied','dismissed','expired')),
                    client_request_id TEXT, version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                    expires_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    CHECK(from_ingredient_id <> to_ingredient_id), CHECK(status <> 'applied' OR safety_status='allowed'),
                    UNIQUE(couple_id,creator_id,client_request_id)
                )"""
            )
            db.execute("CREATE INDEX idx_ingredient_substitutions_owner ON ingredient_substitutions(couple_id,creator_id,status,created_at DESC)")
        store.migrate()
        with store.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 34)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(ingredient_substitutions)")}
            self.assertTrue({
                "meal_item_id", "snapshot_ingredient_ref", "meal_version",
                "expected_versions_json", "rule_key", "rule_version",
            }.issubset(columns))
            self.assertTrue(schema_inventory(db, expected_version=34)["ok"])

    def test_fresh_v34_has_frozen_tables_extensions_and_inventory(self) -> None:
        LoveMomentsStore(self.db_path, seed_demo_data=False)
        self.assertEqual(SCHEMA_VERSION, 34)
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 34)
            inventory = schema_inventory(db)
            self.assertEqual(inventory["missing_tables"], [])
            self.assertEqual(inventory["uncovered_tables"], [])
            self.assertEqual(set(REQUIRED_TABLES_V34) - set(REQUIRED_TABLES_V33), NEW_TABLES)
            for table, expected in {
                "ai_context_grants": {"revoked_at", "last_used_at", "resource_version", "usage_scope", "request_id", "version"},
                "calendar_entries": {"participant_a_id", "participant_b_id", "blocks_time", "source_type"},
                "shared_list_items": {"item_kind", "deleted_at", "created_at", "updated_at"},
                "media_assets": {"captured_at", "date_source", "source_type", "duration_ms"},
            }.items():
                columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                self.assertTrue(expected.issubset(columns), (table, expected - columns))

    def test_new_foreign_keys_checks_and_partial_uniqueness_are_enforced(self) -> None:
        store = LoveMomentsStore(self.db_path, seed_demo_data=True)
        with store.connection() as db:
            couple = db.execute("SELECT * FROM couples LIMIT 1").fetchone()
            item = db.execute(
                "SELECT i.* FROM shared_list_items i JOIN shared_lists l ON l.id=i.list_id WHERE l.couple_id=? LIMIT 1",
                (couple["id"],),
            ).fetchone()
            if item is None:
                db.execute("INSERT INTO shared_lists(id,couple_id,type,title) VALUES ('gift-list',?,'wishlist','礼物')", (couple["id"],))
                db.execute("INSERT INTO shared_list_items(id,list_id,creator_id,text,position,item_kind) VALUES ('gift-item','gift-list',?,'礼物',0,'gift')", (couple["member_a"],))
                item_id = "gift-item"
            else:
                item_id = item["id"]
                db.execute("UPDATE shared_list_items SET item_kind='gift' WHERE id=?", (item_id,))
            db.execute(
                "INSERT INTO wishlist_claims(id,item_id,claimant_id,item_version_at_claim,created_at,updated_at) VALUES ('claim-1',?,?,1,'2026-01-01','2026-01-01')",
                (item_id, couple["member_b"]),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO wishlist_claims(id,item_id,claimant_id,item_version_at_claim,created_at,updated_at) VALUES ('claim-2',?,?,1,'2026-01-01','2026-01-01')",
                    (item_id, couple["member_a"]),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO ingredient_catalog(id,canonical_name,normalized_name,aliases_json,allergens_json,category,source_type,created_at,updated_at) VALUES ('bad','坏','坏','{}','[]','invalid','human','2026-01-01','2026-01-01')"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO ingredient_catalog(id,canonical_name,normalized_name,aliases_json,allergens_json,category,source_type,created_at,updated_at) VALUES ('bad-json','坏','坏2','{}','[]','other','human','2026-01-01','2026-01-01')"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO trip_packing_items(id,trip_id,creator_id,label,assignment,source_type,created_at,updated_at) VALUES ('bad-pack','missing',?,'伞','shared','human','2026-01-01','2026-01-01')",
                    (couple["member_a"],),
                )
            for decision_id in ("decision-a", "decision-b"):
                db.execute(
                    "INSERT INTO decisions(id,couple_id,creator_id,title,selection_mode,closes_at,created_at,updated_at) VALUES (?,?,?,'测试','single','2099-01-01','2026-01-01','2026-01-01')",
                    (decision_id, couple["id"], couple["member_a"]),
                )
            db.execute("INSERT INTO decision_options(id,decision_id,label,position) VALUES ('option-a','decision-a','A',0)")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO decision_votes(decision_id,voting_round,user_id,option_id,submitted_at) VALUES ('decision-b',1,?,'option-a','2026-01-01')",
                    (couple["member_a"],),
                )

    def test_v33_upgrade_preserves_content_and_backfills_unknown_provenance_once(self) -> None:
        old_definitions = """
        CREATE TABLE ai_context_grants (
            id TEXT PRIMARY KEY,couple_id TEXT NOT NULL REFERENCES couples(id),user_id TEXT NOT NULL REFERENCES users(id),
            surface TEXT NOT NULL,resource_type TEXT NOT NULL,resource_id TEXT NOT NULL,purpose TEXT NOT NULL,
            expires_at TEXT NOT NULL,created_at TEXT NOT NULL
        );
        CREATE TABLE calendar_entries (
            id TEXT PRIMARY KEY,couple_id TEXT NOT NULL REFERENCES couples(id),creator_id TEXT NOT NULL REFERENCES users(id),
            entry_type TEXT NOT NULL,title TEXT NOT NULL,starts_at TEXT NOT NULL,ends_at TEXT,all_day INTEGER NOT NULL DEFAULT 0,
            recurrence TEXT NOT NULL DEFAULT 'none',location TEXT NOT NULL DEFAULT '',note TEXT NOT NULL DEFAULT '',
            completed_at TEXT,deleted_at TEXT,version INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
        );
        CREATE TABLE shared_list_items (
            id TEXT PRIMARY KEY,list_id TEXT NOT NULL REFERENCES shared_lists(id),creator_id TEXT NOT NULL REFERENCES users(id),
            text TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',completed_at TEXT,position INTEGER NOT NULL,version INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE media_assets (
            id TEXT PRIMARY KEY,couple_id TEXT NOT NULL REFERENCES couples(id),owner_id TEXT NOT NULL REFERENCES users(id),
            kind TEXT NOT NULL,original_name TEXT NOT NULL,mime_type TEXT NOT NULL,relative_path TEXT NOT NULL UNIQUE,
            thumbnail_path TEXT,thumbnail_checksum TEXT,size_bytes INTEGER NOT NULL,width INTEGER,height INTEGER,
            metadata_stripped INTEGER NOT NULL DEFAULT 0,visibility TEXT NOT NULL DEFAULT 'shared',reveal_at TEXT,
            status TEXT NOT NULL DEFAULT 'ready',storage_provider TEXT NOT NULL DEFAULT 'local',storage_key TEXT,checksum TEXT,
            deleted_at TEXT,purged_at TEXT,version INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL
        );
        """
        base_schema, _ = SCHEMA_SQL.split(V34_SCHEMA_MARKER, 1)
        with sqlite3.connect(self.db_path) as db:
            db.executescript(old_definitions)
            db.executescript(base_schema)
            db.execute("INSERT INTO users(id,username,nickname,avatar_label) VALUES ('u1','u1','一','一'),('u2','u2','二','二')")
            db.execute("INSERT INTO couples(id,member_a,member_b,paired_at,anniversary) VALUES ('c1','u1','u2','2023-01-01','2023-01-01')")
            db.execute("INSERT INTO shared_lists(id,couple_id,type,title) VALUES ('l1','c1','wishlist','愿望')")
            db.execute("INSERT INTO shared_list_items(id,list_id,creator_id,text,position) VALUES ('w1','l1','u1','旧愿望',0)")
            db.execute("INSERT INTO media_assets(id,couple_id,owner_id,kind,original_name,mime_type,relative_path,size_bytes,visibility,status,created_at) VALUES ('m1','c1','u1','image','old.jpg','image/jpeg','images/old.jpg',4,'private','ready','2023-01-02')")
            db.execute("INSERT INTO moments(id,couple_id,creator_id,type,title,body,occurred_at,visibility,media_url,media_id,created_at) VALUES ('moment-old','c1','u1','photo','旧时光','原文','2023-01-02T12:00:00+08:00','private','/api/v1/media/m1','m1','2023-01-02')")
            db.execute("INSERT INTO calendar_entries(id,couple_id,creator_id,entry_type,title,starts_at,version,created_at,updated_at) VALUES ('cal1','c1','u1','todo','旧待办','2023-01-03',1,'2023-01-01','2023-01-01')")
            db.execute("INSERT INTO ai_context_grants(id,couple_id,user_id,surface,resource_type,resource_id,purpose,expires_at,created_at) VALUES ('g1','c1','u1','home','moment','moment-old','ui.compose','2099-01-01','2023-01-01')")
            db.execute("INSERT INTO schema_migrations(version) VALUES (33)")
            db.execute("PRAGMA user_version=33")
            before = dict(zip(("id", "body", "occurred_at", "media_url", "visibility", "created_at"), db.execute("SELECT id,body,occurred_at,media_url,visibility,created_at FROM moments WHERE id='moment-old'").fetchone()))
        store = LoveMomentsStore(self.db_path, seed_demo_data=False)
        store.migrate()
        store.migrate()
        with store.connection() as db:
            after = dict(db.execute("SELECT id,body,occurred_at,media_url,visibility,created_at FROM moments WHERE id=?", (before["id"],)).fetchone())
            self.assertEqual(after, before)
            provenance = db.execute("SELECT * FROM memory_provenance WHERE moment_id=?", (before["id"],)).fetchall()
            self.assertEqual(len(provenance), 1)
            self.assertEqual(provenance[0]["source_type"], "legacy_unknown")
            self.assertEqual(provenance[0]["date_source"], "legacy_unknown")
            calendar = db.execute("SELECT participant_a_id,participant_b_id,blocks_time,source_type FROM calendar_entries WHERE id='cal1'").fetchone()
            if calendar:
                self.assertIsNone(calendar["participant_a_id"])
                self.assertIsNone(calendar["participant_b_id"])
                self.assertEqual(calendar["blocks_time"], 0)
                self.assertEqual(calendar["source_type"], "legacy_unknown")
            grant = db.execute("SELECT purpose,usage_scope,request_id,resource_version FROM ai_context_grants WHERE id='g1'").fetchone()
            self.assertEqual(grant["purpose"], "ui.compose")
            self.assertEqual(grant["usage_scope"], "legacy_unknown")
            self.assertIsNone(grant["request_id"])
            self.assertIsNone(grant["resource_version"])


if __name__ == "__main__":
    unittest.main()
