"""Render a signed couple card as a compact, printable PDF."""

from __future__ import annotations

import io
import unicodedata
from pathlib import Path

from PIL import Image
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A5, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas


_FONT_NAME = "LoveMomentsCardNotoSansSC"
_FONT_PATH = Path(__file__).resolve().parent / "assets" / "fonts" / "NotoSansSC-Regular.ttf"
_WIDTH, _HEIGHT = landscape(A5)


def _font() -> str:
    if _FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(_FONT_NAME, str(_FONT_PATH)))
    return _FONT_NAME


def _text(value: object) -> str:
    value = unicodedata.normalize("NFC", str(value or ""))
    return "".join(char if 32 <= ord(char) <= 0xFFFF else " " for char in value).strip()


def _lines(value: object, width: float, size: float, maximum: int) -> list[str]:
    output: list[str] = []
    current = ""
    for char in _text(value):
        candidate = current + char
        if current and pdfmetrics.stringWidth(candidate, _font(), size) > width:
            output.append(current)
            current = char
            if len(output) >= maximum:
                output[-1] = output[-1][:-1] + "…"
                return output
        else:
            current = candidate
    if current:
        output.append(current)
    return output[:maximum]


def _pattern(pdf: Canvas, name: str, accent, paper) -> None:
    pdf.saveState()
    pdf.setStrokeColor(accent)
    pdf.setFillColor(accent)
    pdf.setFillAlpha(0.12)
    pdf.setStrokeAlpha(0.17)
    if name == "wish_map":
        pdf.setDash(5, 5)
        for index in range(5):
            x, y = 24 + index * 125, 46 + (index % 2) * 55
            pdf.circle(x, y, 7, stroke=1, fill=1)
            if index: pdf.line(x - 118, 46 + ((index - 1) % 2) * 55, x, y)
    elif name == "reconcile_letter":
        for y in range(38, int(_HEIGHT - 38), 28): pdf.line(30, y, _WIDTH - 30, y)
        pdf.line(75, 30, 75, _HEIGHT - 30)
    elif name == "kiss_strawberry":
        for row in range(5):
            for col in range(8):
                x, y = 28 + col * 78, 25 + row * 92
                pdf.circle(x, y, 4, stroke=1, fill=1); pdf.line(x, y + 4, x + 5, y + 10)
    elif name == "hug_cloud":
        for index in range(14):
            x, y = 16 + (index * 89) % int(_WIDTH - 32), 22 + (index * 53) % int(_HEIGHT - 44)
            pdf.roundRect(x, y, 38 + index % 3 * 7, 19 + index % 2 * 6, 10, stroke=1, fill=0)
    elif name == "date_calendar":
        for x in range(18, int(_WIDTH), 48): pdf.line(x, 0, x, _HEIGHT)
        for y in range(18, int(_HEIGHT), 42): pdf.line(0, y, _WIDTH, y)
    elif name == "company_moon":
        for index in range(16):
            x, y = 24 + (index * 73) % int(_WIDTH - 48), 24 + (index * 47) % int(_HEIGHT - 48)
            pdf.circle(x, y, 2 + index % 2, stroke=0, fill=1)
        pdf.circle(_WIDTH - 65, _HEIGHT - 65, 22, stroke=1, fill=0); pdf.circle(_WIDTH - 55, _HEIGHT - 57, 22, stroke=1, fill=0)
    elif name == "massage_garden":
        pdf.setFont(_font(), 17)
        for index in range(13): pdf.drawString(18 + (index * 91) % int(_WIDTH - 36), 18 + (index * 61) % int(_HEIGHT - 36), "❧")
    elif name == "chore_plaid":
        for x in range(18, int(_WIDTH), 56): pdf.line(x, 0, x, _HEIGHT)
        for x in range(36, int(_WIDTH), 56): pdf.line(x, 0, x, _HEIGHT)
        for y in range(18, int(_HEIGHT), 48): pdf.line(0, y, _WIDTH, y)
    elif name == "cooking_kitchen":
        for row in range(5):
            for col in range(8):
                pdf.circle(28 + col * 78, 25 + row * 92, 3 + (row + col) % 2, stroke=1, fill=1)
    elif name == "breakfast_sunrise":
        for offset in range(-80, 560, 62): pdf.arc(offset, -35, offset + 180, 120, 0, 180)
        for index in range(12): pdf.line(28 + index * 52, 0, 45 + index * 52, 36)
    elif name == "movie_cinema":
        for x in range(12, int(_WIDTH), 34):
            pdf.roundRect(x, 12, 18, 10, 2, stroke=1, fill=1); pdf.roundRect(x, _HEIGHT - 22, 18, 10, 2, stroke=1, fill=1)
    elif name == "walk_sky":
        for offset in range(-80, 560, 68): pdf.arc(offset, -30, offset + 210, 100, 0, 180)
    elif name == "travel_luggage":
        pdf.setDash(7, 4); pdf.roundRect(20, 20, _WIDTH - 40, _HEIGHT - 40, 26, stroke=1, fill=0)
        for index in range(7): pdf.roundRect(30 + index * 88, 30 + index % 2 * 44, 44, 28, 6, stroke=1, fill=0)
    elif name == "surprise_gift":
        for x in range(18, int(_WIDTH), 70):
            for y in range(18, int(_HEIGHT), 70):
                pdf.rect(x, y, 36, 36, stroke=1, fill=0); pdf.line(x + 18, y, x + 18, y + 36); pdf.line(x, y + 18, x + 36, y + 18)
    elif name == "compliment_starlight":
        for index in range(18):
            x, y = 24 + (index * 73) % int(_WIDTH - 48), 24 + (index * 47) % int(_HEIGHT - 48)
            pdf.setFont(_font(), 9 + index % 4)
            pdf.drawString(x, y, "✦")
    elif name == "listen_radio":
        for index in range(7): pdf.arc(-30 - index * 14, 110 - index * 14, 150 + index * 32, 300 + index * 24, -55, 110)
    elif name == "comfort_pillow":
        for index in range(14):
            x, y = 16 + (index * 89) % int(_WIDTH - 32), 22 + (index * 53) % int(_HEIGHT - 44)
            pdf.roundRect(x, y, 42, 28, 10, stroke=1, fill=0)
    elif name == "game_board":
        for row in range(7):
            for col in range(11):
                if (row + col) % 2 == 0: pdf.rect(12 + col * 58, 12 + row * 58, 58, 58, stroke=0, fill=1)
    elif name == "anniversary_rose":
        pdf.setFont(_font(), 18)
        for index in range(13): pdf.drawString(18 + (index * 91) % int(_WIDTH - 36), 18 + (index * 61) % int(_HEIGHT - 36), "❀")
    elif name == "choice_ticket":
        pdf.setDash(3, 3)
        for x in range(38, int(_WIDTH), 92): pdf.line(x, 0, x, _HEIGHT)
        for y in range(30, int(_HEIGHT), 66): pdf.circle(20, y, 8, stroke=1, fill=0); pdf.circle(_WIDTH - 20, y, 8, stroke=1, fill=0)
    elif name == "goodnight_moon":
        for index in range(17): pdf.circle(24 + (index * 73) % int(_WIDTH - 48), 24 + (index * 47) % int(_HEIGHT - 48), 2, stroke=0, fill=1)
        pdf.circle(_WIDTH - 70, _HEIGHT - 70, 24, stroke=1, fill=0)
    elif name == "goodmorning_rainbow":
        for offset in range(-90, 560, 54): pdf.arc(offset, -30, offset + 190, 125, 0, 180)
    elif name == "photo_frame":
        for x, y in ((24, 24), (_WIDTH - 94, 24), (24, _HEIGHT - 76), (_WIDTH - 94, _HEIGHT - 76)):
            pdf.line(x, y, x + 42, y); pdf.line(x, y, x, y + 28)
    elif name == "promise_seal":
        for index in range(8): pdf.circle(44 + index * 82, 44 + index % 2 * 52, 18 + index % 3 * 3, stroke=1, fill=0)
        for offset in range(-200, 700, 72): pdf.line(offset, 0, offset + 260, _HEIGHT)
    else:
        pdf.roundRect(18, 18, _WIDTH - 36, _HEIGHT - 36, 24, stroke=1, fill=0)
    pdf.restoreState()


def _ornaments(pdf: Canvas, name: str, accent, gold) -> None:
    """Add print-safe themed ornamentation without covering handwriting areas."""
    pdf.saveState()
    pdf.setStrokeColor(gold)
    pdf.setFillColor(accent)
    pdf.setLineWidth(0.8)
    pdf.setStrokeAlpha(0.62)
    pdf.setFillAlpha(0.11)

    floral = {"massage_garden", "anniversary_rose", "goodmorning_rainbow", "walk_sky"}
    celestial = {"company_moon", "compliment_starlight", "goodnight_moon"}
    stationery = {"reconcile_letter", "choice_ticket", "promise_seal", "travel_luggage"}
    celebration = {"date_calendar", "surprise_gift", "photo_frame", "movie_cinema"}
    cosy = {"hug_cloud", "comfort_pillow", "breakfast_sunrise", "listen_radio"}

    if name in floral:
        for x, y, flip in ((42, 42, 1), (_WIDTH - 42, _HEIGHT - 42, -1)):
            for radius in (7, 13, 19):
                pdf.arc(x - radius, y - radius, x + radius, y + radius, 18 if flip > 0 else 198, 145)
            pdf.line(x, y, x + flip * 30, y + flip * 22)
        for offset in range(0, 5):
            pdf.circle(32 + offset * 10, _HEIGHT - 35 - (offset % 2) * 4, 2.2, stroke=1, fill=1)
    elif name in celestial:
        for index in range(9):
            x = 34 + index * 67
            y = _HEIGHT - 35 - (index % 3) * 5
            pdf.circle(x, y, 1.6 + index % 2, stroke=0, fill=1)
            if index % 3 == 0:
                pdf.line(x - 5, y, x + 5, y); pdf.line(x, y - 5, x, y + 5)
        pdf.arc(_WIDTH - 82, 23, _WIDTH - 28, 77, 42, 235)
    elif name in stationery:
        pdf.setDash(2, 3)
        pdf.line(46, _HEIGHT - 42, _WIDTH - 46, _HEIGHT - 42)
        pdf.line(46, 42, _WIDTH - 46, 42)
        pdf.setDash()
        pdf.circle(_WIDTH - 51, 51, 14, stroke=1, fill=0)
        pdf.circle(_WIDTH - 51, 51, 10, stroke=1, fill=0)
    elif name in celebration:
        for x in range(38, int(_WIDTH - 36), 42):
            pdf.line(x, _HEIGHT - 35, x + 9, _HEIGHT - 25)
            pdf.line(x + 9, _HEIGHT - 25, x + 18, _HEIGHT - 35)
        pdf.roundRect(29, 29, 54, 22, 7, stroke=1, fill=0)
        pdf.roundRect(_WIDTH - 83, _HEIGHT - 51, 54, 22, 7, stroke=1, fill=0)
    elif name in cosy:
        for x in range(35, int(_WIDTH - 35), 25):
            pdf.circle(x, 31, 4.5, stroke=1, fill=0)
            pdf.circle(x + 7, 31, 4.5, stroke=1, fill=0)
        pdf.arc(24, _HEIGHT - 74, 80, _HEIGHT - 20, 5, 175)
        pdf.arc(_WIDTH - 80, 20, _WIDTH - 24, 74, 185, 175)
    else:
        for x, y in ((40, 40), (_WIDTH - 40, 40), (40, _HEIGHT - 40), (_WIDTH - 40, _HEIGHT - 40)):
            pdf.circle(x, y, 7, stroke=1, fill=0)
            pdf.line(x - 10, y, x + 10, y); pdf.line(x, y - 10, x, y + 10)

    # A restrained satin ribbon and foil medallion make the card feel like a
    # keepsake while remaining crisp on ordinary home printers.
    pdf.setFillAlpha(0.08)
    pdf.roundRect(26, _HEIGHT - 55, _WIDTH - 52, 18, 9, stroke=0, fill=1)
    pdf.setFillAlpha(0.2)
    pdf.circle(42, _HEIGHT - 46, 9, stroke=1, fill=1)
    pdf.setFillColor(gold)
    pdf.setFillAlpha(0.7)
    pdf.setFont(_font(), 8)
    pdf.drawCentredString(42, _HEIGHT - 49, "✦")
    pdf.restoreState()


def _draw_signature(pdf: Canvas, strokes: object, x: float, y: float, width: float, height: float, color) -> None:
    if not isinstance(strokes, list):
        return
    pdf.saveState(); pdf.setStrokeColor(color); pdf.setLineWidth(1.5); pdf.setLineCap(1); pdf.setLineJoin(1)
    for stroke in strokes[:20]:
        if not isinstance(stroke, list) or not stroke:
            continue
        path = pdf.beginPath()
        for index, point in enumerate(stroke[:240]):
            if not isinstance(point, list) or len(point) != 2:
                continue
            px, py = x + float(point[0]) * width, y + (1 - float(point[1])) * height
            if index == 0: path.moveTo(px, py)
            else: path.lineTo(px, py)
        pdf.drawPath(path, stroke=1, fill=0)
    pdf.restoreState()


def _sticker_image(sticker_sheets: dict[str, bytes] | None, index: int) -> io.BytesIO | None:
    sheets = sticker_sheets or {}
    key, local_index, columns, rows = f"cards_{index // 6 + 1}", index % 6, 3, 2
    raw = sheets.get(key)
    if not raw:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as source:
            source.load(); width, height = source.width // columns, source.height // rows
            left, top = (local_index % columns) * width, (local_index // columns) * height
            image = source.crop((left, top, left + width, top + height)).convert("RGBA")
            bounds = image.getbbox()
            if bounds: image = image.crop(bounds)
            image.thumbnail((420, 300), Image.Resampling.LANCZOS)
            output = io.BytesIO(); image.save(output, "PNG", optimize=True); output.seek(0); return output
    except (OSError, ValueError):
        return None


def render_couple_card_pdf(card: dict, *, sticker_sheets: dict[str, bytes] | None = None) -> bytes:
    if not isinstance(card, dict) or card.get("status") != "signed":
        raise ValueError("只有已签名卡片可以生成 PDF")
    style = card.get("style") or {}
    paper = HexColor(str(style.get("paper") or "#FFF4F7"))
    accent = HexColor(str(style.get("accent") or "#C93465"))
    ink = HexColor(str(style.get("ink") or "#3E2830"))
    _font()
    output = io.BytesIO()
    pdf = Canvas(output, pagesize=landscape(A5), pageCompression=1, invariant=1)
    pdf.setTitle(_text(card.get("title")))
    pdf.setAuthor("恋屿 Love Moments")
    gold = HexColor("#C79A42")
    pattern = str(style.get("pattern") or "reconcile_letter")
    # Build the printable card as a real keepsake envelope: themed outer paper,
    # folded flaps, a raised letter sheet, gold edging and a wax seal.  The
    # letter itself remains light so personalised text and signatures stay
    # readable on an ordinary home printer.
    pdf.setFillColor(paper); pdf.rect(0, 0, _WIDTH, _HEIGHT, stroke=0, fill=1)
    _pattern(pdf, pattern, accent, paper)
    _ornaments(pdf, pattern, accent, gold)
    pdf.saveState()
    pdf.setStrokeColor(gold); pdf.setLineWidth(1.1); pdf.setStrokeAlpha(0.78)
    pdf.roundRect(16, 16, _WIDTH - 32, _HEIGHT - 32, 28, stroke=1, fill=0)
    pdf.setStrokeAlpha(1)
    pdf.setFillColor(accent); pdf.setFillAlpha(0.075)
    flap = pdf.beginPath(); flap.moveTo(21, _HEIGHT - 21); flap.lineTo(_WIDTH - 21, _HEIGHT - 21); flap.lineTo(_WIDTH / 2, _HEIGHT - 168); flap.close()
    pdf.drawPath(flap, stroke=0, fill=1)
    left_fold = pdf.beginPath(); left_fold.moveTo(21, 21); left_fold.lineTo(21, _HEIGHT - 154); left_fold.lineTo(_WIDTH / 2, 21); left_fold.close()
    right_fold = pdf.beginPath(); right_fold.moveTo(_WIDTH - 21, 21); right_fold.lineTo(_WIDTH - 21, _HEIGHT - 154); right_fold.lineTo(_WIDTH / 2, 21); right_fold.close()
    pdf.setFillAlpha(0.09); pdf.drawPath(left_fold, stroke=0, fill=1); pdf.drawPath(right_fold, stroke=0, fill=1)
    pdf.setFillAlpha(0.96); pdf.setFillColor(HexColor("#FFFCF7")); pdf.setStrokeColor(HexColor("#D8B56C")); pdf.setLineWidth(0.75)
    pdf.roundRect(35, 31, _WIDTH - 70, _HEIGHT - 63, 20, stroke=1, fill=1)
    pdf.setFillAlpha(1)
    pdf.setStrokeColor(accent); pdf.setStrokeAlpha(0.18); pdf.setDash(1, 3)
    pdf.roundRect(42, 38, _WIDTH - 84, _HEIGHT - 77, 17, stroke=1, fill=0)
    pdf.setDash(); pdf.setStrokeAlpha(1)
    pdf.restoreState()
    pdf.setFillColor(accent); pdf.roundRect(54, _HEIGHT - 91, 126, 27, 13, stroke=0, fill=1)
    pdf.setFillColor(HexColor("#FFFFFF")); pdf.setFont(_font(), 9)
    pdf.drawCentredString(117, _HEIGHT - 82, "LOVE MOMENTS · 恋屿珍藏")
    sticker = _sticker_image(sticker_sheets, int(style.get("sticker_index") or 0))
    if sticker:
        pdf.drawImage(ImageReader(sticker), _WIDTH - 178, _HEIGHT - 171, width=122, height=98, preserveAspectRatio=True, mask="auto", anchor="c")
    pdf.setFillColor(ink); pdf.setFont(_font(), 25)
    pdf.drawString(54, _HEIGHT - 132, _text(card.get("title"))[:20])
    pdf.setFillColor(accent); pdf.setFont(_font(), 10)
    pdf.drawRightString(_WIDTH - 54, _HEIGHT - 82, f"NO. {_text(card.get('id'))[-12:].upper()}")
    pdf.setFillColor(gold); pdf.setFont(_font(), 7.5); pdf.drawString(54, _HEIGHT - 151, "OUR LITTLE PROMISE · 由我们共同签署与珍藏")
    y = _HEIGHT - 177
    recipient_message = _text(card.get("recipient_message"))
    pdf.setFillColor(ink); pdf.setFont(_font(), 12)
    for line in _lines(card.get("message"), _WIDTH - 116, 12, 3 if recipient_message else 5):
        pdf.drawString(58, y, line); y -= 20
    if recipient_message:
        reply_lines = _lines(recipient_message, _WIDTH - 152, 9.5, 2)
        reply_height, reply_y = 23 + len(reply_lines) * 14, 128
        pdf.saveState(); pdf.setFillColor(paper); pdf.setFillAlpha(0.5); pdf.setStrokeColor(accent); pdf.setStrokeAlpha(0.28)
        pdf.roundRect(54, reply_y, _WIDTH - 108, reply_height, 9, stroke=1, fill=1)
        pdf.setFillAlpha(1); pdf.setStrokeAlpha(1); pdf.setFillColor(accent); pdf.setFont(_font(), 7.5)
        pdf.drawString(66, reply_y + reply_height - 13, "TA 的回信")
        pdf.setFillColor(ink); pdf.setFont(_font(), 9.5)
        reply_text_y = reply_y + reply_height - 27
        for line in reply_lines:
            pdf.drawString(66, reply_text_y, line); reply_text_y -= 14
        pdf.restoreState()
    pdf.setStrokeColor(gold); pdf.setStrokeAlpha(0.48); pdf.line(54, 122, _WIDTH - 54, 122); pdf.setStrokeAlpha(1)
    signature_width = (_WIDTH - 136) / 2
    _draw_signature(pdf, card.get("creator_signature"), 58, 65, signature_width, 45, accent)
    _draw_signature(pdf, card.get("recipient_signature"), 78 + signature_width, 65, signature_width, 45, accent)
    pdf.setFillColor(ink); pdf.setFont(_font(), 7.5)
    pdf.drawString(58, 51, f"发卡人签名 · {_text(card.get('creator_signature_name'))}")
    pdf.drawString(58, 39, _text(card.get("created_at"))[:16].replace("T", " "))
    pdf.drawString(78 + signature_width, 51, f"收卡人签名 · {_text(card.get('recipient_signature_name'))}")
    pdf.drawString(78 + signature_width, 39, _text(card.get("signed_at"))[:16].replace("T", " "))
    pdf.saveState(); pdf.setFillColor(accent); pdf.setStrokeColor(gold); pdf.setLineWidth(1.2)
    pdf.circle(_WIDTH - 46, 47, 19, stroke=1, fill=1)
    pdf.setFillColor(HexColor("#FFE7AD")); pdf.setFont(_font(), 7); pdf.drawCentredString(_WIDTH - 46, 45, "LM")
    pdf.restoreState()
    pdf.save()
    value = output.getvalue()
    if not value.startswith(b"%PDF-") or len(value) > 4 * 1024 * 1024:
        raise ValueError("卡片 PDF 生成失败")
    return value
