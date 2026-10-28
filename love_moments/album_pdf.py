"""Pure local A4 renderer for short-lived memory albums."""

from __future__ import annotations

import io
import unicodedata
import warnings
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageFile, UnidentifiedImageError
from reportlab.lib.colors import Color, HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas


Image.MAX_IMAGE_PIXELS = 40_000_000
ImageFile.LOAD_TRUNCATED_IMAGES = False
_FONT_NAME = "LoveMomentsNotoSansSC"
_FONT_PATH = Path(__file__).resolve().parent / "assets" / "fonts" / "NotoSansSC-Regular.ttf"
_BURGUNDY, _INK, _MUTED = HexColor("#8B2941"), HexColor("#302426"), HexColor("#867477")
_PAPER, _BLUSH, _WHITE = HexColor("#FBF6F1"), HexColor("#F1DEDE"), HexColor("#FFFFFF")
_PAGE_WIDTH, _PAGE_HEIGHT = A4
_MARGIN = 51.0
_MAX_OUTPUT = 64 * 1024 * 1024


def _font() -> str:
    if _FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(_FONT_NAME, str(_FONT_PATH)))
    return _FONT_NAME


def _safe_text(value: object) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    result: list[str] = []
    for char in text:
        code = ord(char)
        if char in {"\n", "\t"}:
            result.append(" " if char == "\t" else "\n")
        elif code < 32 or code == 127:
            continue
        elif code > 0xFFFF:
            result.append("[表情]")
        else:
            result.append(char)
    return "".join(result)


def _lines(text: object, width: float, size: float, *, max_lines: int | None = None) -> list[str]:
    output: list[str] = []
    for paragraph in _safe_text(text).splitlines() or [""]:
        current = ""
        for char in paragraph:
            candidate = current + char
            if current and pdfmetrics.stringWidth(candidate, _font(), size) > width:
                output.append(current)
                current = char
                if max_lines and len(output) >= max_lines:
                    output[-1] = output[-1][:-1] + "…" if output[-1] else "…"
                    return output
            else:
                current = candidate
        if current or not paragraph:
            output.append(current)
        if max_lines and len(output) >= max_lines:
            return output[:max_lines]
    return output


def _draw_lines(pdf: Canvas, lines: Iterable[str], x: float, y: float, *, size: float, leading: float, color: Color = _INK) -> float:
    pdf.setFillColor(color)
    pdf.setFont(_font(), size)
    current = y
    for line in lines:
        pdf.drawString(x, current, line)
        current -= leading
    return current


def _footer(pdf: Canvas, page_number: int, generated_at: str) -> None:
    pdf.setStrokeColor(_BLUSH)
    pdf.line(_MARGIN, 32, _PAGE_WIDTH - _MARGIN, 32)
    pdf.setFillColor(_MUTED)
    pdf.setFont(_font(), 7.5)
    pdf.drawString(_MARGIN, 18, "仅包含生成时及下载时仍有权限的内容")
    pdf.drawRightString(_PAGE_WIDTH - _MARGIN, 18, f"{generated_at[:10]}  ·  {page_number}")


def _cover(pdf: Canvas, document: dict[str, Any], page_number: int) -> None:
    pdf.setFillColor(_PAPER)
    pdf.rect(0, 0, _PAGE_WIDTH, _PAGE_HEIGHT, stroke=0, fill=1)
    pdf.setFillColor(_BURGUNDY)
    pdf.roundRect(_MARGIN, _PAGE_HEIGHT - 235, _PAGE_WIDTH - 2 * _MARGIN, 126, 18, stroke=0, fill=1)
    pdf.setFillColor(_WHITE)
    pdf.setFont(_font(), 12)
    pdf.drawString(_MARGIN + 24, _PAGE_HEIGHT - 142, "LOVE MOMENTS · 恋屿")
    title_lines = _lines(document.get("title"), _PAGE_WIDTH - 2 * _MARGIN - 48, 27, max_lines=3)
    _draw_lines(pdf, title_lines, _MARGIN + 24, _PAGE_HEIGHT - 178, size=27, leading=34, color=_WHITE)
    audience = "个人纪念册" if document.get("audience") == "personal" else "共同纪念册"
    pdf.setFillColor(_INK)
    pdf.setFont(_font(), 13)
    pdf.drawString(_MARGIN, _PAGE_HEIGHT - 292, audience)
    pdf.setFillColor(_MUTED)
    pdf.setFont(_font(), 11)
    pdf.drawString(_MARGIN, _PAGE_HEIGHT - 316, _safe_text(document.get("date_range")))
    pdf.drawString(_MARGIN, _PAGE_HEIGHT - 342, f"已选 {len(document.get('items') or [])} 条回忆")
    pdf.setFillColor(_BLUSH)
    pdf.circle(_PAGE_WIDTH - _MARGIN - 26, _PAGE_HEIGHT - 320, 24, stroke=0, fill=1)
    pdf.setFillColor(_BURGUNDY)
    pdf.setFont(_font(), 23)
    pdf.drawCentredString(_PAGE_WIDTH - _MARGIN - 26, _PAGE_HEIGHT - 328, "♥")
    _footer(pdf, page_number, str(document.get("generated_at") or ""))


def _summary(pdf: Canvas, document: dict[str, Any], page_number: int) -> None:
    pdf.setFillColor(_PAPER)
    pdf.rect(0, 0, _PAGE_WIDTH, _PAGE_HEIGHT, stroke=0, fill=1)
    pdf.setFillColor(_INK)
    pdf.setFont(_font(), 23)
    pdf.drawString(_MARGIN, _PAGE_HEIGHT - 70, "回忆目录")
    dates: list[str] = []
    for item in document.get("items") or []:
        value = str(item.get("event_at") or "")[:10] or "日期未记录"
        if value not in dates:
            dates.append(value)
    y = _PAGE_HEIGHT - 112
    for index, value in enumerate(dates[:26], 1):
        pdf.setFillColor(_BURGUNDY)
        pdf.circle(_MARGIN + 7, y + 3, 3.5, stroke=0, fill=1)
        pdf.setFillColor(_INK)
        pdf.setFont(_font(), 11)
        pdf.drawString(_MARGIN + 23, y, f"{index:02d}  {value}")
        y -= 24
    pdf.setFillColor(_MUTED)
    pdf.setFont(_font(), 8.5)
    notice = "旅行内容按日期窗口筛选并由你在预览中确认。" if document.get("template") == "trip" else "内容按发生日期排列。"
    pdf.drawString(_MARGIN, 55, notice)
    _footer(pdf, page_number, str(document.get("generated_at") or ""))


def _safe_image(raw: object) -> tuple[io.BytesIO, int, int] | None:
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(bytes(raw))) as source:
                source.load()
                if source.width * source.height > Image.MAX_IMAGE_PIXELS:
                    return None
                image = source.convert("RGB")
                image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                target = io.BytesIO()
                image.save(target, format="JPEG", quality=84, optimize=True)
                target.seek(0)
                return target, image.width, image.height
    except (
        UnidentifiedImageError, OSError, ValueError,
        Image.DecompressionBombError, Image.DecompressionBombWarning,
    ):
        return None


def _content_page(pdf: Canvas, document: dict[str, Any], item: dict[str, Any], page_number: int) -> bool:
    pdf.setFillColor(_PAPER)
    pdf.rect(0, 0, _PAGE_WIDTH, _PAGE_HEIGHT, stroke=0, fill=1)
    date_text = str(item.get("event_at") or "")[:10] or "日期未记录"
    pdf.setFillColor(_BURGUNDY)
    pdf.setFont(_font(), 10)
    pdf.drawString(_MARGIN, _PAGE_HEIGHT - 57, date_text)
    title_lines = _lines(item.get("title") or "未命名回忆", _PAGE_WIDTH - 2 * _MARGIN, 20, max_lines=2)
    y = _draw_lines(pdf, title_lines, _MARGIN, _PAGE_HEIGHT - 87, size=20, leading=26)
    image = _safe_image(item.get("image_bytes"))
    image_top = y - 8
    if image:
        buffer, width, height = image
        scale = min((_PAGE_WIDTH - 2 * _MARGIN) / width, 245.0 / height)
        draw_width, draw_height = width * scale, height * scale
        pdf.drawImage(ImageReader(buffer), (_PAGE_WIDTH - draw_width) / 2, image_top - draw_height, width=draw_width, height=draw_height, preserveAspectRatio=True, mask="auto")
        y = image_top - draw_height - 18
    else:
        pdf.setStrokeColor(_BLUSH)
        pdf.setFillColor(HexColor("#F7ECE9"))
        pdf.roundRect(_MARGIN, image_top - 172, _PAGE_WIDTH - 2 * _MARGIN, 172, 14, stroke=1, fill=1)
        pdf.setFillColor(_MUTED)
        pdf.setFont(_font(), 11)
        placeholder = "照片暂不可用 · 已保留文字回忆" if item.get("expects_image") else "一段值得珍藏的共同回忆"
        pdf.drawCentredString(_PAGE_WIDTH / 2, image_top - 88, placeholder)
        y = image_top - 190
    meta_parts = [value for value in (item.get("location"), item.get("source_label"), item.get("date_source_label")) if value]
    if meta_parts:
        meta = "  ·  ".join(_safe_text(value) for value in meta_parts)
        y = _draw_lines(pdf, _lines(meta, _PAGE_WIDTH - 2 * _MARGIN, 8.5, max_lines=2), _MARGIN, y, size=8.5, leading=12, color=_MUTED) - 4
    y = _draw_lines(pdf, _lines(item.get("body"), _PAGE_WIDTH - 2 * _MARGIN, 10.5, max_lines=9), _MARGIN, y, size=10.5, leading=16) - 8
    for description in (item.get("descriptions") or [])[:2]:
        if y < 70:
            break
        label = f"{_safe_text(description.get('nickname') or '我们')}：{_safe_text(description.get('body'))}"
        lines = _lines(label, _PAGE_WIDTH - 2 * _MARGIN - 20, 9, max_lines=3)
        height = max(36, len(lines) * 13 + 16)
        pdf.setFillColor(_WHITE)
        pdf.roundRect(_MARGIN, y - height + 6, _PAGE_WIDTH - 2 * _MARGIN, height, 10, stroke=0, fill=1)
        _draw_lines(pdf, lines, _MARGIN + 10, y - 9, size=9, leading=13, color=_INK)
        y -= height + 7
    _footer(pdf, page_number, str(document.get("generated_at") or ""))
    return bool(item.get("expects_image")) and image is None


def render_album_pdf(document: dict[str, Any]) -> tuple[bytes, list[str]]:
    """Return PDF bytes and fixed warning codes; nothing is persisted."""
    if not isinstance(document, dict) or document.get("template") not in {"year", "trip"}:
        raise ValueError("纪念册渲染参数无效")
    items = document.get("items")
    if not isinstance(items, list) or not items or len(items) > 120:
        raise ValueError("纪念册内容数量无效")
    _font()
    output = io.BytesIO()
    pdf = Canvas(output, pagesize=A4, pageCompression=1, invariant=1)
    page = 1
    _cover(pdf, document, page)
    pdf.showPage()
    page += 1
    _summary(pdf, document, page)
    warnings: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("纪念册内容无效")
        pdf.showPage()
        page += 1
        if _content_page(pdf, document, item, page) and "media_missing" not in warnings:
            warnings.append("media_missing")
    pdf.save()
    value = output.getvalue()
    if len(value) > _MAX_OUTPUT:
        raise ValueError("纪念册文件过大")
    return value, warnings
