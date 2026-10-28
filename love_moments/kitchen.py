"""Structured food profiles, simple pantry state and shopping categories.

The kitchen domain deliberately models only current state.  It never turns
human notes into a quantity ledger and never trusts provider allergen labels.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, timedelta, timezone, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from zoneinfo import ZoneInfo

from .intelligent_ui import ConflictError
from .recipe_catalog_quality import allergen_report, inferred_allergens
from .recipes import (
    INGREDIENT_ALIASES,
    MEAL_SLOTS,
    build_shopping_items,
    canonical_ingredient_name,
    format_quantity,
    normalize_name,
    recipe_snapshot,
    shopping_diff,
)


SHOPPING_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("produce", "蔬菜水果"),
    ("meat_eggs", "肉禽蛋"),
    ("seafood", "水产"),
    ("dairy", "乳品"),
    ("staples_oils", "粮油"),
    ("seasonings", "调味"),
    ("frozen", "冷冻"),
    ("household", "日用品"),
    ("other", "其他"),
)
CATEGORY_LABELS = dict(SHOPPING_CATEGORIES)
CATEGORY_BY_LABEL = {label: key for key, label in SHOPPING_CATEGORIES}
CATEGORY_KEYS = frozenset(CATEGORY_LABELS)
PANTRY_STATES = frozenset({"have", "low", "out"})
STORAGE_LOCATIONS = frozenset({"ambient", "chilled", "frozen"})
ALLERGEN_KEYS = frozenset({
    "蛋类", "乳制品", "含麸质谷物", "可能含麸质谷物", "甲壳类",
    "鱼类", "软体动物", "花生", "坚果", "芝麻", "大豆",
})
LEDGER_FIELDS = frozenset({
    "quantity", "quantity_milli", "unit", "batch", "batch_id", "reserved",
    "movement", "consumed_amount", "reservation", "movements",
})
FOOD_PROFILE_FIELDS = frozenset({"allergies", "avoidances", "preferences", "expected_version", "idempotency_key"})
PANTRY_UPSERT_FIELDS = frozenset({
    "ingredient_id", "name", "category", "state", "storage_location", "expires_on",
    "quantity_note", "expected_version", "idempotency_key",
})
PANTRY_UPDATE_FIELDS = frozenset({
    "state", "storage_location", "expires_on", "quantity_note", "expected_version", "idempotency_key",
})
SHOPPING_STOCK_FIELDS = frozenset({
    "expected_version", "confirm_food", "storage_location", "expires_on", "quantity_note", "idempotency_key",
})
CATEGORY_ORDER_FIELDS = frozenset({"categories", "expected_versions", "idempotency_key"})
WEEKLY_CREATE_FIELDS = frozenset({
    "schema_version", "starts_on", "ends_on", "meal_slots", "servings",
    "availability_mode", "idempotency_key",
})
WEEKLY_UPDATE_FIELDS = frozenset({
    "recipe_id", "date", "meal_slot", "planned_time", "swap_with_item_id",
    "expected_version", "idempotency_key",
})
WEEKLY_CONFIRM_FIELDS = frozenset({"expected_version", "idempotency_key"})
WEEKLY_REASON_LABELS = {
    "meal_wish": "来自双方点菜",
    "expires_soon": "可优先使用临期食材",
    "pantry_match": "可优先核对家中已有食材",
    "liked_tag": "符合已保存的双方口味偏好",
    "available_time": "适合当天可用时间",
}
SUBSTITUTION_PREVIEW_FIELDS = frozenset({
    "snapshot_ingredient_ref", "expected_meal_version",
    "expected_shopping_list_version", "idempotency_key",
})
SUBSTITUTION_APPLY_FIELDS = frozenset({
    "expected_meal_version", "expected_shopping_list_version",
    "expected_version", "idempotency_key",
})
SUBSTITUTION_DISMISS_FIELDS = frozenset({"expected_version", "idempotency_key"})
COOK_START_FIELDS = frozenset({
    "recipe_id", "meal_item_id", "visibility", "expected_meal_version", "idempotency_key",
})
COOK_STEP_FIELDS = frozenset({"step_index", "expected_version", "idempotency_key"})
COOK_TIMER_FIELDS = frozenset({"step_index", "duration_seconds", "label", "idempotency_key"})
COOK_TIMER_CANCEL_FIELDS = frozenset({"expected_version", "idempotency_key"})
COOK_END_FIELDS = frozenset({"expected_version", "idempotency_key"})
COOK_PANTRY_FIELDS = frozenset({"expected_version", "updates", "idempotency_key"})
COOK_PANTRY_UPDATE_FIELDS = frozenset({"ingredient_ref", "state", "expected_pantry_version"})


@dataclass(frozen=True)
class SubstitutionRule:
    key: str
    version: int
    from_names: tuple[str, ...]
    to_name: str
    ratio_numerator: int
    ratio_denominator: int
    compatible_unit_families: tuple[str, ...]
    suitable_methods: tuple[str, ...]
    flavor_note: str
    texture_note: str
    usage_note: str
    requires_package_review: bool = False


SUBSTITUTION_RULES: tuple[SubstitutionRule, ...] = (
    SubstitutionRule(
        "milk_to_unsweetened_soy_milk", 1, ("牛奶",), "无糖豆奶", 1, 1,
        ("volume",), ("热饮", "汤羹", "面糊", "普通烘焙"),
        "豆香更明显，乳脂香减少", "液体稠度接近，成品丰润度可能下降", "按原液体加入时机使用",
    ),
    SubstitutionRule(
        "milk_to_unsweetened_oat_milk", 1, ("牛奶",), "无糖燕麦奶", 1, 1,
        ("volume",), ("热饮", "汤羹", "面糊", "普通烘焙"),
        "谷物甜味更明显，乳脂香减少", "稠度接近，成品可能略清爽", "按原液体加入时机使用",
    ),
    SubstitutionRule(
        "cream_to_coconut_cream", 1, ("淡奶油",), "椰浆", 1, 1,
        ("volume",), ("汤羹", "咖喱", "炖", "炖煮酱汁"),
        "椰香明显，乳脂香减少", "冷却后的凝固性与淡奶油不同", "适合汤羹、咖喱和炖煮酱汁",
    ),
    SubstitutionRule(
        "butter_to_neutral_oil", 1, ("黄油",), "无味植物油", 4, 5,
        ("mass", "volume"), ("煎", "炒", "炖", "炖煮酱汁"),
        "奶香减少", "成品更流动，不形成黄油起酥结构", "不适用于起酥、曲奇塑形或裹入黄油面团",
    ),
    SubstitutionRule(
        "peanut_to_sunflower_seed", 1, ("花生米", "花生"), "烤葵花籽仁", 1, 1,
        ("mass",), ("凉拌", "撒料", "炒", "快炒收尾"),
        "坚果香减弱", "仍可保留脆感", "作为凉拌、撒料或快炒收尾使用",
    ),
    SubstitutionRule(
        "sesame_paste_to_peanut_butter", 1, ("芝麻酱",), "无糖花生酱", 1, 1,
        ("mass", "volume"), ("拌面", "蘸料", "凉拌", "凉拌酱"),
        "花生风味明显", "黏度接近", "先少量调开，再按原酱料加入",
    ),
    SubstitutionRule(
        "flour_to_cornstarch_for_thickening", 1, ("小麦面粉", "面粉"), "玉米淀粉", 1, 2,
        ("mass",), ("勾芡",),
        "味道更清", "芡汁更透明", "仅用于勾芡，不可替代面包、饼皮或蛋糕主体",
    ),
    SubstitutionRule(
        "soy_sauce_to_coconut_amino_sauce", 1, ("生抽",), "椰子氨基调味汁", 1, 1,
        ("volume",), ("炒", "拌", "腌"),
        "甜味更明显，咸度通常较低", "液体质地接近", "先减少其他糖和盐，再按口味补足",
        requires_package_review=True,
    ),
)
SUBSTITUTION_RULES_BY_KEY = {rule.key: rule for rule in SUBSTITUTION_RULES}


class KitchenConflictError(ConflictError):
    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = "VERSION_CONFLICT"
        self.details = details or {}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _clean_text(value: object, maximum: int, field: str, *, required: bool = False) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if required and not text:
        raise ValueError(f"{field}不能为空")
    if len(text) > maximum:
        raise ValueError(f"{field}不能超过 {maximum} 个字符")
    return text


def shopping_category_key(name: object, explicit: object = None) -> str:
    raw_explicit = str(explicit or "").strip()
    if raw_explicit in CATEGORY_KEYS:
        return raw_explicit
    if raw_explicit in CATEGORY_BY_LABEL:
        return CATEGORY_BY_LABEL[raw_explicit]
    text = canonical_ingredient_name(name) if str(name or "").strip() else ""
    if any(word in text for word in ("冷冻", "速冻")):
        return "frozen"
    if any(word in text for word in ("洗洁精", "纸巾", "厨房纸", "垃圾袋", "保鲜膜", "清洁剂", "洗衣", "牙膏", "肥皂")):
        return "household"
    if any(word in text for word in ("鱼", "虾", "蟹", "贝", "蛤", "鱿鱼", "海鲜", "鳝")):
        return "seafood"
    if any(word in text for word in ("牛奶", "羊奶", "奶油", "黄油", "芝士", "奶酪", "酸奶", "炼乳", "乳清")):
        return "dairy"
    if any(word in text for word in ("鸡蛋", "鸭蛋", "鹅蛋", "鹌鹑蛋", "蛋黄", "蛋清", "猪", "牛肉", "羊", "鸡肉", "鸭肉", "肉", "火腿", "培根")):
        return "meat_eggs"
    if any(word in text for word in ("植物油", "橄榄油", "食用油", "花生油", "菜籽油", "玉米油")):
        return "staples_oils"
    if any(word in text for word in ("盐", "糖", "酱", "醋", "料酒", "胡椒", "香料", "淀粉", "咖喱", "蜂蜜", "味精", "蚝油")):
        return "seasonings"
    if any(word in text for word in ("菜", "葱", "姜", "蒜", "番茄", "土豆", "萝卜", "蘑菇", "菌", "椒", "瓜", "茄", "笋", "豆芽", "水果", "苹果", "梨", "橙", "香蕉", "玉米")):
        return "produce"
    staple_grains = ("米", "大米", "小米", "糯米", "黑米", "糙米", "米饭", "糯米粉")
    if any(word in text for word in ("面粉", "面条", "意大利面", "燕麦", "面包", "吐司", "饺子皮", "馄饨皮")) or any(text == grain or text.endswith(grain) for grain in staple_grains):
        return "staples_oils"
    return "other"


def shopping_category_label(category_key: str) -> str:
    return CATEGORY_LABELS.get(category_key, CATEGORY_LABELS["other"])


def normalize_shopping_category(value: object, *, ingredient_name: object = "") -> str:
    text = str(value or "").strip()
    if not text:
        return shopping_category_key(ingredient_name)
    if text in CATEGORY_KEYS:
        return text
    if text in CATEGORY_BY_LABEL:
        return CATEGORY_BY_LABEL[text]
    return "other"


def evaluate_recipe_food_safety(
    recipe_snapshot: dict,
    profiles: list[dict],
    ingredient_catalog: dict[str, dict],
) -> dict:
    ingredients = recipe_snapshot.get("ingredients", [])
    if not isinstance(ingredients, list):
        ingredients = []
    rows = [row for row in ingredients if isinstance(row, dict)]
    names = {str(row.get("name") or row.get("canonical_name") or "").strip() for row in rows}
    names.discard("")
    evidence = allergen_report(names)
    actual_allergens = {item["name"]: item.get("sources", []) for item in evidence["allergens"]}
    severe_conflicts: list[dict] = []
    avoid_conflicts: list[dict] = []
    ingredient_avoidances: list[dict] = []
    severe_profile_exists = False
    avoided_ids: dict[str, list[str]] = {}
    for profile in profiles:
        user_id = str(profile.get("user_id") or "")
        for allergy in profile.get("allergies", []):
            key = allergy.get("allergen_key")
            severity = allergy.get("severity")
            if severity == "severe":
                severe_profile_exists = True
            if key not in actual_allergens:
                continue
            conflict = {"user_id": user_id, "allergen_key": key, "sources": actual_allergens[key]}
            (severe_conflicts if severity == "severe" else avoid_conflicts).append(conflict)
        for avoidance in profile.get("avoidances", []):
            ingredient_id = str(avoidance.get("ingredient_id") or "")
            if ingredient_id:
                avoided_ids.setdefault(ingredient_id, []).append(user_id)
    for row in rows:
        ingredient_id = str(row.get("ingredient_id") or row.get("id") or "")
        canonical = canonical_ingredient_name(row.get("canonical_name") or row.get("name") or "") if (row.get("canonical_name") or row.get("name")) else ""
        candidate_ids = {ingredient_id} if ingredient_id else set()
        for catalog_id, catalog_row in ingredient_catalog.items():
            if canonical and canonical_ingredient_name(catalog_row.get("canonical_name") or "") == canonical:
                candidate_ids.add(catalog_id)
        users = sorted({user for candidate_id in candidate_ids for user in avoided_ids.get(candidate_id, [])})
        if users:
            ingredient_avoidances.append({"ingredient_id": ingredient_id or None, "canonical_name": canonical, "user_ids": users})
    review_ingredients = list(evidence["review_ingredients"])
    if severe_conflicts:
        status = "blocked"
    elif avoid_conflicts or ingredient_avoidances or (severe_profile_exists and review_ingredients):
        status = "needs_review"
    else:
        status = "safe"
    return {
        "status": status,
        "severe_conflicts": severe_conflicts,
        "avoid_conflicts": avoid_conflicts,
        "ingredient_avoidances": ingredient_avoidances,
        "review_ingredients": review_ingredients,
        "note": "仅按当前配料表与已保存饮食资料核对；未识别到不代表医学上的安全。",
    }


class KitchenMixin:
    @staticmethod
    def _kitchen_fields(payload: dict, allowed: frozenset[str], label: str) -> None:
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"{label}包含不支持的字段：{sorted(unknown)[0]}")

    def _kitchen_request(self, payload: dict, operation: str, *, resource_id: str | None = None) -> tuple[str, str]:
        key = str(payload.get("idempotency_key") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9:._-]{8,160}", key):
            raise ValueError("幂等键格式无效")
        safe_payload = {name: value for name, value in payload.items() if name != "idempotency_key"}
        fingerprint = hashlib.sha256(_json({
            "operation": operation,
            "resource_id": resource_id,
            "payload": safe_payload,
        }).encode("utf-8")).hexdigest()
        return key, fingerprint

    @staticmethod
    def _kitchen_replay(db: sqlite3.Connection, couple_id: str, user_id: str, operation: str, key: str, fingerprint: str) -> dict | None:
        row = db.execute(
            "SELECT result_json FROM mutation_idempotency WHERE couple_id=? AND user_id=? AND operation=? AND idempotency_key=?",
            (couple_id, user_id, operation, key),
        ).fetchone()
        if not row:
            return None
        stored = json.loads(row["result_json"])
        if stored.get("request_hash") != fingerprint:
            raise ConflictError("幂等键已用于另一项操作")
        return stored["response"]

    @staticmethod
    def _kitchen_save(db: sqlite3.Connection, couple_id: str, user_id: str, operation: str, key: str, fingerprint: str, response: dict) -> None:
        db.execute(
            "INSERT INTO mutation_idempotency(couple_id,user_id,operation,idempotency_key,result_json,created_at) VALUES (?,?,?,?,?,?)",
            (couple_id, user_id, operation, key, _json({"request_hash": fingerprint, "response": response}), _now()),
        )

    def _resolve_ingredient_in_connection(
        self,
        db: sqlite3.Connection,
        *,
        couple_id: str,
        name: str,
        explicit_category: str | None = None,
        source_type: str = "human",
    ) -> sqlite3.Row:
        canonical = canonical_ingredient_name(name)
        normalized = normalize_name(canonical)
        row = db.execute(
            "SELECT * FROM ingredient_catalog WHERE couple_id IS NULL AND normalized_name=?",
            (normalized,),
        ).fetchone()
        if not row:
            row = db.execute(
                "SELECT * FROM ingredient_catalog WHERE couple_id=? AND normalized_name=?",
                (couple_id, normalized),
            ).fetchone()
        if row:
            return row
        if source_type not in {"human", "real_api", "rules", "ai", "mock", "cache", "legacy_unknown"}:
            raise ValueError("食材来源无效")
        category = shopping_category_key(canonical, explicit_category)
        now = _now()
        ingredient_id = _id("ingredient")
        db.execute(
            """
            INSERT INTO ingredient_catalog(id,couple_id,canonical_name,normalized_name,aliases_json,allergens_json,category,source_type,created_at,updated_at)
            VALUES (?,?,?,?,'[]',?,?,?,?,?)
            """,
            (ingredient_id, couple_id, canonical, normalized, _json(inferred_allergens({canonical})), category, source_type, now, now),
        )
        return db.execute("SELECT * FROM ingredient_catalog WHERE id=?", (ingredient_id,)).fetchone()

    @staticmethod
    def _ingredient_by_id(db: sqlite3.Connection, couple_id: str, ingredient_id: object) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM ingredient_catalog WHERE id=? AND (couple_id IS NULL OR couple_id=?)",
            (str(ingredient_id or ""), couple_id),
        ).fetchone()
        if not row:
            raise PermissionError("食材不存在或不可访问")
        return row

    def _food_profiles_in_connection(self, db: sqlite3.Connection, couple_id: str) -> list[dict]:
        couple = db.execute("SELECT * FROM couples WHERE id=?", (couple_id,)).fetchone()
        if not couple:
            return []
        result: list[dict] = []
        for user_id in (couple["member_a"], couple["member_b"]):
            user = db.execute("SELECT nickname FROM users WHERE id=?", (user_id,)).fetchone()
            row = db.execute("SELECT * FROM food_profiles WHERE couple_id=? AND user_id=?", (couple_id, user_id)).fetchone()
            allergies = json.loads(row["allergies_json"]) if row else []
            stored_avoidances = json.loads(row["avoidances_json"]) if row else []
            avoidances = []
            for item in stored_avoidances:
                ingredient = self._ingredient_by_id(db, couple_id, item.get("ingredient_id"))
                avoidances.append({
                    "ingredient_id": ingredient["id"],
                    "canonical_name": ingredient["canonical_name"],
                    "category_key": normalize_shopping_category(ingredient["category"], ingredient_name=ingredient["canonical_name"]),
                    "category_label": shopping_category_label(normalize_shopping_category(ingredient["category"], ingredient_name=ingredient["canonical_name"])),
                })
            result.append({
                "user_id": user_id,
                "nickname": user["nickname"] if user else "伴侣",
                "allergies": allergies,
                "avoidances": avoidances,
                "preferences": json.loads(row["preferences_json"]) if row else {"liked_tags": [], "disliked_tags": []},
                "version": int(row["version"]) if row else 0,
                "updated_at": row["updated_at"] if row else None,
            })
        return result

    def get_food_profiles(self, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            return {"profiles": self._food_profiles_in_connection(db, couple["id"])}

    def update_my_food_profile(self, payload: dict, user_id: str) -> dict:
        if "subject_user_id" in payload:
            raise PermissionError("只能修改自己的饮食资料")
        self._kitchen_fields(payload, FOOD_PROFILE_FIELDS, "饮食资料")
        allergies = payload.get("allergies", [])
        avoidances = payload.get("avoidances", [])
        preferences = payload.get("preferences", {})
        if not isinstance(allergies, list) or len(allergies) > 30:
            raise ValueError("过敏项最多 30 条")
        normalized_allergies: list[dict] = []
        seen_allergens: set[str] = set()
        for item in allergies:
            if not isinstance(item, dict) or set(item) != {"allergen_key", "severity"} or item.get("allergen_key") not in ALLERGEN_KEYS:
                raise ValueError("过敏原类别无效")
            if item.get("severity") not in {"avoid", "severe"}:
                raise ValueError("过敏严重程度无效")
            key_name = str(item["allergen_key"])
            if key_name in seen_allergens:
                raise ValueError("同一过敏原不能重复")
            seen_allergens.add(key_name)
            normalized_allergies.append({"allergen_key": key_name, "severity": item["severity"]})
        normalized_allergies.sort(key=lambda item: item["allergen_key"])
        if not isinstance(avoidances, list) or len(avoidances) > 100:
            raise ValueError("忌口食材最多 100 条")
        if not isinstance(preferences, dict) or set(preferences) - {"liked_tags", "disliked_tags"}:
            raise ValueError("口味偏好字段无效")
        normalized_preferences: dict[str, list[str]] = {}
        for field in ("liked_tags", "disliked_tags"):
            values = preferences.get(field, [])
            if not isinstance(values, list) or len(values) > 30:
                raise ValueError("口味标签最多 30 条")
            if any(not isinstance(value, str) for value in values):
                raise ValueError("口味标签必须是文字")
            cleaned = [_clean_text(value, 40, "口味标签", required=True) for value in values]
            if len(set(cleaned)) != len(cleaned):
                raise ValueError("口味标签不能重复")
            normalized_preferences[field] = sorted(cleaned)
        key, fingerprint = self._kitchen_request(payload, "food_profile.update")
        expected = payload.get("expected_version")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            raise ValueError("饮食资料版本无效")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            replay = self._kitchen_replay(db, couple["id"], user_id, "food_profile.update", key, fingerprint)
            if replay is not None:
                return replay
            normalized_avoidances: list[dict] = []
            seen_ingredients: set[str] = set()
            for item in avoidances:
                if not isinstance(item, dict) or set(item) != {"ingredient_id"}:
                    raise ValueError("忌口食材格式无效")
                ingredient = self._ingredient_by_id(db, couple["id"], item.get("ingredient_id"))
                if ingredient["id"] in seen_ingredients:
                    raise ValueError("忌口食材不能重复")
                seen_ingredients.add(ingredient["id"])
                normalized_avoidances.append({"ingredient_id": ingredient["id"]})
            normalized_avoidances.sort(key=lambda item: item["ingredient_id"])
            row = db.execute("SELECT * FROM food_profiles WHERE couple_id=? AND user_id=?", (couple["id"], user_id)).fetchone()
            current_version = int(row["version"]) if row else 0
            if current_version != expected:
                raise KitchenConflictError("饮食资料已被更新，请刷新后再试", details={"current_version": current_version})
            serialized = (_json(normalized_allergies), _json(normalized_avoidances), _json(normalized_preferences))
            now = _now()
            changed = not row or (row["allergies_json"], row["avoidances_json"], row["preferences_json"]) != serialized
            if row and not changed:
                pass
            elif row:
                db.execute(
                    "UPDATE food_profiles SET allergies_json=?,avoidances_json=?,preferences_json=?,version=version+1,updated_at=? WHERE couple_id=? AND user_id=? AND version=?",
                    (*serialized, now, couple["id"], user_id, expected),
                )
            else:
                db.execute(
                    "INSERT INTO food_profiles(couple_id,user_id,allergies_json,avoidances_json,preferences_json,version,updated_at) VALUES (?,?,?,?,?,1,?)",
                    (couple["id"], user_id, *serialized, now),
                )
            result = next(item for item in self._food_profiles_in_connection(db, couple["id"]) if item["user_id"] == user_id)
            self._kitchen_save(db, couple["id"], user_id, "food_profile.update", key, fingerprint, result)
            if changed:
                self._audit(db, "food_profile.updated", "food_profile", user_id, {"version": result["version"]}, actor_id=user_id)
                self._event(db, couple["id"], user_id, "kitchen.updated", user_id, {"resource": "food_profile", "operation": "updated", "version": result["version"]})
            return result
    def _ingredient_catalog_snapshot(self, db: sqlite3.Connection, couple_id: str) -> dict[str, dict]:
        return {row["id"]: dict(row) for row in db.execute("SELECT * FROM ingredient_catalog WHERE couple_id IS NULL OR couple_id=?", (couple_id,)).fetchall()}

    def _evaluate_recipe_food_safety_in_connection(self, db: sqlite3.Connection, *, couple_id: str, recipe_snapshot: dict) -> dict:
        return evaluate_recipe_food_safety(
            recipe_snapshot,
            self._food_profiles_in_connection(db, couple_id),
            self._ingredient_catalog_snapshot(db, couple_id),
        )

    def evaluate_recipe_food_safety(self, recipe_snapshot: dict, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            return self._evaluate_recipe_food_safety_in_connection(db, couple_id=couple["id"], recipe_snapshot=recipe_snapshot)

    def _pantry_item_dict(self, row: sqlite3.Row, ingredient: sqlite3.Row, *, changed: bool | None = None) -> dict:
        value = dict(row)
        try:
            stored_aliases = json.loads(ingredient["aliases_json"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            stored_aliases = []
        canonical_name = str(ingredient["canonical_name"])
        aliases = {
            str(alias).strip()
            for alias in stored_aliases
            if isinstance(alias, str) and str(alias).strip()
        }
        aliases.update(alias for alias, canonical in INGREDIENT_ALIASES.items() if canonical == canonical_name)
        aliases.discard(canonical_name)
        category_key = normalize_shopping_category(ingredient["category"], ingredient_name=ingredient["canonical_name"])
        value.update({
            "ingredient_id": ingredient["id"], "canonical_name": canonical_name,
            "aliases": sorted(aliases),
            "category_key": category_key, "category_label": shopping_category_label(category_key),
        })
        today = date.today()
        expires = date.fromisoformat(value["expires_on"]) if value.get("expires_on") else None
        value["is_expired"] = bool(expires and expires < today and value["state"] != "out")
        value["expires_soon"] = bool(expires and today <= expires <= today + timedelta(days=3) and value["state"] != "out")
        if changed is not None:
            value["changed"] = changed
        return value

    def _category_order_in_connection(self, db: sqlite3.Connection, couple_id: str) -> list[str]:
        rows = db.execute("SELECT category FROM shopping_category_preferences WHERE couple_id=? ORDER BY position,category", (couple_id,)).fetchall()
        return [row["category"] for row in rows] if len(rows) == len(SHOPPING_CATEGORIES) else [key for key, _ in SHOPPING_CATEGORIES]

    def _pantry_snapshot_in_connection(self, db: sqlite3.Connection, couple_id: str) -> list[dict]:
        order = {key: index for index, key in enumerate(self._category_order_in_connection(db, couple_id))}
        items = []
        for row in db.execute("SELECT * FROM pantry_items WHERE couple_id=?", (couple_id,)).fetchall():
            ingredient = self._ingredient_by_id(db, couple_id, row["ingredient_id"])
            items.append(self._pantry_item_dict(row, ingredient))
        state_order = {"have": 0, "low": 1, "out": 2}
        items.sort(key=lambda item: (
            state_order[item["state"]], item["expires_on"] is None, item["expires_on"] or "9999-12-31",
            order.get(item["category_key"], 99), item["canonical_name"],
        ))
        return items

    def list_pantry_items(self, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            return {"items": self._pantry_snapshot_in_connection(db, couple["id"])}

    @staticmethod
    def _pantry_metadata(payload: dict, current: sqlite3.Row | None = None) -> tuple[str | None, str | None, str]:
        storage = payload.get("storage_location", current["storage_location"] if current else None)
        if storage in ("​", ""):
            storage = None
        if storage is not None and storage not in STORAGE_LOCATIONS:
            raise ValueError("存放位置无效")
        expires = payload.get("expires_on", current["expires_on"] if current else None)
        if expires in ("​", ""):
            expires = None
        if expires is not None:
            try:
                expires = date.fromisoformat(str(expires)).isoformat()
            except ValueError as error:
                raise ValueError("到期日必须是有效日期") from error
        note = _clean_text(payload.get("quantity_note", current["quantity_note"] if current else ""), 100, "数量备注")
        return storage, expires, note

    def _set_pantry_state_in_connection(
        self,
        db: sqlite3.Connection,
        *,
        couple_id: str,
        actor_id: str,
        ingredient_id: str,
        state: str,
        metadata: dict,
        expected_version: int | None,
    ) -> tuple[dict, bool]:
        if state not in PANTRY_STATES:
            raise ValueError("库存状态只能是有、少或无")
        ingredient = self._ingredient_by_id(db, couple_id, ingredient_id)
        row = db.execute("SELECT * FROM pantry_items WHERE couple_id=? AND ingredient_id=?", (couple_id, ingredient_id)).fetchone()
        current_version = int(row["version"]) if row else 0
        storage, expires, note = self._pantry_metadata(metadata, row)
        if row and (row["state"], row["storage_location"], row["expires_on"], row["quantity_note"]) == (state, storage, expires, note):
            return self._pantry_item_dict(row, ingredient, changed=False), False
        if expected_version is None or int(expected_version) != current_version:
            raise KitchenConflictError("库存已被另一方修改，请刷新", details={"current_version": current_version})
        now = _now()
        if row:
            updated = db.execute(
                """
                UPDATE pantry_items SET state=?,storage_location=?,expires_on=?,quantity_note=?,updated_by=?,version=version+1,updated_at=?
                WHERE id=? AND couple_id=? AND version=?
                """,
                (state, storage, expires, note, actor_id, now, row["id"], couple_id, current_version),
            ).rowcount
            if updated != 1:
                raise KitchenConflictError("库存已被另一方修改，请刷新")
            pantry_id = row["id"]
        else:
            pantry_id = _id("pantry")
            db.execute(
                """
                INSERT INTO pantry_items(id,couple_id,ingredient_id,updated_by,state,storage_location,expires_on,quantity_note,version,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,1,?,?)
                """,
                (pantry_id, couple_id, ingredient_id, actor_id, state, storage, expires, note, now, now),
            )
        current = db.execute("SELECT * FROM pantry_items WHERE id=?", (pantry_id,)).fetchone()
        return self._pantry_item_dict(current, ingredient, changed=True), True

    def upsert_pantry_item(self, payload: dict, user_id: str) -> dict:
        self._kitchen_fields(payload, PANTRY_UPSERT_FIELDS, "简化库存")
        if bool(payload.get("ingredient_id")) == bool(str(payload.get("name") or "").strip()):
            raise ValueError("请提供食材 ID 或名称中的一项")
        key, fingerprint = self._kitchen_request(payload, "pantry.upsert")
        state = str(payload.get("state") or "")
        expected = payload.get("expected_version")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            raise ValueError("库存版本无效")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            replay = self._kitchen_replay(db, couple["id"], user_id, "pantry.upsert", key, fingerprint)
            if replay is not None:
                return replay
            ingredient = self._ingredient_by_id(db, couple["id"], payload["ingredient_id"]) if payload.get("ingredient_id") else self._resolve_ingredient_in_connection(
                db, couple_id=couple["id"], name=str(payload["name"]), explicit_category=payload.get("category"), source_type="human",
            )
            result, changed = self._set_pantry_state_in_connection(
                db, couple_id=couple["id"], actor_id=user_id, ingredient_id=ingredient["id"],
                state=state, metadata=payload, expected_version=expected,
            )
            self._kitchen_save(db, couple["id"], user_id, "pantry.upsert", key, fingerprint, result)
            if changed:
                self._audit(db, "pantry.updated", "pantry_item", result["id"], {"version": result["version"]}, actor_id=user_id)
                self._event(db, couple["id"], user_id, "kitchen.updated", result["id"], {"resource": "pantry_item", "operation": "updated", "version": result["version"]})
            return result

    def update_pantry_item(self, item_id: str, payload: dict, user_id: str) -> dict:
        self._kitchen_fields(payload, PANTRY_UPDATE_FIELDS, "简化库存")
        if set(payload).intersection({"ingredient_id", "name", "category"}):
            raise ValueError("库存食材身份不可修改，请新建食材")
        key, fingerprint = self._kitchen_request(payload, "pantry.update", resource_id=item_id)
        expected = payload.get("expected_version")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
            raise ValueError("库存版本无效")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            replay = self._kitchen_replay(db, couple["id"], user_id, "pantry.update", key, fingerprint)
            if replay is not None:
                return replay
            row = db.execute("SELECT * FROM pantry_items WHERE id=? AND couple_id=?", (item_id, couple["id"])).fetchone()
            if not row:
                raise KeyError("库存食材不存在")
            old_state = row["state"]
            result, changed = self._set_pantry_state_in_connection(
                db, couple_id=couple["id"], actor_id=user_id, ingredient_id=row["ingredient_id"],
                state=str(payload.get("state", row["state"])), metadata=payload, expected_version=expected,
            )
            if changed and result["state"] != old_state:
                result["undo"] = self.create_mutation_receipt(
                    db, actor_id=user_id, resource_type="pantry_item", resource_id=item_id,
                    operation="pantry.set_state", inverse={"state": old_state},
                    expected_post_version=result["version"], client_request_id=key,
                )
            self._kitchen_save(db, couple["id"], user_id, "pantry.update", key, fingerprint, result)
            if changed:
                self._audit(db, "pantry.updated", "pantry_item", item_id, {"version": result["version"]}, actor_id=user_id)
                self._event(db, couple["id"], user_id, "kitchen.updated", item_id, {"resource": "pantry_item", "operation": "updated", "version": result["version"]})
            return result

    def get_shopping_category_order(self, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            stored = {row["category"]: row for row in db.execute("SELECT * FROM shopping_category_preferences WHERE couple_id=?", (couple["id"],)).fetchall()}
            order = self._category_order_in_connection(db, couple["id"])
            return {"categories": [
                {"key": key, "label": shopping_category_label(key), "position": position, "version": int(stored[key]["version"]) if key in stored else 0}
                for position, key in enumerate(order)
            ]}

    def update_shopping_category_order(self, payload: dict, user_id: str) -> dict:
        self._kitchen_fields(payload, CATEGORY_ORDER_FIELDS, "采购分类顺序")
        categories = payload.get("categories")
        versions = payload.get("expected_versions")
        if not isinstance(categories, list) or len(categories) != len(SHOPPING_CATEGORIES) or set(categories) != CATEGORY_KEYS:
            raise ValueError("分类顺序必须完整包含九个分类且不可重复")
        if not isinstance(versions, dict) or set(versions) != CATEGORY_KEYS or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in versions.values()):
            raise ValueError("分类版本快照无效")
        key, fingerprint = self._kitchen_request(payload, "shopping.category_order")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            replay = self._kitchen_replay(db, couple["id"], user_id, "shopping.category_order", key, fingerprint)
            if replay is not None:
                return replay
            stored = {row["category"]: row for row in db.execute("SELECT * FROM shopping_category_preferences WHERE couple_id=?", (couple["id"],)).fetchall()}
            current_versions = {category: int(stored[category]["version"]) if category in stored else 0 for category in CATEGORY_KEYS}
            if current_versions != versions:
                raise KitchenConflictError("分类顺序已被另一方修改，请刷新", details={"current_versions": current_versions})
            current_order = self._category_order_in_connection(db, couple["id"])
            if current_order != categories:
                now = _now()
                for position, category in enumerate(categories):
                    if category in stored:
                        db.execute("UPDATE shopping_category_preferences SET position=?,version=version+1,updated_at=? WHERE couple_id=? AND category=? AND version=?", (position, now, couple["id"], category, current_versions[category]))
                    else:
                        db.execute("INSERT INTO shopping_category_preferences(couple_id,category,position,version,updated_at) VALUES (?,?,?,1,?)", (couple["id"], category, position, now))
            result = self.get_shopping_category_order_in_connection(db, couple["id"])
            self._kitchen_save(db, couple["id"], user_id, "shopping.category_order", key, fingerprint, result)
            if current_order != categories:
                self._audit(db, "shopping.category_order.updated", "shopping_category_order", couple["id"], {"changed_count": 9}, actor_id=user_id)
                self._event(db, couple["id"], user_id, "kitchen.updated", couple["id"], {"resource": "shopping_category_order", "operation": "updated", "changed_count": 9})
            return result

    def get_shopping_category_order_in_connection(self, db: sqlite3.Connection, couple_id: str) -> dict:
        stored = {row["category"]: row for row in db.execute("SELECT * FROM shopping_category_preferences WHERE couple_id=?", (couple_id,)).fetchall()}
        order = self._category_order_in_connection(db, couple_id)
        return {"categories": [
            {"key": key, "label": shopping_category_label(key), "position": position, "version": int(stored[key]["version"]) if key in stored else 0}
            for position, key in enumerate(order)
        ]}

    def _stock_shopping_item_in_connection(
        self,
        db: sqlite3.Connection,
        *,
        couple_id: str,
        actor_id: str,
        shopping_item_id: str,
        payload: dict,
    ) -> dict:
        item = db.execute(
            """
            SELECT i.*,l.status AS list_status,l.completed_at
            FROM shopping_list_items i JOIN shopping_lists l ON l.id=i.shopping_list_id
            WHERE i.id=? AND l.couple_id=? AND l.status IN ('active','completed')
            """,
            (shopping_item_id, couple_id),
        ).fetchone()
        if not item:
            raise KeyError("采购项目不存在")
        if item["list_status"] == "completed":
            completed = datetime.fromisoformat(str(item["completed_at"] or "").replace("Z", "+00:00"))
            if completed < datetime.now(timezone.utc) - timedelta(days=30):
                raise ConflictError("这份采购清单已经归档较久，不能再转入库存")
        expected = payload.get("expected_version")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected != int(item["version"]):
            raise KitchenConflictError("采购项目已被另一方修改，请刷新", details={"current_version": int(item["version"])})
        if not bool(item["checked"]):
            raise ConflictError("只有已勾选购买的食材才能记为家里有")
        category_key = normalize_shopping_category(item["category"], ingredient_name=item["canonical_name"])
        if category_key == "household":
            raise ConflictError("日用品不能加入食材库存")
        if category_key == "other" and payload.get("confirm_food") is not True:
            raise ConflictError("“其他”分类需要先明确确认这是食品")
        ingredient = self._resolve_ingredient_in_connection(
            db, couple_id=couple_id, name=str(item["canonical_name"] or item["display_name"]),
            explicit_category=category_key, source_type="human",
        )
        pantry = db.execute("SELECT * FROM pantry_items WHERE couple_id=? AND ingredient_id=?", (couple_id, ingredient["id"])).fetchone()
        pantry_item, changed = self._set_pantry_state_in_connection(
            db, couple_id=couple_id, actor_id=actor_id, ingredient_id=ingredient["id"], state="have",
            metadata=payload, expected_version=int(pantry["version"]) if pantry else 0,
        )
        return {"shopping_item_id": shopping_item_id, "changed": changed, "pantry_item": pantry_item}

    def stock_shopping_item(self, item_id: str, payload: dict, user_id: str) -> dict:
        self._kitchen_fields(payload, SHOPPING_STOCK_FIELDS, "采购入库")
        key, fingerprint = self._kitchen_request(payload, "shopping.stock", resource_id=item_id)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            replay = self._kitchen_replay(db, couple["id"], user_id, "shopping.stock", key, fingerprint)
            if replay is not None:
                return replay
            result = self._stock_shopping_item_in_connection(
                db, couple_id=couple["id"], actor_id=user_id, shopping_item_id=item_id, payload=payload,
            )
            self._kitchen_save(db, couple["id"], user_id, "shopping.stock", key, fingerprint, result)
            if result["changed"]:
                pantry = result["pantry_item"]
                self._audit(db, "shopping_item.stocked", "pantry_item", pantry["id"], {"version": pantry["version"]}, actor_id=user_id)
                self._event(db, couple["id"], user_id, "kitchen.updated", pantry["id"], {"resource": "pantry_item", "operation": "stocked", "version": pantry["version"]})
            return result

    # ---- Deterministic weekly meal plans -----------------------------

    @staticmethod
    def _weekly_hash(value: object) -> str:
        return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _weekly_request(payload: dict, base_operation: str, resource_id: str = "") -> tuple[str, str, str]:
        key = str(payload.get("idempotency_key") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9:._-]{8,160}", key):
            raise ValueError("幂等键格式无效")
        request_hash = hashlib.sha256(_json({
            "operation": base_operation,
            "resource_id": resource_id,
            "payload": {name: value for name, value in payload.items() if name != "idempotency_key"},
        }).encode("utf-8")).hexdigest()
        return key, request_hash, f"{base_operation}:{request_hash[:16]}"

    @staticmethod
    def _weekly_replay(
        db: sqlite3.Connection,
        *,
        couple_id: str,
        user_id: str,
        base_operation: str,
        operation: str,
        idempotency_key: str,
    ) -> dict | None:
        rows = db.execute(
            """
            SELECT operation,result_json FROM mutation_idempotency
            WHERE couple_id=? AND user_id=? AND idempotency_key=? AND operation LIKE ?
            """,
            (couple_id, user_id, idempotency_key, f"{base_operation}:%"),
        ).fetchall()
        if not rows:
            return None
        for row in rows:
            if row["operation"] == operation:
                return json.loads(row["result_json"])["response"]
        raise ConflictError("幂等键已用于另一项周菜单操作")

    @staticmethod
    def _weekly_save(
        db: sqlite3.Connection,
        *,
        couple_id: str,
        user_id: str,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        response: dict,
    ) -> None:
        db.execute(
            """
            INSERT INTO mutation_idempotency(couple_id,user_id,operation,idempotency_key,result_json,created_at)
            VALUES (?,?,?,?,?,?)
            """,
            (couple_id, user_id, operation, idempotency_key,
             _json({"request_hash": request_hash, "response": response}), _now()),
        )

    @staticmethod
    def _weekly_parse_time(value: object, field: str = "计划时间") -> str:
        text = str(value or "").strip()
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
            raise ValueError(f"{field}必须是 HH:MM")
        return text

    @staticmethod
    def _weekly_meal_sort_key(item: dict) -> tuple[str, str, str, str]:
        return (
            str(item.get("date") or ""),
            str(item.get("planned_time") or ""),
            str(item.get("meal_slot") or ""),
            str(item.get("id") or ""),
        )

    def _weekly_expected_versions(
        self,
        db: sqlite3.Connection,
        *,
        couple: sqlite3.Row,
        starts_on: str,
        ends_on: str,
        recipe_ids: list[str],
    ) -> dict:
        couple_id = str(couple["id"])
        profiles = {
            row["user_id"]: int(row["version"])
            for row in db.execute("SELECT user_id,version FROM food_profiles WHERE couple_id=? ORDER BY user_id", (couple_id,)).fetchall()
        }
        pantry = [dict(row) for row in db.execute(
            "SELECT id,ingredient_id,state,expires_on,version FROM pantry_items WHERE couple_id=? ORDER BY id", (couple_id,)
        ).fetchall()]
        wishes = [dict(row) for row in db.execute(
            "SELECT id,recipe_id,version FROM meal_wishes WHERE couple_id=? AND deleted_at IS NULL ORDER BY id", (couple_id,)
        ).fetchall()]
        calendar_rows: list[dict] = []
        for row in db.execute(
            """
            SELECT id,starts_at,ends_at,all_day,recurrence,participant_a_id,participant_b_id,version
            FROM calendar_entries
            WHERE couple_id=? AND deleted_at IS NULL AND blocks_time=1
              AND (participant_a_id IN (?,?) OR participant_b_id IN (?,?))
            ORDER BY id
            """,
            (couple_id, couple["member_a"], couple["member_b"], couple["member_a"], couple["member_b"]),
        ).fetchall():
            current = date.fromisoformat(starts_on)
            final = date.fromisoformat(ends_on)
            occurs = False
            while current <= final and not occurs:
                occurs = bool(self._weekly_calendar_occurrences(row, current))
                current += timedelta(days=1)
            if occurs:
                calendar_rows.append(dict(row))
        cart = db.execute("SELECT version FROM meal_carts WHERE couple_id=? AND status='active'", (couple_id,)).fetchone()
        shopping = db.execute("SELECT version FROM shopping_lists WHERE couple_id=? AND status='active'", (couple_id,)).fetchone()
        versions: dict[str, Any] = {
            "couple": int(couple["version"]),
            f"profile:{couple['member_a']}": profiles.get(couple["member_a"], 0),
            f"profile:{couple['member_b']}": profiles.get(couple["member_b"], 0),
            "pantry_snapshot": self._weekly_hash(pantry),
            "meal_wishes_snapshot": self._weekly_hash(wishes),
            "busy_calendar_snapshot": self._weekly_hash(calendar_rows),
            "meal_cart": int(cart["version"]) if cart else 0,
            "shopping_list": int(shopping["version"]) if shopping else 0,
        }
        if recipe_ids:
            placeholders = ",".join("?" for _ in recipe_ids)
            rows = db.execute(
                f"SELECT id,version FROM recipes WHERE id IN ({placeholders}) ORDER BY id", recipe_ids,
            ).fetchall()
            versions.update({f"recipe:{row['id']}": int(row["version"]) for row in rows})
        return versions

    def _weekly_require_fresh(self, db: sqlite3.Connection, couple: sqlite3.Row, row: sqlite3.Row) -> None:
        payload = json.loads(row["payload_json"])
        recipe_ids = sorted({str(item["recipe_id"]) for item in payload.get("meals", []) if item.get("recipe_id")})
        current = self._weekly_expected_versions(
            db, couple=couple, starts_on=row["starts_on"], ends_on=row["ends_on"], recipe_ids=recipe_ids,
        )
        expected = json.loads(row["expected_versions_json"])
        if current != expected:
            raise KitchenConflictError("饮食设置、库存、点菜或日历已经变化，请重新生成周菜单")

    @staticmethod
    def _weekly_is_expired(row: sqlite3.Row) -> bool:
        return datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00")) <= datetime.now(timezone.utc)

    def _weekly_row(self, db: sqlite3.Connection, draft_id: str, couple_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM weekly_meal_plan_drafts WHERE id=? AND couple_id=?", (draft_id, couple_id)).fetchone()
        if not row:
            raise KeyError("周菜单草稿不存在")
        if self._weekly_is_expired(row):
            raise ConflictError("周菜单草稿已经过期，请重新生成")
        return row

    @staticmethod
    def _weekly_draft_dict(row: sqlite3.Row) -> dict:
        payload = json.loads(row["payload_json"])
        preview = json.loads(row["preview_json"])
        result = json.loads(row["result_json"]) if row["result_json"] else None
        items = sorted(preview.get("items", []), key=KitchenMixin._weekly_meal_sort_key)
        return {
            "id": row["id"], "starts_on": row["starts_on"], "ends_on": row["ends_on"],
            "status": row["status"], "source_type": row["source_type"], "version": int(row["version"]),
            "expires_at": row["expires_at"], "items": items,
            "shopping_preview": preview.get("shopping_preview", {}),
            "unresolved_wish_ids": preview.get("unresolved_wish_ids", []),
            "warnings": preview.get("warnings", []),
            "confirmations": {
                "member_a_confirmed": bool(row["confirmed_a_id"]),
                "member_b_confirmed": bool(row["confirmed_b_id"]),
                "both_confirmed": bool(row["confirmed_a_id"] and row["confirmed_b_id"]),
            },
            "result": result,
            "availability_mode": payload.get("availability_mode", "either_partner"),
        }

    def _weekly_recipe_catalog(self, db: sqlite3.Connection, couple_id: str) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for row in db.execute(
            """
            SELECT * FROM recipes WHERE deleted_at IS NULL AND
              (visibility='system' OR (couple_id=? AND visibility='couple'))
            ORDER BY id
            """,
            (couple_id,),
        ).fetchall():
            recipe = self._recipe_row_to_dict(db, row)
            snapshot = recipe_snapshot(recipe)
            snapshot["version"] = int(row["version"])
            snapshot["active_minutes"] = int(recipe.get("active_minutes", 0))
            snapshot["passive_minutes"] = int(recipe.get("passive_minutes", 0))
            result[str(row["id"])] = snapshot
        return result

    @staticmethod
    def _weekly_cooking_method_key(recipe: dict) -> str:
        """Return a real cooking-method key, not a generic variant label.

        Older built-in recipes used ``经典做法`` as both the variant name and
        cooking method.  Treating that migration default as an actual method
        would make every third meal a placeholder, even though the recipes are
        otherwise different.  New/custom recipes with a concrete method still
        participate in the two-per-week diversity cap.
        """
        value = str(recipe.get("cooking_method") or "").strip().casefold()
        variant = str(recipe.get("variant_name") or "").strip().casefold()
        generic = {"", "经典做法", "家常做法", "默认做法", "classic", "default"}
        if value in generic or (value == variant and value in generic):
            return ""
        return value

    @staticmethod
    def _weekly_main_ingredient_key(recipe: dict) -> str:
        main = next(
            (ingredient for ingredient in recipe.get("ingredients", []) if ingredient.get("ingredient_group") == "main"),
            None,
        )
        if not main:
            return ""
        return canonical_ingredient_name(main.get("canonical_name") or main.get("name") or "")

    def _weekly_validate_diversity(self, meals: list[dict], catalog: dict[str, dict]) -> None:
        dishes: dict[str, int] = {}
        methods: dict[str, int] = {}
        mains: dict[str, int] = {}
        for item in sorted(meals, key=self._weekly_meal_sort_key):
            if not item.get("recipe_id"):
                continue
            recipe = catalog.get(str(item["recipe_id"]))
            if not recipe:
                continue
            dish = str(recipe.get("dish_id") or recipe["id"])
            method = self._weekly_cooking_method_key(recipe)
            main = self._weekly_main_ingredient_key(recipe)
            dishes[dish] = dishes.get(dish, 0) + 1
            if method:
                methods[method] = methods.get(method, 0) + 1
            if main:
                mains[main] = mains.get(main, 0) + 1
        if any(count > 1 for count in dishes.values()):
            raise ConflictError("同一道菜一周只能安排一次")
        if any(count > 2 for count in methods.values()):
            raise ConflictError("同一种烹饪方式一周最多安排两次")
        if any(count > 2 for count in mains.values()):
            raise ConflictError("同一种主要食材一周最多安排两次")

    @staticmethod
    def _weekly_interval(value: object, *, end: bool = False) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return parsed.astimezone(ZoneInfo("Asia/Shanghai"))

    def _weekly_calendar_occurrences(
        self,
        row: sqlite3.Row,
        day_value: date,
    ) -> list[tuple[datetime, datetime]]:
        """Expand one calendar row with the same recurrence rules as date plans."""
        zone = ZoneInfo("Asia/Shanghai")
        day_start = datetime.combine(day_value, datetime.min.time(), tzinfo=zone)
        day_end = day_start + timedelta(days=1)
        recurrence = str(row["recurrence"] or "none")
        intervals: list[tuple[datetime, datetime]] = []
        if bool(row["all_day"]):
            try:
                base_start = date.fromisoformat(str(row["starts_at"])[:10])
                base_end = date.fromisoformat(str(row["ends_at"])[:10]) if row["ends_at"] else base_start + timedelta(days=1)
            except ValueError:
                return []
            if base_end <= base_start:
                base_end = base_start + timedelta(days=1)
            duration = base_end - base_start
            for occurrence_date in self._recurrence_dates(base_start, day_value, recurrence):
                occurrence_start = datetime.combine(occurrence_date, datetime.min.time(), tzinfo=zone)
                occurrence_end = occurrence_start + duration
                if occurrence_start < day_end and occurrence_end > day_start:
                    intervals.append((occurrence_start, occurrence_end))
            return intervals
        base_start_dt = self._weekly_interval(row["starts_at"])
        base_end_dt = self._weekly_interval(row["ends_at"] or row["starts_at"])
        if not base_start_dt or not base_end_dt or base_end_dt <= base_start_dt:
            return []
        duration = base_end_dt - base_start_dt
        for occurrence_date in self._recurrence_dates(base_start_dt.date(), day_value, recurrence):
            occurrence_start = datetime(
                occurrence_date.year, occurrence_date.month, occurrence_date.day,
                base_start_dt.hour, base_start_dt.minute, base_start_dt.second,
                tzinfo=zone, fold=base_start_dt.fold,
            )
            occurrence_end = occurrence_start + duration
            if occurrence_start < day_end and occurrence_end > day_start:
                intervals.append((occurrence_start, occurrence_end))
        return intervals

    def _weekly_available(
        self,
        db: sqlite3.Connection,
        *,
        couple: sqlite3.Row,
        day: str,
        planned_time: str,
        active_minutes: int,
        max_active_minutes: int,
        availability_mode: str,
    ) -> bool:
        end_at = datetime.fromisoformat(f"{day}T{planned_time}:00").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        start_at = end_at - timedelta(minutes=max_active_minutes)
        members = (str(couple["member_a"]), str(couple["member_b"]))
        busy: dict[str, list[tuple[datetime, datetime]]] = {member: [] for member in members}
        rows = db.execute(
            """
            SELECT starts_at,ends_at,all_day,recurrence,participant_a_id,participant_b_id
            FROM calendar_entries WHERE couple_id=? AND deleted_at IS NULL AND blocks_time=1
            """,
            (couple["id"],),
        ).fetchall()
        for row in rows:
            participants = {str(value) for value in (row["participant_a_id"], row["participant_b_id"]) if value}
            if not participants:
                continue
            for blocker_start, blocker_end in self._weekly_calendar_occurrences(row, date.fromisoformat(day)):
                if blocker_start >= end_at or blocker_end <= start_at:
                    continue
                for user_id in participants:
                    if user_id in busy:
                        busy[user_id].append((max(start_at, blocker_start), min(end_at, blocker_end)))

        def maximum_gap(intervals: list[tuple[datetime, datetime]]) -> float:
            cursor = start_at
            largest = 0.0
            for block_start, block_end in sorted(intervals):
                if block_end <= cursor:
                    continue
                largest = max(largest, (max(cursor, block_start) - cursor).total_seconds() / 60)
                cursor = max(cursor, block_end)
                if cursor >= end_at:
                    break
            return max(largest, (end_at - cursor).total_seconds() / 60)

        if availability_mode == "both_partners":
            # Both people must be free at the same time, so a blocker for either
            # partner closes that part of the common interval.
            return maximum_gap(busy[members[0]] + busy[members[1]]) >= active_minutes
        return any(maximum_gap(busy[member]) >= active_minutes for member in members)

    def _weekly_build_preview(
        self,
        db: sqlite3.Connection,
        *,
        couple: sqlite3.Row,
        payload: dict,
        meals: list[dict],
        unresolved_wish_ids: list[str],
        catalog: dict[str, dict],
    ) -> dict:
        pantry = self._pantry_snapshot_in_connection(db, couple["id"])
        pantry_names = {
            canonical_ingredient_name(item["canonical_name"]): item
            for item in pantry if item["state"] in {"have", "low"}
        }
        public_items: list[dict] = []
        shopping_meals: list[dict] = []
        warnings: list[str] = []
        for item in sorted(meals, key=self._weekly_meal_sort_key):
            recipe = catalog.get(str(item.get("recipe_id"))) if item.get("recipe_id") else None
            reason_codes = list(item.get("reason_codes", []))
            likely: list[str] = []
            if recipe:
                for ingredient in recipe.get("ingredients", []):
                    name = canonical_ingredient_name(ingredient.get("canonical_name") or ingredient.get("name") or "")
                    pantry_item = pantry_names.get(name)
                    if pantry_item and (
                        not pantry_item.get("expires_on") or str(pantry_item["expires_on"]) >= str(item["date"])
                    ):
                        likely.append(name)
                shopping_meals.append({
                    "id": item["id"], "couple_id": couple["id"], "planned_date": item["date"],
                    "servings": item["servings"], "recipe_snapshot": recipe,
                })
            public_items.append({
                **item,
                "recipe": ({
                    "id": recipe["id"], "name": recipe["name"],
                    "active_minutes": recipe.get("active_minutes", 0),
                    "passive_minutes": recipe.get("passive_minutes", 0),
                } if recipe else None),
                "reason_labels": [WEEKLY_REASON_LABELS[code] for code in reason_codes if code in WEEKLY_REASON_LABELS],
                "availability_label": "两个人都有足够动手时间" if payload["availability_mode"] == "both_partners" else "至少一人有足够动手时间",
                "likely_on_hand": sorted(set(likely)),
            })
        required = build_shopping_items(shopping_meals) if shopping_meals else []
        active = db.execute("SELECT * FROM shopping_lists WHERE couple_id=? AND status='active'", (couple["id"],)).fetchone()
        existing = [dict(row) for row in db.execute(
            "SELECT * FROM shopping_list_items WHERE shopping_list_id=?", (active["id"],)
        ).fetchall()] if active else []
        raw_diff = shopping_diff(existing, required)
        shopping_preview = {
            "required_items": required,
            "likely_on_hand": sorted({name for item in public_items for name in item["likely_on_hand"]}),
            "diff": {
                "add": len(raw_diff["added"]), "update": len(raw_diff["updated"]),
                "remove": len(raw_diff["removed"]),
                "unchanged": max(0, len(required) - len(raw_diff["added"]) - len(raw_diff["updated"])),
            },
        }
        if any(int((catalog.get(str(item.get("recipe_id"))) or {}).get("passive_minutes", 0)) > 120 for item in meals):
            warnings.append("部分菜品需要较长等待时间，请提前准备")
        return {
            "schema_version": 1, "items": public_items,
            "unresolved_wish_ids": unresolved_wish_ids,
            "shopping_preview": shopping_preview, "warnings": warnings,
        }

    def create_weekly_meal_plan_draft(self, payload: dict, user_id: str) -> dict:
        self._kitchen_fields(payload, WEEKLY_CREATE_FIELDS, "周菜单")
        if payload.get("schema_version") != 1:
            raise ValueError("周菜单协议版本无效")
        try:
            starts = date.fromisoformat(str(payload.get("starts_on") or ""))
            ends = date.fromisoformat(str(payload.get("ends_on") or ""))
        except ValueError as error:
            raise ValueError("周菜单日期无效") from error
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        if starts < today or ends < starts or (ends - starts).days > 6:
            raise ValueError("周菜单日期必须是今天起连续 1 到 7 天")
        raw_slots = payload.get("meal_slots")
        if not isinstance(raw_slots, list) or not 1 <= len(raw_slots) <= 4:
            raise ValueError("请选择 1 到 4 个餐次")
        slots: list[dict] = []
        seen_slots: set[str] = set()
        for raw in raw_slots:
            if not isinstance(raw, dict) or set(raw) != {"meal_slot", "planned_time", "max_active_minutes"}:
                raise ValueError("餐次设置字段无效")
            slot = str(raw.get("meal_slot") or "")
            maximum = raw.get("max_active_minutes")
            if slot not in MEAL_SLOTS or slot in seen_slots:
                raise ValueError("餐次无效或重复")
            if not isinstance(maximum, int) or isinstance(maximum, bool) or not 5 <= maximum <= 240:
                raise ValueError("可用动手时间必须为 5 到 240 分钟")
            slots.append({"meal_slot": slot, "planned_time": self._weekly_parse_time(raw.get("planned_time")), "max_active_minutes": maximum})
            seen_slots.add(slot)
        slots.sort(key=lambda item: (item["planned_time"], item["meal_slot"]))
        servings = payload.get("servings")
        if not isinstance(servings, int) or isinstance(servings, bool) or not 1 <= servings <= 20:
            raise ValueError("用餐人数必须为 1 到 20")
        availability_mode = str(payload.get("availability_mode") or "either_partner")
        if availability_mode not in {"either_partner", "both_partners"}:
            raise ValueError("空闲时间模式无效")
        normalized = {
            "schema_version": 1, "starts_on": starts.isoformat(), "ends_on": ends.isoformat(),
            "meal_slots": slots, "servings": servings, "availability_mode": availability_mode,
            "idempotency_key": payload.get("idempotency_key"),
        }
        key, request_hash, operation = self._weekly_request(normalized, "weekly_plan.create")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            replay = self._weekly_replay(
                db, couple_id=couple["id"], user_id=user_id, base_operation="weekly_plan.create",
                operation=operation, idempotency_key=key,
            )
            if replay is not None:
                return replay
            active = db.execute(
                "SELECT id FROM weekly_meal_plan_drafts WHERE couple_id=? AND status IN ('draft','pending_confirmation') AND expires_at>?",
                (couple["id"], _now()),
            ).fetchone()
            if active:
                raise ConflictError("当前已有一份待确认周菜单，请先处理")
            catalog = self._weekly_recipe_catalog(db, couple["id"])
            profiles = self._food_profiles_in_connection(db, couple["id"])
            ingredient_catalog = {row["id"]: dict(row) for row in db.execute(
                "SELECT * FROM ingredient_catalog WHERE couple_id IS NULL OR couple_id=?", (couple["id"],)
            ).fetchall()}
            wishes = db.execute(
                "SELECT id,recipe_id FROM meal_wishes WHERE couple_id=? AND deleted_at IS NULL ORDER BY created_at,id", (couple["id"],)
            ).fetchall()
            wish_ids = {str(row["recipe_id"]) for row in wishes if row["recipe_id"]}
            unresolved = [str(row["id"]) for row in wishes if not row["recipe_id"]]
            pantry = self._pantry_snapshot_in_connection(db, couple["id"])
            pantry_by_name = {canonical_ingredient_name(item["canonical_name"]): item for item in pantry if item["state"] in {"have", "low"}}
            disliked = {str(tag).casefold() for profile in profiles for tag in profile.get("preferences", {}).get("disliked_tags", [])}
            liked = {str(tag).casefold() for profile in profiles for tag in profile.get("preferences", {}).get("liked_tags", [])}
            meals: list[dict] = []
            used_dishes: set[str] = set()
            used_methods: dict[str, int] = {}
            used_mains: dict[str, int] = {}
            previous_method = ""
            previous_main = ""
            current = starts
            while current <= ends:
                for slot in slots:
                    ranked: list[tuple[int, str, dict, list[str]]] = []
                    for recipe_id, recipe in catalog.items():
                        if slot["meal_slot"] not in recipe.get("meal_slots", []):
                            continue
                        if int(recipe.get("active_minutes", 0)) > slot["max_active_minutes"]:
                            continue
                        if self._evaluate_recipe_food_safety_in_connection(
                            db, couple_id=couple["id"], recipe_snapshot=recipe,
                        ).get("status") != "safe":
                            continue
                        tags = {str(recipe.get("category") or "").casefold(), str(recipe.get("cooking_method") or "").casefold()}
                        tags.update(str(tag).casefold() for tag in recipe.get("method_tags", []))
                        if disliked.intersection(tags):
                            continue
                        if not self._weekly_available(
                            db, couple=couple, day=current.isoformat(), planned_time=slot["planned_time"],
                            active_minutes=int(recipe.get("active_minutes", 0)),
                            max_active_minutes=int(slot["max_active_minutes"]), availability_mode=availability_mode,
                        ):
                            continue
                        dish_id = str(recipe.get("dish_id") or recipe_id)
                        if dish_id in used_dishes:
                            continue
                        method_key = self._weekly_cooking_method_key(recipe)
                        if method_key and used_methods.get(method_key, 0) >= 2:
                            continue
                        reasons = ["available_time"]
                        score = 0
                        if recipe_id in wish_ids:
                            score += 100
                            reasons.append("meal_wish")
                        main = next((ingredient for ingredient in recipe.get("ingredients", []) if ingredient.get("ingredient_group") == "main"), None)
                        main_name = ""
                        if main:
                            main_name = canonical_ingredient_name(main.get("canonical_name") or main.get("name") or "")
                            if main_name and used_mains.get(main_name, 0) >= 2:
                                continue
                            pantry_item = pantry_by_name.get(main_name)
                            if pantry_item and pantry_item.get("expires_on") and str(pantry_item["expires_on"]) < current.isoformat():
                                pantry_item = None
                            if pantry_item:
                                score += 15
                                reasons.append("pantry_match")
                                expiry = pantry_item.get("expires_on")
                                if expiry and current.isoformat() <= expiry <= (current + timedelta(days=3)).isoformat():
                                    score += 30
                                    reasons.append("expires_soon")
                        if liked.intersection(tags):
                            score += 5
                            reasons.append("liked_tag")
                        if method_key and method_key == previous_method:
                            score -= 20
                        if main_name and main_name == previous_main:
                            score -= 20
                        ranked.append((-score, recipe_id, recipe, list(dict.fromkeys(reasons))))
                    ranked.sort(key=lambda item: (item[0], item[1]))
                    if ranked:
                        _, recipe_id, chosen, reasons = ranked[0]
                        used_dishes.add(str(chosen.get("dish_id") or recipe_id))
                        chosen_method = self._weekly_cooking_method_key(chosen)
                        previous_method = chosen_method
                        if chosen_method:
                            used_methods[chosen_method] = used_methods.get(chosen_method, 0) + 1
                        previous_main = ""
                        chosen_main = next((ingredient for ingredient in chosen.get("ingredients", []) if ingredient.get("ingredient_group") == "main"), None)
                        if chosen_main:
                            chosen_main_name = canonical_ingredient_name(chosen_main.get("canonical_name") or chosen_main.get("name") or "")
                            if chosen_main_name:
                                used_mains[chosen_main_name] = used_mains.get(chosen_main_name, 0) + 1
                                previous_main = chosen_main_name
                        meal = {
                            "id": _id("weekly_item"), "date": current.isoformat(), "meal_slot": slot["meal_slot"],
                            "planned_time": slot["planned_time"], "recipe_id": recipe_id,
                            "recipe_version": int(chosen["version"]), "servings": servings,
                            "course_type": "main", "status": "planned", "reason_codes": reasons,
                        }
                    else:
                        previous_method = ""
                        previous_main = ""
                        meal = {
                            "id": _id("weekly_item"), "date": current.isoformat(), "meal_slot": slot["meal_slot"],
                            "planned_time": slot["planned_time"], "recipe_id": None, "recipe_version": None,
                            "servings": servings, "course_type": "main", "status": "needs_selection", "reason_codes": [],
                        }
                    meals.append(meal)
                current += timedelta(days=1)
            plan_payload = {
                "schema_version": 1, "request_fingerprint": "sha256:" + request_hash,
                "servings": servings, "availability_mode": availability_mode,
                "slot_preferences": slots, "meals": meals,
            }
            preview = self._weekly_build_preview(
                db, couple=couple, payload=plan_payload, meals=meals,
                unresolved_wish_ids=unresolved, catalog=catalog,
            )
            recipe_ids = sorted({str(item["recipe_id"]) for item in meals if item.get("recipe_id")})
            expected = self._weekly_expected_versions(
                db, couple=couple, starts_on=starts.isoformat(), ends_on=ends.isoformat(), recipe_ids=recipe_ids,
            )
            draft_id = _id("weekly_plan")
            now = datetime.now(timezone.utc).replace(microsecond=0)
            local_end = datetime.combine(ends, datetime.max.time(), ZoneInfo("Asia/Shanghai")).replace(microsecond=0).astimezone(timezone.utc)
            expires = min(now + timedelta(days=7), local_end).isoformat()
            db.execute(
                """
                INSERT INTO weekly_meal_plan_drafts(
                    id,couple_id,creator_id,starts_on,ends_on,status,payload_json,preview_json,
                    expected_versions_json,source_type,client_request_id,expires_at,created_at,updated_at
                ) VALUES (?,?,?,?,?,'draft',?,?,?,'rules',?,?,?,?)
                """,
                (draft_id, couple["id"], user_id, starts.isoformat(), ends.isoformat(), _json(plan_payload),
                 _json(preview), _json(expected), key, expires, now.isoformat(), now.isoformat()),
            )
            row = db.execute("SELECT * FROM weekly_meal_plan_drafts WHERE id=?", (draft_id,)).fetchone()
            result = self._weekly_draft_dict(row)
            self._weekly_save(
                db, couple_id=couple["id"], user_id=user_id, operation=operation,
                idempotency_key=key, request_hash=request_hash, response=result,
            )
            return result

    def get_active_weekly_meal_plan_draft(self, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute(
                """
                SELECT * FROM weekly_meal_plan_drafts
                WHERE couple_id=? AND status IN ('draft','pending_confirmation') AND expires_at>?
                ORDER BY created_at DESC LIMIT 1
                """,
                (couple["id"], _now()),
            ).fetchone()
            if not row:
                return {"id": None, "status": "empty", "items": [], "warnings": [], "result": None}
            return self._weekly_draft_dict(row)

    def update_weekly_meal_plan_item(self, draft_id: str, item_id: str, payload: dict, user_id: str) -> dict:
        self._kitchen_fields(payload, WEEKLY_UPDATE_FIELDS, "周菜单项目")
        changed_fields = set(payload).intersection({"recipe_id", "date", "meal_slot", "planned_time"})
        if not changed_fields:
            raise ValueError("请至少修改一项周菜单内容")
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 1:
            raise ValueError("周菜单版本无效")
        key, request_hash, operation = self._weekly_request(payload, f"weekly_plan.update:{draft_id}:{item_id}", draft_id)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            replay = self._weekly_replay(
                db, couple_id=couple["id"], user_id=user_id,
                base_operation=f"weekly_plan.update:{draft_id}:{item_id}", operation=operation, idempotency_key=key,
            )
            if replay is not None:
                return replay
            row = self._weekly_row(db, draft_id, couple["id"])
            if row["status"] not in {"draft", "pending_confirmation"}:
                raise ConflictError("当前周菜单不可编辑")
            if int(row["version"]) != expected_version:
                raise KitchenConflictError("周菜单已被另一方修改，请刷新", details={"current_version": int(row["version"])})
            self._weekly_require_fresh(db, couple, row)
            stored = json.loads(row["payload_json"])
            meals = stored["meals"]
            index = next((position for position, item in enumerate(meals) if item["id"] == item_id), None)
            if index is None:
                raise KeyError("周菜单项目不存在")
            item = meals[index]
            target_date = str(payload.get("date", item["date"]))
            try:
                parsed_date = date.fromisoformat(target_date)
            except ValueError as error:
                raise ValueError("餐食日期无效") from error
            if not (date.fromisoformat(row["starts_on"]) <= parsed_date <= date.fromisoformat(row["ends_on"])):
                raise ValueError("餐食日期必须位于周菜单范围内")
            target_slot = str(payload.get("meal_slot", item["meal_slot"]))
            if target_slot not in MEAL_SLOTS:
                raise ValueError("餐次无效")
            slot_preferences = stored.get("slot_preferences", [])
            slot_preference = next((value for value in slot_preferences if value.get("meal_slot") == target_slot), None)
            if not slot_preference:
                raise ConflictError("目标餐次未包含在当前周菜单设置中")
            source_date, source_slot = item["date"], item["meal_slot"]
            source_preference = next((value for value in slot_preferences if value.get("meal_slot") == source_slot), None)
            default_target_time = slot_preference["planned_time"] if target_slot != source_slot else item["planned_time"]
            target_time = self._weekly_parse_time(payload.get("planned_time", default_target_time))
            collision = next((other for other in meals if other["id"] != item_id and other["date"] == target_date and other["meal_slot"] == target_slot), None)
            swap_id = str(payload.get("swap_with_item_id") or "")
            if collision and collision["id"] != swap_id:
                raise ConflictError("目标日期和餐次已有菜品，请明确交换")
            if swap_id and (not collision or collision["id"] != swap_id):
                raise ConflictError("交换项目与目标餐次不匹配")
            if collision:
                if not source_preference:
                    raise ConflictError("原餐次未包含在当前周菜单设置中")
                collision["date"], collision["meal_slot"], collision["planned_time"] = (
                    source_date, source_slot, source_preference["planned_time"],
                )
            item["date"], item["meal_slot"], item["planned_time"] = target_date, target_slot, target_time
            catalog = self._weekly_recipe_catalog(db, couple["id"])
            if "recipe_id" in payload:
                recipe_id = str(payload.get("recipe_id") or "")
                recipe = catalog.get(recipe_id)
                if not recipe:
                    raise KeyError("菜谱不存在或不可共享")
                if target_slot not in recipe.get("meal_slots", []):
                    raise ConflictError("菜谱不适合当前餐次")
                item.update({"recipe_id": recipe_id, "recipe_version": int(recipe["version"]), "status": "planned", "reason_codes": ["available_time"]})
            selected_recipe = catalog.get(str(item.get("recipe_id"))) if item.get("recipe_id") else None
            if selected_recipe:
                if target_slot not in selected_recipe.get("meal_slots", []):
                    raise ConflictError("菜谱不适合当前餐次")
                if int(selected_recipe.get("active_minutes", 0)) > int(slot_preference["max_active_minutes"]):
                    raise ConflictError("这道菜的动手时间超过当前餐次设置")
                if not self._weekly_available(
                    db, couple=couple, day=target_date, planned_time=target_time,
                    active_minutes=int(selected_recipe.get("active_minutes", 0)),
                    max_active_minutes=int(slot_preference["max_active_minutes"]),
                    availability_mode=stored.get("availability_mode", "either_partner"),
                ):
                    raise ConflictError("当前时间没有足够的连续动手时间")
                if self._evaluate_recipe_food_safety_in_connection(db, couple_id=couple["id"], recipe_snapshot=selected_recipe).get("status") != "safe":
                    raise ConflictError("这道菜不符合双方当前饮食设置")
                profiles = self._food_profiles_in_connection(db, couple["id"])
                disliked = {str(tag).casefold() for profile in profiles for tag in profile.get("preferences", {}).get("disliked_tags", [])}
                tags = {str(selected_recipe.get("category") or "").casefold(), str(selected_recipe.get("cooking_method") or "").casefold()}
                tags.update(str(tag).casefold() for tag in selected_recipe.get("method_tags", []))
                if disliked.intersection(tags):
                    raise ConflictError("这道菜命中双方不喜欢的口味设置")
            if collision and collision.get("recipe_id"):
                swapped_recipe = catalog.get(str(collision["recipe_id"]))
                swapped_preference = next((value for value in slot_preferences if value.get("meal_slot") == collision["meal_slot"]), None)
                if (
                    not swapped_recipe or not swapped_preference
                    or collision["meal_slot"] not in swapped_recipe.get("meal_slots", [])
                    or int(swapped_recipe.get("active_minutes", 0)) > int(swapped_preference["max_active_minutes"])
                    or not self._weekly_available(
                        db, couple=couple, day=collision["date"], planned_time=collision["planned_time"],
                        active_minutes=int(swapped_recipe.get("active_minutes", 0)),
                        max_active_minutes=int(swapped_preference["max_active_minutes"]),
                        availability_mode=stored.get("availability_mode", "either_partner"),
                    )
                    or self._evaluate_recipe_food_safety_in_connection(
                        db, couple_id=couple["id"], recipe_snapshot=swapped_recipe,
                    ).get("status") != "safe"
                ):
                    raise ConflictError("交换后的另一道菜不适合目标餐次、可用时间或饮食设置")
            meals.sort(key=self._weekly_meal_sort_key)
            self._weekly_validate_diversity(meals, catalog)
            stored["meals"] = meals
            unresolved = json.loads(row["preview_json"]).get("unresolved_wish_ids", [])
            preview = self._weekly_build_preview(
                db, couple=couple, payload=stored, meals=meals, unresolved_wish_ids=unresolved, catalog=catalog,
            )
            recipe_ids = sorted({str(value["recipe_id"]) for value in meals if value.get("recipe_id")})
            expected = self._weekly_expected_versions(
                db, couple=couple, starts_on=row["starts_on"], ends_on=row["ends_on"], recipe_ids=recipe_ids,
            )
            updated = db.execute(
                """
                UPDATE weekly_meal_plan_drafts SET payload_json=?,preview_json=?,expected_versions_json=?,status='draft',
                    confirmed_a_id=NULL,confirmed_a_version=NULL,confirmed_a_at=NULL,
                    confirmed_b_id=NULL,confirmed_b_version=NULL,confirmed_b_at=NULL,
                    version=version+1,updated_at=? WHERE id=? AND version=?
                """,
                (_json(stored), _json(preview), _json(expected), _now(), draft_id, expected_version),
            ).rowcount
            if updated != 1:
                raise KitchenConflictError("周菜单已被另一方修改，请刷新")
            result = self._weekly_draft_dict(db.execute("SELECT * FROM weekly_meal_plan_drafts WHERE id=?", (draft_id,)).fetchone())
            self._weekly_save(
                db, couple_id=couple["id"], user_id=user_id, operation=operation,
                idempotency_key=key, request_hash=request_hash, response=result,
            )
            self._audit(db, "weekly_meal_plan.updated", "weekly_meal_plan", draft_id, {"version": result["version"]}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "weekly_meal_plan.updated", draft_id, {"version": result["version"]})
            return result

    def confirm_weekly_meal_plan_draft(self, draft_id: str, payload: dict, user_id: str) -> dict:
        self._kitchen_fields(payload, WEEKLY_CONFIRM_FIELDS, "周菜单确认")
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 1:
            raise ValueError("周菜单版本无效")
        key, request_hash, operation = self._weekly_request(payload, f"weekly_plan.confirm:{draft_id}", draft_id)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            replay = self._weekly_replay(
                db, couple_id=couple["id"], user_id=user_id,
                base_operation=f"weekly_plan.confirm:{draft_id}", operation=operation, idempotency_key=key,
            )
            if replay is not None:
                return replay
            row = self._weekly_row(db, draft_id, couple["id"])
            if row["status"] not in {"draft", "pending_confirmation"}:
                raise ConflictError("当前周菜单不可确认")
            if int(row["version"]) != expected_version:
                raise KitchenConflictError("周菜单已被另一方修改，请刷新", details={"current_version": int(row["version"])})
            self._weekly_require_fresh(db, couple, row)
            plan = json.loads(row["payload_json"])
            plan_meals = sorted(plan.get("meals", []), key=self._weekly_meal_sort_key)
            if any(item.get("status") != "planned" or not item.get("recipe_id") for item in plan_meals):
                raise ConflictError("周菜单仍有待选择菜品，不能确认")
            catalog = self._weekly_recipe_catalog(db, couple["id"])
            slot_preferences = {
                str(value.get("meal_slot")): value for value in plan.get("slot_preferences", [])
                if isinstance(value, dict)
            }
            profiles = self._food_profiles_in_connection(db, couple["id"])
            disliked = {
                str(tag).casefold() for profile in profiles
                for tag in profile.get("preferences", {}).get("disliked_tags", [])
            }
            for item in plan_meals:
                recipe = catalog.get(str(item["recipe_id"]))
                if not recipe or int(recipe["version"]) != int(item["recipe_version"]):
                    raise KitchenConflictError("菜谱已经变化，请重新生成周菜单")
                slot_preference = slot_preferences.get(str(item.get("meal_slot")))
                try:
                    item_date = date.fromisoformat(str(item.get("date") or ""))
                    planned_time = self._weekly_parse_time(item.get("planned_time"))
                except ValueError as error:
                    raise ConflictError("周菜单中有不再适用的日期或时间") from error
                if (
                    not slot_preference
                    or not (date.fromisoformat(row["starts_on"]) <= item_date <= date.fromisoformat(row["ends_on"]))
                    or item["meal_slot"] not in recipe.get("meal_slots", [])
                    or int(recipe.get("active_minutes", 0)) > int(slot_preference["max_active_minutes"])
                    or not self._weekly_available(
                        db, couple=couple, day=item_date.isoformat(), planned_time=planned_time,
                        active_minutes=int(recipe.get("active_minutes", 0)),
                        max_active_minutes=int(slot_preference["max_active_minutes"]),
                        availability_mode=plan.get("availability_mode", "either_partner"),
                    )
                ):
                    raise ConflictError("周菜单中有菜品不再适合当前餐次或可用时间")
                safety = self._evaluate_recipe_food_safety_in_connection(
                    db, couple_id=couple["id"], recipe_snapshot=recipe,
                )
                if safety.get("status") != "safe":
                    raise ConflictError("饮食安全设置已变化，请重新生成周菜单")
                tags = {str(recipe.get("category") or "").casefold(), str(recipe.get("cooking_method") or "").casefold()}
                tags.update(str(tag).casefold() for tag in recipe.get("method_tags", []))
                if disliked.intersection(tags):
                    raise ConflictError("周菜单中有菜品不再符合双方口味设置")
            slot = "a" if couple["member_a"] == user_id else "b"
            already = row[f"confirmed_{slot}_id"]
            if already:
                result = self._weekly_draft_dict(row)
                self._weekly_save(
                    db, couple_id=couple["id"], user_id=user_id, operation=operation,
                    idempotency_key=key, request_hash=request_hash, response=result,
                )
                return result
            other_slot = "b" if slot == "a" else "a"
            now = _now()
            if not row[f"confirmed_{other_slot}_id"]:
                db.execute(
                    f"""
                    UPDATE weekly_meal_plan_drafts SET confirmed_{slot}_id=?,confirmed_{slot}_version=?,confirmed_{slot}_at=?,
                        status='pending_confirmation',updated_at=? WHERE id=? AND version=?
                    """,
                    (user_id, expected_version, now, now, draft_id, expected_version),
                )
                result = self._weekly_draft_dict(db.execute("SELECT * FROM weekly_meal_plan_drafts WHERE id=?", (draft_id,)).fetchone())
                self._weekly_save(
                    db, couple_id=couple["id"], user_id=user_id, operation=operation,
                    idempotency_key=key, request_hash=request_hash, response=result,
                )
                return result

            cart = self._active_cart(db, couple["id"], create=True)
            meal_ids: list[str] = []
            for item in plan_meals:
                meal_id = _id("meal")
                meal_ids.append(meal_id)
                db.execute(
                    """
                    INSERT INTO meal_cart_items(
                        id,cart_id,couple_id,recipe_id,recipe_snapshot_json,planned_date,meal_slot,planned_time,
                        servings,ordered_by,cook_user_id,status,course_type,note,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,'accepted',?,'',?,?)
                    """,
                    (meal_id, cart["id"], couple["id"], item["recipe_id"], _json(catalog[item["recipe_id"]]),
                     item["date"], item["meal_slot"], item["planned_time"], item["servings"], user_id,
                     item.get("course_type", "main"), now, now),
                )
            db.execute("UPDATE meal_carts SET version=version+1 WHERE id=?", (cart["id"],))
            _, rebuild_start, rebuild_end, meal_values = self._shopping_rebuild_scope_in_connection(
                db, couple_id=couple["id"], range_start=row["starts_on"], range_end=row["ends_on"],
            )
            shopping = self._apply_shopping_items_in_connection(
                db, couple_id=couple["id"], actor_id=user_id, meal_items=meal_values,
                range_start=rebuild_start, range_end=rebuild_end,
                expected_list_version=int(json.loads(row["expected_versions_json"])["shopping_list"]),
                mark_accepted_as_shopping=False,
            )
            result_value = {
                "meal_item_ids": meal_ids,
                "shopping_list_id": shopping["shopping_list"]["id"],
                "created_count": len(meal_ids), "shopping_diff": shopping["diff"],
            }
            updated = db.execute(
                f"""
                UPDATE weekly_meal_plan_drafts SET confirmed_{slot}_id=?,confirmed_{slot}_version=?,confirmed_{slot}_at=?,
                    status='applied',result_json=?,applied_at=?,version=version+1,updated_at=?
                WHERE id=? AND version=? AND status='pending_confirmation'
                """,
                (user_id, expected_version, now, _json(result_value), now, now, draft_id, expected_version),
            ).rowcount
            if updated != 1:
                raise KitchenConflictError("周菜单已被另一方修改，请刷新")
            for recipient in (couple["member_a"], couple["member_b"]):
                self._insert_in_app_notification(
                    db, couple_id=couple["id"], recipient_id=recipient, actor_id=user_id,
                    event_type="weekly_meal_plan.applied", resource_type="weekly_meal_plan", resource_id=draft_id,
                    title="周菜单已生效", body=f"{row['starts_on']} 至 {row['ends_on']} 已写入餐食日历和采购清单",
                    url="/?screen=life&meal_mode=calendar", dedupe_key=f"weekly-plan-applied:{draft_id}:{recipient}",
                )
            final_row = db.execute("SELECT * FROM weekly_meal_plan_drafts WHERE id=?", (draft_id,)).fetchone()
            result = self._weekly_draft_dict(final_row)
            self._audit(
                db, "weekly_meal_plan.applied", "weekly_meal_plan", draft_id,
                {"version": result["version"], "starts_on": row["starts_on"], "ends_on": row["ends_on"], "created_count": len(meal_ids)},
                actor_id=user_id,
            )
            self._event(
                db, couple["id"], user_id, "weekly_meal_plan.applied", draft_id,
                {"version": result["version"], "starts_on": row["starts_on"], "ends_on": row["ends_on"], "created_count": len(meal_ids)},
            )
            self._weekly_save(
                db, couple_id=couple["id"], user_id=user_id, operation=operation,
                idempotency_key=key, request_hash=request_hash, response=result,
            )
            return result

    def cancel_weekly_meal_plan_draft(self, draft_id: str, payload: dict, user_id: str) -> dict:
        self._kitchen_fields(payload, WEEKLY_CONFIRM_FIELDS, "取消周菜单")
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 1:
            raise ValueError("周菜单版本无效")
        key, request_hash, operation = self._weekly_request(payload, f"weekly_plan.cancel:{draft_id}", draft_id)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            replay = self._weekly_replay(
                db, couple_id=couple["id"], user_id=user_id,
                base_operation=f"weekly_plan.cancel:{draft_id}", operation=operation, idempotency_key=key,
            )
            if replay is not None:
                return replay
            row = self._weekly_row(db, draft_id, couple["id"])
            if row["status"] not in {"draft", "pending_confirmation"}:
                raise ConflictError("当前周菜单不可取消")
            if int(row["version"]) != expected_version:
                raise KitchenConflictError("周菜单已被另一方修改，请刷新", details={"current_version": int(row["version"])})
            updated = db.execute(
                """
                UPDATE weekly_meal_plan_drafts SET status='cancelled',version=version+1,
                    confirmed_a_id=NULL,confirmed_a_version=NULL,confirmed_a_at=NULL,
                    confirmed_b_id=NULL,confirmed_b_version=NULL,confirmed_b_at=NULL,updated_at=?
                WHERE id=? AND version=? AND status IN ('draft','pending_confirmation')
                """,
                (_now(), draft_id, expected_version),
            ).rowcount
            if updated != 1:
                raise KitchenConflictError("周菜单已被另一方修改，请刷新")
            result = self._weekly_draft_dict(db.execute("SELECT * FROM weekly_meal_plan_drafts WHERE id=?", (draft_id,)).fetchone())
            self._weekly_save(
                db, couple_id=couple["id"], user_id=user_id, operation=operation,
                idempotency_key=key, request_hash=request_hash, response=result,
            )
            self._audit(db, "weekly_meal_plan.cancelled", "weekly_meal_plan", draft_id, {"version": result["version"]}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "weekly_meal_plan.cancelled", draft_id, {"version": result["version"]})
            return result

    # ---- Ingredient substitutions ------------------------------------

    @staticmethod
    def _substitution_source_row(snapshot: dict, snapshot_ref: str) -> dict:
        rows = [item for item in snapshot.get("ingredients", []) if isinstance(item, dict)]
        exact = [item for item in rows if str(item.get("id") or "") == snapshot_ref]
        if len(exact) == 1:
            return exact[0]
        if snapshot_ref.startswith("position:"):
            try:
                position = int(snapshot_ref.split(":", 1)[1])
            except ValueError as error:
                raise ValueError("食材引用无效") from error
            positioned = [item for item in rows if int(item.get("position", -1)) == position]
            if len(positioned) == 1:
                return positioned[0]
        raise ValueError("食材引用不存在或不唯一")

    @staticmethod
    def _substitution_method_allowed(snapshot: dict, rule: SubstitutionRule) -> bool:
        values = {
            str(snapshot.get("cooking_method") or "").strip(),
            *(str(value).strip() for value in snapshot.get("method_tags", []) if str(value).strip()),
        }
        combined = " ".join(sorted(values))
        return any(
            method in values
            or method in combined
            or method.removeprefix("普通") in combined
            for method in rule.suitable_methods
        )

    @staticmethod
    def _substitution_evidence(snapshot: dict) -> dict:
        names = {
            str(item.get("name") or item.get("canonical_name") or "").strip()
            for item in snapshot.get("ingredients", []) if isinstance(item, dict)
        }
        names.discard("")
        return allergen_report(names)

    def _substitution_expected_versions(
        self,
        db: sqlite3.Connection,
        *,
        couple_id: str,
        meal_row: sqlite3.Row,
    ) -> dict:
        cart = db.execute("SELECT version FROM meal_carts WHERE id=?", (meal_row["cart_id"],)).fetchone()
        shopping = db.execute(
            "SELECT version FROM shopping_lists WHERE couple_id=? AND status='active'", (couple_id,),
        ).fetchone()
        profiles = {
            str(item["user_id"]): int(item["version"])
            for item in self._food_profiles_in_connection(db, couple_id)
        }
        return {
            "meal": int(meal_row["version"]),
            "meal_cart": int(cart["version"]) if cart else 0,
            "shopping_list": int(shopping["version"]) if shopping else 0,
            "profiles": profiles,
        }

    def _build_substitution_candidate_in_connection(
        self,
        db: sqlite3.Connection,
        *,
        couple_id: str,
        meal_row: sqlite3.Row,
        snapshot_ingredient_ref: str,
        rule: SubstitutionRule,
    ) -> dict:
        meal = self._meal_item_dict(meal_row)
        snapshot = meal["recipe_snapshot"]
        source = self._substitution_source_row(snapshot, snapshot_ingredient_ref)
        source_name = canonical_ingredient_name(source.get("canonical_name") or source.get("name") or "")
        if source_name not in {canonical_ingredient_name(value) for value in rule.from_names}:
            raise ConflictError("替代规则与当前食材不匹配")
        from_ingredient = self._resolve_ingredient_in_connection(
            db, couple_id=couple_id, name=source_name, source_type="rules",
        )
        to_ingredient = self._resolve_ingredient_in_connection(
            db, couple_id=couple_id, name=rule.to_name, source_type="rules",
        )
        if from_ingredient["id"] == to_ingredient["id"]:
            raise ConflictError("替代食材与原食材相同")

        hypothetical = json.loads(json.dumps(snapshot, ensure_ascii=False))
        target = self._substitution_source_row(hypothetical, snapshot_ingredient_ref)
        quantity = source.get("quantity_milli")
        quantity_ok = isinstance(quantity, int) and not isinstance(quantity, bool) and quantity > 0
        family = str(source.get("unit_family") or "")
        unit_ok = family in rule.compatible_unit_families
        method_ok = self._substitution_method_allowed(snapshot, rule)
        if quantity_ok and unit_ok:
            target_quantity = int(
                (Decimal(quantity) * Decimal(rule.ratio_numerator) / Decimal(rule.ratio_denominator)).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP,
                )
            )
            target["quantity_milli"] = target_quantity
            target["quantity_text"] = ""
            target["display_quantity"] = format_quantity(
                target_quantity, str(target.get("canonical_unit") or "vague"), "",
            )
        target.update({
            "name": rule.to_name,
            "canonical_name": canonical_ingredient_name(rule.to_name),
            "ingredient_id": to_ingredient["id"],
            "preparation": str(source.get("preparation") or rule.usage_note),
        })
        before_evidence = self._substitution_evidence(snapshot)
        after_evidence = self._substitution_evidence(hypothetical)
        before_names = {str(item.get("name")) for item in before_evidence["allergens"]}
        added_allergens = [
            {"name": str(item["name"]), "basis": str(item.get("basis") or "当前配料表")}
            for item in after_evidence["allergens"] if str(item.get("name")) not in before_names
        ]
        hypothetical.update({
            "allergen_tags": [item["name"] for item in after_evidence["allergens"]],
            "allergen_details": after_evidence["allergens"],
            "allergen_review_ingredients": after_evidence["review_ingredients"],
            "allergen_note": after_evidence["note"],
        })
        safety = self._evaluate_recipe_food_safety_in_connection(
            db, couple_id=couple_id, recipe_snapshot=hypothetical,
        )
        review_required = bool(
            rule.requires_package_review
            or not quantity_ok
            or not unit_ok
            or not method_ok
            or safety["avoid_conflicts"]
            or safety["ingredient_avoidances"]
            or safety["review_ingredients"]
        )
        if safety["severe_conflicts"]:
            status = "blocked"
        elif review_required or safety["status"] != "safe":
            status = "needs_review"
        else:
            status = "allowed"
        reasons = ["actual_ingredients_checked", "current_profiles_checked"]
        if not quantity_ok:
            reasons.append("quantity_needs_review")
        if not unit_ok:
            reasons.append("unit_needs_review")
        if not method_ok:
            reasons.append("method_needs_review")
        if rule.requires_package_review or safety["review_ingredients"]:
            reasons.append("package_needs_review")
        if status == "blocked":
            reasons.append("saved_severe_conflict")
        elif safety["avoid_conflicts"] or safety["ingredient_avoidances"]:
            reasons.append("saved_avoidance_needs_review")
        return {
            "rule": rule,
            "source_row": source,
            "from_ingredient": dict(from_ingredient),
            "to_ingredient": dict(to_ingredient),
            "hypothetical_snapshot": hypothetical,
            "added_allergens": added_allergens,
            "safety_status": status,
            "safety_reason_codes": reasons,
            "can_apply": status == "allowed",
        }

    @staticmethod
    def _substitution_safety_message(status: str) -> str:
        if status == "blocked":
            return "与已保存的严重过敏设置冲突，只能查看，不能直接应用。"
        if status == "needs_review":
            return "存在包装成分、普通忌口、用量或做法需要人工核对，只能查看。"
        return "当前配料表未识别到已保存冲突；仍请核对实际包装。"

    def _substitution_public_item(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict:
        from_item = self._ingredient_by_id(db, row["couple_id"], row["from_ingredient_id"])
        to_item = self._ingredient_by_id(db, row["couple_id"], row["to_ingredient_id"])
        rule = SUBSTITUTION_RULES_BY_KEY.get(str(row["rule_key"] or ""))
        try:
            effect = json.loads(row["effect_note"] or "{}")
        except (TypeError, json.JSONDecodeError):
            effect = {}
        expired = bool(row["expires_at"] and row["expires_at"] <= _now())
        status = "expired" if expired and row["status"] == "suggested" else row["status"]
        safety_status = str(row["safety_status"])
        flavor = str(effect.get("flavor_change") or (rule.flavor_note if rule else ""))
        texture = str(effect.get("texture_change") or (rule.texture_note if rule else ""))
        usage = str(effect.get("usage_note") or (rule.usage_note if rule else ""))
        return {
            "id": row["id"],
            "from_ingredient": {"id": from_item["id"], "name": from_item["canonical_name"]},
            "to_ingredient": {"id": to_item["id"], "name": to_item["canonical_name"]},
            "ratio_note": row["ratio_note"],
            "suitable_methods": json.loads(row["suitable_methods_json"]),
            "flavor_change": flavor,
            "texture_change": texture,
            "effect_note": usage,
            "added_allergens": json.loads(row["added_allergens_json"]),
            "safety_status": safety_status,
            "safety_reason_codes": list(effect.get("safety_reason_codes") or []),
            "safety_message": self._substitution_safety_message(safety_status),
            "can_apply": bool(status == "suggested" and safety_status == "allowed"),
            "source_type": "rules",
            "source_label": "本地校准规则",
            "expires_at": row["expires_at"],
            "status": status,
            "version": int(row["version"]),
        }

    def _substitution_shopping_preview(
        self,
        db: sqlite3.Connection,
        *,
        couple_id: str,
        meal_row: sqlite3.Row,
        hypothetical_snapshot: dict | None,
    ) -> dict:
        active, range_start, range_end, meals = self._shopping_rebuild_scope_in_connection(
            db, couple_id=couple_id, range_start=meal_row["planned_date"], range_end=meal_row["planned_date"],
        )
        if not any(item["id"] == meal_row["id"] for item in meals):
            # A selected meal may be safely edited before the couple advances
            # its workflow status.  Include only this explicit target in the
            # preview so the confirm operation never fails halfway merely
            # because the meal has not yet reached ``accepted``.
            meals.append(self._meal_item_dict(meal_row))
        if hypothetical_snapshot is not None:
            for item in meals:
                if item["id"] == meal_row["id"]:
                    item["recipe_snapshot"] = hypothetical_snapshot
        required = build_shopping_items(meals) if meals else []
        existing = [dict(row) for row in db.execute(
            "SELECT * FROM shopping_list_items WHERE shopping_list_id=?", (active["id"],),
        ).fetchall()] if active else []
        raw = shopping_diff(existing, required)
        changed_names = {
            "add": [str(item.get("display_name") or item.get("canonical_name") or "") for item in raw["added"]],
            "update": [str(item["after"].get("display_name") or item["after"].get("canonical_name") or "") for item in raw["updated"]],
            "remove": [str(item.get("display_name") or item.get("canonical_name") or "") for item in raw["removed"]],
            "protected": [
                str(item["current"].get("display_name") or item["current"].get("canonical_name") or "")
                for item in raw["protected"]
            ],
        }
        return {
            "expected_list_version": int(active["version"]) if active else 0,
            "range_start": range_start,
            "range_end": range_end,
            "diff": {
                "add": len(raw["added"]), "update": len(raw["updated"]),
                "remove": len(raw["removed"]),
                "unchanged": max(0, len(required) - len(raw["added"]) - len(raw["updated"])),
            },
            "changes": changed_names,
        }

    def preview_ingredient_substitutions(self, meal_item_id: str, payload: dict, user_id: str) -> dict:
        self._kitchen_fields(payload, SUBSTITUTION_PREVIEW_FIELDS, "食材替代预览")
        snapshot_ref = _clean_text(payload.get("snapshot_ingredient_ref"), 160, "食材引用", required=True)
        expected_meal = payload.get("expected_meal_version")
        expected_shopping = payload.get("expected_shopping_list_version")
        if not isinstance(expected_meal, int) or isinstance(expected_meal, bool) or expected_meal < 1:
            raise ValueError("餐食版本无效")
        if not isinstance(expected_shopping, int) or isinstance(expected_shopping, bool) or expected_shopping < 0:
            raise ValueError("采购清单版本无效")
        key, request_hash, operation = self._weekly_request(
            payload, "ingredient_substitution.preview", f"{meal_item_id}:{snapshot_ref}",
        )
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            replay = self._weekly_replay(
                db, couple_id=couple["id"], user_id=user_id,
                base_operation="ingredient_substitution.preview", operation=operation, idempotency_key=key,
            )
            if replay is not None:
                return replay
            meal_row = db.execute(
                "SELECT * FROM meal_cart_items WHERE id=? AND couple_id=?", (meal_item_id, couple["id"]),
            ).fetchone()
            if not meal_row:
                raise KeyError("餐食不存在")
            if meal_row["status"] not in {"selected", "accepted", "shopping"}:
                raise ConflictError("当前餐食状态不能自动替换食材")
            if int(meal_row["version"]) != expected_meal:
                raise KitchenConflictError("餐食已经变化，请刷新", details={"current_version": int(meal_row["version"])})
            current_versions = self._substitution_expected_versions(
                db, couple_id=couple["id"], meal_row=meal_row,
            )
            if current_versions["shopping_list"] != expected_shopping:
                raise KitchenConflictError(
                    "采购清单已经变化，请刷新", details={"current_version": current_versions["shopping_list"]},
                )
            snapshot = self._meal_item_dict(meal_row)["recipe_snapshot"]
            source_row = self._substitution_source_row(snapshot, snapshot_ref)
            source_name = canonical_ingredient_name(source_row.get("canonical_name") or source_row.get("name") or "")
            matching = [
                rule for rule in SUBSTITUTION_RULES
                if source_name in {canonical_ingredient_name(value) for value in rule.from_names}
            ]
            candidates = [
                self._build_substitution_candidate_in_connection(
                    db, couple_id=couple["id"], meal_row=meal_row,
                    snapshot_ingredient_ref=snapshot_ref, rule=rule,
                )
                for rule in matching
            ]
            now = datetime.now(timezone.utc).replace(microsecond=0)
            expires = (now + timedelta(hours=24)).isoformat()
            db.execute(
                """
                UPDATE ingredient_substitutions SET status='expired',version=version+1,updated_at=?
                WHERE couple_id=? AND creator_id=? AND meal_item_id=? AND snapshot_ingredient_ref=? AND status='suggested'
                """,
                (now.isoformat(), couple["id"], user_id, meal_item_id, snapshot_ref),
            )
            public_items: list[dict] = []
            for candidate in candidates:
                rule = candidate["rule"]
                suggestion_id = _id("substitution")
                child_key = hashlib.sha256(f"{key}:{rule.key}".encode("utf-8")).hexdigest()
                effect = {
                    "flavor_change": rule.flavor_note,
                    "texture_change": rule.texture_note,
                    "usage_note": rule.usage_note,
                    "safety_reason_codes": candidate["safety_reason_codes"],
                }
                db.execute(
                    """
                    INSERT INTO ingredient_substitutions(
                        id,couple_id,creator_id,from_ingredient_id,to_ingredient_id,recipe_id,
                        meal_item_id,snapshot_ingredient_ref,meal_version,expected_versions_json,
                        rule_key,rule_version,ratio_note,suitable_methods_json,effect_note,
                        added_allergens_json,safety_status,profile_versions_json,source_type,status,
                        client_request_id,version,expires_at,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'rules','suggested',?,1,?,?,?)
                    """,
                    (
                        suggestion_id, couple["id"], user_id,
                        candidate["from_ingredient"]["id"], candidate["to_ingredient"]["id"], meal_row["recipe_id"],
                        meal_item_id, snapshot_ref, int(meal_row["version"]), _json(current_versions),
                        rule.key, rule.version,
                        f"目标用量按原用量 {rule.ratio_numerator}:{rule.ratio_denominator}",
                        _json(list(rule.suitable_methods)), _json(effect), _json(candidate["added_allergens"]),
                        candidate["safety_status"], _json(current_versions["profiles"]), child_key,
                        expires, now.isoformat(), now.isoformat(),
                    ),
                )
                stored = db.execute("SELECT * FROM ingredient_substitutions WHERE id=?", (suggestion_id,)).fetchone()
                public_item = self._substitution_public_item(db, stored)
                public_item["shopping_preview"] = self._substitution_shopping_preview(
                    db, couple_id=couple["id"], meal_row=meal_row,
                    hypothetical_snapshot=candidate["hypothetical_snapshot"],
                )
                public_items.append(public_item)
            shopping_preview = public_items[0]["shopping_preview"] if public_items else self._substitution_shopping_preview(
                db, couple_id=couple["id"], meal_row=meal_row, hypothetical_snapshot=None,
            )
            source_catalog = self._resolve_ingredient_in_connection(
                db, couple_id=couple["id"], name=source_name, source_type="rules",
            )
            result = {
                "meal_item_id": meal_item_id,
                "meal_version": int(meal_row["version"]),
                "source_ingredient": {
                    "snapshot_ref": snapshot_ref,
                    "ingredient_id": source_catalog["id"],
                    "name": source_catalog["canonical_name"],
                },
                "items": public_items,
                "shopping_preview": shopping_preview,
            }
            if not public_items:
                result["message"] = "暂无经过核对的替代规则，可人工编辑菜谱后重新安排。"
            self._weekly_save(
                db, couple_id=couple["id"], user_id=user_id, operation=operation,
                idempotency_key=key, request_hash=request_hash, response=result,
            )
            return result

    def list_ingredient_substitutions(self, meal_item_id: str, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            meal = db.execute(
                "SELECT * FROM meal_cart_items WHERE id=? AND couple_id=?", (meal_item_id, couple["id"]),
            ).fetchone()
            if not meal:
                raise KeyError("餐食不存在")
            rows = db.execute(
                """
                SELECT * FROM ingredient_substitutions
                WHERE couple_id=? AND meal_item_id=? AND creator_id=?
                ORDER BY created_at DESC,id
                """,
                (couple["id"], meal_item_id, user_id),
            ).fetchall()
            items: list[dict] = []
            for row in rows:
                item = self._substitution_public_item(db, row)
                rule = SUBSTITUTION_RULES_BY_KEY.get(str(row["rule_key"] or ""))
                if item["status"] == "suggested" and rule:
                    try:
                        candidate = self._build_substitution_candidate_in_connection(
                            db, couple_id=couple["id"], meal_row=meal,
                            snapshot_ingredient_ref=str(row["snapshot_ingredient_ref"]), rule=rule,
                        )
                        item["shopping_preview"] = self._substitution_shopping_preview(
                            db, couple_id=couple["id"], meal_row=meal,
                            hypothetical_snapshot=candidate["hypothetical_snapshot"],
                        )
                    except (ConflictError, KeyError, ValueError):
                        item["can_apply"] = False
                items.append(item)
            return {
                "meal_item_id": meal_item_id,
                "meal_version": int(meal["version"]),
                "items": items,
            }

    def apply_ingredient_substitution(
        self,
        meal_item_id: str,
        substitution_id: str,
        payload: dict,
        user_id: str,
    ) -> dict:
        self._kitchen_fields(payload, SUBSTITUTION_APPLY_FIELDS, "应用食材替代")
        expected_meal = payload.get("expected_meal_version")
        expected_shopping = payload.get("expected_shopping_list_version")
        expected_substitution = payload.get("expected_version")
        if not isinstance(expected_meal, int) or isinstance(expected_meal, bool) or expected_meal < 1:
            raise ValueError("餐食版本无效")
        if not isinstance(expected_shopping, int) or isinstance(expected_shopping, bool) or expected_shopping < 0:
            raise ValueError("采购清单版本无效")
        if not isinstance(expected_substitution, int) or isinstance(expected_substitution, bool) or expected_substitution < 1:
            raise ValueError("替代建议版本无效")
        key, request_hash, operation = self._weekly_request(
            payload, "ingredient_substitution.apply", f"{meal_item_id}:{substitution_id}",
        )
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            replay = self._weekly_replay(
                db, couple_id=couple["id"], user_id=user_id,
                base_operation="ingredient_substitution.apply", operation=operation, idempotency_key=key,
            )
            if replay is not None:
                return replay
            suggestion = db.execute(
                """
                SELECT * FROM ingredient_substitutions
                WHERE id=? AND couple_id=? AND meal_item_id=? AND creator_id=?
                """,
                (substitution_id, couple["id"], meal_item_id, user_id),
            ).fetchone()
            if not suggestion:
                raise KeyError("替代建议不存在")
            if suggestion["status"] != "suggested" or (suggestion["expires_at"] and suggestion["expires_at"] <= _now()):
                raise ConflictError("替代建议已经失效，请重新预览")
            if int(suggestion["version"]) != expected_substitution:
                raise KitchenConflictError(
                    "替代建议已经变化，请刷新", details={"current_version": int(suggestion["version"])},
                )
            meal_row = db.execute(
                "SELECT * FROM meal_cart_items WHERE id=? AND couple_id=?", (meal_item_id, couple["id"]),
            ).fetchone()
            if not meal_row:
                raise KeyError("餐食不存在")
            if meal_row["status"] not in {"selected", "accepted", "shopping"}:
                raise ConflictError("当前餐食状态不能自动替换食材")
            if int(meal_row["version"]) != expected_meal:
                raise KitchenConflictError("餐食已经变化，请重新预览", details={"current_version": int(meal_row["version"])})
            current_versions = self._substitution_expected_versions(
                db, couple_id=couple["id"], meal_row=meal_row,
            )
            saved_versions = json.loads(suggestion["expected_versions_json"])
            if current_versions != saved_versions:
                raise KitchenConflictError("饮食资料、餐食或采购清单已经变化，请重新预览")
            if current_versions["shopping_list"] != expected_shopping:
                raise KitchenConflictError(
                    "采购清单已经变化，请重新预览", details={"current_version": current_versions["shopping_list"]},
                )
            rule = SUBSTITUTION_RULES_BY_KEY.get(str(suggestion["rule_key"] or ""))
            if not rule or int(suggestion["rule_version"] or 0) != rule.version:
                raise ConflictError("替代规则已经更新，请重新预览")
            candidate = self._build_substitution_candidate_in_connection(
                db, couple_id=couple["id"], meal_row=meal_row,
                snapshot_ingredient_ref=str(suggestion["snapshot_ingredient_ref"]), rule=rule,
            )
            if not candidate["can_apply"] or candidate["safety_status"] != "allowed":
                raise ConflictError("当前替代存在过敏、忌口或人工核对项，不能直接应用")
            confirmed_preview = self._substitution_shopping_preview(
                db, couple_id=couple["id"], meal_row=meal_row,
                hypothetical_snapshot=candidate["hypothetical_snapshot"],
            )
            now = _now()
            changed = db.execute(
                """
                UPDATE meal_cart_items
                SET recipe_snapshot_json=?,version=version+1,updated_at=?
                WHERE id=? AND couple_id=? AND version=?
                """,
                (_json(candidate["hypothetical_snapshot"]), now, meal_item_id, couple["id"], expected_meal),
            ).rowcount
            if changed != 1:
                raise KitchenConflictError("餐食已经变化，请重新预览")
            cart_changed = db.execute(
                "UPDATE meal_carts SET version=version+1 WHERE id=? AND version=?",
                (meal_row["cart_id"], current_versions["meal_cart"]),
            ).rowcount
            if cart_changed != 1:
                raise KitchenConflictError("点菜车已经变化，请重新预览")
            _, rebuild_start, rebuild_end, meals = self._shopping_rebuild_scope_in_connection(
                db, couple_id=couple["id"], range_start=meal_row["planned_date"], range_end=meal_row["planned_date"],
            )
            if not any(item["id"] == meal_item_id for item in meals):
                updated_target = db.execute(
                    "SELECT * FROM meal_cart_items WHERE id=? AND couple_id=?", (meal_item_id, couple["id"]),
                ).fetchone()
                meals.append(self._meal_item_dict(updated_target))
            shopping = self._apply_shopping_items_in_connection(
                db, couple_id=couple["id"], actor_id=user_id, meal_items=meals,
                range_start=rebuild_start, range_end=rebuild_end,
                expected_list_version=expected_shopping, mark_accepted_as_shopping=False,
            )
            if shopping["diff"] != confirmed_preview["diff"]:
                raise KitchenConflictError("采购差异已经变化，请重新预览")
            updated = db.execute(
                """
                UPDATE ingredient_substitutions SET status='applied',version=version+1,updated_at=?
                WHERE id=? AND creator_id=? AND meal_item_id=? AND version=? AND status='suggested'
                """,
                (now, substitution_id, user_id, meal_item_id, expected_substitution),
            ).rowcount
            if updated != 1:
                raise KitchenConflictError("替代建议已经变化，请刷新")
            applied_meal = db.execute("SELECT version FROM meal_cart_items WHERE id=?", (meal_item_id,)).fetchone()
            result = {
                "substitution_id": substitution_id,
                "status": "applied",
                "meal_item": {"id": meal_item_id, "version": int(applied_meal["version"])},
                "shopping_list": {
                    "id": shopping["shopping_list"]["id"],
                    "version": int(shopping["shopping_list"]["version"]),
                },
                "shopping_diff": shopping["diff"],
                "shopping_preview": confirmed_preview,
                "source_type": "rules",
                "idempotent": False,
            }
            self._audit(
                db, "ingredient_substitution.applied", "meal_item", meal_item_id,
                {
                    "substitution_id": substitution_id,
                    "meal_version": result["meal_item"]["version"],
                    "shopping_list_id": result["shopping_list"]["id"],
                    "shopping_version": result["shopping_list"]["version"],
                    "diff": result["shopping_diff"],
                },
                actor_id=user_id,
            )
            self._event(
                db, couple["id"], user_id, "ingredient_substitution.applied", meal_item_id,
                {
                    "resource": "meal_item", "operation": "ingredient_substitution.applied",
                    "version": result["meal_item"]["version"],
                },
            )
            partner_id = couple["member_b"] if user_id == couple["member_a"] else couple["member_a"]
            self._insert_in_app_notification(
                db, couple_id=couple["id"], recipient_id=partner_id, actor_id=user_id,
                event_type="ingredient_substitution.applied", resource_type="meal_item", resource_id=meal_item_id,
                title="餐食食材已更新", body="一项经过确认的食材替代已同步到餐食和采购清单",
                url="/?screen=life&meal_mode=calendar",
                dedupe_key=f"substitution-applied:{substitution_id}:{partner_id}",
            )
            self._weekly_save(
                db, couple_id=couple["id"], user_id=user_id, operation=operation,
                idempotency_key=key, request_hash=request_hash, response=result,
            )
            return result

    def dismiss_ingredient_substitution(
        self,
        meal_item_id: str,
        substitution_id: str,
        payload: dict,
        user_id: str,
    ) -> dict:
        self._kitchen_fields(payload, SUBSTITUTION_DISMISS_FIELDS, "忽略食材替代")
        expected = payload.get("expected_version")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
            raise ValueError("替代建议版本无效")
        key, request_hash, operation = self._weekly_request(
            payload, "ingredient_substitution.dismiss", f"{meal_item_id}:{substitution_id}",
        )
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            replay = self._weekly_replay(
                db, couple_id=couple["id"], user_id=user_id,
                base_operation="ingredient_substitution.dismiss", operation=operation, idempotency_key=key,
            )
            if replay is not None:
                return replay
            row = db.execute(
                """
                SELECT * FROM ingredient_substitutions
                WHERE id=? AND couple_id=? AND meal_item_id=? AND creator_id=?
                """,
                (substitution_id, couple["id"], meal_item_id, user_id),
            ).fetchone()
            if not row:
                raise KeyError("替代建议不存在")
            if row["status"] != "suggested" or (row["expires_at"] and row["expires_at"] <= _now()):
                raise ConflictError("替代建议已经失效")
            if int(row["version"]) != expected:
                raise KitchenConflictError("替代建议已经变化，请刷新", details={"current_version": int(row["version"])})
            changed = db.execute(
                """
                UPDATE ingredient_substitutions SET status='dismissed',version=version+1,updated_at=?
                WHERE id=? AND creator_id=? AND meal_item_id=? AND version=? AND status='suggested'
                """,
                (_now(), substitution_id, user_id, meal_item_id, expected),
            ).rowcount
            if changed != 1:
                raise KitchenConflictError("替代建议已经变化，请刷新")
            result = {"substitution_id": substitution_id, "status": "dismissed", "version": expected + 1}
            self._weekly_save(
                db, couple_id=couple["id"], user_id=user_id, operation=operation,
                idempotency_key=key, request_hash=request_hash, response=result,
            )
            return result

    # ---- Recoverable cooking sessions -------------------------------

    @staticmethod
    def _cook_timer_dict(row: sqlite3.Row, now: datetime | None = None) -> dict:
        value = dict(row)
        current = now or datetime.now(timezone.utc)
        ends = datetime.fromisoformat(str(row["ends_at"]).replace("Z", "+00:00"))
        if value["status"] == "running" and ends <= current:
            value["status"] = "completed"
        return value

    @staticmethod
    def _cook_ingredient_ref(ingredient: dict, position: int, used: set[str]) -> str:
        candidate = str(ingredient.get("id") or "").strip()
        if not candidate:
            raw_position = ingredient.get("position")
            if not isinstance(raw_position, int) or isinstance(raw_position, bool) or raw_position < 0:
                raise ConflictError("旧菜谱缺少唯一食材位置，暂时不能更新库存")
            candidate = f"position:{raw_position}"
        if candidate in used:
            raise ConflictError("菜谱食材引用重复，暂时不能更新库存")
        used.add(candidate)
        return candidate

    def _cook_access_row(
        self, db: sqlite3.Connection, couple_id: str, user_id: str, session_id: str,
    ) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM cook_sessions WHERE id=? AND couple_id=?",
            (session_id, couple_id),
        ).fetchone()
        if not row or (row["visibility"] == "private" and row["creator_id"] != user_id):
            raise KeyError("烹饪会话不存在")
        return row

    def _cook_pantry_suggestions(
        self, db: sqlite3.Connection, couple_id: str, snapshot: dict,
    ) -> list[dict]:
        ingredients = snapshot.get("ingredients")
        if not isinstance(ingredients, list):
            return []
        used: set[str] = set()
        suggestions: list[dict] = []
        for position, raw in enumerate(ingredients):
            if not isinstance(raw, dict):
                continue
            name = _clean_text(raw.get("name") or raw.get("canonical_name"), 100, "食材名称")
            if not name:
                continue
            ingredient_ref = self._cook_ingredient_ref(raw, position, used)
            normalized = normalize_name(canonical_ingredient_name(name))
            catalog = db.execute(
                """SELECT * FROM ingredient_catalog
                   WHERE normalized_name=? AND (couple_id IS NULL OR couple_id=?)
                   ORDER BY CASE WHEN couple_id IS NULL THEN 0 ELSE 1 END LIMIT 1""",
                (normalized, couple_id),
            ).fetchone()
            pantry = db.execute(
                "SELECT * FROM pantry_items WHERE couple_id=? AND ingredient_id=?",
                (couple_id, catalog["id"]),
            ).fetchone() if catalog else None
            suggestions.append({
                "ingredient_ref": ingredient_ref,
                "name": catalog["canonical_name"] if catalog else canonical_ingredient_name(name),
                "ingredient_group": str(raw.get("ingredient_group") or raw.get("category") or "main"),
                "optional": bool(raw.get("optional", False)),
                "pantry_state": pantry["state"] if pantry else None,
                "pantry_version": int(pantry["version"]) if pantry else 0,
                "allowed_states": ["low", "out"],
                "selected_state": None,
            })
        return suggestions

    def _cook_session_dict(
        self,
        db: sqlite3.Connection,
        row: sqlite3.Row,
        user_id: str,
        *,
        resumed: bool = False,
        detail: bool = True,
    ) -> dict:
        value = dict(row)
        snapshot = json.loads(value.pop("recipe_snapshot_json"))
        cook_context = snapshot.pop("_cook_context", {}) if isinstance(snapshot.get("_cook_context"), dict) else {}
        value["name"] = snapshot.get("name", "未命名菜谱")
        value["servings"] = int(cook_context.get("servings") or snapshot.get("base_servings") or 1)
        value["can_edit"] = True
        value["resumed"] = bool(resumed)
        value["server_now"] = _now()
        creator = db.execute("SELECT nickname FROM users WHERE id=?", (row["creator_id"],)).fetchone()
        value["creator_name"] = creator["nickname"] if creator else ""
        if row["meal_item_id"]:
            value["source_meal_version"] = cook_context.get("source_meal_version")
        if detail:
            value["recipe_snapshot"] = snapshot
            now = datetime.now(timezone.utc)
            value["timers"] = [self._cook_timer_dict(timer, now) for timer in db.execute(
                "SELECT * FROM cook_timers WHERE session_id=? ORDER BY started_at,id", (row["id"],),
            ).fetchall()]
            value["pantry_suggestions"] = (
                self._cook_pantry_suggestions(db, row["couple_id"], snapshot)
                if row["status"] == "completed" else []
            )
        return value

    def list_cook_sessions(
        self, user_id: str, status: str = "active", meal_item_id: str | None = None,
    ) -> list[dict]:
        if status not in {"active", "completed", "cancelled", "all"}:
            raise ValueError("烹饪会话状态无效")
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            where_status = "" if status == "all" else " AND status=?"
            where_meal = " AND meal_item_id=?" if meal_item_id else ""
            params: list[object] = [couple["id"], user_id]
            if status != "all":
                params.append(status)
            if meal_item_id:
                params.append(str(meal_item_id))
            rows = db.execute(
                f"""
                SELECT * FROM cook_sessions
                WHERE couple_id=? AND (visibility='shared' OR creator_id=?){where_status}{where_meal}
                ORDER BY updated_at DESC,id DESC
                """,
                params,
            ).fetchall()
            return [self._cook_session_dict(db, row, user_id, detail=False) for row in rows]

    def get_cook_session(self, session_id: str, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = self._cook_access_row(db, couple["id"], user_id, session_id)
            return self._cook_session_dict(db, row, user_id)

    def start_cook_session(self, payload: dict, user_id: str) -> dict:
        self._kitchen_fields(payload, COOK_START_FIELDS, "开始烹饪")
        recipe_id = str(payload.get("recipe_id") or "").strip() or None
        meal_item_id = str(payload.get("meal_item_id") or "").strip() or None
        if bool(recipe_id) == bool(meal_item_id):
            raise ValueError("请只选择菜谱或菜单中的一个来源")
        visibility = str(payload.get("visibility") or "private")
        if visibility not in {"private", "shared"}:
            raise ValueError("烹饪会话可见范围无效")
        if meal_item_id and visibility != "shared":
            raise ValueError("共同菜单只能创建双方可见的烹饪会话；仅自己可见请从个人菜谱开始")
        key, request_hash, operation = self._weekly_request(
            payload, "cook.start", meal_item_id or recipe_id or "",
        )
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            replay = self._weekly_replay(
                db, couple_id=couple["id"], user_id=user_id,
                base_operation="cook.start", operation=operation, idempotency_key=key,
            )
            if replay is not None:
                return replay
            if meal_item_id:
                active = db.execute(
                    "SELECT * FROM cook_sessions WHERE couple_id=? AND meal_item_id=? AND status='active'",
                    (couple["id"], meal_item_id),
                ).fetchone()
            else:
                active = db.execute(
                    """SELECT * FROM cook_sessions
                       WHERE couple_id=? AND creator_id=? AND recipe_id=? AND meal_item_id IS NULL
                         AND status='active'""",
                    (couple["id"], user_id, recipe_id),
                ).fetchone()
            if active:
                if active["visibility"] == "private" and active["creator_id"] != user_id:
                    raise ConflictError("这份菜单已经在烹饪中")
                result = self._cook_session_dict(db, active, user_id, resumed=True)
                self._weekly_save(
                    db, couple_id=couple["id"], user_id=user_id, operation=operation,
                    idempotency_key=key, request_hash=request_hash, response=result,
                )
                return result
            now = _now()
            if recipe_id:
                recipe_row = self._get_visible_recipe_row(db, recipe_id, couple["id"], user_id)
                if visibility == "shared" and recipe_row["visibility"] == "private":
                    raise PermissionError("个人私密菜谱不能创建共享烹饪会话")
                snapshot = recipe_snapshot(self._recipe_row_to_dict(db, recipe_row))
                snapshot["_cook_context"] = {"servings": int(snapshot.get("base_servings") or 1)}
            else:
                meal = db.execute(
                    """SELECT i.* FROM meal_cart_items i JOIN meal_carts c ON c.id=i.cart_id
                       WHERE i.id=? AND c.couple_id=?""",
                    (meal_item_id, couple["id"]),
                ).fetchone()
                if not meal:
                    raise KeyError("菜单项目不存在")
                expected = payload.get("expected_meal_version")
                if not isinstance(expected, int) or isinstance(expected, bool) or expected != int(meal["version"]):
                    raise KitchenConflictError("菜单已经变化，请刷新", details={"current_version": int(meal["version"])})
                if meal["status"] not in {"accepted", "shopping", "cooking"}:
                    raise ConflictError("当前菜单状态不能开始烹饪")
                snapshot = json.loads(meal["recipe_snapshot_json"])
                recipe_id = meal["recipe_id"]
                source_meal_version = int(meal["version"])
                if meal["status"] != "cooking":
                    changed = db.execute(
                        """UPDATE meal_cart_items SET status='cooking',cook_user_id=?,version=version+1,updated_at=?
                           WHERE id=? AND version=? AND status IN ('accepted','shopping')""",
                        (user_id, now, meal_item_id, expected),
                    ).rowcount
                    if changed != 1:
                        raise KitchenConflictError("菜单已经变化，请刷新")
                    db.execute("UPDATE meal_carts SET version=version+1 WHERE id=?", (meal["cart_id"],))
                    source_meal_version += 1
                snapshot["_cook_context"] = {
                    "servings": int(meal["servings"]),
                    "source_meal_version": source_meal_version,
                }
            session_id = _id("cook")
            db.execute(
                """INSERT INTO cook_sessions(
                   id,couple_id,creator_id,recipe_id,meal_item_id,recipe_snapshot_json,visibility,status,
                   current_step,client_request_id,version,started_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,'active',0,?,1,?,?)""",
                (session_id, couple["id"], user_id, recipe_id, meal_item_id,
                 _json(snapshot), visibility, key, now, now),
            )
            row = db.execute("SELECT * FROM cook_sessions WHERE id=?", (session_id,)).fetchone()
            result = self._cook_session_dict(db, row, user_id)
            self._weekly_save(
                db, couple_id=couple["id"], user_id=user_id, operation=operation,
                idempotency_key=key, request_hash=request_hash, response=result,
            )
            if visibility == "shared":
                self._audit(db, "cook_session.started", "cook_session", session_id,
                            {"status": "active", "version": 1}, actor_id=user_id)
                self._event(db, couple["id"], user_id, "kitchen.updated", session_id,
                            {"resource": "cook_session", "operation": "started", "version": 1})
            return result

    def set_cook_session_step(self, session_id: str, payload: dict, user_id: str) -> dict:
        self._kitchen_fields(payload, COOK_STEP_FIELDS, "切换烹饪步骤")
        step = payload.get("step_index")
        expected = payload.get("expected_version")
        if not isinstance(step, int) or isinstance(step, bool) or step < 0:
            raise ValueError("步骤序号无效")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
            raise ValueError("烹饪会话版本无效")
        key, request_hash, operation = self._weekly_request(payload, "cook.step", session_id)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            replay = self._weekly_replay(db, couple_id=couple["id"], user_id=user_id,
                base_operation="cook.step", operation=operation, idempotency_key=key)
            if replay is not None:
                return replay
            row = self._cook_access_row(db, couple["id"], user_id, session_id)
            if row["status"] != "active":
                raise ConflictError("烹饪会话已经结束")
            steps = json.loads(row["recipe_snapshot_json"]).get("steps") or []
            if step >= len(steps):
                raise ValueError("步骤序号超出菜谱范围")
            if int(row["version"]) != expected:
                raise KitchenConflictError("烹饪会话已经变化，请刷新", details={"current_version": int(row["version"])})
            did_change = int(row["current_step"]) != step
            if did_change:
                db.execute(
                    "UPDATE cook_sessions SET current_step=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                    (step, _now(), session_id, expected),
                )
                row = db.execute("SELECT * FROM cook_sessions WHERE id=?", (session_id,)).fetchone()
            result = self._cook_session_dict(db, row, user_id)
            result["changed"] = did_change
            self._weekly_save(db, couple_id=couple["id"], user_id=user_id, operation=operation,
                idempotency_key=key, request_hash=request_hash, response=result)
            if int(row["version"]) != expected and row["visibility"] == "shared":
                self._audit(db, "cook_session.step", "cook_session", session_id,
                    {"step_index": step, "version": int(row["version"])}, actor_id=user_id)
                self._event(db, couple["id"], user_id, "kitchen.updated", session_id,
                    {"resource": "cook_session", "operation": "step", "version": int(row["version"])})
            return result

    def start_cook_timer(self, session_id: str, payload: dict, user_id: str) -> dict:
        self._kitchen_fields(payload, COOK_TIMER_FIELDS, "开始计时")
        step = payload.get("step_index")
        duration = payload.get("duration_seconds")
        if not isinstance(step, int) or isinstance(step, bool) or step < 0:
            raise ValueError("计时器步骤无效")
        if not isinstance(duration, int) or isinstance(duration, bool) or not 1 <= duration <= 604800:
            raise ValueError("计时时长必须在 1 秒到 7 天之间")
        label = _clean_text(payload.get("label"), 80, "计时器名称")
        key, request_hash, operation = self._weekly_request(payload, "cook.timer.start", session_id)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            replay = self._weekly_replay(db, couple_id=couple["id"], user_id=user_id,
                base_operation="cook.timer.start", operation=operation, idempotency_key=key)
            if replay is not None:
                return replay
            session = self._cook_access_row(db, couple["id"], user_id, session_id)
            if session["status"] != "active":
                raise ConflictError("烹饪会话已经结束")
            steps = json.loads(session["recipe_snapshot_json"]).get("steps") or []
            if step >= len(steps):
                raise ValueError("计时器步骤超出菜谱范围")
            started = datetime.now(timezone.utc).replace(microsecond=0)
            ends = started + timedelta(seconds=duration)
            timer_id = _id("cook_timer")
            db.execute(
                """INSERT INTO cook_timers(id,session_id,step_index,label,duration_seconds,started_at,ends_at,status,client_request_id,version,updated_at)
                   VALUES (?,?,?,?,?,?,?,'running',?,1,?)""",
                (timer_id, session_id, step, label, duration, started.isoformat(), ends.isoformat(), key, started.isoformat()),
            )
            timer = db.execute("SELECT * FROM cook_timers WHERE id=?", (timer_id,)).fetchone()
            result = self._cook_timer_dict(timer, started)
            self._weekly_save(db, couple_id=couple["id"], user_id=user_id, operation=operation,
                idempotency_key=key, request_hash=request_hash, response=result)
            if session["visibility"] == "shared":
                self._audit(db, "cook_timer.started", "cook_timer", timer_id,
                    {"status": "running", "step_index": step}, actor_id=user_id)
                self._event(db, couple["id"], user_id, "kitchen.updated", session_id,
                    {"resource": "cook_timer", "operation": "started", "timer_id": timer_id})
            return result

    def cancel_cook_timer(self, session_id: str, timer_id: str, payload: dict, user_id: str) -> dict:
        self._kitchen_fields(payload, COOK_TIMER_CANCEL_FIELDS, "取消计时")
        expected = payload.get("expected_version")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
            raise ValueError("计时器版本无效")
        key, request_hash, operation = self._weekly_request(payload, "cook.timer.cancel", f"{session_id}:{timer_id}")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            replay = self._weekly_replay(db, couple_id=couple["id"], user_id=user_id,
                base_operation="cook.timer.cancel", operation=operation, idempotency_key=key)
            if replay is not None:
                return replay
            session = self._cook_access_row(db, couple["id"], user_id, session_id)
            timer = db.execute("SELECT * FROM cook_timers WHERE id=? AND session_id=?", (timer_id, session_id)).fetchone()
            if not timer:
                raise KeyError("计时器不存在")
            effective = self._cook_timer_dict(timer)
            if effective["status"] != "running":
                raise ConflictError("计时器已经结束")
            if int(timer["version"]) != expected:
                raise KitchenConflictError("计时器已经变化，请刷新", details={"current_version": int(timer["version"])})
            db.execute("UPDATE cook_timers SET status='cancelled',version=version+1,updated_at=? WHERE id=? AND version=?",
                (_now(), timer_id, expected))
            result = self._cook_timer_dict(db.execute("SELECT * FROM cook_timers WHERE id=?", (timer_id,)).fetchone())
            self._weekly_save(db, couple_id=couple["id"], user_id=user_id, operation=operation,
                idempotency_key=key, request_hash=request_hash, response=result)
            if session["visibility"] == "shared":
                self._audit(db, "cook_timer.cancelled", "cook_timer", timer_id,
                    {"status": "cancelled"}, actor_id=user_id)
                self._event(db, couple["id"], user_id, "kitchen.updated", session_id,
                    {"resource": "cook_timer", "operation": "cancelled", "timer_id": timer_id})
            return result

    def _end_cook_session(self, session_id: str, payload: dict, user_id: str, status: str) -> dict:
        self._kitchen_fields(payload, COOK_END_FIELDS, "结束烹饪")
        expected = payload.get("expected_version")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
            raise ValueError("烹饪会话版本无效")
        base = f"cook.{status}"
        key, request_hash, operation = self._weekly_request(payload, base, session_id)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            replay = self._weekly_replay(db, couple_id=couple["id"], user_id=user_id,
                base_operation=base, operation=operation, idempotency_key=key)
            if replay is not None:
                return replay
            row = self._cook_access_row(db, couple["id"], user_id, session_id)
            if row["status"] != "active":
                raise ConflictError("烹饪会话已经结束")
            if int(row["version"]) != expected:
                raise KitchenConflictError("烹饪会话已经变化，请刷新", details={"current_version": int(row["version"])})
            now = _now()
            changed = db.execute(
                "UPDATE cook_sessions SET status=?,version=version+1,updated_at=?,ended_at=? WHERE id=? AND status='active' AND version=?",
                (status, now, now, session_id, expected),
            ).rowcount
            if changed != 1:
                raise KitchenConflictError("烹饪会话已经变化，请刷新")
            db.execute("UPDATE cook_timers SET status='cancelled',version=version+1,updated_at=? WHERE session_id=? AND status='running' AND ends_at>?",
                (now, session_id, now))
            db.execute("UPDATE cook_timers SET status='completed',version=version+1,updated_at=? WHERE session_id=? AND status='running' AND ends_at<=?",
                (now, session_id, now))
            if status == "completed" and row["meal_item_id"]:
                context = json.loads(row["recipe_snapshot_json"]).get("_cook_context") or {}
                meal_expected = context.get("source_meal_version")
                if not isinstance(meal_expected, int):
                    raise ConflictError("菜单版本快照缺失，请重新开始本次烹饪")
                meal_changed = db.execute(
                    """UPDATE meal_cart_items SET status='completed',version=version+1,updated_at=?
                       WHERE id=? AND status='cooking' AND version=?""",
                    (now, row["meal_item_id"], meal_expected),
                ).rowcount
                if meal_changed != 1:
                    raise KitchenConflictError("菜单已经被另一方修改，请刷新")
            current = db.execute("SELECT * FROM cook_sessions WHERE id=?", (session_id,)).fetchone()
            result = self._cook_session_dict(db, current, user_id)
            self._weekly_save(db, couple_id=couple["id"], user_id=user_id, operation=operation,
                idempotency_key=key, request_hash=request_hash, response=result)
            if row["visibility"] == "shared":
                self._audit(db, f"cook_session.{status}", "cook_session", session_id,
                    {"status": status, "version": int(current["version"])}, actor_id=user_id)
                self._event(db, couple["id"], user_id, "kitchen.updated", session_id,
                    {"resource": "cook_session", "operation": status, "version": int(current["version"])})
            return result

    def complete_cook_session(self, session_id: str, payload: dict, user_id: str) -> dict:
        return self._end_cook_session(session_id, payload, user_id, "completed")

    def cancel_cook_session(self, session_id: str, payload: dict, user_id: str) -> dict:
        return self._end_cook_session(session_id, payload, user_id, "cancelled")

    def apply_cook_pantry_updates(self, session_id: str, payload: dict, user_id: str) -> dict:
        self._kitchen_fields(payload, COOK_PANTRY_FIELDS, "确认烹饪后的库存")
        expected = payload.get("expected_version")
        updates = payload.get("updates")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
            raise ValueError("烹饪会话版本无效")
        if not isinstance(updates, list) or not 1 <= len(updates) <= 50:
            raise ValueError("请选择 1 到 50 项库存变化")
        refs: set[str] = set()
        for update in updates:
            if not isinstance(update, dict):
                raise ValueError("库存变化格式无效")
            self._kitchen_fields(update, COOK_PANTRY_UPDATE_FIELDS, "库存变化")
            ref = str(update.get("ingredient_ref") or "").strip()
            if not ref or ref in refs:
                raise ValueError("库存食材引用无效或重复")
            refs.add(ref)
            if update.get("state") not in {"low", "out"}:
                raise ValueError("烹饪后只能手工标记为少或无")
            version = update.get("expected_pantry_version")
            if not isinstance(version, int) or isinstance(version, bool) or version < 0:
                raise ValueError("库存版本无效")
        key, request_hash, operation = self._weekly_request(payload, "cook.pantry.apply", session_id)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            replay = self._weekly_replay(db, couple_id=couple["id"], user_id=user_id,
                base_operation="cook.pantry.apply", operation=operation, idempotency_key=key)
            if replay is not None:
                return replay
            session = self._cook_access_row(db, couple["id"], user_id, session_id)
            if session["status"] != "completed":
                raise ConflictError("只有完成烹饪后才能更新库存")
            if int(session["version"]) != expected:
                raise KitchenConflictError("烹饪会话已经变化，请刷新", details={"current_version": int(session["version"])})
            suggestion_list = self._cook_pantry_suggestions(
                db, couple["id"], json.loads(session["recipe_snapshot_json"]),
            )
            by_ref = {item["ingredient_ref"]: item for item in suggestion_list}
            for update in updates:
                suggestion = by_ref.get(str(update["ingredient_ref"]))
                if not suggestion:
                    raise ValueError("库存变化不是本次烹饪的食材")
                if int(suggestion["pantry_version"]) != int(update["expected_pantry_version"]):
                    raise KitchenConflictError("库存已被另一方修改，请刷新", details={
                        "ingredient_ref": update["ingredient_ref"],
                        "current_version": suggestion["pantry_version"],
                    })
            result_items: list[dict] = []
            for update in updates:
                suggestion = by_ref[str(update["ingredient_ref"])]
                catalog = self._resolve_ingredient_in_connection(
                    db, couple_id=couple["id"], name=suggestion["name"],
                    source_type="rules",
                )
                was_created = int(suggestion["pantry_version"]) == 0
                old_state = suggestion["pantry_state"]
                item, changed = self._set_pantry_state_in_connection(
                    db, couple_id=couple["id"], actor_id=user_id,
                    ingredient_id=catalog["id"], state=str(update["state"]),
                    metadata={}, expected_version=int(update["expected_pantry_version"]),
                )
                public_item = {
                    "id": item["id"], "ingredient_ref": update["ingredient_ref"],
                    "name": item["canonical_name"], "state": item["state"],
                    "version": item["version"], "changed": changed,
                }
                if changed:
                    public_item["undo"] = self.create_mutation_receipt(
                        db, actor_id=user_id, resource_type="pantry_item", resource_id=item["id"],
                        operation="pantry.set_state",
                        inverse={"delete_if_created": True} if was_created else {"state": old_state},
                        expected_post_version=item["version"],
                        client_request_id=f"{key}:{update['ingredient_ref']}",
                    )
                    result_items.append(public_item)
            now = _now()
            changed = db.execute(
                "UPDATE cook_sessions SET version=version+1,updated_at=? WHERE id=? AND version=? AND status='completed'",
                (now, session_id, expected),
            ).rowcount
            if changed != 1:
                raise KitchenConflictError("烹饪会话已经变化，请刷新")
            result = {"session_id": session_id, "version": expected + 1, "items": result_items}
            self._weekly_save(db, couple_id=couple["id"], user_id=user_id, operation=operation,
                idempotency_key=key, request_hash=request_hash, response=result)
            if session["visibility"] == "shared":
                self._audit(db, "cook_session.pantry_applied", "cook_session", session_id,
                    {"changed_count": sum(bool(item.get("changed")) for item in result_items), "version": expected + 1},
                    actor_id=user_id)
                self._event(db, couple["id"], user_id, "kitchen.updated", session_id,
                    {"resource": "cook_session", "operation": "pantry_applied", "version": expected + 1})
            return result
