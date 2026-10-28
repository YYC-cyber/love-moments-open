"""Shared recipe, meal calendar and grocery persistence for a couple."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .http_client import TRUSTED_SSL_CONTEXT
from .intelligent_ui import ConflictError
from .kitchen import CATEGORY_BY_LABEL, CATEGORY_KEYS, normalize_shopping_category, shopping_category_label
from .recipe_catalog_quality import allergen_report
from .recipes import (
    COURSE_ORDER,
    MEAL_SLOT_ORDER,
    MEAL_SLOTS,
    build_shopping_items,
    format_quantity,
    load_builtin_recipes,
    normalize_ingredient,
    normalize_name,
    parse_local_date,
    recipe_snapshot,
    shopping_diff,
    validate_detailed_recipe,
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _today() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _parse(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


@lru_cache(maxsize=1)
def _builtin_cover_manifest() -> dict[str, dict]:
    """Load curated, locally cached built-in recipe covers.

    This file is generated only after an exact-title and image quality audit.  Keeping
    it outside the seed database lets the catalog gain covers without rewriting the
    user's recipe rows or pretending an unverified image is recipe media.
    """

    path = Path(__file__).resolve().parent / "data" / "recipe_cover_manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


class MealConflictError(ConflictError):
    def __init__(self, message: str, code: str = "VERSION_CONFLICT", details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class MealRecipeMixin:
    """Mixin relying on LoveMomentsStore connection, audit and event helpers."""

    # ---- Built-in data and recipe CRUD ---------------------------------

    @staticmethod
    def _dish_key(scope: str, cuisine: str, normalized_name: str) -> str:
        digest = hashlib.sha256(f"{scope}:{cuisine}:{normalized_name}".encode("utf-8")).hexdigest()[:24]
        return f"dish_{digest}"

    def _ensure_recipe_dish(
        self,
        db: sqlite3.Connection,
        value: dict,
        couple_id: str | None,
        owner_id: str | None,
        visibility: str,
        requested_dish_id: str | None = None,
    ) -> str:
        if requested_dish_id:
            row = db.execute("SELECT * FROM recipe_dishes WHERE id=?", (requested_dish_id,)).fetchone()
            if not row:
                raise KeyError("菜品分组不存在")
            allowed = row["visibility"] == "system" or row["couple_id"] == couple_id or row["owner_id"] == owner_id
            if not allowed:
                raise PermissionError("无权向该菜品添加做法")
            return str(row["id"])
        scope = couple_id or owner_id or "system"
        normalized = str(value.get("normalized_dish_name") or normalize_name(value.get("dish_name") or value["name"]))
        dish_id = self._dish_key(scope, value["cuisine"], normalized)
        now = _now()
        db.execute(
            """
            INSERT OR IGNORE INTO recipe_dishes(
                id,couple_id,owner_id,canonical_name,normalized_name,cuisine,category,visibility,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                dish_id, couple_id, owner_id, value.get("dish_name") or value["name"], normalized,
                value["cuisine"], value["category"], visibility, now, now,
            ),
        )
        return dish_id

    def _seed_builtin_recipes(self) -> None:
        recipes = load_builtin_recipes()
        now = _now()
        with self.connection() as db:
            for value in recipes:
                dish_id = self._ensure_recipe_dish(db, value, None, None, "system")
                current = db.execute("SELECT version,source_type FROM recipes WHERE id=?", (value["id"],)).fetchone()
                if current and (current["source_type"] != "built_in" or int(current["version"]) >= value["data_version"]):
                    continue
                if current:
                    db.execute(
                        """
                        UPDATE recipes SET dish_id=?,name=?,normalized_name=?,variant_name=?,cooking_method=?,method_tags_json=?,cuisine=?,category=?,meal_slots_json=?,
                            base_servings=?,prep_minutes=?,cook_minutes=?,passive_minutes=?,difficulty=?,tips_json=?,
                            allergen_tags_json=?,version=?,deleted_at=NULL,updated_at=? WHERE id=?
                        """,
                        (
                            dish_id, value["name"], value["normalized_name"], value["variant_name"], value["cooking_method"], _json(value["method_tags"]), value["cuisine"], value["category"], _json(value["meal_slots"]),
                            value["base_servings"], value["prep_minutes"], value["cook_minutes"], value["passive_minutes"], value["difficulty"],
                            _json(value["tips"]), _json(value["allergen_tags"]), value["data_version"], now, value["id"],
                        ),
                    )
                    db.execute("DELETE FROM recipe_ingredients WHERE recipe_id=?", (value["id"],))
                    db.execute("DELETE FROM recipe_steps WHERE recipe_id=?", (value["id"],))
                else:
                    db.execute(
                        """
                        INSERT INTO recipes(
                            id,dish_id,couple_id,owner_id,name,normalized_name,variant_name,cooking_method,method_tags_json,cuisine,category,meal_slots_json,
                            base_servings,prep_minutes,cook_minutes,passive_minutes,difficulty,tips_json,allergen_tags_json,
                            source_type,source_provider,source_ref,visibility,version,created_at,updated_at
                        ) VALUES (?,?,NULL,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'built_in','local','','system',?,?,?)
                        """,
                        (
                            value["id"], dish_id, value["name"], value["normalized_name"], value["variant_name"], value["cooking_method"], _json(value["method_tags"]), value["cuisine"], value["category"],
                            _json(value["meal_slots"]), value["base_servings"], value["prep_minutes"], value["cook_minutes"],
                            value["passive_minutes"], value["difficulty"], _json(value["tips"]), _json(value["allergen_tags"]),
                            value["data_version"], now, now,
                        ),
                    )
                self._replace_recipe_parts(db, value["id"], value)

    @staticmethod
    def _replace_recipe_parts(db: sqlite3.Connection, recipe_id: str, value: dict) -> None:
        db.execute("DELETE FROM recipe_ingredients WHERE recipe_id=?", (recipe_id,))
        db.execute("DELETE FROM recipe_steps WHERE recipe_id=?", (recipe_id,))
        for ingredient in value["ingredients"]:
            db.execute(
                """
                INSERT INTO recipe_ingredients(
                    id,recipe_id,name,canonical_name,quantity_milli,quantity_text,canonical_unit,
                    unit_family,specification,preparation,ingredient_group,optional,position
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"{recipe_id}_ingredient_{ingredient['position']}", recipe_id, ingredient["name"], ingredient["canonical_name"],
                    ingredient["quantity_milli"], ingredient["quantity_text"], ingredient["canonical_unit"],
                    ingredient["unit_family"], ingredient["specification"], ingredient["preparation"],
                    ingredient["ingredient_group"], ingredient["optional"], ingredient["position"],
                ),
            )
        for step in value["steps"]:
            db.execute(
                "INSERT INTO recipe_steps(id,recipe_id,instruction,timer_seconds,title,heat_level,key_point,position) VALUES (?,?,?,?,?,?,?,?)",
                (
                    f"{recipe_id}_step_{step['position']}", recipe_id, step["instruction"], step["timer_seconds"],
                    step["title"], step["heat_level"], step["key_point"], step["position"],
                ),
            )

    @staticmethod
    def _recipe_media_dict(row: sqlite3.Row) -> dict:
        value = dict(row)
        value["match_details"] = _parse(value.pop("match_details_json", "{}"), {})
        value["can_play_inline"] = value["media_type"] == "video" and value["provider"] in {"local", "qiniu"}
        value["is_external"] = value["provider"] not in {"local", "qiniu"}
        return value

    def _insert_recipe_media_candidates(
        self,
        db: sqlite3.Connection,
        recipe_id: str,
        media: list[dict],
        user_id: str | None,
    ) -> None:
        now = _now()
        next_check = (datetime.now(timezone.utc) + timedelta(days=30)).replace(microsecond=0).isoformat()
        for item in media:
            if item["media_type"] == "image" and item.get("match_status") != "verified":
                continue
            db.execute(
                """
                INSERT INTO recipe_media(
                    id,recipe_id,media_type,role,provider,url,thumbnail_url,title,attribution,license_note,
                    match_details_json,match_status,availability_status,next_check_at,created_by,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'unknown',?,?,?,?)
                """,
                (
                    _id("recipe_media"), recipe_id, item["media_type"], item["role"], item.get("provider", "")[:60],
                    item["url"], item.get("thumbnail_url", ""), item.get("title", ""), item.get("attribution", ""),
                    item.get("license_note", ""), _json(item.get("match_details", {})),
                    item.get("match_status", "unverified"), next_check, user_id, now, now,
                ),
            )
            local_id = self._local_media_id(item["url"])
            if local_id:
                self._sync_media_visibility(db, local_id)

    @staticmethod
    def _recipe_row_to_dict(db: sqlite3.Connection, row: sqlite3.Row, include_details: bool = True) -> dict:
        value = dict(row)
        value["meal_slots"] = _parse(value.pop("meal_slots_json", "[]"), [])
        value["method_tags"] = _parse(value.pop("method_tags_json", "[]"), [])
        value["tips"] = _parse(value.pop("tips_json", "[]"), [])
        value["allergen_tags"] = _parse(value.pop("allergen_tags_json", "[]"), [])
        dish = db.execute("SELECT canonical_name FROM recipe_dishes WHERE id=?", (row["dish_id"],)).fetchone() if row["dish_id"] else None
        value["dish_name"] = dish["canonical_name"] if dish else value["name"]
        value["variant_count"] = int(db.execute(
            "SELECT COUNT(*) FROM recipes WHERE dish_id=? AND deleted_at IS NULL", (row["dish_id"],)
        ).fetchone()[0]) if row["dish_id"] else 1
        value["active_minutes"] = int(value["prep_minutes"]) + int(value["cook_minutes"])
        value["total_minutes"] = value["active_minutes"] + int(value.get("passive_minutes", 0))
        cover = db.execute(
            """
            SELECT url,thumbnail_url,attribution,provider FROM recipe_media
            WHERE recipe_id=? AND media_type='image' AND role='cover' AND match_status='verified'
                AND availability_status NOT IN ('unavailable')
            ORDER BY created_at DESC LIMIT 1
            """,
            (row["id"],),
        ).fetchone()
        value["cover"] = dict(cover) if cover else None
        if value["cover"] is None and value.get("source_type") == "built_in":
            curated = _builtin_cover_manifest().get(str(row["id"]))
            if curated and curated.get("url"):
                value["cover"] = {
                    "url": str(curated["url"]),
                    "thumbnail_url": str(curated.get("url") or ""),
                    "attribution": str(curated.get("attribution") or ""),
                    "provider": str(curated.get("provider") or "openverse"),
                }
        if include_details:
            value["ingredients"] = [
                dict(item) for item in db.execute(
                    "SELECT * FROM recipe_ingredients WHERE recipe_id=? ORDER BY position", (row["id"],)
                ).fetchall()
            ]
            for ingredient in value["ingredients"]:
                ingredient["display_quantity"] = format_quantity(
                    ingredient["quantity_milli"], ingredient["canonical_unit"], ingredient["quantity_text"]
                )
            evidence = allergen_report({str(item.get("name", "")) for item in value["ingredients"]})
            value["allergen_tags"] = [item["name"] for item in evidence["allergens"]]
            value["allergen_details"] = evidence["allergens"]
            value["allergen_review_ingredients"] = evidence["review_ingredients"]
            value["allergen_note"] = evidence["note"]
            value["steps"] = [
                dict(item) for item in db.execute(
                    "SELECT * FROM recipe_steps WHERE recipe_id=? ORDER BY position", (row["id"],)
                ).fetchall()
            ]
            value["media"] = [self_value for self_value in (
                MealRecipeMixin._recipe_media_dict(item) for item in db.execute(
                    "SELECT * FROM recipe_media WHERE recipe_id=? AND match_status!='rejected' ORDER BY media_type,role,created_at",
                    (row["id"],),
                ).fetchall()
            )]
            value["variants"] = [dict(item) for item in db.execute(
                """
                SELECT id,name,variant_name,cooking_method,method_tags_json,prep_minutes,cook_minutes,passive_minutes,difficulty
                FROM recipes WHERE dish_id=? AND deleted_at IS NULL ORDER BY updated_at DESC,id
                """,
                (row["dish_id"],),
            ).fetchall()] if row["dish_id"] else []
            for variant in value["variants"]:
                variant["method_tags"] = _parse(variant.pop("method_tags_json", "[]"), [])
        return value

    def _get_visible_recipe_row(self, db: sqlite3.Connection, recipe_id: str, couple_id: str, user_id: str) -> sqlite3.Row:
        row = db.execute(
            """
            SELECT * FROM recipes WHERE id=? AND deleted_at IS NULL AND (
                visibility='system' OR
                (couple_id=? AND visibility='couple') OR
                (owner_id=? AND visibility='private')
            )
            """,
            (recipe_id, couple_id, user_id),
        ).fetchone()
        if not row:
            raise KeyError("菜谱不存在或无权查看")
        return row

    def list_recipes(
        self,
        user_id: str,
        query: str = "",
        cuisine: str = "",
        meal_slot: str = "",
        difficulty: str = "",
        max_minutes: object = "",
        cursor: object = 0,
        limit: object = 20,
    ) -> dict:
        keyword = str(query or "").strip()[:80]
        cuisine = str(cuisine or "").strip().lower()
        meal_slot = str(meal_slot or "").strip().lower()
        difficulty = str(difficulty or "").strip().lower()
        if cuisine and cuisine not in {"chinese", "western"}:
            raise ValueError("菜系筛选无效")
        if meal_slot and meal_slot not in MEAL_SLOTS:
            raise ValueError("餐次筛选无效")
        if difficulty and difficulty not in {"easy", "medium", "hard"}:
            raise ValueError("难度筛选无效")
        try:
            offset = max(0, int(cursor or 0))
            page_size = max(1, min(int(limit or 20), 50))
        except (TypeError, ValueError) as error:
            raise ValueError("分页参数无效") from error
        try:
            maximum_minutes = int(max_minutes) if str(max_minutes or "").strip() else 0
        except (TypeError, ValueError) as error:
            raise ValueError("用时筛选无效") from error
        if maximum_minutes < 0 or maximum_minutes > 2880:
            raise ValueError("用时筛选无效")
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            params: list[Any] = [couple["id"], user_id]
            sql = """
                SELECT * FROM recipes WHERE deleted_at IS NULL AND (
                    visibility='system' OR (couple_id=? AND visibility='couple') OR
                    (owner_id=? AND visibility='private')
                )
            """
            if keyword:
                sql += " AND (name LIKE ? OR category LIKE ?)"
                params.extend([f"%{keyword}%", f"%{keyword}%"])
            if cuisine:
                sql += " AND cuisine=?"
                params.append(cuisine)
            if meal_slot:
                sql += " AND meal_slots_json LIKE ?"
                params.append(f'%"{meal_slot}"%')
            if difficulty:
                sql += " AND difficulty=?"
                params.append(difficulty)
            if maximum_minutes:
                sql += " AND (prep_minutes+cook_minutes+passive_minutes)<=?"
                params.append(maximum_minutes)
            count = db.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
            rows = db.execute(
                sql + " ORDER BY source_type!='built_in', cuisine, category, name LIMIT ? OFFSET ?",
                (*params, page_size, offset),
            ).fetchall()
            items = [self._recipe_row_to_dict(db, row, include_details=False) for row in rows]
        next_cursor = str(offset + len(items)) if offset + len(items) < int(count) else None
        return {"items": items, "next_cursor": next_cursor, "total": int(count)}

    def get_recipe(self, recipe_id: str, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = self._get_visible_recipe_row(db, recipe_id, couple["id"], user_id)
            result = self._recipe_row_to_dict(db, row)
            result["food_safety"] = self._evaluate_recipe_food_safety_in_connection(
                db, couple_id=couple["id"], recipe_snapshot=result,
            )
            return result

    def list_recipe_variants(self, dish_id: str, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            dish = db.execute("SELECT * FROM recipe_dishes WHERE id=?", (dish_id,)).fetchone()
            if not dish:
                raise KeyError("菜品不存在")
            rows = db.execute(
                """
                SELECT * FROM recipes WHERE dish_id=? AND deleted_at IS NULL AND (
                    visibility='system' OR (couple_id=? AND visibility='couple') OR
                    (owner_id=? AND visibility='private')
                ) ORDER BY source_type!='built_in',updated_at DESC,id
                """,
                (dish_id, couple["id"], user_id),
            ).fetchall()
            if not rows:
                raise KeyError("菜品不存在或无权查看")
            items = [self._recipe_row_to_dict(db, row, include_details=False) for row in rows]
        return {"dish": dict(dish), "items": items, "count": len(items)}

    def compare_recipe_variants(self, payload: dict, user_id: str) -> dict:
        recipe_ids = payload.get("recipe_ids")
        if not isinstance(recipe_ids, list) or not 2 <= len(recipe_ids) <= 3:
            raise ValueError("请选择 2 到 3 种做法进行比较")
        recipe_ids = list(dict.fromkeys(str(value) for value in recipe_ids))
        if len(recipe_ids) < 2:
            raise ValueError("请选择不同的做法")
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            recipes = [
                self._recipe_row_to_dict(db, self._get_visible_recipe_row(db, recipe_id, couple["id"], user_id))
                for recipe_id in recipe_ids
            ]
        if len({recipe["dish_id"] for recipe in recipes}) != 1:
            raise ValueError("只能比较同一菜品的不同做法")
        names = sorted({item["canonical_name"] for recipe in recipes for item in recipe["ingredients"]})
        ingredient_matrix = []
        for name in names:
            values = []
            for recipe in recipes:
                item = next((value for value in recipe["ingredients"] if value["canonical_name"] == name), None)
                values.append(item["display_quantity"] if item else "不使用")
            if len(set(values)) > 1:
                ingredient_matrix.append({"name": name, "values": values})
        return {
            "dish_name": recipes[0]["dish_name"],
            "items": [{
                "id": item["id"], "variant_name": item["variant_name"], "cooking_method": item["cooking_method"],
                "method_tags": item["method_tags"], "total_minutes": item["total_minutes"],
                "difficulty": item["difficulty"], "base_servings": item["base_servings"],
            } for item in recipes],
            "ingredient_differences": ingredient_matrix,
        }

    @staticmethod
    def _media_host_allowed(url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        allowed = {
            "bilibili.com", "b23.tv", "douyin.com", "iesdouyin.com", "xiaohongshu.com", "xhslink.com",
            "themealdb.com", "spoonacular.com", "img.spoonacular.com",
        }
        return bool(host) and any(host == value or host.endswith(f".{value}") for value in allowed)

    @staticmethod
    def _external_recipe_image_host_allowed(url: str) -> bool:
        """Allow only image hosts belonging to a configured recipe source.

        Video/search allowlists are intentionally not reused here: a remote
        recipe image is downloaded by the server, so the narrower list also
        acts as an SSRF boundary.
        """

        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        allowed = {
            "themealdb.com", "spoonacular.com", "img.spoonacular.com",
            "tianapi.com", "tianapi.cn", "apis.tianapi.com",
        }
        return bool(host) and any(host == value or host.endswith(f".{value}") for value in allowed)

    def _cache_external_recipe_cover(self, value: dict, user_id: str) -> dict:
        """Copy one exact provider-bound cover into protected media storage.

        The provider result supplies image, ingredients and instructions as a
        single record.  We never search by dish name or substitute an image
        from another result.  The original URL and attribution remain in the
        matching evidence for auditing.
        """

        covers = [
            item for item in value.get("media", [])
            if item.get("media_type") == "image" and item.get("role") == "cover"
            and item.get("match_status") == "verified"
        ]
        if not covers:
            return value
        cover = covers[0]
        if self._local_media_id(str(cover.get("url") or "")):
            return value
        source_url = str(cover.get("url") or "").strip()
        details = cover.get("match_details") or {}
        if details.get("binding") != "provider_recipe_result":
            raise ValueError("外部封面缺少与原菜谱记录的绑定证明，请上传并人工确认准确图片")
        if not self._external_recipe_image_host_allowed(source_url):
            raise ValueError("外部封面来源不在可信菜谱图片范围，请上传并人工确认准确图片")
        maximum = 20 * 1024 * 1024
        request = urlrequest.Request(
            source_url,
            headers={"Accept": "image/jpeg,image/png,image/webp,image/gif", "User-Agent": "LoveMoments/1.0"},
        )
        try:
            with urlrequest.urlopen(request, timeout=20, context=TRUSTED_SSL_CONTEXT) as response:
                final_url = str(response.geturl() or source_url)
                if not self._external_recipe_image_host_allowed(final_url):
                    raise ValueError("外部图片跳转到了未授权地址")
                content_type = str(response.headers.get_content_type() or "").lower()
                if content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
                    raise ValueError("外部菜谱返回的不是可识别图片")
                declared_length = response.headers.get("Content-Length")
                if declared_length and int(declared_length) > maximum:
                    raise ValueError("外部菜谱图片过大")
                data = response.read(maximum + 1)
        except (urlerror.URLError, TimeoutError, OSError) as exc:
            raise ValueError("外部菜谱图片下载失败，请稍后重试或上传准确成品图") from exc
        if len(data) > maximum:
            raise ValueError("外部菜谱图片过大")
        stored = self.save_media(
            data,
            f"{value.get('dish_name') or value.get('name') or '菜谱'}-{value.get('variant_name') or '做法'}",
            content_type,
            "shared",
            user_id,
        )
        cached_cover = {
            **cover,
            "url": stored["url"],
            "thumbnail_url": stored["thumbnail_url"],
            "provider": "local",
            "license_note": (str(cover.get("license_note") or "") + "；原图已缓存到双方私有媒体空间").strip("；"),
            "match_details": {
                **details,
                "source_url": source_url,
                "cached_media_id": stored["id"],
            },
        }
        return {**value, "media": [cached_cover, *[item for item in value.get("media", []) if item is not cover]]}

    @staticmethod
    def _local_media_id(url: str) -> str | None:
        match = re.fullmatch(r"/api/v1/media/(media_[A-Za-z0-9_-]+)", url)
        return match.group(1) if match else None

    @staticmethod
    def _recipe_alignment_signature(value: dict) -> str:
        """Fingerprint only the facts a paired tutorial must agree with."""

        payload = {
            "ingredients": [{
                "name": item.get("canonical_name") or item.get("name"),
                "quantity_milli": item.get("quantity_milli"),
                "quantity_text": item.get("quantity_text", ""),
                "unit": item.get("canonical_unit", ""),
                "preparation": item.get("preparation", ""),
            } for item in value.get("ingredients", [])],
            "steps": [{
                "instruction": item.get("instruction", ""),
                "timer_seconds": item.get("timer_seconds"),
                "heat_level": item.get("heat_level", ""),
                "key_point": item.get("key_point", ""),
            } for item in value.get("steps", [])],
        }
        return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_local_recipe_image(
        db: sqlite3.Connection, url: str, couple_id: str, user_id: str,
    ) -> sqlite3.Row | None:
        media_id = MealRecipeMixin._local_media_id(url)
        if not media_id:
            return None
        row = db.execute(
            """
            SELECT * FROM media_assets
            WHERE id=? AND couple_id=? AND owner_id=? AND kind='image'
                AND status='ready' AND purged_at IS NULL
            """,
            (media_id, couple_id, user_id),
        ).fetchone()
        if not row:
            raise PermissionError("只能使用你刚上传、仍可访问的图片作为菜谱封面")
        return row

    def add_recipe_media(self, recipe_id: str, payload: dict, user_id: str) -> dict:
        media_type = str(payload.get("media_type", "video")).strip().lower()
        role = str(payload.get("role", "reference")).strip().lower()
        url = str(payload.get("url") or "").strip()
        if media_type not in {"image", "video", "link"}:
            raise ValueError("媒体类型无效")
        if role not in {"cover", "step", "paired", "reference", "search"}:
            raise ValueError("媒体用途无效")
        parsed = urlsplit(url)
        local_media_id = self._local_media_id(url)
        if len(url) > 2000 or not local_media_id and (parsed.scheme not in {"http", "https"} or not parsed.hostname):
            raise ValueError("媒体链接无效")
        confirmed_exact = payload.get("confirmed_exact") is True
        if media_type == "image" and not confirmed_exact:
            raise ValueError("请先确认图片就是当前这种做法，不会使用相似菜图冒充")
        match_status = "verified" if confirmed_exact else "reference"
        if media_type == "video" and role == "paired" and not confirmed_exact:
            raise ValueError("配套视频必须先人工核对食材和做法")
        try:
            alignment_score = float(payload.get("alignment_score", 1 if confirmed_exact else 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("教学匹配度无效") from exc
        if not 0 <= alignment_score <= 1:
            raise ValueError("教学匹配度必须在 0 到 1 之间")
        if media_type == "video" and role == "paired" and alignment_score < 0.8:
            raise ValueError("食材、调料或流程差异较大，只能作为参考视频")
        if media_type == "video" and role == "paired" and not (
            payload.get("checked_ingredients") is True and payload.get("checked_steps") is True
        ):
            raise ValueError("配套视频必须逐项核对食材用量、备菜处理和教学步骤")
        differences = payload.get("differences", [])
        if not isinstance(differences, list) or len(differences) > 20:
            raise ValueError("教学差异说明无效")
        match_details = {
            "alignment_score": round(alignment_score, 3),
            "differences": [str(value).strip()[:200] for value in differences if str(value).strip()],
            "checked_ingredients": bool(payload.get("checked_ingredients", confirmed_exact)),
            "checked_steps": bool(payload.get("checked_steps", confirmed_exact)),
        }
        provider = str(payload.get("provider") or ("local" if local_media_id else parsed.hostname)).strip()[:60]
        now = _now()
        next_check = (datetime.now(timezone.utc) + timedelta(days=30)).replace(microsecond=0).isoformat()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            recipe = self._get_visible_recipe_row(db, recipe_id, couple["id"], user_id)
            recipe_detail = self._recipe_row_to_dict(db, recipe)
            match_details["recipe_version"] = int(recipe["version"])
            match_details["recipe_alignment_signature"] = self._recipe_alignment_signature(recipe_detail)
            if local_media_id:
                if media_type != "image":
                    raise ValueError("站内上传目前只用于菜谱图片")
                self._validate_local_recipe_image(db, url, couple["id"], user_id)
            if recipe["source_type"] == "built_in":
                raise PermissionError("内置菜谱需要先复制成双方菜谱，再添加私人媒体")
            media_id = _id("recipe_media")
            availability = "healthy" if local_media_id else ("unknown" if self._media_host_allowed(url) else "manual_check")
            db.execute(
                """
                INSERT INTO recipe_media(
                    id,recipe_id,media_type,role,provider,url,thumbnail_url,title,attribution,license_note,
                    match_details_json,match_status,availability_status,next_check_at,created_by,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    media_id, recipe_id, media_type, role, provider, url,
                    str(payload.get("thumbnail_url") or "")[:2000], str(payload.get("title") or "")[:160],
                    str(payload.get("attribution") or "")[:200], str(payload.get("license_note") or "")[:200],
                    _json(match_details), match_status, availability, next_check, user_id, now, now,
                ),
            )
            result = self._recipe_media_dict(db.execute("SELECT * FROM recipe_media WHERE id=?", (media_id,)).fetchone())
            if local_media_id:
                self._sync_media_visibility(db, local_media_id)
            self._event(db, couple["id"], user_id, "meals.updated", recipe_id, {"resource": "recipe_media", "operation": "created"})
        return result

    def copy_recipe(self, recipe_id: str, payload: dict, user_id: str) -> dict:
        """Copy a visible recipe into the couple/private library before personal media is attached."""
        visibility = str(payload.get("visibility", "couple"))
        if visibility not in {"couple", "private"}:
            raise ValueError("菜谱可见性无效")
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            source_row = self._get_visible_recipe_row(db, recipe_id, couple["id"], user_id)
            source = self._recipe_row_to_dict(db, source_row)
            value = validate_detailed_recipe({
                **source,
                "dish_id": "",
                # Do not silently copy third-party media into a personal recipe.
                # The user can explicitly attach an exact cover/video afterwards.
                "media": [],
            })
            result = self._insert_recipe(
                db, value, couple["id"], user_id, source_type="user", visibility=visibility,
                source_provider="copied", source_ref=recipe_id,
            )
            self._audit(db, "recipe.copied", "recipe", result["id"], {"source_recipe_id": recipe_id}, actor_id=user_id)
            if visibility == "couple":
                self._event(db, couple["id"], user_id, "meals.updated", result["id"], {"resource": "recipe", "operation": "created", "version": 1})
        return result

    def check_recipe_media(self, media_id: str, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute(
                """
                SELECT m.* FROM recipe_media m JOIN recipes r ON r.id=m.recipe_id
                WHERE m.id=? AND r.deleted_at IS NULL AND (
                    r.visibility='system' OR (r.couple_id=? AND r.visibility='couple') OR
                    (r.owner_id=? AND r.visibility='private')
                )
                """,
                (media_id, couple["id"], user_id),
            ).fetchone()
            if not row:
                raise KeyError("媒体记录不存在")
            value = dict(row)
        if not self._media_host_allowed(value["url"]):
            return {**self._recipe_media_dict(row), "availability_status": "manual_check", "check_result": "该来源需人工打开确认"}
        ok, status_code, result, error_type = False, None, "", ""
        try:
            req = urlrequest.Request(value["url"], headers={"User-Agent": "LoveMoments-LinkCheck/1.0"}, method="HEAD")
            with urlrequest.urlopen(req, timeout=8, context=TRUSTED_SSL_CONTEXT) as response:
                status_code = int(response.status)
                ok = 200 <= status_code < 400
                result = "链接正常" if ok else f"HTTP {status_code}"
        except urlerror.HTTPError as exc:
            status_code = int(exc.code)
            if status_code in {401, 403, 405, 429}:
                result, error_type = "平台限制自动检查，请人工打开", "platform_restricted"
            else:
                result, error_type = f"HTTP {status_code}", "http_error"
        except (urlerror.URLError, TimeoutError, OSError) as exc:
            result, error_type = "连接超时或网络异常", type(exc).__name__
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        now = now_dt.isoformat()
        with self.connection() as db:
            current = db.execute("SELECT * FROM recipe_media WHERE id=?", (media_id,)).fetchone()
            failures = 0 if ok else int(current["consecutive_failures"] or 0) + 1
            if ok:
                availability, next_check = "healthy", now_dt + timedelta(days=30)
            elif error_type == "platform_restricted":
                availability, next_check = "manual_check", now_dt + timedelta(days=30)
            else:
                availability = "unavailable" if failures >= 2 else "suspect"
                next_check = now_dt + timedelta(days=1 if failures < 2 else 30)
            db.execute(
                """
                UPDATE recipe_media SET availability_status=?,consecutive_failures=?,last_checked_at=?,
                    last_ok_at=CASE WHEN ? THEN ? ELSE last_ok_at END,next_check_at=?,updated_at=? WHERE id=?
                """,
                (availability, failures, now, int(ok), now, next_check.isoformat(), now, media_id),
            )
            db.execute(
                "INSERT INTO recipe_media_checks(id,media_id,checked_at,ok,status_code,result,error_type) VALUES (?,?,?,?,?,?,?)",
                (_id("media_check"), media_id, now, int(ok), status_code, result, error_type),
            )
            updated = self._recipe_media_dict(db.execute("SELECT * FROM recipe_media WHERE id=?", (media_id,)).fetchone())
        updated["check_result"] = result
        return updated

    def check_due_recipe_media(self, *, limit: int = 100) -> dict:
        now = _now()
        with self.connection() as db:
            rows = db.execute(
                """
                SELECT id,created_by FROM recipe_media
                WHERE media_type IN ('video','link') AND next_check_at<=? AND availability_status!='manual_check'
                ORDER BY next_check_at LIMIT ?
                """,
                (now, max(1, min(int(limit), 500))),
            ).fetchall()
        checked, failed = 0, 0
        for row in rows:
            if not row["created_by"]:
                continue
            try:
                self.check_recipe_media(row["id"], row["created_by"])
                checked += 1
            except (KeyError, PermissionError, ValueError, OSError):
                failed += 1
        return {"checked": checked, "failed": failed, "due": len(rows)}

    def _insert_recipe(
        self,
        db: sqlite3.Connection,
        value: dict,
        couple_id: str,
        owner_id: str,
        *,
        source_type: str,
        visibility: str,
        source_provider: str = "",
        source_ref: str = "",
    ) -> dict:
        recipe_id = _id("recipe")
        now = _now()
        dish_id = self._ensure_recipe_dish(
            db, value, couple_id, owner_id, visibility, value.get("dish_id") or None,
        )
        db.execute(
            """
            INSERT INTO recipes(
                id,dish_id,couple_id,owner_id,name,normalized_name,variant_name,cooking_method,method_tags_json,cuisine,category,meal_slots_json,
                base_servings,prep_minutes,cook_minutes,passive_minutes,difficulty,tips_json,allergen_tags_json,
                source_type,source_provider,source_ref,visibility,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                recipe_id, dish_id, couple_id, owner_id, value["name"], value["normalized_name"], value["variant_name"],
                value["cooking_method"], _json(value["method_tags"]), value["cuisine"], value["category"],
                _json(value["meal_slots"]), value["base_servings"], value["prep_minutes"], value["cook_minutes"], value["passive_minutes"], value["difficulty"],
                _json(value["tips"]), _json(value["allergen_tags"]), source_type, source_provider[:60], source_ref[:300], visibility, now, now,
            ),
        )
        self._replace_recipe_parts(db, recipe_id, value)
        self._insert_recipe_media_candidates(db, recipe_id, value.get("media", []), owner_id)
        return self._recipe_row_to_dict(db, db.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone())

    def create_recipe(self, payload: dict, user_id: str) -> dict:
        value = validate_detailed_recipe(payload)
        verified_covers = [
            media for media in value.get("media", [])
            if media["media_type"] == "image" and media["role"] == "cover" and media["match_status"] == "verified"
        ]
        if not verified_covers:
            raise ValueError("每份新菜谱都必须提供并确认一张与当前做法准确对应的成品图")
        visibility = str(payload.get("visibility", "couple"))
        if visibility not in {"couple", "private"}:
            raise ValueError("菜谱可见性无效")
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            for media in verified_covers:
                if not self._validate_local_recipe_image(db, media["url"], couple["id"], user_id):
                    raise ValueError("自建菜谱封面必须先上传到双方媒体空间并人工确认，不能直接填写外链")
            result = self._insert_recipe(db, value, couple["id"], user_id, source_type="user", visibility=visibility)
            self._audit(db, "recipe.created", "recipe", result["id"], {"visibility": visibility}, actor_id=user_id)
            if visibility == "couple":
                self._event(db, couple["id"], user_id, "meals.updated", result["id"], {"resource": "recipe", "operation": "created", "version": 1})
        return result

    def update_recipe(self, recipe_id: str, payload: dict, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = self._get_visible_recipe_row(db, recipe_id, couple["id"], user_id)
            if row["source_type"] == "built_in":
                raise PermissionError("内置菜谱不可修改，可以复制后编辑")
            if row["owner_id"] != user_id:
                raise PermissionError("只有菜谱创建者可以修改")
            if payload.get("expected_version") is not None and int(payload["expected_version"]) != int(row["version"]):
                raise MealConflictError("菜谱已被修改，请刷新", details={"current_version": row["version"]})
            current = self._recipe_row_to_dict(db, row)
            merged = {**current, **payload}
            value = validate_detailed_recipe(merged)
            identity_changed = any([
                normalize_name(current.get("name")) != normalize_name(value.get("name")),
                str(current.get("variant_name") or "") != str(value.get("variant_name") or ""),
                str(current.get("cooking_method") or "") != str(value.get("cooking_method") or ""),
            ])
            alignment_changed = self._recipe_alignment_signature(current) != self._recipe_alignment_signature(value)
            replacement_cover = payload.get("replacement_cover")
            if identity_changed:
                if not isinstance(replacement_cover, dict) or replacement_cover.get("confirmed_exact") is not True:
                    raise ValueError("修改菜品名或做法版本时，必须同时上传并确认新的准确成品图")
                replacement_url = str(replacement_cover.get("url") or "").strip()
                if not self._validate_local_recipe_image(db, replacement_url, couple["id"], user_id):
                    raise ValueError("新的成品图必须先上传到双方媒体空间")
            visibility = str(payload.get("visibility", row["visibility"]))
            if visibility not in {"couple", "private"}:
                raise ValueError("菜谱可见性无效")
            now = _now()
            dish_id = self._ensure_recipe_dish(
                db, value, couple["id"], user_id, visibility, value.get("dish_id") or None,
            )
            db.execute(
                """
                UPDATE recipes SET dish_id=?,name=?,normalized_name=?,variant_name=?,cooking_method=?,method_tags_json=?,cuisine=?,category=?,meal_slots_json=?,base_servings=?,
                    prep_minutes=?,cook_minutes=?,passive_minutes=?,difficulty=?,tips_json=?,allergen_tags_json=?,visibility=?,
                    version=version+1,updated_at=? WHERE id=?
                """,
                (
                    dish_id, value["name"], value["normalized_name"], value["variant_name"], value["cooking_method"],
                    _json(value["method_tags"]), value["cuisine"], value["category"], _json(value["meal_slots"]),
                    value["base_servings"], value["prep_minutes"], value["cook_minutes"], value["passive_minutes"], value["difficulty"],
                    _json(value["tips"]), _json(value["allergen_tags"]), visibility, now, recipe_id,
                ),
            )
            self._replace_recipe_parts(db, recipe_id, value)
            if identity_changed:
                # A photo confirmed for another dish/method must disappear
                # from the cover slot until it is reviewed again.
                db.execute(
                    "UPDATE recipe_media SET match_status='unverified',updated_at=? WHERE recipe_id=? AND media_type='image' AND match_status='verified'",
                    (now, recipe_id),
                )
                self._insert_recipe_media_candidates(db, recipe_id, [{
                    "media_type": "image", "role": "cover", "provider": "local",
                    "url": str(replacement_cover["url"]),
                    "thumbnail_url": str(replacement_cover.get("thumbnail_url") or "")[:2000],
                    "title": f"{value['name']}成品图",
                    "attribution": "用户上传并人工核对",
                    "license_note": "双方私有媒体空间",
                    "match_status": "verified",
                    "match_details": {"binding": "manual_exact_confirmation"},
                }], user_id)
            if alignment_changed:
                paired = db.execute(
                    "SELECT id,match_details_json FROM recipe_media WHERE recipe_id=? AND media_type='video' AND role='paired' AND match_status='verified'",
                    (recipe_id,),
                ).fetchall()
                for media in paired:
                    details = _parse(media["match_details_json"], {})
                    differences = list(details.get("differences") or [])
                    warning = "页面配方或步骤已修改，需要重新对照视频"
                    if warning not in differences:
                        differences.append(warning)
                    details.update({
                        "differences": differences[:20],
                        "checked_ingredients": False,
                        "checked_steps": False,
                        "stale_after_recipe_version": int(row["version"]) + 1,
                    })
                    db.execute(
                        "UPDATE recipe_media SET role='reference',match_status='reference',match_details_json=?,updated_at=? WHERE id=?",
                        (_json(details), now, media["id"]),
                    )
            updated = self._recipe_row_to_dict(db, db.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone())
            for media in db.execute("SELECT url FROM recipe_media WHERE recipe_id=?", (recipe_id,)).fetchall():
                local_id = self._local_media_id(media["url"])
                if local_id:
                    self._sync_media_visibility(db, local_id)
            self._audit(db, "recipe.updated", "recipe", recipe_id, {"version": updated["version"]}, actor_id=user_id)
            if row["visibility"] == "couple" or visibility == "couple":
                self._event(db, couple["id"], user_id, "meals.updated", recipe_id, {"resource": "recipe", "operation": "updated", "version": updated["version"]})
        return updated

    def delete_recipe(self, recipe_id: str, expected_version: object, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = self._get_visible_recipe_row(db, recipe_id, couple["id"], user_id)
            if row["source_type"] == "built_in":
                raise PermissionError("内置菜谱不可删除")
            if row["owner_id"] != user_id:
                raise PermissionError("只有菜谱创建者可以删除")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise MealConflictError("菜谱已被修改，请刷新", details={"current_version": row["version"]})
            now = _now()
            db.execute("UPDATE recipes SET deleted_at=?,version=version+1,updated_at=? WHERE id=?", (now, now, recipe_id))
            for media in db.execute("SELECT url FROM recipe_media WHERE recipe_id=?", (recipe_id,)).fetchall():
                local_id = self._local_media_id(media["url"])
                if local_id:
                    self._sync_media_visibility(db, local_id)
            self._audit(db, "recipe.deleted", "recipe", recipe_id, actor_id=user_id)
            if row["visibility"] == "couple":
                self._event(db, couple["id"], user_id, "meals.updated", recipe_id, {"resource": "recipe", "operation": "deleted"})
        return {"id": recipe_id, "deleted": True}

    # ---- Recipe drafts and providers ----------------------------------

    def _save_recipe_draft(self, db: sqlite3.Connection, couple_id: str, user_id: str, value: dict, source_type: str, provider: str) -> dict:
        draft_id = _id("recipe_draft")
        now = datetime.now(timezone.utc).replace(microsecond=0)
        expires = (now + timedelta(hours=24)).isoformat()
        db.execute(
            "INSERT INTO recipe_drafts(id,couple_id,creator_id,source_type,source_provider,payload_json,status,expires_at,created_at) VALUES (?,?,?,?,?,?,'pending',?,?)",
            (draft_id, couple_id, user_id, source_type, provider[:60], _json(value), expires, now.isoformat()),
        )
        return {"id": draft_id, "status": "pending", "source_type": source_type, "source_provider": provider, "recipe": value, "expires_at": expires}

    def generate_recipe_draft(self, payload: dict, user_id: str) -> dict:
        from .recipe_providers import recipe_generator_from_env

        request_name = str(payload.get("name") or payload.get("prompt") or "").strip()
        if not request_name or len(request_name) > 200:
            raise ValueError("请用不超过 200 个字符描述想做的菜")
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            job_id = _id("recipe_job")
            input_hash = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
            db.execute(
                "INSERT INTO recipe_generation_jobs(id,couple_id,user_id,job_type,provider,input_hash,status,created_at) VALUES (?,?,?,'generate','pending',?,'pending',?)",
                (job_id, couple["id"], user_id, input_hash, _now()),
            )
        try:
            generator = recipe_generator_from_env(self)
            generated = generator.generate(payload)
            value = validate_detailed_recipe(generated["recipe"])
            with self.connection() as db:
                couple, _ = self._couple_context(db, user_id)
                draft = self._save_recipe_draft(db, couple["id"], user_id, value, "ai", generated["provider"])
                db.execute(
                    "UPDATE recipe_generation_jobs SET provider=?,status='completed',result_draft_id=?,completed_at=? WHERE id=?",
                    (generated["provider"], draft["id"], _now(), job_id),
                )
            return {"id": job_id, "status": "completed", "draft": draft, "mock": bool(generated.get("mock"))}
        except Exception as error:
            with self.connection() as db:
                db.execute(
                    "UPDATE recipe_generation_jobs SET status='failed',error_code=?,completed_at=? WHERE id=?",
                    (type(error).__name__[:80], _now(), job_id),
                )
            raise

    def get_recipe_job(self, job_id: str, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute("SELECT * FROM recipe_generation_jobs WHERE id=? AND couple_id=? AND user_id=?", (job_id, couple["id"], user_id)).fetchone()
            if not row:
                raise KeyError("菜谱任务不存在")
            result = dict(row)
            if row["result_draft_id"]:
                draft = db.execute("SELECT * FROM recipe_drafts WHERE id=? AND creator_id=?", (row["result_draft_id"], user_id)).fetchone()
                if draft:
                    result["draft"] = {**dict(draft), "recipe": _parse(draft["payload_json"], {})}
                    result["draft"].pop("payload_json", None)
        return result

    def confirm_recipe_draft(self, draft_id: str, payload: dict, user_id: str) -> dict:
        visibility = str(payload.get("visibility", "couple"))
        if visibility not in {"couple", "private"}:
            raise ValueError("菜谱可见性无效")

        # Read and prepare the draft first.  Provider images are downloaded
        # outside an open SQLite transaction because media persistence opens
        # its own short transaction and may also write to remote object
        # storage.
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute("SELECT * FROM recipe_drafts WHERE id=? AND couple_id=? AND creator_id=?", (draft_id, couple["id"], user_id)).fetchone()
            if not row:
                raise KeyError("菜谱草稿不存在")
            if row["status"] == "confirmed":
                existing = db.execute("SELECT * FROM recipes WHERE source_ref=? AND owner_id=? AND deleted_at IS NULL", (draft_id, user_id)).fetchone()
                if existing:
                    return self._recipe_row_to_dict(db, existing)
                raise ConflictError("菜谱草稿已经确认")
            if row["status"] != "pending" or row["expires_at"] <= _now():
                db.execute("UPDATE recipe_drafts SET status='expired' WHERE id=? AND status='pending'", (draft_id,))
                raise ConflictError("菜谱草稿已经失效，请重新生成")
            draft_value = _parse(row["payload_json"], {})
            source_type = str(row["source_type"])
            source_provider = str(row["source_provider"])
            couple_id = str(couple["id"])
            cover_url = str(payload.get("cover_url") or "").strip()
            if cover_url:
                if payload.get("cover_confirmed_exact") is not True:
                    raise ValueError("必须确认封面就是当前菜品和做法")
                local_cover = self._validate_local_recipe_image(db, cover_url, couple["id"], user_id)
                if not local_cover:
                    raise ValueError("人工补充的封面必须先上传到双方媒体空间，不能直接填写外链")
                draft_value.setdefault("media", []).append({
                    "media_type": "image", "role": "cover", "provider": "local",
                    "url": cover_url, "title": f"{draft_value.get('name', '')}成品图",
                    "attribution": str(payload.get("cover_attribution") or "用户确认"),
                    "match_status": "verified", "match_details": {"binding": "manual_exact_confirmation"},
                })

        value = validate_detailed_recipe(draft_value)
        if source_type == "external":
            value = self._cache_external_recipe_cover(value, user_id)
            value = validate_detailed_recipe(value)
        verified_covers = [
            media for media in value.get("media", [])
            if media["media_type"] == "image" and media["role"] == "cover" and media["match_status"] == "verified"
        ]
        if not verified_covers:
            raise ValueError("这份菜谱没有可核对的对应图片，请先补充并确认准确封面")

        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            if couple["id"] != couple_id:
                raise ConflictError("情侣空间已经变化，请重新打开菜谱草稿")
            row = db.execute("SELECT * FROM recipe_drafts WHERE id=? AND couple_id=? AND creator_id=?", (draft_id, couple_id, user_id)).fetchone()
            if not row:
                raise KeyError("菜谱草稿不存在")
            if row["status"] == "confirmed":
                existing = db.execute("SELECT * FROM recipes WHERE source_ref=? AND owner_id=? AND deleted_at IS NULL", (draft_id, user_id)).fetchone()
                if existing:
                    return self._recipe_row_to_dict(db, existing)
                raise ConflictError("菜谱草稿已经确认")
            if row["status"] != "pending" or row["expires_at"] <= _now():
                db.execute("UPDATE recipe_drafts SET status='expired' WHERE id=? AND status='pending'", (draft_id,))
                raise ConflictError("菜谱草稿已经失效，请重新生成")
            for media in verified_covers:
                if not self._validate_local_recipe_image(db, media["url"], couple_id, user_id):
                    raise ValueError("菜谱封面没有安全保存到双方媒体空间")
            result = self._insert_recipe(
                db, value, couple_id, user_id, source_type=source_type, visibility=visibility,
                source_provider=source_provider, source_ref=draft_id,
            )
            db.execute("UPDATE recipe_drafts SET status='confirmed',confirmed_at=? WHERE id=?", (_now(), draft_id))
            self._audit(db, "recipe.draft.confirmed", "recipe", result["id"], {"draft_id": draft_id}, actor_id=user_id)
            if visibility == "couple":
                self._event(db, couple["id"], user_id, "meals.updated", result["id"], {"resource": "recipe", "operation": "created", "version": 1})
        return result

    def search_online_recipes(self, payload: dict, user_id: str) -> dict:
        from .recipe_providers import online_recipe_search_from_env

        keyword = str(payload.get("query", "")).strip()
        if not keyword or len(keyword) > 80:
            raise ValueError("搜索词不能为空且不能超过 80 个字符")
        try:
            limit = max(1, min(int(payload.get("limit", 5)), 5))
        except (TypeError, ValueError) as error:
            raise ValueError("搜索数量无效") from error
        provider_name = str(payload.get("provider", "tianapi"))
        search = online_recipe_search_from_env(provider_name, self)
        result = search.search_and_localize(keyword, limit)
        drafts = []
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            for item in result["items"]:
                value = validate_detailed_recipe(item)
                drafts.append(self._save_recipe_draft(db, couple["id"], user_id, value, "external", result["provider"]))
        return {"items": drafts, "provider": result["provider"], "mock": result.get("mock", False), "notice": result.get("notice", "")}

    def import_recipe(self, payload: dict, user_id: str) -> dict:
        """Confirm an external/AI draft through the generic import endpoint."""
        draft_id = str(payload.get("draft_id") or "").strip()
        if not draft_id:
            raise ValueError("缺少要导入的菜谱草稿")
        return self.confirm_recipe_draft(draft_id, {"visibility": payload.get("visibility", "couple")}, user_id)

    # ---- Wishes and carts ---------------------------------------------

    def list_meal_wishes(self, user_id: str) -> list[dict]:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            rows = db.execute(
                """
                SELECT w.*,u.nickname AS creator_name,r.name AS recipe_name
                FROM meal_wishes w JOIN users u ON u.id=w.creator_id
                LEFT JOIN recipes r ON r.id=w.recipe_id
                WHERE w.couple_id=? AND w.deleted_at IS NULL ORDER BY w.created_at DESC
                """,
                (couple["id"],),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_meal_wish(self, payload: dict, user_id: str) -> dict:
        recipe_id = str(payload.get("recipe_id") or "").strip() or None
        title = str(payload.get("title") or "").strip()
        note = str(payload.get("note") or "").strip()
        if len(title) > 100 or len(note) > 1000:
            raise ValueError("想吃内容过长")
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            if recipe_id:
                recipe = self._get_visible_recipe_row(db, recipe_id, couple["id"], user_id)
                title = title or recipe["name"]
            if not title:
                raise ValueError("请填写想吃的内容")
            wish_id = _id("meal_wish")
            db.execute(
                "INSERT INTO meal_wishes(id,couple_id,creator_id,recipe_id,title,note,created_at) VALUES (?,?,?,?,?,?,?)",
                (wish_id, couple["id"], user_id, recipe_id, title, note, _now()),
            )
            self._audit(db, "meal_wish.created", "meal_wish", wish_id, actor_id=user_id)
            self._event(db, couple["id"], user_id, "meals.updated", wish_id, {"resource": "meal_wish", "operation": "created"})
            row = db.execute("SELECT * FROM meal_wishes WHERE id=?", (wish_id,)).fetchone()
        return dict(row)

    def update_meal_wish(self, wish_id: str, payload: dict, user_id: str) -> dict:
        """Link a free-text wish to a real shared recipe.

        A wish may start as nothing more than "煎牛排".  Persisting the chosen
        recipe here makes the selection visible to both partners and gives the
        later cart entry a stable recipe snapshot instead of relying on browser
        state or a title match.
        """
        recipe_id = str(payload.get("recipe_id") or "").strip()
        if not recipe_id:
            raise ValueError("请选择要关联的菜谱")
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            wish = db.execute(
                "SELECT * FROM meal_wishes WHERE id=? AND couple_id=? AND deleted_at IS NULL",
                (wish_id, couple["id"]),
            ).fetchone()
            if not wish:
                raise KeyError("想吃内容不存在")
            expected_version = payload.get("expected_version")
            if expected_version is not None and int(expected_version) != int(wish["version"]):
                raise MealConflictError("想吃清单已被修改，请刷新", details={"current_version": wish["version"]})
            recipe = self._get_visible_recipe_row(db, recipe_id, couple["id"], user_id)
            if recipe["visibility"] == "private":
                raise PermissionError("仅自己可见的菜谱不能关联到双方共享的想吃清单")
            db.execute(
                "UPDATE meal_wishes SET recipe_id=?,version=version+1 WHERE id=?",
                (recipe_id, wish_id),
            )
            self._audit(
                db, "meal_wish.recipe_linked", "meal_wish", wish_id,
                {"recipe_id": recipe_id}, actor_id=user_id,
            )
            self._event(
                db, couple["id"], user_id, "meals.updated", wish_id,
                {"resource": "meal_wish", "operation": "recipe_linked", "recipe_id": recipe_id},
            )
            row = db.execute(
                """
                SELECT w.*,u.nickname AS creator_name,r.name AS recipe_name
                FROM meal_wishes w JOIN users u ON u.id=w.creator_id
                LEFT JOIN recipes r ON r.id=w.recipe_id WHERE w.id=?
                """,
                (wish_id,),
            ).fetchone()
        return dict(row)

    def delete_meal_wish(self, wish_id: str, expected_version: object, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute("SELECT * FROM meal_wishes WHERE id=? AND couple_id=? AND deleted_at IS NULL", (wish_id, couple["id"])).fetchone()
            if not row:
                raise KeyError("想吃内容不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise MealConflictError("想吃清单已被修改，请刷新", details={"current_version": row["version"]})
            db.execute("UPDATE meal_wishes SET deleted_at=?,version=version+1 WHERE id=?", (_now(), wish_id))
            self._audit(db, "meal_wish.deleted", "meal_wish", wish_id, actor_id=user_id)
            self._event(db, couple["id"], user_id, "meals.updated", wish_id, {"resource": "meal_wish", "operation": "deleted"})
        return {"id": wish_id, "deleted": True}

    @staticmethod
    def _active_cart(db: sqlite3.Connection, couple_id: str, create: bool = False) -> sqlite3.Row | None:
        row = db.execute("SELECT * FROM meal_carts WHERE couple_id=? AND status='active'", (couple_id,)).fetchone()
        if row or not create:
            return row
        cart_id = _id("meal_cart")
        now = _now()
        db.execute("INSERT INTO meal_carts(id,couple_id,title,status,created_at) VALUES (?,?,?,'active',?)", (cart_id, couple_id, "我们的点菜车", now))
        return db.execute("SELECT * FROM meal_carts WHERE id=?", (cart_id,)).fetchone()

    @staticmethod
    def _meal_item_dict(row: sqlite3.Row) -> dict:
        value = dict(row)
        value["recipe_snapshot"] = _parse(value.pop("recipe_snapshot_json", "{}"), {})
        value["name"] = value["recipe_snapshot"].get("name", "未命名菜品")
        return value

    def get_meal_cart(self, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            cart = self._active_cart(db, couple["id"], create=False)
            if not cart:
                return {"id": None, "title": "我们的点菜车", "status": "active", "version": 0, "items": []}
            items = [self._meal_item_dict(row) for row in db.execute(
                "SELECT * FROM meal_cart_items WHERE cart_id=? ORDER BY planned_date,CASE meal_slot WHEN 'breakfast' THEN 0 WHEN 'lunch' THEN 1 WHEN 'dinner' THEN 2 ELSE 3 END,planned_time,created_at",
                (cart["id"],),
            ).fetchall()]
            result = dict(cart)
            result["items"] = items
        return result

    @staticmethod
    def _validate_meal_values(payload: dict, current: sqlite3.Row | None = None) -> dict:
        def value(name: str, default: Any = None) -> Any:
            return payload[name] if name in payload else (current[name] if current else default)

        planned_date = parse_local_date(value("planned_date"), "计划日期")
        meal_slot = str(value("meal_slot", "dinner"))
        if meal_slot not in MEAL_SLOTS:
            raise ValueError("餐次无效")
        planned_time = str(value("planned_time") or "").strip() or None
        if planned_time and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", planned_time):
            raise ValueError("计划时间格式无效")
        try:
            servings = int(value("servings", 2))
        except (TypeError, ValueError) as error:
            raise ValueError("人数必须是整数") from error
        if not 1 <= servings <= 20:
            raise ValueError("人数必须在 1 到 20 之间")
        course_type = str(value("course_type", "main"))
        if course_type not in COURSE_ORDER:
            raise ValueError("菜品类型无效")
        note = str(value("note", "")).strip()
        if len(note) > 1000:
            raise ValueError("点菜备注不能超过 1000 个字符")
        return {"planned_date": planned_date, "meal_slot": meal_slot, "planned_time": planned_time, "servings": servings, "course_type": course_type, "note": note}

    def add_meal_cart_item(self, payload: dict, user_id: str) -> dict:
        recipe_id = str(payload.get("recipe_id") or "").strip()
        if not recipe_id:
            raise ValueError("请选择菜谱")
        values = self._validate_meal_values(payload)
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            previous = self._idempotent_result(db, couple["id"], user_id, "meal_item.create", payload.get("idempotency_key"))
            if previous:
                return previous
            recipe_row = self._get_visible_recipe_row(db, recipe_id, couple["id"], user_id)
            if recipe_row["visibility"] == "private":
                raise PermissionError("仅自己可见的菜谱不能加入双方共享的用餐日历")
            recipe = self._recipe_row_to_dict(db, recipe_row)
            cart = self._active_cart(db, couple["id"], create=True)
            duplicate = db.execute(
                "SELECT id,version FROM meal_cart_items WHERE cart_id=? AND recipe_id=? AND planned_date=? AND meal_slot=? AND status!='cancelled' LIMIT 1",
                (cart["id"], recipe_id, values["planned_date"], values["meal_slot"]),
            ).fetchone()
            if duplicate and not bool(payload.get("allow_duplicate", False)):
                raise MealConflictError(
                    "这道菜已经安排在同一餐次",
                    code="DUPLICATE_MEAL",
                    details={"existing_id": duplicate["id"], "current_version": duplicate["version"]},
                )
            item_id = _id("meal")
            now = _now()
            db.execute(
                """
                INSERT INTO meal_cart_items(
                    id,cart_id,couple_id,recipe_id,recipe_snapshot_json,planned_date,meal_slot,planned_time,
                    servings,ordered_by,status,course_type,note,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,'selected',?,?,?,?)
                """,
                (
                    item_id, cart["id"], couple["id"], recipe_id, _json(recipe_snapshot(recipe)), values["planned_date"],
                    values["meal_slot"], values["planned_time"], values["servings"], user_id, values["course_type"], values["note"], now, now,
                ),
            )
            wish_id = str(payload.get("wish_id") or "").strip()
            if wish_id:
                db.execute("UPDATE meal_wishes SET deleted_at=?,version=version+1 WHERE id=? AND couple_id=? AND deleted_at IS NULL", (now, wish_id, couple["id"]))
            db.execute("UPDATE meal_carts SET version=version+1 WHERE id=?", (cart["id"],))
            result = self._meal_item_dict(db.execute("SELECT * FROM meal_cart_items WHERE id=?", (item_id,)).fetchone())
            self._save_idempotent_result(db, couple["id"], user_id, "meal_item.create", payload.get("idempotency_key"), result)
            self._audit(db, "meal_item.created", "meal_item", item_id, {"date": values["planned_date"], "slot": values["meal_slot"]}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "meals.updated", item_id, {"resource": "meal_item", "operation": "created", "date": values["planned_date"], "version": 1})
        return result

    def update_meal_cart_item(self, item_id: str, payload: dict, user_id: str) -> dict:
        allowed_transitions = {
            "selected": {"accepted", "cancelled"},
            "accepted": {"shopping", "cancelled"},
            "shopping": {"cooking", "cancelled"},
            "cooking": {"completed", "cancelled"},
            "completed": set(),
            "cancelled": set(),
        }
        with self.connection() as db:
            couple, partner_id = self._couple_context(db, user_id)
            row = db.execute("SELECT * FROM meal_cart_items WHERE id=? AND couple_id=?", (item_id, couple["id"])).fetchone()
            if not row:
                raise KeyError("点菜记录不存在")
            if payload.get("expected_version") is not None and int(payload["expected_version"]) != int(row["version"]):
                raise MealConflictError("点菜记录已被另一方修改，请刷新", details={"current_version": row["version"]})
            if row["status"] in {"completed", "cancelled"}:
                raise ConflictError("历史记录不可直接修改，请使用再安排一次")
            values = self._validate_meal_values(payload, row)
            status = str(payload.get("status", row["status"]))
            if status != row["status"] and status not in allowed_transitions[row["status"]]:
                raise ValueError("点菜状态不能跳级修改")
            cook_user_id = payload.get("cook_user_id", row["cook_user_id"])
            if cook_user_id is not None and cook_user_id not in {user_id, partner_id}:
                raise PermissionError("做饭人必须是情侣双方")
            if status == "accepted" and not cook_user_id:
                cook_user_id = user_id
            completed_at = _now() if status == "completed" else row["completed_at"]
            now = _now()
            db.execute(
                """
                UPDATE meal_cart_items SET planned_date=?,meal_slot=?,planned_time=?,servings=?,course_type=?,note=?,
                    cook_user_id=?,status=?,completed_at=?,version=version+1,updated_at=? WHERE id=?
                """,
                (*values.values(), cook_user_id, status, completed_at, now, item_id),
            )
            db.execute("UPDATE meal_carts SET version=version+1 WHERE id=?", (row["cart_id"],))
            updated = self._meal_item_dict(db.execute("SELECT * FROM meal_cart_items WHERE id=?", (item_id,)).fetchone())
            self._audit(db, "meal_item.updated", "meal_item", item_id, {"status": status, "date": values["planned_date"]}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "meals.updated", item_id, {"resource": "meal_item", "operation": "updated", "date": values["planned_date"], "version": updated["version"]})
        return updated

    def cancel_meal_cart_item(self, item_id: str, expected_version: object, user_id: str) -> dict:
        return self.update_meal_cart_item(item_id, {"status": "cancelled", "expected_version": expected_version}, user_id)

    def archive_meal_cart(self, payload: dict, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            cart = self._active_cart(db, couple["id"], create=False)
            if not cart:
                raise KeyError("当前没有点菜车")
            if payload.get("expected_version") is not None and int(payload["expected_version"]) != int(cart["version"]):
                raise MealConflictError("点菜车已被修改，请刷新", details={"current_version": cart["version"]})
            remaining = db.execute("SELECT COUNT(*) FROM meal_cart_items WHERE cart_id=? AND status NOT IN ('completed','cancelled')", (cart["id"],)).fetchone()[0]
            if remaining:
                raise ConflictError("仍有未完成的点菜，不能归档整个点菜车")
            now = _now()
            db.execute("UPDATE meal_carts SET status='archived',archived_at=?,version=version+1 WHERE id=?", (now, cart["id"]))
            db.execute("UPDATE meal_cart_items SET archived_at=COALESCE(archived_at,?) WHERE cart_id=?", (now, cart["id"]))
            self._audit(db, "meal_cart.archived", "meal_cart", cart["id"], actor_id=user_id)
            self._event(db, couple["id"], user_id, "meals.updated", cart["id"], {"resource": "meal_cart", "operation": "archived"})
        return {"id": cart["id"], "archived": True}

    # ---- Calendar and archive -----------------------------------------

    def meal_calendar(self, start: object, end: object, user_id: str) -> dict:
        start_date = date.fromisoformat(parse_local_date(start, "开始日期"))
        end_date = date.fromisoformat(parse_local_date(end, "结束日期"))
        if end_date < start_date or (end_date - start_date).days > 61:
            raise ValueError("日历范围必须为连续的 1 到 62 天")
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            rows = db.execute(
                """
                SELECT * FROM meal_cart_items WHERE couple_id=? AND planned_date BETWEEN ? AND ?
                ORDER BY planned_date,CASE meal_slot WHEN 'breakfast' THEN 0 WHEN 'lunch' THEN 1 WHEN 'dinner' THEN 2 ELSE 3 END,
                    CASE course_type WHEN 'staple' THEN 0 WHEN 'main' THEN 1 WHEN 'side' THEN 2 WHEN 'soup' THEN 3 WHEN 'dessert' THEN 4 ELSE 5 END,
                    planned_time,created_at
                """,
                (couple["id"], start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        by_date: dict[str, dict[str, list]] = {}
        current = start_date
        while current <= end_date:
            by_date[current.isoformat()] = {slot: [] for slot in MEAL_SLOTS}
            current += timedelta(days=1)
        for row in rows:
            by_date[row["planned_date"]][row["meal_slot"]].append(self._meal_item_dict(row))
        return {
            "timezone": "Asia/Shanghai",
            "from": start_date.isoformat(),
            "to": end_date.isoformat(),
            "days": [{"date": day, "meals": meals} for day, meals in by_date.items()],
        }

    def meal_archive(self, year: object, month: object, user_id: str) -> dict:
        try:
            year_value, month_value = int(year), int(month)
            start = date(year_value, month_value, 1)
        except (TypeError, ValueError) as error:
            raise ValueError("归档年月无效") from error
        next_month = date(year_value + (month_value == 12), 1 if month_value == 12 else month_value + 1, 1)
        end = next_month - timedelta(days=1)
        result = self.meal_calendar(start.isoformat(), end.isoformat(), user_id)
        result["year"] = year_value
        result["month"] = month_value
        return result

    def repeat_meal_item(self, item_id: str, payload: dict, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute("SELECT * FROM meal_cart_items WHERE id=? AND couple_id=?", (item_id, couple["id"])).fetchone()
            if not row:
                raise KeyError("历史点菜记录不存在")
            snapshot = _parse(row["recipe_snapshot_json"], {})
            recipe_id = row["recipe_id"]
            if recipe_id:
                visible = db.execute(
                    "SELECT id,visibility FROM recipes WHERE id=? AND deleted_at IS NULL AND (visibility='system' OR (couple_id=? AND visibility='couple'))",
                    (recipe_id, couple["id"]),
                ).fetchone()
                recipe_id = recipe_id if visible else None
                if not recipe_id:
                    raise PermissionError("原菜谱当前不可共享，请先复制为双方可见菜谱")
            cart = self._active_cart(db, couple["id"], create=True)
            values = self._validate_meal_values({
                "planned_date": payload.get("planned_date"),
                "meal_slot": payload.get("meal_slot", row["meal_slot"]),
                "planned_time": payload.get("planned_time", row["planned_time"]),
                "servings": payload.get("servings", row["servings"]),
                "course_type": row["course_type"],
                "note": payload.get("note", row["note"]),
            })
            new_item_id, now = _id("meal"), _now()
            db.execute(
                """
                INSERT INTO meal_cart_items(id,cart_id,couple_id,recipe_id,recipe_snapshot_json,planned_date,meal_slot,planned_time,
                    servings,ordered_by,status,course_type,note,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,'selected',?,?,?,?)
                """,
                (new_item_id, cart["id"], couple["id"], recipe_id, _json(snapshot), values["planned_date"], values["meal_slot"], values["planned_time"], values["servings"], user_id, values["course_type"], values["note"], now, now),
            )
            db.execute("UPDATE meal_carts SET version=version+1 WHERE id=?", (cart["id"],))
            result = self._meal_item_dict(db.execute("SELECT * FROM meal_cart_items WHERE id=?", (new_item_id,)).fetchone())
            self._audit(db, "meal_item.repeated", "meal_item", new_item_id, {"source_id": item_id}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "meals.updated", new_item_id, {"resource": "meal_item", "operation": "created", "date": values["planned_date"], "version": 1})
        return result

    # ---- Shopping lists -----------------------------------------------

    @staticmethod
    def _shopping_item_dict(row: sqlite3.Row, sources: list[dict] | None = None) -> dict:
        value = dict(row)
        value["checked"] = bool(value["checked"])
        value["manual_locked"] = bool(value["manual_locked"])
        value["manually_added"] = bool(value["manually_added"])
        value["display_quantity"] = format_quantity(value["quantity_milli"], value["canonical_unit"], value["quantity_text"])
        value["category_key"] = normalize_shopping_category(value.get("category"), ingredient_name=value.get("canonical_name"))
        value["category_label"] = shopping_category_label(value["category_key"])
        value["sources"] = sources or []
        return value

    def get_shopping_list(self, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute("SELECT * FROM shopping_lists WHERE couple_id=? AND status='active'", (couple["id"],)).fetchone()
            if not row:
                return {"id": None, "title": "采购清单", "status": "empty", "version": 0, "items": []}
            items = []
            category_order = {key: index for index, key in enumerate(self._category_order_in_connection(db, couple["id"]))}
            raw_items = db.execute("SELECT * FROM shopping_list_items WHERE shopping_list_id=? ORDER BY position,display_name", (row["id"],)).fetchall()
            raw_items = sorted(raw_items, key=lambda item: (category_order.get(normalize_shopping_category(item["category"], ingredient_name=item["canonical_name"]), 99), item["position"], item["display_name"]))
            for item in raw_items:
                sources = [dict(source) for source in db.execute(
                    """
                    SELECT s.*,json_extract(m.recipe_snapshot_json,'$.name') AS recipe_name
                    FROM shopping_item_sources s JOIN meal_cart_items m ON m.id=s.meal_item_id
                    WHERE s.shopping_item_id=? ORDER BY recipe_name
                    """,
                    (item["id"],),
                ).fetchall()]
                items.append(self._shopping_item_dict(item, sources))
            result = dict(row)
            result["items"] = items
        return result

    def _shopping_rebuild_scope_in_connection(
        self,
        db: sqlite3.Connection,
        *,
        couple_id: str,
        range_start: str,
        range_end: str,
    ) -> tuple[sqlite3.Row | None, str, str, list[dict]]:
        """Load the complete meal set covered by the active and requested ranges."""
        active = db.execute(
            "SELECT * FROM shopping_lists WHERE couple_id=? AND status='active'", (couple_id,)
        ).fetchone()
        if active:
            range_start = min(str(active["range_start"]), range_start)
            range_end = max(str(active["range_end"]), range_end)
        rows = db.execute(
            """
            SELECT * FROM meal_cart_items WHERE couple_id=? AND planned_date BETWEEN ? AND ?
              AND status IN ('accepted','shopping','cooking') ORDER BY planned_date,meal_slot,id
            """,
            (couple_id, range_start, range_end),
        ).fetchall()
        return active, range_start, range_end, [self._meal_item_dict(row) for row in rows]

    def _apply_shopping_items_in_connection(
        self,
        db: sqlite3.Connection,
        *,
        couple_id: str,
        actor_id: str,
        meal_items: list[dict],
        range_start: str,
        range_end: str,
        expected_list_version: int,
        mark_accepted_as_shopping: bool = True,
    ) -> dict:
        """Rebuild the automatic portion of the active shopping list.

        The caller owns the transaction.  Manual rows and locked automatic
        rows are never overwritten, and this helper never commits or emits an
        event on its own.
        """
        if not meal_items:
            raise ConflictError("没有可用于采购清单的已确认菜品")
        for item in meal_items:
            if item.get("couple_id") != couple_id or not isinstance(item.get("recipe_snapshot"), dict):
                raise PermissionError("餐食不属于当前情侣空间")
        generated = build_shopping_items(meal_items)
        if not generated:
            raise ConflictError("菜谱缺少可靠食材，不能生成采购清单")
        active = db.execute(
            "SELECT * FROM shopping_lists WHERE couple_id=? AND status='active'", (couple_id,)
        ).fetchone()
        current_version = int(active["version"]) if active else 0
        if current_version != int(expected_list_version):
            raise MealConflictError("采购清单已被另一方修改，请刷新", details={"current_version": current_version})
        existing_rows = list(db.execute(
            "SELECT * FROM shopping_list_items WHERE shopping_list_id=?", (active["id"],)
        ).fetchall()) if active else []
        diff_detail = shopping_diff([dict(row) for row in existing_rows], generated)
        now = _now()
        created_list = not active
        if not active:
            cart = self._active_cart(db, couple_id, create=True)
            list_id = _id("shopping")
            db.execute(
                "INSERT INTO shopping_lists(id,couple_id,cart_id,title,range_start,range_end,status,created_at) VALUES (?,?,?,?,?,?,'active',?)",
                (list_id, couple_id, cart["id"], "采购清单", range_start, range_end, now),
            )
            active = db.execute("SELECT * FROM shopping_lists WHERE id=?", (list_id,)).fetchone()
        else:
            range_start = min(str(active["range_start"]), range_start)
            range_end = max(str(active["range_end"]), range_end)
        existing = {
            (row["canonical_name"], row["unit_family"], row["specification"]): row
            for row in existing_rows
        }
        generated_keys: set[tuple[str, str, str]] = set()
        changed_ids: list[str] = []
        logical_change = False
        for position, value in enumerate(generated, 1):
            key = (value["canonical_name"], value["unit_family"], value["specification"])
            generated_keys.add(key)
            old = existing.get(key)
            if old and (old["manual_locked"] or old["manually_added"]):
                item_id = str(old["id"])
            elif old:
                item_id = str(old["id"])
                changed = any((
                    old["display_name"] != value["display_name"],
                    old["quantity_milli"] != value["quantity_milli"],
                    old["quantity_text"] != value["quantity_text"],
                    old["canonical_unit"] != value["canonical_unit"],
                    old["category"] != value["category"],
                    int(old["position"]) != position,
                ))
                if changed:
                    quantity_changed = old["quantity_milli"] != value["quantity_milli"] or old["quantity_text"] != value["quantity_text"]
                    db.execute(
                        """
                        UPDATE shopping_list_items SET display_name=?,quantity_milli=?,quantity_text=?,canonical_unit=?,
                            unit_family=?,category=?,specification=?,position=?,checked=?,version=version+1,updated_at=?
                        WHERE id=?
                        """,
                        (value["display_name"], value["quantity_milli"], value["quantity_text"], value["canonical_unit"],
                         value["unit_family"], value["category"], value["specification"], position,
                         0 if quantity_changed else int(old["checked"]), now, item_id),
                    )
                    changed_ids.append(item_id)
                    logical_change = True
            else:
                item_id = _id("shopping_item")
                db.execute(
                    """
                    INSERT INTO shopping_list_items(id,shopping_list_id,canonical_name,display_name,quantity_milli,quantity_text,
                        canonical_unit,unit_family,category,specification,position,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (item_id, active["id"], value["canonical_name"], value["display_name"], value["quantity_milli"],
                     value["quantity_text"], value["canonical_unit"], value["unit_family"], value["category"],
                     value["specification"], position, now),
                )
                changed_ids.append(item_id)
                logical_change = True
            if not (old and (old["manual_locked"] or old["manually_added"])):
                db.execute("DELETE FROM shopping_item_sources WHERE shopping_item_id=?", (item_id,))
                for source in value["sources"]:
                    db.execute(
                        "INSERT INTO shopping_item_sources(shopping_item_id,meal_item_id,recipe_ingredient_id,contributed_quantity_milli) VALUES (?,?,?,?)",
                        (item_id, source["meal_item_id"], source["recipe_ingredient_id"], source["contributed_quantity_milli"]),
                    )
        for key, old in existing.items():
            if key not in generated_keys and not old["manual_locked"] and not old["manually_added"]:
                db.execute("DELETE FROM shopping_list_items WHERE id=?", (old["id"],))
                changed_ids.append(str(old["id"]))
                logical_change = True
        range_changed = active["range_start"] != range_start or active["range_end"] != range_end
        if (logical_change or range_changed) and not created_list:
            db.execute(
                "UPDATE shopping_lists SET range_start=?,range_end=?,version=version+1 WHERE id=?",
                (range_start, range_end, active["id"]),
            )
        if mark_accepted_as_shopping:
            db.execute(
                "UPDATE meal_cart_items SET status='shopping',version=version+1,updated_at=? WHERE couple_id=? AND planned_date BETWEEN ? AND ? AND status='accepted'",
                (now, couple_id, range_start, range_end),
            )
        result_list = self._shopping_list_in_connection(db, active["id"])
        return {
            "shopping_list": result_list,
            "diff": {
                "add": len(diff_detail["added"]), "update": len(diff_detail["updated"]),
                "remove": len(diff_detail["removed"]),
                "unchanged": max(0, len(generated) - len(diff_detail["added"]) - len(diff_detail["updated"])),
            },
            "changed_item_ids": changed_ids,
        }

    def preview_shopping_list(self, payload: dict, user_id: str) -> dict:
        range_start = date.fromisoformat(parse_local_date(payload.get("range_start"), "采购开始日期"))
        range_end = date.fromisoformat(parse_local_date(payload.get("range_end"), "采购结束日期"))
        if range_end < range_start or (range_end - range_start).days > 30:
            raise ValueError("采购范围必须为连续的 1 到 31 天")
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            active_list, rebuild_start, rebuild_end, meal_items = self._shopping_rebuild_scope_in_connection(
                db, couple_id=couple["id"], range_start=range_start.isoformat(), range_end=range_end.isoformat(),
            )
            if active_list and payload.get("expected_version") is not None and int(payload["expected_version"]) != int(active_list["version"]):
                raise MealConflictError("采购清单已被另一方修改，请刷新", details={"current_version": active_list["version"]})
            if not meal_items:
                raise ValueError("所选日期内还没有双方确认的菜品，请先在用餐日历确认")
            generated = build_shopping_items(meal_items)
            category_order = {
                key: index for index, key in enumerate(self._category_order_in_connection(db, couple["id"]))
            }
            generated.sort(key=lambda item: (
                category_order.get(normalize_shopping_category(item.get("category"), ingredient_name=item.get("canonical_name")), 99),
                item.get("display_name", ""), item.get("specification", ""),
            ))
            for position, item in enumerate(generated, 1):
                item["position"] = position
            existing = []
            if active_list:
                existing = [dict(row) for row in db.execute("SELECT * FROM shopping_list_items WHERE shopping_list_id=?", (active_list["id"],)).fetchall()]
            diff = shopping_diff(existing, generated)
            draft_id = _id("shopping_preview")
            now = datetime.now(timezone.utc).replace(microsecond=0)
            expires = (now + timedelta(minutes=15)).isoformat()
            expected = {f"meal:{item['id']}": int(item["version"]) for item in meal_items}
            expected["shopping_list"] = int(active_list["version"]) if active_list else 0
            stored_payload = {"range_start": rebuild_start, "range_end": rebuild_end, "generated": generated}
            preview = {"id": draft_id, "range_start": rebuild_start, "range_end": rebuild_end, "items": generated, "diff": diff, "meal_count": len(meal_items), "expires_at": expires}
            db.execute(
                """
                INSERT INTO ui_action_drafts(id,couple_id,creator_id,action_type,payload_json,preview_json,expected_versions_json,status,expires_at,created_at)
                VALUES (?,?,?,'shopping_list.apply',?,?,?,'draft',?,?)
                """,
                (draft_id, couple["id"], user_id, _json(stored_payload), _json(preview), _json(expected), expires, now.isoformat()),
            )
        return {"preview_id": draft_id, "preview": preview}

    def apply_shopping_preview(self, payload: dict, user_id: str) -> dict:
        preview_id = str(payload.get("preview_id") or "").strip()
        if not preview_id:
            raise ValueError("缺少采购预览")
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            previous = self._idempotent_result(db, couple["id"], user_id, "shopping.apply", payload.get("idempotency_key"))
            if previous:
                return previous
            draft = db.execute("SELECT * FROM ui_action_drafts WHERE id=? AND couple_id=? AND creator_id=? AND action_type='shopping_list.apply'", (preview_id, couple["id"], user_id)).fetchone()
            if not draft:
                raise KeyError("采购预览不存在")
            if draft["status"] != "draft" or draft["expires_at"] <= _now():
                raise ConflictError("采购预览已经失效，请重新生成")
            expected = _parse(draft["expected_versions_json"], {})
            active_list = db.execute("SELECT * FROM shopping_lists WHERE couple_id=? AND status='active'", (couple["id"],)).fetchone()
            current_list_version = int(active_list["version"]) if active_list else 0
            if current_list_version != int(expected.get("shopping_list", 0)):
                raise MealConflictError("采购清单已被另一方修改，请重新预览", details={"current_version": current_list_version})
            for key, version in expected.items():
                if not key.startswith("meal:"):
                    continue
                item = db.execute("SELECT version FROM meal_cart_items WHERE id=? AND couple_id=?", (key[5:], couple["id"])).fetchone()
                if not item or int(item["version"]) != int(version):
                    raise MealConflictError("点菜计划已经变化，请重新生成采购清单", code="MEAL_PLAN_CHANGED")
            stored = _parse(draft["payload_json"], {})
            now = _now()
            active_list, rebuild_start, rebuild_end, rebuild_meals = self._shopping_rebuild_scope_in_connection(
                db, couple_id=couple["id"], range_start=stored["range_start"], range_end=stored["range_end"],
            )
            applied = self._apply_shopping_items_in_connection(
                db, couple_id=couple["id"], actor_id=user_id,
                meal_items=rebuild_meals,
                range_start=rebuild_start, range_end=rebuild_end,
                expected_list_version=current_list_version,
            )
            db.execute("UPDATE ui_action_drafts SET status='confirmed',idempotency_key=?,confirmed_at=? WHERE id=?", (str(payload.get("idempotency_key") or "") or None, now, preview_id))
            result = applied["shopping_list"]
            self._save_idempotent_result(db, couple["id"], user_id, "shopping.apply", payload.get("idempotency_key"), result)
            self._audit(db, "shopping_list.applied", "shopping_list", result["id"], {"range_start": stored["range_start"], "range_end": stored["range_end"]}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "meals.updated", result["id"], {"resource": "shopping_list", "operation": "updated", "version": result["version"]})
        return result

    def _shopping_list_in_connection(self, db: sqlite3.Connection, list_id: str) -> dict:
        row = db.execute("SELECT * FROM shopping_lists WHERE id=?", (list_id,)).fetchone()
        result = dict(row)
        result["items"] = []
        order = {key: index for index, key in enumerate(self._category_order_in_connection(db, row["couple_id"]))}
        raw_items = db.execute("SELECT * FROM shopping_list_items WHERE shopping_list_id=? ORDER BY position,display_name", (list_id,)).fetchall()
        raw_items = sorted(raw_items, key=lambda item: (order.get(normalize_shopping_category(item["category"], ingredient_name=item["canonical_name"]), 99), item["position"], item["display_name"]))
        for item in raw_items:
            sources = [dict(value) for value in db.execute("SELECT * FROM shopping_item_sources WHERE shopping_item_id=?", (item["id"],)).fetchall()]
            result["items"].append(self._shopping_item_dict(item, sources))
        return result

    def create_shopping_item(self, payload: dict, user_id: str) -> dict:
        ingredient = normalize_ingredient(payload, 1)
        raw_category = str(payload.get("category") or "").strip()
        if raw_category and raw_category not in CATEGORY_KEYS and raw_category not in CATEGORY_BY_LABEL:
            raise ValueError("采购分类必须使用固定九类")
        category = normalize_shopping_category(raw_category, ingredient_name=ingredient["canonical_name"])
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            shopping = db.execute("SELECT * FROM shopping_lists WHERE couple_id=? AND status='active'", (couple["id"],)).fetchone()
            if not shopping:
                today = _today().isoformat()
                list_id = _id("shopping")
                cart = self._active_cart(db, couple["id"], create=True)
                db.execute("INSERT INTO shopping_lists(id,couple_id,cart_id,title,range_start,range_end,status,created_at) VALUES (?,?,?,?,?,?,'active',?)", (list_id, couple["id"], cart["id"], "采购清单", today, today, _now()))
                shopping = db.execute("SELECT * FROM shopping_lists WHERE id=?", (list_id,)).fetchone()
            position = db.execute("SELECT COALESCE(MAX(position),0)+1 FROM shopping_list_items WHERE shopping_list_id=?", (shopping["id"],)).fetchone()[0]
            item_id, now = _id("shopping_item"), _now()
            db.execute(
                """
                INSERT INTO shopping_list_items(id,shopping_list_id,canonical_name,display_name,quantity_milli,quantity_text,
                    canonical_unit,unit_family,category,specification,manual_locked,manually_added,position,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,1,1,?,?)
                """,
                (item_id, shopping["id"], ingredient["canonical_name"], ingredient["name"], ingredient["quantity_milli"], ingredient["quantity_text"], ingredient["canonical_unit"], ingredient["unit_family"], category, ingredient["specification"], position, now),
            )
            db.execute("UPDATE shopping_lists SET version=version+1 WHERE id=?", (shopping["id"],))
            row = db.execute("SELECT * FROM shopping_list_items WHERE id=?", (item_id,)).fetchone()
            result = self._shopping_item_dict(row)
            self._event(db, couple["id"], user_id, "meals.updated", shopping["id"], {"resource": "shopping_list", "operation": "updated"})
        return result

    def update_shopping_item(self, item_id: str, payload: dict, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute(
                "SELECT i.* FROM shopping_list_items i JOIN shopping_lists l ON l.id=i.shopping_list_id WHERE i.id=? AND l.couple_id=? AND l.status='active'",
                (item_id, couple["id"]),
            ).fetchone()
            if not row:
                raise KeyError("采购项目不存在")
            if payload.get("expected_version") is not None and int(payload["expected_version"]) != int(row["version"]):
                raise MealConflictError("采购项目已被另一方修改，请刷新", details={"current_version": row["version"]})
            if "checked" in payload and not isinstance(payload["checked"], bool):
                raise ValueError("购买状态必须是布尔值")
            checked = int(payload.get("checked", bool(row["checked"])))
            display_name = str(payload.get("name", row["display_name"])).strip()[:80]
            if not display_name:
                raise ValueError("食材名称不能为空")
            raw_category = str(payload.get("category", row["category"])).strip()
            if "category" in payload and raw_category not in CATEGORY_KEYS and raw_category not in CATEGORY_BY_LABEL:
                raise ValueError("采购分类必须使用固定九类")
            category = normalize_shopping_category(raw_category, ingredient_name=display_name)
            quantity_fields = {"quantity", "unit", "quantity_text", "specification", "category"}
            manual_change = bool(quantity_fields.intersection(payload)) or "name" in payload
            ingredient = None
            if manual_change:
                ingredient = normalize_ingredient({
                    "name": display_name,
                    "quantity": payload.get("quantity") if "quantity" in payload else (row["quantity_milli"] / 1000 if row["quantity_milli"] is not None else None),
                    "unit": payload.get("unit", {"g": "克", "ml": "毫升", "piece": "个", "vague": row["quantity_text"]}.get(row["canonical_unit"], row["canonical_unit"])),
                    "quantity_text": payload.get("quantity_text", row["quantity_text"]),
                    "specification": payload.get("specification", row["specification"]),
                }, 1)
            now = _now()
            if ingredient:
                db.execute(
                    """
                    UPDATE shopping_list_items SET canonical_name=?,display_name=?,quantity_milli=?,quantity_text=?,canonical_unit=?,
                        unit_family=?,category=?,specification=?,checked=?,manual_locked=1,version=version+1,updated_at=? WHERE id=?
                    """,
                    (ingredient["canonical_name"], ingredient["name"], ingredient["quantity_milli"], ingredient["quantity_text"], ingredient["canonical_unit"], ingredient["unit_family"], category, ingredient["specification"], checked, now, item_id),
                )
            else:
                db.execute("UPDATE shopping_list_items SET checked=?,version=version+1,updated_at=? WHERE id=?", (checked, now, item_id))
            db.execute("UPDATE shopping_lists SET version=version+1 WHERE id=?", (row["shopping_list_id"],))
            updated = self._shopping_item_dict(db.execute("SELECT * FROM shopping_list_items WHERE id=?", (item_id,)).fetchone())
            self._event(db, couple["id"], user_id, "meals.updated", row["shopping_list_id"], {"resource": "shopping_list", "operation": "updated", "version": updated["version"]})
        return updated

    def delete_shopping_item(self, item_id: str, expected_version: object, user_id: str) -> dict:
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            row = db.execute(
                "SELECT i.* FROM shopping_list_items i JOIN shopping_lists l ON l.id=i.shopping_list_id WHERE i.id=? AND l.couple_id=? AND l.status='active'",
                (item_id, couple["id"]),
            ).fetchone()
            if not row:
                raise KeyError("采购项目不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise MealConflictError("采购项目已被另一方修改，请刷新", details={"current_version": row["version"]})
            db.execute("DELETE FROM shopping_list_items WHERE id=?", (item_id,))
            db.execute("UPDATE shopping_lists SET version=version+1 WHERE id=?", (row["shopping_list_id"],))
            self._event(db, couple["id"], user_id, "meals.updated", row["shopping_list_id"], {"resource": "shopping_list", "operation": "updated"})
        return {"id": item_id, "deleted": True}

    def complete_shopping_list(self, payload: dict, user_id: str) -> dict:
        self._kitchen_fields(payload, frozenset({
            "expected_version", "allow_incomplete", "stock_checked_item_ids",
            "stock_item_versions", "confirm_food_item_ids", "idempotency_key",
        }), "完成采购")
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 1:
            raise ValueError("采购清单版本无效")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            couple, _ = self._couple_context(db, user_id)
            key, _ = self._kitchen_request(payload, "shopping.complete")
            receipt = db.execute(
                "SELECT result_json FROM mutation_idempotency WHERE couple_id=? AND user_id=? AND operation='shopping.complete' AND idempotency_key=?",
                (couple["id"], user_id, key),
            ).fetchone()
            if receipt:
                stored = json.loads(receipt["result_json"])
                prior_id = str(stored.get("response", {}).get("id") or "")
                if not prior_id:
                    raise ConflictError("幂等回执缺少原采购清单，请使用新的幂等键")
                _, prior_fingerprint = self._kitchen_request(
                    payload, "shopping.complete", resource_id=prior_id,
                )
                replay = self._kitchen_replay(
                    db, couple["id"], user_id, "shopping.complete", key, prior_fingerprint,
                )
                if replay is not None:
                    return replay
            row = db.execute("SELECT * FROM shopping_lists WHERE couple_id=? AND status='active'", (couple["id"],)).fetchone()
            if not row:
                raise KeyError("当前没有采购清单")
            key, fingerprint = self._kitchen_request(payload, "shopping.complete", resource_id=row["id"])
            replay = self._kitchen_replay(db, couple["id"], user_id, "shopping.complete", key, fingerprint)
            if replay is not None:
                return replay
            if expected_version != int(row["version"]):
                raise MealConflictError("采购清单已被另一方修改，请刷新", details={"current_version": row["version"]})
            unchecked = db.execute("SELECT COUNT(*) FROM shopping_list_items WHERE shopping_list_id=? AND checked=0", (row["id"],)).fetchone()[0]
            if unchecked and not bool(payload.get("allow_incomplete", False)):
                raise ConflictError("仍有未购买食材，请确认后再完成")
            stock_ids = payload.get("stock_checked_item_ids", [])
            stock_versions = payload.get("stock_item_versions", {})
            if (
                not isinstance(stock_ids, list)
                or len(stock_ids) > 500
                or any(not isinstance(item_id, str) or not item_id for item_id in stock_ids)
                or len(stock_ids) != len(set(stock_ids))
                or not isinstance(stock_versions, dict)
            ):
                raise ValueError("入库选择格式无效")
            if set(stock_versions) != set(stock_ids):
                raise ValueError("入库项目版本不完整")
            current_item_ids = {
                item["id"]
                for item in db.execute(
                    "SELECT id FROM shopping_list_items WHERE shopping_list_id=?",
                    (row["id"],),
                ).fetchall()
            }
            if not set(stock_ids).issubset(current_item_ids):
                raise ConflictError("只能把当前采购清单中的食材转入库存")
            raw_confirm_food_ids = payload.get("confirm_food_item_ids", [])
            if (
                not isinstance(raw_confirm_food_ids, list)
                or any(not isinstance(item_id, str) or not item_id for item_id in raw_confirm_food_ids)
                or len(raw_confirm_food_ids) != len(set(raw_confirm_food_ids))
            ):
                raise ValueError("食品确认项目格式无效")
            confirm_food_ids = set(raw_confirm_food_ids)
            if not confirm_food_ids.issubset(set(stock_ids)):
                raise ValueError("食品确认项目必须属于本次入库选择")
            stocked = []
            for item_id in stock_ids:
                stocked.append(self._stock_shopping_item_in_connection(
                    db, couple_id=couple["id"], actor_id=user_id, shopping_item_id=str(item_id),
                    payload={"expected_version": stock_versions[item_id], "confirm_food": item_id in confirm_food_ids},
                ))
            now = _now()
            updated = db.execute(
                "UPDATE shopping_lists SET status='completed',completed_at=?,version=version+1 WHERE id=? AND status='active' AND version=?",
                (now, row["id"], expected_version),
            ).rowcount
            if updated != 1:
                raise MealConflictError("采购清单已被另一方修改，请刷新")
            result = {"id": row["id"], "completed": True, "unchecked": int(unchecked), "stocked_count": sum(1 for item in stocked if item["changed"]), "stocked_items": stocked}
            self._kitchen_save(db, couple["id"], user_id, "shopping.complete", key, fingerprint, result)
            self._audit(db, "shopping_list.completed", "shopping_list", row["id"], {"unchecked": unchecked, "stocked_count": result["stocked_count"]}, actor_id=user_id)
            self._event(db, couple["id"], user_id, "meals.updated", row["id"], {"resource": "shopping_list", "operation": "completed", "stocked_count": result["stocked_count"]})
        return result

    def meal_summary(self, user_id: str) -> dict:
        today = _today().isoformat()
        with self.connection() as db:
            couple, _ = self._couple_context(db, user_id)
            today_count = db.execute("SELECT COUNT(*) FROM meal_cart_items WHERE couple_id=? AND planned_date=? AND status!='cancelled'", (couple["id"], today)).fetchone()[0]
            wish_count = db.execute("SELECT COUNT(*) FROM meal_wishes WHERE couple_id=? AND deleted_at IS NULL", (couple["id"],)).fetchone()[0]
            unchecked = db.execute(
                "SELECT COUNT(*) FROM shopping_list_items i JOIN shopping_lists l ON l.id=i.shopping_list_id WHERE l.couple_id=? AND l.status='active' AND i.checked=0",
                (couple["id"],),
            ).fetchone()[0]
            next_row = db.execute(
                "SELECT planned_date,recipe_snapshot_json FROM meal_cart_items WHERE couple_id=? AND planned_date>=? AND status!='cancelled' ORDER BY planned_date,CASE meal_slot WHEN 'breakfast' THEN 0 WHEN 'lunch' THEN 1 WHEN 'dinner' THEN 2 ELSE 3 END LIMIT 1",
                (couple["id"], today),
            ).fetchone()
        next_meal = None
        if next_row:
            next_meal = {"date": next_row["planned_date"], "name": _parse(next_row["recipe_snapshot_json"], {}).get("name", "已安排菜品")}
        return {"today_count": int(today_count), "wish_count": int(wish_count), "unchecked_shopping_count": int(unchecked), "next_meal": next_meal}
