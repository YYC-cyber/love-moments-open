from __future__ import annotations

import base64
import calendar as calendar_module
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .providers import DatePlannerProvider, LocalDatePlanner, LocalTravelPlanner, TravelPlannerProvider
from .phase_three import BROAD_DESTINATIONS, PhaseThreeMixin
from .storage import LocalMediaStorage, MediaStorage
from .schema import SCHEMA_SQL, SCHEMA_VERSION, V34_SCHEMA_MARKER, schema_inventory
from .intelligent_ui import (
    COMPONENTS_BY_SURFACE,
    SURFACES,
    ConflictError,
    RuleBasedUiComposer,
    UiComposer,
    validate_component_order,
    validate_manifest,
)
from .media_security import sanitize_image
from .meals import MealRecipeMixin
from .shared_content import SharedContentMixin
from .quota import QuotaExceededError, period_values, quota_policies
from .daily_questions import daily_question_for
from .maps import map_provider_from_env
from .travel_assistant import TravelAssistantProvider, travel_assistant_from_env
from .travel_assistant_store import TravelAssistantMixin
from .notifications import NotificationMixin
from .life_assistant import LifeAssistantProvider, RuleBasedLifeAssistant
from .life_assistant_store import LifeAssistantMixin
from .daily_summary import DailySummaryMixin
from .provenance import ProvenanceMixin
from .couple_interactions import CoupleInteractionMixin
from .expression_assistant import ExpressionAssistant, MODES, expression_assistant_from_env
from .kitchen import KitchenMixin
from .travel_tools import TravelToolsMixin
from .memories import DATE_SOURCE_LABELS, SOURCE_LABELS, MemoryMixin
from .future_letters import FutureLetterMixin
from .album_exports import AlbumExportMixin


DEMO_USER = "demo-user-a"
DEMO_PARTNER = "demo-user-b"
DEMO_COUPLE = "demo-couple"
DEMO_TRIP = "demo-trip"
DEMO_PASSWORD = "test123"
PASSWORD_ITERATIONS = 310_000


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def next_annual_occurrence(original: date, today: date | None = None) -> date:
    """Return the next occurrence, treating Feb 29 as Feb 28 in non-leap years."""
    reference = today or date.today()

    def occurrence(year: int) -> date:
        try:
            return original.replace(year=year)
        except ValueError:
            return date(year, 2, 28)

    candidate = occurrence(reference.year)
    return candidate if candidate >= reference else occurrence(reference.year + 1)


def parse_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(actual, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False


def parse_iso_datetime(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field_name}必须是有效的 ISO 日期时间") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


DUMMY_PASSWORD_HASH = hash_password("invalid-password-placeholder", b"\0" * 16)


def validate_new_password(password: str, username: str = "") -> None:
    if len(password) < 10 or len(password) > 128:
        raise ValueError("密码长度需为 10–128 位")
    lowered = password.casefold()
    if lowered in {"12345678", "1234567890", "password123", "love1234", "qwerty12345"}:
        raise ValueError("密码过于常见，请换一个更安全的密码")
    if username and username.casefold() in lowered:
        raise ValueError("密码不能包含账号名")
    classes = sum(bool(re.search(pattern, password)) for pattern in (r"[A-Za-z]", r"\d", r"[^A-Za-z0-9]"))
    if classes < 2:
        raise ValueError("密码需组合字母、数字或符号中的至少两类")


class LoveMomentsStore(
    DailySummaryMixin,
    AlbumExportMixin,
    MemoryMixin,
    FutureLetterMixin,
    ProvenanceMixin,
    CoupleInteractionMixin,
    LifeAssistantMixin,
    NotificationMixin,
    TravelAssistantMixin,
    SharedContentMixin,
    KitchenMixin,
    TravelToolsMixin,
    MealRecipeMixin,
    PhaseThreeMixin,
):
    def __init__(
        self,
        db_path: str | Path,
        planner: TravelPlannerProvider | None = None,
        date_planner: DatePlannerProvider | None = None,
        ui_composer: UiComposer | None = None,
        media_root: str | Path | None = None,
        storage: MediaStorage | None = None,
        travel_assistant: TravelAssistantProvider | None = None,
        life_assistant: LifeAssistantProvider | None = None,
        expression_assistant: ExpressionAssistant | None = None,
        seed_demo_data: bool = False,
    ):
        self.db_path = str(db_path)
        self.planner = planner or LocalTravelPlanner()
        self.date_planner = date_planner or LocalDatePlanner()
        self.ui_composer = ui_composer or RuleBasedUiComposer()
        self.travel_assistant = travel_assistant or travel_assistant_from_env()
        self._hotel_destination_query_cache: dict[tuple[str, str], str] = {}
        self._hotel_destination_query_cache_lock = threading.Lock()
        self._hotel_name_localization_cache: dict[tuple[str, str], str] = {}
        self._hotel_name_localization_cache_lock = threading.Lock()
        # Exact coordinates never leave memory.  The short-lived, rounded-key
        # cache prevents two partners refreshing the same live panel from
        # spending two map requests for identical nearby restaurants.
        self._live_restaurant_cache: dict[tuple[str, float, float, str], dict[str, Any]] = {}
        self._live_restaurant_cache_lock = threading.Lock()
        self.life_assistant_provider = life_assistant or RuleBasedLifeAssistant()
        self.expression_assistant = expression_assistant or expression_assistant_from_env()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.media_root = Path(media_root) if media_root else Path(self.db_path).parent / "media"
        for folder in ("images", "audio", "videos", "thumbnails", "temp"):
            (self.media_root / folder).mkdir(parents=True, exist_ok=True)
        self.album_export_root = self.media_root / "temp" / "album_exports"
        self.album_export_root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.album_export_root.chmod(0o700)
        self._album_artifact_lock = threading.RLock()
        self.local_storage = LocalMediaStorage(self.media_root)
        self.storage = storage or self.local_storage
        self._media_signing_key = self._load_media_signing_key()
        self.migrate()
        self.recover_stale_ai_usage_events()
        self._recover_life_assistant_jobs_after_restart()
        self._recover_ui_generation_jobs_after_restart()
        self._seed_builtin_recipes()
        self._configure_external_request_guards()
        if hasattr(self.travel_assistant, "before_request"):
            self.travel_assistant.before_request = self.external_request_guard("llm")
        if hasattr(self.life_assistant_provider, "before_request"):
            self.life_assistant_provider.before_request = self.external_request_guard(
                "llm"
            )
        self._import_legacy_serpapi_usage()
        if seed_demo_data:
            self.seed_demo()
        self._ensure_minimum_plan_choices()
        self._repair_legacy_travel_templates()
        self._harden_local_permissions()

    def _recover_ui_generation_jobs_after_restart(self) -> int:
        """Close orphaned UI jobs without storing or replaying private intents."""
        now = utc_now()
        with self.connection() as db:
            return int(
                db.execute(
                    """
                    UPDATE ui_generation_jobs
                    SET status='failed', error_code='SERVER_RESTARTED', completed_at=?
                    WHERE status IN ('queued','running') AND completed_at IS NULL
                    """,
                    (now,),
                ).rowcount
            )

    def _ensure_minimum_plan_choices(self) -> None:
        """Add newly introduced plan styles without replacing existing edits."""
        local_travel, local_date = LocalTravelPlanner(), LocalDatePlanner()
        with self.connection() as db:
            trips = db.execute("SELECT id,destination,budget_cents,start_date,end_date FROM trips WHERE deleted_at IS NULL").fetchall()
            for trip in trips:
                existing = {row["style"] for row in db.execute("SELECT style FROM plan_variants WHERE trip_id=?", (trip["id"],)).fetchall()}
                context = {"start_date": trip["start_date"], "end_date": trip["end_date"]}
                missing = [plan for plan in local_travel.generate_variants(trip["destination"], int(trip["budget_cents"]), context) if plan.style not in existing]
                if missing:
                    self._insert_generated_plans(db, trip["id"], trip["destination"], int(trip["budget_cents"]), missing)
            date_plans = db.execute("SELECT * FROM date_plans").fetchall()
            for plan in date_plans:
                existing = {row["style"] for row in db.execute("SELECT style FROM date_plan_variants WHERE date_plan_id=?", (plan["id"],)).fetchall()}
                generated = local_date.generate_variants({"city": plan["city"], "budget_cents": plan["budget_cents"], "start_time": plan["start_time"]})
                for value in generated:
                    if value["style"] in existing:
                        continue
                    db.execute(
                        "INSERT INTO date_plan_variants(id,date_plan_id,style,name,summary,expected_cost_cents,intensity,weather_fit,items_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (new_id("date_variant"), plan["id"], value["style"], value["name"], value["summary"], value["expected_cost_cents"], value["intensity"], value["weather_fit"], json.dumps(value["items"], ensure_ascii=False), utc_now()),
                    )

    def _repair_legacy_travel_templates(self) -> None:
        """Replace old fabricated POIs with honest destination-aware placeholders.

        Exact-title matching keeps user-created items untouched. Locked items are
        also preserved because they represent an explicit manual decision.
        """
        legacy_titles = {
            "北山街散步", "曲院风荷", "茶人村午餐", "茅家埠日落",
            "断桥残雪", "灵隐寺", "龙井村", "雷峰塔", "苏堤春晓",
            "浙江省博物馆", "河坊街", "九溪烟树",
            # Early offline variants also used these generic labels as though
            # they were real POIs.  They are not verifiable place names.
            "城市自然公园", "山野观景台", "湖畔慢行", "日落约会点",
        }
        placeholders = {
            "slow": "周边慢游地点待确认",
            "classic": "当地代表地点待确认",
            "value": "免费或低价地点待确认",
            "nature": "附近自然地点待确认",
        }
        with self.connection() as db:
            rows = db.execute(
                "SELECT i.id,i.title,i.note,v.style,t.destination FROM itinerary_items i "
                "JOIN trip_days d ON d.id=i.day_id JOIN plan_variants v ON v.id=d.variant_id "
                "JOIN trips t ON t.id=v.trip_id WHERE i.locked=0 AND t.deleted_at IS NULL"
            ).fetchall()
            for row in rows:
                if "杭州" in str(row["destination"]) or row["title"] not in legacy_titles:
                    continue
                replacement = f"{row['destination']} · {placeholders.get(row['style'], '真实地点待确认')}"
                note = "已移除旧版本中未经核实的示例地点；请搜索、重新生成或手动填写真实地点。"
                db.execute("UPDATE itinerary_items SET title=?,note=? WHERE id=?", (replacement, note, row["id"]))

    def _load_media_signing_key(self) -> bytes:
        configured = os.getenv("MEDIA_SIGNING_SECRET", "").strip()
        if configured:
            if len(configured) < 32:
                raise ValueError("MEDIA_SIGNING_SECRET 至少需要 32 个字符")
            return hashlib.sha256(configured.encode("utf-8")).digest()
        key_path = self.media_root / ".media-access-key"
        try:
            encoded = key_path.read_text(encoding="ascii").strip()
            key = base64.urlsafe_b64decode(encoded.encode("ascii"))
            if len(key) == 32:
                return key
        except (OSError, ValueError):
            pass
        key = secrets.token_bytes(32)
        key_path.write_text(base64.urlsafe_b64encode(key).decode("ascii"), encoding="ascii")
        if os.name != "nt":
            key_path.chmod(0o600)
        return key

    def _configure_external_request_guards(self) -> None:
        """Attach the shared LLM allowance to every remote model client."""
        guard = self.external_request_guard("llm")
        for provider in (self.planner, self.date_planner, self.ui_composer):
            if hasattr(provider, "before_request"):
                provider.before_request = guard

    def external_request_guard(self, provider: str):
        return lambda operation="request": self.reserve_external_api(provider, operation=operation)

    def reserve_external_api(self, provider: str, *, units: int = 1, operation: str = "request") -> dict:
        """Atomically reserve units immediately before a real network request."""
        policies = quota_policies()
        if provider not in policies:
            raise ValueError("未知的外部接口额度")
        policy = policies[provider]
        period_key, period_label, resets_at = period_values(policy.period)
        safe_units = max(1, min(int(units), 1000))
        now = utc_now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT used_count FROM external_api_usage WHERE provider=? AND period_key=?",
                (provider, period_key),
            ).fetchone()
            used = int(row["used_count"]) if row else 0
            if policy.limit is not None and (policy.limit <= 0 or used + safe_units > policy.limit):
                raise QuotaExceededError(
                    provider=provider, label=policy.label, used=used, limit=policy.limit,
                    period_label=period_label, resets_at=resets_at,
                )
            used += safe_units
            db.execute(
                """
                INSERT INTO external_api_usage(provider,period_type,period_key,used_count,limit_count,last_operation,updated_at)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(provider,period_key) DO UPDATE SET
                    period_type=excluded.period_type,used_count=excluded.used_count,
                    limit_count=excluded.limit_count,last_operation=excluded.last_operation,updated_at=excluded.updated_at
                """,
                (provider, policy.period, period_key, used, policy.limit or 0, str(operation)[:120], now),
            )
        return self.external_api_quota(provider)

    def reserve_external_apis(self, providers: list[str], *, units: int = 1, operation: str = "request") -> list[dict]:
        """Reserve several related limits in one transaction or reserve none."""
        policies = quota_policies()
        unique_providers = list(dict.fromkeys(str(provider) for provider in providers))
        if not unique_providers or any(provider not in policies for provider in unique_providers):
            raise ValueError("未知的外部接口额度")
        safe_units = max(1, min(int(units), 1000))
        now = utc_now()
        pending: list[tuple[str, Any, str, str, str | None, int]] = []
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for provider in unique_providers:
                policy = policies[provider]
                period_key, period_label, resets_at = period_values(policy.period)
                row = db.execute(
                    "SELECT used_count FROM external_api_usage WHERE provider=? AND period_key=?",
                    (provider, period_key),
                ).fetchone()
                used = int(row["used_count"]) if row else 0
                if policy.limit is not None and (policy.limit <= 0 or used + safe_units > policy.limit):
                    raise QuotaExceededError(
                        provider=provider, label=policy.label, used=used, limit=policy.limit,
                        period_label=period_label, resets_at=resets_at,
                    )
                pending.append((provider, policy, period_key, period_label, resets_at, used + safe_units))
            for provider, policy, period_key, _, _, used in pending:
                db.execute(
                    """
                    INSERT INTO external_api_usage(provider,period_type,period_key,used_count,limit_count,last_operation,updated_at)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(provider,period_key) DO UPDATE SET
                        period_type=excluded.period_type,used_count=excluded.used_count,
                        limit_count=excluded.limit_count,last_operation=excluded.last_operation,updated_at=excluded.updated_at
                    """,
                    (provider, policy.period, period_key, used, policy.limit or 0, str(operation)[:120], now),
                )
        return [self.external_api_quota(provider) for provider in unique_providers]

    def external_api_quota(self, provider: str) -> dict:
        policies = quota_policies()
        if provider not in policies:
            raise ValueError("未知的外部接口额度")
        policy = policies[provider]
        period_key, period_label, resets_at = period_values(policy.period)
        with self.connection() as db:
            row = db.execute(
                "SELECT used_count,last_operation,updated_at FROM external_api_usage WHERE provider=? AND period_key=?",
                (provider, period_key),
            ).fetchone()
        used = int(row["used_count"]) if row else 0
        unlimited = policy.limit is None
        remaining = None if unlimited else max(0, policy.limit - used)
        ratio = 0.0 if unlimited else used / policy.limit if policy.limit else 1.0
        return {
            "provider": provider, "label": policy.label, "period": policy.period,
            "period_key": period_key, "period_label": period_label, "used": used,
            "limit": policy.limit, "remaining": remaining, "ratio": round(ratio, 4),
            "unlimited": unlimited,
            "warning": False if unlimited else ratio >= 0.8,
            "exhausted": False if unlimited else remaining == 0,
            "resets_at": resets_at, "last_operation": row["last_operation"] if row else "",
            "updated_at": row["updated_at"] if row else None,
        }

    def external_api_quotas(self) -> list[dict]:
        return [self.external_api_quota(provider) for provider in quota_policies()]

    def _import_legacy_serpapi_usage(self) -> None:
        """Carry the old JSON counter forward so an upgrade cannot regain calls."""
        raw_path = Path(os.getenv("SERPAPI_USAGE_PATH", "data/serpapi-usage.json"))
        path = raw_path if raw_path.is_absolute() else Path(__file__).resolve().parent.parent / raw_path
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            count = max(0, int(value.get("count", 0)))
            month = str(value.get("month", ""))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return
        policy = quota_policies()["serpapi"]
        period_key, _, _ = period_values("month")
        if month != period_key or count <= 0:
            return
        with self.connection() as db:
            db.execute(
                """
                INSERT INTO external_api_usage(provider,period_type,period_key,used_count,limit_count,last_operation,updated_at)
                VALUES ('serpapi','month',?,?,?,'legacy import',?)
                ON CONFLICT(provider,period_key) DO UPDATE SET
                    used_count=MAX(external_api_usage.used_count,excluded.used_count),
                    limit_count=excluded.limit_count,updated_at=excluded.updated_at
                """,
                (period_key, count, policy.limit, utc_now()),
            )
    def _harden_local_permissions(self) -> None:
        """Keep the self-hosted database and local media private to the owner."""
        if os.name == "nt":
            return
        roots = [Path(self.db_path).parent, self.media_root]
        paths = roots + [path for root in roots for path in root.rglob("*") if path.is_dir()]
        for path in paths:
            try:
                path.chmod(0o700)
            except OSError:
                pass
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.db_path}{suffix}")
            if path.is_file():
                try:
                    path.chmod(0o600)
                except OSError:
                    pass
        for path in self.media_root.rglob("*"):
            if path.is_file():
                try:
                    path.chmod(0o600)
                except OSError:
                    pass

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        with self.connection() as db:
            existing_version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if existing_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库版本 {existing_version} 高于当前程序支持的 {SCHEMA_VERSION}，已拒绝写入"
                )
            # Journal mode is a database-level setting. Changing it for every
            # request can race when both partners log in at the same time.
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = NORMAL")
            base_schema, v34_schema = SCHEMA_SQL.split(V34_SCHEMA_MARKER, 1)
            db.executescript(base_schema)
            # executescript commits before a script, so begin explicitly in
            # the script that creates the v34 tables.  It intentionally has no
            # COMMIT: all new tables, guarded ALTERs, sidecar backfills,
            # verification and version stamping share this transaction.
            db.executescript(f"BEGIN IMMEDIATE;\n{v34_schema}")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()}
            if "username" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN username TEXT")
            if "password_hash" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
            if "deleted_at" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN deleted_at TEXT")
            session_columns = {row["name"] for row in db.execute("PRAGMA table_info(sessions)").fetchall()}
            for name, definition in {
                "device_name": "TEXT NOT NULL DEFAULT '未知设备'",
                "user_agent": "TEXT NOT NULL DEFAULT ''",
                "ip_address": "TEXT NOT NULL DEFAULT ''",
                "revoked_at": "TEXT",
            }.items():
                if name not in session_columns:
                    db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")
            call_columns = {row["name"] for row in db.execute("PRAGMA table_info(call_records)").fetchall()}
            if "callee_id" not in call_columns:
                db.execute("ALTER TABLE call_records ADD COLUMN callee_id TEXT REFERENCES users(id)")
            if "updated_at" not in call_columns:
                db.execute("ALTER TABLE call_records ADD COLUMN updated_at TEXT")
            moment_columns = {row["name"] for row in db.execute("PRAGMA table_info(moments)").fetchall()}
            if "source_message_id" not in moment_columns:
                db.execute("ALTER TABLE moments ADD COLUMN source_message_id TEXT REFERENCES messages(id)")
            if "purged_at" not in moment_columns:
                db.execute("ALTER TABLE moments ADD COLUMN purged_at TEXT")
            if "media_id" not in moment_columns:
                db.execute("ALTER TABLE moments ADD COLUMN media_id TEXT REFERENCES media_assets(id)")
            trip_columns = {row["name"] for row in db.execute("PRAGMA table_info(trips)").fetchall()}
            for name, definition in {
                "created_at": "TEXT",
                "updated_at": "TEXT",
                "departure_city": "TEXT NOT NULL DEFAULT ''",
                "arrival_city": "TEXT NOT NULL DEFAULT ''",
                "outbound_time": "TEXT",
                "return_time": "TEXT",
                "travelers": "INTEGER NOT NULL DEFAULT 2",
                "rooms": "INTEGER NOT NULL DEFAULT 1",
                "hotel_budget_cents": "INTEGER NOT NULL DEFAULT 0",
                "creation_job_id": "TEXT",
                "deleted_at": "TEXT",
                "deleted_by": "TEXT REFERENCES users(id)",
            }.items():
                if name not in trip_columns:
                    db.execute(f"ALTER TABLE trips ADD COLUMN {name} {definition}")
            db.execute("UPDATE trips SET created_at=COALESCE(created_at, CURRENT_TIMESTAMP), updated_at=COALESCE(updated_at, CURRENT_TIMESTAMP)")
            broad = tuple(sorted(BROAD_DESTINATIONS))
            placeholders = ",".join("?" for _ in broad)
            db.execute(
                f"UPDATE trips SET arrival_city=destination WHERE COALESCE(arrival_city,'')='' AND destination NOT IN ({placeholders}) AND destination NOT LIKE '%省' AND destination NOT LIKE '%自治区'",
                broad,
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_trips_couple_active ON trips(couple_id, deleted_at, start_date DESC)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trips_creation_job ON trips(creation_job_id) WHERE creation_job_id IS NOT NULL")
            plan_variant_columns = {row["name"] for row in db.execute("PRAGMA table_info(plan_variants)").fetchall()}
            if "generation_source" not in plan_variant_columns:
                db.execute("ALTER TABLE plan_variants ADD COLUMN generation_source TEXT NOT NULL DEFAULT 'legacy'")
            if "generation_message" not in plan_variant_columns:
                db.execute("ALTER TABLE plan_variants ADD COLUMN generation_message TEXT NOT NULL DEFAULT ''")
            itinerary_columns = {row["name"] for row in db.execute("PRAGMA table_info(itinerary_items)").fetchall()}
            if "details_json" not in itinerary_columns:
                db.execute("ALTER TABLE itinerary_items ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}'")
            db.execute(
                "UPDATE plan_variants SET generation_source='rules', "
                "generation_message='这套历史方案由本地安全模板生成；可点击重新生成再次调用大模型。' "
                "WHERE summary LIKE '本地安全模板%' AND generation_source='legacy'"
            )
            db.execute(
                "UPDATE itinerary_items SET note='此项是可编辑占位，不代表真实店名或景点；请搜索真实地点后替换。' "
                "WHERE note='未调用模型时不虚构店名或景点；请搜索真实地点后替换本项。'"
            )
            media_columns = {row["name"] for row in db.execute("PRAGMA table_info(media_assets)").fetchall()}
            for name, definition in {
                "storage_provider": "TEXT NOT NULL DEFAULT 'local'",
                "storage_key": "TEXT",
                "checksum": "TEXT",
                "thumbnail_checksum": "TEXT",
                "width": "INTEGER",
                "height": "INTEGER",
                "metadata_stripped": "INTEGER NOT NULL DEFAULT 0",
                "reveal_at": "TEXT",
                "deleted_at": "TEXT",
                "purged_at": "TEXT",
                "version": "INTEGER NOT NULL DEFAULT 1",
                "captured_at": "TEXT",
                "date_source": "TEXT NOT NULL DEFAULT 'legacy_unknown' CHECK(date_source IN ('legacy_unknown','user_confirmed','media_capture','trip_date','system_provisional'))",
                "source_type": "TEXT NOT NULL DEFAULT 'legacy_unknown' CHECK(source_type IN ('human','real_api','rules','ai','mock','cache','legacy_unknown'))",
                "duration_ms": "INTEGER CHECK(duration_ms IS NULL OR duration_ms >= 0)",
            }.items():
                if name not in media_columns:
                    db.execute(f"ALTER TABLE media_assets ADD COLUMN {name} {definition}")
            db.execute("UPDATE media_assets SET storage_key=COALESCE(storage_key, relative_path), storage_provider=COALESCE(storage_provider, 'local')")
            upload_columns = {row["name"] for row in db.execute("PRAGMA table_info(upload_sessions)").fetchall()}
            for name, definition in {
                "visibility": "TEXT NOT NULL DEFAULT 'shared'",
                "expected_checksum": "TEXT",
                "chunk_size": "INTEGER NOT NULL DEFAULT 4194304",
                "thumbnail_temp_path": "TEXT",
                "media_id": "TEXT REFERENCES media_assets(id)",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in upload_columns:
                    db.execute(f"ALTER TABLE upload_sessions ADD COLUMN {name} {definition}")
            db.execute("UPDATE upload_sessions SET updated_at=COALESCE(NULLIF(updated_at,''), created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_upload_sessions_owner_status ON upload_sessions(owner_id, status, expires_at)")
            quote_columns = {row["name"] for row in db.execute("PRAGMA table_info(quote_snapshots)").fetchall()}
            if "selected_at" not in quote_columns:
                db.execute("ALTER TABLE quote_snapshots ADD COLUMN selected_at TEXT")
            if "selected_by" not in quote_columns:
                db.execute("ALTER TABLE quote_snapshots ADD COLUMN selected_by TEXT REFERENCES users(id)")
            if "details_json" not in quote_columns:
                db.execute("ALTER TABLE quote_snapshots ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}'")
            assistant_draft_sql_row = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='trip_assistant_action_drafts'"
            ).fetchone()
            assistant_draft_sql = str(assistant_draft_sql_row["sql"] or "") if assistant_draft_sql_row else ""
            assistant_draft_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(trip_assistant_action_drafts)").fetchall()
            }
            if "day_replan" not in assistant_draft_sql or "expected_item_versions_json" not in assistant_draft_columns:
                db.execute("ALTER TABLE trip_assistant_action_drafts RENAME TO trip_assistant_action_drafts_v30")
                db.execute(
                    """CREATE TABLE trip_assistant_action_drafts (
                        id TEXT PRIMARY KEY,
                        trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
                        couple_id TEXT NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
                        creator_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        action_type TEXT NOT NULL CHECK(action_type IN ('itinerary_change','day_replan')),
                        payload_json TEXT NOT NULL,
                        preview_json TEXT NOT NULL,
                        expected_trip_version INTEGER NOT NULL,
                        expected_item_version INTEGER NOT NULL,
                        expected_item_versions_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','confirmed','cancelled','expired')),
                        idempotency_key TEXT,
                        result_json TEXT,
                        expires_at TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        confirmed_at TEXT,
                        UNIQUE(couple_id, idempotency_key)
                    )"""
                )
                old_columns = {
                    row["name"] for row in db.execute("PRAGMA table_info(trip_assistant_action_drafts_v30)").fetchall()
                }
                versions_expression = "expected_item_versions_json" if "expected_item_versions_json" in old_columns else "'{}'"
                db.execute(
                    f"""INSERT INTO trip_assistant_action_drafts(
                        id,trip_id,couple_id,creator_id,action_type,payload_json,preview_json,
                        expected_trip_version,expected_item_version,expected_item_versions_json,status,
                        idempotency_key,result_json,expires_at,created_at,confirmed_at
                    ) SELECT id,trip_id,couple_id,creator_id,action_type,payload_json,preview_json,
                        expected_trip_version,expected_item_version,{versions_expression},status,
                        idempotency_key,result_json,expires_at,created_at,confirmed_at
                    FROM trip_assistant_action_drafts_v30"""  # noqa: S608
                )
                db.execute("DROP TABLE trip_assistant_action_drafts_v30")
                db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_trip_assistant_drafts_owner "
                    "ON trip_assistant_action_drafts(trip_id, creator_id, status, expires_at)"
                )
            food_columns = {row["name"] for row in db.execute("PRAGMA table_info(trip_food_recommendations)").fetchall()}
            if "distance_meters" not in food_columns:
                db.execute("ALTER TABLE trip_food_recommendations ADD COLUMN distance_meters INTEGER")
            if "search_area" not in food_columns:
                db.execute("ALTER TABLE trip_food_recommendations ADD COLUMN search_area TEXT NOT NULL DEFAULT ''")
            # Search engines sometimes return an old http URL for otherwise
            # valid public result pages. Keep persisted evidence encrypted in
            # transit when the platform supports HTTPS (all allowlisted hosts).
            db.execute("UPDATE trip_food_review_evidence SET url='https://' || substr(url,8) WHERE url LIKE 'http://%'")
            ingredient_columns = {row["name"] for row in db.execute("PRAGMA table_info(recipe_ingredients)").fetchall()}
            for name, definition in {
                "preparation": "TEXT NOT NULL DEFAULT ''",
                "ingredient_group": "TEXT NOT NULL DEFAULT 'main'",
            }.items():
                if name not in ingredient_columns:
                    db.execute(f"ALTER TABLE recipe_ingredients ADD COLUMN {name} {definition}")
            step_columns = {row["name"] for row in db.execute("PRAGMA table_info(recipe_steps)").fetchall()}
            for name, definition in {
                "title": "TEXT NOT NULL DEFAULT ''",
                "heat_level": "TEXT NOT NULL DEFAULT ''",
                "key_point": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in step_columns:
                    db.execute(f"ALTER TABLE recipe_steps ADD COLUMN {name} {definition}")
            recipe_columns = {row["name"] for row in db.execute("PRAGMA table_info(recipes)").fetchall()}
            if "passive_minutes" not in recipe_columns:
                db.execute("ALTER TABLE recipes ADD COLUMN passive_minutes INTEGER NOT NULL DEFAULT 0")
            for name, definition in {
                "dish_id": "TEXT REFERENCES recipe_dishes(id)",
                "variant_name": "TEXT NOT NULL DEFAULT '经典做法'",
                "cooking_method": "TEXT NOT NULL DEFAULT ''",
                "method_tags_json": "TEXT NOT NULL DEFAULT '[]'",
            }.items():
                if name not in recipe_columns:
                    db.execute(f"ALTER TABLE recipes ADD COLUMN {name} {definition}")
            # Existing recipes become the first concrete method of a dish.
            # The deterministic id keeps repeated upgrades idempotent and lets
            # system recipes with the same canonical name share a group.
            for recipe in db.execute(
                "SELECT id,couple_id,owner_id,name,normalized_name,cuisine,category,visibility,created_at,updated_at,dish_id FROM recipes"
            ).fetchall():
                if recipe["dish_id"]:
                    continue
                scope = recipe["couple_id"] or recipe["owner_id"] or "system"
                digest = hashlib.sha256(
                    f"{scope}:{recipe['cuisine']}:{recipe['normalized_name']}".encode("utf-8")
                ).hexdigest()[:24]
                dish_id = f"dish_{digest}"
                db.execute(
                    """
                    INSERT OR IGNORE INTO recipe_dishes(
                        id,couple_id,owner_id,canonical_name,normalized_name,cuisine,category,visibility,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        dish_id, recipe["couple_id"], recipe["owner_id"], recipe["name"], recipe["normalized_name"],
                        recipe["cuisine"], recipe["category"], recipe["visibility"],
                        recipe["created_at"] or utc_now(), recipe["updated_at"] or utc_now(),
                    ),
                )
                db.execute("UPDATE recipes SET dish_id=? WHERE id=?", (dish_id, recipe["id"]))
            db.execute("CREATE INDEX IF NOT EXISTS idx_recipes_dish ON recipes(dish_id, deleted_at, updated_at DESC)")
            recipe_media_columns = {row["name"] for row in db.execute("PRAGMA table_info(recipe_media)").fetchall()}
            if "match_details_json" not in recipe_media_columns:
                db.execute("ALTER TABLE recipe_media ADD COLUMN match_details_json TEXT NOT NULL DEFAULT '{}'")
            db.execute("UPDATE moments SET media_id=substr(media_url, length('/api/v1/media/') + 1) WHERE media_id IS NULL AND media_url LIKE '/api/v1/media/media_%' AND EXISTS (SELECT 1 FROM media_assets WHERE id=substr(moments.media_url, length('/api/v1/media/') + 1))")
            db.execute("CREATE INDEX IF NOT EXISTS idx_media_assets_couple_status ON media_assets(couple_id, status, created_at DESC)")
            db.execute("UPDATE call_records SET updated_at=COALESCE(updated_at, ended_at, answered_at, started_at)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token_hash)")

            grant_columns = {row["name"] for row in db.execute("PRAGMA table_info(ai_context_grants)").fetchall()}
            for name, definition in {
                "revoked_at": "TEXT",
                "last_used_at": "TEXT",
                "resource_version": "INTEGER CHECK(resource_version IS NULL OR resource_version >= 1)",
                "usage_scope": "TEXT NOT NULL DEFAULT 'legacy_unknown' CHECK(usage_scope IN ('legacy_unknown','selected_content'))",
                "request_id": "TEXT",
                "version": "INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1)",
            }.items():
                if name not in grant_columns:
                    db.execute(f"ALTER TABLE ai_context_grants ADD COLUMN {name} {definition}")

            calendar_columns = {row["name"] for row in db.execute("PRAGMA table_info(calendar_entries)").fetchall()}
            for name, definition in {
                "participant_a_id": "TEXT REFERENCES users(id)",
                "participant_b_id": "TEXT REFERENCES users(id) CHECK(participant_b_id IS NULL OR (participant_a_id IS NOT NULL AND participant_b_id <> participant_a_id))",
                "blocks_time": "INTEGER NOT NULL DEFAULT 0 CHECK(blocks_time IN (0,1))",
                "source_type": "TEXT NOT NULL DEFAULT 'legacy_unknown' CHECK(source_type IN ('human','real_api','rules','ai','mock','cache','legacy_unknown'))",
            }.items():
                if name not in calendar_columns:
                    db.execute(f"ALTER TABLE calendar_entries ADD COLUMN {name} {definition}")

            list_item_columns = {row["name"] for row in db.execute("PRAGMA table_info(shared_list_items)").fetchall()}
            for name, definition in {
                "item_kind": "TEXT NOT NULL DEFAULT 'legacy_unknown' CHECK(item_kind IN ('legacy_unknown','general','gift','meal','travel'))",
                "deleted_at": "TEXT",
                "created_at": "TEXT",
                "updated_at": "TEXT",
            }.items():
                if name not in list_item_columns:
                    db.execute(f"ALTER TABLE shared_list_items ADD COLUMN {name} {definition}")

            substitution_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(ingredient_substitutions)").fetchall()
            }
            for name, definition in {
                "meal_item_id": "TEXT REFERENCES meal_cart_items(id) ON DELETE CASCADE",
                "snapshot_ingredient_ref": "TEXT",
                "meal_version": "INTEGER CHECK(meal_version IS NULL OR meal_version >= 1)",
                "expected_versions_json": "TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(expected_versions_json) AND json_type(expected_versions_json)='object')",
                "rule_key": "TEXT",
                "rule_version": "INTEGER CHECK(rule_version IS NULL OR rule_version >= 1)",
            }.items():
                if name not in substitution_columns:
                    db.execute(f"ALTER TABLE ingredient_substitutions ADD COLUMN {name} {definition}")

            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ai_grants_active ON ai_context_grants(couple_id,user_id,purpose,expires_at) WHERE revoked_at IS NULL"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ai_grants_resource ON ai_context_grants(couple_id,user_id,resource_type,resource_id)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_calendar_busy ON calendar_entries(couple_id,starts_at,ends_at) WHERE deleted_at IS NULL AND blocks_time=1"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_shared_list_items_active ON shared_list_items(list_id,deleted_at,position)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_media_owner_visibility ON media_assets(couple_id,owner_id,visibility,status,created_at DESC)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ingredient_substitutions_meal "
                "ON ingredient_substitutions(meal_item_id,status,created_at DESC) "
                "WHERE meal_item_id IS NOT NULL"
            )

            # Stable sidecar identifiers and INSERT OR IGNORE make repeated
            # upgrades non-destructive.  Unknown means exactly unknown: no
            # dates, authors or source claims are inferred from content.
            provenance_sources = (
                ("moment", "moments", "id", "couple_id", "creator_id", "created_at"),
                ("media", "media_assets", "id", "couple_id", "owner_id", "created_at"),
                ("trip", "trips", "id", "couple_id", None, "created_at"),
                ("recipe", "recipes", "id", "couple_id", "owner_id", "created_at"),
                ("calendar_entry", "calendar_entries", "id", "couple_id", "creator_id", "created_at"),
            )
            for resource_type, table, id_column, couple_column, owner_column, created_column in provenance_sources:
                target_column = f"{resource_type}_id"
                owner_expression = owner_column if owner_column else "NULL"
                predicate = f"{couple_column} IS NOT NULL"
                db.execute(
                    f"""
                    INSERT OR IGNORE INTO memory_provenance(
                        id,couple_id,owner_id,{target_column},source_type,date_source,
                        evidence_json,version,created_at,updated_at
                    )
                    SELECT 'prov_{resource_type}_' || {id_column},{couple_column},{owner_expression},{id_column},
                           'legacy_unknown','legacy_unknown','{{}}',1,
                           COALESCE({created_column},CURRENT_TIMESTAMP),COALESCE({created_column},CURRENT_TIMESTAMP)
                    FROM {table} WHERE {predicate}
                    """
                )
            db.execute(
                """
                INSERT OR IGNORE INTO memory_provenance(
                    id,couple_id,owner_id,shared_list_item_id,source_type,date_source,
                    evidence_json,version,created_at,updated_at
                )
                SELECT 'prov_shared_list_item_' || i.id,l.couple_id,i.creator_id,i.id,
                       'legacy_unknown','legacy_unknown','{}',1,
                       COALESCE(i.created_at,CURRENT_TIMESTAMP),COALESCE(i.updated_at,i.created_at,CURRENT_TIMESTAMP)
                FROM shared_list_items i JOIN shared_lists l ON l.id=i.list_id
                """
            )

            inventory = schema_inventory(db, expected_version=SCHEMA_VERSION)
            if not inventory["structural_ok"]:
                raise RuntimeError("数据库 v34 结构不完整，迁移已回滚")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("数据库存在外键孤立记录，迁移已回滚且未升级版本")
            db.execute(
                "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def seed_demo(self) -> None:
        with self.connection() as db:
            if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                # Demo fixtures may be completed after a schema upgrade, but an
                # existing account password must never be reset on application
                # startup. This also keeps a deliberately deleted account gone.
                self._seed_phase_two(db)
                return
            db.executemany(
                "INSERT INTO users(id, phone, username, password_hash, nickname, avatar_label) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (DEMO_USER, "13800000001", "test", hash_password(DEMO_PASSWORD), "演示甲", "甲"),
                    (DEMO_PARTNER, "13800000002", "test1", hash_password(DEMO_PASSWORD), "演示乙", "乙"),
                ],
            )
            db.execute(
                "INSERT INTO couples(id, member_a, member_b, paired_at, anniversary) VALUES (?, ?, ?, ?, ?)",
                (DEMO_COUPLE, DEMO_USER, DEMO_PARTNER, "2024-02-14", "2024-02-14"),
            )
            db.executemany(
                "INSERT INTO messages(id, couple_id, sender_id, type, content, metadata, sent_at, read_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("msg-1", DEMO_COUPLE, DEMO_PARTNER, "text", "刚刚看到一家很适合我们的湖景民宿", "{}", "2026-10-12T04:14:00+00:00", "2026-10-12T04:14:08+00:00"),
                    ("msg-2", DEMO_COUPLE, DEMO_PARTNER, "trip", "杭州 · 青山湖", json.dumps({"subtitle": "湖景房 · ¥ 568 / 晚"}, ensure_ascii=False), "2026-10-12T04:15:00+00:00", "2026-10-12T04:15:10+00:00"),
                    ("msg-3", DEMO_COUPLE, DEMO_USER, "text", "好呀，放进松弛方案里 ♡", "{}", "2026-10-12T04:16:00+00:00", "2026-10-12T04:16:00+00:00"),
                ],
            )
            db.executemany(
                "INSERT INTO moments(id, couple_id, creator_id, type, title, body, occurred_at, location, visibility, media_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("moment-1", DEMO_COUPLE, DEMO_USER, "photo", "风吹过洱海，也吹进我们的故事里", "下次还要一起追日落。", "2026-09-18T10:00:00+08:00", "大理", "shared", "/static/assets/couple-lake-hero.png"),
                    ("moment-2", DEMO_COUPLE, DEMO_PARTNER, "call", "睡前电话", "聊了杭州旅行和周末晚餐", "2026-09-06T22:10:00+08:00", None, "shared", None),
                    ("moment-3", DEMO_COUPLE, DEMO_USER, "wishlist", "第一次一起做陶艺", "愿望清单 #17 已完成", "2026-08-22T15:30:00+08:00", "景德镇", "shared", None),
                ],
            )
            db.execute(
                "INSERT INTO trips(id, couple_id, title, destination, start_date, end_date, budget_cents, active_variant) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (DEMO_TRIP, DEMO_COUPLE, "杭州 · 三日", "杭州", "2026-10-24", "2026-10-26", 400000, "slow"),
            )
            db.executemany(
                "INSERT INTO trip_preferences(trip_id, user_id, pace, interests, transport, diet, hotel, must_visits) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (DEMO_TRIP, DEMO_USER, "松弛", "摄影,人文,美食", "公共交通", "不吃香菜", "湖景,安静", "西湖,灵隐寺"),
                    (DEMO_TRIP, DEMO_PARTNER, "适中", "自然,茶园,徒步", "公共交通", "无", "大床房", "九溪烟树"),
                ],
            )
            # Demo seeding runs inside one SQLite transaction. It must stay
            # deterministic and must never call a remote planner (whose quota
            # reservation uses a separate transaction and could deadlock this
            # fresh database). Real model generation remains available through
            # the explicit regenerate/create flows after startup.
            demo_plans = LocalTravelPlanner().generate_variants("杭州", 400000)
            self._insert_generated_plans(db, DEMO_TRIP, "杭州", 400000, demo_plans)
            db.execute(
                "INSERT INTO hotel_options(id, trip_id, name, area, room_type, price_cents, selected) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("hotel-1", DEMO_TRIP, "西湖秋水民宿", "断桥 800m", "湖景大床房", 56800, 1),
            )
            db.executemany(
                "INSERT INTO booking_links(id, target_id, provider, url, quote_cents, terms, includes_tax, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("quote-1", "hotel-1", "携程", "https://hotels.ctrip.com/", 56800, "入住前一天可取消", 1, "2026-10-12T12:00:00+08:00"),
                    ("quote-2", "hotel-1", "飞猪", "https://www.fliggy.com/", 58600, "不可取消", 1, "2026-10-12T12:00:00+08:00"),
                ],
            )
            db.execute(
                "INSERT INTO shared_lists(id, couple_id, type, title) VALUES (?, ?, ?, ?)",
                ("list-october", DEMO_COUPLE, "wishlist", "十月心愿"),
            )
            db.executemany(
                "INSERT INTO shared_list_items(id, list_id, creator_id, text, note, completed_at, position) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("wish-1", "list-october", DEMO_USER, "一起看一场露天电影", "10月2日完成", "2026-10-02T21:00:00+08:00", 1),
                    ("wish-2", "list-october", DEMO_PARTNER, "给彼此拍一组秋日照片", "适合本周末", None, 2),
                    ("wish-3", "list-october", DEMO_USER, "完成杭州旅行最终方案", "还需双方确认", None, 3),
                ],
            )
            db.execute(
                "INSERT INTO daily_answers(id, couple_id, question_date, question, user_id, answer, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("answer-chen", DEMO_COUPLE, date.today().isoformat(), "如果现在可以立刻出发，你最想和我去哪里？", DEMO_PARTNER, "想去冰岛自驾", utc_now()),
            )
            self._seed_phase_two(db)

    def _seed_phase_two(self, db: sqlite3.Connection) -> None:
        """Add stable self-hosted demo data without overwriting user changes."""
        if not db.execute(
            "SELECT 1 FROM couples WHERE id=? AND status='active'", (DEMO_COUPLE,),
        ).fetchone():
            return
        now = utc_now()
        db.executemany(
            "INSERT OR IGNORE INTO user_locations(user_id, city, country, latitude, longitude, timezone, updated_at) VALUES (?, ?, '中国', ?, ?, ?, ?)",
            [
                (DEMO_USER, "上海", 31.2304, 121.4737, "Asia/Shanghai", now),
                (DEMO_PARTNER, "杭州", 30.2741, 120.1551, "Asia/Shanghai", now),
            ],
        )
        db.execute(
            "INSERT OR IGNORE INTO couple_statuses(user_id, couple_id, status_code, emoji, text, expires_at, updated_at) VALUES (?, ?, 'home', '⌂', '已到家', ?, ?)",
            (DEMO_PARTNER, DEMO_COUPLE, (datetime.now(timezone.utc) + timedelta(hours=8)).replace(microsecond=0).isoformat(), now),
        )

    def _insert_generated_plans(
        self,
        db: sqlite3.Connection,
        trip_id: str,
        destination: str,
        budget_cents: int,
        plans: list | None = None,
    ) -> None:
        trip_dates = db.execute("SELECT start_date, end_date FROM trips WHERE id = ?", (trip_id,)).fetchone()
        first_date = date.fromisoformat(trip_dates["start_date"])
        last_date = date.fromisoformat(trip_dates["end_date"])
        day_count = max(1, (last_date - first_date).days + 1)
        day_titles = {
            "slow": ["抵达与初见", "慢游与相处", "留白与返程"],
            "classic": ["经典地标初见", "核心路线巡游", "收尾与返程"],
            "value": ["轻预算初见", "公共交通漫游", "免费风景与返程"],
            "food": ["本地味道初见", "市场与特色正餐", "甜品夜市与返程"],
            "nature": ["自然风景初见", "山野湖畔慢行", "日落与返程"],
        }
        for plan in plans or self.planner.generate_variants(destination, budget_cents):
            variant_id = f"variant-{plan.style}-{trip_id}"
            db.execute(
                "INSERT INTO plan_variants(id, trip_id, style, name, summary, estimated_budget_cents, intensity, transport_minutes, generation_source, generation_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    variant_id, trip_id, plan.style, plan.name, plan.summary,
                    plan.budget_cents, plan.intensity, plan.transport_minutes,
                    getattr(plan, "generation_source", "legacy"),
                    getattr(plan, "generation_message", ""),
                ),
            )
            if plan.style == "food":
                # A food plan must be useful for every travel day. The planner may
                # only return a short sample, so materialize breakfast, lunch and
                # dinner for every date here. Exact dishes and nearby restaurants
                # are resolved later by the places provider for each day's area.
                meal_templates = (
                    {"start_time": "08:00", "title": "早餐 · 附近本地早点", "type": "meal", "duration_minutes": 60, "cost_cents": 0, "note": "选择当天住宿或游览区域后查询附近特色早餐", "details": {"activities": ["查看附近真实早餐店", "从候选店中选择双方想吃的早点"], "highlights": ["按当天区域控制距离", "可比较评分、距离与特色菜"], "recommendation_reason": "先确定当天活动区域再找早餐，可以减少绕路，也不会用虚构店名代替真实结果。", "practical_tips": ["点击“查本日早中晚餐馆”获取高德真实候选"], "best_time": "出发前 45–60 分钟", "reservation_note": "早餐通常无需预约，以店铺实时状态为准。"}},
                    {"start_time": "12:00", "title": "午餐 · 附近本地菜", "type": "meal", "duration_minutes": 90, "cost_cents": 0, "note": "选择当天游览区域后查询附近特色正餐", "details": {"activities": ["查看附近本地菜馆", "比较招牌菜、评分与步行距离"], "highlights": ["优先当地特色口味", "可由两个人共同选择餐馆"], "recommendation_reason": "午餐安排在上午和下午行程之间的同一区域，能保留更多游玩时间。", "practical_tips": ["用餐高峰前确认营业状态和排队情况"], "best_time": "11:30–13:00", "reservation_note": "热门餐厅建议提前电话或平台确认。"}},
                    {"start_time": "18:30", "title": "晚餐 · 附近特色晚餐", "type": "meal", "duration_minutes": 90, "cost_cents": 0, "note": "选择当天住宿或夜游区域后查询附近特色晚餐", "details": {"activities": ["查看住宿或夜游区域附近餐厅", "选择适合双人分享的特色菜"], "highlights": ["结束一天后从容用餐", "方便接续夜游或返回酒店"], "recommendation_reason": "把晚餐放在夜游或酒店附近，可以减少疲劳并为当天留出轻松收尾时间。", "practical_tips": ["确认末班交通和餐厅停止接单时间"], "best_time": "18:00–20:00", "reservation_note": "周末和节假日建议预约。"}},
                )
                buckets = [[dict(item) for item in meal_templates] for _ in range(day_count)]
            else:
                buckets = [[] for _ in range(day_count)]
                for index, item in enumerate(plan.items):
                    try:
                        item_day = int(item.get("day_number", 0))
                    except (TypeError, ValueError):
                        item_day = 0
                    target = item_day - 1 if 1 <= item_day <= day_count else index % day_count
                    buckets[target].append(item)
                for day_index, bucket in enumerate(buckets):
                    if bucket:
                        continue
                    bucket.append({
                        "start_time": "10:00",
                        "title": f"{destination} · 第 {day_index + 1} 天地点待确认",
                        "type": "place",
                        "duration_minutes": 90,
                        "cost_cents": 0,
                        "note": "生成结果未覆盖当天。为避免虚构地点，请搜索或手动填写真实地点。",
                        "details": {},
                    })
            for day_index in range(day_count):
                day_number = day_index + 1
                day_id = f"day-{plan.style}-{trip_id}-{day_number}"
                generated_themes = getattr(plan, "day_themes", None) or {}
                title_options = day_titles.get(plan.style, [])
                day_title = str(generated_themes.get(day_number) or (title_options[day_index] if day_index < len(title_options) else f"{destination}第 {day_number} 天"))[:80]
                db.execute(
                    "INSERT INTO trip_days(id, variant_id, day_number, date, title, intensity, position) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (day_id, variant_id, day_number, (first_date + timedelta(days=day_index)).isoformat(), day_title, plan.intensity, day_number),
                )
                for position, item in enumerate(buckets[day_index], 1):
                    db.execute(
                        "INSERT INTO itinerary_items(id, day_id, type, title, start_time, duration_minutes, cost_cents, note, details_json, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (new_id("item"), day_id, item["type"], item["title"], item["start_time"], item["duration_minutes"], item["cost_cents"], item["note"], json.dumps(item.get("details") or {}, ensure_ascii=False), position),
                    )

    def _audit(self, db: sqlite3.Connection, action: str, target_type: str, target_id: str, detail: dict | None = None, actor_id: str = DEMO_USER) -> None:
        couple = db.execute("SELECT id FROM couples WHERE (member_a=? OR member_b=?) AND status='active' LIMIT 1", (actor_id, actor_id)).fetchone()
        couple_id = couple["id"] if couple else "unpaired"
        db.execute(
            "INSERT INTO audit_events(id, actor_id, couple_id, action, target_type, target_id, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id("audit"), actor_id, couple_id, action, target_type, target_id, json.dumps(detail or {}, ensure_ascii=False), utc_now()),
        )

    def _event(
        self,
        db: sqlite3.Connection,
        couple_id: str,
        actor_id: str,
        event_type: str,
        resource_id: str,
        payload: dict | None = None,
    ) -> int:
        cursor = db.execute(
            "INSERT INTO realtime_events(couple_id, actor_id, event_type, resource_id, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (couple_id, actor_id, event_type, resource_id, json.dumps(payload or {}, ensure_ascii=False), utc_now()),
        )
        # A couple only needs a bounded replay window on this self-hosted server.
        db.execute(
            "DELETE FROM realtime_events WHERE couple_id=? AND sequence < (SELECT COALESCE(MAX(sequence), 0) - 2000 FROM realtime_events WHERE couple_id=?)",
            (couple_id, couple_id),
        )
        sequence = int(cursor.lastrowid)
        if not (payload or {}).get("owner_only"):
            self._insert_realtime_partner_notification(
                db,
                couple_id=couple_id,
                actor_id=actor_id,
                event_type=event_type,
                resource_id=resource_id,
                payload=payload or {},
                sequence=sequence,
            )
        return sequence

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["payload"] = parse_json(item.pop("payload_json", "{}"), {})
        return item

    def _resource_visible_to_user(self, db: sqlite3.Connection, resource_type: str, resource_id: str, user_id: str) -> bool:
        """Keep private shared-resource metadata out of partner replay/export."""
        if resource_type == "media":
            row = db.execute("SELECT owner_id AS owner,visibility FROM media_assets WHERE id=?", (resource_id,)).fetchone()
        elif resource_type == "moment":
            row = db.execute("SELECT creator_id AS owner,visibility FROM moments WHERE id=?", (resource_id,)).fetchone()
        elif resource_type == "trip_emergency":
            row = db.execute(
                "SELECT creator_id AS owner,visibility FROM trip_emergency_entries WHERE id=? AND deleted_at IS NULL",
                (resource_id,),
            ).fetchone()
        elif resource_type == "voice_transcript":
            row = db.execute(
                """SELECT t.owner_id AS owner,
                          CASE WHEN t.status='ready'
                                 AND (t.visibility='shared'
                                      OR (t.visibility='delayed' AND t.reveal_at<=?))
                                 AND m.visibility='shared' AND media.visibility='shared'
                                 AND m.deleted_at IS NULL AND m.purged_at IS NULL
                                 AND media.status='ready' AND media.deleted_at IS NULL
                                 AND media.purged_at IS NULL
                                 AND c.status='active'
                                 AND a.deleted_at IS NULL AND b.deleted_at IS NULL
                               THEN 'shared' ELSE 'private' END AS visibility
                   FROM voice_transcripts t
                   JOIN moments m ON m.id=t.moment_id AND m.media_id=t.media_id
                                          AND m.couple_id=t.couple_id
                   JOIN media_assets media ON media.id=t.media_id AND media.couple_id=t.couple_id
                   JOIN couples c ON c.id=t.couple_id
                   JOIN users a ON a.id=c.member_a
                   JOIN users b ON b.id=c.member_b
                   WHERE t.id=? AND ? IN (c.member_a,c.member_b)""",
                (utc_now(), resource_id, user_id),
            ).fetchone()
        elif resource_type == "future_letter":
            letter = db.execute("SELECT * FROM future_letters WHERE id=?", (resource_id,)).fetchone()
            if not letter or letter["status"] == "deleted":
                return False
            if letter["author_id"] == user_id:
                return True
            return bool(
                letter["status"] == "delivered"
                and letter["recipient_id"] == user_id
                and self._future_pair_active(db, letter)
            )
        else:
            return True
        return bool(row and (row["owner"] == user_id or row["visibility"] == "shared"))

    def _event_visible_to_user(self, db: sqlite3.Connection, row: sqlite3.Row, user_id: str) -> bool:
        if row["actor_id"] == user_id:
            return True
        payload = parse_json(row["payload_json"], {})
        if payload.get("owner_only") or payload.get("visibility") in {"private", "delayed"}:
            return False
        if row["event_type"].startswith("media."):
            resource_type = "media"
        elif row["event_type"].startswith("moment."):
            resource_type = "moment"
        else:
            resource_type = payload.get("resource", "")
        return self._resource_visible_to_user(db, resource_type, row["resource_id"], user_id)

    def list_realtime_events(self, after: object = 0, limit: int = 100, user_id: str = DEMO_USER) -> dict:
        try:
            after_number = max(0, int(after))
        except (TypeError, ValueError) as error:
            raise ValueError("事件序号必须是整数") from error
        visible_limit = max(1, min(int(limit), 200))
        cursor = after_number
        items: list[dict] = []
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            # Limit visible results, while bounding each request to five
            # chunks (at most 1,000 examined rows). A hidden-only page can
            # advance its cursor without skipping the next shared event.
            for _ in range(5):
                rows = db.execute(
                    "SELECT * FROM realtime_events WHERE couple_id=? AND sequence>? ORDER BY sequence LIMIT 200",
                    (couple["id"], cursor),
                ).fetchall()
                for row in rows:
                    cursor = int(row["sequence"])
                    if self._event_visible_to_user(db, row, user_id):
                        items.append(self._event_dict(row))
                        if len(items) == visible_limit:
                            break
                if len(items) == visible_limit or len(rows) < 200:
                    break
        return {"items": items, "latest_sequence": cursor}

    @staticmethod
    def _idempotent_result(
        db: sqlite3.Connection,
        couple_id: str,
        user_id: str,
        operation: str,
        key: object,
    ) -> dict | None:
        if not key:
            return None
        key_text = str(key).strip()
        if not re.fullmatch(r"[A-Za-z0-9:._-]{8,160}", key_text):
            raise ValueError("幂等键格式无效")
        row = db.execute(
            "SELECT result_json FROM mutation_idempotency WHERE couple_id=? AND user_id=? AND operation=? AND idempotency_key=?",
            (couple_id, user_id, operation, key_text),
        ).fetchone()
        return parse_json(row["result_json"], {}) if row else None

    @staticmethod
    def _save_idempotent_result(
        db: sqlite3.Connection,
        couple_id: str,
        user_id: str,
        operation: str,
        key: object,
        result: dict,
    ) -> None:
        if not key:
            return
        db.execute(
            "INSERT OR IGNORE INTO mutation_idempotency(couple_id, user_id, operation, idempotency_key, result_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (couple_id, user_id, operation, str(key).strip(), json.dumps(result, ensure_ascii=False), utc_now()),
        )

    def _couple_context(self, db: sqlite3.Connection, user_id: str) -> tuple[sqlite3.Row, str]:
        couple = db.execute("SELECT * FROM couples WHERE (member_a=? OR member_b=?) AND status='active' LIMIT 1", (user_id, user_id)).fetchone()
        if not couple:
            raise ValueError("请先绑定伴侣")
        partner_id = couple["member_b"] if couple["member_a"] == user_id else couple["member_a"]
        return couple, partner_id

    def notification_pair(self, user_id: str) -> dict[str, str]:
        """Return the only valid actor/recipient pair for a couple notification."""
        with self.connection() as db:
            couple, partner_id = self._couple_context(db, user_id)
            actor = db.execute("SELECT nickname FROM users WHERE id=?", (user_id,)).fetchone()
            if not actor:
                raise KeyError("用户不存在")
        return {
            "couple_id": str(couple["id"]),
            "actor_id": user_id,
            "actor_name": str(actor["nickname"]),
            "partner_id": partner_id,
        }

    @staticmethod
    def public_user(row: sqlite3.Row | dict) -> dict:
        if row is None:
            raise KeyError("用户不存在")
        value = dict(row)
        return {key: value.get(key) for key in ("id", "username", "nickname", "avatar_label", "locale", "timezone", "created_at")}

    def register(self, username: str, password: str, nickname: str) -> dict:
        username = username.strip().lower()
        nickname = nickname.strip()
        if not re.fullmatch(r"[a-z0-9_]{3,30}", username):
            raise ValueError("账号需为 3–30 位小写字母、数字或下划线")
        validate_new_password(password, username)
        if not nickname or len(nickname) > 30:
            raise ValueError("昵称长度需为 1–30 位")
        user_id = new_id("user")
        try:
            with self.connection() as db:
                db.execute(
                    "INSERT INTO users(id, username, password_hash, nickname, avatar_label) VALUES (?, ?, ?, ?, ?)",
                    (user_id, username, hash_password(password), nickname, nickname[0]),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("该账号已被注册") from error
        return self.get_user(user_id)

    def get_user(self, user_id: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise KeyError("用户不存在")
        return self.public_user(row)

    def authenticate(self, username: str, password: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM users WHERE username=? AND deleted_at IS NULL", (username.strip().lower(),)).fetchone()
        encoded = row["password_hash"] if row else DUMMY_PASSWORD_HASH
        valid = verify_password(password, encoded)
        if not row or not valid:
            raise ValueError("账号或密码不正确")
        return self.public_user(row)

    @staticmethod
    def _auth_failure_hash(value: str) -> str:
        return hashlib.sha256(value.strip().casefold().encode("utf-8")).hexdigest()

    def login_attempt_allowed(self, username: str, ip_address: str, limit: int = 8, window_minutes: int = 15) -> bool:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).replace(microsecond=0).isoformat()
        username_hash = self._auth_failure_hash(username)
        ip_hash = self._auth_failure_hash(ip_address)
        with self.connection() as db:
            db.execute("DELETE FROM auth_failures WHERE failed_at<?", ((datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat(),))
            count = db.execute(
                "SELECT COUNT(*) FROM auth_failures WHERE username_hash=? AND ip_hash=? AND failed_at>=?",
                (username_hash, ip_hash, cutoff),
            ).fetchone()[0]
        return int(count) < max(1, int(limit))

    def record_login_failure(self, username: str, ip_address: str) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO auth_failures(username_hash,ip_hash,failed_at) VALUES (?,?,?)",
                (self._auth_failure_hash(username), self._auth_failure_hash(ip_address), utc_now()),
            )

    def clear_login_failures(self, username: str) -> None:
        with self.connection() as db:
            db.execute("DELETE FROM auth_failures WHERE username_hash=?", (self._auth_failure_hash(username),))

    def change_password(
        self,
        current_password: object,
        new_password: object,
        confirmation: object,
        user_id: str,
        current_token: str | None = None,
    ) -> dict:
        current = str(current_password or "")
        new = str(new_password or "")
        if new != str(confirmation or ""):
            raise ValueError("两次输入的新密码不一致")
        validate_new_password(new)
        if current == new:
            raise ValueError("新密码不能与当前密码相同")
        token_hash = hashlib.sha256(current_token.encode()).hexdigest() if current_token else ""
        now = utc_now()
        with self.connection() as db:
            row = db.execute(
                "SELECT password_hash FROM users WHERE id=? AND deleted_at IS NULL",
                (user_id,),
            ).fetchone()
            encoded = row["password_hash"] if row else DUMMY_PASSWORD_HASH
            if not row or not verify_password(current, encoded):
                raise ValueError("当前密码不正确")
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(new), user_id))
            if token_hash:
                revoked = db.execute(
                    "UPDATE sessions SET revoked_at=? WHERE user_id=? AND token_hash!=? AND revoked_at IS NULL",
                    (now, user_id, token_hash),
                ).rowcount
            else:
                revoked = db.execute(
                    "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                    (now, user_id),
                ).rowcount
            db.execute(
                """
                UPDATE push_subscriptions SET active=0,updated_at=?
                WHERE session_id IN (SELECT id FROM sessions WHERE user_id=? AND revoked_at IS NOT NULL)
                """,
                (now, user_id),
            )
            self._audit(db, "account.password_changed", "user", user_id, {"revoked_sessions": revoked}, actor_id=user_id)
        return {"changed": True, "revoked_sessions": revoked}

    def create_session(self, user_id: str, device_name: str = "未知设备", user_agent: str = "", ip_address: str = "") -> str:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = utc_now()
        expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).replace(microsecond=0).isoformat()
        with self.connection() as db:
            db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            db.execute(
                "INSERT INTO sessions(id, user_id, token_hash, expires_at, created_at, last_seen_at, device_name, user_agent, ip_address) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (new_id("session"), user_id, token_hash, expires_at, now, now, str(device_name or "未知设备")[:80], str(user_agent or "")[:500], str(ip_address or "")[:80]),
            )
            db.execute(
                "DELETE FROM sessions WHERE user_id=? AND id NOT IN (SELECT id FROM sessions WHERE user_id=? ORDER BY created_at DESC, rowid DESC LIMIT 10)",
                (user_id, user_id),
            )
        return token

    def user_for_session(self, token: str | None) -> dict | None:
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = utc_now()
        with self.connection() as db:
            row = db.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND s.revoked_at IS NULL AND u.deleted_at IS NULL",
                (token_hash, now),
            ).fetchone()
            if row:
                db.execute("UPDATE sessions SET last_seen_at=? WHERE token_hash=?", (now, token_hash))
        return self.public_user(row) if row else None

    def delete_session(self, token: str | None) -> None:
        if not token:
            return
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.connection() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    @staticmethod
    def _active_session_id(db: sqlite3.Connection, token: str | None, user_id: str) -> str:
        if not token:
            raise PermissionError("请先登录")
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = db.execute(
            "SELECT id FROM sessions WHERE token_hash=? AND user_id=? AND expires_at>? AND revoked_at IS NULL",
            (token_hash, user_id, utc_now()),
        ).fetchone()
        if not row:
            raise PermissionError("当前设备登录已经失效")
        return str(row["id"])

    def save_push_subscription(
        self,
        *,
        endpoint_hash: str,
        subscription_ciphertext: str,
        user_id: str,
        session_token: str | None,
        device_name: str,
        user_agent: str,
    ) -> dict:
        if not re.fullmatch(r"[a-f0-9]{64}", endpoint_hash):
            raise ValueError("推送订阅标识无效")
        if not subscription_ciphertext or len(subscription_ciphertext) > 16_384:
            raise ValueError("推送订阅内容无效")
        now = utc_now()
        with self.connection() as db:
            session_id = self._active_session_id(db, session_token, user_id)
            existing = db.execute(
                "SELECT id,created_at FROM push_subscriptions WHERE endpoint_hash=?",
                (endpoint_hash,),
            ).fetchone()
            subscription_id = str(existing["id"]) if existing else new_id("push")
            created_at = str(existing["created_at"]) if existing else now
            db.execute(
                """
                INSERT INTO push_subscriptions(
                    id,user_id,session_id,endpoint_hash,subscription_ciphertext,
                    device_name,user_agent,active,failure_count,last_error,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,1,0,NULL,?,?)
                ON CONFLICT(endpoint_hash) DO UPDATE SET
                    user_id=excluded.user_id,session_id=excluded.session_id,
                    subscription_ciphertext=excluded.subscription_ciphertext,
                    device_name=excluded.device_name,user_agent=excluded.user_agent,
                    active=1,failure_count=0,last_error=NULL,updated_at=excluded.updated_at
                """,
                (
                    subscription_id, user_id, session_id, endpoint_hash, subscription_ciphertext,
                    str(device_name or "未知设备")[:80], str(user_agent or "")[:500], created_at, now,
                ),
            )
            self._audit(
                db, "push.subscription_enabled", "push_subscription", subscription_id,
                {"device_name": str(device_name or "未知设备")[:80]}, actor_id=user_id,
            )
        return {"id": subscription_id, "subscribed": True, "updated_at": now}

    def push_subscription_status(self, user_id: str, endpoint_hash: str | None = None) -> dict:
        now = utc_now()
        with self.connection() as db:
            db.execute(
                """
                UPDATE push_subscriptions SET active=0,updated_at=?
                WHERE active=1 AND session_id IN (
                    SELECT id FROM sessions WHERE revoked_at IS NOT NULL OR expires_at<=?
                )
                """,
                (now, now),
            )
            count = int(db.execute(
                """
                SELECT COUNT(*) FROM push_subscriptions p
                JOIN sessions s ON s.id=p.session_id
                WHERE p.user_id=? AND p.active=1 AND s.revoked_at IS NULL AND s.expires_at>?
                """,
                (user_id, now),
            ).fetchone()[0])
            current = False
            if endpoint_hash and re.fullmatch(r"[a-f0-9]{64}", endpoint_hash):
                current = bool(db.execute(
                    """
                    SELECT 1 FROM push_subscriptions p
                    JOIN sessions s ON s.id=p.session_id
                    WHERE p.user_id=? AND p.endpoint_hash=? AND p.active=1
                      AND s.revoked_at IS NULL AND s.expires_at>?
                    """,
                    (user_id, endpoint_hash, now),
                ).fetchone())
        return {"subscribed": current, "active_device_count": count}

    def active_push_subscriptions(self, user_id: str, endpoint_hash: str | None = None) -> list[dict]:
        now = utc_now()
        parameters: list[object] = [user_id, now]
        suffix = ""
        if endpoint_hash:
            if not re.fullmatch(r"[a-f0-9]{64}", endpoint_hash):
                raise ValueError("推送订阅标识无效")
            suffix = " AND p.endpoint_hash=?"
            parameters.append(endpoint_hash)
        with self.connection() as db:
            rows = db.execute(
                """
                SELECT p.id,p.subscription_ciphertext,p.failure_count
                FROM push_subscriptions p JOIN sessions s ON s.id=p.session_id
                WHERE p.user_id=? AND p.active=1 AND s.revoked_at IS NULL AND s.expires_at>?
                """ + suffix + " ORDER BY p.updated_at DESC",
                tuple(parameters),
            ).fetchall()
        return [dict(row) for row in rows]

    def remove_push_subscription(self, endpoint_hash: str, user_id: str) -> dict:
        if not re.fullmatch(r"[a-f0-9]{64}", endpoint_hash):
            raise ValueError("推送订阅标识无效")
        with self.connection() as db:
            row = db.execute(
                "SELECT id FROM push_subscriptions WHERE endpoint_hash=? AND user_id=?",
                (endpoint_hash, user_id),
            ).fetchone()
            if row:
                db.execute("DELETE FROM push_subscriptions WHERE id=?", (row["id"],))
                self._audit(
                    db, "push.subscription_disabled", "push_subscription", row["id"],
                    actor_id=user_id,
                )
        return {"subscribed": False, "removed": bool(row)}

    def record_push_delivery(self, subscription_id: str, *, success: bool, status: int | None = None, error: str = "") -> None:
        now = utc_now()
        with self.connection() as db:
            row = db.execute("SELECT failure_count FROM push_subscriptions WHERE id=?", (subscription_id,)).fetchone()
            if not row:
                return
            if success:
                db.execute(
                    "UPDATE push_subscriptions SET failure_count=0,last_error=NULL,last_success_at=?,updated_at=? WHERE id=?",
                    (now, now, subscription_id),
                )
                return
            failures = int(row["failure_count"] or 0) + 1
            expired = status in {404, 410}
            active = 0 if expired or failures >= 3 else 1
            db.execute(
                "UPDATE push_subscriptions SET failure_count=?,last_error=?,active=?,updated_at=? WHERE id=?",
                (failures, str(error or f"HTTP {status or 'unknown'}")[:300], active, now, subscription_id),
            )

    @staticmethod
    def _normalize_push_payload(payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("推送内容无效")
        event_type = str(payload.get("type", "")).strip()
        title = str(payload.get("title", "")).strip()
        body = str(payload.get("body", "")).strip()
        url = str(payload.get("url", "/?screen=chat")).strip()
        tag = str(payload.get("tag", "")).strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", event_type):
            raise ValueError("推送类型无效")
        if not title or len(title) > 80 or not body or len(body) > 180:
            raise ValueError("推送文案无效")
        if not url.startswith("/") or url.startswith("//") or len(url) > 500:
            raise ValueError("推送跳转地址无效")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", tag):
            raise ValueError("推送标签无效")
        normalized = {"type": event_type, "title": title, "body": body, "url": url, "tag": tag}
        call_id = str(payload.get("call_id", "")).strip()
        if call_id:
            if not re.fullmatch(r"call_[a-f0-9]{32}", call_id):
                raise ValueError("通话标识无效")
            normalized["call_id"] = call_id
        return normalized

    def enqueue_push_notification(
        self,
        user_id: str,
        payload: dict,
        *,
        ttl_seconds: int = 60,
        max_attempts: int = 3,
    ) -> dict:
        """Persist a privacy-safe notification before an HTTP response is returned."""
        normalized = self._normalize_push_payload(payload)
        ttl = max(10, min(int(ttl_seconds), 86_400))
        attempts = max(1, min(int(max_attempts), 5))
        now_value = datetime.now(timezone.utc).replace(microsecond=0)
        now = now_value.isoformat()
        expires_at = (now_value + timedelta(seconds=ttl)).isoformat()
        job_id = new_id("push_job")
        with self.connection() as db:
            active_devices = int(db.execute(
                """
                SELECT COUNT(*) FROM push_subscriptions p
                JOIN sessions s ON s.id=p.session_id
                WHERE p.user_id=? AND p.active=1 AND s.revoked_at IS NULL AND s.expires_at>?
                """,
                (user_id, now),
            ).fetchone()[0])
            if active_devices == 0:
                return {"queued": False, "active_device_count": 0, "job_id": None}
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO push_outbox(
                    id,user_id,event_type,tag,payload_json,status,attempt_count,max_attempts,
                    next_attempt_at,expires_at,created_at,updated_at
                ) VALUES (?,?,?,?,?,'pending',0,?,?,?,?,?)
                """,
                (
                    job_id, user_id, normalized["type"], normalized["tag"],
                    json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
                    attempts, now, expires_at, now, now,
                ),
            )
            if not cursor.rowcount:
                existing = db.execute(
                    "SELECT id,status FROM push_outbox WHERE user_id=? AND tag=?",
                    (user_id, normalized["tag"]),
                ).fetchone()
                return {
                    "queued": bool(existing and existing["status"] in {"pending", "processing", "sent"}),
                    "active_device_count": active_devices,
                    "job_id": existing["id"] if existing else None,
                }
        return {"queued": True, "active_device_count": active_devices, "job_id": job_id}

    def recover_push_outbox(self) -> dict:
        """Return interrupted jobs to the queue when the self-hosted service restarts."""
        now = utc_now()
        with self.connection() as db:
            expired = db.execute(
                "UPDATE push_outbox SET status='expired',updated_at=? WHERE status IN ('pending','processing') AND expires_at<=?",
                (now, now),
            ).rowcount
            recovered = db.execute(
                "UPDATE push_outbox SET status='pending',next_attempt_at=?,updated_at=? WHERE status='processing' AND expires_at>?",
                (now, now, now),
            ).rowcount
        return {"recovered": recovered, "expired": expired}

    def claim_push_notifications(self, limit: int = 10) -> list[dict]:
        safe_limit = max(1, min(int(limit), 50))
        now_value = datetime.now(timezone.utc).replace(microsecond=0)
        now = now_value.isoformat()
        completed_cutoff = (now_value - timedelta(days=7)).isoformat()
        failed_cutoff = (now_value - timedelta(days=30)).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE push_outbox SET status='expired',updated_at=? WHERE status IN ('pending','processing') AND expires_at<=?",
                (now, now),
            )
            db.execute(
                "DELETE FROM push_outbox WHERE (status IN ('sent','expired','cancelled') AND updated_at<?) OR (status='failed' AND updated_at<?)",
                (completed_cutoff, failed_cutoff),
            )
            rows = db.execute(
                """
                SELECT * FROM push_outbox
                WHERE status='pending' AND next_attempt_at<=? AND expires_at>?
                ORDER BY created_at LIMIT ?
                """,
                (now, now, safe_limit),
            ).fetchall()
            claimed: list[dict] = []
            for row in rows:
                changed = db.execute(
                    "UPDATE push_outbox SET status='processing',attempt_count=attempt_count+1,updated_at=? WHERE id=? AND status='pending'",
                    (now, row["id"]),
                ).rowcount
                if not changed:
                    continue
                item = dict(row)
                item["attempt_count"] = int(item["attempt_count"]) + 1
                item["payload"] = parse_json(item.pop("payload_json"), {})
                claimed.append(item)
        return claimed

    def finish_push_notification(
        self,
        job_id: str,
        *,
        delivered: bool,
        error: str = "",
        retryable: bool = True,
    ) -> dict:
        now_value = datetime.now(timezone.utc).replace(microsecond=0)
        now = now_value.isoformat()
        with self.connection() as db:
            row = db.execute("SELECT * FROM push_outbox WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError("推送任务不存在")
            if row["status"] in {"cancelled", "expired"}:
                return {"id": job_id, "status": row["status"]}
            if delivered:
                status, next_attempt_at, sent_at = "sent", row["next_attempt_at"], now
            elif now >= row["expires_at"]:
                status, next_attempt_at, sent_at = "expired", row["next_attempt_at"], None
            elif not retryable or int(row["attempt_count"]) >= int(row["max_attempts"]):
                status, next_attempt_at, sent_at = "failed", row["next_attempt_at"], None
            else:
                delay = min(20, 2 ** max(1, int(row["attempt_count"])))
                status = "pending"
                next_attempt_at = (now_value + timedelta(seconds=delay)).isoformat()
                sent_at = None
            db.execute(
                "UPDATE push_outbox SET status=?,next_attempt_at=?,last_error=?,updated_at=?,sent_at=? WHERE id=?",
                (status, next_attempt_at, str(error)[:300] or None, now, sent_at, job_id),
            )
        return {"id": job_id, "status": status, "next_attempt_at": next_attempt_at}

    def cancel_push_notification(self, tag: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", str(tag or "")):
            raise ValueError("推送标签无效")
        now = utc_now()
        with self.connection() as db:
            cancelled = db.execute(
                "UPDATE push_outbox SET status='cancelled',updated_at=? WHERE tag=? AND status IN ('pending','processing')",
                (now, tag),
            ).rowcount
        return {"cancelled": cancelled}

    def push_outbox_status(self) -> dict:
        with self.connection() as db:
            rows = db.execute("SELECT status,COUNT(*) AS count FROM push_outbox GROUP BY status").fetchall()
        counts = {row["status"]: int(row["count"]) for row in rows}
        return {"counts": counts, "pending": counts.get("pending", 0) + counts.get("processing", 0)}

    def pairing_state(self, user_id: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM couples WHERE (member_a=? OR member_b=?) AND status='active' LIMIT 1", (user_id, user_id)).fetchone()
        return {"paired": bool(row), "couple_id": row["id"] if row else None}

    def create_user_invite(self, user_id: str) -> dict:
        if self.pairing_state(user_id)["paired"]:
            raise ValueError("当前账号已经绑定伴侣")
        raw_token = secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:10].upper()
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        invite_id = new_id("invite")
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).replace(microsecond=0).isoformat()
        with self.connection() as db:
            db.execute("UPDATE invites SET consumed_at=? WHERE creator_id=? AND consumed_at IS NULL", (utc_now(), user_id))
            db.execute("INSERT INTO invites(id, creator_id, token_hash, expires_at) VALUES (?, ?, ?, ?)", (invite_id, user_id, token_hash, expires_at))
            self._audit(db, "invite.created", "invite", invite_id, actor_id=user_id)
        return {"id": invite_id, "code": raw_token, "expires_at": expires_at, "single_use": True}

    def accept_user_invite(self, user_id: str, code: str, anniversary: object) -> dict:
        if not re.fullmatch(r"[A-Z0-9]{6,16}", code.strip().upper()):
            raise ValueError("邀请码格式不正确")
        try:
            anniversary_date = date.fromisoformat(str(anniversary or "").strip())
        except ValueError as error:
            raise ValueError("请选择你们正式在一起的日期") from error
        if anniversary_date > date.today():
            raise ValueError("在一起日期不能晚于今天")
        token_hash = hashlib.sha256(code.strip().upper().encode()).hexdigest()
        now = utc_now()
        try:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM couples WHERE (member_a=? OR member_b=?) AND status='active'", (user_id, user_id)).fetchone():
                    raise ValueError("当前账号已经绑定伴侣")
                invite = db.execute("SELECT * FROM invites WHERE token_hash=? AND consumed_at IS NULL AND expires_at>?", (token_hash, now)).fetchone()
                if not invite:
                    raise ValueError("邀请码无效或已过期")
                if invite["creator_id"] == user_id:
                    raise ValueError("不能接受自己创建的邀请码")
                if db.execute("SELECT 1 FROM couples WHERE (member_a=? OR member_b=?) AND status='active'", (invite["creator_id"], invite["creator_id"])).fetchone():
                    raise ValueError("邀请方已经绑定伴侣")
                couple_id = new_id("couple")
                db.execute(
                    "INSERT INTO couples(id, member_a, member_b, paired_at, anniversary) VALUES (?, ?, ?, ?, ?)",
                    (couple_id, invite["creator_id"], user_id, date.today().isoformat(), anniversary_date.isoformat()),
                )
                db.execute("UPDATE invites SET consumed_at=? WHERE id=?", (now, invite["id"]))
                list_id = new_id("list")
                db.execute("INSERT INTO shared_lists(id, couple_id, type, title) VALUES (?, ?, 'wishlist', '我们的心愿')", (list_id, couple_id))
                self._audit(db, "couple.paired", "couple", couple_id, actor_id=user_id)
        except sqlite3.IntegrityError as error:
            raise ValueError("其中一个账号已经绑定伴侣") from error
        return {"paired": True, "couple_id": couple_id}

    def bootstrap(self, user_id: str = DEMO_USER) -> dict:
        with self.connection() as db:
            user_row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            user = self.public_user(user_row)
            try:
                couple_row, partner_id = self._couple_context(db, user_id)
            except ValueError:
                return {"user": user, "needs_pairing": True, "permission_version": 0}
            partner = self.public_user(db.execute("SELECT * FROM users WHERE id = ?", (partner_id,)).fetchone())
            couple = dict(couple_row)
            trip_row = db.execute("SELECT id FROM trips WHERE couple_id=? AND deleted_at IS NULL ORDER BY start_date DESC, created_at DESC, rowid DESC LIMIT 1", (couple["id"],)).fetchone()
        return {
            "user": user,
            "partner": partner,
            "couple": couple,
            "permission_version": couple["version"],
            "needs_pairing": False,
            "messages": self.list_messages(user_id=user_id),
            "moments": self.list_moments(user_id=user_id),
            "trip": self.get_trip(trip_row["id"], user_id=user_id) if trip_row else None,
            "wishlist": self.get_wishlist(user_id=user_id),
            "dailyQuestion": self.get_daily_question(user_id=user_id),
            "coupleContext": self.get_couple_context(user_id=user_id),
            "weeklyReview": self.get_weekly_review(user_id=user_id),
            "datePlans": self.list_date_plans(user_id=user_id),
            "decisions": self.list_decisions(user_id=user_id),
            "trips": self.list_trips(user_id),
            "calendar": self.list_calendar_entries(user_id),
            "preferences": self.list_preferences(user_id),
            "mealSummary": self.meal_summary(user_id),
        }

    STATUS_PRESETS = {
        "busy": ("◷", "忙碌中"),
        "want_chat": ("♡", "想和你聊聊"),
        "need_company": ("♧", "需要陪伴"),
        "feeling_low": ("☁", "心情不太好"),
        "moving": ("↗", "在路上"),
        "home": ("⌂", "已到家"),
        "custom": ("✦", "分享此刻"),
    }
    CITY_COORDINATES = {
        "北京": (39.9042, 116.4074, "Asia/Shanghai"),
        "上海": (31.2304, 121.4737, "Asia/Shanghai"),
        "广州": (23.1291, 113.2644, "Asia/Shanghai"),
        "深圳": (22.5431, 114.0579, "Asia/Shanghai"),
        "杭州": (30.2741, 120.1551, "Asia/Shanghai"),
        "成都": (30.5728, 104.0668, "Asia/Shanghai"),
        "重庆": (29.4316, 106.9123, "Asia/Shanghai"),
        "南京": (32.0603, 118.7969, "Asia/Shanghai"),
        "武汉": (30.5928, 114.3055, "Asia/Shanghai"),
        "西安": (34.3416, 108.9398, "Asia/Shanghai"),
        "香港": (22.3193, 114.1694, "Asia/Hong_Kong"),
        "东京": (35.6762, 139.6503, "Asia/Tokyo"),
        "伦敦": (51.5072, -0.1276, "Europe/London"),
        "纽约": (40.7128, -74.0060, "America/New_York"),
    }

    def set_status(self, payload: dict, user_id: str = DEMO_USER) -> dict:
        code = str(payload.get("status_code", "")).strip()
        if code not in self.STATUS_PRESETS:
            raise ValueError("请选择有效的状态")
        duration = payload.get("duration_minutes")
        if isinstance(duration, bool) or not isinstance(duration, int) or not 30 <= duration <= 10080:
            raise ValueError("状态时长需为 30–10080 分钟的整数")
        default_emoji, default_text = self.STATUS_PRESETS[code]
        supplied_text = payload.get("text")
        if supplied_text is not None and not isinstance(supplied_text, str):
            raise ValueError("状态文字必须是文字")
        text = str(supplied_text or ("" if code == "custom" else default_text)).strip()[:60]
        if not text:
            raise ValueError("状态文字不能为空")
        emoji = default_emoji
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        now = now_dt.isoformat()
        expires_at = (now_dt + timedelta(minutes=duration)).isoformat()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            existing = db.execute(
                "SELECT version FROM couple_statuses WHERE user_id=? AND couple_id=?",
                (user_id, couple["id"]),
            ).fetchone()
            version = (existing["version"] + 1) if existing else 1
            db.execute(
                "INSERT INTO couple_statuses(user_id, couple_id, status_code, emoji, text, expires_at, version, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET couple_id=excluded.couple_id, status_code=excluded.status_code, emoji=excluded.emoji, text=excluded.text, expires_at=excluded.expires_at, version=excluded.version, updated_at=excluded.updated_at",
                (user_id, couple["id"], code, emoji, text, expires_at, version, now),
            )
            self._event(db, couple["id"], user_id, "presence.changed", user_id, {"statusCode": code, "version": version, "expiresAt": expires_at})
            row = db.execute(
                "SELECT * FROM couple_statuses WHERE user_id=? AND couple_id=?",
                (user_id, couple["id"]),
            ).fetchone()
        return dict(row)

    def clear_status(self, user_id: str = DEMO_USER) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            existed = db.execute("DELETE FROM couple_statuses WHERE user_id=? AND couple_id=?", (user_id, couple["id"])).rowcount
            if existed:
                self._event(db, couple["id"], user_id, "presence.changed", user_id, {"cleared": True})
        return {"cleared": bool(existed)}

    def set_location(self, payload: dict, user_id: str = DEMO_USER) -> dict:
        city = str(payload.get("city", "")).strip()
        if not city or len(city) > 80:
            raise ValueError("城市长度需为 1–80 位")
        defaults = self.CITY_COORDINATES.get(city)
        latitude = payload.get("latitude")
        longitude = payload.get("longitude")
        provider = map_provider_from_env(self.external_request_guard("amap"))
        if latitude is None and longitude is None and not provider.is_mock:
            try:
                resolved = provider.geocode(city, city)
                latitude, longitude = resolved["latitude"], resolved["longitude"]
            except QuotaExceededError:
                raise
            except (RuntimeError, ValueError) as error:
                raise ValueError(f"无法确认城市位置：{error}") from error
        elif latitude is None and longitude is None and defaults:
            latitude, longitude = defaults[0], defaults[1]
        elif (latitude is None) != (longitude is None):
            raise ValueError("经纬度必须同时提供")
        country = str(payload.get("country", "中国")).strip()[:40]
        supplied_timezone = str(payload.get("timezone", "")).strip()
        if supplied_timezone:
            timezone_name = supplied_timezone
        elif defaults:
            timezone_name = defaults[2]
        elif country in {"中国", "中国大陆", "CN", "China"}:
            timezone_name = "Asia/Shanghai"
        else:
            raise ValueError("海外城市请同时选择正确时区")
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ValueError("时区名称无效") from error
        if latitude is not None:
            try:
                latitude = float(latitude)
                longitude = float(longitude)
            except (TypeError, ValueError) as error:
                raise ValueError("经纬度必须是数字") from error
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise ValueError("经纬度超出范围")
        now = utc_now()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            db.execute(
                "INSERT INTO user_locations(user_id, city, country, latitude, longitude, timezone, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET city=excluded.city, country=excluded.country, latitude=excluded.latitude, longitude=excluded.longitude, timezone=excluded.timezone, updated_at=excluded.updated_at",
                (user_id, city, country, latitude, longitude, timezone_name, now),
            )
            db.execute("UPDATE users SET timezone=? WHERE id=?", (timezone_name, user_id))
            self._event(db, couple["id"], user_id, "presence.changed", user_id, {"locationChanged": True})
            row = db.execute("SELECT * FROM user_locations WHERE user_id=?", (user_id,)).fetchone()
        return dict(row)

    def get_couple_context(self, user_id: str = DEMO_USER) -> dict:
        now = utc_now()
        with self.connection() as db:
            couple, partner_id = self._couple_context(db, user_id)
            valid_codes = tuple(self.STATUS_PRESETS)
            placeholders = ",".join("?" for _ in valid_codes)
            db.execute(
                f"""DELETE FROM couple_statuses WHERE couple_id=? AND (
                    expires_at IS NULL OR expires_at<=? OR status_code NOT IN ({placeholders})
                )""",
                (couple["id"], now, *valid_codes),
            )
            rows = db.execute(
                "SELECT u.id, u.nickname, u.avatar_label, u.timezone AS account_timezone, s.status_code, s.emoji, s.text AS status_text, s.expires_at, s.updated_at AS status_updated_at, l.city, l.country, l.latitude, l.longitude, l.timezone FROM users u LEFT JOIN couple_statuses s ON s.user_id=u.id AND s.couple_id=? LEFT JOIN user_locations l ON l.user_id=u.id WHERE u.id IN (?, ?)",
                (couple["id"], user_id, partner_id),
            ).fetchall()
            trip = db.execute("SELECT title, start_date FROM trips WHERE couple_id=? AND deleted_at IS NULL AND start_date>=? ORDER BY start_date LIMIT 1", (couple["id"], date.today().isoformat())).fetchone()
        members = []
        for row in rows:
            item = dict(row)
            tz_name = item.get("timezone") or item.get("account_timezone") or "Asia/Shanghai"
            try:
                local = datetime.now(ZoneInfo(tz_name)).replace(microsecond=0)
            except ZoneInfoNotFoundError:
                tz_name = "Asia/Shanghai"
                local = datetime.now(ZoneInfo(tz_name)).replace(microsecond=0)
            item["timezone"] = tz_name
            item["local_time"] = local.isoformat()
            item["is_me"] = item["id"] == user_id
            if item.get("status_updated_at"):
                updated = datetime.fromisoformat(str(item["status_updated_at"]).replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                item["status_age_minutes"] = max(0, int((datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds() // 60))
                item["status_stale"] = item["status_age_minutes"] >= 24 * 60
            else:
                item["status_age_minutes"] = None
                item["status_stale"] = False
            item.pop("account_timezone", None)
            members.append(item)
        members.sort(key=lambda item: not item["is_me"])
        today = date.today()
        anniversary = date.fromisoformat(couple["anniversary"])
        next_anniversary = next_annual_occurrence(anniversary, today)
        context = {
            "members": members,
            "anniversary": couple["anniversary"],
            "anniversary_days": (next_anniversary - today).days,
            "next_trip": dict(trip) | {"days": (date.fromisoformat(trip["start_date"]) - today).days} if trip else None,
            "time_difference_minutes": self._time_difference_minutes(members),
        }
        with self.connection() as db:
            version_row = db.execute("SELECT version,snapshot_json FROM widget_snapshots WHERE user_id=?", (user_id,)).fetchone()
            def stable_widget_value(value: dict) -> dict:
                clean = json.loads(json.dumps(value, ensure_ascii=False))
                for member in clean.get("members", []):
                    member.pop("local_time", None)
                    member.pop("status_age_minutes", None)
                return clean
            previous = None
            if version_row:
                try:
                    previous = json.loads(version_row["snapshot_json"])
                except (TypeError, json.JSONDecodeError):
                    previous = None
            changed = previous is None or stable_widget_value(previous) != stable_widget_value(context)
            version = 1 if not version_row else int(version_row["version"]) + int(changed)
            db.execute(
                "INSERT INTO widget_snapshots(user_id, couple_id, version, snapshot_json, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET version=excluded.version, snapshot_json=excluded.snapshot_json, updated_at=excluded.updated_at",
                (user_id, couple["id"], version, json.dumps(context, ensure_ascii=False), now),
            )
        context["widget_version"] = version
        return context

    @staticmethod
    def _time_difference_minutes(members: list[dict]) -> int:
        if len(members) < 2:
            return 0
        utc_value = datetime.now(timezone.utc)
        offsets = [utc_value.astimezone(ZoneInfo(item["timezone"])).utcoffset() or timedelta() for item in members[:2]]
        return int(abs((offsets[0] - offsets[1]).total_seconds()) // 60)

    @staticmethod
    def _current_week() -> tuple[str, str]:
        today = date.today()
        start = today - timedelta(days=today.weekday())
        return start.isoformat(), (start + timedelta(days=6)).isoformat()

    def generate_weekly_review(self, user_id: str = DEMO_USER) -> dict:
        week_start, week_end = self._current_week()
        start_at = f"{week_start}T00:00:00+00:00"
        end_at = f"{(date.fromisoformat(week_end) + timedelta(days=1)).isoformat()}T00:00:00+00:00"
        with self.connection() as db:
            # Both partners can open a newly created space at the same time.
            # Acquire the write slot before checking/inserting so the unique
            # weekly review row is created once instead of producing a race.
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            existing = db.execute("SELECT id FROM weekly_reviews WHERE couple_id=? AND week_start=?", (couple["id"], week_start)).fetchone()
            if existing:
                review_id = existing["id"]
            else:
                review_id = new_id("weekly")
                moments = db.execute(
                    "SELECT COUNT(*) AS total, SUM(CASE WHEN type IN ('photo','video') THEN 1 ELSE 0 END) AS media FROM moments WHERE couple_id=? AND visibility='shared' AND deleted_at IS NULL AND occurred_at>=? AND occurred_at<?",
                    (couple["id"], start_at, end_at),
                ).fetchone()
                calls = db.execute(
                    "SELECT COUNT(*) AS total, COALESCE(SUM(duration_seconds),0) AS seconds FROM call_records WHERE couple_id=? AND started_at>=? AND started_at<? AND state='ended'",
                    (couple["id"], start_at, end_at),
                ).fetchone()
                completed = db.execute(
                    "SELECT COUNT(*) FROM shared_list_items i JOIN shared_lists l ON l.id=i.list_id WHERE l.couple_id=? AND i.deleted_at IS NULL AND i.completed_at>=? AND i.completed_at<?",
                    (couple["id"], start_at, end_at),
                ).fetchone()[0]
                dates = db.execute(
                    "SELECT COUNT(*) FROM date_plans WHERE couple_id=? AND status='completed' AND plan_date BETWEEN ? AND ?",
                    (couple["id"], week_start, week_end),
                ).fetchone()[0]
                summary = {
                    "momentCount": int(moments["total"] or 0),
                    "mediaCount": int(moments["media"] or 0),
                    "completedCount": int(completed),
                    "callCount": int(calls["total"] or 0),
                    "callSeconds": int(calls["seconds"] or 0),
                    "dateCount": int(dates),
                    "title": "这一周，我们又多了一些共同故事",
                }
                now = utc_now()
                db.execute(
                    "INSERT INTO weekly_reviews(id, couple_id, week_start, week_end, summary_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)",
                    (review_id, couple["id"], week_start, week_end, json.dumps(summary, ensure_ascii=False), now, now),
                )
                self._event(db, couple["id"], user_id, "weekly_review.created", review_id, {"weekStart": week_start})
        return self.get_weekly_review(review_id=review_id, user_id=user_id)

    def get_weekly_review(self, review_id: str | None = None, user_id: str = DEMO_USER) -> dict:
        week_start, _ = self._current_week()
        with self.connection() as db:
            couple, partner_id = self._couple_context(db, user_id)
            if review_id:
                row = db.execute("SELECT * FROM weekly_reviews WHERE id=? AND couple_id=?", (review_id, couple["id"])).fetchone()
            else:
                row = db.execute("SELECT * FROM weekly_reviews WHERE couple_id=? AND week_start=?", (couple["id"], week_start)).fetchone()
            if not row:
                row = None
            else:
                answers = db.execute(
                    "SELECT a.*, u.nickname FROM weekly_review_answers a JOIN users u ON u.id=a.user_id WHERE a.review_id=? ORDER BY a.submitted_at",
                    (row["id"],),
                ).fetchall()
        if row is None:
            return self.generate_weekly_review(user_id=user_id)
        both_submitted = len(answers) == 2
        visible_answers = []
        for answer in answers:
            value = dict(answer)
            if answer["user_id"] != user_id and not both_submitted:
                value = {"user_id": answer["user_id"], "nickname": answer["nickname"], "submitted": True, "locked": True, "confirmed_at": answer["confirmed_at"]}
            else:
                value["submitted"] = True
                value["locked"] = False
            visible_answers.append(value)
        return {
            **dict(row),
            "summary": parse_json(row["summary_json"], {}),
            "answers": visible_answers,
            "answered_by_me": any(answer["user_id"] == user_id for answer in answers),
            "partner_answered": any(answer["user_id"] == partner_id for answer in answers),
            "both_revealed": both_submitted,
            "confirmations": sum(1 for answer in answers if answer["confirmed_at"]),
        }

    def answer_weekly_review(self, review_id: str, payload: dict, user_id: str = DEMO_USER) -> dict:
        fields = {}
        for key, label in (("favorite", "最喜欢的一刻"), ("gratitude", "感谢"), ("next_week", "下周计划")):
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"请填写{label}")
            fields[key] = value.strip()[:500]
        now = utc_now()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            review = db.execute("SELECT * FROM weekly_reviews WHERE id=? AND couple_id=?", (review_id, couple["id"])).fetchone()
            if not review:
                raise KeyError("周回顾不存在")
            if review["status"] == "confirmed":
                raise ValueError("已确认的周回顾不能修改")
            db.execute(
                "INSERT INTO weekly_review_answers(review_id, user_id, favorite, gratitude, next_week, submitted_at, confirmed_at) VALUES (?, ?, ?, ?, ?, ?, NULL) ON CONFLICT(review_id,user_id) DO UPDATE SET favorite=excluded.favorite, gratitude=excluded.gratitude, next_week=excluded.next_week, submitted_at=excluded.submitted_at, confirmed_at=NULL",
                (review_id, user_id, fields["favorite"], fields["gratitude"], fields["next_week"], now),
            )
            db.execute("UPDATE weekly_reviews SET updated_at=? WHERE id=?", (now, review_id))
            self._event(db, couple["id"], user_id, "weekly_review.answered", review_id, {})
        return self.get_weekly_review(review_id, user_id)

    def confirm_weekly_review(self, review_id: str, user_id: str = DEMO_USER) -> dict:
        now = utc_now()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            review = db.execute("SELECT * FROM weekly_reviews WHERE id=? AND couple_id=?", (review_id, couple["id"])).fetchone()
            if not review:
                raise KeyError("周回顾不存在")
            own = db.execute("SELECT * FROM weekly_review_answers WHERE review_id=? AND user_id=?", (review_id, user_id)).fetchone()
            if not own:
                raise ValueError("请先填写本周回顾")
            db.execute("UPDATE weekly_review_answers SET confirmed_at=COALESCE(confirmed_at, ?) WHERE review_id=? AND user_id=?", (now, review_id, user_id))
            answers = db.execute("SELECT * FROM weekly_review_answers WHERE review_id=?", (review_id,)).fetchall()
            confirmed = len(answers) == 2 and all(row["confirmed_at"] for row in answers)
            if confirmed and review["status"] != "confirmed":
                moment_id = new_id("moment")
                summary = parse_json(review["summary_json"], {})
                body = "\n".join(
                    [
                        f"本周新增 {summary.get('momentCount', 0)} 个瞬间，完成 {summary.get('completedCount', 0)} 件共同小事。",
                        *[f"{row['gratitude']} · 下周：{row['next_week']}" for row in answers],
                    ]
                )
                db.execute(
                    "INSERT INTO moments(id, couple_id, creator_id, type, title, body, occurred_at, visibility) VALUES (?, ?, ?, 'weekly_review', ?, ?, ?, 'shared')",
                    (moment_id, couple["id"], user_id, f"{review['week_start']} 这一周", body[:5000], now),
                )
                db.execute("UPDATE weekly_reviews SET status='confirmed', moment_id=?, updated_at=? WHERE id=?", (moment_id, now, review_id))
                self._event(db, couple["id"], user_id, "moment.created", moment_id, {"type": "weekly_review"})
            self._event(db, couple["id"], user_id, "weekly_review.confirmed", review_id, {"complete": confirmed})
        return self.get_weekly_review(review_id, user_id)

    DATE_STYLES = {
        "romantic": ("轻松浪漫", "慢一点，把时间留给彼此", 0.90, "松弛"),
        "classic": ("经典充实", "吃饭、体验和散步都安排得刚刚好", 1.00, "适中"),
        "value": ("预算友好", "把预算用在最值得一起记住的地方", 0.68, "轻快"),
    }

    @staticmethod
    def _date_interest_tags(preferences: object) -> list[str]:
        if not isinstance(preferences, dict):
            return []
        raw = preferences.get("interests", preferences.get("interest", []))
        if isinstance(raw, str):
            values = re.split(r"[,，、/\s]+", raw)
        elif isinstance(raw, list):
            values = [str(item) for item in raw]
        else:
            values = []
        allowed = {"不限", "室内", "室外", "美食", "展览", "运动", "散步", "摄影", "电影", "手作", "自然"}
        return list(dict.fromkeys(item.strip() for item in values if item.strip() in allowed))[:8]

    @staticmethod
    def _date_plan_zone(candidate: dict | None) -> tuple[ZoneInfo | None, bool]:
        country = str((candidate or {}).get("country") or "").strip().casefold()
        provider_name = str((candidate or {}).get("provider") or "").strip()
        explicit_timezone = str((candidate or {}).get("timezone") or "").strip()
        if provider_name == "amap" or country in {"中国", "china", "cn"}:
            return ZoneInfo("Asia/Shanghai"), True
        if explicit_timezone:
            try:
                return ZoneInfo(explicit_timezone), True
            except ZoneInfoNotFoundError:
                pass
        # Account timezone is not destination evidence. An unverified overseas
        # or manual scope therefore cannot claim exact timed conflicts.
        return None, False

    @staticmethod
    def _month_occurrence(anchor: date, year: int, month: int) -> date:
        return date(year, month, min(anchor.day, calendar_module.monthrange(year, month)[1]))

    @classmethod
    def _recurrence_dates(cls, anchor: date, target: date, recurrence: str) -> list[date]:
        if recurrence == "none":
            return [anchor]
        if target < anchor:
            return []
        if recurrence == "daily":
            values = [target, target - timedelta(days=1)]
        elif recurrence == "weekly":
            latest = target - timedelta(days=(target - anchor).days % 7)
            values = [latest, latest - timedelta(days=7)]
        elif recurrence == "monthly":
            month_index = target.year * 12 + target.month - 1
            values = []
            for index in (month_index, month_index - 1):
                year, month_zero = divmod(index, 12)
                values.append(cls._month_occurrence(anchor, year, month_zero + 1))
        elif recurrence == "yearly":
            values = [
                date(year, anchor.month, min(anchor.day, calendar_module.monthrange(year, anchor.month)[1]))
                for year in (target.year, target.year - 1)
            ]
        else:
            return [anchor]
        return list(dict.fromkeys(value for value in values if value >= anchor))

    def _date_busy_context(
        self,
        db: sqlite3.Connection,
        *,
        user_id: str,
        plan_date: str,
        start_time: str,
        end_time: str,
        candidate: dict | None,
    ) -> dict:
        couple, partner_id = self._couple_context(db, user_id)
        rows = db.execute(
            """SELECT starts_at,ends_at,all_day,recurrence,participant_a_id,participant_b_id
            FROM calendar_entries
            WHERE couple_id=? AND deleted_at IS NULL AND blocks_time=1
              AND (participant_a_id IN (?,?) OR participant_b_id IN (?,?))""",
            (couple["id"], user_id, partner_id, user_id, partner_id),
        ).fetchall()
        plan_zone, timezone_confirmed = self._date_plan_zone(candidate)
        intervals: list[dict[str, object]] = []
        day_value = date.fromisoformat(plan_date)
        day_start = datetime.combine(day_value, datetime.min.time(), tzinfo=plan_zone) if plan_zone else None
        requested_start = day_start + timedelta(minutes=LocalDatePlanner._minutes(start_time)) if day_start else None
        requested_end = day_start + timedelta(minutes=LocalDatePlanner._minutes(end_time)) if day_start else None
        for row in rows:
            recurrence = str(row["recurrence"] or "none")
            if row["all_day"]:
                try:
                    base_start = date.fromisoformat(str(row["starts_at"])[:10])
                    base_end = date.fromisoformat(str(row["ends_at"])[:10]) if row["ends_at"] else base_start + timedelta(days=1)
                except ValueError:
                    continue
                if base_end <= base_start:
                    base_end = base_start + timedelta(days=1)
                duration = base_end - base_start
                if any(
                    occurrence < day_value + timedelta(days=1) and occurrence + duration > day_value
                    for occurrence in self._recurrence_dates(base_start, day_value, recurrence)
                ):
                    raise ValueError("这一天有明确的全天占用，不能生成双人约会")
                continue
            if not row["ends_at"] or plan_zone is None:
                continue
            try:
                base_start_dt = datetime.fromisoformat(str(row["starts_at"]).replace("Z", "+00:00"))
                base_end_dt = datetime.fromisoformat(str(row["ends_at"]).replace("Z", "+00:00"))
                if base_start_dt.tzinfo is None:
                    base_start_dt = base_start_dt.replace(tzinfo=plan_zone)
                if base_end_dt.tzinfo is None:
                    base_end_dt = base_end_dt.replace(tzinfo=plan_zone)
                base_start_dt = base_start_dt.astimezone(plan_zone)
                base_end_dt = base_end_dt.astimezone(plan_zone)
            except ValueError:
                continue
            duration = base_end_dt - base_start_dt
            if duration <= timedelta(0) or requested_start is None or requested_end is None:
                continue
            for occurrence_date in self._recurrence_dates(base_start_dt.date(), day_value, recurrence):
                occurrence_start = datetime(
                    occurrence_date.year, occurrence_date.month, occurrence_date.day,
                    base_start_dt.hour, base_start_dt.minute, base_start_dt.second,
                    tzinfo=plan_zone, fold=base_start_dt.fold,
                )
                occurrence_end = occurrence_start + duration
                overlap_start = max(occurrence_start, requested_start)
                overlap_end = min(occurrence_end, requested_end)
                if overlap_start < overlap_end:
                    intervals.append({
                        "start": overlap_start.strftime("%H:%M"),
                        "end": overlap_end.strftime("%H:%M"),
                        "all_day": False,
                    })
        intervals = [dict(item) for item in {
            (str(item["start"]), str(item["end"]), bool(item["all_day"])): item for item in intervals
        }.values()]
        intervals.sort(key=lambda item: (str(item["start"]), str(item["end"])))
        if len(LocalDatePlanner._free_starts({
            "start_time": start_time, "end_time": end_time, "busy_intervals": intervals,
        })) < 2:
            raise ValueError("双方空闲时间不足以安排一套完整约会")
        member_ids = [user_id, partner_id]
        busy_hash = hashlib.sha256(json.dumps({
            "couple_id": str(couple["id"]), "member_ids": sorted(member_ids),
            "plan_date": plan_date, "start_time": start_time, "end_time": end_time,
            "busy_intervals": intervals,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return {
            "couple_id": str(couple["id"]), "member_ids": member_ids,
            "busy_hash": busy_hash, "busy_intervals": intervals,
            "timezone_confirmed": timezone_confirmed,
        }

    def _date_context_snapshot(
        self,
        *,
        user_id: str,
        plan_date: str,
        start_time: str,
        end_time: str,
        place_candidate_id: object,
        preferences: object,
        weather_provider: object | None = None,
        candidate_snapshot: dict | None = None,
        place_candidates_snapshot: list[dict] | None = None,
    ) -> dict:
        candidate = dict(candidate_snapshot) if isinstance(candidate_snapshot, dict) else (
            self._destination_candidate(place_candidate_id, user_id) if place_candidate_id else None
        )
        if candidate is not None:
            candidate = {
                "candidate_id": str(candidate.get("candidate_id") or place_candidate_id or ""), **candidate,
                "source_type": str(candidate.get("source_type") or ("human" if candidate.get("provider") == "manual" else "legacy_unknown")),
                "evidence_at": str(candidate.get("evidence_at") or candidate.get("created_at") or ""),
            }
        with self.connection() as db:
            busy = self._date_busy_context(
                db, user_id=user_id, plan_date=plan_date, start_time=start_time,
                end_time=end_time, candidate=candidate,
            )
        weather: dict[str, object] = {"source_type": "unavailable"}
        if candidate and candidate.get("latitude") is not None and candidate.get("longitude") is not None and weather_provider:
            try:
                forecast = weather_provider.forecast(
                    candidate["latitude"], candidate["longitude"], plan_date, plan_date, "auto",
                )
            except (RuntimeError, ValueError, TimeoutError):
                forecast = None
            if isinstance(forecast, dict) and forecast.get("items"):
                item = forecast["items"][0]
                weather = {
                    "source_type": str(forecast.get("source_type") or "legacy_unknown"),
                    "original_source_type": str(forecast.get("original_source_type") or forecast.get("source_type") or "legacy_unknown"),
                    "provider": str(forecast.get("provider") or forecast.get("source") or "Open-Meteo")[:80],
                    "evidence_at": str(forecast.get("evidence_at") or ""),
                    "cache_age_seconds": forecast.get("cache_age_seconds"),
                    "summary": str(item.get("label") or "")[:100],
                    "precipitation_probability": item.get("precipitation_probability"),
                    "temperature_min": item.get("temperature_min"),
                    "temperature_max": item.get("temperature_max"),
                }
        interest_tags = self._date_interest_tags(preferences)
        if place_candidates_snapshot is not None:
            place_candidates = [dict(item) for item in place_candidates_snapshot if isinstance(item, dict)][:6]
        else:
            place_candidates = (
                self._destination_candidate_pool(place_candidate_id, user_id, 6, interest_tags)
                if candidate is not None else []
            )
        return {
            **busy,
            "interest_tags": interest_tags,
            "place_candidates": place_candidates,
            "location": candidate,
            "weather": weather,
        }

    def create_date_plan(
        self,
        payload: dict,
        user_id: str = DEMO_USER,
        weather_provider: object | None = None,
    ) -> dict:
        city_query = str(payload.get("city", "")).strip()
        title = str(payload.get("title", "我们的约会")).strip()[:100]
        plan_date = str(payload.get("plan_date", "")).strip()
        start_time = str(payload.get("start_time", "14:00"))
        end_time = str(payload.get("end_time", "21:00"))
        if not city_query or len(city_query) > 80:
            raise ValueError("请填写约会城市")
        try:
            date.fromisoformat(plan_date)
        except ValueError as error:
            raise ValueError("约会日期无效") from error
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start_time) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", end_time) or end_time <= start_time:
            raise ValueError("约会开始和结束时间无效")
        try:
            budget_cents = int(float(payload.get("budget", 0)) * 100)
        except (TypeError, ValueError) as error:
            raise ValueError("预算必须是数字") from error
        if budget_cents < 0 or budget_cents > 10_000_000:
            raise ValueError("约会预算超出范围")
        preferences = payload.get("preferences", {})
        if not isinstance(preferences, dict):
            raise ValueError("约会偏好必须是对象")
        context = self._date_context_snapshot(
            user_id=user_id, plan_date=plan_date, start_time=start_time, end_time=end_time,
            place_candidate_id=payload.get("place_candidate_id"), preferences=preferences,
            weather_provider=weather_provider,
        )
        location = context.get("location") or {}
        city = next((
            str(location.get(field) or "").strip()
            for field in ("city", "province", "district", "country", "name")
            if str(location.get(field) or "").strip()
        ), "") if location else city_query
        if location and not city:
            raise ValueError("已确认地点缺少城市或行政区域，请重新搜索")
        saved_preferences = {
            "interest_tags": context["interest_tags"], "location": context["location"],
            "place_candidates": context["place_candidates"],
            "busy_intervals": context["busy_intervals"], "weather": context["weather"],
            "timezone_confirmed": context["timezone_confirmed"],
            "generation_context": {
                "couple_id": context["couple_id"],
                "member_ids": sorted(context["member_ids"]),
                "busy_hash": context["busy_hash"],
            },
        }
        date_plan_id, now = new_id("date"), utc_now()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            db.execute(
                "INSERT INTO date_plans(id, couple_id, creator_id, title, plan_date, start_time, end_time, city, budget_cents, preferences_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)",
                (date_plan_id, couple["id"], user_id, title or "我们的约会", plan_date, start_time, end_time, city, budget_cents, json.dumps(saved_preferences, ensure_ascii=False), now, now),
            )
            self._event(db, couple["id"], user_id, "date_plan.created", date_plan_id, {})
        return self.generate_date_plan(date_plan_id, user_id=user_id)

    def _local_date_variants(self, plan: sqlite3.Row | dict) -> list[dict]:
        preferences = parse_json(plan["preferences_json"], {})
        return self.date_planner.generate_variants({
            "title": plan["title"],
            "city": plan["city"],
            "plan_date": plan["plan_date"],
            "start_time": plan["start_time"],
            "end_time": plan["end_time"],
            "budget_cents": plan["budget_cents"],
            "interest_tags": preferences.get("interest_tags", []),
            "busy_intervals": preferences.get("busy_intervals", []),
            "place_candidates": preferences.get("place_candidates") or ([preferences["location"]] if preferences.get("location") else []),
            "weather": preferences.get("weather", {"source_type": "unavailable"}),
        })

    def generate_date_plan(self, date_plan_id: str, user_id: str = DEMO_USER) -> dict:
        now = utc_now()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            plan = db.execute("SELECT * FROM date_plans WHERE id=? AND couple_id=?", (date_plan_id, couple["id"])).fetchone()
            if not plan:
                raise KeyError("约会计划不存在")
            if plan["status"] in {"confirmed", "completed"}:
                raise ValueError("已确认的约会不能重新生成")
            plan_snapshot = dict(plan)

        snapshot_preferences = parse_json(plan_snapshot["preferences_json"], {})
        persisted_location = snapshot_preferences.get("location") if isinstance(snapshot_preferences.get("location"), dict) else None
        persisted_pool = snapshot_preferences.get("place_candidates") if isinstance(snapshot_preferences.get("place_candidates"), list) else []
        # Establish the provider-before baseline from durable plan snapshots,
        # not the short-lived search-candidate table. This makes old plans
        # safely regenerable while still detecting every concurrent calendar
        # or relationship change made after the provider starts.
        try:
            expected_context = self._date_context_snapshot(
                user_id=user_id,
                plan_date=str(plan_snapshot["plan_date"]),
                start_time=str(plan_snapshot["start_time"]),
                end_time=str(plan_snapshot["end_time"]),
                place_candidate_id=None,
                preferences={"interests": snapshot_preferences.get("interest_tags", [])},
                weather_provider=None,
                candidate_snapshot=persisted_location,
                place_candidates_snapshot=persisted_pool,
            )
        except (KeyError, ValueError) as error:
            raise ConflictError("情侣关系或共同日历已变化，请重新生成约会方案") from error
        snapshot_preferences.update({
            "busy_intervals": expected_context["busy_intervals"],
            "timezone_confirmed": expected_context["timezone_confirmed"],
            "generation_context": {
                "couple_id": expected_context["couple_id"],
                "member_ids": sorted(expected_context["member_ids"]),
                "busy_hash": expected_context["busy_hash"],
            },
        })
        plan_snapshot["preferences_json"] = json.dumps(snapshot_preferences, ensure_ascii=False)
        legacy_scope_warning = not persisted_location or str(persisted_location.get("source_type") or "legacy_unknown") not in {"real_api", "cache"}

        # The provider may reserve an external quota and perform a slow network
        # request. Never keep a SQLite transaction open while that happens: the
        # atomic quota reservation uses its own connection, and both partners
        # must remain able to read and write while the model is responding.
        variants = self._local_date_variants(plan_snapshot)
        for variant in variants:
            generation_source = str(variant.get("generation_source") or "legacy_unknown")
            generation_message = str(variant.get("generation_message") or "")[:120]
            if legacy_scope_warning:
                generation_message = (
                    "旧计划地点范围尚未地图核验，请重新选择真实地点；" + generation_message
                )[:120]
            for item in variant.get("items", []):
                item["generationSource"] = generation_source
                item["generationMessage"] = generation_message

        try:
            current_context = self._date_context_snapshot(
                user_id=user_id,
                plan_date=str(plan_snapshot["plan_date"]),
                start_time=str(plan_snapshot["start_time"]),
                end_time=str(plan_snapshot["end_time"]),
                place_candidate_id=None,
                preferences={"interests": snapshot_preferences.get("interest_tags", [])},
                weather_provider=None,
                candidate_snapshot=persisted_location,
                place_candidates_snapshot=persisted_pool,
            )
        except (KeyError, ValueError) as error:
            raise ConflictError("情侣关系或共同日历已变化，请重新生成约会方案") from error
        if (
            str(current_context.get("couple_id")) != str(expected_context.get("couple_id"))
            or sorted(current_context.get("member_ids", [])) != sorted(expected_context.get("member_ids", []))
            or str(current_context.get("busy_hash")) != str(expected_context.get("busy_hash"))
        ):
            raise ConflictError("共同日历的空闲时间已变化，请重新生成约会方案")

        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                final_context = self._date_busy_context(
                    db,
                    user_id=user_id,
                    plan_date=str(plan_snapshot["plan_date"]),
                    start_time=str(plan_snapshot["start_time"]),
                    end_time=str(plan_snapshot["end_time"]),
                    candidate=persisted_location,
                )
            except ValueError as error:
                raise ConflictError("情侣关系或共同日历已变化，请重新生成约会方案") from error
            if (
                str(final_context.get("couple_id")) != str(expected_context.get("couple_id"))
                or sorted(final_context.get("member_ids", [])) != sorted(expected_context.get("member_ids", []))
                or str(final_context.get("busy_hash")) != str(expected_context.get("busy_hash"))
            ):
                raise ConflictError("共同日历的空闲时间已变化，请重新生成约会方案")
            couple_id = str(final_context["couple_id"])
            couple = db.execute("SELECT * FROM couples WHERE id=? AND status='active'", (couple_id,)).fetchone()
            if not couple:
                raise ConflictError("情侣关系已变化，请重新生成约会方案")
            plan = db.execute("SELECT * FROM date_plans WHERE id=? AND couple_id=?", (date_plan_id, couple["id"])).fetchone()
            if not plan:
                raise KeyError("约会计划不存在")
            if plan["status"] in {"confirmed", "completed"}:
                raise ValueError("已确认的约会不能重新生成")
            if int(plan["version"]) != int(plan_snapshot["version"]):
                raise ConflictError("约会计划已被另一方修改，请刷新后重试")
            db.execute("DELETE FROM date_plan_votes WHERE date_plan_id=?", (date_plan_id,))
            db.execute("DELETE FROM date_plan_variants WHERE date_plan_id=?", (date_plan_id,))
            for value in variants:
                variant_id = new_id("date_variant")
                db.execute(
                    "INSERT INTO date_plan_variants(id, date_plan_id, style, name, summary, expected_cost_cents, intensity, weather_fit, items_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (variant_id, date_plan_id, value["style"], value["name"], value["summary"], value["expected_cost_cents"], value["intensity"], value["weather_fit"], json.dumps(value["items"], ensure_ascii=False), now),
                )
            db.execute(
                "UPDATE date_plans SET status='voting', selected_variant_id=NULL, final_items_json='[]', preferences_json=?, version=version+1, updated_at=? WHERE id=?",
                (json.dumps(snapshot_preferences, ensure_ascii=False), now, date_plan_id),
            )
            self._event(db, couple["id"], user_id, "date_plan.generated", date_plan_id, {})
        return self.get_date_plan(date_plan_id, user_id)

    def list_date_plans(self, user_id: str = DEMO_USER) -> list[dict]:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            rows = db.execute("SELECT id FROM date_plans WHERE couple_id=? ORDER BY plan_date DESC, created_at DESC LIMIT 20", (couple["id"],)).fetchall()
        return [self.get_date_plan(row["id"], user_id) for row in rows]

    def get_date_plan(self, date_plan_id: str, user_id: str = DEMO_USER) -> dict:
        with self.connection() as db:
            couple, partner_id = self._couple_context(db, user_id)
            plan = db.execute("SELECT * FROM date_plans WHERE id=? AND couple_id=?", (date_plan_id, couple["id"])).fetchone()
            if not plan:
                raise KeyError("约会计划不存在")
            variants = db.execute("SELECT * FROM date_plan_variants WHERE date_plan_id=? ORDER BY CASE style WHEN 'romantic' THEN 1 WHEN 'classic' THEN 2 WHEN 'value' THEN 3 WHEN 'food' THEN 4 WHEN 'adventure' THEN 5 ELSE 6 END", (date_plan_id,)).fetchall()
            votes = db.execute("SELECT v.*, u.nickname FROM date_plan_votes v JOIN users u ON u.id=v.user_id WHERE v.date_plan_id=?", (date_plan_id,)).fetchall()
            confirmations = db.execute("SELECT user_id FROM date_plan_confirmations WHERE date_plan_id=? AND version=?", (date_plan_id, plan["version"])).fetchall()
        both_voted = len(votes) == 2
        public_votes = []
        for vote in votes:
            if both_voted or vote["user_id"] == user_id:
                public_votes.append(dict(vote))
            else:
                public_votes.append({"user_id": vote["user_id"], "nickname": vote["nickname"], "locked": True})
        result = dict(plan)
        result["preferences"] = parse_json(result.pop("preferences_json"), {})
        result["preferences"].pop("generation_context", None)
        result["preferences"].pop("place_candidates", None)
        result["location"] = result["preferences"].get("location")
        result["busy_intervals"] = result["preferences"].get("busy_intervals", [])
        result["weather"] = result["preferences"].get("weather", {"source_type": "unavailable"})
        result["final_items"] = parse_json(result.pop("final_items_json"), [])
        result["variants"] = [{**dict(row), "items": parse_json(row["items_json"], [])} for row in variants]
        for value in result["variants"]:
            value.pop("items_json", None)
            value["generation_source"] = (
                value["items"][0].get("generationSource", value["items"][0].get("recommendationSource", "legacy_unknown"))
                if value["items"] else "legacy_unknown"
            )
            value["generation_message"] = (
                value["items"][0].get("generationMessage") if value["items"]
                else ""
            ) or (
                "由模型根据已确认信息生成" if value["generation_source"] == "ai"
                else "使用本地安全规则生成" if value["generation_source"] == "rules"
                else "来源未记录"
            )
        sources = {value["generation_source"] for value in result["variants"]}
        result["generation_source"] = next(iter(sources)) if len(sources) == 1 else "legacy_unknown"
        messages = list(dict.fromkeys(value["generation_message"] for value in result["variants"] if value["generation_message"]))
        result["generation_message"] = messages[0] if len(messages) == 1 else "方案来源不同，请查看每套方案标记"
        result["votes"] = public_votes
        result["both_voted"] = both_voted
        result["my_vote"] = next((vote["variant_id"] for vote in votes if vote["user_id"] == user_id), None)
        result["partner_voted"] = any(vote["user_id"] == partner_id for vote in votes)
        result["confirmations"] = len(confirmations)
        result["confirmed_by_me"] = any(row["user_id"] == user_id for row in confirmations)
        return result

    def vote_date_plan(self, date_plan_id: str, variant_id: str, user_id: str = DEMO_USER) -> dict:
        now = utc_now()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            plan = db.execute("SELECT * FROM date_plans WHERE id=? AND couple_id=?", (date_plan_id, couple["id"])).fetchone()
            variant = db.execute("SELECT * FROM date_plan_variants WHERE id=? AND date_plan_id=?", (variant_id, date_plan_id)).fetchone()
            if not plan or not variant:
                raise KeyError("约会方案不存在")
            if plan["status"] not in {"voting", "draft"}:
                raise ValueError("当前约会已经结束投票")
            db.execute(
                "INSERT INTO date_plan_votes(date_plan_id, user_id, variant_id, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(date_plan_id,user_id) DO UPDATE SET variant_id=excluded.variant_id, updated_at=excluded.updated_at",
                (date_plan_id, user_id, variant_id, now),
            )
            vote_count = db.execute("SELECT COUNT(*) FROM date_plan_votes WHERE date_plan_id=?", (date_plan_id,)).fetchone()[0]
            if vote_count == 2:
                db.execute("UPDATE date_plan_votes SET revealed=1 WHERE date_plan_id=?", (date_plan_id,))
            self._event(db, couple["id"], user_id, "date_vote.updated", date_plan_id, {"votes": vote_count})
        return self.get_date_plan(date_plan_id, user_id)

    def compose_date_plan(self, date_plan_id: str, payload: dict, user_id: str = DEMO_USER) -> dict:
        variant_id = str(payload.get("variant_id", "")).strip()
        selected_ids = payload.get("item_ids")
        if selected_ids is not None and (not isinstance(selected_ids, list) or len(selected_ids) > 20):
            raise ValueError("约会项目选择无效")
        now = utc_now()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            plan = db.execute("SELECT * FROM date_plans WHERE id=? AND couple_id=?", (date_plan_id, couple["id"])).fetchone()
            variant = db.execute("SELECT * FROM date_plan_variants WHERE id=? AND date_plan_id=?", (variant_id, date_plan_id)).fetchone()
            if not plan or not variant:
                raise KeyError("约会方案不存在")
            all_items = []
            variants = db.execute("SELECT items_json FROM date_plan_variants WHERE date_plan_id=?", (date_plan_id,)).fetchall()
            for row in variants:
                all_items.extend(parse_json(row["items_json"], []))
            requested = set(str(value) for value in selected_ids) if selected_ids else {item["id"] for item in parse_json(variant["items_json"], [])}
            final_items = [item for item in all_items if item.get("id") in requested]
            if not final_items:
                raise ValueError("至少选择一个约会项目")
            final_items.sort(key=lambda item: item.get("startTime", ""))
            new_version = int(plan["version"]) + 1
            db.execute(
                "UPDATE date_plans SET selected_variant_id=?, final_items_json=?, status='pending_confirmation', version=?, updated_at=? WHERE id=?",
                (variant_id, json.dumps(final_items, ensure_ascii=False), new_version, now, date_plan_id),
            )
            db.execute("DELETE FROM date_plan_confirmations WHERE date_plan_id=?", (date_plan_id,))
            self._event(db, couple["id"], user_id, "date_plan.composed", date_plan_id, {"version": new_version})
        return self.get_date_plan(date_plan_id, user_id)

    def confirm_date_plan(self, date_plan_id: str, expected_version: object = None, user_id: str = DEMO_USER) -> dict:
        now = utc_now()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            plan = db.execute("SELECT * FROM date_plans WHERE id=? AND couple_id=?", (date_plan_id, couple["id"])).fetchone()
            if not plan:
                raise KeyError("约会计划不存在")
            if plan["status"] not in {"pending_confirmation", "confirmed"}:
                raise ValueError("请先选出最终约会方案")
            if expected_version is not None and int(expected_version) != plan["version"]:
                raise ConflictError("约会方案已被另一方修改，请刷新后重新确认")
            db.execute(
                "INSERT OR IGNORE INTO date_plan_confirmations(date_plan_id, version, user_id, confirmed_at) VALUES (?, ?, ?, ?)",
                (date_plan_id, plan["version"], user_id, now),
            )
            count = db.execute("SELECT COUNT(*) FROM date_plan_confirmations WHERE date_plan_id=? AND version=?", (date_plan_id, plan["version"])).fetchone()[0]
            if count == 2:
                db.execute("UPDATE date_plans SET status='confirmed', updated_at=? WHERE id=?", (now, date_plan_id))
            self._event(db, couple["id"], user_id, "date_plan.confirmed", date_plan_id, {"confirmations": count})
        return self.get_date_plan(date_plan_id, user_id)

    def complete_date_plan(self, date_plan_id: str, payload: dict, user_id: str = DEMO_USER) -> dict:
        now = utc_now()
        note = str(payload.get("note", "这次约会，值得好好记住。"))[:2000]
        try:
            actual_cost = int(float(payload.get("actual_cost", 0)) * 100)
        except (TypeError, ValueError) as error:
            raise ValueError("实际花费必须是数字") from error
        if actual_cost < 0 or actual_cost > 10_000_000:
            raise ValueError("实际花费超出范围")
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            plan = db.execute("SELECT * FROM date_plans WHERE id=? AND couple_id=?", (date_plan_id, couple["id"])).fetchone()
            if not plan:
                raise KeyError("约会计划不存在")
            if plan["status"] == "completed":
                return self.get_date_plan(date_plan_id, user_id)
            if plan["status"] != "confirmed":
                raise ValueError("约会需双方确认后才能完成")
            media_value = payload.get("media_id")
            if media_value is not None and not isinstance(media_value, str):
                raise ValueError("约会媒体 ID 无效")
            media_id = media_value.strip() or None if isinstance(media_value, str) else None
            media_url = None
            if media_id:
                media = db.execute("SELECT * FROM media_assets WHERE id=? AND couple_id=? AND owner_id=? AND status='ready'", (media_id, couple["id"], user_id)).fetchone()
                if not media:
                    raise ValueError("约会照片不存在")
                media_url = f"/api/v1/media/{media_id}"
            moment_id = new_id("moment")
            body = f"{note}\n实际花费：¥{actual_cost / 100:.2f}" if actual_cost else note
            db.execute(
                "INSERT INTO moments(id, couple_id, creator_id, type, title, body, occurred_at, location, visibility, media_url, media_id) VALUES (?, ?, ?, 'date', ?, ?, ?, ?, 'shared', ?, ?)",
                (moment_id, couple["id"], user_id, plan["title"], body, now, plan["city"], media_url, media_id),
            )
            if media_id:
                self._sync_media_visibility(db, media_id)
            db.execute("UPDATE date_plans SET status='completed', moment_id=?, updated_at=? WHERE id=?", (moment_id, now, date_plan_id))
            self._event(db, couple["id"], user_id, "date_plan.completed", date_plan_id, {"momentId": moment_id})
            self._event(db, couple["id"], user_id, "moment.created", moment_id, {"type": "date"})
        return self.get_date_plan(date_plan_id, user_id)

    def list_messages(self, limit: int = 100, user_id: str = DEMO_USER) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            rows = db.execute(
                "SELECT * FROM (SELECT m.*,u.nickname AS sender_name FROM messages m JOIN users u ON u.id=m.sender_id WHERE couple_id=? ORDER BY sent_at DESC,m.id DESC LIMIT ?) ORDER BY sent_at ASC,id ASC",
                (couple["id"], limit),
            ).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            item["metadata"] = parse_json(item["metadata"], {})
            item["mine"] = item["sender_id"] == user_id
            item["delivery_status"] = "read" if item["read_at"] else ("delivered" if item["mine"] else "received")
        return items

    def create_message(self, payload: dict, user_id: str = DEMO_USER) -> dict:
        content = str(payload.get("content", "")).strip()
        if not content:
            raise ValueError("消息内容不能为空")
        if len(content) > 4000:
            raise ValueError("消息内容不能超过 4000 个字符")
        message_id, sent_at = new_id("msg"), utc_now()
        message_type = str(payload.get("type", "text"))
        if message_type not in {"text", "image", "voice", "video", "location", "trip"}:
            raise ValueError("不支持的消息类型")
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("消息 metadata 必须是对象")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, partner_id = self._couple_context(db, user_id)
            previous = self._idempotent_result(db, couple["id"], user_id, "message.create", payload.get("idempotency_key"))
            if previous:
                return previous
            media_id = metadata.get("mediaId")
            if media_id:
                media = db.execute("SELECT * FROM media_assets WHERE id=? AND couple_id=? AND status='ready'", (media_id, couple["id"])).fetchone()
                if not media:
                    raise ValueError("消息引用的媒体不存在")
                if media["owner_id"] != user_id:
                    raise PermissionError("只能发送自己上传的媒体")
                expected_kind = {"image": "image", "voice": "audio", "video": "video"}.get(message_type)
                if expected_kind and media["kind"] != expected_kind:
                    raise ValueError("消息类型与媒体类型不匹配")
            db.execute(
                "INSERT INTO messages(id, couple_id, sender_id, type, content, metadata, sent_at, read_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (message_id, couple["id"], user_id, message_type, content, json.dumps(metadata, ensure_ascii=False), sent_at, None),
            )
            if media_id:
                self._sync_media_visibility(db, media_id)
            self._audit(db, "message.created", "message", message_id, actor_id=user_id)
            row = db.execute(
                "SELECT m.*, u.nickname AS sender_name FROM messages m JOIN users u ON u.id=m.sender_id WHERE m.id=?",
                (message_id,),
            ).fetchone()
            result = dict(row)
            result["metadata"] = parse_json(result["metadata"], {})
            result["mine"] = True
            result["delivery_status"] = "delivered"
            self._save_idempotent_result(db, couple["id"], user_id, "message.create", payload.get("idempotency_key"), result)
            actor = db.execute(
                "SELECT nickname FROM users WHERE id=?", (user_id,)
            ).fetchone()
            self._insert_in_app_notification(
                db,
                couple_id=couple["id"],
                recipient_id=partner_id,
                actor_id=user_id,
                event_type="message.created",
                resource_type="message",
                resource_id=message_id,
                title="恋屿有一条新消息",
                body=f"{actor['nickname']}给你发来一条消息",
                url="/?screen=chat",
                dedupe_key=f"message:{message_id}",
            )
            self._event(db, couple["id"], user_id, "message.created", message_id, {"type": message_type})
        return result

    def mark_messages_read(self, user_id: str = DEMO_USER) -> dict:
        now = utc_now()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            rows = db.execute(
                "SELECT id FROM messages WHERE couple_id=? AND sender_id<>? AND read_at IS NULL",
                (couple["id"], user_id),
            ).fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                db.execute(
                    "UPDATE messages SET read_at=? WHERE couple_id=? AND sender_id<>? AND read_at IS NULL",
                    (now, couple["id"], user_id),
                )
                placeholders = ",".join("?" for _ in ids)
                db.execute(
                    f"""
                    UPDATE in_app_notifications
                    SET read_at=COALESCE(read_at,?)
                    WHERE couple_id=? AND recipient_id=? AND resource_type='message'
                      AND resource_id IN ({placeholders})
                    """,
                    (now, couple["id"], user_id, *ids),
                )
                self._event(db, couple["id"], user_id, "message.read", ids[-1], {"messageIds": ids, "readAt": now})
        return {"updated": len(ids), "read_at": now}

    def suggest_expression(
        self,
        *,
        mode: object,
        message_id: object = None,
        draft: object = None,
        idempotency_key: object,
        user_id: str,
    ) -> dict:
        """Rewrite exactly one caller-owned text without reading chat context."""
        idempotency_key = str(idempotency_key or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9:._-]{8,160}", idempotency_key):
            raise ValueError("幂等键格式无效")
        if mode not in MODES:
            raise ValueError("表达模式无效")
        has_message = isinstance(message_id, str) and bool(message_id.strip())
        has_draft = isinstance(draft, str) and bool(draft.strip())
        if has_message == has_draft:
            raise ValueError("请选择本人一条消息或填写当前草稿，且只能选择一种")
        request_payload = {
            "mode": mode,
            "message_id": message_id.strip() if has_message else None,
            "draft": draft.strip() if has_draft else None,
        }
        request_hash = hashlib.sha256(
            json.dumps(request_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        request_id = new_id("expression")
        owner_token = secrets.token_hex(24)
        # Provider calls are capped at 60 seconds.  Wait beyond the 75-second
        # stale boundary so an abandoned owner is recovered by this request
        # instead of leaving a durable pending receipt behind.
        wait_deadline = time.monotonic() + 80
        couple_id = ""
        couple_version = 0
        initial_couple_id: str | None = None
        initial_couple_version: int | None = None
        source_text = ""
        poll_attempt = 0

        # Claim one provider execution before leaving SQLite.  Exact concurrent
        # retries poll through read-only connections; only the initial insert or
        # a confirmed stale takeover briefly acquires a write transaction.
        while True:
            owns_pending = False
            with self.connection() as db:
                try:
                    couple, _ = self._couple_context(db, user_id)
                except ValueError as error:
                    if initial_couple_id is not None:
                        raise ConflictError("情侣空间已经变化，请重新提交") from error
                    raise
                couple_id = str(couple["id"])
                couple_version = int(couple["version"])
                if initial_couple_id is None:
                    initial_couple_id = couple_id
                    initial_couple_version = couple_version
                elif couple_id != initial_couple_id or couple_version != initial_couple_version:
                    raise ConflictError("情侣空间已经变化，请重新提交")
                if has_message:
                    message = db.execute(
                        "SELECT id,sender_id,type,content FROM messages WHERE id=? AND couple_id=?",
                        (request_payload["message_id"], couple_id),
                    ).fetchone()
                    if not message or message["sender_id"] != user_id:
                        raise PermissionError("只能整理自己明确选择的文字消息")
                    if message["type"] != "text" or not str(message["content"] or "").strip():
                        raise ValueError("只能整理本人发送的文字消息")
                    source_text = str(message["content"]).strip()
                    if len(source_text) > 4000:
                        raise ValueError("所选文字不能超过 4000 个字符")
                else:
                    source_text = str(request_payload["draft"] or "").strip()
                    if not 1 <= len(source_text) <= 4000:
                        raise ValueError("当前草稿长度需为 1–4000 个字符")
                row = db.execute(
                    """SELECT result_json,created_at FROM mutation_idempotency
                    WHERE couple_id=? AND user_id=? AND operation='expression.suggest'
                      AND idempotency_key=?""",
                    (couple_id, user_id, idempotency_key),
                ).fetchone()
                receipt = parse_json(row["result_json"], {}) if row else None
                if receipt is not None:
                    if receipt.get("_request_hash") != request_hash:
                        raise ConflictError("幂等标识已用于另一项表达请求")
                    state = str(receipt.get("_state") or "legacy")
                    if state == "succeeded":
                        saved = receipt.get("result")
                        if not isinstance(saved, dict):
                            raise ConflictError("表达请求回执无效，请重新提交")
                        return dict(saved)
                    if state == "failed":
                        if receipt.get("failure_code") == "scope_changed":
                            raise ConflictError("情侣空间已经变化，请重新提交")
                        raise RuntimeError("表达辅助暂时不可用，原草稿已保留，请稍后重试")
                    if state == "legacy":
                        return {key: value for key, value in receipt.items() if key != "_request_hash"}
                    started_raw = str(receipt.get("_started_at") or row["created_at"])
                    try:
                        started_at = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
                        if started_at.tzinfo is None:
                            started_at = started_at.replace(tzinfo=timezone.utc)
                    except ValueError:
                        started_at = datetime.fromtimestamp(0, timezone.utc)
                    stale = (datetime.now(timezone.utc) - started_at.astimezone(timezone.utc)).total_seconds() >= 75
                else:
                    stale = False

            if row is None:
                with self.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        current_couple, _ = self._couple_context(db, user_id)
                    except ValueError as error:
                        raise ConflictError("情侣空间已经变化，请重新提交") from error
                    if (
                        str(current_couple["id"]) != initial_couple_id
                        or int(current_couple["version"]) != initial_couple_version
                    ):
                        raise ConflictError("情侣空间已经变化，请重新提交")
                    existing = db.execute(
                        """SELECT 1 FROM mutation_idempotency
                        WHERE couple_id=? AND user_id=? AND operation='expression.suggest'
                          AND idempotency_key=?""",
                        (couple_id, user_id, idempotency_key),
                    ).fetchone()
                    if existing:
                        continue
                    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).replace(microsecond=0).isoformat()
                    db.execute(
                        """DELETE FROM mutation_idempotency
                        WHERE operation='expression.suggest' AND created_at<=?
                          AND COALESCE(json_extract(result_json,'$._state'),'')<>'pending'""",
                        (cutoff,),
                    )
                    pending = {
                        "_state": "pending", "_request_hash": request_hash,
                        "_owner_token": owner_token, "_request_id": request_id,
                        "_started_at": utc_now(),
                    }
                    db.execute(
                        """INSERT INTO mutation_idempotency(
                        couple_id,user_id,operation,idempotency_key,result_json,created_at
                        ) VALUES (?,?, 'expression.suggest',?,?,?)""",
                        (couple_id, user_id, idempotency_key, json.dumps(pending, ensure_ascii=False), pending["_started_at"]),
                    )
                    owns_pending = True
                    if has_message:
                        expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).replace(microsecond=0).isoformat()
                        grant_id = new_id("grant")
                        db.execute(
                            """INSERT INTO ai_context_grants(
                                id,couple_id,user_id,surface,resource_type,resource_id,purpose,
                                expires_at,created_at,resource_version,usage_scope,request_id,last_used_at
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                grant_id, couple_id, user_id, "chat", "message", request_payload["message_id"],
                                "expression.suggest", expires, utc_now(), 1, "selected_content",
                                request_id, utc_now(),
                            ),
                        )
            elif stale:
                with self.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        current_couple, _ = self._couple_context(db, user_id)
                    except ValueError as error:
                        raise ConflictError("情侣空间已经变化，请重新提交") from error
                    if (
                        str(current_couple["id"]) != initial_couple_id
                        or int(current_couple["version"]) != initial_couple_version
                    ):
                        raise ConflictError("情侣空间已经变化，请重新提交")
                    current_row = db.execute(
                        """SELECT result_json,created_at FROM mutation_idempotency
                        WHERE couple_id=? AND user_id=? AND operation='expression.suggest'
                          AND idempotency_key=?""",
                        (couple_id, user_id, idempotency_key),
                    ).fetchone()
                    if not current_row:
                        continue
                    current_receipt = parse_json(current_row["result_json"], {})
                    if current_receipt.get("_request_hash") != request_hash:
                        raise ConflictError("幂等标识已用于另一项表达请求")
                    if current_receipt.get("_state") != "pending":
                        continue
                    current_started_raw = str(current_receipt.get("_started_at") or current_row["created_at"])
                    try:
                        current_started = datetime.fromisoformat(current_started_raw.replace("Z", "+00:00"))
                        if current_started.tzinfo is None:
                            current_started = current_started.replace(tzinfo=timezone.utc)
                    except ValueError:
                        current_started = datetime.fromtimestamp(0, timezone.utc)
                    if (datetime.now(timezone.utc) - current_started.astimezone(timezone.utc)).total_seconds() < 75:
                        continue
                    old_request_id = str(current_receipt.get("_request_id") or "")
                    if old_request_id:
                        db.execute(
                            """UPDATE ai_usage_events SET status='unknown',finished_at=?,
                            failure_code='process_interrupted'
                            WHERE couple_id=? AND request_id=? AND status='started'""",
                            (utc_now(), couple_id, old_request_id),
                        )
                    pending = {
                        "_state": "pending", "_request_hash": request_hash,
                        "_owner_token": owner_token, "_request_id": request_id,
                        "_started_at": utc_now(),
                    }
                    db.execute(
                        """UPDATE mutation_idempotency SET result_json=?,created_at=?
                        WHERE couple_id=? AND user_id=? AND operation='expression.suggest'
                          AND idempotency_key=?""",
                        (json.dumps(pending, ensure_ascii=False), pending["_started_at"], couple_id, user_id, idempotency_key),
                    )
                    owns_pending = True
                    if has_message:
                        expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).replace(microsecond=0).isoformat()
                        db.execute(
                            """INSERT INTO ai_context_grants(
                                id,couple_id,user_id,surface,resource_type,resource_id,purpose,
                                expires_at,created_at,resource_version,usage_scope,request_id,last_used_at
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                new_id("grant"), couple_id, user_id, "chat", "message", request_payload["message_id"],
                                "expression.suggest", expires, utc_now(), 1, "selected_content",
                                request_id, utc_now(),
                            ),
                        )
            if owns_pending:
                break
            if time.monotonic() >= wait_deadline:
                raise RuntimeError("相同表达请求仍在处理中，请稍后重试")
            base_delay = min(0.5, 0.025 * (2 ** min(poll_attempt, 5)))
            jitter = 0.75 + (secrets.randbelow(501) / 1000)
            time.sleep(min(base_delay * jitter, max(0.0, wait_deadline - time.monotonic())))
            poll_attempt += 1

        provider_name = str(getattr(self.expression_assistant, "name", "unknown"))[:100]
        provider_model = str(getattr(self.expression_assistant, "model", "unknown"))[:100]
        is_external = bool(getattr(self.expression_assistant, "is_external", False))
        if is_external:
            try:
                self.record_ai_usage_event(
                    actor_kind="user", feature="expression.suggest", provider=provider_name,
                    model=provider_model, request_id=request_id, status="started",
                    request_sent=False, user_id=user_id, couple_id=couple_id,
                )
            except (PermissionError, ValueError) as error:
                with self.connection() as db:
                    failed = {
                        "_state": "failed", "_request_hash": request_hash,
                        "failure_code": "scope_changed", "_request_id": request_id,
                    }
                    db.execute(
                        """UPDATE mutation_idempotency SET result_json=?
                        WHERE couple_id=? AND user_id=? AND operation='expression.suggest'
                          AND idempotency_key=? AND json_extract(result_json,'$._owner_token')=?""",
                        (json.dumps(failed, ensure_ascii=False), couple_id, user_id, idempotency_key, owner_token),
                    )
                raise ConflictError("情侣空间已经变化，请重新提交") from error

            # This short transaction is the dispatch boundary: a lifecycle
            # change either wins first and prevents the external call, or this
            # marks the one usage row as sent before provider I/O begins.
            with self.connection() as db:
                dispatch_allowed = db.execute(
                    """UPDATE ai_usage_events SET request_sent=1
                    WHERE request_id=? AND provider=? AND model=? AND status='started'
                      AND request_sent=0
                      AND EXISTS (
                        SELECT 1 FROM couples c JOIN users u ON u.id=?
                        WHERE c.id=? AND (c.member_a=? OR c.member_b=?)
                          AND c.status='active' AND c.version=? AND u.deleted_at IS NULL
                      )
                      AND EXISTS (
                        SELECT 1 FROM mutation_idempotency m
                        WHERE m.couple_id=? AND m.user_id=?
                          AND m.operation='expression.suggest' AND m.idempotency_key=?
                          AND json_extract(m.result_json,'$._owner_token')=?
                          AND json_extract(m.result_json,'$._state')='pending'
                      )""",
                    (
                        request_id, provider_name, provider_model,
                        user_id, couple_id, user_id, user_id, couple_version,
                        couple_id, user_id, idempotency_key, owner_token,
                    ),
                ).rowcount == 1
                if not dispatch_allowed:
                    db.execute(
                        """UPDATE ai_usage_events SET status='failed',finished_at=?,
                        failure_code='scope_changed'
                        WHERE request_id=? AND provider=? AND model=? AND status='started'""",
                        (utc_now(), request_id, provider_name, provider_model),
                    )
                    failed = {
                        "_state": "failed", "_request_hash": request_hash,
                        "failure_code": "scope_changed", "_request_id": request_id,
                    }
                    db.execute(
                        """UPDATE mutation_idempotency SET result_json=?
                        WHERE couple_id=? AND user_id=? AND operation='expression.suggest'
                          AND idempotency_key=? AND json_extract(result_json,'$._owner_token')=?""",
                        (
                            json.dumps(failed, ensure_ascii=False), couple_id, user_id,
                            idempotency_key, owner_token,
                        ),
                    )
            if not dispatch_allowed:
                raise ConflictError("情侣空间已经变化，请重新提交")
        started = datetime.now(timezone.utc)
        provider_error: Exception | None = None
        suggestion = None
        try:
            suggestion = self.expression_assistant.suggest(
                source_text, mode=str(mode), request_id=request_id,
            )
            if not isinstance(suggestion.text, str) or not suggestion.text.strip() or len(suggestion.text.strip()) > 4000:
                raise ValueError("表达服务返回内容无效")
        except Exception as error:
            provider_error = error

        latency_ms = max(0, int((datetime.now(timezone.utc) - started).total_seconds() * 1000))
        result = None if suggestion is None else {
            "text": suggestion.text.strip(), "mode": str(mode), "provider": suggestion.provider,
            "model": suggestion.model, "source": "message" if has_message else "draft",
        }
        final_error: str | None = None
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            scope = db.execute(
                """SELECT c.status,c.version,u.deleted_at
                FROM couples c JOIN users u ON u.id=?
                WHERE c.id=? AND (c.member_a=? OR c.member_b=?)""",
                (user_id, couple_id, user_id, user_id),
            ).fetchone()
            active_scope = bool(
                scope and scope["status"] == "active" and scope["deleted_at"] is None
                and int(scope["version"]) == couple_version
            )
            pending_row = db.execute(
                """SELECT result_json FROM mutation_idempotency
                WHERE couple_id=? AND user_id=? AND operation='expression.suggest'
                  AND idempotency_key=?""",
                (couple_id, user_id, idempotency_key),
            ).fetchone()
            pending = parse_json(pending_row["result_json"], {}) if pending_row else {}
            owns_receipt = pending.get("_owner_token") == owner_token and pending.get("_state") == "pending"
            if not active_scope or not owns_receipt:
                status = "failed"
                failure_code = "scope_changed"
                final_error = "scope_changed"
                receipt = {
                    "_state": "failed", "_request_hash": request_hash,
                    "failure_code": failure_code, "_request_id": request_id,
                }
            elif provider_error is not None:
                is_timeout = isinstance(provider_error, TimeoutError)
                status = "timeout" if is_timeout else "failed"
                failure_code = "timeout" if is_timeout else "provider_error"
                final_error = failure_code
                receipt = {
                    "_state": "failed", "_request_hash": request_hash,
                    "failure_code": failure_code, "_request_id": request_id,
                }
            else:
                status = "succeeded"
                failure_code = None
                receipt = {
                    "_state": "succeeded", "_request_hash": request_hash,
                    "_request_id": request_id, "result": result,
                }
            usage = suggestion.usage if suggestion is not None and isinstance(suggestion.usage, dict) else {}
            if is_external:
                if owns_receipt and active_scope:
                    db.execute(
                        """UPDATE ai_usage_events SET status=?,finished_at=?,latency_ms=?,
                        input_tokens=?,output_tokens=?,total_tokens=?,failure_code=?
                        WHERE request_id=? AND provider=? AND model=? AND status='started'
                          AND request_sent=1""",
                        (
                            status, utc_now(), latency_ms, usage.get("input_tokens"),
                            usage.get("output_tokens"), usage.get("total_tokens"), failure_code,
                            request_id, provider_name, provider_model,
                        ),
                    )
                else:
                    # Lifecycle cleanup already fixed the terminal meaning.
                    # Only supplement non-content telemetry from the real call.
                    db.execute(
                        """UPDATE ai_usage_events SET request_sent=1,
                        latency_ms=COALESCE(?,latency_ms),
                        input_tokens=COALESCE(?,input_tokens),
                        output_tokens=COALESCE(?,output_tokens),
                        total_tokens=COALESCE(?,total_tokens)
                        WHERE request_id=? AND provider=? AND model=?""",
                        (
                            latency_ms, usage.get("input_tokens"), usage.get("output_tokens"),
                            usage.get("total_tokens"), request_id, provider_name, provider_model,
                        ),
                    )
            if owns_receipt:
                db.execute(
                    """UPDATE mutation_idempotency SET result_json=?
                    WHERE couple_id=? AND user_id=? AND operation='expression.suggest'
                      AND idempotency_key=?""",
                    (json.dumps(receipt, ensure_ascii=False), couple_id, user_id, idempotency_key),
                )
        if final_error == "scope_changed":
            raise ConflictError("情侣空间已经变化，请重新提交")
        if provider_error is not None:
            raise RuntimeError("表达辅助暂时不可用，原草稿已保留，请稍后重试") from provider_error
        if result is None:  # pragma: no cover - defensive terminal guard
            raise RuntimeError("表达辅助暂时不可用，原草稿已保留，请稍后重试")
        return result

    @staticmethod
    def _detect_media(data: bytes, declared_type: object = "") -> tuple[str, str, str]:
        head = data[:32]
        if head.startswith(b"\xff\xd8\xff"):
            return "image", "image/jpeg", ".jpg"
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image", "image/png", ".png"
        if head.startswith((b"GIF87a", b"GIF89a")):
            return "image", "image/gif", ".gif"
        if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
            return "image", "image/webp", ".webp"
        if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
            return "audio", "audio/wav", ".wav"
        if head.startswith(b"OggS"):
            return "audio", "audio/ogg", ".ogg"
        if head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
            return "audio", "audio/mpeg", ".mp3"
        if len(head) >= 12 and head[4:8] == b"ftyp":
            brand = head[8:12]
            declared = str(declared_type or "").split(";", 1)[0].strip().lower()
            if brand in {b"M4A ", b"M4B ", b"M4P "} or declared == "audio/mp4":
                return "audio", "audio/mp4", ".m4a"
            if brand == b"qt  ":
                return "video", "video/quicktime", ".mov"
            return "video", "video/mp4", ".mp4"
        if head.startswith(b"\x1aE\xdf\xa3"):
            declared = str(declared_type or "").split(";", 1)[0].strip().lower()
            if declared == "audio/webm":
                return "audio", "audio/webm", ".webm"
            return "video", "video/webm", ".webm"
        raise ValueError("仅支持 JPG、PNG、GIF、WebP、MP3、WAV、Ogg、M4A、MP4、MOV 或 WebM")

    @staticmethod
    def _media_public(row: sqlite3.Row | dict) -> dict:
        item = dict(row)
        item.pop("relative_path", None)
        item.pop("thumbnail_path", None)
        item.pop("storage_key", None)
        item.pop("checksum", None)
        item.pop("thumbnail_checksum", None)
        item.pop("purged_at", None)
        item["url"] = f"/api/v1/media/{item['id']}"
        item["thumbnail_url"] = f"/api/v1/media/{item['id']}?thumbnail=1"
        item["metadata_stripped"] = bool(item.get("metadata_stripped"))
        return item

    def _assert_media_visible(self, row: sqlite3.Row | dict, user_id: str) -> None:
        if row["owner_id"] == user_id:
            return
        visibility = row["visibility"]
        if visibility == "shared":
            return
        raise PermissionError("无权查看这项私密媒体")

    def _media_row_for_user(self, db: sqlite3.Connection, media_id: str, user_id: str, *, status: str = "ready") -> sqlite3.Row:
        self._reconcile_delayed_moments(db)
        couple, _ = self._couple_context(db, user_id)
        row = db.execute(
            "SELECT * FROM media_assets WHERE id=? AND couple_id=? AND status=? AND purged_at IS NULL",
            (media_id, couple["id"], status),
        ).fetchone()
        if not row:
            raise KeyError("媒体不存在")
        self._assert_media_visible(row, user_id)
        return row

    @staticmethod
    def _upload_session_public(row: sqlite3.Row | dict) -> dict:
        item = dict(row)
        for private_key in ("temp_path", "thumbnail_temp_path"):
            item.pop(private_key, None)
        expected = max(1, int(item.get("expected_size") or 1))
        item["progress"] = min(1, int(item.get("received_size") or 0) / expected)
        item["resumable"] = item.get("status") == "pending"
        return item

    def _upload_temp_file(self, relative_path: str) -> Path:
        path = (self.media_root / relative_path).resolve()
        root = self.media_root.resolve()
        if root not in path.parents:
            raise ValueError("上传临时路径无效")
        return path

    def _cleanup_expired_uploads(self) -> None:
        now = utc_now()
        with self.connection() as db:
            rows = db.execute(
                "SELECT id,temp_path,thumbnail_temp_path FROM upload_sessions WHERE status='pending' AND expires_at<=?",
                (now,),
            ).fetchall()
            for row in rows:
                for value in (row["temp_path"], row["thumbnail_temp_path"]):
                    if value:
                        try:
                            self._upload_temp_file(value).unlink(missing_ok=True)
                        except ValueError:
                            pass
                db.execute("UPDATE upload_sessions SET status='expired',updated_at=? WHERE id=?", (now, row["id"]))

    def _upload_row_for_user(self, db: sqlite3.Connection, upload_id: str, user_id: str) -> sqlite3.Row:
        couple, _ = self._couple_context(db, user_id)
        row = db.execute(
            "SELECT * FROM upload_sessions WHERE id=? AND couple_id=? AND owner_id=?",
            (upload_id, couple["id"], user_id),
        ).fetchone()
        if not row:
            raise KeyError("上传任务不存在")
        return row

    def create_media_upload(self, payload: dict, user_id: str = DEMO_USER) -> dict:
        self._cleanup_expired_uploads()
        original_name = str(payload.get("name") or "video").strip()
        mime_type = str(payload.get("mime_type") or "application/octet-stream").split(";", 1)[0].strip().lower()
        visibility = str(payload.get("visibility") or "shared").strip()
        try:
            expected_size = int(payload.get("size_bytes"))
        except (TypeError, ValueError) as exc:
            raise ValueError("文件大小必须是整数") from exc
        if expected_size <= 0:
            raise ValueError("文件大小必须大于 0")
        accepted = {
            "image/jpeg": ("image", 20 * 1024 * 1024), "image/png": ("image", 20 * 1024 * 1024),
            "image/gif": ("image", 20 * 1024 * 1024), "image/webp": ("image", 20 * 1024 * 1024),
            "audio/mpeg": ("audio", 30 * 1024 * 1024), "audio/wav": ("audio", 30 * 1024 * 1024),
            "audio/ogg": ("audio", 30 * 1024 * 1024), "audio/mp4": ("audio", 30 * 1024 * 1024),
            "audio/webm": ("audio", 30 * 1024 * 1024),
            "video/mp4": ("video", 200 * 1024 * 1024), "video/quicktime": ("video", 200 * 1024 * 1024),
            "video/webm": ("video", 200 * 1024 * 1024), "application/octet-stream": ("unknown", 200 * 1024 * 1024),
        }
        if mime_type not in accepted:
            raise ValueError("上传文件类型不受支持")
        kind_hint, maximum = accepted[mime_type]
        if expected_size > maximum:
            raise ValueError("上传文件过大")
        if visibility not in {"shared", "private"}:
            raise ValueError("媒体可见范围无效")
        expected_checksum = str(payload.get("sha256") or "").strip().lower() or None
        if expected_checksum and not re.fullmatch(r"[0-9a-f]{64}", expected_checksum):
            raise ValueError("SHA-256 校验值无效")
        try:
            chunk_size = max(256 * 1024, min(int(payload.get("chunk_size") or 4 * 1024 * 1024), 8 * 1024 * 1024))
            user_quota = max(1, int(os.getenv("MEDIA_USER_QUOTA_MB", "5120"))) * 1024 * 1024
            couple_quota = max(2, int(os.getenv("MEDIA_COUPLE_QUOTA_MB", "10240"))) * 1024 * 1024
            minimum_free = max(128, int(os.getenv("MEDIA_MIN_FREE_MB", "2048"))) * 1024 * 1024
            video_minimum_free = max(256, int(os.getenv("MEDIA_VIDEO_MIN_FREE_MB", "5120"))) * 1024 * 1024
        except (TypeError, ValueError) as exc:
            raise ValueError("媒体容量配置无效") from exc
        now = utc_now()
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).replace(microsecond=0).isoformat()
        upload_id = new_id("upload")
        relative_path = f"temp/uploads/{upload_id}.part"
        temp_path = self._upload_temp_file(relative_path)
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            temp_path.parent.chmod(0o700)
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            active_count = int(db.execute("SELECT COUNT(*) FROM upload_sessions WHERE owner_id=? AND status='pending'", (user_id,)).fetchone()[0])
            if active_count >= 5:
                raise ValueError("同时进行的上传任务过多，请先完成或取消旧任务")
            user_used = int(db.execute("SELECT COALESCE(SUM(size_bytes),0) FROM media_assets WHERE owner_id=? AND purged_at IS NULL", (user_id,)).fetchone()[0])
            user_pending = int(db.execute("SELECT COALESCE(SUM(expected_size),0) FROM upload_sessions WHERE owner_id=? AND status='pending'", (user_id,)).fetchone()[0])
            couple_used = int(db.execute("SELECT COALESCE(SUM(size_bytes),0) FROM media_assets WHERE couple_id=? AND purged_at IS NULL", (couple["id"],)).fetchone()[0])
            couple_pending = int(db.execute("SELECT COALESCE(SUM(expected_size),0) FROM upload_sessions WHERE couple_id=? AND status='pending'", (couple["id"],)).fetchone()[0])
            if user_used + user_pending + expected_size > user_quota:
                raise ValueError("个人媒体空间不足，无法创建该上传任务")
            if couple_used + couple_pending + expected_size > couple_quota:
                raise ValueError("情侣媒体空间不足，无法创建该上传任务")
            if self.storage.keeps_local_copy:
                reserve = video_minimum_free if kind_hint == "video" else minimum_free
                if shutil.disk_usage(self.media_root).free - expected_size < reserve:
                    raise ValueError("服务器磁盘空间不足，无法创建该上传任务")
            db.execute(
                """INSERT INTO upload_sessions(
                    id,couple_id,owner_id,original_name,mime_type,visibility,expected_size,received_size,
                    expected_checksum,chunk_size,temp_path,status,expires_at,updated_at,created_at
                ) VALUES (?,?,?,?,?,?,?,0,?,?,?,'pending',?,?,?)""",
                (upload_id, couple["id"], user_id, original_name[:180], mime_type, visibility, expected_size,
                 expected_checksum, chunk_size, relative_path, expires_at, now, now),
            )
            row = db.execute("SELECT * FROM upload_sessions WHERE id=?", (upload_id,)).fetchone()
        temp_path.touch(exist_ok=False)
        if os.name != "nt":
            temp_path.chmod(0o600)
        return self._upload_session_public(row)

    def get_media_upload(self, upload_id: str, user_id: str = DEMO_USER) -> dict:
        self._cleanup_expired_uploads()
        with self.connection() as db:
            return self._upload_session_public(self._upload_row_for_user(db, upload_id, user_id))

    def append_media_upload(self, upload_id: str, data: bytes, offset: object, user_id: str = DEMO_USER) -> dict:
        if not data:
            raise ValueError("上传分片不能为空")
        if len(data) > 8 * 1024 * 1024:
            raise ValueError("单个上传分片不能超过 8 MB")
        try:
            requested_offset = int(offset)
        except (TypeError, ValueError) as exc:
            raise ValueError("上传偏移量必须是整数") from exc
        now = utc_now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._upload_row_for_user(db, upload_id, user_id)
            if row["status"] != "pending":
                raise ValueError("上传任务已经结束")
            if row["expires_at"] <= now:
                raise ValueError("上传任务已过期，请重新选择文件")
            received = int(row["received_size"])
            if requested_offset != received:
                error = ConflictError("上传偏移量已变化，请从服务器记录的位置继续")
                error.details = {"expected_offset": received}
                raise error
            if received + len(data) > int(row["expected_size"]):
                raise ValueError("上传分片超过文件声明大小")
            path = self._upload_temp_file(row["temp_path"])
            actual_size = path.stat().st_size if path.is_file() else 0
            if actual_size != received:
                if actual_size > received:
                    with path.open("r+b") as stream:
                        stream.truncate(received)
                else:
                    raise RuntimeError("上传临时文件不完整，请取消后重新上传")
            with path.open("ab") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            next_offset = received + len(data)
            db.execute("UPDATE upload_sessions SET received_size=?,updated_at=? WHERE id=?", (next_offset, now, upload_id))
            updated = db.execute("SELECT * FROM upload_sessions WHERE id=?", (upload_id,)).fetchone()
        return self._upload_session_public(updated)

    def set_media_upload_thumbnail(self, upload_id: str, data: bytes, user_id: str = DEMO_USER) -> dict:
        if not data or len(data) > 3 * 1024 * 1024:
            raise ValueError("视频封面必须是 3 MB 以内的图片")
        _, detected_type, _ = self._detect_media(data)
        if not detected_type.startswith("image/"):
            raise ValueError("视频封面必须是图片")
        sanitized = sanitize_image(data, detected_type)
        relative_path = f"temp/uploads/{upload_id}.thumbnail.jpg"
        target = self._upload_temp_file(relative_path)
        now = utc_now()
        with self.connection() as db:
            row = self._upload_row_for_user(db, upload_id, user_id)
            if row["status"] != "pending" or row["expires_at"] <= now:
                raise ValueError("上传任务已经结束或过期")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(sanitized.content)
            if os.name != "nt":
                target.chmod(0o600)
            db.execute("UPDATE upload_sessions SET thumbnail_temp_path=?,updated_at=? WHERE id=?", (relative_path, now, upload_id))
        return {"upload_id": upload_id, "thumbnail_ready": True}

    def complete_media_upload(self, upload_id: str, user_id: str = DEMO_USER) -> dict:
        with self.connection() as db:
            row = self._upload_row_for_user(db, upload_id, user_id)
            if row["status"] == "completed" and row["media_id"]:
                media = self._media_row_for_user(db, row["media_id"], user_id)
                result = self._media_public(media)
                result["upload_id"] = upload_id
                return result
            if row["status"] != "pending":
                raise ValueError("上传任务已经结束")
            if int(row["received_size"]) != int(row["expected_size"]):
                raise ValueError("文件尚未上传完整")
            path = self._upload_temp_file(row["temp_path"])
            if not path.is_file() or path.stat().st_size != int(row["expected_size"]):
                raise RuntimeError("上传临时文件不完整")
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if row["expected_checksum"] and digest != row["expected_checksum"]:
                raise ValueError("文件完整性校验失败，请重新上传")
            thumbnail_path = self._upload_temp_file(row["thumbnail_temp_path"]) if row["thumbnail_temp_path"] else None
            thumbnail_data = thumbnail_path.read_bytes() if thumbnail_path and thumbnail_path.is_file() else None
            original_name, mime_type, visibility = row["original_name"], row["mime_type"], row["visibility"]
        result = self.save_media(data, original_name, mime_type, visibility, user_id=user_id, thumbnail_data=thumbnail_data)
        now = utc_now()
        with self.connection() as db:
            current = self._upload_row_for_user(db, upload_id, user_id)
            if current["status"] == "pending":
                db.execute("UPDATE upload_sessions SET status='completed',media_id=?,updated_at=? WHERE id=?", (result["id"], now, upload_id))
            elif current["media_id"] != result["id"]:
                raise ConflictError("上传任务已由另一请求完成")
        path.unlink(missing_ok=True)
        if thumbnail_path:
            thumbnail_path.unlink(missing_ok=True)
        result["upload_id"] = upload_id
        return result

    def cancel_media_upload(self, upload_id: str, user_id: str = DEMO_USER) -> dict:
        now = utc_now()
        with self.connection() as db:
            row = self._upload_row_for_user(db, upload_id, user_id)
            if row["status"] == "completed":
                raise ValueError("已完成的上传不能取消；如需移除请使用媒体回收站")
            for value in (row["temp_path"], row["thumbnail_temp_path"]):
                if value:
                    self._upload_temp_file(value).unlink(missing_ok=True)
            db.execute("UPDATE upload_sessions SET status='cancelled',updated_at=? WHERE id=?", (now, upload_id))
        return {"id": upload_id, "cancelled": True}

    def save_media(
        self,
        data: bytes,
        original_name: object,
        declared_type: object,
        visibility: object = "shared",
        user_id: str = DEMO_USER,
        thumbnail_data: bytes | None = None,
    ) -> dict:
        if not data:
            raise ValueError("媒体文件不能为空")
        kind, detected_type, extension = self._detect_media(data, declared_type)
        limits = {"image": 20 * 1024 * 1024, "audio": 30 * 1024 * 1024, "video": 200 * 1024 * 1024}
        if len(data) > limits[kind]:
            label = {"image": "图片", "audio": "音频", "video": "视频"}[kind]
            raise ValueError(f"{label}文件过大")
        declared = str(declared_type or "").split(";", 1)[0].strip().lower()
        if declared and declared not in {detected_type, "application/octet-stream"}:
            compatible = declared.startswith(f"{kind}/")
            if not compatible:
                raise ValueError("声明的文件类型与实际内容不一致")
        visibility_text = str(visibility or "shared")
        if visibility_text not in {"shared", "private"}:
            raise ValueError("媒体可见范围无效")
        # Uploading never grants partner access. The authoritative association
        # promotes this owner-private object in its own database transaction.
        visibility_text = "private"
        safe_name = re.sub(r"[\x00-\x1f\x7f]", "_", Path(str(original_name or f"media{extension}")).name).strip()[:180] or f"media{extension}"
        media_id = new_id("media")
        width = height = None
        captured_at = None
        metadata_stripped = False
        thumbnail: bytes | None = None
        thumbnail_key: str | None = None
        thumbnail_checksum: str | None = None
        with self.connection() as db:
            self._couple_context(db, user_id)
            timezone_row = db.execute("SELECT timezone FROM users WHERE id=? AND deleted_at IS NULL", (user_id,)).fetchone()
            if not timezone_row:
                raise KeyError("用户不存在")
            uploader_timezone = str(timezone_row["timezone"] or "Asia/Shanghai")
        if kind == "image":
            sanitized = sanitize_image(data, detected_type, uploader_timezone)
            data = sanitized.content
            detected_type = sanitized.mime_type
            extension = sanitized.extension
            width, height = sanitized.width, sanitized.height
            metadata_stripped = sanitized.metadata_stripped
            thumbnail = sanitized.thumbnail
            captured_at = sanitized.captured_at
        elif kind == "video" and thumbnail_data:
            if len(thumbnail_data) > 3 * 1024 * 1024:
                raise ValueError("视频封面文件过大")
            thumbnail_kind, thumbnail_type, _ = self._detect_media(thumbnail_data)
            if thumbnail_kind != "image":
                raise ValueError("视频封面必须是图片")
            sanitized_thumbnail = sanitize_image(thumbnail_data, thumbnail_type)
            thumbnail = sanitized_thumbnail.thumbnail
            width, height = sanitized_thumbnail.width, sanitized_thumbnail.height
        checksum = hashlib.sha256(data).hexdigest()
        try:
            user_quota = max(1, int(os.getenv("MEDIA_USER_QUOTA_MB", "5120"))) * 1024 * 1024
            couple_quota = max(2, int(os.getenv("MEDIA_COUPLE_QUOTA_MB", "10240"))) * 1024 * 1024
            minimum_free = max(128, int(os.getenv("MEDIA_MIN_FREE_MB", "2048"))) * 1024 * 1024
            video_minimum_free = max(256, int(os.getenv("MEDIA_VIDEO_MIN_FREE_MB", "5120"))) * 1024 * 1024
        except ValueError as exc:
            raise ValueError("媒体容量配置无效") from exc
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            duplicate = db.execute(
                "SELECT * FROM media_assets WHERE couple_id=? AND owner_id=? AND checksum=? AND visibility=? AND reveal_at IS NULL AND status='ready' AND purged_at IS NULL AND NOT EXISTS (SELECT 1 FROM moments WHERE media_id=media_assets.id) AND NOT EXISTS (SELECT 1 FROM recipe_media WHERE url='/api/v1/media/' || media_assets.id) ORDER BY created_at DESC LIMIT 1",
                (couple["id"], user_id, checksum, visibility_text),
            ).fetchone()
            if duplicate:
                result = self._media_public(duplicate)
                result["duplicate"] = True
                return result
            user_bytes = int(db.execute(
                "SELECT COALESCE(SUM(size_bytes),0) FROM media_assets WHERE owner_id=? AND purged_at IS NULL",
                (user_id,),
            ).fetchone()[0])
            couple_bytes = int(db.execute(
                "SELECT COALESCE(SUM(size_bytes),0) FROM media_assets WHERE couple_id=? AND purged_at IS NULL",
                (couple["id"],),
            ).fetchone()[0])
        if user_bytes + len(data) > user_quota:
            raise ValueError("个人媒体空间已达到上限，请先清理回收站或旧文件")
        if couple_bytes + len(data) > couple_quota:
            raise ValueError("情侣媒体空间已达到上限，请先清理不需要的文件")
        if self.storage.keeps_local_copy:
            disk_free = shutil.disk_usage(self.media_root).free
            required_free = video_minimum_free if kind == "video" else minimum_free
            if disk_free - len(data) < required_free:
                if kind == "video":
                    raise ValueError("服务器磁盘空间偏低，已暂停视频上传；照片和文字仍可使用")
                raise ValueError("服务器磁盘空间不足，已暂停媒体上传")
        folder = {"image": "images", "audio": "audio", "video": "videos"}[kind]
        relative_path = f"{folder}/{media_id}{extension}"
        stored = self.storage.put(relative_path, data, detected_type)
        stored_thumbnail = None
        if thumbnail is not None:
            thumbnail_key = f"thumbnails/{media_id}.jpg"
            try:
                stored_thumbnail = self.storage.put(thumbnail_key, thumbnail, "image/jpeg")
                thumbnail_checksum = stored_thumbnail.checksum
            except Exception:
                self.storage.delete(stored.key)
                raise
        now = utc_now()
        try:
            with self.connection() as db:
                couple, _ = self._couple_context(db, user_id)
                db.execute(
                    """INSERT INTO media_assets(
                        id,couple_id,owner_id,kind,original_name,mime_type,relative_path,
                        thumbnail_path,thumbnail_checksum,size_bytes,width,height,metadata_stripped,
                        visibility,status,storage_provider,storage_key,checksum,created_at,
                        captured_at,date_source,source_type
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'ready',?,?,?,?,?,?,?)""",
                    (
                        media_id, couple["id"], user_id, kind, safe_name, detected_type,
                        relative_path, thumbnail_key, thumbnail_checksum, len(data), width, height,
                        int(metadata_stripped), visibility_text, stored.provider, stored.key,
                        stored.checksum, now, captured_at,
                        "media_capture" if captured_at else "legacy_unknown", "human",
                    ),
                )
                self._upsert_memory_provenance_in_connection(
                    db, "media", media_id, couple["id"], user_id,
                    source_type="human",
                    date_source="media_capture" if captured_at else "legacy_unknown",
                    resource_version=1,
                )
                self._audit(db, "media.created", "media", media_id, {"kind": kind, "size": len(data), "metadata_stripped": metadata_stripped}, actor_id=user_id)
                self._event(db, couple["id"], user_id, "media.created", media_id, {"kind": kind, "owner_only": True})
                row = db.execute("SELECT * FROM media_assets WHERE id=?", (media_id,)).fetchone()
        except Exception:
            self.storage.delete(stored.key)
            if stored_thumbnail:
                self.storage.delete(stored_thumbnail.key)
            raise
        return self._media_public(row)

    def get_media(self, media_id: str, user_id: str = DEMO_USER, thumbnail: bool = False) -> tuple[Path, str, int]:
        with self.connection() as db:
            row = self._media_row_for_user(db, media_id, user_id)
        if row["storage_provider"] not in {"local", "qiniu"}:
            raise ValueError("远程媒体请使用 get_media_content 读取")
        relative = row["thumbnail_path"] if thumbnail and row["thumbnail_path"] else (row["storage_key"] or row["relative_path"])
        path = (self.media_root / relative).resolve()
        if self.media_root.resolve() not in path.parents or not path.is_file():
            raise KeyError("媒体文件不存在")
        mime = row["mime_type"] if not thumbnail or not row["thumbnail_path"] else mimetypes.guess_type(path.name)[0] or row["mime_type"]
        return path, mime, path.stat().st_size

    def get_media_content(self, media_id: str, user_id: str = DEMO_USER, thumbnail: bool = False, access_ticket: str | None = None) -> tuple[bytes, str, str]:
        with self.connection() as db:
            row = self._media_row_for_user(db, media_id, user_id)
        if access_ticket is not None:
            self.validate_media_access(access_ticket, media_id, user_id, thumbnail)
        key = row["thumbnail_path"] if thumbnail and row["thumbnail_path"] else (row["storage_key"] or row["relative_path"])
        provider = row["storage_provider"]
        if provider == "local":
            storage = self.local_storage
        elif provider == "qiniu" and self.storage.name != "qiniu":
            # Qiniu is a dual-copy provider. Existing objects stay readable
            # from the private local mirror even when cloud credentials are
            # temporarily unavailable.
            storage = self.local_storage
        else:
            storage = self.storage
        if storage.name not in {provider, "local" if provider == "qiniu" else provider}:
            raise RuntimeError(f"媒体需要 {provider} 存储配置")
        local_missing = provider == "qiniu" and hasattr(storage, "local_exists") and not storage.local_exists(key)
        content = storage.get(key)
        checksum = row["thumbnail_checksum"] if thumbnail and row["thumbnail_path"] else row["checksum"]
        if checksum and hashlib.sha256(content).hexdigest() != checksum:
            raise RuntimeError("媒体文件校验失败")
        mime = row["mime_type"] if not thumbnail or not row["thumbnail_path"] else mimetypes.guess_type(key)[0] or row["mime_type"]
        if local_missing and hasattr(storage, "repair_local"):
            storage.repair_local(key, content, mime)
        return content, mime, row["original_name"]

    def list_media(self, user_id: str = DEMO_USER) -> list[dict]:
        with self.connection() as db:
            self._reconcile_delayed_moments(db)
            couple, _ = self._couple_context(db, user_id)
            rows = db.execute("SELECT * FROM media_assets WHERE couple_id=? AND status='ready' AND purged_at IS NULL ORDER BY created_at DESC", (couple["id"],)).fetchall()
        result = []
        for row in rows:
            try:
                self._assert_media_visible(row, user_id)
            except PermissionError:
                continue
            result.append(self._media_public(row))
        return result

    def issue_media_access(self, media_id: str, user_id: str, thumbnail: bool = False, ttl_seconds: object = 300) -> dict:
        try:
            ttl = int(ttl_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("媒体访问有效期必须是整数") from exc
        ttl = max(60, min(ttl, 600))
        with self.connection() as db:
            self._media_row_for_user(db, media_id, user_id)
        expires = int(datetime.now(timezone.utc).timestamp()) + ttl
        payload = json.dumps({"m": media_id, "u": user_id, "t": int(thumbnail), "e": expires}, separators=(",", ":"), sort_keys=True).encode()
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        signature = hmac.new(self._media_signing_key, encoded.encode(), hashlib.sha256).hexdigest()
        ticket = f"{encoded}.{signature}"
        suffix = "?thumbnail=1&" if thumbnail else "?"
        return {"url": f"/api/v1/media/{media_id}{suffix}ticket={ticket}", "expires_at": datetime.fromtimestamp(expires, timezone.utc).replace(microsecond=0).isoformat()}

    def validate_media_access(self, ticket: str, media_id: str, user_id: str, thumbnail: bool) -> None:
        try:
            encoded, signature = ticket.split(".", 1)
            expected = hmac.new(self._media_signing_key, encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            padding = "=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode((encoded + padding).encode()))
            now = int(datetime.now(timezone.utc).timestamp())
            if payload != {"m": media_id, "u": user_id, "t": int(thumbnail), "e": payload.get("e")}:
                raise ValueError
            if not isinstance(payload["e"], int) or payload["e"] < now or payload["e"] > now + 600:
                raise ValueError
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise PermissionError("媒体访问票据无效或已过期") from exc

    def _media_reference_labels(self, db: sqlite3.Connection, media_id: str) -> list[str]:
        labels: list[str] = []
        if db.execute("SELECT 1 FROM moments WHERE media_id=? AND purged_at IS NULL LIMIT 1", (media_id,)).fetchone():
            labels.append("恋爱瞬间")
        if db.execute("SELECT 1 FROM voice_transcripts WHERE media_id=? LIMIT 1", (media_id,)).fetchone():
            labels.append("语音转写")
        if db.execute("SELECT 1 FROM chat_imports WHERE media_id=? AND status IN ('draft','confirmed') LIMIT 1", (media_id,)).fetchone():
            labels.append("聊天导入")
        if db.execute("SELECT 1 FROM recipe_media m JOIN recipes r ON r.id=m.recipe_id WHERE m.url=? AND r.deleted_at IS NULL LIMIT 1", (f"/api/v1/media/{media_id}",)).fetchone():
            labels.append("菜谱图片")
        for row in db.execute("SELECT metadata FROM messages WHERE metadata LIKE ?", (f"%{media_id}%",)).fetchall():
            if parse_json(row["metadata"], {}).get("mediaId") == media_id:
                labels.append("消息")
                break
        return labels

    @staticmethod
    def _sharing_context_valid(db: sqlite3.Connection, couple_id: str, owner_id: str) -> bool:
        return bool(db.execute(
            "SELECT 1 FROM couples c JOIN users a ON a.id=c.member_a JOIN users b ON b.id=c.member_b WHERE c.id=? AND c.status='active' AND ? IN (c.member_a,c.member_b) AND a.deleted_at IS NULL AND b.deleted_at IS NULL",
            (couple_id, owner_id),
        ).fetchone())

    def _reconcile_delayed_moments(self, db: sqlite3.Connection) -> None:
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        for moment in db.execute("SELECT id,couple_id,creator_id,media_id FROM moments WHERE visibility='delayed' AND reveal_at<=? AND deleted_at IS NULL AND purged_at IS NULL", (utc_now(),)).fetchall():
            visible = self._sharing_context_valid(db, moment["couple_id"], moment["creator_id"])
            db.execute("UPDATE moments SET visibility=?,reveal_at=NULL,version=version+1 WHERE id=?", ("shared" if visible else "private", moment["id"]))
            if moment["media_id"]:
                self._sync_media_visibility(db, moment["media_id"])

    def _sync_media_visibility(self, db: sqlite3.Connection, media_id: str) -> None:
        """Reconcile one object against every active message and moment using it."""
        media = db.execute("SELECT * FROM media_assets WHERE id=? AND status='ready'", (media_id,)).fetchone()
        if not media:
            return
        if db.execute("SELECT 1 FROM recipe_media m JOIN recipes r ON r.id=m.recipe_id WHERE m.url=? AND r.deleted_at IS NULL AND r.visibility='couple' LIMIT 1", (f"/api/v1/media/{media_id}",)).fetchone():
            db.execute("UPDATE media_assets SET visibility='shared',reveal_at=NULL,version=version+1 WHERE id=? AND visibility!='shared'", (media_id,))
            return
        visibility: str
        reveal_at: str | None
        for message in db.execute("SELECT metadata FROM messages WHERE metadata LIKE ?", (f"%{media_id}%",)).fetchall():
            if parse_json(message["metadata"], {}).get("mediaId") == media_id:
                visibility, reveal_at = "shared", None
                break
        else:
            moments = db.execute("SELECT * FROM moments WHERE media_id=? AND deleted_at IS NULL AND purged_at IS NULL", (media_id,)).fetchall()
            now = utc_now()
            if any(item["visibility"] == "shared" or (item["visibility"] == "delayed" and item["reveal_at"] and item["reveal_at"] <= now and self._sharing_context_valid(db, item["couple_id"], item["creator_id"])) for item in moments):
                visibility, reveal_at = "shared", None
            else:
                delayed = sorted(item["reveal_at"] for item in moments if item["visibility"] == "delayed" and item["reveal_at"] and item["reveal_at"] > now)
                visibility, reveal_at = ("delayed", delayed[0]) if delayed else ("private", None)
        if media["visibility"] != visibility or media["reveal_at"] != reveal_at:
            db.execute("UPDATE media_assets SET visibility=?,reveal_at=?,version=version+1 WHERE id=?", (visibility, reveal_at, media_id))

    def trash_media(self, media_id: str, user_id: str) -> dict:
        with self.connection() as db:
            row = self._media_row_for_user(db, media_id, user_id)
            if row["owner_id"] != user_id:
                raise PermissionError("只能删除自己上传的媒体")
            references = self._media_reference_labels(db, media_id)
            if references:
                raise ValueError(f"图片仍被{'、'.join(references)}使用，请先移除对应内容")
            now = utc_now()
            db.execute("UPDATE media_assets SET status='trashed',deleted_at=?,version=version+1 WHERE id=?", (now, media_id))
            self._audit(db, "media.trashed", "media", media_id, actor_id=user_id)
        return {"id": media_id, "deleted": True}

    def list_media_trash(self, user_id: str) -> list[dict]:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            rows = db.execute("SELECT * FROM media_assets WHERE couple_id=? AND owner_id=? AND status='trashed' AND purged_at IS NULL ORDER BY deleted_at DESC", (couple["id"], user_id)).fetchall()
        return [self._media_public(row) for row in rows]

    def restore_media(self, media_id: str, expected_version: object, user_id: str) -> dict:
        try:
            version = int(expected_version)
        except (TypeError, ValueError) as exc:
            raise ValueError("媒体版本无效") from exc
        with self.connection() as db:
            row = self._media_row_for_user(db, media_id, user_id, status="trashed")
            if row["owner_id"] != user_id:
                raise PermissionError("只能恢复自己上传的媒体")
            if row["version"] != version:
                raise ConflictError("媒体已被另一项操作修改")
            db.execute("UPDATE media_assets SET status='ready',deleted_at=NULL,version=version+1 WHERE id=?", (media_id,))
            self._audit(db, "media.restored", "media", media_id, actor_id=user_id)
            updated = db.execute("SELECT * FROM media_assets WHERE id=?", (media_id,)).fetchone()
        return self._media_public(updated)

    def purge_media(self, media_id: str, user_id: str) -> dict:
        with self.connection() as db:
            row = self._media_row_for_user(db, media_id, user_id, status="trashed")
            if row["owner_id"] != user_id:
                raise PermissionError("只能永久删除自己上传的媒体")
            keys = [row["storage_key"] or row["relative_path"]]
            if row["thumbnail_path"]:
                keys.append(row["thumbnail_path"])
            provider = row["storage_provider"]
        storage = self.local_storage if provider == "local" else self.storage
        if storage.name != provider:
            raise RuntimeError(f"媒体需要 {provider} 存储配置")
        for key in keys:
            storage.delete(key)
        with self.connection() as db:
            now = utc_now()
            db.execute("UPDATE media_assets SET status='purged',original_name='已永久删除',purged_at=?,version=version+1 WHERE id=?", (now, media_id))
            self._audit(db, "media.purged", "media", media_id, actor_id=user_id)
        return {"id": media_id, "purged": True}

    def media_usage(self, user_id: str = DEMO_USER) -> dict:
        with self.connection() as db:
            self._reconcile_delayed_moments(db)
            couple, _ = self._couple_context(db, user_id)
            rows = db.execute(
                "SELECT kind, COUNT(*) AS count, COALESCE(SUM(size_bytes), 0) AS bytes FROM media_assets WHERE couple_id=? AND status='ready' AND purged_at IS NULL AND (owner_id=? OR visibility='shared') GROUP BY kind",
                (couple["id"], user_id),
            ).fetchall()
            owner_bytes = int(db.execute("SELECT COALESCE(SUM(size_bytes),0) FROM media_assets WHERE owner_id=? AND purged_at IS NULL", (user_id,)).fetchone()[0])
            couple_bytes = int(db.execute("SELECT COALESCE(SUM(size_bytes),0) FROM media_assets WHERE couple_id=? AND purged_at IS NULL", (couple["id"],)).fetchone()[0])
        by_kind = {row["kind"]: {"count": row["count"], "bytes": row["bytes"]} for row in rows}
        try:
            user_quota = max(1, int(os.getenv("MEDIA_USER_QUOTA_MB", "5120"))) * 1024 * 1024
            couple_quota = max(2, int(os.getenv("MEDIA_COUPLE_QUOTA_MB", "10240"))) * 1024 * 1024
        except ValueError:
            user_quota, couple_quota = 5 * 1024**3, 10 * 1024**3
        return {
            "total_bytes": sum(item["bytes"] for item in by_kind.values()),
            "by_kind": by_kind,
            "owner_bytes": owner_bytes,
            "owner_quota_bytes": user_quota,
            "owner_warning": owner_bytes >= user_quota * 0.8,
            "couple_quota_bytes": couple_quota,
            "couple_warning": couple_bytes >= couple_quota * 0.8,
        }

    @staticmethod
    def _public_call(db: sqlite3.Connection, row: sqlite3.Row, user_id: str) -> dict:
        value = dict(row)
        peer_id = row["callee_id"] if row["caller_id"] == user_id else row["caller_id"]
        peer = db.execute("SELECT id, nickname, avatar_label FROM users WHERE id=?", (peer_id,)).fetchone() if peer_id else None
        value["is_caller"] = row["caller_id"] == user_id
        value["peer"] = dict(peer) if peer else None
        return value

    def _call_for_user(self, db: sqlite3.Connection, call_id: str, user_id: str) -> sqlite3.Row:
        couple, _ = self._couple_context(db, user_id)
        row = db.execute(
            "SELECT * FROM call_records WHERE id=? AND couple_id=? AND (caller_id=? OR callee_id=?)",
            (call_id, couple["id"], user_id, user_id),
        ).fetchone()
        if not row:
            raise KeyError("通话不存在")
        return row

    def _expire_stale_calls(self, db: sqlite3.Connection, couple_id: str) -> None:
        """Release calls abandoned by closed/crashed browser tabs.

        A ringing call stays alive while the caller page sends heartbeats and is
        marked missed after two silent minutes. A connected call is finalized
        after 90 silent seconds so it cannot permanently block the next call.
        """
        now_value = utc_now()
        now = datetime.fromisoformat(now_value)
        ringing_before = (now - timedelta(minutes=2)).isoformat()
        connected_before = (now - timedelta(seconds=90)).isoformat()
        stale_ringing = db.execute(
            "SELECT * FROM call_records WHERE couple_id=? AND state='ringing' AND COALESCE(updated_at, started_at)<?",
            (couple_id, ringing_before),
        ).fetchall()
        for row in stale_ringing:
            db.execute(
                "UPDATE call_records SET state='missed', ended_at=?, updated_at=? WHERE id=? AND state='ringing'",
                (now_value, now_value, row["id"]),
            )
            self._event(db, couple_id, row["caller_id"], "call.missed", row["id"], {"toUserId": row["callee_id"], "reason": "heartbeat_timeout"})

        stale_connected = db.execute(
            "SELECT * FROM call_records WHERE couple_id=? AND state='connected' AND COALESCE(updated_at, answered_at, started_at)<?",
            (couple_id, connected_before),
        ).fetchall()
        for row in stale_connected:
            answered_at = datetime.fromisoformat(str(row["answered_at"] or row["started_at"]).replace("Z", "+00:00"))
            duration_seconds = max(0, min(int((now - answered_at).total_seconds()), 86400))
            note = "连接中断自动结束"
            updated = db.execute(
                "UPDATE call_records SET state='ended', ended_at=?, duration_seconds=?, note=?, updated_at=? WHERE id=? AND state='connected'",
                (now_value, duration_seconds, note, now_value, row["id"]),
            )
            if updated.rowcount != 1:
                continue
            moment_id = new_id("moment")
            db.execute(
                "INSERT INTO moments(id, couple_id, creator_id, type, title, body, occurred_at, visibility) VALUES (?, ?, ?, 'call', '语音通话', ?, ?, 'shared')",
                (moment_id, couple_id, row["caller_id"], note, now_value),
            )
            self._audit(db, "call.heartbeat_timeout", "call", row["id"], {"duration_seconds": duration_seconds}, actor_id=row["caller_id"])
            self._event(db, couple_id, row["caller_id"], "call.ended", row["id"], {"toUserId": row["callee_id"], "durationSeconds": duration_seconds, "reason": "heartbeat_timeout"})
            self._event(db, couple_id, row["caller_id"], "moment.created", moment_id, {"type": "call"})

    def start_call(self, user_id: str = DEMO_USER) -> dict:
        call_id, now = new_id("call"), utc_now()
        with self.connection() as db:
            couple, partner_id = self._couple_context(db, user_id)
            self._expire_stale_calls(db, couple["id"])
            active = db.execute(
                "SELECT * FROM call_records WHERE couple_id=? AND state IN ('ringing','connected') ORDER BY started_at DESC LIMIT 1",
                (couple["id"],),
            ).fetchone()
            if active:
                if active["caller_id"] == user_id and active["state"] == "ringing":
                    return self._public_call(db, active, user_id)
                raise ValueError("当前已有一通进行中的语音通话")
            db.execute(
                "INSERT INTO call_records(id, couple_id, caller_id, callee_id, started_at, state, updated_at) VALUES (?, ?, ?, ?, ?, 'ringing', ?)",
                (call_id, couple["id"], user_id, partner_id, now, now),
            )
            self._audit(db, "call.invited", "call", call_id, {"callee_id": partner_id}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "call.invited", call_id, {"toUserId": partner_id, "fromUserId": user_id})
            row = db.execute("SELECT * FROM call_records WHERE id=?", (call_id,)).fetchone()
            return self._public_call(db, row, user_id)

    def get_active_call(self, user_id: str = DEMO_USER) -> dict | None:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            self._expire_stale_calls(db, couple["id"])
            row = db.execute(
                "SELECT * FROM call_records WHERE couple_id=? AND (caller_id=? OR callee_id=?) AND state IN ('ringing','connected') ORDER BY started_at DESC LIMIT 1",
                (couple["id"], user_id, user_id),
            ).fetchone()
            return self._public_call(db, row, user_id) if row else None

    def heartbeat_call(self, call_id: str, user_id: str = DEMO_USER) -> dict:
        now = utc_now()
        with self.connection() as db:
            row = self._call_for_user(db, call_id, user_id)
            if row["state"] not in {"ringing", "connected"}:
                raise ValueError("通话已经结束")
            db.execute("UPDATE call_records SET updated_at=? WHERE id=?", (now, call_id))
            updated = db.execute("SELECT * FROM call_records WHERE id=?", (call_id,)).fetchone()
            return self._public_call(db, updated, user_id)

    def get_call(self, call_id: str, user_id: str = DEMO_USER) -> dict:
        """Return a call only when the requesting user is one of its participants."""
        with self.connection() as db:
            row = self._call_for_user(db, call_id, user_id)
            return self._public_call(db, row, user_id)

    def answer_call(self, call_id: str, user_id: str = DEMO_USER) -> dict:
        now = utc_now()
        with self.connection() as db:
            row = self._call_for_user(db, call_id, user_id)
            if row["callee_id"] != user_id:
                raise PermissionError("只有被呼叫方可以接听")
            if row["state"] == "connected":
                return self._public_call(db, row, user_id)
            if row["state"] != "ringing":
                raise ValueError("这通电话已经无法接听")
            db.execute("UPDATE call_records SET state='connected', answered_at=?, updated_at=? WHERE id=?", (now, now, call_id))
            self._event(db, row["couple_id"], user_id, "call.answered", call_id, {"toUserId": row["caller_id"]})
            updated = db.execute("SELECT * FROM call_records WHERE id=?", (call_id,)).fetchone()
            return self._public_call(db, updated, user_id)

    def decline_call(self, call_id: str, user_id: str = DEMO_USER) -> dict:
        now = utc_now()
        with self.connection() as db:
            row = self._call_for_user(db, call_id, user_id)
            if row["callee_id"] != user_id:
                raise PermissionError("只有被呼叫方可以拒绝")
            if row["state"] not in {"ringing", "rejected"}:
                raise ValueError("这通电话已经结束")
            if row["state"] == "ringing":
                db.execute("UPDATE call_records SET state='rejected', ended_at=?, updated_at=? WHERE id=?", (now, now, call_id))
                self._event(db, row["couple_id"], user_id, "call.rejected", call_id, {"toUserId": row["caller_id"]})
            updated = db.execute("SELECT * FROM call_records WHERE id=?", (call_id,)).fetchone()
            return self._public_call(db, updated, user_id)

    def cancel_call(self, call_id: str, user_id: str = DEMO_USER) -> dict:
        now = utc_now()
        with self.connection() as db:
            row = self._call_for_user(db, call_id, user_id)
            if row["caller_id"] != user_id:
                raise PermissionError("只有发起方可以取消呼叫")
            if row["state"] not in {"ringing", "cancelled"}:
                raise ValueError("这通电话已经接通或结束")
            if row["state"] == "ringing":
                db.execute("UPDATE call_records SET state='cancelled', ended_at=?, updated_at=? WHERE id=?", (now, now, call_id))
                self._event(db, row["couple_id"], user_id, "call.cancelled", call_id, {"toUserId": row["callee_id"]})
            updated = db.execute("SELECT * FROM call_records WHERE id=?", (call_id,)).fetchone()
            return self._public_call(db, updated, user_id)

    def create_call_signal(self, call_id: str, payload: dict, user_id: str = DEMO_USER) -> dict:
        signal_type = str(payload.get("type", "")).strip().lower()
        signal = payload.get("payload")
        if signal_type not in {"offer", "answer", "ice"} or not isinstance(signal, dict):
            raise ValueError("信令类型或内容无效")
        if signal_type in {"offer", "answer"}:
            if signal.get("type") != signal_type or not isinstance(signal.get("sdp"), str) or not signal["sdp"].strip():
                raise ValueError("SDP 信令内容无效")
        else:
            candidate = signal.get("candidate")
            if not isinstance(candidate, str) or not candidate.strip() or len(candidate) > 8192:
                raise ValueError("ICE Candidate 无效")
            if signal.get("sdpMid") is not None and not isinstance(signal.get("sdpMid"), str):
                raise ValueError("ICE sdpMid 无效")
            line_index = signal.get("sdpMLineIndex")
            if line_index is not None and (isinstance(line_index, bool) or not isinstance(line_index, int) or not 0 <= line_index <= 65535):
                raise ValueError("ICE sdpMLineIndex 无效")
        encoded = json.dumps(signal, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 128_000:
            raise ValueError("信令内容过大")
        now = utc_now()
        with self.connection() as db:
            row = self._call_for_user(db, call_id, user_id)
            if row["state"] not in {"ringing", "connected"}:
                raise ValueError("通话已经结束")
            if signal_type == "offer" and row["caller_id"] != user_id:
                raise PermissionError("只有发起方可以发送 Offer")
            if signal_type == "answer" and (row["callee_id"] != user_id or row["state"] != "connected"):
                raise PermissionError("只有接听方可以发送 Answer")
            cursor = db.execute(
                "INSERT INTO call_signals(call_id, sender_id, signal_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (call_id, user_id, signal_type, encoded, now),
            )
            sequence = int(cursor.lastrowid)
            target_id = row["callee_id"] if row["caller_id"] == user_id else row["caller_id"]
            self._event(db, row["couple_id"], user_id, "call.signal", call_id, {"toUserId": target_id, "signalSequence": sequence, "signalType": signal_type})
            return {"sequence": sequence, "call_id": call_id, "type": signal_type, "created_at": now}

    def list_call_signals(self, call_id: str, after: object = 0, user_id: str = DEMO_USER) -> dict:
        try:
            cursor = max(0, int(after or 0))
        except (TypeError, ValueError) as error:
            raise ValueError("信令序号必须是整数") from error
        with self.connection() as db:
            self._call_for_user(db, call_id, user_id)
            rows = db.execute(
                "SELECT sequence, call_id, sender_id, signal_type, payload_json, created_at FROM call_signals WHERE call_id=? AND sequence>? ORDER BY sequence LIMIT 500",
                (call_id, cursor),
            ).fetchall()
        items = [
            {
                "sequence": row["sequence"],
                "call_id": row["call_id"],
                "sender_id": row["sender_id"],
                "type": row["signal_type"],
                "payload": parse_json(row["payload_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
        return {"items": items, "latest_sequence": items[-1]["sequence"] if items else cursor}

    def end_call(self, call_id: str, duration_seconds: object, note: object = "", user_id: str = DEMO_USER) -> dict:
        ended_at = utc_now()
        try:
            duration_seconds = int(duration_seconds)
        except (TypeError, ValueError) as error:
            raise ValueError("通话时长必须是整数") from error
        duration_seconds = max(0, min(duration_seconds, 86400))
        if not isinstance(note, str):
            raise ValueError("通话备注必须是文字")
        with self.connection() as db:
            row = self._call_for_user(db, call_id, user_id)
            if row["state"] in {"ended", "rejected", "cancelled", "missed"}:
                return self._public_call(db, row, user_id)
            if row["state"] == "ringing":
                final_state = "cancelled" if row["caller_id"] == user_id else "rejected"
                db.execute("UPDATE call_records SET ended_at=?, duration_seconds=0, state=?, note=?, updated_at=? WHERE id=?", (ended_at, final_state, note[:500], ended_at, call_id))
                self._event(db, row["couple_id"], user_id, f"call.{final_state}", call_id, {})
                updated = db.execute("SELECT * FROM call_records WHERE id=?", (call_id,)).fetchone()
                return self._public_call(db, updated, user_id)
            if row["answered_at"]:
                answered_at = datetime.fromisoformat(str(row["answered_at"]).replace("Z", "+00:00"))
                finished_at = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
                duration_seconds = max(0, min(int((finished_at - answered_at).total_seconds()), 86400))
            db.execute(
                "UPDATE call_records SET ended_at=?, duration_seconds=?, state='ended', note=?, updated_at=? WHERE id=?",
                (ended_at, duration_seconds, note[:500], ended_at, call_id),
            )
            moment_id = new_id("moment")
            db.execute(
                "INSERT INTO moments(id, couple_id, creator_id, type, title, body, occurred_at, visibility) VALUES (?, ?, ?, 'call', '语音通话', ?, ?, 'shared')",
                (moment_id, row["couple_id"], user_id, note or f"通话 {duration_seconds} 秒", ended_at),
            )
            self._upsert_memory_provenance_in_connection(
                db, "moment", moment_id, row["couple_id"], user_id,
                source_type="human", date_source="user_confirmed", resource_version=1,
            )
            self._audit(db, "call.ended", "call", call_id, {"duration_seconds": duration_seconds}, actor_id=user_id)
            target_id = row["callee_id"] if row["caller_id"] == user_id else row["caller_id"]
            self._event(db, row["couple_id"], user_id, "call.ended", call_id, {"toUserId": target_id, "durationSeconds": duration_seconds})
            self._event(db, row["couple_id"], user_id, "moment.created", moment_id, {"type": "call"})
            result = db.execute("SELECT * FROM call_records WHERE id=?", (call_id,)).fetchone()
            return self._public_call(db, result, user_id)

    def list_moments(self, limit: int = 50, user_id: str = DEMO_USER) -> list[dict]:
        with self.connection() as db:
            self._reconcile_delayed_moments(db)
            try:
                couple, _ = self._memory_couple_context(db, user_id)
            except ValueError:
                return []
            rows = db.execute(
                f"""SELECT m.*,COALESCE(p.source_type,'legacy_unknown') AS provenance_source_type,
                           COALESCE(p.date_source,'legacy_unknown') AS provenance_date_source
                    FROM moments m LEFT JOIN memory_provenance p ON p.moment_id=m.id
                    WHERE {self._visible_moment_sql('m')}
                    ORDER BY m.occurred_at DESC,m.id DESC LIMIT ?""",
                (couple["id"], user_id, utc_now(), limit),
            ).fetchall()
            items = [dict(row) for row in rows]
            for item in items:
                source_type = item.pop("provenance_source_type")
                date_source = item.pop("provenance_date_source")
                item["provenance"] = {
                    "source_type": source_type,
                    "source_label": SOURCE_LABELS.get(source_type, "来源未记录"),
                    "date_source": date_source,
                    "date_source_label": DATE_SOURCE_LABELS.get(date_source, "时间来源未记录"),
                }
                reactions = db.execute(
                    "SELECT r.user_id,r.reaction,r.updated_at,u.nickname,u.avatar_label FROM moment_reactions r JOIN users u ON u.id=r.user_id WHERE r.moment_id=? ORDER BY r.updated_at",
                    (item["id"],),
                ).fetchall()
                item["reactions"] = [dict(reaction) for reaction in reactions]
                item["my_reaction"] = next((reaction["reaction"] for reaction in reactions if reaction["user_id"] == user_id), None)
        return items

    def set_moment_reaction(self, moment_id: str, reaction: object, user_id: str = DEMO_USER) -> dict:
        value = str(reaction or "").strip()
        if value not in {"heart", "smile", "hug", "none"}:
            raise ValueError("不支持的回应")
        now = utc_now()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            moment = db.execute(
                "SELECT id FROM moments WHERE id=? AND couple_id=? AND deleted_at IS NULL AND purged_at IS NULL AND (visibility='shared' OR creator_id=?)",
                (moment_id, couple["id"], user_id),
            ).fetchone()
            if not moment:
                raise KeyError("瞬间不存在")
            if value == "none":
                db.execute("DELETE FROM moment_reactions WHERE moment_id=? AND user_id=?", (moment_id, user_id))
            else:
                db.execute(
                    "INSERT INTO moment_reactions(moment_id,user_id,reaction,created_at,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(moment_id,user_id) DO UPDATE SET reaction=excluded.reaction,updated_at=excluded.updated_at",
                    (moment_id, user_id, value, now, now),
                )
            self._event(db, couple["id"], user_id, "life.updated", moment_id, {"resource": "moment_reaction"})
        return {"moment_id": moment_id, "reaction": None if value == "none" else value}

    def create_moment(self, payload: dict, user_id: str = DEMO_USER) -> dict:
        title = str(payload.get("title", "")).strip()
        if not title:
            raise ValueError("瞬间标题不能为空")
        visibility_map = {"双方可见": "shared", "仅自己": "private", "延时公开": "delayed", "shared": "shared", "private": "private", "delayed": "delayed"}
        visibility = visibility_map.get(str(payload.get("visibility", "shared")))
        if not visibility:
            raise ValueError("无效的可见范围")
        moment_type = str(payload.get("type", "text"))
        if moment_type not in {"text", "photo", "video", "voice", "chat", "call", "trip", "anniversary", "wishlist", "location", "weekly_review", "date"}:
            raise ValueError("不支持的瞬间类型")
        reveal_at = payload.get("reveal_at")
        if visibility == "delayed" and not reveal_at:
            reveal_at = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
        elif visibility == "delayed":
            reveal_at = parse_iso_datetime(reveal_at, "公开时间")
            if reveal_at <= utc_now():
                raise ValueError("延时公开时间必须晚于当前时间")
        else:
            reveal_at = None
        explicit_occurred_at = bool(payload.get("occurred_at"))
        occurred_at = parse_iso_datetime(payload["occurred_at"], "发生时间") if explicit_occurred_at else None
        date_source = "user_confirmed" if explicit_occurred_at else "system_provisional"
        moment_id = new_id("moment")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            previous = self._idempotent_result(db, couple["id"], user_id, "moment.create", payload.get("idempotency_key"))
            if previous:
                return previous
            media_value = payload.get("media_id")
            if media_value is not None and not isinstance(media_value, str):
                raise ValueError("瞬间媒体 ID 无效")
            media_id = media_value.strip() or None if isinstance(media_value, str) else None
            media_url = None
            if media_id:
                media = db.execute("SELECT * FROM media_assets WHERE id=? AND couple_id=? AND status='ready'", (media_id, couple["id"])).fetchone()
                if not media:
                    raise ValueError("瞬间引用的媒体不存在")
                if media["owner_id"] != user_id:
                    raise PermissionError("只能使用自己上传的媒体")
                media_url = f"/api/v1/media/{media_id}"
                if occurred_at is None and media["captured_at"]:
                    occurred_at = str(media["captured_at"])
                    date_source = "media_capture"
            if occurred_at is None:
                occurred_at = utc_now()
            db.execute(
                "INSERT INTO moments(id, couple_id, creator_id, type, title, body, occurred_at, location, mood, visibility, reveal_at, media_url, media_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (moment_id, couple["id"], user_id, moment_type, title[:200], str(payload.get("body", ""))[:5000], occurred_at, str(payload.get("location", ""))[:200] or None, str(payload.get("mood", ""))[:50] or None, visibility, reveal_at, media_url, media_id),
            )
            self._upsert_memory_provenance_in_connection(
                db, "moment", moment_id, couple["id"], user_id,
                source_type="human", date_source=date_source, resource_version=1,
            )
            if media_id:
                self._sync_media_visibility(db, media_id)
            self._audit(db, "moment.created", "moment", moment_id, {"visibility": visibility}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "moment.created", moment_id, {"type": moment_type, "visibility": visibility, "owner_only": visibility != "shared"})
            row = db.execute("SELECT * FROM moments WHERE id = ?", (moment_id,)).fetchone()
            result = dict(row)
            self._save_idempotent_result(db, couple["id"], user_id, "moment.create", payload.get("idempotency_key"), result)
        return result

    def update_moment(self, moment_id: str, payload: dict, user_id: str = DEMO_USER) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("瞬间修改内容无效")
        visibility_map = {"双方可见": "shared", "仅自己": "private", "延时公开": "delayed", "shared": "shared", "private": "private", "delayed": "delayed"}
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute(
                "SELECT * FROM moments WHERE id=? AND couple_id=? AND deleted_at IS NULL AND purged_at IS NULL",
                (moment_id, couple["id"]),
            ).fetchone()
            if not row:
                raise KeyError("瞬间不存在")
            if row["creator_id"] != user_id:
                raise PermissionError("只能修改自己创建的瞬间")

            expected_version = payload.get("expected_version")
            if expected_version is not None:
                try:
                    expected_version = int(expected_version)
                except (TypeError, ValueError) as error:
                    raise ValueError("瞬间版本无效") from error
                if expected_version != int(row["version"]):
                    raise ConflictError("这段回忆已发生变化，请刷新后重新修改")

            if "title" in payload and not isinstance(payload["title"], str):
                raise ValueError("瞬间标题必须是文字")
            if "body" in payload and not isinstance(payload["body"], str):
                raise ValueError("瞬间正文必须是文字")
            if "location" in payload and payload["location"] is not None and not isinstance(payload["location"], str):
                raise ValueError("瞬间地点必须是文字")
            if "mood" in payload and payload["mood"] is not None and not isinstance(payload["mood"], str):
                raise ValueError("瞬间心情必须是文字")
            title = payload["title"].strip() if "title" in payload else row["title"]
            if not title:
                raise ValueError("瞬间标题不能为空")
            body = str(payload.get("body") or "") if "body" in payload else row["body"]
            location = (str(payload.get("location") or "").strip() or None) if "location" in payload else row["location"]
            mood = (str(payload.get("mood") or "").strip() or None) if "mood" in payload else row["mood"]
            occurred_at = parse_iso_datetime(payload.get("occurred_at"), "发生时间") if "occurred_at" in payload else row["occurred_at"]

            visibility_value = str(payload.get("visibility", row["visibility"]))
            visibility = visibility_map.get(visibility_value)
            if not visibility:
                raise ValueError("无效的可见范围")
            if visibility == "delayed":
                reveal_value = payload.get("reveal_at", row["reveal_at"])
                reveal = parse_iso_datetime(reveal_value, "公开时间") if reveal_value else (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
                if reveal <= utc_now() and not (visibility == row["visibility"] and reveal == row["reveal_at"]):
                    raise ValueError("延时公开时间必须晚于当前时间")
            else:
                reveal = None

            db.execute(
                "UPDATE moments SET title=?,body=?,occurred_at=?,location=?,mood=?,visibility=?,reveal_at=?,version=version+1 WHERE id=?",
                (title[:200], body[:5000], occurred_at, location[:200] if location else None, mood[:50] if mood else None, visibility, reveal, moment_id),
            )
            provenance = db.execute("SELECT * FROM memory_provenance WHERE moment_id=?", (moment_id,)).fetchone()
            self._upsert_memory_provenance_in_connection(
                db, "moment", moment_id, couple["id"], user_id,
                source_type="human",
                date_source="user_confirmed" if "occurred_at" in payload else (provenance["date_source"] if provenance else "legacy_unknown"),
                resource_version=int(row["version"]) + 1,
            )
            if row["media_id"]:
                self._sync_media_visibility(db, row["media_id"])
            self._audit(db, "moment.updated", "moment", moment_id, {"visibility": visibility}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "life.updated", moment_id, {"resource": "moment", "visibility": visibility})
            updated = db.execute("SELECT * FROM moments WHERE id=?", (moment_id,)).fetchone()
        return dict(updated)

    def update_moment_visibility(self, moment_id: str, visibility_value: str, reveal_at: object = None, user_id: str = DEMO_USER) -> dict:
        return self.update_moment(moment_id, {"visibility": visibility_value, "reveal_at": reveal_at}, user_id)

    def get_trip(self, trip_id: str = DEMO_TRIP, user_id: str = DEMO_USER) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            trip_row = db.execute("SELECT * FROM trips WHERE id = ? AND couple_id=? AND deleted_at IS NULL", (trip_id, couple["id"])).fetchone()
            if not trip_row:
                raise KeyError("旅行不存在")
            trip = dict(trip_row)
            destination_row = db.execute(
                "SELECT provider,provider_place_id,name,formatted_address,country,province,city,district,latitude,longitude,category,confirmed_by,confirmed_at FROM trip_destination_places WHERE trip_id=?",
                (trip_id,),
            ).fetchone()
            trip["destination_place"] = dict(destination_row) if destination_row else None
            trip["needs_destination_detail"] = destination_row is None or not bool(str(trip.get("arrival_city") or "").strip())
            variants = []
            for variant_row in db.execute("SELECT * FROM plan_variants WHERE trip_id=? ORDER BY CASE style WHEN 'slow' THEN 1 WHEN 'classic' THEN 2 WHEN 'value' THEN 3 WHEN 'food' THEN 4 WHEN 'nature' THEN 5 ELSE 6 END", (trip_id,)).fetchall():
                variant = dict(variant_row)
                days = []
                for day_row in db.execute("SELECT * FROM trip_days WHERE variant_id=? ORDER BY position", (variant["id"],)).fetchall():
                    day = dict(day_row)
                    day["items"] = []
                    for item_row in db.execute("SELECT * FROM itinerary_items WHERE day_id=? ORDER BY position", (day["id"],)).fetchall():
                        item = dict(item_row)
                        try:
                            item["details"] = json.loads(item.pop("details_json", "{}") or "{}")
                        except (TypeError, json.JSONDecodeError):
                            item.pop("details_json", None)
                            item["details"] = {}
                        place_row = db.execute(
                            "SELECT provider,provider_place_id,name,formatted_address,country,province,city,district,"
                            "latitude,longitude,category,confirmed_by,confirmed_at FROM itinerary_item_places "
                            "WHERE itinerary_item_id=?",
                            (item["id"],),
                        ).fetchone()
                        item["place"] = dict(place_row) if place_row else None
                        item["food_recommendations"] = [dict(row) for row in db.execute(
                            "SELECT * FROM trip_food_recommendations WHERE trip_id=? AND itinerary_item_id=? ORDER BY selected_at IS NULL, rating IS NULL, rating DESC, name",
                            (trip_id, item["id"]),
                        ).fetchall()]
                        item["review_evidence"] = [dict(row) for row in db.execute(
                            "SELECT id,platform,title,snippet,url,fetched_at FROM trip_food_review_evidence WHERE trip_id=? AND itinerary_item_id=? ORDER BY platform,title",
                            (trip_id, item["id"]),
                        ).fetchall()]
                        day["items"].append(item)
                    days.append(day)
                variant["days"] = days
                variants.append(variant)
            hotels = [dict(row) for row in db.execute("SELECT * FROM hotel_options WHERE trip_id=?", (trip_id,)).fetchall()]
            for hotel in hotels:
                hotel["quotes"] = [dict(row) for row in db.execute("SELECT * FROM booking_links WHERE target_id=? ORDER BY quote_cents", (hotel["id"],)).fetchall()]
            trip["variants"] = variants
            trip["hotels"] = hotels
            trip["quotes"] = []
            for quote_row in db.execute("SELECT * FROM quote_snapshots WHERE trip_id=? ORDER BY selected_at IS NULL, selected_at DESC, fetched_at DESC LIMIT 100", (trip_id,)).fetchall():
                quote = dict(quote_row)
                quote["details"] = parse_json(quote.pop("details_json", "{}"), {})
                trip["quotes"].append(quote)
            trip["revisions"] = [dict(row) for row in db.execute("SELECT * FROM plan_revisions WHERE trip_id=? ORDER BY revision DESC LIMIT 10", (trip_id,)).fetchall()]
            approvals = db.execute(
                "SELECT user_id FROM approvals WHERE trip_id=? AND revision=? AND status='approved'",
                (trip_id, trip["current_revision"]),
            ).fetchall()
            approved_user_ids = [row["user_id"] for row in approvals]
            trip["approval_state"] = {
                "approved_count": len(approved_user_ids),
                "required_count": 2,
                "approved_by_me": user_id in approved_user_ids,
            }
            conflicts = db.execute(
                "SELECT id,title,destination,start_date,end_date FROM trips WHERE couple_id=? AND id<>? AND deleted_at IS NULL AND start_date<=? AND end_date>=? ORDER BY start_date",
                (couple["id"], trip_id, trip["end_date"], trip["start_date"]),
            ).fetchall()
            trip["date_conflicts"] = [dict(row) for row in conflicts]
        return trip

    def select_variant(
        self,
        trip_id: str,
        style: str,
        user_id: str = DEMO_USER,
        idempotency_key: object = None,
    ) -> dict:
        if style not in {"slow", "classic", "value", "food", "nature"}:
            raise ValueError("无效的方案类型")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            previous = self._idempotent_result(
                db,
                couple["id"],
                user_id,
                "trip.variant.select",
                idempotency_key,
            )
            if previous:
                if previous.get("trip_id") != trip_id or previous.get("style") != style:
                    raise ConflictError("相同幂等键已用于另一套旅行方案")
                replayed = True
            else:
                replayed = False
            trip = db.execute("SELECT * FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL", (trip_id, couple["id"])).fetchone()
            if not trip:
                raise KeyError("旅行不存在")
            variant = db.execute("SELECT id, name FROM plan_variants WHERE trip_id=? AND style=?", (trip_id, style)).fetchone()
            if not variant:
                raise ValueError("这套方案尚未生成")
            if not replayed and not (trip["active_variant"] == style and trip["status"] in {"pending_approval", "confirmed"}):
                revision = int(trip["current_revision"]) + 1
                revision_id = new_id("revision")
                item_count = db.execute(
                    "SELECT COUNT(*) AS n FROM itinerary_items i JOIN trip_days d ON d.id=i.day_id WHERE d.variant_id=?",
                    (variant["id"],),
                ).fetchone()["n"]
                diff = {"selected_variant": style, "variant_name": variant["name"], "selected_count": item_count}
                db.execute(
                    "UPDATE trips SET active_variant=?, current_revision=?, status='pending_approval', version=version+1, updated_at=? WHERE id=?",
                    (style, revision, utc_now(), trip_id),
                )
                db.execute("DELETE FROM approvals WHERE trip_id=?", (trip_id,))
                db.execute(
                    "INSERT INTO plan_revisions(id, trip_id, revision, parent_revision, author_type, diff_json, reason, created_at) VALUES (?, ?, ?, ?, 'user', ?, '采用整套方案', ?)",
                    (revision_id, trip_id, revision, trip["current_revision"], json.dumps(diff, ensure_ascii=False), utc_now()),
                )
                self._audit(db, "trip.variant_selected", "trip", trip_id, {**diff, "revision": revision}, actor_id=user_id)
                self._event(db, couple["id"], user_id, "life.updated", trip_id, {"resource": "trip", "style": style, "revision": revision})
            self._save_idempotent_result(
                db,
                couple["id"],
                user_id,
                "trip.variant.select",
                idempotency_key,
                {"trip_id": trip_id, "style": style},
            )
        return self.get_trip(trip_id, user_id=user_id)

    def regenerate_trip(self, trip_id: str, user_id: str = DEMO_USER, preserve_manual: bool = True) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            trip = db.execute("SELECT * FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL", (trip_id, couple["id"])).fetchone()
            if not trip:
                raise KeyError("旅行不存在")
            destination, budget_cents = trip["destination"], int(trip["budget_cents"])
            preference_rows = db.execute(
                "SELECT user_id,pace,interests,transport,diet,hotel,must_visits FROM trip_preferences WHERE trip_id=? ORDER BY user_id",
                (trip_id,),
            ).fetchall()
            preference_values = [dict(row) for row in preference_rows]
            destination_place_row = db.execute("SELECT * FROM trip_destination_places WHERE trip_id=?", (trip_id,)).fetchone()
            context = {
                "start_date": trip["start_date"],
                "end_date": trip["end_date"],
                "departure_city": trip["departure_city"],
                "arrival_city": trip["arrival_city"],
                "outbound_time": trip["outbound_time"],
                "return_time": trip["return_time"],
                "travelers": trip["travelers"],
                "rooms": trip["rooms"],
                "hotel_budget_cents": trip["hotel_budget_cents"],
                "preferences": preference_values[0] if preference_values else {},
                "partner_preferences": preference_values[1] if len(preference_values) > 1 else {},
                "destination_place": dict(destination_place_row) if destination_place_row else {},
            }
        plans = self.planner.generate_variants(destination, budget_cents, context)
        if len(plans) < 5:
            raise ValueError("行程生成必须返回至少五套方案")
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            trip = db.execute("SELECT * FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL", (trip_id, couple["id"])).fetchone()
            if not trip:
                raise KeyError("旅行不存在")
            locked_rows = db.execute(
                "SELECT v.style, d.day_number, i.position, i.title, i.start_time, i.duration_minutes, i.cost_cents, i.note, i.details_json "
                "FROM itinerary_items i JOIN trip_days d ON d.id=i.day_id "
                "JOIN plan_variants v ON v.id=d.variant_id WHERE v.trip_id=? AND i.locked=1",
                (trip_id,),
            ).fetchall() if preserve_manual else []
            db.execute("DELETE FROM trip_food_review_evidence WHERE trip_id=?", (trip_id,))
            db.execute("DELETE FROM trip_food_recommendations WHERE trip_id=?", (trip_id,))
            db.execute(
                "DELETE FROM itinerary_items WHERE day_id IN (SELECT d.id FROM trip_days d JOIN plan_variants v ON v.id=d.variant_id WHERE v.trip_id=?)",
                (trip_id,),
            )
            db.execute("DELETE FROM trip_days WHERE variant_id IN (SELECT id FROM plan_variants WHERE trip_id=?)", (trip_id,))
            db.execute("DELETE FROM plan_variants WHERE trip_id=?", (trip_id,))
            self._insert_generated_plans(db, trip_id, destination, budget_cents, plans)
            for locked in locked_rows:
                replacement = db.execute(
                    "SELECT i.id FROM itinerary_items i JOIN trip_days d ON d.id=i.day_id "
                    "JOIN plan_variants v ON v.id=d.variant_id WHERE v.trip_id=? AND v.style=? AND d.day_number=? AND i.position=?",
                    (trip_id, locked["style"], locked["day_number"], locked["position"]),
                ).fetchone()
                if replacement:
                    db.execute(
                        "UPDATE itinerary_items SET title=?, start_time=?, duration_minutes=?, cost_cents=?, note=?, details_json=?, locked=1 WHERE id=?",
                        (locked["title"], locked["start_time"], locked["duration_minutes"], locked["cost_cents"], locked["note"], locked["details_json"], replacement["id"]),
                    )
            revision = int(trip["current_revision"]) + 1
            revision_id = new_id("revision")
            generation_source = plans[0].generation_source if plans else "rules"
            generation_message = plans[0].generation_message if plans else "未生成方案"
            diff = {
                "regenerated_styles": [plan.style for plan in plans],
                "protected_manual_items": len(locked_rows),
                "cleared_old_scope_items": not preserve_manual,
                "generation_source": generation_source,
                "generation_message": generation_message,
            }
            db.execute(
                "UPDATE trips SET active_variant='slow', current_revision=?, status='planning', version=version+1 WHERE id=?",
                (revision, trip_id),
            )
            db.execute("DELETE FROM approvals WHERE trip_id=?", (trip_id,))
            db.execute(
                "INSERT INTO plan_revisions(id, trip_id, revision, parent_revision, author_type, diff_json, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id, trip_id, revision, trip["current_revision"],
                    "ai" if generation_source == "ai" else "rules",
                    json.dumps(diff, ensure_ascii=False),
                    "大模型重新生成五套方案" if generation_source == "ai" else "大模型未完成，保留本地安全方案",
                    utc_now(),
                ),
            )
            self._audit(db, "trip.regenerated", "trip", trip_id, diff, actor_id=user_id)
            self._event(db, couple["id"], user_id, "life.updated", trip_id, {"resource": "trip", "revision": revision})
        return self.get_trip(trip_id, user_id=user_id)

    def impact_analysis(self, item_id: str, changes: dict, user_id: str = DEMO_USER) -> dict:
        if not isinstance(changes, dict):
            raise ValueError("changes 必须是对象")
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute("SELECT i.* FROM itinerary_items i JOIN trip_days d ON d.id=i.day_id JOIN plan_variants v ON v.id=d.variant_id JOIN trips t ON t.id=v.trip_id WHERE i.id=? AND t.couple_id=?", (item_id, couple["id"])).fetchone()
        if not row:
            raise KeyError("行程项目不存在")
        return self.planner.analyze_impact(dict(row), changes)

    def revise_item(
        self, item_id: str, changes: dict, reason: str = "用户手动调整",
        user_id: str = DEMO_USER, expected_trip_version: object = None,
        expected_item_version: object = None,
    ) -> dict:
        if not isinstance(changes, dict):
            raise ValueError("changes 必须是对象")
        allowed = {"title", "start_time", "duration_minutes", "note", "position"}
        updates = {key: value for key, value in changes.items() if key in allowed}
        if not updates:
            raise ValueError("没有可更新的字段")
        if "title" in updates:
            updates["title"] = str(updates["title"]).strip()[:100]
            if not updates["title"]:
                raise ValueError("行程标题不能为空")
        if "start_time" in updates and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(updates["start_time"])):
            raise ValueError("开始时间格式必须为 HH:MM")
        if "duration_minutes" in updates:
            updates["duration_minutes"] = int(updates["duration_minutes"])
            if not 15 <= updates["duration_minutes"] <= 1440:
                raise ValueError("行程时长需为 15–1440 分钟")
        if "position" in updates:
            updates["position"] = int(updates["position"])
            if updates["position"] < 1:
                raise ValueError("行程顺序必须大于 0")
        if "note" in updates:
            updates["note"] = str(updates["note"])[:500]
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute(
                "SELECT i.*, v.trip_id FROM itinerary_items i JOIN trip_days d ON d.id=i.day_id JOIN plan_variants v ON v.id=d.variant_id JOIN trips t ON t.id=v.trip_id WHERE i.id=? AND t.couple_id=?",
                (item_id, couple["id"]),
            ).fetchone()
            if not row:
                raise KeyError("行程项目不存在")
            trip_id = row["trip_id"]
            trip = db.execute("SELECT current_revision,version FROM trips WHERE id=?", (trip_id,)).fetchone()
            if expected_trip_version is not None and int(expected_trip_version) != int(trip["version"]):
                raise ConflictError("旅行已经被另一方修改，请重新预览")
            if expected_item_version is not None and int(expected_item_version) != int(row["version"]):
                raise ConflictError("这条行程已经被另一方修改，请重新预览")
            revision = int(trip["current_revision"]) + 1
            diff = {key: {"before": row[key], "after": value} for key, value in updates.items()}
            if "title" in updates and updates["title"] != row["title"]:
                # Details generated for the old place must not be presented as
                # if they describe a manually renamed destination.
                updates["details_json"] = "{}"
                # A renamed item is a different place until the user confirms
                # a new map candidate. Adjacent route caches remain available
                # for unaffected item pairs because cache keys use coordinates.
                db.execute("DELETE FROM itinerary_item_places WHERE itinerary_item_id=?", (item_id,))
            set_sql = ", ".join(f"{key}=?" for key in updates)
            db.execute(f"UPDATE itinerary_items SET {set_sql}, locked=1, version=version+1 WHERE id=?", (*updates.values(), item_id))
            db.execute("UPDATE trips SET current_revision=?, version=version+1 WHERE id=?", (revision, trip_id))
            revision_id = new_id("revision")
            db.execute(
                "INSERT INTO plan_revisions(id, trip_id, revision, parent_revision, author_type, diff_json, reason, created_at) VALUES (?, ?, ?, ?, 'user', ?, ?, ?)",
                (revision_id, trip_id, revision, revision - 1, json.dumps(diff, ensure_ascii=False), reason[:500], utc_now()),
            )
            db.execute("DELETE FROM approvals WHERE trip_id=?", (trip_id,))
            self._audit(db, "trip.item_revised", "itinerary_item", item_id, diff, actor_id=user_id)
            self._event(db, couple["id"], user_id, "life.updated", trip_id, {"resource": "trip", "revision": revision})
        return {"revision_id": revision_id, "revision": revision, "diff": diff, "trip": self.get_trip(trip_id, user_id=user_id)}

    def compose_plan(self, trip_id: str, selected_item_ids: object, user_id: str = DEMO_USER) -> dict:
        if not isinstance(selected_item_ids, list) or not all(isinstance(item_id, str) for item_id in selected_item_ids):
            raise ValueError("selected_item_ids 必须是字符串数组")
        if len(selected_item_ids) > 100:
            raise ValueError("一次最多混搭 100 个项目")
        if not selected_item_ids:
            raise ValueError("至少选择一个行程项目")
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            if not db.execute("SELECT 1 FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL", (trip_id, couple["id"])).fetchone():
                raise KeyError("旅行不存在")
            placeholders = ",".join("?" for _ in selected_item_ids)
            rows = db.execute(
                f"SELECT i.id, i.title, v.trip_id FROM itinerary_items i JOIN trip_days d ON d.id=i.day_id JOIN plan_variants v ON v.id=d.variant_id WHERE i.id IN ({placeholders}) AND v.trip_id=?",
                (*selected_item_ids, trip_id),
            ).fetchall()
            if len(rows) != len(set(selected_item_ids)):
                raise ValueError("包含无效的行程项目")
            current = db.execute("SELECT current_revision FROM trips WHERE id=?", (trip_id,)).fetchone()["current_revision"]
            revision = int(current) + 1
            revision_id = new_id("revision")
            diff = {"selected_items": [dict(row) for row in rows]}
            db.execute("UPDATE trips SET current_revision=?, status='pending_approval', version=version+1 WHERE id=?", (revision, trip_id))
            db.execute(
                "INSERT INTO plan_revisions(id, trip_id, revision, parent_revision, author_type, diff_json, reason, created_at) VALUES (?, ?, ?, ?, 'user', ?, '跨方案混搭', ?)",
                (revision_id, trip_id, revision, current, json.dumps(diff, ensure_ascii=False), utc_now()),
            )
            self._audit(db, "trip.composed", "trip", trip_id, diff, actor_id=user_id)
            self._event(db, couple["id"], user_id, "life.updated", trip_id, {"resource": "trip", "revision": revision})
        return {"revision_id": revision_id, "revision": revision, "status": "pending_approval", "selected_count": len(rows)}

    def approve_trip(self, trip_id: str, revision: object, user_id: str = DEMO_USER) -> dict:
        try:
            revision = int(revision)
        except (TypeError, ValueError) as error:
            raise ValueError("旅行版本必须是整数") from error
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            trip = db.execute("SELECT current_revision FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL", (trip_id, couple["id"])).fetchone()
            if not trip or int(trip["current_revision"]) != int(revision):
                raise ValueError("只能确认当前版本")
            db.execute(
                "INSERT INTO approvals(trip_id, revision, user_id, status, updated_at) VALUES (?, ?, ?, 'approved', ?) ON CONFLICT(trip_id, revision, user_id) DO UPDATE SET status='approved', updated_at=excluded.updated_at",
                (trip_id, revision, user_id, utc_now()),
            )
            approvals = db.execute("SELECT COUNT(*) AS n FROM approvals WHERE trip_id=? AND revision=? AND status='approved'", (trip_id, revision)).fetchone()["n"]
            if approvals >= 2:
                db.execute("UPDATE trips SET status='confirmed' WHERE id=?", (trip_id,))
            self._audit(db, "trip.approved", "trip", trip_id, {"revision": revision}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "life.updated", trip_id, {"resource": "trip", "approvals": approvals})
        return {"revision": revision, "approvals": approvals, "confirmed": approvals >= 2}

    def get_wishlist(self, user_id: str = DEMO_USER) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            list_row = db.execute("SELECT * FROM shared_lists WHERE couple_id=? AND type='wishlist' LIMIT 1", (couple["id"],)).fetchone()
            if not list_row:
                return {"id": None, "couple_id": couple["id"], "type": "wishlist", "title": "我们的心愿", "items": []}
            result = dict(list_row)
            rows = db.execute(
                "SELECT * FROM shared_list_items WHERE list_id=? AND deleted_at IS NULL ORDER BY position",
                (result["id"],),
            ).fetchall()
            result["items"] = [self._wishlist_item_for_user(db, row, user_id) for row in rows]
        return result

    @staticmethod
    def _wishlist_item_for_user(db: sqlite3.Connection, row: sqlite3.Row, user_id: str) -> dict:
        item = {
            key: row[key]
            for key in (
                "id", "list_id", "creator_id", "text", "note", "completed_at", "position",
                "version", "item_kind", "created_at", "updated_at",
            )
        }
        item["is_creator"] = row["creator_id"] == user_id
        if not item["is_creator"]:
            claim = db.execute(
                "SELECT * FROM wishlist_claims WHERE item_id=? AND claimant_id=? AND status='active'",
                (row["id"], user_id),
            ).fetchone()
            item["can_claim"] = bool(row["item_kind"] == "gift" and not row["completed_at"] and not claim)
            item["my_claim"] = CoupleInteractionMixin._claim_private(claim, include_note=True) if claim else None
        return item

    def _cancel_active_wishlist_claim(
        self, db: sqlite3.Connection, *, couple_id: str, item_id: str,
        actor_id: str, now: str, reason: str,
    ) -> None:
        claim = db.execute(
            "SELECT * FROM wishlist_claims WHERE item_id=? AND status='active'",
            (item_id,),
        ).fetchone()
        if not claim:
            return
        db.execute(
            "UPDATE wishlist_claims SET status='cancelled',version=version+1,updated_at=?,ended_at=? WHERE id=?",
            (now, now, claim["id"]),
        )
        self._insert_in_app_notification(
            db, couple_id=couple_id, recipient_id=str(claim["claimant_id"]), actor_id=actor_id,
            event_type="wishlist.claim_cancelled", resource_type="shared_list_item", resource_id=item_id,
            title="一份礼物心愿有变化", body="这份礼物心愿已结束，你的私密准备记录已经关闭。",
            url="/?screen=life", dedupe_key=f"wishlist-claim-cancelled:{item_id}:{reason}",
        )

    def toggle_wishlist_item(self, item_id: str, completed: object, user_id: str = DEMO_USER) -> dict:
        if not isinstance(completed, bool):
            raise ValueError("completed 必须是布尔值")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            row = db.execute("SELECT i.* FROM shared_list_items i JOIN shared_lists l ON l.id=i.list_id WHERE i.id=? AND l.couple_id=? AND i.deleted_at IS NULL", (item_id, couple["id"])).fetchone()
            if not row:
                raise KeyError("心愿不存在")
            if row["item_kind"] == "gift" and row["creator_id"] != user_id:
                raise PermissionError("礼物心愿请使用私密认领入口")
            active_claim = db.execute("SELECT claimant_id FROM wishlist_claims WHERE item_id=? AND status='active'", (item_id,)).fetchone()
            if completed and active_claim and row["creator_id"] != user_id:
                raise PermissionError("请使用礼物认领的完成入口")
            completed_at = utc_now() if completed else None
            if completed and row["creator_id"] == user_id:
                self._cancel_active_wishlist_claim(
                    db, couple_id=str(couple["id"]), item_id=item_id,
                    actor_id=user_id, now=completed_at, reason="completed",
                )
            db.execute("UPDATE shared_list_items SET completed_at=?, version=version+1,updated_at=? WHERE id=?", (completed_at, utc_now(), item_id))
            self._audit(db, "wishlist.toggled", "shared_list_item", item_id, {"completed": completed}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "life.updated", item_id, {"resource": "wishlist", "completed": completed})
            updated = self._wishlist_item_for_user(db, db.execute("SELECT * FROM shared_list_items WHERE id=?", (item_id,)).fetchone(), user_id)
        return updated

    def create_wishlist_item(self, payload: dict, user_id: str = DEMO_USER) -> dict:
        text = str(payload.get("text", "")).strip()
        note = str(payload.get("note", "")).strip()
        item_kind = str(payload.get("item_kind", "general")).strip()
        if not text:
            raise ValueError("愿望内容不能为空")
        if len(text) > 300 or len(note) > 1000:
            raise ValueError("愿望内容过长")
        if item_kind not in {"general", "gift", "meal", "travel"}:
            raise ValueError("愿望类型无效")
        now = utc_now()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            previous = self._idempotent_result(db, couple["id"], user_id, "wishlist.create", payload.get("idempotency_key"))
            if previous:
                return previous
            list_row = db.execute("SELECT id FROM shared_lists WHERE couple_id=? AND type='wishlist' LIMIT 1", (couple["id"],)).fetchone()
            if not list_row:
                list_id = new_id("list")
                db.execute("INSERT INTO shared_lists(id,couple_id,type,title) VALUES (?,?,'wishlist','我们的心愿')", (list_id, couple["id"]))
            else:
                list_id = list_row["id"]
            position = db.execute("SELECT COALESCE(MAX(position),0)+1 AS value FROM shared_list_items WHERE list_id=?", (list_id,)).fetchone()["value"]
            item_id = new_id("wish")
            db.execute("INSERT INTO shared_list_items(id,list_id,creator_id,text,note,position,item_kind,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (item_id, list_id, user_id, text, note, position, item_kind, now, now))
            self._audit(db, "wishlist.created", "shared_list_item", item_id, actor_id=user_id)
            self._event(db, couple["id"], user_id, "life.updated", item_id, {"resource": "wishlist", "operation": "created"})
            row = db.execute("SELECT * FROM shared_list_items WHERE id=?", (item_id,)).fetchone()
            result = self._wishlist_item_for_user(db, row, user_id)
            self._save_idempotent_result(db, couple["id"], user_id, "wishlist.create", payload.get("idempotency_key"), result)
        return result

    def update_wishlist_item(self, item_id: str, payload: dict, user_id: str = DEMO_USER) -> dict:
        text = str(payload.get("text", "")).strip()
        note = str(payload.get("note", "")).strip()
        if not text:
            raise ValueError("愿望内容不能为空")
        if len(text) > 300 or len(note) > 1000:
            raise ValueError("愿望内容过长")
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute("SELECT i.* FROM shared_list_items i JOIN shared_lists l ON l.id=i.list_id WHERE i.id=? AND l.couple_id=? AND i.deleted_at IS NULL", (item_id, couple["id"])).fetchone()
            if not row:
                raise KeyError("心愿不存在")
            if row["item_kind"] == "gift" and row["creator_id"] != user_id:
                raise PermissionError("只有创建者可以编辑礼物心愿")
            if payload.get("expected_version") is not None and int(payload["expected_version"]) != int(row["version"]):
                raise ConflictError("心愿已被另一方修改，请刷新")
            db.execute("UPDATE shared_list_items SET text=?,note=?,version=version+1,updated_at=? WHERE id=?", (text, note, utc_now(), item_id))
            self._audit(db, "wishlist.updated", "shared_list_item", item_id, actor_id=user_id)
            self._event(db, couple["id"], user_id, "life.updated", item_id, {"resource": "wishlist", "operation": "updated"})
            updated = db.execute("SELECT * FROM shared_list_items WHERE id=?", (item_id,)).fetchone()
            return self._wishlist_item_for_user(db, updated, user_id)

    def delete_wishlist_item(self, item_id: str, expected_version: object, user_id: str = DEMO_USER) -> dict:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            row = db.execute("SELECT i.* FROM shared_list_items i JOIN shared_lists l ON l.id=i.list_id WHERE i.id=? AND l.couple_id=? AND i.deleted_at IS NULL", (item_id, couple["id"])).fetchone()
            if not row:
                raise KeyError("心愿不存在")
            if row["item_kind"] == "gift" and row["creator_id"] != user_id:
                raise PermissionError("只有创建者可以删除礼物心愿")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("心愿已被另一方修改，请刷新")
            now = utc_now()
            if row["creator_id"] == user_id:
                self._cancel_active_wishlist_claim(
                    db, couple_id=str(couple["id"]), item_id=item_id,
                    actor_id=user_id, now=now, reason="deleted",
                )
            db.execute("UPDATE shared_list_items SET deleted_at=?,version=version+1,updated_at=? WHERE id=?", (now, now, item_id))
            self._audit(db, "wishlist.deleted", "shared_list_item", item_id, actor_id=user_id)
            self._event(db, couple["id"], user_id, "life.updated", item_id, {"resource": "wishlist", "operation": "deleted"})
        return {"id": item_id, "deleted": True}

    def get_daily_question(self, user_id: str = DEMO_USER) -> dict:
        question_date = date.today().isoformat()
        with self.connection() as db:
            couple, partner_id = self._couple_context(db, user_id)
            rows = db.execute("SELECT * FROM daily_answers WHERE couple_id=? AND question_date=?", (couple["id"], question_date)).fetchall()
        answers = {row["user_id"]: row["answer"] for row in rows}
        question = rows[0]["question"] if rows else daily_question_for(couple["id"], question_date)
        return {
            "date": question_date,
            "question": question,
            "answered_by_me": user_id in answers,
            "partner_answered": partner_id in answers,
            "answers": answers if user_id in answers and partner_id in answers else {},
        }

    def answer_daily_question(self, answer: object, user_id: str = DEMO_USER) -> dict:
        if not isinstance(answer, str):
            raise ValueError("答案必须是文字")
        answer = answer.strip()
        if not answer:
            raise ValueError("答案不能为空")
        if len(answer) > 1000:
            raise ValueError("答案不能超过 1000 个字符")
        now = utc_now()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            existing = db.execute(
                "SELECT question FROM daily_answers WHERE couple_id=? AND question_date=? LIMIT 1",
                (couple["id"], date.today().isoformat()),
            ).fetchone()
            question = existing["question"] if existing else daily_question_for(couple["id"], date.today().isoformat())
            db.execute(
                "INSERT INTO daily_answers(id, couple_id, question_date, question, user_id, answer, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(couple_id, question_date, user_id) DO UPDATE SET answer=excluded.answer, created_at=excluded.created_at",
                (new_id("answer"), couple["id"], date.today().isoformat(), question, user_id, answer, now),
            )
            self._audit(db, "daily_question.answered", "daily_question", date.today().isoformat(), actor_id=user_id)
            self._event(db, couple["id"], user_id, "life.updated", date.today().isoformat(), {"resource": "daily_question"})
        return self.get_daily_question(user_id=user_id)

    def create_invite(self, user_id: str) -> dict:
        """Backward-compatible name without an implicit demo identity."""
        return self.create_user_invite(user_id)

    def stats(self) -> dict:
        # Health is public, so never expose relationship activity or table sizes.
        return {"database": "sqlite", "schema_version": SCHEMA_VERSION, "media_storage": self.storage.name}

    # ---- Controlled Intelligent UI -------------------------------------

    def _ui_context(self, db: sqlite3.Connection, surface: str, user_id: str, resource_refs: list[str] | None = None, current_trip_id: str | None = None) -> tuple[dict, sqlite3.Row]:
        if surface not in SURFACES:
            raise ValueError("不支持的智能界面")
        couple, partner_id = self._couple_context(db, user_id)
        trip = None
        if surface == "trip" and current_trip_id:
            trip = db.execute(
                "SELECT id,title,destination,start_date,end_date,budget_cents,status,active_variant,current_revision,version FROM trips WHERE id=? AND couple_id=? AND deleted_at IS NULL",
                (current_trip_id, couple["id"]),
            ).fetchone()
            if not trip:
                raise KeyError("当前旅行不存在或已经删除")
        if not trip:
            trip = db.execute("SELECT id, title, destination, start_date, end_date, budget_cents, status, active_variant, current_revision, version FROM trips WHERE couple_id=? AND deleted_at IS NULL ORDER BY start_date DESC, created_at DESC, rowid DESC LIMIT 1", (couple["id"],)).fetchone()
        message_count = db.execute("SELECT COUNT(*) AS n FROM messages WHERE couple_id=?", (couple["id"],)).fetchone()["n"]
        shared_moment_count = db.execute("SELECT COUNT(*) AS n FROM moments WHERE couple_id=? AND visibility='shared' AND deleted_at IS NULL", (couple["id"],)).fetchone()["n"]
        wish = db.execute("SELECT COUNT(*) AS total, SUM(CASE WHEN i.completed_at IS NULL THEN 1 ELSE 0 END) AS pending FROM shared_list_items i JOIN shared_lists l ON l.id=i.list_id WHERE l.couple_id=? AND i.deleted_at IS NULL", (couple["id"],)).fetchone()
        calendar = db.execute("SELECT COUNT(*) AS total, SUM(CASE WHEN entry_type='anniversary' THEN 1 ELSE 0 END) AS anniversaries, SUM(CASE WHEN entry_type='todo' AND completed_at IS NULL THEN 1 ELSE 0 END) AS pending_todos FROM calendar_entries WHERE couple_id=? AND deleted_at IS NULL", (couple["id"],)).fetchone()
        preferences = db.execute("SELECT COUNT(*) AS n FROM partner_preferences WHERE couple_id=? AND deleted_at IS NULL", (couple["id"],)).fetchone()["n"]
        meal_counts = db.execute("SELECT COUNT(*) AS total, SUM(CASE WHEN status='selected' THEN 1 ELSE 0 END) AS pending FROM meal_cart_items WHERE couple_id=? AND status!='cancelled'", (couple["id"],)).fetchone()
        meal_wishes = db.execute("SELECT COUNT(*) AS n FROM meal_wishes WHERE couple_id=? AND deleted_at IS NULL", (couple["id"],)).fetchone()["n"]
        shopping_pending = db.execute("SELECT COUNT(*) AS n FROM shopping_list_items i JOIN shopping_lists l ON l.id=i.shopping_list_id WHERE l.couple_id=? AND l.status='active' AND i.checked=0", (couple["id"],)).fetchone()["n"]
        context: dict[str, Any] = {
            "surface": surface,
            "couple": {"id": couple["id"], "version": couple["version"], "anniversary": couple["anniversary"]},
            "counts": {"messages": message_count, "sharedMoments": shared_moment_count, "wishes": wish["total"] or 0, "pendingWishes": wish["pending"] or 0, "calendar": calendar["total"] or 0, "anniversaries": calendar["anniversaries"] or 0, "pendingTodos": calendar["pending_todos"] or 0, "preferences": preferences, "meals": meal_counts["total"] or 0, "pendingMeals": meal_counts["pending"] or 0, "mealWishes": meal_wishes or 0, "shoppingPending": shopping_pending or 0},
            "trip": dict(trip) if trip else None,
            "authorizedContent": [],
        }
        refs = resource_refs or []
        if not isinstance(refs, list) or len(refs) > 10:
            raise ValueError("一次最多授权 10 条内容")
        expiry = (datetime.now(timezone.utc) + timedelta(minutes=15)).replace(microsecond=0).isoformat()
        grant_request_id = new_id("grant_request")
        for ref in refs:
            if not isinstance(ref, str) or not re.fullmatch(r"(?:message|moment):[A-Za-z0-9_-]{1,120}", ref):
                raise ValueError("授权资源格式无效")
            kind, resource_id = ref.split(":", 1)
            if kind == "message":
                row = db.execute("SELECT id, type, content, sent_at FROM messages WHERE id=? AND couple_id=?", (resource_id, couple["id"])).fetchone()
                content = {"ref": ref, "type": row["type"], "text": row["content"], "date": row["sent_at"]} if row else None
                resource_version = 1 if row else None
            else:
                row = db.execute("SELECT id, creator_id, type, title, body, occurred_at, visibility,version FROM moments WHERE id=? AND couple_id=? AND deleted_at IS NULL", (resource_id, couple["id"])).fetchone()
                if row and row["visibility"] != "shared" and row["creator_id"] != user_id:
                    row = None
                content = {"ref": ref, "type": row["type"], "title": row["title"], "text": row["body"], "date": row["occurred_at"]} if row else None
                resource_version = int(row["version"]) if row else None
            if not content:
                raise PermissionError("无权授权该内容")
            context["authorizedContent"].append(content)
            db.execute(
                """
                INSERT INTO ai_context_grants(
                    id,couple_id,user_id,surface,resource_type,resource_id,purpose,
                    expires_at,created_at,resource_version,usage_scope,request_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    new_id("grant"), couple["id"], user_id, surface, kind, resource_id,
                    "ui.compose", expiry, utc_now(), resource_version,
                    "selected_content", grant_request_id,
                ),
            )
        if refs:
            context["grantRequestId"] = grant_request_id
        return context, couple

    def _consume_ai_context_grants(
        self, request_id: str, user_id: str, expected_count: int
    ) -> None:
        """Recheck selected content immediately before an external dispatch."""
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            rows = db.execute(
                """
                SELECT * FROM ai_context_grants
                WHERE request_id=? AND couple_id=? AND user_id=?
                  AND purpose='ui.compose' AND usage_scope='selected_content'
                  AND revoked_at IS NULL AND expires_at>?
                ORDER BY id
                """,
                (request_id, couple["id"], user_id, utc_now()),
            ).fetchall()
            if len(rows) != expected_count:
                raise ConflictError("内容授权已经过期，请重新选择")
            for grant in rows:
                if grant["resource_type"] == "message":
                    current = db.execute(
                        "SELECT 1 AS version FROM messages WHERE id=? AND couple_id=?",
                        (grant["resource_id"], couple["id"]),
                    ).fetchone()
                elif grant["resource_type"] == "moment":
                    current = db.execute(
                        """
                        SELECT version FROM moments
                        WHERE id=? AND couple_id=? AND deleted_at IS NULL
                          AND (visibility='shared' OR creator_id=?)
                        """,
                        (grant["resource_id"], couple["id"], user_id),
                    ).fetchone()
                else:
                    current = None
                if not current or int(current["version"]) != int(grant["resource_version"] or 0):
                    raise ConflictError("授权内容已经变化，请重新选择")
            db.execute(
                "UPDATE ai_context_grants SET last_used_at=?,version=version+1 WHERE request_id=? AND user_id=? AND last_used_at IS NULL",
                (utc_now(), request_id, user_id),
            )

    @staticmethod
    def _context_identity(context: dict) -> tuple[int, str]:
        encoded = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        return int(digest[:12], 16), digest

    def _new_ui_draft(self, db: sqlite3.Connection, couple_id: str, user_id: str, action_type: str, payload: dict, preview: dict, expected: dict) -> str:
        draft_id = new_id("ui_action")
        expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(microsecond=0).isoformat()
        db.execute("DELETE FROM ui_action_drafts WHERE status IN ('draft', 'cancelled') AND expires_at<=?", (utc_now(),))
        db.execute("INSERT INTO ui_action_drafts(id, couple_id, creator_id, action_type, payload_json, preview_json, expected_versions_json, status, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)", (draft_id, couple_id, user_id, action_type, json.dumps(payload, ensure_ascii=False), json.dumps(preview, ensure_ascii=False), json.dumps(expected, ensure_ascii=False), expires, utc_now()))
        return draft_id

    @staticmethod
    def _personal_ui_overlay(db: sqlite3.Connection, surface: str, couple_id: str, user_id: str, authorized_count: int = 0) -> list[dict]:
        private_count = db.execute("SELECT COUNT(*) AS n FROM moments WHERE couple_id=? AND creator_id=? AND visibility!='shared' AND deleted_at IS NULL", (couple_id, user_id)).fetchone()["n"]
        if authorized_count:
            title, subtitle = "本次个人授权", f"仅本次使用 {authorized_count} 条你主动选择的内容"
        else:
            title, subtitle = "仅我的内容", f"{private_count} 条私密瞬间未参与共享编排"
        return [{"id": f"{surface}:personal", "type": "privacy_status", "resourceRef": None, "variant": "quiet", "props": {"title": title, "subtitle": subtitle}, "actions": [{"id": f"navigate:{surface}:privacy_status", "type": "navigate", "requiresConfirmation": False, "expectedVersions": {}}]}]

    @staticmethod
    def _calendar_intent(intent: str) -> dict[str, Any] | None:
        """Turn an explicit anniversary creation request into a safe draft payload.

        A date is deliberately required. If it is missing, the UI opens the real
        anniversary form instead of pretending that anything was saved.
        """
        text = intent.strip()
        if "纪念日" not in text or not re.search(r"(?:添加|新增|创建|记录|保存|安排)", text):
            return None
        match = re.search(r"(?P<year>20\d{2})\s*(?:[-/.年])\s*(?P<month>\d{1,2})\s*(?:[-/.月])\s*(?P<day>\d{1,2})\s*日?", text)
        if match:
            year, month, day = (int(match.group(name)) for name in ("year", "month", "day"))
        else:
            match = re.search(r"(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日?", text)
            if not match:
                return None
            year, month, day = date.today().year, int(match.group("month")), int(match.group("day"))
        try:
            starts_at = date(year, month, day).isoformat()
        except ValueError:
            return None
        title = text
        title = title.replace(match.group(0), " ")
        title = re.sub(r"(?:请|帮我|给我们|我们|一个|添加|新增|创建|记录|保存|安排|纪念日)", " ", title)
        title = re.sub(r"[，,。.!！：:]", " ", title)
        title = re.sub(r"\s+", " ", title).strip() or "我们的纪念日"
        return {"entry_type": "anniversary", "title": title[:160], "starts_at": starts_at, "all_day": True, "recurrence": "yearly", "location": "", "note": "由智能整理生成，确认后保存到共同生活。"}

    @staticmethod
    def _moment_intent(intent: str) -> dict[str, Any] | None:
        text = intent.strip()
        if not re.search(r"(?:保存|记录|创建|添加)", text) or not re.search(r"(?:瞬间|回忆|时光)", text):
            return None
        body = re.sub(r"^(?:请|帮我|给我们|我们)*\s*(?:保存|记录|创建|添加)(?:为|一个|一条)*\s*(?:恋爱)?(?:瞬间|回忆|时光)\s*[：:,，-]*", "", text).strip()
        if not body or body == text:
            return None
        return {"title": body[:60], "body": body[:1000], "visibility": "shared", "type": "text"}

    @staticmethod
    def _wishlist_intent(intent: str) -> dict[str, Any] | None:
        text = intent.strip()
        if "愿望" not in text or not re.search(r"(?:添加|新增|创建|记录|保存)", text):
            return None
        wish = re.sub(r"^(?:请|帮我|给我们|我们)*\s*(?:添加|新增|创建|记录|保存)(?:一个|一条|到|进)*\s*(?:共同)?愿望(?:清单)?\s*[：:,，-]*", "", text).strip()
        if not wish or wish == text:
            return None
        return {"text": wish[:300], "note": "由智能整理创建，已由用户确认。"}

    @staticmethod
    def _trip_variant_intent(intent: str, trip: dict) -> dict[str, Any] | None:
        if not trip or not re.search(r"(?:采用|选择|切换|换成|使用)", intent):
            return None
        keywords = (("slow", ("松弛", "浪漫", "慢游")), ("classic", ("经典", "充实")), ("value", ("性价比", "省钱", "预算")), ("food", ("美食", "寻味")), ("nature", ("自然", "山野", "治愈")))
        style = next((value for value, words in keywords if any(word in intent for word in words)), None)
        return {"trip_id": trip["id"], "style": style} if style else None

    @staticmethod
    def _trip_destination_intent(intent: str) -> str | None:
        match = re.search(r"(?:想去|要去|计划去|准备去)\s*([^，。,.!！\s]{2,40}?)(?:玩|旅行|旅游|度假|看看|$)", intent.strip())
        return match.group(1).strip() if match else None

    def _hydrate_ui(self, db: sqlite3.Connection, surface: str, components: list[str], context: dict, couple: sqlite3.Row, user_id: str, generated_by: str) -> dict:
        trip = context.get("trip") or {}
        counts = context["counts"]
        labels = {
            "couple_overview": ("我们的共同小岛", "把今天也收进恋爱时间线"), "anniversary": ("纪念日", f"{counts['anniversaries']} 条自定义纪念日 · 恋爱起点 {couple['anniversary']}"),
            "trip_countdown": (trip.get("title", "下一次旅行"), trip.get("start_date", "尚未创建")), "pending_items": ("待确认事项", f"{counts['pendingWishes']} 个愿望 · {counts['pendingTodos']} 个待办"),
            "daily_question": ("今日默契问题", "双方回答后互相解锁"), "quick_capture": ("记录这一刻", "照片、文字、语音或位置"),
            "message_summary": ("最近的消息", f"已安全保存 {counts['messages']} 条"), "trip_share": ("旅行灵感", trip.get("destination", "一起发现目的地")),
            "favorite_memory": ("收藏成瞬间", "选择聊天后可按次授权整理"), "suggested_reply": ("回复建议", "选择一条消息并授权后，才会根据真实上下文生成"),
            "import_guide": ("导入外部聊天", "本地识别、逐条确认后才保存"), "trip_variant_compare": ("五套旅行方案", "松弛 · 经典 · 性价比 · 美食 · 自然"),
            "itinerary_timeline": ("我们的行程", f"当前方案：{trip.get('active_variant') or '待选择'}"), "budget_summary": ("旅行预算", f"¥ {int(trip.get('budget_cents', 0)) / 100:,.0f}"),
            "hotel_quote": ("酒店与报价", "未配置真实报价时支持手动录入"), "risk_alert": ("出行提醒", "营业时间、预约与交通衔接会单独标记"),
            "impact_diff": ("变更影响", "人工改动不会被 AI 自动覆盖"), "approval_state": ("双方确认", f"当前版本 v{trip.get('current_revision', 1)}"),
            "featured_memory": ("精选回忆", f"共同瞬间 {counts['sharedMoments']} 条"), "timeline_summary": ("恋爱时间线", "照片、聊天、通话和旅行统一回顾"),
            "month_album": ("月份相册", "按月浏览共同照片"), "footprint_summary": ("足迹摘要", "地图服务未配置时显示地点列表"),
            "year_report": ("年度恋爱报告", "从共同数据生成，不读取私密正文"), "privacy_status": ("我的私密叠加层", "仅当前账号可见；登录自己的其他设备仍可查看"),
            "wishlist": ("共同愿望", f"{counts['pendingWishes']} 项还未完成"), "calendar_summary": ("共享日历", f"已保存 {counts['calendar']} 条共同事项"),
            "todo_summary": ("双人待办", f"{counts['pendingTodos']} 项尚未完成"), "preference_summary": ("对方偏好备忘", f"已保存 {counts['preferences']} 条偏好"),
            "recipe_library": ("中文菜谱库", "中餐与西餐都能查看完整食材和做法"), "recipe_detail": ("菜谱详情", "食材、步骤、用时与份量"),
            "meal_wishlist": ("想吃清单", f"{counts['mealWishes']} 道菜等你们安排"), "meal_cart": ("我们的点菜车", f"{counts['pendingMeals']} 道菜等待确认"),
            "meal_calendar": ("用餐日历", f"已安排 {counts['meals']} 道菜"), "meal_day_detail": ("当天三餐", "早餐、午餐和晚餐按时间排开"),
            "meal_archive": ("吃饭归档", "按月份回看哪天一起吃了什么"), "shopping_list": ("采购清单", f"{counts['shoppingPending']} 项食材还未购买"),
        }
        sections = []
        for position, component in enumerate(components):
            title, subtitle = labels[component]
            actions: list[dict] = [{"id": f"navigate:{surface}:{component}", "type": "navigate", "requiresConfirmation": False, "expectedVersions": {}}]
            sections.append({"id": f"{surface}:{component}:{position}", "type": component, "resourceRef": trip.get("id") if component.startswith(("trip_", "itinerary", "budget", "hotel", "risk", "impact", "approval")) and trip else None, "variant": "editorial", "props": {"title": title, "subtitle": subtitle}, "actions": actions})
        personal = self._personal_ui_overlay(db, surface, couple["id"], user_id, len(context.get("authorizedContent", [])))
        context_version, _ = self._context_identity({key: value for key, value in context.items() if key != "authorizedContent"})
        expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).replace(microsecond=0).isoformat()
        manifest = {"schemaVersion": "1.0", "surface": surface, "contextVersion": context_version, "sharedManifestId": f"shared:{couple['id']}:{surface}:{context_version}", "sections": sections, "personalOverlay": personal, "generatedBy": generated_by, "expiresAt": expires}
        return validate_manifest(manifest)

    def get_ui_manifest(self, surface: str, user_id: str = DEMO_USER, current_trip_id: str | None = None) -> dict:
        with self.connection() as db:
            context, couple = self._ui_context(db, surface, user_id, current_trip_id=current_trip_id)
            context_version, context_hash = self._context_identity(context)
            row = db.execute("SELECT manifest_json FROM ui_manifests WHERE couple_id=? AND user_id IS NULL AND surface=? AND context_hash=? AND expires_at>? ORDER BY created_at DESC LIMIT 1", (couple["id"], surface, context_hash, utc_now())).fetchone()
            if row:
                cached = validate_manifest(parse_json(row["manifest_json"], {}))
                ordering = [node["type"] for node in cached["sections"]]
                return self._hydrate_ui(db, surface, ordering, context, couple, user_id, "cache")
            components = list(COMPONENTS_BY_SURFACE[surface])
            manifest = self._hydrate_ui(db, surface, components, context, couple, user_id, "rules")
            manifest_id = new_id("manifest")
            stored = {**manifest, "sections": [{**node, "actions": []} for node in manifest["sections"]], "personalOverlay": []}
            db.execute("INSERT INTO ui_manifests(id, couple_id, user_id, surface, context_version, context_hash, manifest_json, source, expires_at, created_at) VALUES (?, ?, NULL, ?, ?, ?, ?, 'rules', ?, ?)", (manifest_id, couple["id"], surface, context_version, context_hash, json.dumps(stored, ensure_ascii=False), manifest["expiresAt"], utc_now()))
        return manifest

    def compose_ui(self, surface: str, intent: object, resource_refs: object = None, user_id: str = DEMO_USER, _job_id: str | None = None, current_trip_id: str | None = None) -> dict:
        if not isinstance(intent, str) or not intent.strip() or len(intent.strip()) > 500:
            raise ValueError("界面意图需为 1–500 个字符")
        refs = resource_refs if resource_refs is not None else []
        job_id = _job_id or new_id("ui_job")
        with self.connection() as db:
            context, couple = self._ui_context(db, surface, user_id, refs, current_trip_id=current_trip_id)
            couple_id = couple["id"]
            if _job_id:
                db.execute("UPDATE ui_generation_jobs SET status='running' WHERE id=? AND user_id=?", (job_id, user_id))
            else:
                db.execute("INSERT INTO ui_generation_jobs(id, couple_id, user_id, surface, intent_hash, status, created_at) VALUES (?, ?, ?, ?, ?, 'running', ?)", (job_id, couple["id"], user_id, surface, hashlib.sha256(intent.strip().encode()).hexdigest(), utc_now()))

        # Compose outside the database transaction. Remote composers first make
        # an atomic quota reservation on a separate connection; holding the job
        # transaction here would otherwise block that reservation until timeout.
        if refs:
            self._consume_ai_context_grants(
                str(context.get("grantRequestId") or ""), user_id, len(refs)
            )
        generated_by = "ai"
        error_code = None
        try:
            selected_components = validate_component_order(surface, self.ui_composer.compose(surface, context, intent.strip()))
            generated_by = getattr(self.ui_composer, "name", "ai")
        except Exception:
            selected_components = RuleBasedUiComposer().compose(surface, context, intent.strip())
            generated_by, error_code = "rules", "MODEL_FALLBACK"
        normalized_intent = intent.strip()
        calendar_payload = self._calendar_intent(normalized_intent) if surface == "life" else None
        wishlist_payload = self._wishlist_intent(normalized_intent) if surface == "life" else None
        moment_payload = self._moment_intent(normalized_intent) if surface in {"home", "timeline"} else None
        trip_payload = self._trip_variant_intent(normalized_intent, context.get("trip") or {}) if surface == "trip" else None
        destination_intent = self._trip_destination_intent(normalized_intent) if surface == "trip" else None

        with self.connection() as db:
            current_couple, _ = self._couple_context(db, user_id)
            if current_couple["id"] != couple_id:
                raise ConflictError("情侣空间已发生变化，请重新整理")
            couple = current_couple
            # A model may choose a compact component subset, but it must never
            # remove the visible target of a write draft. Otherwise the backend
            # would create a valid action that the user cannot inspect or confirm.
            if calendar_payload and "anniversary" not in selected_components:
                selected_components.insert(0, "anniversary")
            if wishlist_payload and "wishlist" not in selected_components:
                selected_components.insert(0, "wishlist")
            if moment_payload and "quick_capture" not in selected_components:
                selected_components.insert(0, "quick_capture")
            if trip_payload and "trip_variant_compare" not in selected_components:
                selected_components.insert(0, "trip_variant_compare")
            # Content-authorized requests are personal. Their model output cannot
            # reorder the couple-level sections seen by the other partner.
            components = list(COMPONENTS_BY_SURFACE[surface]) if refs else selected_components
            manifest = self._hydrate_ui(db, surface, components, context, couple, user_id, generated_by)
            if destination_intent:
                target = next((node for node in manifest["sections"] if node["type"] == "trip_variant_compare"), None)
                if target:
                    target["props"]["title"] = f"已识别目的地：{destination_intent}"
                    target["props"]["subtitle"] = "打开旅行创建页，继续填写日期、预算和双方偏好"
            if calendar_payload:
                draft = self._new_ui_draft(
                    db,
                    couple["id"],
                    user_id,
                    "create_calendar_entry",
                    calendar_payload,
                    {
                        "title": "保存纪念日",
                        "before": "共同生活中尚无这条记录",
                        "after": f"{calendar_payload['title']} · {calendar_payload['starts_at']} · 每年显示",
                        "impact": "确认后双方都能在共同生活的纪念日列表中查看和编辑",
                    },
                    {"couple": couple["version"]},
                )
                for node in manifest["sections"]:
                    if node["type"] == "anniversary":
                        node["actions"].append({"id": draft, "type": "create_calendar_entry", "requiresConfirmation": True, "expectedVersions": {"couple": couple["version"]}})
                        break
            intent_actions = []
            if wishlist_payload:
                intent_actions.append(("wishlist", "create_wishlist_item", wishlist_payload, {"title": "添加共同愿望", "before": "愿望清单中尚无此项", "after": wishlist_payload["text"], "impact": "确认后双方都能查看和编辑"}, {"couple": couple["version"]}))
            if moment_payload:
                intent_actions.append(("quick_capture", "create_moment", moment_payload, {"title": "保存共同瞬间", "before": "尚未保存", "after": moment_payload["title"], "impact": "确认后会进入双方可见的恋爱时间线"}, {"couple": couple["version"]}))
            if trip_payload:
                intent_actions.append(("trip_variant_compare", "select_trip_variant", trip_payload, {"title": "采用旅行方案", "before": (context.get("trip") or {}).get("active_variant"), "after": trip_payload["style"], "impact": "只切换当前采用方案，不覆盖锁定的人工行程"}, {"trip": (context.get("trip") or {}).get("version", 0)}))
            for component, action_type, payload, preview, expected in intent_actions:
                draft = self._new_ui_draft(db, couple["id"], user_id, action_type, payload, preview, expected)
                target = next((node for node in manifest["sections"] if node["type"] == component), None)
                if target:
                    target["actions"].append({"id": draft, "type": action_type, "requiresConfirmation": True, "expectedVersions": expected})
            context_version, context_hash = self._context_identity(context)
            result_manifest_id = new_id("manifest")
            if refs:
                db.execute("INSERT INTO ui_manifests(id, couple_id, user_id, surface, context_version, context_hash, manifest_json, source, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (result_manifest_id, couple["id"], user_id, surface, context_version, context_hash, json.dumps(manifest, ensure_ascii=False), generated_by, manifest["expiresAt"], utc_now()))
            else:
                shared_manifest_id = new_id("manifest")
                shared = {**manifest, "sections": [{**node, "actions": []} for node in manifest["sections"]], "personalOverlay": []}
                db.execute("INSERT INTO ui_manifests(id, couple_id, user_id, surface, context_version, context_hash, manifest_json, source, expires_at, created_at) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)", (shared_manifest_id, couple["id"], surface, context_version, context_hash, json.dumps(shared, ensure_ascii=False), generated_by, manifest["expiresAt"], utc_now()))
                # The result copy owns action drafts that only the requesting
                # user may preview or confirm. The couple-level copy above keeps
                # just the shared ordering for the partner.
                db.execute("INSERT INTO ui_manifests(id, couple_id, user_id, surface, context_version, context_hash, manifest_json, source, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (result_manifest_id, couple["id"], user_id, surface, context_version, context_hash, json.dumps(manifest, ensure_ascii=False), generated_by, manifest["expiresAt"], utc_now()))
            db.execute("UPDATE ui_generation_jobs SET status='completed', result_manifest_id=?, error_code=?, completed_at=? WHERE id=?", (result_manifest_id, error_code, utc_now(), job_id))
        return {"id": job_id, "status": "completed", "errorCode": error_code, "manifest": manifest}

    def start_ui_compose_job(self, surface: str, intent: object, resource_refs: object = None, user_id: str = DEMO_USER, current_trip_id: str | None = None) -> dict:
        if surface not in SURFACES:
            raise ValueError("不支持的智能界面")
        if not isinstance(intent, str) or not intent.strip() or len(intent.strip()) > 500:
            raise ValueError("界面意图需为 1–500 个字符")
        refs = resource_refs if resource_refs is not None else []
        if not isinstance(refs, list) or len(refs) > 10:
            raise ValueError("一次最多授权 10 条内容")
        job_id = new_id("ui_job")
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            db.execute("INSERT INTO ui_generation_jobs(id, couple_id, user_id, surface, intent_hash, status, created_at) VALUES (?, ?, ?, ?, ?, 'queued', ?)", (job_id, couple["id"], user_id, surface, hashlib.sha256(intent.strip().encode()).hexdigest(), utc_now()))

        def run() -> None:
            try:
                self.compose_ui(surface, intent, refs, user_id=user_id, _job_id=job_id, current_trip_id=current_trip_id)
            except Exception as error:
                with self.connection() as db:
                    db.execute("UPDATE ui_generation_jobs SET status='failed', error_code=?, completed_at=? WHERE id=?", (type(error).__name__.upper(), utc_now(), job_id))

        threading.Thread(target=run, name=f"ui-compose-{job_id[-8:]}", daemon=True).start()
        return {"id": job_id, "status": "queued", "errorCode": None, "manifest": None}

    def get_ui_job(self, job_id: str, user_id: str = DEMO_USER) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute("SELECT * FROM ui_generation_jobs WHERE id=? AND couple_id=? AND user_id=?", (job_id, couple["id"], user_id)).fetchone()
            if not row:
                raise KeyError("生成任务不存在")
            result = dict(row)
            result["manifest"] = None
            if row["result_manifest_id"]:
                manifest_row = db.execute("SELECT manifest_json, user_id FROM ui_manifests WHERE id=?", (row["result_manifest_id"],)).fetchone()
                if manifest_row:
                    stored = validate_manifest(parse_json(manifest_row["manifest_json"], {}))
                    if manifest_row["user_id"] is None:
                        context, couple = self._ui_context(db, row["surface"], user_id)
                        result["manifest"] = self._hydrate_ui(db, row["surface"], [node["type"] for node in stored["sections"]], context, couple, user_id, stored["generatedBy"])
                    else:
                        result["manifest"] = stored
            # Keep the asynchronous polling response consistent with the job
            # creation response used by both H5 and React Native clients.
            result["errorCode"] = result.get("error_code")
        return result

    def preview_ui_action(self, action_id: str, user_id: str = DEMO_USER) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute("SELECT * FROM ui_action_drafts WHERE id=? AND couple_id=? AND creator_id=?", (action_id, couple["id"], user_id)).fetchone()
            if not row:
                raise KeyError("动作草稿不存在")
            if row["status"] != "draft" or row["expires_at"] <= utc_now():
                raise ValueError("动作草稿已失效")
        return {"id": action_id, "type": row["action_type"], "preview": parse_json(row["preview_json"], {}), "expectedVersions": parse_json(row["expected_versions_json"], {}), "expiresAt": row["expires_at"]}

    def confirm_ui_action(self, action_id: str, idempotency_key: object, user_id: str = DEMO_USER) -> dict:
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{8,100}", idempotency_key):
            raise ValueError("幂等键格式无效")
        domain_key = f"ui:{action_id}:{hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]}"
        operation_by_action = {
            "send_message": "message.create",
            "create_moment": "moment.create",
            "select_trip_variant": "trip.variant.select",
            "create_calendar_entry": "calendar.create",
            "create_wishlist_item": "wishlist.create",
        }
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute("SELECT * FROM ui_action_drafts WHERE id=? AND couple_id=? AND creator_id=?", (action_id, couple["id"], user_id)).fetchone()
            if not row:
                raise KeyError("动作草稿不存在")
            if row["idempotency_key"] == idempotency_key and row["result_json"]:
                return {"ok": True, "idempotent": True, "result": parse_json(row["result_json"], {})}
            resuming = row["status"] == "executing" and row["idempotency_key"] == idempotency_key
            if row["status"] == "executing" and not resuming:
                raise ConflictError("这项操作正在使用另一请求执行")
            if row["status"] not in {"draft", "executing"} or row["expires_at"] <= utc_now():
                raise ValueError("动作草稿已失效")
            expected, payload, action_type = parse_json(row["expected_versions_json"], {}), parse_json(row["payload_json"], {}), row["action_type"]
            operation = operation_by_action.get(action_type, "")
            domain_receipt = db.execute(
                "SELECT result_json FROM mutation_idempotency WHERE couple_id=? AND user_id=? AND operation=? AND idempotency_key=?",
                (couple["id"], user_id, operation, domain_key),
            ).fetchone() if resuming and operation else None
            if not domain_receipt:
                if "couple" in expected and int(couple["version"]) != int(expected["couple"]):
                    raise ConflictError("共同空间已变化，请重新预览")
                if "trip" in expected:
                    trip = db.execute("SELECT version FROM trips WHERE id=? AND couple_id=?", (payload.get("trip_id"), couple["id"])).fetchone()
                    if not trip or int(trip["version"]) != int(expected["trip"]):
                        raise ConflictError("旅行方案已变化，请重新预览")
            if not resuming:
                try:
                    claimed = db.execute(
                        "UPDATE ui_action_drafts SET status='executing', idempotency_key=? WHERE id=? AND couple_id=? AND creator_id=? AND status='draft'",
                        (idempotency_key, action_id, couple["id"], user_id),
                    )
                except sqlite3.IntegrityError as error:
                    raise ConflictError("这个幂等键已用于另一项操作") from error
                if claimed.rowcount != 1:
                    raise ConflictError("动作状态已变化，请重新预览")
        try:
            if action_type == "send_message":
                result = self.create_message({"type": "text", "content": payload["content"], "idempotency_key": domain_key}, user_id=user_id)
            elif action_type == "create_moment":
                result = self.create_moment({**payload, "idempotency_key": domain_key}, user_id=user_id)
            elif action_type == "select_trip_variant":
                result = self.select_variant(
                    payload["trip_id"],
                    payload["style"],
                    user_id=user_id,
                    idempotency_key=domain_key,
                )
            elif action_type == "create_calendar_entry":
                result = self.create_calendar_entry({**payload, "idempotency_key": domain_key}, user_id=user_id)
            elif action_type == "create_wishlist_item":
                result = self.create_wishlist_item({**payload, "idempotency_key": domain_key}, user_id=user_id)
            else:
                raise ValueError("该动作尚未开放执行")
        except Exception:
            with self.connection() as db:
                # A domain mutation may already have committed before a process
                # interruption. Keep an existing executing claim recoverable;
                # ordinary validation/provider failures can safely return the
                # draft to the user because no idempotent receipt was written.
                receipt = db.execute(
                    "SELECT result_json FROM mutation_idempotency WHERE couple_id=? AND user_id=? AND operation=? AND idempotency_key=?",
                    (couple["id"], user_id, operation_by_action.get(action_type, ""), domain_key),
                ).fetchone()
                if not receipt:
                    db.execute("UPDATE ui_action_drafts SET status='draft', idempotency_key=NULL WHERE id=? AND couple_id=? AND creator_id=? AND status='executing' AND idempotency_key=?", (action_id, couple["id"], user_id, idempotency_key))
            raise
        with self.connection() as db:
            confirmed = db.execute(
                "UPDATE ui_action_drafts SET status='confirmed', result_json=?, confirmed_at=? WHERE id=? AND couple_id=? AND creator_id=? AND status='executing' AND idempotency_key=?",
                (json.dumps(result, ensure_ascii=False), utc_now(), action_id, couple["id"], user_id, idempotency_key),
            )
            if confirmed.rowcount != 1:
                current = db.execute("SELECT result_json,idempotency_key FROM ui_action_drafts WHERE id=? AND couple_id=? AND creator_id=?", (action_id, couple["id"], user_id)).fetchone()
                if current and current["idempotency_key"] == idempotency_key and current["result_json"]:
                    return {"ok": True, "idempotent": True, "result": parse_json(current["result_json"], {})}
                raise ConflictError("动作完成状态无法确认，请重新读取结果")
            self._audit(db, "ui.action_confirmed", "ui_action", action_id, {"type": action_type}, actor_id=user_id)
        return {"ok": True, "idempotent": False, "result": result}

    def cancel_ui_action(self, action_id: str, user_id: str = DEMO_USER) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            cursor = db.execute("UPDATE ui_action_drafts SET status='cancelled' WHERE id=? AND couple_id=? AND creator_id=? AND status='draft'", (action_id, couple["id"], user_id))
            if cursor.rowcount != 1:
                raise KeyError("可取消的动作草稿不存在")
            self._audit(db, "ui.action_cancelled", "ui_action", action_id, actor_id=user_id)
        return {"ok": True, "status": "cancelled"}
