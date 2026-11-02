"""Private, opt-in AI preparation for printable bead portraits.

The browser keeps the original photo locally until the user explicitly chooses
AI enhancement.  A single request is then sent to the configured Model Studio
workspace and the temporary provider result is downloaded immediately.  No
source or generated portrait is persisted by this module.
"""

from __future__ import annotations

import base64
import io
import json
import os
import random
from dataclasses import dataclass
from urllib import error, parse, request

from PIL import Image, ImageOps

from .http_client import TRUSTED_SSL_CONTEXT


_ALLOWED_INPUTS = {"image/jpeg", "image/png", "image/webp"}
_ALLOWED_OUTPUT_HOST_SUFFIXES = (
    ".oss-accelerate.aliyuncs.com",
    ".oss-cn-beijing.aliyuncs.com",
)
_MAX_INPUT_BYTES = 10 * 1024 * 1024
_MAX_OUTPUT_BYTES = 12 * 1024 * 1024

_BASE_PROMPT = """
基于输入照片生成一张适合后续制作实体拼豆板图的精修底图。先自动判断主体是单人、双人、宠物，
还是人与宠物互动合照。严格保留每个人物身份、脸型、五官比例、发型、帽子、服装、手势以及每只
动物的物种、毛色、斑纹、耳朵、眼睛和姿态；不要换人、换宠物或增减主体。主体整体自然提亮，暗部有
层次但不过曝；加强脸、头发、帽子、服装和手部之间的轮廓分界。简化杂乱背景为低细节的浅粉与
奶油色柔和背景。输出应是一张清晰、自然、适合后续取色的肖像底图，不要提前制作像素画、马赛克、
拼豆效果或卡通化人物。让肤色、头发、帽子、衣服和手部的明暗分区清楚、轮廓连续，避免密集噪点。
不要添加文字、坐标、网格线、水印或边框。竖版构图，人物居中，保留
完整头部、肩部、手势和宠物身体关键部分。避免改变人物身份或动物品种、脸部变形、眼睛不对称、
多余手指或肢体、断指、错误的爪子、塑料皮肤、
极端磨皮、低清晰度、廉价马赛克效果或任何网格。最终拼豆网格由应用在下一步生成。
""".strip()

_RETRY_DIRECTIONS = (
    "优先保证人物五官或宠物面部的相似度，背景尽量简洁，白色毛发与帽子要保留冷暖明暗层次。",
    "优先保证手部、爪子、发丝、毛发外轮廓和服装边缘准确，减少背景小色块，让实体制作更容易。",
    "使用更清晰的中等大小色块与柔和渐变，眼睛、嘴唇和帽檐保留关键细节，整体更明亮。",
    "减少粉色偏色，保持真实肤色和深棕头发；白色帽子、黄色装饰和粉色衣服彼此边界清楚。",
    "采用高档定制拼豆肖像的构图，主体稍大、背景更少，关键轮廓适合转成 87 格以上板图。",
)


@dataclass(frozen=True)
class BeadAiResult:
    image: bytes
    mime_type: str
    model: str
    attempt: int


class BailianBeadProvider:
    def __init__(self) -> None:
        self.api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        self.base_url = os.getenv("DASHSCOPE_BASE_URL", "").strip().rstrip("/")
        self.model = os.getenv("DASHSCOPE_IMAGE_MODEL", "qwen-image-3.0").strip() or "qwen-image-3.0"
        self.final_model = os.getenv("DASHSCOPE_IMAGE_FINAL_MODEL", "qwen-image-3.0-pro").strip() or "qwen-image-3.0-pro"

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url.startswith("https://"))

    @staticmethod
    def _validated_input(image: bytes, mime_type: str) -> None:
        if mime_type not in _ALLOWED_INPUTS:
            raise ValueError("AI 精修仅支持 JPG、PNG 或 WebP 照片")
        if not image or len(image) > _MAX_INPUT_BYTES:
            raise ValueError("AI 精修照片不能为空且不能超过 10 MB")
        try:
            with Image.open(io.BytesIO(image)) as source:
                source.verify()
            with Image.open(io.BytesIO(image)) as source:
                width, height = source.size
                if width < 256 or height < 256:
                    raise ValueError("照片分辨率过低，请选择宽高至少 256 像素的照片")
                if width * height > 40_000_000:
                    raise ValueError("照片像素尺寸过大，请先在系统相册中缩小")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("照片内容损坏或格式无效") from exc

    @staticmethod
    def _provider_url(value: object) -> str:
        url = str(value or "").strip()
        parsed = parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise RuntimeError("百炼没有返回安全的图片地址")
        hostname = parsed.hostname.lower()
        if not any(hostname.endswith(suffix) for suffix in _ALLOWED_OUTPUT_HOST_SUFFIXES):
            raise RuntimeError("百炼返回了未授权的图片存储地址")
        return url

    def _download_result(self, url: str) -> tuple[bytes, str]:
        response = request.urlopen(
            request.Request(url, headers={"Accept": "image/png,image/jpeg,image/webp"}),
            timeout=45,
            context=TRUSTED_SSL_CONTEXT,
        )
        try:
            length = int(response.headers.get("Content-Length", "0") or 0)
            if length > _MAX_OUTPUT_BYTES:
                raise RuntimeError("百炼生成图片过大")
            raw = response.read(_MAX_OUTPUT_BYTES + 1)
        finally:
            response.close()
        if len(raw) > _MAX_OUTPUT_BYTES:
            raise RuntimeError("百炼生成图片过大")
        try:
            with Image.open(io.BytesIO(raw)) as source:
                source.load()
                image = ImageOps.exif_transpose(source).convert("RGB")
                image.thumbnail((1536, 2048), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                image.save(output, "JPEG", quality=92, optimize=True)
        except Exception as exc:
            raise RuntimeError("百炼返回的图片无法读取") from exc
        return output.getvalue(), "image/jpeg"

    def enhance(self, image: bytes, mime_type: str, *, attempt: int = 1, final_quality: bool = False) -> BeadAiResult:
        self._validated_input(image, mime_type)
        if not self.configured:
            raise RuntimeError("百炼图片服务尚未配置")
        attempt = max(1, min(20, int(attempt or 1)))
        model = self.final_model if final_quality else self.model
        prompt = f"{_BASE_PROMPT}\n\n本次变化重点：{_RETRY_DIRECTIONS[(attempt - 1) % len(_RETRY_DIRECTIONS)]}"
        payload = json.dumps(
            {
                "model": model,
                "prompt": prompt,
                "image": f"data:{mime_type};base64,{base64.b64encode(image).decode('ascii')}",
                "n": 1,
                "size": "768x1024",
                "watermark": False,
                "prompt_extend": True,
                "seed": random.SystemRandom().randint(1, 2_147_483_647),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        api_request = request.Request(
            f"{self.base_url}/images/generations",
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            response = request.urlopen(api_request, timeout=120, context=TRUSTED_SSL_CONTEXT)
            try:
                body = json.loads(response.read(2_000_000).decode("utf-8"))
            finally:
                response.close()
        except error.HTTPError as exc:
            try:
                detail = json.loads(exc.read(200_000).decode("utf-8")).get("error", {}).get("message")
            except Exception:
                detail = None
            raise RuntimeError(f"百炼图片生成失败：{detail or '请稍后重试'}") from exc
        except (error.URLError, TimeoutError) as exc:
            raise RuntimeError("百炼图片生成超时，请稍后重试") from exc
        items = body.get("data") if isinstance(body, dict) else None
        if not isinstance(items, list) or not items:
            raise RuntimeError("百炼没有返回生成图片")
        output, output_type = self._download_result(self._provider_url(items[0].get("url")))
        return BeadAiResult(output, output_type, model, attempt)


def bead_ai_status() -> dict[str, object]:
    provider = BailianBeadProvider()
    return {
        "configured": provider.configured,
        "provider": "aliyun_bailian" if provider.configured else "unconfigured",
        "model": provider.model if provider.configured else None,
        "final_model": provider.final_model if provider.configured else None,
    }
