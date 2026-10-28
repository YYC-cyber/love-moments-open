"""Controlled Intelligent UI protocol and composers.

The model may choose component ordering only. Data, resource references and actions
are hydrated by the server after validation, so model output is never executable.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib import request

from .http_client import TRUSTED_SSL_CONTEXT


SCHEMA_VERSION = "1.0"
SURFACES = {"home", "chat", "trip", "timeline", "life"}
COMPONENTS_BY_SURFACE = {
    "home": ("couple_overview", "anniversary", "trip_countdown", "pending_items", "daily_question", "quick_capture"),
    "chat": ("message_summary", "trip_share", "favorite_memory", "suggested_reply", "import_guide"),
    "trip": ("trip_variant_compare", "itinerary_timeline", "budget_summary", "hotel_quote", "risk_alert", "impact_diff", "approval_state"),
    "timeline": ("featured_memory", "timeline_summary", "month_album", "footprint_summary", "year_report", "privacy_status"),
    "life": ("meal_calendar", "meal_wishlist", "shopping_list", "recipe_library", "wishlist", "calendar_summary", "anniversary", "todo_summary", "preference_summary", "pending_items"),
}
ALL_COMPONENTS = frozenset(item for values in COMPONENTS_BY_SURFACE.values() for item in values)
ACTION_TYPES = frozenset({"navigate", "send_message", "select_trip_variant", "create_moment", "create_calendar_entry", "create_wishlist_item"})


class UiComposer(Protocol):
    name: str

    def compose(self, surface: str, context: dict[str, Any], intent: str) -> list[str]: ...


@dataclass
class RuleBasedUiComposer:
    name: str = "rules"

    def compose(self, surface: str, context: dict[str, Any], intent: str) -> list[str]:
        components = list(COMPONENTS_BY_SURFACE[surface])
        text = intent.lower()
        priorities = {
            "预算": "budget_summary", "酒店": "hotel_quote", "确认": "pending_items",
            "行程": "itinerary_timeline", "回忆": "featured_memory", "城市": "footprint_summary",
            "愿望": "wishlist", "消息": "message_summary", "纪念日": "anniversary",
            "菜谱": "recipe_library", "点菜": "meal_wishlist", "吃": "meal_calendar", "采购": "shopping_list",
        }
        for keyword, component in priorities.items():
            if keyword in text and component in components:
                components.remove(component)
                components.insert(0, component)
        return components


class OpenAICompatibleUiComposer:
    name = "ai"

    def __init__(self, api_key: str, base_url: str, model: str, timeout: int = 30, before_request: Callable[[str], None] | None = None):
        self.api_key, self.base_url, self.model, self.timeout = api_key, base_url.rstrip("/"), model, timeout
        self.before_request = before_request

    def compose(self, surface: str, context: dict[str, Any], intent: str) -> list[str]:
        allowed = list(COMPONENTS_BY_SURFACE[surface])
        safe_context = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "max_tokens": 512,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "你是恋屿界面编排器。只返回 JSON：{\"components\":[...]}; 组件必须来自白名单，不得返回 HTML、脚本、资源ID或操作参数。"},
                {"role": "user", "content": f"页面={surface}\n白名单={json.dumps(allowed, ensure_ascii=False)}\n安全上下文={safe_context}\n用户意图={intent[:500]}"},
            ],
        }
        if "api.deepseek.com" in self.base_url.lower():
            payload["thinking"] = {"type": "disabled"}
        if self.before_request:
            self.before_request(f"ui compose {surface}")
        req = request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=self.timeout, context=TRUSTED_SSL_CONTEXT) as response:  # noqa: S310 - configured trusted endpoint
            raw = json.loads(response.read().decode())
        content = raw["choices"][0]["message"]["content"]
        result = json.loads(content)
        components = result.get("components")
        if not isinstance(components, list) or not components:
            raise ValueError("模型未返回组件清单")
        return [str(item) for item in components]


def ui_composer_from_env() -> UiComposer:
    api_key = os.getenv("UI_LLM_API_KEY") or os.getenv("LLM_API_KEY", "")
    if not api_key:
        return RuleBasedUiComposer()
    return OpenAICompatibleUiComposer(
        api_key,
        os.getenv("UI_LLM_BASE_URL") or os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1"),
        os.getenv("UI_LLM_MODEL") or os.getenv("LLM_MODEL", "openai/gpt-oss-20b"),
        int(os.getenv("UI_LLM_TIMEOUT_SECONDS") or os.getenv("LLM_TIMEOUT_SECONDS", "30")),
    )


def validate_component_order(surface: str, components: Any) -> list[str]:
    if surface not in SURFACES:
        raise ValueError("不支持的智能界面")
    if not isinstance(components, list) or not 1 <= len(components) <= 24:
        raise ValueError("组件数量无效")
    allowed = set(COMPONENTS_BY_SURFACE[surface])
    result: list[str] = []
    for component in components:
        if not isinstance(component, str) or component not in allowed:
            raise ValueError("包含未知或越权组件")
        if component not in result:
            result.append(component)
    return result


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    if set(manifest) != {"schemaVersion", "surface", "contextVersion", "sharedManifestId", "sections", "personalOverlay", "generatedBy", "expiresAt"}:
        raise ValueError("清单字段不符合协议")
    if manifest["schemaVersion"] != SCHEMA_VERSION or manifest["surface"] not in SURFACES:
        raise ValueError("清单版本或页面无效")
    nodes = manifest["sections"] + manifest["personalOverlay"]
    if not isinstance(manifest["sections"], list) or not isinstance(manifest["personalOverlay"], list) or len(nodes) > 24:
        raise ValueError("清单节点数量无效")
    ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or set(node) != {"id", "type", "resourceRef", "variant", "props", "actions"}:
            raise ValueError("节点结构无效")
        if node["type"] not in ALL_COMPONENTS or not re.fullmatch(r"[a-z0-9:_-]{1,80}", node["id"]):
            raise ValueError("节点类型或 ID 无效")
        if node["id"] in ids or not isinstance(node["props"], dict) or not isinstance(node["actions"], list):
            raise ValueError("节点内容无效")
        ids.add(node["id"])
        if len(json.dumps(node["props"], ensure_ascii=False)) > 12000:
            raise ValueError("节点属性过大")
        for action in node["actions"]:
            if not isinstance(action, dict) or action.get("type") not in ACTION_TYPES:
                raise ValueError("动作类型无效")
            if action["type"] != "navigate" and action.get("requiresConfirmation") is not True:
                raise ValueError("写操作必须确认")
    return manifest


class ConflictError(Exception):
    """Raised when a shared resource changed after an action preview."""
