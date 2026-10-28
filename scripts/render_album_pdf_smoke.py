"""Generate a representative local-only Task 16 PDF for visual QA."""

from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from love_moments.album_pdf import render_album_pdf  # noqa: E402


def _sample_image() -> bytes:
    image = Image.new("RGB", (1600, 1000), "#c98279")
    draw = ImageDraw.Draw(image)
    for offset, color in ((0, "#8b2941"), (180, "#dcae9f"), (360, "#f3ded6")):
        draw.ellipse((180 + offset, 180, 900 + offset, 900), fill=color)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=88)
    return output.getvalue()


def main() -> None:
    destination = ROOT / "output" / "pdf" / "task16-album-smoke.pdf"
    destination.parent.mkdir(parents=True, exist_ok=True)
    photograph = _sample_image()
    items = [
        {
            "resource_type": "moment", "resource_id": f"smoke-{index}",
            "event_at": f"2026-0{index + 3}-1{index}T08:30:00+08:00",
            "title": title,
            "body": body,
            "location": location, "visibility": "shared",
            "source_label": "本人填写", "date_source_label": "用户确认时间",
            "descriptions": [
                {"nickname": "演示甲", "body": "这一天值得我们以后再一起回看。"},
                {"nickname": "演示乙", "body": "照片之外，还有当时的风和笑声。"},
            ],
            "image_bytes": photograph if index in {0, 2} else (b"broken" if index == 3 else None),
            "image_mime_type": "image/jpeg" if index in {0, 2, 3} else None,
            "expects_image": index in {0, 2, 3},
        }
        for index, (title, body, location) in enumerate((
            ("春日西湖散步", "沿着湖边慢慢走，傍晚的光落在水面上。", "杭州 · 西湖"),
            ("第一次一起做晚饭", "从采购到收拾厨房都没有着急，普通的一天也很珍贵。", "家里"),
            ("望仙谷夜景", "山谷亮灯以后像一幅画，我们在桥上停留了很久。", "上饶 · 望仙谷"),
            ("照片损坏时仍保留文字", "这里用于检查图片缺失占位和较长中文段落是否会越界。" * 8, "旅行途中"),
            ("很长的标题用于检查自动换行与页面边界是否稳定不会裁切", "最后一页检查双人留言、页脚和正文之间没有重叠。", "上海"),
        ))
    ]
    pdf, warnings = render_album_pdf({
        "template": "year", "title": "2026 年 · 我们的恋爱纪念册",
        "date_range": "2026-01-01 — 2026-12-31", "audience": "shared",
        "generated_at": "2026-10-28T18:30:00+08:00", "items": items,
    })
    destination.write_bytes(pdf)
    print(f"{destination}\npages={len(items) + 2} bytes={len(pdf)} warnings={','.join(warnings) or 'none'}")


if __name__ == "__main__":
    main()
