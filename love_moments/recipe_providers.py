"""External and OpenAI-compatible providers for Chinese recipe drafts."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib import error, parse, request

from .http_client import TRUSTED_SSL_CONTEXT
from .quota import QuotaExceededError
from .recipe_catalog_quality import CONCENTRATED_MAX_GRAMS, allergen_report, inferred_allergens, safety_tip
from .recipes import load_builtin_recipes, validate_detailed_recipe


def _strip_markup(value: Any) -> Any:
    if isinstance(value, str):
        return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", html.unescape(value))).strip()
    if isinstance(value, list):
        return [_strip_markup(item) for item in value]
    if isinstance(value, dict):
        return {key: _strip_markup(item) for key, item in value.items()}
    return value


def _enabled(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _json_request(url: str, *, headers: dict[str, str] | None = None, data: dict | None = None, timeout: int = 20) -> dict:
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    req = request.Request(url, data=payload, headers={"Accept": "application/json", **(headers or {})}, method="POST" if data is not None else "GET")
    with request.urlopen(req, timeout=max(5, min(timeout, 60)), context=TRUSTED_SSL_CONTEXT) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("菜谱服务返回格式无效")
    return value


def _model_content(response: dict) -> dict:
    content = response["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise ValueError("模型没有返回文字")
    content = content.strip()
    if not content:
        raise ValueError("模型返回空内容")
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I)
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("模型返回内容不是对象")
    return value


def _llm_base_url() -> str:
    """Use the same OpenAI-compatible endpoint as the rest of the app.

    LLM_API_URL existed only in an early recipe draft.  Keeping it as a
    backwards-compatible alias prevents an older private .env from breaking,
    while LLM_BASE_URL remains the documented setting.
    """

    return (
        os.getenv("LLM_BASE_URL")
        or os.getenv("LLM_API_URL")
        or "https://api.deepseek.com/v1"
    ).strip().rstrip("/")


def _finalize_external_recipe(payload: dict) -> dict:
    """Normalize and safety-check one model-localized external recipe.

    External provider data is untrusted.  It is never shown as an actionable
    draft until quantities, preparation, timers, heat levels and concentrated
    seasonings pass the same conservative boundaries used by the local catalog.
    """

    recipe = validate_detailed_recipe(_strip_markup(payload))
    ingredient_names = {str(item["name"]) for item in recipe["ingredients"]}

    # Model-written temperature claims are replaced with a deterministic rule
    # derived from the actual ingredient list.
    tips = [tip for tip in recipe["tips"] if "食品安全" not in tip and "中心温度" not in tip]
    tips.append("保存提示：含肉、蛋、奶或熟食不要在室温放置超过 2 小时；剩余食物及时密封冷藏。")
    expected_safety = safety_tip(ingredient_names)
    if expected_safety:
        tips.append(expected_safety)
    recipe["tips"] = list(dict.fromkeys(tips))[:20]
    # Never trust or merge a provider/model allergen label.  Recompute it from
    # the normalized ingredient rows using the same resolver as local recipes.
    evidence = allergen_report(ingredient_names)
    recipe["allergen_tags"] = inferred_allergens(ingredient_names)
    recipe["allergen_details"] = evidence["allergens"]
    recipe["allergen_review_ingredients"] = evidence["review_ingredients"]
    recipe["allergen_note"] = evidence["note"]

    for item in recipe["ingredients"]:
        quantity = int(item.get("quantity_milli") or 0) / 1000
        unit = item.get("canonical_unit")
        name = str(item.get("name") or "")
        if quantity <= 0:
            raise ValueError(f"{name} 缺少明确用量")
        if (unit == "g" and quantity > 2000) or (unit == "ml" and quantity > 5000) or (unit == "piece" and quantity > 60):
            raise ValueError(f"{name} 用量超出家庭菜谱合理边界")
        maximum = CONCENTRATED_MAX_GRAMS.get(name)
        if maximum is not None and unit == "g" and quantity > maximum:
            raise ValueError(f"{name} {quantity:g} 克超过安全审核上限 {maximum} 克")

    searchable = "".join(
        str(step.get(field, ""))
        for step in recipe["steps"]
        for field in ("title", "instruction", "heat_level", "key_point")
    )
    referenced = sum(1 for name in ingredient_names if name and name in searchable)
    if ingredient_names and referenced < max(1, len(ingredient_names) // 3):
        raise ValueError("步骤与食材表匹配度过低")
    return recipe


class LocalRecipeGenerator:
    provider = "local-rules"

    def generate(self, request_data: dict) -> dict:
        prompt = str(request_data.get("name") or request_data.get("prompt") or "创意家常菜").strip()
        rows = load_builtin_recipes()
        compact = re.sub(r"\s+", "", prompt)
        match = next((row for row in rows if row["name"] in compact or compact in row["name"]), None)
        if match:
            recipe = {key: value for key, value in match.items() if key not in {"id", "data_version", "normalized_name"}}
        else:
            chinese_name = prompt[:40] if re.search(r"[\u3400-\u9fff]", prompt) else f"创意{prompt[:30]}"
            recipe = {
                "name": chinese_name,
                "cuisine": str(request_data.get("cuisine", "chinese")) if str(request_data.get("cuisine", "chinese")) in {"chinese", "western"} else "chinese",
                "category": "自定义菜谱",
                "meal_slots": request_data.get("meal_slots", ["lunch", "dinner"]),
                "base_servings": int(request_data.get("servings", 2) or 2),
                "prep_minutes": 15,
                "cook_minutes": 20,
                "difficulty": "easy",
                "ingredients": [
                    {"name": "主要食材", "quantity": 300, "unit": "克", "specification": "请确认后替换为实际食材", "group": "main", "preparation": "洗净或擦干后切成大小一致的块"},
                    {"name": "时令蔬菜", "quantity": 200, "unit": "克", "group": "main", "preparation": "洗净、沥干并切成适合入口的大小"},
                    {"name": "盐", "quantity": 2, "unit": "克", "group": "seasoning", "preparation": "提前称好"},
                    {"name": "植物油", "quantity": 15, "unit": "毫升", "group": "seasoning", "preparation": "提前量好"},
                ],
                "steps": [
                    {"title": "备料", "instruction": "主要食材和蔬菜按食材表处理；称出盐 2 克、植物油 15 毫升。", "timer_seconds": 300, "heat_level": "无需加热", "key_point": "全部切配和称量完成后再开火。"},
                    {"title": "加热主料", "instruction": "锅烧热后加入植物油 15 毫升，放入主要食材并铺开。", "timer_seconds": 360, "heat_level": "中火", "key_point": "主要食材中心完全熟透后再继续。"},
                    {"title": "加入蔬菜", "instruction": "加入时令蔬菜翻炒，使食材均匀受热。", "timer_seconds": 240, "heat_level": "中大火", "key_point": "蔬菜断生但仍保持颜色和口感。"},
                    {"title": "调味出锅", "instruction": "均匀撒入盐 2 克，快速翻匀后关火装盘。", "timer_seconds": 60, "heat_level": "中火后关火", "key_point": "先尝味再追加调料，确认所有食材熟透。"},
                ],
                "tips": ["这是离线生成的可编辑草稿，请在保存前确认食材、火候和过敏原。"],
                "allergen_tags": [],
            }
        return {"recipe": validate_detailed_recipe(recipe), "provider": self.provider, "mock": True}


class OpenAICompatibleRecipeGenerator:
    provider = "openai-compatible"

    def __init__(self, api_key: str, base_url: str, model: str, before_request: Callable[..., Any]):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.before_request = before_request
        self.fallback = LocalRecipeGenerator()

    def generate(self, request_data: dict) -> dict:
        prompt = {
            "task": "生成一份可实际操作的中文菜谱草稿",
            "request": request_data,
            "requirements": {
                "language": "所有用户可见文字必须为简体中文",
                "cuisine": "chinese 或 western",
                "meal_slots": ["breakfast", "lunch", "dinner", "snack"],
                "units": ["克", "千克", "毫升", "升", "个", "片", "根", "瓣", "汤匙", "茶匙", "适量", "少许"],
                "ingredients": "1 到 20 项；主料和所有调味料分别列出，调味料必须给出可称量的近似用量；每项包含 name、quantity、unit、group(main|seasoning)、preparation",
                "steps": "至少 4 个、最多 15 个明确步骤；每步包含 title、instruction、timer_seconds、heat_level、key_point，写清加入的调料量、火力、时长和成熟判断",
            },
        }
        body = {
            "model": self.model,
            "temperature": 0.35,
            "max_tokens": 3000,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "你是严谨的中文家庭菜谱助手。只返回严格 JSON，不使用 Markdown，不输出英文步骤。菜谱必须能让新手照做：食材写清预处理，调味料给近似克数或毫升数，步骤写清火力、时长和完成判断。不要提供危险、生食或无法确认安全性的做法。"},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
        }
        try:
            self.before_request(["llm", "recipe_llm"], operation="recipe generate")
            response = _json_request(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                data=body,
                timeout=int(os.getenv("LLM_TIMEOUT_SECONDS", "30")),
            )
            value = _model_content(response)
            recipe = value.get("recipe", value)
            recipe = _finalize_external_recipe(recipe)
            return {"recipe": recipe, "provider": self.provider, "mock": False}
        except QuotaExceededError:
            return self.fallback.generate(request_data)
        except (error.URLError, TimeoutError, KeyError, IndexError, ValueError, TypeError, json.JSONDecodeError, RuntimeError):
            return self.fallback.generate(request_data)


def recipe_generator_from_env(store) -> LocalRecipeGenerator | OpenAICompatibleRecipeGenerator:
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        return LocalRecipeGenerator()
    base_url = _llm_base_url()
    model = os.getenv("LLM_MODEL", "deepseek-chat").strip() or "deepseek-chat"
    return OpenAICompatibleRecipeGenerator(api_key, base_url, model, store.reserve_external_apis)


class CachedRecipeProvider:
    provider = "base"
    ttl_seconds = 3600

    def __init__(self, store):
        self.store = store

    def _cached(self, key_payload: dict, operation: str, fetch: Callable[[], dict]) -> dict:
        cache_key = hashlib.sha256(f"{self.provider}:{json.dumps(key_payload, sort_keys=True, ensure_ascii=False)}".encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        with self.store.connection() as db:
            row = db.execute("SELECT response_json,expires_at FROM recipe_provider_cache WHERE cache_key=?", (cache_key,)).fetchone()
            if row and row["expires_at"] > now.isoformat():
                try:
                    cached = json.loads(row["response_json"])
                    if isinstance(cached, dict):
                        return cached
                except json.JSONDecodeError:
                    pass
        self.store.reserve_external_api(self.provider, operation=operation)
        value = fetch()
        expires = (now + timedelta(seconds=self.ttl_seconds)).isoformat()
        with self.store.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO recipe_provider_cache(cache_key,provider,response_json,created_at,expires_at) VALUES (?,?,?,?,?)",
                (cache_key, self.provider, json.dumps(value, ensure_ascii=False), now.isoformat(), expires),
            )
            db.execute("DELETE FROM recipe_provider_cache WHERE expires_at<=?", (now.isoformat(),))
        return value


class TheMealDbProvider(CachedRecipeProvider):
    provider = "themealdb"

    def __init__(self, store):
        super().__init__(store)
        self.ttl_seconds = max(300, min(int(os.getenv("THEMEALDB_CACHE_TTL_SECONDS", "86400")), 604800))

    def search(self, keyword: str, limit: int) -> list[dict]:
        url = "https://www.themealdb.com/api/json/v1/1/search.php?" + parse.urlencode({"s": keyword})
        response = self._cached({"query": keyword}, "recipe search", lambda: _json_request(url))
        rows = response.get("meals") or []
        return [self._normalize(row) for row in rows[:limit] if isinstance(row, dict)]

    @staticmethod
    def _normalize(row: dict) -> dict:
        ingredients = []
        for index in range(1, 21):
            name = str(row.get(f"strIngredient{index}") or "").strip()
            measure = str(row.get(f"strMeasure{index}") or "").strip()
            if name:
                ingredients.append({"name": name, "measure": measure})
        return {
            "external_id": str(row.get("idMeal") or ""),
            "name": str(row.get("strMeal") or ""),
            "category": str(row.get("strCategory") or ""),
            "area": str(row.get("strArea") or ""),
            "ingredients": ingredients,
            "instructions": str(row.get("strInstructions") or ""),
            "media": ([{
                "media_type": "image", "role": "cover", "provider": "themealdb",
                "url": str(row.get("strMealThumb") or ""), "title": str(row.get("strMeal") or ""),
                "attribution": "TheMealDB", "match_status": "verified",
                "match_details": {
                    "binding": "provider_recipe_result",
                    "source_recipe_name": str(row.get("strMeal") or ""),
                    "source_recipe_id": str(row.get("idMeal") or ""),
                },
            }] if row.get("strMealThumb") else []),
        }


class TianApiRecipeProvider(CachedRecipeProvider):
    """Official Chinese recipe search from TianAPI (天聚数行)."""

    provider = "tianapi_recipe"

    def __init__(self, store, api_key: str):
        super().__init__(store)
        self.api_key = api_key
        self.ttl_seconds = max(
            300,
            min(int(os.getenv("TIANAPI_RECIPE_CACHE_TTL_SECONDS", "86400")), 604800),
        )

    def search(self, keyword: str, limit: int) -> list[dict]:
        params = {
            "key": self.api_key,
            "word": keyword,
            "num": limit,
            "page": 1,
        }
        url = "https://apis.tianapi.com/caipu/index?" + parse.urlencode(params)

        def fetch() -> dict:
            value = _json_request(url)
            try:
                code = int(value.get("code", 0))
            except (TypeError, ValueError):
                code = 0
            if code != 200:
                raise RuntimeError(str(value.get("msg") or "天聚数行菜谱服务返回错误"))
            return value

        response = self._cached(
            {"query": keyword, "limit": limit},
            "recipe search",
            fetch,
        )
        result = response.get("result") or {}
        rows = result.get("list") or result.get("newslist") or []
        if not isinstance(rows, list):
            raise RuntimeError("天聚数行菜谱服务返回格式无效")
        return [self._normalize(row) for row in rows[:limit] if isinstance(row, dict)]

    @staticmethod
    def _ingredient_rows(value: object, group: str) -> list[dict]:
        text = _strip_markup(str(value or ""))
        return [
            {"name": item, "measure": "", "group": group}
            for item in re.split(r"[；;\n]+", text)
            if item.strip()
        ]

    @classmethod
    def _normalize(cls, row: dict) -> dict:
        ingredients = [
            *cls._ingredient_rows(row.get("yuanliao"), "main"),
            *cls._ingredient_rows(row.get("tiaoliao"), "seasoning"),
        ]
        image_url = str(
            row.get("pic") or row.get("image") or row.get("img")
            or row.get("thumb") or row.get("thumbnail") or ""
        ).strip()
        image_media = []
        if re.fullmatch(r"https?://[^\s]+", image_url, flags=re.I):
            source_name = str(row.get("cp_name") or row.get("name") or row.get("title") or "")
            image_media = [{
                "media_type": "image", "role": "cover", "provider": "tianapi_recipe",
                "url": image_url, "title": source_name, "attribution": "天聚数行菜谱",
                "match_status": "verified",
                "match_details": {
                    "binding": "provider_recipe_result",
                    "source_recipe_name": source_name,
                    "source_recipe_id": str(row.get("id") or row.get("type_id") or ""),
                },
            }]
        return {
            "external_id": str(row.get("id") or row.get("type_id") or ""),
            "name": str(row.get("cp_name") or row.get("name") or row.get("title") or ""),
            "category": str(row.get("type_name") or row.get("type") or "中式菜谱"),
            "area": "中国",
            "ingredients": ingredients,
            "instructions": str(row.get("zuofa") or row.get("content") or ""),
            "tips": [str(row.get("tishi") or "")] if row.get("tishi") else [],
            "characteristics": str(row.get("texing") or ""),
            "media": image_media,
        }


class SpoonacularProvider(CachedRecipeProvider):
    provider = "spoonacular"

    def __init__(self, store, api_key: str):
        super().__init__(store)
        self.api_key = api_key
        self.ttl_seconds = max(60, min(int(os.getenv("SPOONACULAR_CACHE_TTL_SECONDS", "3600")), 3600))

    def search(self, keyword: str, limit: int) -> list[dict]:
        params = {
            "apiKey": self.api_key,
            "query": keyword,
            "number": limit,
            "addRecipeInformation": "true",
            "fillIngredients": "true",
        }
        url = "https://api.spoonacular.com/recipes/complexSearch?" + parse.urlencode(params)
        response = self._cached({"query": keyword, "limit": limit}, "recipe search", lambda: _json_request(url))
        result = []
        for row in response.get("results", []):
            if not isinstance(row, dict):
                continue
            steps = []
            for block in row.get("analyzedInstructions") or []:
                steps.extend(str(step.get("step") or "") for step in block.get("steps") or [] if step.get("step"))
            result.append({
                "external_id": str(row.get("id") or ""),
                "name": str(row.get("title") or ""),
                "category": "",
                "area": "",
                "ingredients": [
                    {"name": str(item.get("name") or ""), "measure": str((item.get("measures") or {}).get("metric", {}).get("amount") or "") + " " + str((item.get("measures") or {}).get("metric", {}).get("unitShort") or "")}
                    for item in row.get("extendedIngredients") or [] if item.get("name")
                ],
                "instructions": _strip_markup("\n".join(steps) or str(row.get("summary") or "")),
                "media": ([{
                    "media_type": "image", "role": "cover", "provider": "spoonacular",
                    "url": str(row.get("image") or ""), "title": str(row.get("title") or ""),
                    "attribution": "Spoonacular", "match_status": "verified",
                    "match_details": {
                        "binding": "provider_recipe_result",
                        "source_recipe_name": str(row.get("title") or ""),
                        "source_recipe_id": str(row.get("id") or ""),
                    },
                }] if row.get("image") else []),
            })
        return result


class OnlineRecipeSearch:
    QUERY_MAP = {
        "鸡肉": "chicken", "牛肉": "beef", "猪肉": "pork", "鱼": "fish", "虾": "shrimp",
        "意面": "pasta", "披萨": "pizza", "沙拉": "salad", "汤": "soup", "甜品": "dessert",
        "早餐": "breakfast", "土豆": "potato", "蘑菇": "mushroom", "番茄": "tomato",
    }

    def __init__(self, provider: CachedRecipeProvider | None, store, *, disabled_notice: str = ""):
        self.provider = provider
        self.store = store
        self.disabled_notice = disabled_notice

    def _fallback(self, keyword: str, limit: int, notice: str) -> dict:
        rows = load_builtin_recipes()
        matches = [row for row in rows if keyword in row["name"] or keyword in row["category"]]
        if not matches:
            matches = rows
        items = []
        for row in matches[:limit]:
            items.append({key: value for key, value in row.items() if key not in {"id", "data_version", "normalized_name"}})
        return {"items": items, "provider": "local-rules", "mock": True, "notice": notice}

    def search_and_localize(self, keyword: str, limit: int) -> dict:
        if not self.provider:
            return self._fallback(keyword, limit, self.disabled_notice or "外部菜谱未启用，已使用本地中文菜谱")
        search_term = keyword if self.provider.provider == "tianapi_recipe" else next(
            (english for chinese, english in self.QUERY_MAP.items() if chinese in keyword),
            keyword,
        )
        try:
            raw = self.provider.search(search_term, limit)
            if not raw:
                return self._fallback(keyword, limit, "外部来源没有匹配结果，已显示本地中文菜谱")
            key = os.getenv("LLM_API_KEY", "").strip()
            if not key:
                return self._fallback(keyword, limit, "未配置中文转换模型，未展示英文结果")
            localized = self._localize_many(raw, key)
            return {"items": localized, "provider": self.provider.provider, "mock": False, "notice": "外部菜谱已转换为中文草稿，请确认后保存"}
        except QuotaExceededError:
            return self._fallback(keyword, limit, "外部或 AI 菜谱额度已用完，已使用本地中文菜谱")
        except (error.URLError, TimeoutError, KeyError, IndexError, ValueError, TypeError, json.JSONDecodeError, RuntimeError):
            return self._fallback(keyword, limit, "外部菜谱暂时不可用，已使用本地中文菜谱")

    def _localize_many(self, raw: list[dict], api_key: str) -> list[dict]:
        base_url = _llm_base_url()
        model = os.getenv("LLM_MODEL", "deepseek-chat").strip() or "deepseek-chat"
        localized: list[dict] = []
        # A five-recipe prompt can exhaust the model output limit halfway
        # through JSON and make every result unusable.  Convert each provider
        # row independently, account for each request, and retain only rows
        # that pass validation.  The API endpoint caps this at three.
        failures: list[str] = []
        for source in raw:
            try:
                body = {
                    "model": model,
                    "temperature": 0.2,
                    "max_tokens": 3000,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": "把外部菜谱转换为简体中文结构化菜谱。只返回严格 JSON。不能输出 HTML、Markdown 或英文步骤。必须区分菜品名与具体做法：dish_name 写菜品通用名，variant_name 写家常版、低脂版、烤箱版等具体做法，cooking_method 写核心烹饪方式。将单位换成克、千克、毫升、升、个、片、根、瓣、汤匙或茶匙；能换算时不要使用模糊的适量或少许。补全食材处理、近似调料量、火力、时长和完成判断。准备时间只计算动手操作；浸泡、腌制、冷藏、发酵和冷却必须计入 passive_minutes，并在步骤中明确写出。不得臆造危险的生食方法。"},
                        {"role": "user", "content": json.dumps({"task": "转换一份菜谱", "recipe": source, "output": {"recipe": {"name": "中文菜名", "dish_name": "不含做法修饰的菜品名", "variant_name": "具体做法版本", "cooking_method": "核心烹饪方式", "method_tags": ["家常"], "cuisine": "chinese|western", "category": "分类", "meal_slots": ["lunch", "dinner"], "base_servings": 2, "prep_minutes": 15, "cook_minutes": 30, "passive_minutes": 0, "difficulty": "easy|medium|hard", "ingredients": [{"name": "食材或调味料", "quantity": 100, "unit": "克", "specification": "", "group": "main|seasoning", "preparation": "清洗、切配或称量方法"}], "steps": [{"title": "步骤标题", "instruction": "含具体调料量的中文步骤", "timer_seconds": 300, "heat_level": "中火或明确温度", "key_point": "判断完成的颜色、质地、中心温度或状态"}], "tips": [], "allergen_tags": []}}}, ensure_ascii=False)},
                    ],
                }
                value = None
                attempt_errors: list[str] = []
                for attempt in range(2):
                    # DeepSeek documents that JSON mode can occasionally
                    # return empty content.  One bounded retry drops forced
                    # JSON mode but keeps the strict prompt; every real call
                    # is separately charged against the local hard quota.
                    self.store.reserve_external_apis(["llm", "recipe_llm"], operation="recipe localization")
                    request_body = dict(body)
                    if attempt:
                        request_body.pop("response_format", None)
                        request_body["messages"] = [
                            {**body["messages"][0], "content": body["messages"][0]["content"] + " 上一次返回为空；这次必须直接从左花括号开始输出完整 JSON 对象。"},
                            body["messages"][1],
                        ]
                    try:
                        response = _json_request(
                            f"{base_url}/chat/completions",
                            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                            data=request_body,
                            timeout=int(os.getenv("LLM_TIMEOUT_SECONDS", "60")),
                        )
                        value = _model_content(response)
                        break
                    except (error.URLError, TimeoutError, ValueError, KeyError, IndexError, json.JSONDecodeError, RuntimeError) as attempt_failure:
                        attempt_errors.append(f"{type(attempt_failure).__name__}: {str(attempt_failure)[:120]}")
                if value is None:
                    raise ValueError("；".join(attempt_errors) or "模型未返回可解析内容")
                row = value.get("recipe", value)
                normalized = _finalize_external_recipe(row)
                # Images stay bound to the exact provider result that supplied
                # the ingredients and instructions.  We never substitute a
                # same-category or similarly named dish image.
                normalized["media"] = source.get("media", [])
                localized.append(normalized)
            except QuotaExceededError:
                if not localized:
                    raise
                break
            except (error.URLError, TimeoutError, ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError, RuntimeError) as failure:
                failures.append(f"{type(failure).__name__}: {str(failure)[:160]}")
                continue
        if not localized:
            detail = "；".join(failures[:3]) or "没有返回可验证内容"
            raise ValueError(f"外部菜谱没有一份通过内容与安全校验：{detail}")
        return localized


def online_recipe_search_from_env(provider_name: str, store) -> OnlineRecipeSearch:
    provider_name = provider_name.strip().lower()
    if provider_name in {"tianapi", "tianapi_recipe"}:
        key = os.getenv("TIANAPI_RECIPE_KEY", "").strip()
        if not _enabled("TIANAPI_RECIPE_ENABLED", False) or not key:
            return OnlineRecipeSearch(None, store, disabled_notice="天聚数行中文菜谱未配置，已使用本地中文菜谱")
        return OnlineRecipeSearch(TianApiRecipeProvider(store, key), store)
    if provider_name == "themealdb":
        if not _enabled("THEMEALDB_ENABLED", True):
            return OnlineRecipeSearch(None, store, disabled_notice="TheMealDB 已关闭，已使用本地中文菜谱")
        return OnlineRecipeSearch(TheMealDbProvider(store), store)
    if provider_name == "spoonacular":
        key = os.getenv("SPOONACULAR_API_KEY", "").strip()
        if not _enabled("SPOONACULAR_ENABLED", False) or not key:
            return OnlineRecipeSearch(None, store, disabled_notice="Spoonacular 未配置，已使用本地中文菜谱")
        return OnlineRecipeSearch(SpoonacularProvider(store, key), store)
    raise ValueError("不支持的外部菜谱来源")
