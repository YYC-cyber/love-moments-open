"""Image validation, privacy sanitising, and thumbnail generation.

Images are decoded before they are accepted.  Static images are re-encoded
without EXIF, comments, ICC profiles, or other metadata so the stored object
cannot silently retain GPS/device information supplied by the uploader.
"""

from __future__ import annotations

import io
import os
import warnings
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError


MAX_IMAGE_PIXELS = 40_000_000
MAX_ANIMATION_FRAMES = 300
THUMBNAIL_SIZE = (640, 640)
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


@dataclass(frozen=True)
class SanitizedImage:
    content: bytes
    mime_type: str
    extension: str
    width: int
    height: int
    thumbnail: bytes
    thumbnail_mime_type: str = "image/jpeg"
    thumbnail_extension: str = ".jpg"
    metadata_stripped: bool = True


def _open_verified(data: bytes) -> Image.Image:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            candidate = Image.open(io.BytesIO(data))
            candidate.verify()
            image = Image.open(io.BytesIO(data))
            image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("图片内容损坏、格式无效或像素尺寸过大") from exc
    if image.width <= 0 or image.height <= 0 or image.width * image.height > MAX_IMAGE_PIXELS:
        image.close()
        raise ValueError("图片像素尺寸超出安全范围")
    frames = int(getattr(image, "n_frames", 1) or 1)
    if frames > MAX_ANIMATION_FRAMES:
        image.close()
        raise ValueError("动图帧数过多")
    if frames > 1:
        image.close()
        raise ValueError("为避免隐藏元数据和资源消耗，暂不支持动图，请先导出为静态图片")
    return image


def _flatten_for_jpeg(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    background = Image.new("RGB", rgba.size, (248, 242, 236))
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


def _thumbnail(image: Image.Image) -> bytes:
    preview = ImageOps.exif_transpose(image).copy()
    preview.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
    preview = _flatten_for_jpeg(preview)
    output = io.BytesIO()
    preview.save(output, format="JPEG", quality=84, optimize=True, progressive=True)
    preview.close()
    return output.getvalue()


def _configured_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(os.getenv(name, str(default))), maximum))
    except (TypeError, ValueError):
        return default


def _resize_for_storage(image: Image.Image) -> Image.Image:
    maximum_edge = _configured_int("MEDIA_IMAGE_MAX_EDGE", 4096, 1280, 8192)
    resized = image.copy()
    if max(resized.size) > maximum_edge:
        resized.thumbnail((maximum_edge, maximum_edge), Image.Resampling.LANCZOS)
    return resized


def sanitize_image(data: bytes, detected_mime_type: str) -> SanitizedImage:
    image = _open_verified(data)
    try:
        transposed = ImageOps.exif_transpose(image)
        if transposed is not image:
            image.close()
        image = _resize_for_storage(transposed)
        if transposed is not image:
            transposed.close()
        width, height = image.size
        thumbnail = _thumbnail(image)
        output = io.BytesIO()
        quality = _configured_int("MEDIA_IMAGE_QUALITY", 88, 70, 95)
        if detected_mime_type == "image/jpeg":
            sanitized = _flatten_for_jpeg(image)
            sanitized.save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
            sanitized.close()
            mime_type, extension = "image/jpeg", ".jpg"
        elif detected_mime_type == "image/png":
            sanitized = image.convert("RGBA") if "A" in image.getbands() else image.convert("RGB")
            sanitized.save(output, format="PNG", optimize=True)
            sanitized.close()
            mime_type, extension = "image/png", ".png"
        elif detected_mime_type == "image/webp":
            sanitized = image.convert("RGBA") if "A" in image.getbands() else image.convert("RGB")
            sanitized.save(output, format="WEBP", quality=quality, method=6)
            sanitized.close()
            mime_type, extension = "image/webp", ".webp"
        elif detected_mime_type == "image/gif":
            sanitized = image.convert("P", palette=Image.Palette.ADAPTIVE, colors=256)
            sanitized.save(output, format="GIF", optimize=True)
            sanitized.close()
            mime_type, extension = "image/gif", ".gif"
        else:
            raise ValueError("不支持的图片格式")
        return SanitizedImage(
            content=output.getvalue(),
            mime_type=mime_type,
            extension=extension,
            width=width,
            height=height,
            thumbnail=thumbnail,
        )
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("不支持"):
            raise
        raise ValueError("图片安全处理失败") from exc
    finally:
        image.close()
