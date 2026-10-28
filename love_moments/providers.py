"""Provider boundaries for production integrations.

The local MVP uses deterministic implementations. Production deployments can
replace these classes without changing API or persistence code.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from datetime import date
from urllib import error, request
from typing import Callable

from .http_client import TRUSTED_SSL_CONTEXT
from .quota import QuotaExceededError


@dataclass(frozen=True)
class GeneratedPlan:
    style: str
    name: str
    summary: str
    budget_cents: int
    intensity: str
    transport_minutes: int
    items: list[dict]
    day_themes: dict[int, str] | None = None
    generation_source: str = "rules"
    generation_message: str = "使用本地安全模板生成"


class TravelPlannerProvider:
    def generate_variants(
        self,
        destination: str,
        budget_cents: int,
        context: dict | None = None,
    ) -> list[GeneratedPlan]:
        raise NotImplementedError

    def analyze_impact(self, item: dict, changes: dict) -> dict:
        raise NotImplementedError


class DatePlannerProvider:
    def generate_variants(self, request_data: dict) -> list[dict]:
        raise NotImplementedError


class LocalDatePlanner(DatePlannerProvider):
    """Deterministic date ideas that keep the complete flow usable offline."""

    STYLES = {
        "romantic": ("轻松浪漫", "慢一点，把时间留给彼此", 0.90, "松弛"),
        "classic": ("经典充实", "吃饭、体验和散步都安排得刚刚好", 1.00, "适中"),
        "value": ("预算友好", "把预算用在最值得一起记住的地方", 0.68, "轻快"),
        "food": ("美食约会", "从一起吃喜欢的味道开始，把城市慢慢尝一遍", 0.86, "松弛"),
        "adventure": ("新鲜体验", "安排一个第一次共同尝试的活动，留下新的共同记忆", 0.96, "活力"),
    }

    def generate_variants(self, request_data: dict) -> list[dict]:
        city = str(request_data.get("city", "约会城市"))
        budget = int(request_data.get("budget_cents", 0))
        start = str(request_data.get("start_time", "14:00"))
        templates = {
            "romantic": [(start, f"{city}慢节奏下午茶", "提前选一个靠窗位置", .22, True, False), ("17:30", "一起散步看日落", "下雨时改为书店或展览", .05, False, False), ("19:00", "安静的双人晚餐", "把手机放下，好好聊天", .55, True, False)],
            "classic": [(start, f"{city}热门展览或体验", "出发前确认预约", .30, True, True), ("17:00", "城市街区散步", "预留拍照时间", .08, False, False), ("19:00", "本地特色晚餐", "选择双方都能吃的口味", .52, True, False)],
            "value": [(start, "免费公园或博物馆", "优先公共交通", .05, True, False), ("17:00", "一起完成一组主题照片", "互相为对方拍三张照片", .03, False, False), ("19:00", "高评分日常小馆", "到店前查看排队情况", .40, True, False)],
            "food": [(start, f"{city}本地甜品或咖啡", "先选双方都想尝的招牌", .18, True, False), ("17:00", "一起逛市集选晚餐", "每人负责挑一道菜", .12, False, False), ("19:00", "预约一家特色餐厅", "提前确认口味与忌口", .48, True, True)],
            "adventure": [(start, f"{city}双人体验课", "选择双方都是第一次尝试的项目", .38, True, True), ("17:30", "城市探索任务", "随机走进一条没去过的街", .08, False, False), ("19:30", "交换今天最喜欢的瞬间", "各自选一张照片收藏", .35, True, False)],
        }
        result = []
        for style, (name, summary, ratio, intensity) in self.STYLES.items():
            items = [
                {"id": f"{style}-{index}", "startTime": item[0], "title": item[1], "note": item[2], "costCents": int(budget * item[3]), "indoor": item[4], "needsReservation": item[5]}
                for index, item in enumerate(templates[style], 1)
            ]
            result.append({"style": style, "name": name, "summary": summary, "expected_cost_cents": int(budget * ratio), "intensity": intensity, "weather_fit": "雨天有备用安排", "items": items})
        return result


class OpenAICompatibleDatePlanner(DatePlannerProvider):
    def __init__(self, api_key: str, base_url: str, model: str, timeout: int = 30, before_request: Callable[[str], None] | None = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = max(5, min(timeout, 120))
        self.fallback = LocalDatePlanner()
        self.before_request = before_request

    def generate_variants(self, request_data: dict) -> list[dict]:
        prompt = {
            "task": "为情侣约会生成五套有明显差异、时间不冲突且预算合理的方案",
            "input": request_data,
            "required_styles": ["romantic", "classic", "value", "food", "adventure"],
            "output": {"plans": [{"style": "romantic|classic|value|food|adventure", "name": "名称", "summary": "摘要", "expected_cost_cny": 300, "intensity": "松弛|适中|轻快|活力", "weather_fit": "天气说明", "items": [{"start_time": "14:00", "title": "活动", "note": "执行提示", "cost_cny": 50, "indoor": True, "needs_reservation": False}]}]},
        }
        payload = {
            "model": self.model,
            "temperature": 0.65,
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "你是情侣约会规划器。只返回严格 JSON。不得提供危险、成人或违法活动。每套方案 2–6 个项目。"},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
        }
        if "api.deepseek.com" in self.base_url.lower():
            payload["thinking"] = {"type": "disabled"}
        try:
            if self.before_request:
                self.before_request("date plan")
            req = request.Request(
                f"{self.base_url}/chat/completions",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            with request.urlopen(req, timeout=self.timeout, context=TRUSTED_SSL_CONTEXT) as response:
                content = json.loads(response.read().decode("utf-8"))["choices"][0]["message"]["content"]
            return self._validate(json.loads(content), int(request_data.get("budget_cents", 0)))
        except QuotaExceededError:
            return self.fallback.generate_variants(request_data)
        except (error.URLError, TimeoutError, KeyError, ValueError, TypeError, OverflowError, UnicodeError, json.JSONDecodeError):
            return self.fallback.generate_variants(request_data)

    @staticmethod
    def _validate(value: dict, budget_cents: int) -> list[dict]:
        rows = value.get("plans")
        expected_styles = ["romantic", "classic", "value", "food", "adventure"]
        if not isinstance(rows, list) or len(rows) != len(expected_styles):
            raise ValueError("模型必须返回五套约会方案")
        plans = []
        for row in rows:
            style = str(row.get("style", ""))
            raw_items = row.get("items")
            if style not in set(expected_styles) or not isinstance(raw_items, list) or not 2 <= len(raw_items) <= 6:
                raise ValueError("模型约会方案结构无效")
            items = []
            previous_time = "00:00"
            for index, item in enumerate(raw_items, 1):
                start_time = str(item.get("start_time", ""))[:5]
                title = str(item.get("title", "")).strip()[:100]
                if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start_time) or not title or start_time < previous_time:
                    raise ValueError("模型约会时间线无效")
                previous_time = start_time
                items.append({"id": f"{style}-{index}", "startTime": start_time, "title": title, "note": str(item.get("note", ""))[:300], "costCents": max(0, int(float(item.get("cost_cny", 0)) * 100)), "indoor": bool(item.get("indoor", False)), "needsReservation": bool(item.get("needs_reservation", False))})
            expected = max(0, int(float(row.get("expected_cost_cny", 0)) * 100))
            if budget_cents and expected > budget_cents * 2:
                raise ValueError("模型方案严重超出预算")
            plans.append({"style": style, "name": str(row.get("name", style))[:50], "summary": str(row.get("summary", ""))[:300], "expected_cost_cents": expected, "intensity": str(row.get("intensity", "适中"))[:20], "weather_fit": str(row.get("weather_fit", "请出发前确认天气"))[:200], "items": items})
        if {plan["style"] for plan in plans} != set(expected_styles):
            raise ValueError("模型约会方案类型不完整")
        return sorted(plans, key=lambda plan: expected_styles.index(plan["style"]))


class LocalTravelPlanner(TravelPlannerProvider):
    """Safe offline planner used until a model provider is configured."""

    def generate_variants(
        self,
        destination: str,
        budget_cents: int,
        context: dict | None = None,
    ) -> list[GeneratedPlan]:
        context = context or {}
        try:
            start_date = date.fromisoformat(str(context.get("start_date", "")))
            end_date = date.fromisoformat(str(context.get("end_date", "")))
            day_count = max(1, min((end_date - start_date).days + 1, 61))
        except ValueError:
            day_count = 3
        base = [
            ("slow", "松弛浪漫", "本地安全模板：保留充足相处时间，具体地点需由用户确认", 0.965, "松弛", 72),
            ("classic", "经典充实", "本地安全模板：按经典路线节奏编排，具体地点需由用户确认", 1.03, "适中", 105),
            ("value", "性价比", "本地安全模板：公共交通和免费体验优先，具体地点需由用户确认", 0.79, "轻快", 92),
            ("food", "美食寻味", "本地安全模板：每天安排早中晚三餐，餐馆由附近地点查询补全", 0.91, "适中", 80),
            ("nature", "自然治愈", "本地安全模板：自然体验优先，具体地点需由用户确认", 0.86, "松弛", 98),
        ]
        templates: dict[str, list[tuple[str, str]]] = {
            "slow": [("10:00", "已确认区域周边慢游"), ("15:00", "双人休息与自由探索")],
            "classic": [("09:00", "当地代表地点（待选择）"), ("14:00", "邻近文化体验（待选择）")],
            "value": [("09:30", "免费或低价体验（待选择）"), ("14:30", "公共交通可达街区（待选择）")],
            "food": [],
            "nature": [("09:00", "附近自然体验（待选择）"), ("16:30", "日落或散步地点（待选择）")],
        }
        plans = []
        for style, name, summary, ratio, intensity, transport in base:
            items = []
            for day_number in range(1, day_count + 1):
                for time, title in templates[style]:
                    items.append({
                        "day_number": day_number,
                        "start_time": time,
                        "title": f"{destination} · {title}",
                        "type": "place",
                        "duration_minutes": 90,
                        "cost_cents": 0,
                        "note": "此项是可编辑占位，不代表真实店名或景点；请搜索真实地点后替换。",
                        "details": {},
                    })
            plans.append(GeneratedPlan(style, name, summary, int(budget_cents * ratio), intensity, transport, items))
        return plans

    def analyze_impact(self, item: dict, changes: dict) -> dict:
        allowed = ("title", "start_time", "duration_minutes", "note", "position")
        diffs = [{"field": key, "before": item.get(key), "after": changes[key]} for key in allowed if key in changes and changes[key] != item.get(key)]
        warnings = []
        if "start_time" in changes and changes["start_time"] != item.get("start_time"):
            try:
                old_hour, old_minute = (int(value) for value in str(item.get("start_time", "00:00")).split(":"))
                new_hour, new_minute = (int(value) for value in str(changes["start_time"]).split(":"))
                delta = new_hour * 60 + new_minute - old_hour * 60 - old_minute
                direction = "延后" if delta > 0 else "提前"
                warnings.append(f"当前项目将{direction} {abs(delta)} 分钟，请确认与前后项目的衔接")
            except (TypeError, ValueError):
                warnings.append("开始时间发生变化，请确认与前后项目的衔接")
        if any(key in changes for key in ("title", "note")):
            warnings.append("地点或备注已改变，真实营业时间与预约条件需在出发前确认")
        if not warnings:
            warnings.append("只更新当前项目，不覆盖其他日期和已锁定的人工内容")
        return {
            "scope": "current_day",
            "protected_manual_changes": True,
            "changes": diffs,
            "warnings": warnings,
        }


class OpenAICompatibleTravelPlanner(TravelPlannerProvider):
    """Structured planner for Groq or another OpenAI-compatible endpoint.

    Any remote failure falls back to the deterministic local planner so manual
    trip planning remains available when a free quota is exhausted.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: int = 30,
        before_request: Callable[[str], None] | None = None,
        max_tokens: int = 8192,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = max(5, min(timeout, 120))
        self.fallback = LocalTravelPlanner()
        self.before_request = before_request
        self.max_tokens = max(1024, min(max_tokens, 32768))

    def _fallback_plans(
        self,
        destination: str,
        budget_cents: int,
        context: dict,
        message: str,
    ) -> list[GeneratedPlan]:
        return [
            replace(plan, generation_source="rules", generation_message=message[:300])
            for plan in self.fallback.generate_variants(destination, budget_cents, context)
        ]

    def _generate_style_variants(
        self,
        destination: str,
        budget_cents: int,
        context: dict,
        day_count: int,
        item_schema: dict,
    ) -> list[GeneratedPlan]:
        """Recover from a truncated five-plan response using small requests.

        Reasoning models can spend most of a large response budget before the
        final JSON. One style per request keeps each JSON document short and
        makes the recovery deterministic instead of saving placeholders.
        """
        styles = ["slow", "classic", "value", "food", "nature"]
        plans: list[GeneratedPlan] = []
        for style in styles:
            compact_prompt = {
                "task": "只生成一套情侣多日旅行方案。每天主题、区域和活动必须不同；不得跨天重复同一地点。",
                "destination": destination,
                "budget_cny": round(budget_cents / 100, 2),
                "start_date": context.get("start_date"),
                "end_date": context.get("end_date"),
                "day_count": day_count,
                "preferences": context.get("preferences", {}),
                "partner_preferences": context.get("partner_preferences", {}),
                "confirmed_destination_place": context.get("destination_place", {}),
                "required_style": style,
                "logistics": {
                    "departure_city": context.get("departure_city", ""),
                    "arrival_city": context.get("arrival_city", "") or destination,
                    "outbound_time": context.get("outbound_time"),
                    "return_time": context.get("return_time"),
                    "travelers": context.get("travelers", 2),
                    "rooms": context.get("rooms", 1),
                    "hotel_budget_cny": round(int(context.get("hotel_budget_cents", 0) or 0) / 100, 2),
                },
                "requirements": (
                    "非 food 方案首末日2至3个项目，完整游览日3至8个真实且具体的POI或主题乐园分区/项目；同一标题只能出现一次。"
                    "food 方案每天早中晚三餐。首日和末日按交通时间减量；相邻项目应在同一区域，避免折返。"
                    "每一天必须有独立且不重复的day_themes主题。不得使用‘待选择、附近景点、自由探索’等占位名称。"
                    "每个项目必须提供details：2至3项具体可做活动、2至3项好玩亮点、针对这趟情侣旅行的推荐原因、"
                    "1至3项实用提醒、最佳体验时段、是否需要预约的文字说明，以及用于查附近餐厅的具体area_hint；不得复述标题或写空泛套话。"
                ),
                "output": {"plans": [{**item_schema, "style": style}]},
            }
            style_payload = {
                "model": self.model,
                "temperature": 0.1,
                "max_tokens": max(4096, min(self.max_tokens, 8192)),
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": self._system_prompt(single_style=True)},
                    {"role": "user", "content": json.dumps(compact_prompt, ensure_ascii=False)},
                ],
            }
            if "api.deepseek.com" in self.base_url.lower():
                style_payload["thinking"] = {"type": "disabled"}
            for style_attempt in range(2):
                attempt_payload = style_payload
                if style_attempt:
                    attempt_payload = {
                        **style_payload,
                        "messages": [
                            {
                                "role": "system",
                                "content": style_payload["messages"][0]["content"]
                                + " 上一次该风格响应不是合法 JSON 或未通过结构校验。请只重写这一套方案，优先保证 JSON 正确闭合。",
                            },
                            style_payload["messages"][1],
                        ],
                    }
                if self.before_request:
                    suffix = " retry" if style_attempt else ""
                    self.before_request(f"travel plan {style} recovery{suffix}")
                req = request.Request(
                    f"{self.base_url}/chat/completions",
                    data=json.dumps(attempt_payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    method="POST",
                )
                with request.urlopen(req, timeout=self.timeout, context=TRUSTED_SSL_CONTEXT) as response:
                    result = json.loads(response.read().decode("utf-8"))
                try:
                    content = result["choices"][0]["message"].get("content")
                    if not isinstance(content, str) or not content.strip():
                        raise ValueError(f"{style} 方案没有返回内容")
                    one_plan = self._validate(
                        json.loads(content), day_count, expected_styles=[style],
                        destination_place=context.get("destination_place") or None,
                    )
                    present_days = {int(item["day_number"]) for item in one_plan[0].items}
                    if present_days != set(range(1, day_count + 1)):
                        raise ValueError(f"{style} 方案没有覆盖全部日期")
                except (KeyError, ValueError, TypeError, OverflowError, UnicodeError, json.JSONDecodeError):
                    if style_attempt == 0:
                        continue
                    raise
                plans.extend(one_plan)
                break
        return [
            replace(
                plan,
                generation_source="ai",
                generation_message=f"由 {self.model} 分套生成并通过结构校验",
            )
            for plan in plans
        ]

    def generate_variants(
        self,
        destination: str,
        budget_cents: int,
        context: dict | None = None,
    ) -> list[GeneratedPlan]:
        context = context or {}
        try:
            start_date = date.fromisoformat(str(context.get("start_date", "")))
            end_date = date.fromisoformat(str(context.get("end_date", "")))
            day_count = max(1, min((end_date - start_date).days + 1, 61))
        except ValueError:
            day_count = 3
        prompt = {
            "task": "为情侣生成五套差异明显、可以直接照着执行并可跨方案混搭的完整多日旅行计划。",
            "destination": destination,
            "budget_cny": round(budget_cents / 100, 2),
            "start_date": context.get("start_date"),
            "end_date": context.get("end_date"),
            "day_count": day_count,
            "preferences": context.get("preferences", {}),
            "partner_preferences": context.get("partner_preferences", {}),
            "confirmed_destination_place": context.get("destination_place", {}),
            "logistics": {
                "departure_city": context.get("departure_city", ""),
                "arrival_city": context.get("arrival_city", "") or destination,
                "outbound_time": context.get("outbound_time"),
                "return_time": context.get("return_time"),
                "travelers": context.get("travelers", 2),
                "rooms": context.get("rooms", 1),
                "hotel_budget_cny": round(int(context.get("hotel_budget_cents", 0) or 0) / 100, 2),
            },
            "required_styles": ["slow", "classic", "value", "food", "nature"],
            "planning_rules": [
                "先判断目的地属于哪个城市或景区；小景区按景区本体和合理周边编排，不能混入远处同名地点。",
                "如果提供confirmed_destination_place，它是用户亲自选定的唯一地点范围；所有项目的location_scope必须属于其国家、城市、区县或景区，不得混入任何异地同名地点。",
                "每套方案每天必须有不同的主题、游览区域和活动组合；同一POI标题不得跨天重复。",
                "主题乐园不能每天只写乐园总名，应细化为真实园区、分区、演出或项目；乐园总名最多出现一次。",
                "首日结合抵达时间，末日结合返程时间；时间未知时首末日也要比中间日更松弛。",
                "同一天的项目按地理邻近和时间顺序排列，优先步行或短程交通，避免跨区往返。",
                "非food方案首末日安排2至3个项目，完整游览日安排3至8个具体项目；food方案每天安排早餐、午餐、晚餐并写清用餐区域。",
                "只写确信真实存在的地点或项目；不得使用待选择、待确认、附近景点、当地代表地点、自由探索等占位名称。",
                "五套方案必须体现主题差异，不能只是交换同一批地点的顺序。",
                "每个项目都要写可点开的详情：2至3个具体活动、2至3个好玩亮点、结合情侣偏好和当天路线的推荐原因、1至3个实用提醒、最佳体验时段、预约说明和具体区域area_hint。不得只复述标题。",
            ],
            "output": {
                "plans": [{
                    "style": "slow|classic|value|food|nature",
                    "name": "中文名称",
                    "summary": "简短摘要",
                    "budget_cny": 1000,
                    "intensity": "松弛|适中|轻快",
                    "transport_minutes": 60,
                    "day_themes": [{"day_number": 1, "theme": "当天独有的短主题"}],
                    "items": [{
                        "day_number": 1,
                        "start_time": "09:00",
                        "title": "地点或活动",
                        "type": "place",
                        "duration_minutes": 90,
                        "cost_cny": 0,
                        "note": "执行提示",
                        "details": {
                            "activities": ["可在这里实际做的活动1", "活动2"],
                            "highlights": ["独特或好玩的亮点1", "亮点2"],
                            "recommendation_reason": "为什么适合这趟情侣旅行和当天路线",
                            "practical_tips": ["排队、交通、拍照或安全提醒"],
                            "best_time": "最佳体验时段",
                            "reservation_note": "是否需要预约以及如何确认",
                            "area_hint": "用于路线和附近餐馆检索的具体景区、街区或商圈",
                            "location_scope": "该项目所属的国家·城市·区县或已确认景区"
                        }
                    }]
                }]
            },
        }
        item_schema = prompt["output"]["plans"][0]
        payload = {
            "model": self.model,
            "temperature": 0.2,
            # Reasoning-capable compatible models count their hidden reasoning
            # against this limit. 4096 can therefore end before final JSON.
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
        }
        if "api.deepseek.com" in self.base_url.lower():
            # DeepSeek enables high-effort thinking by default. Structured
            # itinerary JSON benefits from deterministic non-thinking mode and
            # otherwise may spend the whole output budget on reasoning.
            payload["thinking"] = {"type": "disabled"}
        try:
            for attempt in range(2):
                if self.before_request:
                    self.before_request("travel plan" if attempt == 0 else "travel plan retry")
                attempt_payload = payload
                if attempt:
                    attempt_payload = {
                        **payload,
                        "messages": [
                            {
                                "role": "system",
                                "content": payload["messages"][0]["content"] + " 上一次响应不完整。本次优先保证 JSON 正确闭合，不要增加解释或多余字段。",
                            },
                            payload["messages"][1],
                        ],
                    }
                req = request.Request(
                    f"{self.base_url}/chat/completions",
                    data=json.dumps(attempt_payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    method="POST",
                )
                with request.urlopen(req, timeout=self.timeout, context=TRUSTED_SSL_CONTEXT) as response:
                    raw_result = response.read().decode("utf-8")
                try:
                    result = json.loads(raw_result)
                    choice = result["choices"][0]
                    content = choice["message"].get("content")
                    if not isinstance(content, str) or not content.strip():
                        finish_reason = str(choice.get("finish_reason", ""))
                        if finish_reason == "length":
                            raise ValueError("模型输出额度被推理内容耗尽，未返回最终行程")
                        raise ValueError("模型没有返回最终行程内容")
                    plans = self._validate(
                        json.loads(content), day_count,
                        destination_place=context.get("destination_place") or None,
                    )
                except (KeyError, ValueError, TypeError, OverflowError, UnicodeError, json.JSONDecodeError):
                    if attempt == 0:
                        return self._generate_style_variants(
                            destination, budget_cents, context, day_count, item_schema,
                        )
                    raise
                return [
                    replace(
                        plan,
                        generation_source="ai",
                        generation_message=f"由 {self.model} 生成并通过结构校验" + ("（第二次尝试）" if attempt else ""),
                    )
                    for plan in plans
                ]
            raise ValueError("模型连续两次未返回完整行程")
        except QuotaExceededError as exc:
            return self._fallback_plans(destination, budget_cents, context, str(exc))
        except error.HTTPError as exc:
            labels = {
                400: "模型请求参数未被服务接受",
                401: "大模型密钥无效或已失效",
                402: "大模型账户余额或额度不足",
                403: "大模型账户没有调用权限",
                429: "大模型服务限流或额度已用完",
            }
            reason = labels.get(exc.code, f"大模型服务返回 HTTP {exc.code}")
            return self._fallback_plans(destination, budget_cents, context, f"{reason}，本次已使用本地安全模板")
        except (error.URLError, TimeoutError):
            return self._fallback_plans(destination, budget_cents, context, "大模型网络连接或响应超时，本次已使用本地安全模板")
        except (KeyError, ValueError, TypeError, OverflowError, UnicodeError, json.JSONDecodeError) as exc:
            reason = str(exc).strip() if isinstance(exc, ValueError) else "模型返回结构无效"
            return self._fallback_plans(destination, budget_cents, context, f"{reason[:180]}，本次已使用本地安全模板")

    @staticmethod
    def _system_prompt(single_style: bool = False) -> str:
        scope = "一套指定风格" if single_style else "五套不同风格"
        return (
            "你是资深情侣旅行规划总监，熟悉中国与国际目的地、主题乐园、小众景区、公共交通、"
            "营业时间、预约节奏和情侣体力分配。你的工作不是罗列景点，而是生成"
            f"{scope}按天可执行的旅行方案。每天必须有独立主题和不同地点；同一地点不能跨天重复。"
            "主题乐园应细化为真实分区、演出或项目，不能每天重复乐园总名。"
            "首日考虑抵达，末日考虑返程，同日地点按地理邻近排序，避免折返。"
            "用户确认的结构化目的地是唯一地理边界；遇到同名地点必须服从其中的国家、城市、区县、地址和坐标，"
            "每个项目都要在location_scope中写明所属城市或景区，不得自行切换到其他城市的同名地点。"
            "只能使用确信真实存在的地点，不得编造，也不得用待选择、附近景点、自由探索等占位词。"
            "严格遵守预算、节奏、必去点、饮食和住宿偏好。只返回正确闭合的紧凑JSON，"
            "不包含Markdown或解释；每个项目包含day_number且按时间递增，day_themes逐日唯一。"
            "每个项目的details必须具体说明可做活动、好玩亮点、推荐原因、实用提醒、最佳时段和预约要求；"
            "推荐原因必须与该地点、情侣偏好或当天路线有关，area_hint要写可用于检索附近餐馆的真实景区、街区或商圈，不能使用‘值得一去’等空话。"
        )

    def _validate(
        self,
        value: dict,
        day_count: int = 3,
        expected_styles: list[str] | None = None,
        destination_place: dict | None = None,
    ) -> list[GeneratedPlan]:
        expected_styles = expected_styles or ["slow", "classic", "value", "food", "nature"]
        rows = value.get("plans")
        if len(expected_styles) == 1:
            # Some OpenAI-compatible models ignore the single-style recovery
            # instruction and return the complete five-plan envelope again;
            # others unwrap the requested plan. Keep the requested row only,
            # then apply the same strict item, scope, timing and detail checks
            # below. This recovers valid model work without accepting an
            # unrelated style or weakening the travel contract.
            expected_style = expected_styles[0]
            if isinstance(rows, list):
                matching = [
                    row for row in rows
                    if isinstance(row, dict) and str(row.get("style", "")) == expected_style
                ]
                if matching:
                    rows = [matching[0]]
            elif isinstance(value.get("plan"), dict):
                rows = [value["plan"]]
            elif str(value.get("style", "")) == expected_style:
                rows = [value]
        if not isinstance(rows, list) or len(rows) != len(expected_styles):
            raise ValueError(f"模型必须返回 {len(expected_styles)} 套指定方案")
        plans: list[GeneratedPlan] = []
        for row in rows:
            style = str(row.get("style", ""))
            raw_items = row.get("items")
            if style not in set(expected_styles) or not isinstance(raw_items, list) or len(raw_items) < 1:
                raise ValueError("模型方案结构无效")
            items = []
            for item_index, item in enumerate(raw_items[: day_count * 6]):
                # Accept older compatible providers that omit day_number and
                # distribute their ordered items safely across the real dates.
                day_number = int(item.get("day_number", (item_index % day_count) + 1))
                start_time = str(item.get("start_time", "09:00"))[:5]
                title = str(item.get("title", "")).strip()[:100]
                if not 1 <= day_number <= day_count or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start_time) or not title:
                    raise ValueError("模型行程项目无效")
                raw_details = item.get("details")
                if not isinstance(raw_details, dict):
                    raise ValueError("模型行程项目缺少可查看详情")

                def detail_list(name: str, maximum: int, length: int) -> list[str]:
                    value = raw_details.get(name)
                    if not isinstance(value, list):
                        return []
                    return [text for raw in value[:maximum] if (text := str(raw).strip()[:length])]

                activities = detail_list("activities", 3, 80)
                highlights = detail_list("highlights", 3, 80)
                reason = str(raw_details.get("recommendation_reason", "")).strip()[:240]
                if len(activities) < 2 or len(highlights) < 2 or not reason:
                    raise ValueError("模型行程详情不完整")
                location_scope = str(raw_details.get("location_scope", "")).strip()[:160]
                if destination_place:
                    terms = []
                    for field in ("name", "city", "district", "province", "country"):
                        term = re.sub(
                            r"(?:特别行政区|自治区|自治州|地区|省|市|区|县)$", "",
                            str(destination_place.get(field, "")).strip(),
                        )
                        if len(term) >= 2:
                            terms.append(term.casefold())
                    scope_text = f"{location_scope} {title} {item.get('note', '')}".casefold()
                    if not location_scope or (terms and not any(term in scope_text for term in terms)):
                        raise ValueError("模型行程出现未归属于已确认目的地的项目")
                items.append({
                    "day_number": day_number,
                    "start_time": start_time,
                    "title": title,
                    "type": str(item.get("type", "place"))[:30],
                    "duration_minutes": max(15, min(int(item.get("duration_minutes", 90)), 720)),
                    "cost_cents": max(0, int(float(item.get("cost_cny", 0)) * 100)),
                    "note": str(item.get("note", ""))[:300],
                    "details": {
                        "activities": activities,
                        "highlights": highlights,
                        "recommendation_reason": reason,
                        "practical_tips": detail_list("practical_tips", 3, 100),
                        "best_time": str(raw_details.get("best_time", "")).strip()[:80],
                        "reservation_note": str(raw_details.get("reservation_note", "")).strip()[:160],
                        "area_hint": str(raw_details.get("area_hint", "")).strip()[:100],
                        "location_scope": location_scope,
                    },
                })
            buckets: dict[int, list[dict]] = {number: [] for number in range(1, day_count + 1)}
            for item in items:
                buckets[item["day_number"]].append(item)
            if any(not bucket for bucket in buckets.values()):
                raise ValueError(f"{style} 方案没有覆盖每一天")
            minimum = 1 if style == "food" else 2
            if any(len(bucket) < minimum for bucket in buckets.values()):
                raise ValueError(f"{style} 方案每天至少需要 {minimum} 个有效项目")
            for bucket in buckets.values():
                if [item["start_time"] for item in bucket] != sorted(item["start_time"] for item in bucket):
                    raise ValueError(f"{style} 方案同一天的时间顺序错误")
            if style != "food":
                forbidden = ("待选择", "待确认", "当地代表", "附近景点", "自由探索", "邻近文化体验")
                normalized_titles: list[str] = []
                for item in items:
                    if any(token in item["title"] for token in forbidden):
                        raise ValueError(f"{style} 方案仍包含占位地点")
                    normalized = re.sub(r"[\W_]+", "", item["title"].casefold(), flags=re.UNICODE)
                    normalized_titles.append(normalized)
                if len(set(normalized_titles)) != len(normalized_titles):
                    raise ValueError(f"{style} 方案跨天重复了同一地点")
            day_themes: dict[int, str] = {}
            raw_themes = row.get("day_themes")
            if isinstance(raw_themes, list):
                for theme_row in raw_themes:
                    if not isinstance(theme_row, dict):
                        continue
                    try:
                        number = int(theme_row.get("day_number"))
                    except (TypeError, ValueError):
                        continue
                    theme = str(theme_row.get("theme", "")).strip()[:40]
                    if 1 <= number <= day_count and theme:
                        day_themes[number] = theme
            for number, bucket in buckets.items():
                if number not in day_themes:
                    day_themes[number] = " · ".join(item["title"] for item in bucket[:2])[:40]
            normalized_themes = [re.sub(r"[\W_]+", "", day_themes[number].casefold(), flags=re.UNICODE) for number in range(1, day_count + 1)]
            if len(set(normalized_themes)) != len(normalized_themes):
                raise ValueError(f"{style} 方案每天的主题不够明确或出现重复")
            plans.append(GeneratedPlan(
                style=style,
                name=str(row.get("name", style))[:50],
                summary=str(row.get("summary", ""))[:300],
                budget_cents=max(0, int(float(row.get("budget_cny", 0)) * 100)),
                intensity=str(row.get("intensity", "适中"))[:20],
                transport_minutes=max(0, min(int(row.get("transport_minutes", 0)), 1440)),
                items=items,
                day_themes=day_themes,
            ))
        if {plan.style for plan in plans} != set(expected_styles):
            raise ValueError("模型方案类型不完整")
        return sorted(plans, key=lambda plan: expected_styles.index(plan.style))

    def analyze_impact(self, item: dict, changes: dict) -> dict:
        return self.fallback.analyze_impact(item, changes)


def travel_planner_from_env() -> TravelPlannerProvider:
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        return LocalTravelPlanner()
    base_url = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
    model = os.getenv("LLM_MODEL", "openai/gpt-oss-20b")
    try:
        timeout = int(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
    except ValueError:
        timeout = 30
    try:
        max_tokens = int(os.getenv("LLM_MAX_TOKENS", "8192"))
    except ValueError:
        max_tokens = 8192
    return OpenAICompatibleTravelPlanner(api_key, base_url, model, timeout, max_tokens=max_tokens)


def date_planner_from_env() -> DatePlannerProvider:
    api_key = os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        return LocalDatePlanner()
    base_url = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
    model = os.getenv("LLM_MODEL", "openai/gpt-oss-20b")
    try:
        timeout = int(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
    except ValueError:
        timeout = 30
    return OpenAICompatibleDatePlanner(api_key, base_url, model, timeout)


class MapProvider:
    """Production seam for AMap domestically and an overseas provider later."""

    def route(self, origin: str, destination: str, mode: str) -> dict:
        raise NotImplementedError


class BookingProvider:
    """Production seam for authorized Ctrip/Fliggy affiliate APIs."""

    def search_quotes(self, target: dict) -> list[dict]:
        raise NotImplementedError
