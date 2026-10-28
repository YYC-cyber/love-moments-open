"""Pure recipe validation, unit conversion and shopping-list helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

from .recipe_catalog_expansion import build_expansion_recipes
from .recipe_catalog_quality import allergen_report, audit_normalized_catalog, inferred_allergens
from .recipe_detailing import SEASONING_NAMES, enrich_builtin_recipe


MEAL_SLOTS = ("breakfast", "lunch", "dinner", "snack")
MEAL_SLOT_ORDER = {value: index for index, value in enumerate(MEAL_SLOTS)}
COURSE_ORDER = {"staple": 0, "main": 1, "side": 2, "soup": 3, "dessert": 4, "drink": 5}
DIFFICULTIES = {"easy", "medium", "hard"}
CUISINES = {"chinese", "western"}
VISIBILITIES = {"couple", "private"}


@dataclass(frozen=True)
class UnitRule:
    family: str
    canonical_unit: str
    factor: Decimal


UNIT_RULES = {
    "克": UnitRule("mass", "g", Decimal("1")),
    "g": UnitRule("mass", "g", Decimal("1")),
    "千克": UnitRule("mass", "g", Decimal("1000")),
    "公斤": UnitRule("mass", "g", Decimal("1000")),
    "kg": UnitRule("mass", "g", Decimal("1000")),
    "毫升": UnitRule("volume", "ml", Decimal("1")),
    "ml": UnitRule("volume", "ml", Decimal("1")),
    "升": UnitRule("volume", "ml", Decimal("1000")),
    "l": UnitRule("volume", "ml", Decimal("1000")),
    "汤匙": UnitRule("volume", "ml", Decimal("15")),
    "大勺": UnitRule("volume", "ml", Decimal("15")),
    "茶匙": UnitRule("volume", "ml", Decimal("5")),
    "小勺": UnitRule("volume", "ml", Decimal("5")),
    "个": UnitRule("count", "piece", Decimal("1")),
    "只": UnitRule("count", "piece", Decimal("1")),
    "枚": UnitRule("count", "piece", Decimal("1")),
    "根": UnitRule("count", "piece", Decimal("1")),
    "颗": UnitRule("count", "piece", Decimal("1")),
    "瓣": UnitRule("count", "piece", Decimal("1")),
    "张": UnitRule("count", "piece", Decimal("1")),
    "条": UnitRule("count", "piece", Decimal("1")),
    "片": UnitRule("count", "piece", Decimal("1")),
    "块": UnitRule("count", "piece", Decimal("1")),
    "份": UnitRule("count", "piece", Decimal("1")),
    "包": UnitRule("count", "piece", Decimal("1")),
    "盒": UnitRule("count", "piece", Decimal("1")),
}

VAGUE_UNITS = {"", "少许", "适量", "若干", "按需", "一小把", "一把"}

INGREDIENT_ALIASES = {
    "小葱": "香葱",
    "葱花": "香葱",
    "青葱": "香葱",
    "西红柿": "番茄",
    "马铃薯": "土豆",
    "洋芋": "土豆",
    "生姜": "姜",
    "姜片": "姜",
    "蒜瓣": "大蒜",
    "蒜末": "大蒜",
    "鸡蛋液": "鸡蛋",
    "食用油": "植物油",
    "色拉油": "植物油",
    "橄榄油": "橄榄油",
    "意粉": "意大利面",
}

CATEGORY_KEYWORDS = {
    "蔬菜": ("菜", "葱", "姜", "蒜", "番茄", "土豆", "萝卜", "蘑菇", "菌", "椒", "瓜", "茄", "笋", "豆芽", "玉米"),
    "肉禽": ("猪", "牛", "羊", "鸡", "鸭", "肉", "培根", "火腿"),
    "水产": ("鱼", "虾", "蟹", "贝", "鱿鱼", "海鲜"),
    "蛋奶": ("蛋", "奶", "芝士", "黄油", "奶油", "酸奶"),
    "主食": ("米", "面", "粉", "吐司", "面包", "燕麦", "意大利面", "饺子皮", "馄饨皮"),
    "调味品": ("盐", "糖", "酱", "醋", "油", "料酒", "胡椒", "香料", "淀粉", "咖喱", "蜂蜜"),
}


def contains_chinese(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", value))


def clean_text(value: object, maximum: int, field: str, *, required: bool = True) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if required and not text:
        raise ValueError(f"{field}不能为空")
    if len(text) > maximum:
        raise ValueError(f"{field}不能超过 {maximum} 个字符")
    return text


def parse_local_date(value: object, field: str = "日期") -> str:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as error:
        raise ValueError(f"{field}必须是有效日期") from error


def normalize_name(value: object) -> str:
    return re.sub(r"[\s·•・]+", "", str(value or "").strip().lower())


def canonical_ingredient_name(value: object) -> str:
    text = clean_text(value, 80, "食材名称")
    normalized = re.sub(r"[\s·•・]+", "", text)
    return INGREDIENT_ALIASES.get(normalized, normalized)


def ingredient_category(name: str) -> str:
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in name for keyword in keywords):
            return category
    return "其他"


def _decimal(value: object, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{field}必须是数字") from error
    if not number.is_finite() or number <= 0 or number > Decimal("1000000"):
        raise ValueError(f"{field}必须大于 0 且数值合理")
    return number


def normalize_ingredient(value: dict, position: int) -> dict:
    if not isinstance(value, dict):
        raise ValueError("食材必须是对象")
    name = clean_text(value.get("name"), 80, "食材名称")
    specification = clean_text(value.get("specification", ""), 120, "食材规格", required=False)
    preparation = clean_text(value.get("preparation", ""), 200, "食材处理", required=False)
    ingredient_group = str(value.get("group", value.get("ingredient_group", "")) or "").strip().lower()
    if not ingredient_group:
        ingredient_group = "seasoning" if name in SEASONING_NAMES else "main"
    if ingredient_group not in {"main", "seasoning"}:
        raise ValueError("食材分组必须是主要食材或调味料")
    raw_unit = str(value.get("unit", value.get("canonical_unit", "")) or "").strip().lower()
    raw_quantity = value.get("quantity")
    quantity_text = clean_text(value.get("quantity_text", ""), 40, "食材用量", required=False)
    normalized_quantity = value.get("quantity_milli")
    if raw_quantity in (None, "") and normalized_quantity not in (None, ""):
        try:
            quantity_milli = int(normalized_quantity)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{name}用量格式无效") from error
        if quantity_milli <= 0 or quantity_milli > 1_000_000_000:
            raise ValueError(f"{name}用量必须大于 0 且数值合理")
        family_by_unit = {rule.canonical_unit: rule.family for rule in UNIT_RULES.values()}
        if raw_unit not in family_by_unit:
            raise ValueError(f"{name}使用了不支持的单位：{raw_unit or '空'}")
        return {
            "name": name,
            "canonical_name": canonical_ingredient_name(name),
            "quantity_milli": quantity_milli,
            "quantity_text": "",
            "canonical_unit": raw_unit,
            "unit_family": str(value.get("unit_family") or family_by_unit[raw_unit]),
            "specification": specification,
            "preparation": preparation,
            "ingredient_group": ingredient_group,
            "optional": int(bool(value.get("optional", False))),
            "position": position,
        }
    if raw_quantity in (None, ""):
        vague = quantity_text or raw_unit or "适量"
        if vague not in VAGUE_UNITS and not quantity_text:
            raise ValueError(f"{name}缺少可识别的数量")
        return {
            "name": name,
            "canonical_name": canonical_ingredient_name(name),
            "quantity_milli": None,
            "quantity_text": vague,
            "canonical_unit": "vague",
            "unit_family": "vague",
            "specification": specification,
            "preparation": preparation,
            "ingredient_group": ingredient_group,
            "optional": int(bool(value.get("optional", False))),
            "position": position,
        }
    rule = UNIT_RULES.get(raw_unit)
    if not rule:
        raise ValueError(f"{name}使用了不支持的单位：{raw_unit or '空'}")
    number = _decimal(raw_quantity, f"{name}用量") * rule.factor
    quantity_milli = int((number * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return {
        "name": name,
        "canonical_name": canonical_ingredient_name(name),
        "quantity_milli": quantity_milli,
        "quantity_text": "",
        "canonical_unit": rule.canonical_unit,
        "unit_family": rule.family,
        "specification": specification,
        "preparation": preparation,
        "ingredient_group": ingredient_group,
        "optional": int(bool(value.get("optional", False))),
        "position": position,
    }


def normalize_recipe_payload(payload: dict, *, require_chinese: bool = True) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("菜谱内容必须是对象")
    name = clean_text(payload.get("name"), 80, "菜名")
    if require_chinese and not contains_chinese(name):
        raise ValueError("菜名必须包含中文")
    cuisine = str(payload.get("cuisine", "chinese")).strip().lower()
    if cuisine not in CUISINES:
        raise ValueError("菜系必须是中餐或西餐")
    category = clean_text(payload.get("category", "家常菜"), 40, "菜谱分类")
    dish_name = clean_text(payload.get("dish_name", name), 80, "菜品名称")
    variant_name = clean_text(payload.get("variant_name", "经典做法"), 60, "做法名称")
    cooking_method = clean_text(payload.get("cooking_method", variant_name), 80, "烹饪方式")
    raw_method_tags = payload.get("method_tags", [])
    if not isinstance(raw_method_tags, list) or len(raw_method_tags) > 10:
        raise ValueError("做法标签无效")
    method_tags = list(dict.fromkeys(clean_text(item, 30, "做法标签") for item in raw_method_tags))
    slots = payload.get("meal_slots", payload.get("mealSlots", ["lunch", "dinner"]))
    if not isinstance(slots, list) or not slots or any(str(slot) not in MEAL_SLOTS for slot in slots):
        raise ValueError("适合餐次无效")
    slots = list(dict.fromkeys(str(slot) for slot in slots))
    try:
        servings = int(payload.get("base_servings", payload.get("servings", 2)))
        prep_minutes = int(payload.get("prep_minutes", payload.get("prepMinutes", 0)))
        cook_minutes = int(payload.get("cook_minutes", payload.get("cookMinutes", 0)))
        passive_minutes = int(payload.get("passive_minutes", payload.get("passiveMinutes", 0)))
    except (TypeError, ValueError) as error:
        raise ValueError("份数和用时必须是整数") from error
    if not 1 <= servings <= 20:
        raise ValueError("默认份数必须在 1 到 20 之间")
    if not 0 <= prep_minutes <= 1440 or not 0 <= cook_minutes <= 1440 or not 0 <= passive_minutes <= 10080:
        raise ValueError("准备、烹饪和等待时间必须在合理范围内")
    difficulty = str(payload.get("difficulty", "easy")).strip().lower()
    if difficulty not in DIFFICULTIES:
        raise ValueError("难度无效")
    raw_ingredients = payload.get("ingredients")
    if not isinstance(raw_ingredients, list) or not 1 <= len(raw_ingredients) <= 50:
        raise ValueError("菜谱必须包含 1 到 50 项食材")
    ingredients = [normalize_ingredient(item, index) for index, item in enumerate(raw_ingredients, 1)]
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not 2 <= len(raw_steps) <= 30:
        raise ValueError("菜谱必须包含 2 到 30 个步骤")
    steps = []
    for index, item in enumerate(raw_steps, 1):
        item = {"instruction": item} if isinstance(item, str) else item
        if not isinstance(item, dict):
            raise ValueError("菜谱步骤格式无效")
        instruction = clean_text(item.get("instruction"), 500, f"第 {index} 步")
        if require_chinese and not contains_chinese(instruction):
            raise ValueError("菜谱步骤必须使用中文")
        timer = item.get("timer_seconds")
        if timer in (None, ""):
            timer_value = None
        else:
            try:
                timer_value = int(timer)
            except (TypeError, ValueError) as error:
                raise ValueError("步骤计时必须是整数") from error
            if not 0 <= timer_value <= 86400:
                raise ValueError("步骤计时超出范围")
        steps.append({
            "title": clean_text(item.get("title", f"第 {index} 步"), 80, "步骤标题"),
            "instruction": instruction,
            "timer_seconds": timer_value,
            "heat_level": clean_text(item.get("heat_level", "按步骤调整"), 80, "火力说明"),
            "key_point": clean_text(item.get("key_point", "达到步骤描述的状态后再继续。"), 300, "完成判断"),
            "position": index,
        })
    tips = payload.get("tips", [])
    allergens = payload.get("allergen_tags", payload.get("allergenTags", []))
    if not isinstance(tips, list) or len(tips) > 20:
        raise ValueError("技巧列表无效")
    if not isinstance(allergens, list) or len(allergens) > 20:
        raise ValueError("过敏原列表无效")
    raw_media = payload.get("media", [])
    if not isinstance(raw_media, list) or len(raw_media) > 20:
        raise ValueError("菜谱媒体列表无效")
    media = []
    for item in raw_media:
        if not isinstance(item, dict):
            raise ValueError("菜谱媒体格式无效")
        media_type = str(item.get("media_type", item.get("type", "image"))).strip().lower()
        role = str(item.get("role", "cover" if media_type == "image" else "reference")).strip().lower()
        url = clean_text(item.get("url"), 2000, "媒体链接")
        if media_type not in {"image", "video", "link"} or role not in {"cover", "step", "paired", "reference", "search"}:
            raise ValueError("菜谱媒体类型无效")
        # User-uploaded exact recipe photos are served through the authenticated
        # media endpoint.  Keep the accepted relative form deliberately narrow;
        # arbitrary relative paths must never become recipe media.
        if not (
            re.fullmatch(r"https?://[^\s]+", url, flags=re.I)
            or re.fullmatch(r"/api/v1/media/media_[A-Za-z0-9_-]+", url)
        ):
            raise ValueError("菜谱媒体链接必须使用 HTTP/HTTPS，或已上传的站内图片")
        match_status = str(item.get("match_status", "reference" if media_type != "image" else "unverified"))
        if match_status not in {"verified", "reference", "unverified", "rejected"}:
            raise ValueError("媒体匹配状态无效")
        media.append({
            "media_type": media_type,
            "role": role,
            "url": url,
            "thumbnail_url": clean_text(item.get("thumbnail_url", ""), 2000, "媒体封面", required=False),
            "title": clean_text(item.get("title", ""), 160, "媒体标题", required=False),
            "provider": clean_text(item.get("provider", ""), 60, "媒体来源", required=False),
            "attribution": clean_text(item.get("attribution", ""), 200, "媒体署名", required=False),
            "license_note": clean_text(item.get("license_note", ""), 200, "媒体授权说明", required=False),
            "match_status": match_status,
            # Matching evidence is stored with the media instead of being
            # inferred again from a similar dish name later.  Only bounded,
            # plain JSON fields are accepted here; provider responses never
            # become executable content.
            "match_details": {
                str(key)[:80]: (
                    [str(entry)[:200] for entry in value[:20]]
                    if isinstance(value, list)
                    else str(value)[:500]
                )
                for key, value in (item.get("match_details") or {}).items()
                if isinstance(key, str) and isinstance(value, (str, int, float, bool, list))
            } if isinstance(item.get("match_details", {}), dict) else {},
        })
    allergen_evidence = allergen_report({str(item["name"]) for item in ingredients})
    return {
        "dish_id": clean_text(payload.get("dish_id", ""), 80, "菜品分组", required=False),
        "name": name,
        "normalized_name": normalize_name(name),
        "dish_name": dish_name,
        "normalized_dish_name": normalize_name(dish_name),
        "variant_name": variant_name,
        "cooking_method": cooking_method,
        "method_tags": method_tags,
        "cuisine": cuisine,
        "category": category,
        "meal_slots": slots,
        "base_servings": servings,
        "prep_minutes": prep_minutes,
        "cook_minutes": cook_minutes,
        "passive_minutes": passive_minutes,
        "difficulty": difficulty,
        "ingredients": ingredients,
        "steps": steps,
        "tips": [clean_text(item, 300, "烹饪技巧") for item in tips],
        # Provider/model supplied labels are not trusted as facts.  Every
        # local, custom, AI and external recipe uses the same ingredient-based
        # resolver so the displayed result cannot be hard-coded per provider.
        "allergen_tags": inferred_allergens({str(item["name"]) for item in ingredients}),
        "allergen_details": allergen_evidence["allergens"],
        "allergen_review_ingredients": allergen_evidence["review_ingredients"],
        "allergen_note": allergen_evidence["note"],
        "media": media,
    }


def validate_detailed_recipe(payload: dict) -> dict:
    """Return a normalized recipe only when every cooking detail is actionable."""

    recipe = normalize_recipe_payload(payload)
    for ingredient in recipe["ingredients"]:
        if ingredient["quantity_milli"] is None:
            raise ValueError(f"{ingredient['name']}必须提供明确用量")
        if not ingredient["preparation"]:
            raise ValueError(f"{ingredient['name']}必须提供处理方法")
    for index, step in enumerate(recipe["steps"], 1):
        if not step["timer_seconds"] or step["timer_seconds"] <= 0:
            raise ValueError(f"第 {index} 步必须提供操作时间")
        if step["heat_level"] in {"", "按步骤调整"}:
            raise ValueError(f"第 {index} 步必须提供明确火力或温度")
        if not step["key_point"]:
            raise ValueError(f"第 {index} 步必须提供完成判断")
    return recipe


def load_builtin_recipes(path: str | Path | None = None) -> list[dict]:
    source = Path(path) if path else Path(__file__).resolve().parent / "data" / "recipes.zh-CN.json"
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("内置中文菜谱文件不可用") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("recipes"), list):
        raise RuntimeError("内置中文菜谱格式无效")
    all_recipes = [*payload["recipes"], *build_expansion_recipes()]
    result = []
    ids = set()
    names = set()
    for raw in all_recipes:
        if not isinstance(raw, dict):
            raise RuntimeError("内置中文菜谱格式无效")
        recipe_id = str(raw.get("id", "")).strip()
        if not re.fullmatch(r"recipe_builtin_[a-z0-9_]{2,80}", recipe_id) or recipe_id in ids:
            raise RuntimeError("内置中文菜谱 ID 无效或重复")
        ids.add(recipe_id)
        detailed = enrich_builtin_recipe(raw)
        normalized = validate_detailed_recipe(detailed)
        if normalized["normalized_name"] in names:
            raise RuntimeError("内置中文菜谱名称重复")
        names.add(normalized["normalized_name"])
        normalized.update({"id": recipe_id, "data_version": max(1, int(detailed.get("version", 1)))})
        result.append(normalized)
    if not 200 <= len(result) <= 300:
        raise RuntimeError("内置中文菜谱数量必须保持在 200 到 300 道")
    # Fail closed: every built-in recipe must pass the catalog-wide content,
    # timing, quantity, allergen and food-safety audit before it can be served.
    audit_normalized_catalog(result)
    return result


def recipe_snapshot(recipe: dict) -> dict:
    return {
        key: recipe[key]
        for key in (
            "id", "dish_id", "dish_name", "name", "variant_name", "cooking_method", "method_tags",
            "cuisine", "category", "meal_slots", "base_servings",
            "prep_minutes", "cook_minutes", "passive_minutes", "difficulty", "ingredients", "steps",
            "tips", "allergen_tags", "allergen_details", "allergen_review_ingredients", "allergen_note", "source_type",
        )
        if key in recipe
    }


def scaled_quantity(quantity_milli: int | None, actual_servings: int, base_servings: int) -> int | None:
    if quantity_milli is None:
        return None
    value = Decimal(quantity_milli) * Decimal(actual_servings) / Decimal(base_servings)
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_quantity(quantity_milli: int | None, canonical_unit: str, quantity_text: str = "") -> str:
    if quantity_milli is None:
        return quantity_text or "适量"
    value = Decimal(quantity_milli) / Decimal(1000)
    if canonical_unit == "g" and value >= 1000:
        return f"{_plain(value / 1000)} 千克"
    if canonical_unit == "ml" and value >= 1000:
        return f"{_plain(value / 1000)} 升"
    labels = {"g": "克", "ml": "毫升", "piece": "个"}
    return f"{_plain(value)} {labels.get(canonical_unit, canonical_unit)}".strip()


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f").rstrip("0").rstrip(".") if "." in format(value.normalize(), "f") else format(value, "f")


def build_shopping_items(meal_items: Iterable[dict]) -> list[dict]:
    # Imported lazily because the kitchen domain reuses the recipe
    # canonicalizer.  Generated shopping rows always use the fixed nine-key
    # contract; the old seven Chinese labels remain read-only compatibility.
    from .kitchen import SHOPPING_CATEGORIES, shopping_category_key

    merged: dict[tuple[str, str, str], dict] = {}
    vague_counter = 0
    for meal in meal_items:
        snapshot = meal.get("recipe_snapshot") or {}
        base_servings = max(1, int(snapshot.get("base_servings", 1)))
        actual_servings = max(1, int(meal.get("servings", base_servings)))
        for ingredient in snapshot.get("ingredients", []):
            canonical_name = str(ingredient.get("canonical_name") or canonical_ingredient_name(ingredient.get("name")))
            # Water is useful in a measured recipe, but it is not a grocery item.
            if canonical_name == "水":
                continue
            family = str(ingredient.get("unit_family", "vague"))
            specification = str(ingredient.get("specification", ""))
            quantity = scaled_quantity(ingredient.get("quantity_milli"), actual_servings, base_servings)
            key = (canonical_name, family, specification)
            if family == "vague":
                vague_counter += 1
                key = (canonical_name, family, f"{specification}#{vague_counter}")
            row = merged.get(key)
            source = {
                "meal_item_id": meal["id"],
                "recipe_ingredient_id": str(ingredient.get("id") or f"snapshot-{ingredient.get('position', 0)}"),
                "contributed_quantity_milli": quantity,
                "recipe_name": snapshot.get("name", "菜谱"),
            }
            if row:
                if quantity is not None:
                    row["quantity_milli"] += quantity
                row["sources"].append(source)
                continue
            row = {
                "canonical_name": canonical_name,
                "display_name": str(ingredient.get("name") or canonical_name),
                "quantity_milli": quantity,
                "quantity_text": str(ingredient.get("quantity_text", "")),
                "canonical_unit": str(ingredient.get("canonical_unit", "vague")),
                "unit_family": family,
                "category": shopping_category_key(canonical_name),
                "specification": specification.split("#", 1)[0],
                "sources": [source],
            }
            merged[key] = row
    category_order = {name: index for index, (name, _) in enumerate(SHOPPING_CATEGORIES)}
    result = sorted(merged.values(), key=lambda row: (category_order.get(row["category"], 99), row["display_name"], row["specification"]))
    for index, row in enumerate(result, 1):
        row["position"] = index
        row["display_quantity"] = format_quantity(row["quantity_milli"], row["canonical_unit"], row["quantity_text"])
    return result


def shopping_diff(existing: Iterable[dict], generated: Iterable[dict]) -> dict:
    def key(row: dict) -> tuple[str, str, str]:
        return (str(row.get("canonical_name")), str(row.get("unit_family")), str(row.get("specification", "")))

    old = {key(row): row for row in existing if not row.get("manually_added")}
    new = {key(row): row for row in generated}
    added, updated, removed, protected = [], [], [], []
    for item_key, row in new.items():
        previous = old.get(item_key)
        if not previous:
            added.append(row)
        elif previous.get("manual_locked"):
            if previous.get("quantity_milli") != row.get("quantity_milli") or previous.get("quantity_text") != row.get("quantity_text"):
                protected.append({"current": previous, "suggested": row})
        elif previous.get("quantity_milli") != row.get("quantity_milli") or previous.get("quantity_text") != row.get("quantity_text"):
            updated.append({"before": previous, "after": row})
    for item_key, row in old.items():
        if item_key not in new:
            if row.get("manual_locked"):
                protected.append({"current": row, "suggested": None})
            else:
                removed.append(row)
    return {
        "added": added,
        "updated": updated,
        "removed": removed,
        "protected": protected,
        "warnings": ["人工修改的食材不会被自动覆盖"] if protected else [],
    }
