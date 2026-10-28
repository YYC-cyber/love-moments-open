from __future__ import annotations

import sqlite3
import tempfile
import unittest
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from love_moments.life_assistant import (
    LifeIntent,
    OpenAICompatibleLifeAssistant,
    RuleBasedLifeAssistant,
    life_assistant_from_env,
)
from love_moments.schema import SCHEMA_VERSION
from love_moments.store import DEMO_PARTNER, DEMO_USER, LoveMomentsStore


class _ModelResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class LifeAssistantProviderTests(unittest.TestCase):
    def test_rule_provider_preserves_reminder_text(self) -> None:
        parsed = RuleBasedLifeAssistant().parse(
            "明天提醒我检查采购清单", timezone_name="Asia/Shanghai"
        )
        self.assertEqual(parsed.task_query, "检查采购清单")
        self.assertTrue(parsed.add_todo)
        self.assertFalse(parsed.add_calendar)

    def test_openai_compatible_provider_validates_structured_intent(self) -> None:
        body = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "requested_date": "2099-10-16",
                                "meal_slot": "dinner",
                                "planned_time": "18:45",
                                "dish_query": "番茄炒蛋",
                                "task_query": "",
                                "add_calendar": True,
                                "add_shopping": True,
                                "add_todo": False,
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
        provider = OpenAICompatibleLifeAssistant(
            "test-key", "https://example.test/v1", "test-model", 5
        )
        with mock.patch(
            "love_moments.life_assistant.request.urlopen",
            return_value=_ModelResponse(body),
        ) as urlopen:
            parsed = provider.parse(
                "2099年10月16日晚餐想吃番茄炒蛋，加入日历并生成采购清单",
                timezone_name="Asia/Shanghai",
            )
        self.assertEqual(parsed.dish_query, "番茄炒蛋")
        self.assertEqual(parsed.planned_time, "18:45")
        self.assertTrue(parsed.add_shopping)
        self.assertEqual(parsed.source, "ai")
        request_payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(request_payload["response_format"], {"type": "json_object"})
        self.assertNotIn("resource_id", request_payload["messages"][1]["content"])

    def test_openai_compatible_provider_falls_back_on_invalid_output(self) -> None:
        provider = OpenAICompatibleLifeAssistant(
            "test-key", "https://example.test/v1", "test-model", 5
        )
        with mock.patch(
            "love_moments.life_assistant.request.urlopen",
            side_effect=TimeoutError("timeout"),
        ):
            parsed = provider.parse(
                "明天提醒我检查采购清单", timezone_name="Asia/Shanghai"
            )
        self.assertEqual(parsed.task_query, "检查采购清单")
        self.assertTrue(parsed.add_todo)
        self.assertEqual(parsed.source, "rules")

    def test_openai_compatible_provider_rejects_null_query_fields(self) -> None:
        invalid = {
            "requested_date": "2099-10-16",
            "meal_slot": "dinner",
            "planned_time": "18:30",
            "dish_query": None,
            "task_query": None,
            "add_calendar": True,
            "add_shopping": False,
            "add_todo": False,
        }
        body = {
            "choices": [
                {"message": {"content": json.dumps(invalid, ensure_ascii=False)}}
            ]
        }
        provider = OpenAICompatibleLifeAssistant(
            "test-key", "https://example.test/v1", "test-model", 5
        )
        with mock.patch(
            "love_moments.life_assistant.request.urlopen",
            return_value=_ModelResponse(body),
        ):
            parsed = provider.parse(
                "明天想吃番茄炒蛋，加入日历", timezone_name="Asia/Shanghai"
            )
        self.assertEqual(parsed.source, "rules")
        self.assertEqual(parsed.dish_query, "番茄炒蛋")

    def test_provider_factory_uses_rules_without_key_and_ai_with_key(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsInstance(life_assistant_from_env(), RuleBasedLifeAssistant)
        with mock.patch.dict(
            "os.environ",
            {
                "LLM_API_KEY": "test-key",
                "LLM_BASE_URL": "https://example.test/v1",
                "LLM_MODEL": "test-model",
            },
            clear=True,
        ):
            self.assertIsInstance(
                life_assistant_from_env(), OpenAICompatibleLifeAssistant
            )


class LifeAssistantSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "assistant.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_schema_version_and_required_tables_are_installed(self) -> None:
        LoveMomentsStore(self.db_path, seed_demo_data=True)
        self.assertEqual(SCHEMA_VERSION, 34)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 34)
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertTrue(
                {
                    "life_assistant_plans",
                    "life_assistant_plan_items",
                    "life_assistant_confirmations",
                    "life_assistant_jobs",
                    "in_app_notifications",
                    "daily_couple_summaries",
                    "couple_assistant_settings",
                }.issubset(tables)
            )

    def test_confirmation_identity_is_bound_to_plan_version_and_user(self) -> None:
        LoveMomentsStore(self.db_path, seed_demo_data=True)
        with sqlite3.connect(self.db_path) as db:
            columns = db.execute(
                "PRAGMA table_info(life_assistant_confirmations)"
            ).fetchall()
            primary_key = [
                name
                for _, name, _, _, _, position in sorted(
                    columns, key=lambda value: value[5]
                )
                if position
            ]
            self.assertEqual(primary_key, ["plan_id", "plan_version", "user_id"])
            indexes = {
                row[1]
                for row in db.execute(
                    "PRAGMA index_list(in_app_notifications)"
                ).fetchall()
            }
            self.assertIn("idx_in_app_notifications_inbox", indexes)

    def test_restart_marks_abandoned_running_jobs_failed(self) -> None:
        store = LoveMomentsStore(self.db_path, seed_demo_data=True)
        created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with store.connection() as db:
            db.execute(
                """
                INSERT INTO life_assistant_jobs(
                    id,couple_id,creator_id,client_request_id,input_hash,status,created_at
                ) VALUES ('assistant_job_abandoned','demo-couple','demo-user-a',
                          'restart-probe','hash','running',?)
                """,
                (created_at,),
            )
        LoveMomentsStore(self.db_path, seed_demo_data=False)
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                """
                SELECT status,error_code,completed_at FROM life_assistant_jobs
                WHERE id='assistant_job_abandoned'
                """
            ).fetchone()
        self.assertEqual(row[0], "failed")
        self.assertEqual(row[1], "server_restarted")
        self.assertTrue(row[2])


class LifeAssistantPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = LoveMomentsStore(
            Path(self.temp.name) / "assistant-plans.db", seed_demo_data=True
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create_plan(self, intent: str | None = None) -> dict:
        result = self.store.create_life_assistant_job(
            {
                "intent": intent
                or "周六晚上想吃番茄炒蛋，加入日历并生成采购清单",
                "client_request_id": f"request-{uuid.uuid4().hex}",
            },
            DEMO_USER,
        )
        self.assertEqual(result["status"], "completed")
        return result["plan"]

    def test_remote_intent_parsing_does_not_hold_a_database_write_lock(self) -> None:
        database = self.store.db_path
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE provider_probe(value TEXT NOT NULL)")

        class ProbeProvider:
            name = "ai"

            def parse(self, _intent: str, *, timezone_name: str) -> LifeIntent:
                with sqlite3.connect(database, timeout=0.1) as db:
                    db.execute("INSERT INTO provider_probe(value) VALUES ('called')")
                return RuleBasedLifeAssistant().parse(
                    "明天提醒我检查采购清单", timezone_name=timezone_name
                )

        self.store.life_assistant_provider = ProbeProvider()
        plan = self.create_plan("明天提醒我检查采购清单")
        self.assertEqual(plan["source"], "rules")
        with sqlite3.connect(database) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM provider_probe").fetchone()[0], 1
            )

    def test_plan_is_shared_before_confirmation_and_resolves_absolute_date(self) -> None:
        plan = self.create_plan()
        mine = self.store.get_life_assistant_plan(plan["id"], DEMO_USER)
        partner = self.store.get_life_assistant_plan(plan["id"], DEMO_PARTNER)
        self.assertEqual(mine["id"], partner["id"])
        self.assertEqual(mine["version"], partner["version"])
        self.assertEqual(mine["status"], "pending_confirmation")
        item_types = {item["item_type"] for item in mine["items"]}
        self.assertTrue(
            {"calendar.create", "meal.schedule", "shopping.merge"}.issubset(
                item_types
            )
        )
        scheduled = next(
            item for item in mine["items"] if item["item_type"] == "meal.schedule"
        )
        target = date.fromisoformat(scheduled["payload"]["planned_date"])
        self.assertGreaterEqual(target, date.today())
        self.assertLessEqual(target, date.today() + timedelta(days=7))
        self.assertEqual(scheduled["payload"]["meal_slot"], "dinner")
        self.assertEqual(
            scheduled["payload"]["recipe_id"], "recipe_builtin_tomato_egg"
        )

    def test_first_confirmation_does_not_write_and_second_is_atomic(self) -> None:
        plan = self.create_plan()
        with self.store.connection() as db:
            before_calendar = db.execute(
                "SELECT COUNT(*) FROM calendar_entries"
            ).fetchone()[0]
            before_meals = db.execute(
                "SELECT COUNT(*) FROM meal_cart_items"
            ).fetchone()[0]
        first = self.store.confirm_life_assistant_plan(
            plan["id"],
            {
                "expected_version": plan["version"],
                "idempotency_key": f"confirm-{uuid.uuid4().hex}",
            },
            DEMO_USER,
        )
        self.assertFalse(first["applied"])
        self.assertEqual(first["confirmation_count"], 1)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM calendar_entries").fetchone()[0],
                before_calendar,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM meal_cart_items").fetchone()[0],
                before_meals,
            )

        second = self.store.confirm_life_assistant_plan(
            plan["id"],
            {
                "expected_version": plan["version"],
                "idempotency_key": f"confirm-{uuid.uuid4().hex}",
            },
            DEMO_PARTNER,
        )
        self.assertTrue(second["applied"])
        self.assertEqual(second["plan"]["status"], "applied")
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM calendar_entries").fetchone()[0],
                before_calendar + 1,
            )
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM meal_cart_items").fetchone()[0],
                before_meals + 1,
            )
            self.assertGreater(
                db.execute("SELECT COUNT(*) FROM shopping_list_items").fetchone()[0],
                0,
            )
            calendar = db.execute(
                """
                SELECT participant_a_id,participant_b_id,blocks_time,source_type
                FROM calendar_entries ORDER BY rowid DESC LIMIT 1
                """
            ).fetchone()
            self.assertEqual(
                {calendar["participant_a_id"], calendar["participant_b_id"]},
                {DEMO_USER, DEMO_PARTNER},
            )
            self.assertEqual(calendar["blocks_time"], 0)
            self.assertEqual(calendar["source_type"], "rules")
        self.assertEqual(
            second["result"]["calendar"][0]["participant_ids"],
            [DEMO_USER, DEMO_PARTNER],
        )
        self.assertNotIn(
            "participant_a_id", second["result"]["calendar"][0]
        )

    def test_repeated_confirmation_key_is_idempotent(self) -> None:
        plan = self.create_plan()
        key = f"confirm-{uuid.uuid4().hex}"
        first = self.store.confirm_life_assistant_plan(
            plan["id"],
            {"expected_version": plan["version"], "idempotency_key": key},
            DEMO_USER,
        )
        repeated = self.store.confirm_life_assistant_plan(
            plan["id"],
            {"expected_version": plan["version"], "idempotency_key": key},
            DEMO_USER,
        )
        self.assertFalse(first["applied"])
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(repeated["confirmation_count"], 1)
        current = self.store.get_life_assistant_plan(plan["id"], DEMO_PARTNER)
        self.assertEqual(current["confirmation_count"], 1)

    def test_execution_failure_rolls_back_confirmation_and_all_domain_writes(self) -> None:
        plan = self.create_plan()
        self.store.confirm_life_assistant_plan(
            plan["id"],
            {
                "expected_version": plan["version"],
                "idempotency_key": f"confirm-{uuid.uuid4().hex}",
            },
            DEMO_USER,
        )
        with self.store.connection() as db:
            before_calendar = db.execute(
                "SELECT COUNT(*) FROM calendar_entries"
            ).fetchone()[0]
        with mock.patch.object(
            self.store,
            "_merge_plan_shopping",
            side_effect=RuntimeError("模拟采购写入失败"),
        ):
            with self.assertRaisesRegex(RuntimeError, "模拟采购写入失败"):
                self.store.confirm_life_assistant_plan(
                    plan["id"],
                    {
                        "expected_version": plan["version"],
                        "idempotency_key": f"confirm-{uuid.uuid4().hex}",
                    },
                    DEMO_PARTNER,
                )
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM calendar_entries").fetchone()[0],
                before_calendar,
            )
            self.assertEqual(
                db.execute(
                    """
                    SELECT COUNT(*) FROM life_assistant_confirmations
                    WHERE plan_id=? AND plan_version=? AND user_id=?
                    """,
                    (plan["id"], plan["version"], DEMO_PARTNER),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute(
                    "SELECT status FROM life_assistant_plans WHERE id=?",
                    (plan["id"],),
                ).fetchone()[0],
                "pending_confirmation",
            )

    def test_concurrent_two_user_confirmation_executes_once(self) -> None:
        plan = self.create_plan()
        with self.store.connection() as db:
            before_calendar = db.execute(
                "SELECT COUNT(*) FROM calendar_entries"
            ).fetchone()[0]

        def confirm(user_id: str) -> dict:
            return self.store.confirm_life_assistant_plan(
                plan["id"],
                {
                    "expected_version": plan["version"],
                    "idempotency_key": f"confirm-{uuid.uuid4().hex}",
                },
                user_id,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(confirm, [DEMO_USER, DEMO_PARTNER])
            )
        self.assertEqual(sorted(result["applied"] for result in results), [False, True])
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM calendar_entries").fetchone()[0],
                before_calendar + 1,
            )
            self.assertEqual(
                db.execute(
                    """
                    SELECT COUNT(*) FROM life_assistant_confirmations
                    WHERE plan_id=? AND plan_version=?
                    """,
                    (plan["id"], plan["version"]),
                ).fetchone()[0],
                2,
            )

    def test_plan_edit_increments_version_and_invalidates_current_confirmations(self) -> None:
        plan = self.create_plan()
        self.store.confirm_life_assistant_plan(
            plan["id"],
            {
                "expected_version": 1,
                "idempotency_key": f"confirm-{uuid.uuid4().hex}",
            },
            DEMO_USER,
        )
        updated = self.store.update_life_assistant_plan(
            plan["id"],
            {"expected_version": 1, "title": "周末一起做饭"},
            DEMO_PARTNER,
        )
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["confirmation_count"], 0)
        self.assertEqual(updated["title"], "周末一起做饭")

    def test_unknown_dish_never_fabricates_a_shopping_list(self) -> None:
        plan = self.create_plan("周六晚饭想吃火星云朵菜，加入采购清单")
        item_types = {item["item_type"] for item in plan["items"]}
        self.assertIn("meal.wish", item_types)
        self.assertNotIn("meal.schedule", item_types)
        self.assertNotIn("shopping.merge", item_types)
        self.assertTrue(plan["warnings"])

    def test_plain_todo_keeps_the_real_task_instead_of_fabricating_a_meal(self) -> None:
        plan = self.create_plan("明天待办：检查采购清单")
        self.assertEqual(
            [item["item_type"] for item in plan["items"]], ["todo.create"]
        )
        todo = plan["items"][0]
        self.assertEqual(todo["payload"]["title"], "检查采购清单")
        self.assertNotIn("晚餐", plan["summary"])
        reminder = self.create_plan("明天提醒我们整理采购清单")
        self.assertEqual(
            [item["item_type"] for item in reminder["items"]], ["todo.create"]
        )
        self.assertEqual(
            reminder["items"][0]["payload"]["title"], "整理采购清单"
        )

    def test_non_meal_calendar_request_keeps_the_real_activity(self) -> None:
        plan = self.create_plan("明天安排晾衣服，加入日历")
        calendar = next(
            item for item in plan["items"] if item["item_type"] == "calendar.create"
        )
        self.assertEqual(calendar["payload"]["title"], "晾衣服")
        self.assertIn("晾衣服", plan["summary"])
        self.assertNotIn("晚餐", json.dumps(plan, ensure_ascii=False))
        self.assertFalse(plan["warnings"])

    def test_past_scheduled_event_is_rejected_and_job_is_not_left_running(self) -> None:
        request_id = f"request-{uuid.uuid4().hex}"
        with self.assertRaisesRegex(ValueError, "过去"):
            self.store.create_life_assistant_job(
                {
                    "intent": "2020年1月1日晚餐想吃番茄炒蛋，加入日历",
                    "client_request_id": request_id,
                },
                DEMO_USER,
            )
        with self.store.connection() as db:
            job = db.execute(
                """
                SELECT status,error_code,completed_at FROM life_assistant_jobs
                WHERE client_request_id=?
                """,
                (request_id,),
            ).fetchone()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_code"], "ValueError")
        self.assertTrue(job["completed_at"])

    def test_today_summary_excludes_private_and_message_bodies(self) -> None:
        secret = "PRIVATE-MARKER-DO-NOT-LEAK"
        self.store.create_moment(
            {"title": "私密标题", "body": secret, "visibility": "private"},
            DEMO_USER,
        )
        self.store.create_message(
            {"content": secret, "idempotency_key": f"message-{uuid.uuid4().hex}"},
            DEMO_USER,
        )
        today = self.store.get_life_assistant_today(DEMO_PARTNER)
        self.assertEqual(today["counts"]["unread_messages"], 1)
        self.assertNotIn(secret, json.dumps(today, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
