"""Editable chat screenshot OCR provider boundary."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import uuid
from datetime import datetime, timezone
from urllib import error, parse, request
from typing import Callable

from .http_client import TRUSTED_SSL_CONTEXT


MAX_OCR_BYTES = 10 * 1024 * 1024
SUPPORTED_OCR_MIME_TYPES = {
    "application/pdf",
    "image/bmp",
    "image/gif",
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/tiff",
    "image/webp",
}


def parse_chat_text(text: str) -> list[dict]:
    """Turn common exported-chat text into editable rows.

    Supported examples: ``12:30 我: 到家啦`` and ``小辰：路上小心``.
    Unlabelled lines alternate speakers so nothing is silently discarded.
    """
    rows: list[dict] = []
    speaker = "me"
    pattern = re.compile(r"^(?:(\d{1,2}:\d{2})\s*)?(?:(我|本人|对方|TA|ta|他|她|[^:：]{1,20})\s*[:：])?\s*(.+)$")
    source_lines: list[str] = []
    for raw in str(text or "").splitlines():
        line = re.sub(r"(?<=\d)[：﹕](?=\d)", ":", raw.strip())
        if not line:
            continue
        # OCR engines commonly flatten a screenshot into one line. Split at
        # every later HH:MM marker so each chat bubble remains independently
        # editable even when the provider did not preserve line breaks.
        starts = [match.start() for match in re.finditer(r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d\s", line)]
        starts = [start for start in starts if start > 0]
        cursor = 0
        for start in starts:
            source_lines.append(line[cursor:start].strip())
            cursor = start
        source_lines.append(line[cursor:].strip())
    for line in source_lines:
        if not line:
            continue
        match = pattern.match(line)
        if not match:
            continue
        time_text, name, content = match.groups()
        if name:
            speaker = "me" if name.lower() in {"我", "本人", "me"} else "partner"
        content = content.strip()[:4000]
        if content:
            rows.append({"speaker": speaker, "content": content, "time": time_text, "confidence": 0.92 if name else 0.65})
        if not name:
            speaker = "partner" if speaker == "me" else "me"
        if len(rows) >= 200:
            break
    return rows


class OcrProvider:
    name = "unknown"
    is_mock = False

    def extract(self, image: bytes, mime_type: str, hint_text: str = "") -> tuple[str, list[dict]]:
        raise NotImplementedError


class ManualTextProvider(OcrProvider):
    """Parse text pasted by the user without presenting it as remote OCR."""

    name = "manual"

    def extract(self, image: bytes, mime_type: str, hint_text: str = "") -> tuple[str, list[dict]]:
        del image, mime_type
        raw = hint_text.strip()
        rows = parse_chat_text(raw)
        if not rows:
            raise RuntimeError("粘贴内容中没有可导入的对话")
        return raw, rows


class MockOcrProvider(OcrProvider):
    name = "mock"
    is_mock = True

    def extract(self, image: bytes, mime_type: str, hint_text: str = "") -> tuple[str, list[dict]]:
        del image, mime_type
        raw = hint_text.strip() or "12:30 我：这是演示识别结果，请按截图逐条修改\n12:31 对方：确认无误后再保存到共同空间"
        return raw, parse_chat_text(raw)


class HttpOcrProvider(OcrProvider):
    name = "http"

    def __init__(self, endpoint: str, api_key: str, timeout: int = 30, before_request: Callable[[str], None] | None = None):
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout
        self.before_request = before_request

    def extract(self, image: bytes, mime_type: str, hint_text: str = "") -> tuple[str, list[dict]]:
        payload = json.dumps({"image_base64": base64.b64encode(image).decode(), "mime_type": mime_type, "hint_text": hint_text}, ensure_ascii=False).encode()
        req = request.Request(self.endpoint, data=payload, headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, method="POST")
        try:
            if self.before_request:
                self.before_request("chat screenshot")
            with request.urlopen(req, timeout=self.timeout, context=TRUSTED_SSL_CONTEXT) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (error.URLError, json.JSONDecodeError, UnicodeError) as exc:
            raise RuntimeError("OCR 服务暂时不可用") from exc
        raw = str(result.get("text", "")).strip()
        remote_rows = result.get("items")
        if isinstance(remote_rows, list):
            lines = []
            for item in remote_rows[:200]:
                if isinstance(item, dict) and str(item.get("content", "")).strip():
                    lines.append({"speaker": "me" if item.get("speaker") == "me" else "partner", "content": str(item["content"]).strip()[:4000], "time": str(item.get("time", ""))[:5] or None, "confidence": max(0.0, min(float(item.get("confidence", 0.8)), 1.0))})
            if lines:
                return raw, lines
        rows = parse_chat_text(raw)
        if not rows:
            raise RuntimeError("OCR 没有识别到可导入的对话")
        return raw, rows


def _rpc_percent_encode(value: object) -> str:
    """Aliyun RPC v1 RFC3986 encoding (spaces must be ``%20``, not ``+``)."""
    return parse.quote(str(value), safe="-_.~")


def _value(mapping: object, key: str) -> object | None:
    if not isinstance(mapping, dict):
        return None
    wanted = key.casefold()
    for current, value in mapping.items():
        if str(current).casefold() == wanted:
            return value
    return None


def _aliyun_content(payload: object) -> str:
    """Read ``Data.Content`` while accepting SDK-style lower-case fields."""
    data = _value(payload, "Data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return data.strip()
    content = _value(data, "Content")
    if isinstance(content, str) and content.strip():
        return content.strip()

    # Keep compatibility with older responses that only expose sub-images.
    parts: list[str] = []
    sub_images = _value(data, "SubImages")
    if isinstance(sub_images, list):
        for item in sub_images:
            text = _value(item, "Content") or _value(item, "Text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts)


class AlibabaCloudOcrProvider(OcrProvider):
    """Aliyun OCR RecognizeAllText RPC adapter using only the standard library."""

    name = "aliyun"

    def __init__(
        self,
        access_key_id: str,
        access_key_secret: str,
        endpoint: str = "ocr-api.cn-hangzhou.aliyuncs.com",
        ocr_type: str = "General",
        timeout: int = 30,
        before_request: Callable[[str], None] | None = None,
    ):
        if not access_key_id.strip() or not access_key_secret.strip():
            raise ValueError("阿里云 OCR AccessKey 配置不完整")
        raw_endpoint = endpoint.strip() or "ocr-api.cn-hangzhou.aliyuncs.com"
        parsed = parse.urlsplit(raw_endpoint if "://" in raw_endpoint else f"https://{raw_endpoint}")
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("阿里云 OCR Endpoint 必须是 HTTPS 主机名")
        self.access_key_id = access_key_id.strip()
        self.access_key_secret = access_key_secret.strip()
        self.endpoint = parsed.hostname
        self.ocr_type = ocr_type.strip() or "General"
        self.timeout = max(1, min(int(timeout), 120))
        self.before_request = before_request

    def _signed_url(self, *, nonce: str | None = None, timestamp: str | None = None) -> str:
        parameters = {
            "AccessKeyId": self.access_key_id,
            "Action": "RecognizeAllText",
            "Format": "JSON",
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": nonce or uuid.uuid4().hex,
            "SignatureVersion": "1.0",
            "Timestamp": timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "Type": self.ocr_type,
            "Version": "2021-07-07",
        }
        canonical = "&".join(f"{_rpc_percent_encode(key)}={_rpc_percent_encode(parameters[key])}" for key in sorted(parameters))
        string_to_sign = f"POST&%2F&{_rpc_percent_encode(canonical)}"
        digest = hmac.new(f"{self.access_key_secret}&".encode(), string_to_sign.encode(), hashlib.sha1).digest()
        parameters["Signature"] = base64.b64encode(digest).decode()
        query = "&".join(f"{_rpc_percent_encode(key)}={_rpc_percent_encode(parameters[key])}" for key in sorted(parameters))
        return f"https://{self.endpoint}/?{query}"

    @staticmethod
    def _validate_image(image: bytes, mime_type: str) -> None:
        if not image:
            raise ValueError("请先上传聊天截图")
        if len(image) > MAX_OCR_BYTES:
            raise ValueError("OCR 图片不能超过 10MB")
        normalized = str(mime_type or "").split(";", 1)[0].strip().lower()
        if normalized not in SUPPORTED_OCR_MIME_TYPES:
            raise ValueError("阿里云 OCR 不支持这个图片格式")

    @staticmethod
    def _http_error(exc: error.HTTPError) -> RuntimeError:
        if exc.code in {401, 403}:
            return RuntimeError("阿里云 OCR 鉴权失败，请检查 AccessKey 和 RAM 的 OCR 权限")
        if exc.code == 429:
            return RuntimeError("阿里云 OCR 请求过于频繁或额度不足")
        if 400 <= exc.code < 500:
            return RuntimeError("阿里云 OCR 拒绝了图片，请检查格式、尺寸和服务开通状态")
        return RuntimeError("阿里云 OCR 服务暂时不可用")

    def extract(self, image: bytes, mime_type: str, hint_text: str = "") -> tuple[str, list[dict]]:
        del hint_text
        self._validate_image(image, mime_type)
        req = request.Request(
            self._signed_url(),
            data=image,
            headers={"Accept": "application/json", "Content-Type": "application/octet-stream"},
            method="POST",
        )
        try:
            if self.before_request:
                self.before_request("RecognizeAllText")
            with request.urlopen(req, timeout=self.timeout, context=TRUSTED_SSL_CONTEXT) as response:
                result = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            raise self._http_error(exc) from exc
        except (error.URLError, TimeoutError, json.JSONDecodeError, UnicodeError) as exc:
            raise RuntimeError("阿里云 OCR 服务暂时不可用") from exc
        code = _value(result, "Code")
        if code not in {None, "", 200, "200"}:
            raise RuntimeError("阿里云 OCR 识别失败，请检查图片或服务额度")
        raw = _aliyun_content(result)
        rows = parse_chat_text(raw)
        if not rows:
            raise RuntimeError("阿里云 OCR 没有识别到可导入的对话")
        return raw, rows


def _enabled(value: str, default: bool = False) -> bool:
    if not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def aliyun_ocr_configured() -> bool:
    return (
        _enabled(os.getenv("ALIBABA_CLOUD_OCR_ENABLED", "true"), True)
        and bool(os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "").strip())
        and bool(os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "").strip())
    )


def real_ocr_configured() -> bool:
    return aliyun_ocr_configured() or bool(
        os.getenv("OCR_API_URL", "").strip() and os.getenv("OCR_API_KEY", "").strip()
    )


def ocr_provider_from_env(before_request: Callable[[str], None] | None = None) -> OcrProvider:
    provider = os.getenv("OCR_PROVIDER", "auto").strip().lower() or "auto"
    if provider not in {"auto", "aliyun", "http", "mock"}:
        raise ValueError("OCR_PROVIDER 只能是 auto、aliyun、http 或 mock")
    if provider in {"auto", "aliyun"} and aliyun_ocr_configured():
        return AlibabaCloudOcrProvider(
            os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID", ""),
            os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", ""),
            os.getenv("ALIBABA_CLOUD_OCR_ENDPOINT", "ocr-api.cn-hangzhou.aliyuncs.com"),
            os.getenv("ALIBABA_CLOUD_OCR_TYPE", "General"),
            int(os.getenv("OCR_TIMEOUT_SECONDS", "30")),
            before_request,
        )
    endpoint, key = os.getenv("OCR_API_URL", "").strip(), os.getenv("OCR_API_KEY", "").strip()
    if provider in {"auto", "http"} and endpoint and key:
        return HttpOcrProvider(endpoint, key, int(os.getenv("OCR_TIMEOUT_SECONDS", "30")), before_request)
    if provider in {"aliyun", "http"}:
        raise ValueError(f"{provider} OCR 配置不完整")
    return MockOcrProvider()
