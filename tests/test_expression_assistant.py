from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

from unittest.mock import patch

from love_moments.expression_assistant import ExpressionResult, OpenAICompatibleExpressionAssistant, RuleBasedExpressionAssistant, expression_assistant_from_env
from love_moments.intelligent_ui import ConflictError
from love_moments.store import DEMO_COUPLE, DEMO_PARTNER, DEMO_USER, LoveMomentsStore


class SpyExpressionProvider:
    name = "spy"
    model = "spy-model"
    is_external = True

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.error = error

    def suggest(self, text: str, *, mode: str, request_id: str) -> ExpressionResult:
        self.calls.append({"text": text, "mode": mode, "request_id": request_id})
        if self.error:
            raise self.error
        return ExpressionResult(
            text=f"建议：{text}", provider=self.name, model=self.model,
            usage={"input_tokens": 5, "output_tokens": 8, "total_tokens": 13},
        )


class BlockingExpressionProvider(SpyExpressionProvider):
    def __init__(self, *, error: Exception | None = None) -> None:
        super().__init__(error=error)
        self.started = threading.Event()
        self.release = threading.Event()

    def suggest(self, text: str, *, mode: str, request_id: str) -> ExpressionResult:
        self.calls.append({"text": text, "mode": mode, "request_id": request_id})
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("测试阻塞超时")
        if self.error:
            raise self.error
        return ExpressionResult(
            text=f"建议：{text}", provider=self.name, model=self.model,
            usage={"input_tokens": 5, "output_tokens": 8, "total_tokens": 13},
        )


class ExpressionAssistantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.provider = SpyExpressionProvider()
        self.store = LoveMomentsStore(
            Path(self.temp.name) / "expression.db",
            seed_demo_data=True,
            expression_assistant=self.provider,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_partner_message_is_rejected_before_provider_call(self) -> None:
        message = self.store.create_message({"content": "伴侣自己的话"}, DEMO_PARTNER)
        with self.assertRaises(PermissionError):
            self.store.suggest_expression(
                mode="gentle", message_id=message["id"], draft=None,
                idempotency_key="expression-key-0001", user_id=DEMO_USER,
            )
        self.assertEqual(self.provider.calls, [])

    def test_exactly_one_owned_text_source_and_allowed_mode_are_required(self) -> None:
        own = self.store.create_message({"content": "我自己的话"}, DEMO_USER)
        media = self.store.create_message({"type": "location", "content": "杭州"}, DEMO_USER)
        invalid = [
            {"mode": "gentle", "message_id": None, "draft": None},
            {"mode": "gentle", "message_id": own["id"], "draft": "重复来源"},
            {"mode": "judge", "message_id": own["id"], "draft": None},
            {"mode": "gentle", "message_id": media["id"], "draft": None},
        ]
        for index, values in enumerate(invalid):
            with self.subTest(values=values), self.assertRaises((ValueError, PermissionError)):
                self.store.suggest_expression(
                    **values, idempotency_key=f"expression-invalid-{index:04d}", user_id=DEMO_USER,
                )
        self.assertEqual(self.provider.calls, [])

    def test_provider_receives_only_selected_text_and_suggestion_never_sends(self) -> None:
        self.store.create_message({"content": "伴侣前文机密"}, DEMO_PARTNER)
        selected = self.store.create_message({"content": "我想更温和地说"}, DEMO_USER)
        self.store.create_message({"content": "伴侣后文机密"}, DEMO_PARTNER)
        with self.store.connection() as db:
            before = {
                "messages": db.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "events": db.execute("SELECT COUNT(*) FROM realtime_events").fetchone()[0],
                "notifications": db.execute("SELECT COUNT(*) FROM in_app_notifications").fetchone()[0],
                "audits": db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0],
            }
        result = self.store.suggest_expression(
            mode="needs", message_id=selected["id"], draft=None,
            idempotency_key="expression-key-0002", user_id=DEMO_USER,
        )
        self.assertEqual(self.provider.calls[0]["text"], "我想更温和地说")
        self.assertEqual(self.provider.calls[0]["mode"], "needs")
        self.assertNotIn("伴侣", json.dumps(self.provider.calls, ensure_ascii=False))
        self.assertEqual(result["text"], "建议：我想更温和地说")
        with self.store.connection() as db:
            after = {
                "messages": db.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "events": db.execute("SELECT COUNT(*) FROM realtime_events").fetchone()[0],
                "notifications": db.execute("SELECT COUNT(*) FROM in_app_notifications").fetchone()[0],
                "audits": db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0],
            }
        self.assertEqual(after, before)

    def test_replay_is_private_and_changed_payload_conflicts_without_second_call(self) -> None:
        first = self.store.suggest_expression(
            mode="summary", message_id=None, draft="只整理这一段",
            idempotency_key="expression-key-0003", user_id=DEMO_USER,
        )
        replay = self.store.suggest_expression(
            mode="summary", message_id=None, draft="只整理这一段",
            idempotency_key="expression-key-0003", user_id=DEMO_USER,
        )
        self.assertEqual(first, replay)
        self.assertEqual(len(self.provider.calls), 1)
        with self.assertRaises(ConflictError):
            self.store.suggest_expression(
                mode="gentle", message_id=None, draft="换一种请求",
                idempotency_key="expression-key-0003", user_id=DEMO_USER,
            )
        self.assertEqual(len(self.provider.calls), 1)

    def test_provider_failure_records_truthful_usage_without_text(self) -> None:
        failing = SpyExpressionProvider(error=TimeoutError("包含用户原文的超时细节"))
        store = LoveMomentsStore(
            Path(self.temp.name) / "failure.db", seed_demo_data=True,
            expression_assistant=failing,
        )
        with self.assertRaisesRegex(RuntimeError, "暂时不可用"):
            store.suggest_expression(
                mode="gentle", message_id=None, draft="失败时必须保留的草稿",
                idempotency_key="expression-key-0004", user_id=DEMO_USER,
            )
        with store.connection() as db:
            usage = dict(db.execute("SELECT * FROM ai_usage_events WHERE feature='expression.suggest'").fetchone())
        self.assertEqual(usage["status"], "timeout")
        self.assertEqual(usage["failure_code"], "timeout")
        self.assertEqual(usage["request_sent"], 1)
        self.assertNotIn("草稿", json.dumps(usage, ensure_ascii=False))
        self.assertNotIn("用户原文", json.dumps(usage, ensure_ascii=False))

    def test_provider_reuses_existing_server_only_llm_configuration(self) -> None:
        with patch.dict("os.environ", {
            "LLM_API_KEY": "test-key", "LLM_BASE_URL": "https://llm.example/v1",
            "LLM_MODEL": "test-model", "LLM_TIMEOUT_SECONDS": "17",
        }, clear=True):
            provider = expression_assistant_from_env()
        self.assertIsInstance(provider, OpenAICompatibleExpressionAssistant)
        self.assertEqual(provider.base_url, "https://llm.example/v1")
        self.assertEqual(provider.model, "test-model")
        self.assertEqual(provider.timeout, 17)

    def test_rule_fallback_does_not_create_external_ai_usage(self) -> None:
        store = LoveMomentsStore(
            Path(self.temp.name) / "rules.db", seed_demo_data=True,
            expression_assistant=RuleBasedExpressionAssistant(),
        )
        result = store.suggest_expression(
            mode="gentle", message_id=None, draft="这次只用本地规则",
            idempotency_key="expression-rules-0001", user_id=DEMO_USER,
        )
        self.assertEqual(result["provider"], "rules")
        with store.connection() as db:
            usage_count = db.execute(
                "SELECT COUNT(*) FROM ai_usage_events WHERE feature='expression.suggest'",
            ).fetchone()[0]
        self.assertEqual(usage_count, 0)

    def test_concurrent_exact_replay_has_one_provider_call_one_usage_and_same_result(self) -> None:
        provider = BlockingExpressionProvider()
        store = LoveMomentsStore(
            Path(self.temp.name) / "expression-concurrent.db", seed_demo_data=True,
            expression_assistant=provider,
        )
        results: list[dict] = []
        errors: list[Exception] = []

        def run() -> None:
            try:
                results.append(store.suggest_expression(
                    mode="gentle", draft="同一个并发表达", message_id=None,
                    idempotency_key="expression-concurrent-exact", user_id=DEMO_USER,
                ))
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        first = threading.Thread(target=run)
        second = threading.Thread(target=run)
        first.start()
        self.assertTrue(provider.started.wait(timeout=2))
        second.start()
        time.sleep(0.1)
        self.assertEqual(len(provider.calls), 1)
        provider.release.set()
        first.join(timeout=5)
        second.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(provider.calls), 1)
        with store.connection() as db:
            usages = db.execute(
                "SELECT status FROM ai_usage_events WHERE feature='expression.suggest'",
            ).fetchall()
        self.assertEqual([row["status"] for row in usages], ["succeeded"])

    def test_changed_payload_conflicts_immediately_while_exact_request_is_pending(self) -> None:
        provider = BlockingExpressionProvider()
        store = LoveMomentsStore(
            Path(self.temp.name) / "expression-pending-conflict.db", seed_demo_data=True,
            expression_assistant=provider,
        )
        owner_errors: list[Exception] = []

        def owner() -> None:
            try:
                store.suggest_expression(
                    mode="gentle", draft="原始请求", message_id=None,
                    idempotency_key="expression-pending-conflict", user_id=DEMO_USER,
                )
            except Exception as error:  # pragma: no cover - asserted below
                owner_errors.append(error)

        thread = threading.Thread(target=owner)
        thread.start()
        self.assertTrue(provider.started.wait(timeout=2))
        started = time.monotonic()
        with self.assertRaises(ConflictError):
            store.suggest_expression(
                mode="needs", draft="不同请求", message_id=None,
                idempotency_key="expression-pending-conflict", user_id=DEMO_USER,
            )
        self.assertLess(time.monotonic() - started, 1.0)
        provider.release.set()
        thread.join(timeout=5)
        self.assertEqual(owner_errors, [])
        self.assertEqual(len(provider.calls), 1)

    def test_waiting_exact_request_cannot_follow_actor_into_a_rebound_couple(self) -> None:
        provider = BlockingExpressionProvider()
        store = LoveMomentsStore(
            Path(self.temp.name) / "expression-rebind.db", seed_demo_data=True,
            expression_assistant=provider,
        )
        results: list[dict] = []
        errors: list[Exception] = []

        def run() -> None:
            try:
                results.append(store.suggest_expression(
                    mode="gentle", draft="关系变化后必须丢弃", message_id=None,
                    idempotency_key="expression-rebind-running", user_id=DEMO_USER,
                ))
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        owner = threading.Thread(target=run)
        waiter = threading.Thread(target=run)
        owner.start()
        self.assertTrue(provider.started.wait(timeout=2))
        waiter.start()
        time.sleep(0.1)
        store.unlink_couple("test123", "解除情侣", DEMO_USER)
        newcomer = store.register("newpartner", "FreshPass123!", "新伴侣")
        invite = store.create_user_invite(newcomer["id"])
        store.accept_user_invite(DEMO_USER, invite["code"], "2024-01-01")
        provider.release.set()
        owner.join(timeout=5)
        waiter.join(timeout=5)

        self.assertEqual(results, [])
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(isinstance(error, ConflictError) for error in errors))
        self.assertEqual(len(provider.calls), 1)
        with store.connection() as db:
            usages = db.execute(
                "SELECT status,failure_code FROM ai_usage_events WHERE feature='expression.suggest'",
            ).fetchall()
        self.assertEqual([(row["status"], row["failure_code"]) for row in usages], [("failed", "scope_changed")])

    def test_unlink_during_provider_call_finishes_usage_and_discards_result(self) -> None:
        provider = BlockingExpressionProvider()
        store = LoveMomentsStore(
            Path(self.temp.name) / "expression-unlink.db", seed_demo_data=True,
            expression_assistant=provider,
        )
        errors: list[Exception] = []

        def run() -> None:
            try:
                store.suggest_expression(
                    mode="summary", draft="解绑后不能返回", message_id=None,
                    idempotency_key="expression-unlink-running", user_id=DEMO_USER,
                )
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(provider.started.wait(timeout=2))
        store.unlink_couple("test123", "解除情侣", DEMO_USER)
        reopened = LoveMomentsStore(store.db_path, seed_demo_data=True, expression_assistant=provider)
        with reopened.connection() as db:
            usages_before_owner_returns = db.execute(
                "SELECT status,failure_code,request_sent FROM ai_usage_events WHERE feature='expression.suggest'",
            ).fetchall()
            pending_before_owner_returns = db.execute(
                "SELECT result_json FROM mutation_idempotency WHERE operation='expression.suggest'",
            ).fetchall()
        self.assertEqual(
            [(row["status"], row["failure_code"], row["request_sent"]) for row in usages_before_owner_returns],
            [("failed", "scope_changed", 1)],
        )
        self.assertEqual(pending_before_owner_returns, [])
        provider.release.set()
        thread.join(timeout=5)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ConflictError)
        with store.connection() as db:
            usages = db.execute(
                "SELECT status,failure_code,request_sent,latency_ms,input_tokens,output_tokens,total_tokens "
                "FROM ai_usage_events WHERE feature='expression.suggest'",
            ).fetchall()
            receipts = db.execute(
                "SELECT result_json FROM mutation_idempotency WHERE operation='expression.suggest'",
            ).fetchall()
        self.assertEqual([(row["status"], row["failure_code"]) for row in usages], [("failed", "scope_changed")])
        self.assertEqual(usages[0]["request_sent"], 1)
        self.assertIsNotNone(usages[0]["latency_ms"])
        self.assertEqual(
            (usages[0]["input_tokens"], usages[0]["output_tokens"], usages[0]["total_tokens"]),
            (5, 8, 13),
        )
        self.assertNotIn("建议：", json.dumps([row["result_json"] for row in receipts], ensure_ascii=False))

    def test_account_delete_during_provider_call_never_leaves_started_usage(self) -> None:
        provider = BlockingExpressionProvider()
        store = LoveMomentsStore(
            Path(self.temp.name) / "expression-delete.db", seed_demo_data=True,
            expression_assistant=provider,
        )
        errors: list[Exception] = []

        def run() -> None:
            try:
                store.suggest_expression(
                    mode="needs", draft="注销后不能返回", message_id=None,
                    idempotency_key="expression-delete-running", user_id=DEMO_USER,
                )
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(provider.started.wait(timeout=2))
        store.delete_account("test123", "注销账号", DEMO_USER)
        reopened = LoveMomentsStore(store.db_path, seed_demo_data=True, expression_assistant=provider)
        with reopened.connection() as db:
            usages_before_owner_returns = db.execute(
                "SELECT status,failure_code FROM ai_usage_events WHERE feature='expression.suggest'",
            ).fetchall()
            pending_before_owner_returns = db.execute(
                "SELECT result_json FROM mutation_idempotency WHERE operation='expression.suggest'",
            ).fetchall()
        self.assertEqual(
            [(row["status"], row["failure_code"]) for row in usages_before_owner_returns],
            [("failed", "scope_changed")],
        )
        self.assertEqual(pending_before_owner_returns, [])
        provider.release.set()
        thread.join(timeout=5)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ConflictError)
        with store.connection() as db:
            usages = db.execute(
                "SELECT status,failure_code FROM ai_usage_events WHERE feature='expression.suggest'",
            ).fetchall()
        self.assertEqual([(row["status"], row["failure_code"]) for row in usages], [("failed", "scope_changed")])

    def test_stale_pending_expression_is_recovered_without_permanent_started_usage(self) -> None:
        payload = {"mode": "gentle", "message_id": None, "draft": "恢复同一请求"}
        request_hash = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.store.record_ai_usage_event(
            actor_kind="user", feature="expression.suggest", provider="spy", model="spy-model",
            request_id="dead-request", status="started", request_sent=False, user_id=DEMO_USER,
        )
        with self.store.connection() as db:
            stale = "2000-01-01T00:00:00+00:00"
            db.execute(
                "UPDATE ai_usage_events SET started_at=? WHERE request_id='dead-request'", (stale,),
            )
            db.execute(
                "INSERT INTO mutation_idempotency(couple_id,user_id,operation,idempotency_key,result_json,created_at) "
                "VALUES (?,?,'expression.suggest','expression-stale-pending',?,?)",
                (DEMO_COUPLE, DEMO_USER, json.dumps({
                    "_state": "pending", "_request_hash": request_hash,
                    "_owner_token": "dead-owner", "_request_id": "dead-request", "_started_at": stale,
                }), stale),
            )
        result = self.store.suggest_expression(
            mode="gentle", draft="恢复同一请求", message_id=None,
            idempotency_key="expression-stale-pending", user_id=DEMO_USER,
        )
        self.assertEqual(result["text"], "建议：恢复同一请求")
        self.assertEqual(len(self.provider.calls), 1)
        with self.store.connection() as db:
            usage = db.execute(
                "SELECT request_id,status,failure_code,request_sent FROM ai_usage_events ORDER BY started_at",
            ).fetchall()
        self.assertNotIn("started", [row["status"] for row in usage])
        self.assertIn(("unknown", "process_interrupted"), [
            (row["status"], row["failure_code"]) for row in usage
        ])
        self.assertEqual(len(usage), 2)
        self.assertEqual({row["request_id"] for row in usage} & {"dead-request"}, {"dead-request"})
        replacement = next(row for row in usage if row["request_id"] != "dead-request")
        self.assertEqual((replacement["status"], replacement["request_sent"]), ("succeeded", 1))

    def test_stale_pending_with_different_payload_still_conflicts(self) -> None:
        with self.store.connection() as db:
            stale = "2000-01-01T00:00:00+00:00"
            db.execute(
                "INSERT INTO mutation_idempotency(couple_id,user_id,operation,idempotency_key,result_json,created_at) "
                "VALUES (?,?,'expression.suggest','expression-stale-conflict',?,?)",
                (DEMO_COUPLE, DEMO_USER, json.dumps({
                    "_state": "pending", "_request_hash": "another-payload",
                    "_owner_token": "dead-owner", "_request_id": "dead-request", "_started_at": stale,
                }), stale),
            )
        with self.assertRaises(ConflictError):
            self.store.suggest_expression(
                mode="gentle", draft="新请求", message_id=None,
                idempotency_key="expression-stale-conflict", user_id=DEMO_USER,
            )

    def test_many_exact_waiters_poll_read_only_without_starving_an_unrelated_writer(self) -> None:
        provider = BlockingExpressionProvider()
        store = LoveMomentsStore(
            Path(self.temp.name) / "expression-waiter-pressure.db", seed_demo_data=True,
            expression_assistant=provider,
        )
        original_connection = store.connection
        metrics_lock = threading.Lock()
        connection_count = 0
        immediate_count = 0

        @contextmanager
        def traced_connection():
            nonlocal connection_count, immediate_count
            with original_connection() as db:
                with metrics_lock:
                    connection_count += 1

                def trace(statement: str) -> None:
                    nonlocal immediate_count
                    if statement.strip().upper().startswith("BEGIN IMMEDIATE"):
                        with metrics_lock:
                            immediate_count += 1

                db.set_trace_callback(trace)
                yield db

        store.connection = traced_connection  # type: ignore[method-assign]
        results: list[dict] = []
        errors: list[Exception] = []

        def run() -> None:
            try:
                results.append(store.suggest_expression(
                    mode="summary", draft="大量等待者共享一次结果", message_id=None,
                    idempotency_key="expression-pressure-exact", user_id=DEMO_USER,
                ))
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        owner = threading.Thread(target=run)
        owner.start()
        self.assertTrue(provider.started.wait(timeout=2))
        waiters = [threading.Thread(target=run) for _ in range(12)]
        for waiter in waiters:
            waiter.start()
        time.sleep(0.4)
        writer_started = time.monotonic()
        store.set_status({"status_code": "busy", "duration_minutes": 30}, DEMO_PARTNER)
        writer_elapsed = time.monotonic() - writer_started
        provider.release.set()
        owner.join(timeout=5)
        for waiter in waiters:
            waiter.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 13)
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(provider.calls), 1)
        self.assertLess(writer_elapsed, 1.0)
        self.assertLessEqual(immediate_count, 2)
        self.assertLessEqual(connection_count, 100)

    def test_exact_waiter_replays_the_same_safe_failure_as_owner(self) -> None:
        provider = BlockingExpressionProvider(error=TimeoutError("外部超时细节"))
        store = LoveMomentsStore(
            Path(self.temp.name) / "expression-waiter-failure.db", seed_demo_data=True,
            expression_assistant=provider,
        )
        errors: list[Exception] = []

        def run() -> None:
            try:
                store.suggest_expression(
                    mode="needs", draft="失败也只调用一次", message_id=None,
                    idempotency_key="expression-failure-exact", user_id=DEMO_USER,
                )
            except Exception as error:
                errors.append(error)

        owner = threading.Thread(target=run)
        waiter = threading.Thread(target=run)
        owner.start()
        self.assertTrue(provider.started.wait(timeout=2))
        waiter.start()
        time.sleep(0.1)
        provider.release.set()
        owner.join(timeout=5)
        waiter.join(timeout=5)

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(errors), 2)
        self.assertEqual([(type(error), str(error)) for error in errors], [
            (RuntimeError, "表达辅助暂时不可用，原草稿已保留，请稍后重试"),
            (RuntimeError, "表达辅助暂时不可用，原草稿已保留，请稍后重试"),
        ])
        with store.connection() as db:
            usages = db.execute(
                "SELECT status,failure_code FROM ai_usage_events WHERE feature='expression.suggest'",
            ).fetchall()
        self.assertEqual([(row["status"], row["failure_code"]) for row in usages], [("timeout", "timeout")])


if __name__ == "__main__":
    unittest.main()
