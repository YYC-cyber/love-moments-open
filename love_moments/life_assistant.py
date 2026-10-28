"""Controlled intent parsing for the couple life assistant.

The provider contract deliberately describes intent instead of database
operations. Resource lookup, permissions and version checks remain server-side.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Protocol
from urllib import request
from zoneinfo import ZoneInfo

from .http_client import TRUSTED_SSL_CONTEXT


MEAL_SLOTS = {
    "早餐": ("breakfast", "08:00"),
    "早饭": ("breakfast", "08:00"),
    "午餐": ("lunch", "12:00"),
    "午饭": ("lunch", "12:00"),
    "晚餐": ("dinner", "18:30"),
    "晚饭": ("dinner", "18:30"),
    "晚上": ("dinner", "18:30"),
    "夜宵": ("snack", "21:30"),
}
WEEKDAYS = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}


@dataclass(frozen=True)
class LifeIntent:
    requested_date: date
    meal_slot: str
    planned_time: str
    dish_query: str
    task_query: str
    add_calendar: bool
    add_shopping: bool
    add_todo: bool
    source: str = "rules"

    @classmethod
    def from_dict(cls, value: object) -> "LifeIntent":
        if not isinstance(value, dict):
            raise ValueError("模型未返回生活安排对象")
        required = {
            "requested_date",
            "meal_slot",
            "planned_time",
            "dish_query",
            "task_query",
            "add_calendar",
            "add_shopping",
            "add_todo",
        }
        if set(value) != required:
            raise ValueError("模型返回的生活安排字段无效")
        if not isinstance(value["requested_date"], str):
            raise ValueError("模型返回的日期无效")
        try:
            requested_date = date.fromisoformat(value["requested_date"])
        except ValueError as error:
            raise ValueError("模型返回的日期无效") from error
        if not isinstance(value["meal_slot"], str):
            raise ValueError("模型返回的餐次无效")
        meal_slot = value["meal_slot"]
        if meal_slot not in {"breakfast", "lunch", "dinner", "snack"}:
            raise ValueError("模型返回的餐次无效")
        if not isinstance(value["planned_time"], str):
            raise ValueError("模型返回的时间无效")
        planned_time = value["planned_time"]
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", planned_time):
            raise ValueError("模型返回的时间无效")
        if not isinstance(value["dish_query"], str) or not isinstance(
            value["task_query"], str
        ):
            raise ValueError("模型返回的安排内容无效")
        dish_query = value["dish_query"].strip()
        task_query = value["task_query"].strip()
        if len(dish_query) > 100 or len(task_query) > 100:
            raise ValueError("模型返回的安排内容过长")
        flags = {
            name: value[name]
            for name in ("add_calendar", "add_shopping", "add_todo")
        }
        if any(type(flag) is not bool for flag in flags.values()):
            raise ValueError("模型返回的动作开关无效")
        if not dish_query and not task_query:
            raise ValueError("模型没有识别到菜品或待办内容")
        return cls(
            requested_date=requested_date,
            meal_slot=meal_slot,
            planned_time=planned_time,
            dish_query=dish_query,
            task_query=task_query,
            add_calendar=flags["add_calendar"],
            add_shopping=flags["add_shopping"],
            add_todo=flags["add_todo"],
            source="ai",
        )


class LifeAssistantProvider(Protocol):
    name: str

    def parse(self, intent: str, *, timezone_name: str) -> LifeIntent:
        ...


def resolve_relative_date(
    text: str, *, timezone_name: str = "Asia/Shanghai", base: date | None = None
) -> date:
    today = base or datetime.now(ZoneInfo(timezone_name)).date()
    if "后天" in text:
        return today + timedelta(days=2)
    if "明天" in text:
        return today + timedelta(days=1)
    if "今天" in text or "今晚" in text:
        return today
    explicit = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", text)
    if explicit:
        return date(*(int(explicit.group(index)) for index in range(1, 4)))
    short = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})日?", text)
    if short:
        candidate = date(today.year, int(short.group(1)), int(short.group(2)))
        if candidate < today:
            candidate = candidate.replace(year=today.year + 1)
        return candidate
    weekday = re.search(r"(下周|本周|这周)?(?:周|星期)([一二三四五六日天])", text)
    if weekday:
        target = WEEKDAYS[weekday.group(2)]
        distance = (target - today.weekday()) % 7
        if distance == 0:
            distance = 7
        if weekday.group(1) == "下周" and distance <= 7 - today.weekday() - 1:
            distance += 7
        return today + timedelta(days=distance)
    return today


class RuleBasedLifeAssistant:
    name = "rules"

    def parse(self, intent: str, *, timezone_name: str) -> LifeIntent:
        text = str(intent or "").strip()
        if not text:
            raise ValueError("请告诉恋屿你们想安排什么")
        if len(text) > 1000:
            raise ValueError("一句话安排不能超过 1000 个字符")
        meal_slot, planned_time = "dinner", "18:30"
        for keyword, value in MEAL_SLOTS.items():
            if keyword in text:
                meal_slot, planned_time = value
                break
        time_match = re.search(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)", text)
        if time_match:
            planned_time = f"{int(time_match.group(1)):02d}:{time_match.group(2)}"
        dish_match = re.search(r"想吃\s*([^，,。；;]+)", text)
        dish_query = dish_match.group(1).strip() if dish_match else ""
        dish_query = re.sub(
            r"(?:到|在)?(?:日历|采购清单|购物清单|待办)$", "", dish_query
        ).strip()
        task_match = re.search(
            r"(?:待办\s*[:：]?|提醒(?:我们|我)?\s*)([^，,。；;]+)", text
        )
        task_query = task_match.group(1).strip() if task_match else ""
        if not task_query and not dish_query:
            activity_match = re.search(r"(?:安排|计划)\s*([^，,。；;]+)", text)
            task_query = activity_match.group(1).strip() if activity_match else ""
        task_query = re.sub(r"^(?:一下|要|请|把)\s*", "", task_query).strip()
        task_query = re.sub(
            r"^(?:今天|今晚|明天|后天|本周|这周|下周|周[一二三四五六日天]|星期[一二三四五六日天])+\s*",
            "",
            task_query,
        ).strip()
        task_query = re.sub(
            r"(?:加入|添加到?|放进)(?:共同)?(?:日历|待办)$", "", task_query
        ).strip()
        reminder_without_meal = "提醒" in text and not dish_query
        return LifeIntent(
            requested_date=resolve_relative_date(text, timezone_name=timezone_name),
            meal_slot=meal_slot,
            planned_time=planned_time,
            dish_query=dish_query[:100],
            task_query=task_query[:100],
            add_calendar=(
                "日历" in text
                or ("安排" in text and not reminder_without_meal)
            ),
            add_shopping=any(value in text for value in ("采购", "购物清单", "买菜")),
            add_todo=("待办" in text or reminder_without_meal),
        )


class OpenAICompatibleLifeAssistant:
    """Extract a constrained intent; all database facts remain server-owned."""

    name = "ai"

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: int = 30,
        before_request: Callable[[str], None] | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.before_request = before_request
        self.fallback = RuleBasedLifeAssistant()

    def parse(self, intent: str, *, timezone_name: str) -> LifeIntent:
        text = str(intent or "").strip()
        if not text:
            raise ValueError("请告诉恋屿你们想安排什么")
        if len(text) > 1000:
            raise ValueError("一句话安排不能超过 1000 个字符")
        local_now = datetime.now(ZoneInfo(timezone_name))
        payload = {
            "model": self.model,
            "temperature": 0.1,
            "max_tokens": 400,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是恋屿双人生活助理的意图解析器，只提取用户明确表达的安排。"
                        "只返回一个 JSON 对象，字段必须且只能是 requested_date(YYYY-MM-DD)、"
                        "meal_slot(breakfast/lunch/dinner/snack)、planned_time(HH:MM)、"
                        "dish_query、task_query、add_calendar、add_shopping、add_todo。"
                        "不得返回数据库ID、食材、价格、评论、私密内容或额外字段。"
                        "用户没说菜品时 dish_query 为空；没说待办时 task_query 为空。"
                        "‘加入日历/安排’才开启 add_calendar，‘采购/买菜’才开启 add_shopping，"
                        "‘待办/提醒’才开启 add_todo。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "timezone": timezone_name,
                            "local_datetime": local_now.isoformat(timespec="minutes"),
                            "intent": text,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        if "api.deepseek.com" in self.base_url.lower():
            payload["thinking"] = {"type": "disabled"}
        try:
            if self.before_request:
                self.before_request("life assistant intent")
            req = request.Request(
                f"{self.base_url}/chat/completions",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with request.urlopen(
                req, timeout=self.timeout, context=TRUSTED_SSL_CONTEXT
            ) as response:  # noqa: S310 - configured OpenAI-compatible endpoint
                raw = json.loads(response.read().decode("utf-8"))
            content = raw["choices"][0]["message"]["content"]
            return LifeIntent.from_dict(json.loads(content))
        except Exception:
            return self.fallback.parse(text, timezone_name=timezone_name)


def life_assistant_from_env(
    before_request: Callable[[str], None] | None = None,
) -> LifeAssistantProvider:
    key = (
        os.getenv("LIFE_ASSISTANT_LLM_API_KEY")
        or os.getenv("LLM_API_KEY")
        or ""
    ).strip()
    if not key:
        return RuleBasedLifeAssistant()
    try:
        timeout = int(
            os.getenv("LIFE_ASSISTANT_LLM_TIMEOUT_SECONDS")
            or os.getenv("LLM_TIMEOUT_SECONDS", "30")
        )
    except ValueError:
        timeout = 30
    return OpenAICompatibleLifeAssistant(
        key,
        os.getenv("LIFE_ASSISTANT_LLM_BASE_URL")
        or os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"),
        os.getenv("LIFE_ASSISTANT_LLM_MODEL")
        or os.getenv("LLM_MODEL", "deepseek-chat"),
        timeout,
        before_request,
    )
