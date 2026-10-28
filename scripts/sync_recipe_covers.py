#!/usr/bin/env python3
"""Download exact recipe covers without consuming configured API allowances.

Openverse and public image-result pages provide automatic discovery.  A small set
of difficult dishes can additionally use manually reviewed recipe-page photos:
those are accepted only after the pixels and the source recipe have both been
checked against this catalog's ingredients and cooking method.  Every downloaded
file is normalised locally and retains its source and usage note in the manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import ipaddress
import json
import re
import socket
import sys
import time
import unicodedata
from io import BytesIO
from functools import lru_cache
from pathlib import Path
from urllib import error as urlerror
from urllib import parse, request

from PIL import Image, ImageOps, UnidentifiedImageError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from love_moments.recipes import load_builtin_recipes


MANIFEST_PATH = ROOT / "love_moments" / "data" / "recipe_cover_manifest.json"
OUTPUT_DIR = ROOT / "static" / "assets" / "recipe-covers"
OPENVERSE_ENDPOINT = "https://api.openverse.org/v1/images/"
USER_AGENT = "LoveMomentsRecipeCoverAudit/1.0 (personal recipe catalog)"
IMAGE_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/140.0 Safari/537.36"
MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024
MIN_WIDTH = 360
MIN_HEIGHT = 300
KNOWN_LICENSES = {
    "cc0", "pdm", "by", "by-sa", "by-nc", "by-nc-sa", "by-nd", "by-nc-nd",
}
LICENSE_PRIORITY = {"cc0": 0, "pdm": 1, "by": 2, "by-sa": 3, "by-nc": 4, "by-nc-sa": 5, "by-nd": 6, "by-nc-nd": 7}
SEARCH_ALIASES = {
    "葱姜蒸鸡": ["葱姜鸡"],
    "煎蛋吐司": ["鸡蛋吐司"],
    "培根蘑菇烘蛋": ["培根蘑菇蛋饼", "培根蘑菇意式烘蛋"],
    "香草烤蔬菜拼盘": ["香草烤蔬菜", "烤蔬菜拼盘"],
    "柠檬黄油三文鱼": ["柠檬黄油煎三文鱼"],
    "苹果肉桂燕麦杯": ["苹果肉桂燕麦"],
    "蜜汁蒜香猪排": ["蜂蜜蒜香猪排"],
    "豆腐抱蛋": ["抱蛋豆腐"],
    "陈皮焖牛肉": ["陈皮牛肉"],
    "冬瓜薏米老火汤": ["冬瓜薏米汤"],
    "酸豆角肉末": ["肉末酸豆角"],
    "湘味口味虾": ["口味虾"],
    "永州血鸭家常版": ["永州血鸭"],
    "家常杀猪菜": ["杀猪菜"],
    "江南糖醋小排": ["糖醋小排"],
    "家常兰州牛肉面": ["兰州牛肉面"],
    "家常白馒头": ["白馒头"],
    "葱香花卷": ["葱花花卷"],
    "红豆薏米汤": ["红豆薏米水"],
    "紫米红枣粥": ["红枣紫米粥"],
    "肉松早餐饭团": ["肉松饭团"],
    "火腿滑蛋吐司": ["火腿鸡蛋吐司"],
    "蔬菜厚蛋饼": ["蔬菜厚蛋烧"],
    "嫩滑蒸水蛋": ["蒸水蛋"],
    "经典海绵蛋糕": ["海绵蛋糕"],
    "葡式蛋挞": ["葡萄牙蛋挞"],
    "黑巧克力布朗尼": ["巧克力布朗尼"],
    "奶油司康": ["原味司康"],
    "家常法棍": ["法棍面包"],
    "蒜香橄榄油意面": ["蒜香意面"],
    "番茄牛肉千层面": ["牛肉千层面"],
    "奶油蘑菇烩饭": ["蘑菇烩饭"],
    "香煎鸡肉排": ["香煎鸡排"],
    "奶油瑞典肉丸": ["瑞典肉丸"],
    "奶油蛤蜊浓汤": ["蛤蜊浓汤"],
    "烤番茄浓汤": ["番茄浓汤"],
    "希腊田园沙拉": ["希腊沙拉"],
    "经典总汇三明治": ["总汇三明治"],
    "松软美式松饼": ["美式松饼"],
    "墨西哥牛肉塔可": ["牛肉塔可"],
    "鸡肉牛油果卷饼": ["牛油果鸡肉卷"],
    "芝士鸡肉薄饼": ["芝士鸡肉墨西哥薄饼"],
    "蓝莓玛芬": ["蓝莓马芬"],
    "鲜虾肠粉": ["虾仁肠粉"],
    "香葱鸡蛋卷": ["香葱厚蛋烧"],
}

# Exact photos manually reviewed after the automatic providers either found no
# result or proposed a visibly different dish.  The local candidate is only a
# staging file used by this audit script; the durable output is the normalised
# recipe-cover asset plus the source record in the manifest.  These source pages
# are suitable for the owner's private recipe catalog.  Recheck licensing before
# redistributing the images in a public product.
REVIEWED_EXACT_CANDIDATES = {
    "煎蛋吐司": {
        "file": "fried_egg_toast.jpg",
        "title": "黑胡椒煎蛋吐司",
        "url": "https://recipe.r10s.jp/recipe-space/d/strg/ctrl/3/273a1e43f6d09d86c0c1874d1e4fae32f32f3288.59.9.3.3.jpg",
        "page": "https://recipe.rakuten.co.jp/recipe/1540032081/",
        "creator": "楽天レシピ · ピクルス",
    },
    "培根蘑菇烘蛋": {
        "file": "bacon_mushroom_frittata.jpg",
        "title": "培根蘑菇烘蛋",
        "url": "https://www.toshiba-lifestyle.com/jp/microwaves/recipes/sites/recipesite/files/2023-12/1de21369cbe28570cfa30cb561a39a5f.jpg",
        "page": "https://www.toshiba-lifestyle.com/jp/microwaves/recipes/detail/500",
        "creator": "东芝生活食谱",
    },
    "柠檬黄油三文鱼": {
        "file": "lemon_butter_salmon.jpg",
        "title": "黄油蒜香柠檬三文鱼",
        "url": "https://imgcache.dealmoon.com/fsvrugccache.dealmoon.com/ugc/14c/dfb/51f/b40f292ab51f9637b2c59b3.jpg_480_0_3_3731.jpg",
        "page": "https://www.dealmoon.com/post/1466347",
        "creator": "Dealmoon 社区食谱",
    },
    "苹果肉桂燕麦杯": {
        "file": "apple_cinnamon_oats.jpg",
        "title": "苹果肉桂燕麦杯",
        "url": "https://df641c55b9.cbaul-cdnwnd.com/35f227d5111165e9d0ce72fa9a86e98d/200000540-c8289c9205/alm%C3%A1s%20fah%C3%A9jas%20zabk%C3%A1sa.jpg",
        "page": "https://dietaenigyszeretlek-hu.webnode.hu/finomsagaim/izletes-reggelik/almas-fahejas-zabkasa/",
        "creator": "dietaénigyszeretlek 食谱",
    },
    "蜜汁蒜香猪排": {
        "file": "honey_garlic_pork.jpg",
        "title": "Honey Garlic Pork Chops",
        "url": "https://assets-jpcust.jwpsrv.com/thumbnails/nv555aqw-720.jpg",
        "page": "https://www.easyhomemadelife.com/honey-garlic-pork-chops/",
        "creator": "Easy Homemade Life",
    },
    "平菇炒鸡蛋": {
        "file": "mushroom_egg.jpg",
        "title": "平菇炒鸡蛋",
        "url": "https://k.sinaimg.cn/n/sinacn20115/320/w640h480/20190108/cf3b-hrkkwef6794712.jpg/w700d1q75cms.jpg",
        "page": "https://k.sina.cn/article_6748504833_1923e030100100dwq4.html",
        "creator": "新浪新闻食谱",
    },
    "豆瓣鲫鱼": {
        "file": "douban_crucian.jpg",
        "title": "豆瓣鲫鱼",
        "url": "https://i2.chuimg.com/4897368286f311e6a9a10242ac110002_450w_570h.jpg",
        "page": "https://m.xiachufang.com/recipe/25030/",
        "creator": "下厨房 · 大菜的美食物语",
    },
    "口水鸡": {
        "file": "mouthwatering_chicken.jpg",
        "title": "口水鸡",
        "url": "https://cdn-akamai.lkk.com/foodservices/-/media/mouth-watering-chicken-recipe.jpg",
        "page": "https://usa.lkk.com/zh-hk/foodservices/recipes/mouth-watering-chicken-recipe",
        "creator": "李锦记美国",
    },
    "上汤娃娃菜": {
        "file": "superior_cabbage.jpg",
        "title": "鸡汁上汤娃娃菜",
        "url": "https://cp1.douguo.com/upload/caiku/2/3/f/960_2328f0def2389e8627392b66434c4c4f.jpeg",
        "page": "https://www.douguo.com/cookbook/3280843.html",
        "creator": "豆果美食",
    },
    "冬瓜薏米老火汤": {
        "file": "wintermelon_barley_soup.jpg",
        "title": "冬瓜薏米猪骨汤",
        "url": "https://static.xsite.com.cn/butiao.com/static/images/2022/12/07/f5da6207508645c0adc26a363d3ad5c4~noop_lw1uiw1aept.png",
        "page": "https://www.butiao.com/shenghuo/28195.html",
        "creator": "布条百科食谱",
    },
    "酸豆角肉末": {
        "file": "pickled_bean_pork.jpg",
        "title": "酸豆角肉末",
        "url": "https://pic.rmb.bdstatic.com/bjh/250426/dump/fe0d7c77e12d9389b86c06958d5a7b12.jpeg",
        "page": "https://health.baidu.com/m/detail/ar_10121548070469003578",
        "creator": "百度健康食谱",
    },
    "上海冷面": {
        "file": "shanghai_cold_noodles.jpg",
        "title": "鸡丝麻酱上海冷面",
        "url": "https://imgcache.dealmoon.com/fsvrugccache.dealmoon.com/ugc/02c/bb9/107/e6af836f45535d26b7feb3e.jpg_1280_1280_3_82cc.jpg",
        "page": "https://www.dealmoon.com/post/1081924",
        "creator": "Dealmoon 社区食谱",
    },
    "火腿滑蛋吐司": {
        "file": "ham_egg_toast.jpg",
        "title": "火腿滑蛋吐司",
        "url": "https://recipe.r10s.jp/recipe-space/d/strg/ctrl/3/d55ac1b1486c7e57a505302857e271ef070dc877.33.9.3.3.jpg",
        "page": "https://recipe.rakuten.co.jp/recipe/1010047474/",
        "creator": "楽天レシピ · Risa0901",
    },
    "香葱鸡蛋卷": {
        "file": "scallion_egg_roll.jpg",
        "title": "香葱鸡蛋卷",
        "url": "https://storage.googleapis.com/onelife-public/blog.onelife.vn/2021/11/cach-lam-trung-chien-cuon-hanh-la-mon-chinh-893699270379.jpg",
        "page": "https://kingfoodmart.com/bai-viet/cong-thuc-nau-an/trung-chien-cuon-hanh-la",
        "creator": "Kingfoodmart 食谱",
    },
    "松软美式松饼": {
        "file": "pancakes.jpg",
        "title": "松软美式松饼",
        "url": "https://img-global-jp.cpcdn.com/recipes/657360/680x781cq80/photo.jpg",
        "page": "https://cookpad.com/jp/recipes/17929877",
        "creator": "Cookpad · nozunao",
    },
    "墨西哥牛肉塔可": {
        "file": "beef_taco_photo2.jpg",
        "title": "墨西哥牛肉塔可",
        "url": "https://www.foodrunfix.com/images/WhiteTacos.jpg",
        "page": "https://www.foodrunfix.com/old-el-paso-tacos.html",
        "creator": "Food Run Fix",
    },
    "芝士鸡肉薄饼": {
        "file": "quesadilla.jpg",
        "title": "芝士鸡肉彩椒薄饼",
        "url": "https://kitchen.sayidaty.net/uploads/small/24/2474e81748942cdb1c77943035b94e17_w750_h750.jpg",
        "page": "https://kitchen.sayidaty.net/node/23375/",
        "creator": "Sayidaty Kitchen",
    },
}


def reviewed_exact_candidates(recipe_name: str) -> list[dict]:
    item = REVIEWED_EXACT_CANDIDATES.get(recipe_name)
    if not item:
        return []
    return [{
        "title": item["title"],
        "url": item["url"],
        "foreign_landing_url": item["page"],
        "creator": item["creator"],
        "license": "source-retained-personal-use",
        "license_url": item["page"],
        "_local_path": str(ROOT / "tmp" / "recipe-cover-candidates" / item["file"]),
    }]


def normalized_title(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text = re.sub(r"^(?:file|image|文件|图片)\s*:\s*", "", text, flags=re.I)
    text = re.sub(r"\.(?:jpe?g|png|webp|gif|tiff?|bmp)$", "", text, flags=re.I)
    # Commons frequently numbers multiple photos of the exact same dish.
    text = re.sub(r"[\s_-]*[0-9０-９]+$", "", text)
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", text)


def is_exact_title(recipe_name: str, candidate_title: str) -> bool:
    return bool(normalized_title(recipe_name)) and normalized_title(recipe_name) == normalized_title(candidate_title)


@lru_cache(maxsize=512)
def _is_public_host(hostname: str) -> bool:
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, None)}
    except socket.gaierror:
        return False
    if not addresses:
        return False
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global:
            return False
    return True


def _safe_https_url(url: str) -> bool:
    parsed = parse.urlsplit(str(url or ""))
    return parsed.scheme == "https" and bool(parsed.hostname) and _is_public_host(str(parsed.hostname))


def get_json(url: str) -> dict:
    last_error: Exception | None = None
    for attempt in range(1):
        try:
            req = request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with request.urlopen(req, timeout=5) as response:
                return json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
        except (OSError, urlerror.URLError) as exc:
            last_error = exc
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"开放图库查询失败：{last_error}")


def candidate_download_urls(candidate: dict) -> list[str]:
    original = str(candidate.get("url") or "")
    values: list[str] = []
    parsed = parse.urlsplit(original)
    if parsed.hostname and parsed.hostname.endswith("wikimedia.org") and "/" in parsed.path:
        filename = parse.unquote(parsed.path.rsplit("/", 1)[-1])
        # Commons' redirect endpoint supplies a resized file and is much less likely
        # to reject a personal catalog sync than repeatedly fetching originals.
        values.append(
            "https://commons.wikimedia.org/wiki/Special:Redirect/file/"
            f"{parse.quote(filename)}?width=960"
        )
    values.append(original)
    thumbnail = str(candidate.get("thumbnail") or "")
    if thumbnail:
        values.append(thumbnail)
    return list(dict.fromkeys(item for item in values if item))


def download_image(url: str, attempts: int = 1) -> bytes:
    if not _safe_https_url(url):
        raise ValueError("图片地址不是安全的公网 HTTPS 地址")
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            req = request.Request(url, headers={"User-Agent": IMAGE_USER_AGENT, "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"})
            with request.urlopen(req, timeout=6) as response:
                final_url = response.geturl()
                if not _safe_https_url(final_url):
                    raise ValueError("图片重定向到了非公网地址")
                content_type = str(response.headers.get("Content-Type") or "").lower()
                if not content_type.startswith("image/"):
                    raise ValueError("返回内容不是图片")
                data = response.read(MAX_DOWNLOAD_BYTES + 1)
            break
        except (OSError, urlerror.URLError) as exc:
            last_error = exc
            time.sleep(1.2 * (attempt + 1))
    else:
        raise RuntimeError(f"图片下载失败：{last_error}")
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError("图片超过 12 MB")
    return data


def encode_cover(data: bytes) -> tuple[bytes, str, int, int]:
    try:
        with Image.open(BytesIO(data)) as source:
            source.verify()
        with Image.open(BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("图片无法解码") from exc
    width, height = image.size
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        raise ValueError(f"图片尺寸过小：{width}×{height}")
    ratio = width / height
    if ratio < 0.55 or ratio > 2.4:
        raise ValueError(f"图片比例不适合作为封面：{ratio:.2f}")
    fingerprint_image = ImageOps.fit(image, (16, 16), method=Image.Resampling.LANCZOS).convert("L")
    get_pixels = getattr(fingerprint_image, "get_flattened_data", fingerprint_image.getdata)
    pixels = list(get_pixels())
    average = sum(pixels) / len(pixels)
    perceptual_hash = f"{sum((1 << index) for index, pixel in enumerate(pixels) if pixel >= average):064x}"
    image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
    output = BytesIO()
    image.save(output, "JPEG", quality=84, optimize=True, progressive=True)
    return output.getvalue(), perceptual_hash, width, height


def openverse_candidates(recipe_name: str) -> list[dict]:
    query = parse.urlencode({"q": recipe_name, "page_size": 20, "mature": "false"})
    payload = get_json(f"{OPENVERSE_ENDPOINT}?{query}")
    results = []
    for row in payload.get("results") or []:
        title = str(row.get("title") or "")
        license_code = str(row.get("license") or "").lower()
        if not is_exact_title(recipe_name, title) or license_code not in KNOWN_LICENSES:
            continue
        if not row.get("url") or not row.get("foreign_landing_url"):
            continue
        results.append(row)
    results.sort(
        key=lambda row: (
            LICENSE_PRIORITY.get(str(row.get("license") or "").lower(), 99),
            -int(row.get("width") or 0) * int(row.get("height") or 0),
        )
    )
    return results


def is_recipe_page_title(recipe_name: str, title: str) -> bool:
    """Require the dish name as a complete phrase, not merely a substring.

    For example, 青椒肉丝的做法 is accepted while 青椒肉丝炒饭 is rejected.
    """

    compact_title = unicodedata.normalize("NFKC", str(title or ""))
    escaped = re.escape(unicodedata.normalize("NFKC", recipe_name))
    return bool(re.search(rf"(?:^|[【\[（(\s]){escaped}(?=$|[】\]）)\s_·｜|的做怎家高清成图视菜])", compact_title))


def bing_candidates(recipe_name: str) -> list[dict]:
    """Find exact recipe-page images through Bing's public web page (no API quota)."""

    # `菜名 + 成品图` currently produces materially cleaner domestic recipe-page
    # results than adding broad words such as 菜谱/做法, which may tokenize the
    # Chinese dish name and return individual ingredients instead.
    values: list[dict] = []
    seen: set[str] = set()
    preferred_hosts = ("xiachufang.com", "douguo.com", "meishichina.com", "meishij.net")
    terms = (f"{recipe_name} 成品图", recipe_name, f"{recipe_name} 做法 步骤图")
    for term in terms:
        query = parse.urlencode({"q": term, "form": "HDRSC2", "first": 1, "setlang": "zh-cn"})
        req = request.Request(
            f"https://www.bing.com/images/search?{query}",
            headers={"User-Agent": IMAGE_USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
        )
        with request.urlopen(req, timeout=20) as response:
            page = response.read(2 * 1024 * 1024).decode("utf-8", "ignore")
        for raw in re.findall(r'\sm="([^"]+)"', page):
            try:
                row = json.loads(html.unescape(raw))
            except (TypeError, json.JSONDecodeError):
                continue
            image_url = str(row.get("murl") or "")
            page_url = str(row.get("purl") or "")
            title = str(row.get("t") or row.get("desc") or "")
            if not image_url or image_url in seen or not page_url or not is_recipe_page_title(recipe_name, title):
                continue
            if not _safe_https_url(image_url) or not _safe_https_url(page_url):
                continue
            seen.add(image_url)
            host = str(parse.urlsplit(page_url).hostname or "")
            values.append({
                "title": title,
                "url": image_url,
                "thumbnail": str(row.get("turl") or ""),
                "foreign_landing_url": page_url,
                "creator": host,
                "license": "source-retained",
                "license_url": page_url,
                "_provider": "bing_recipe_page",
                "_preferred": any(host.endswith(item) for item in preferred_hosts),
            })
            if len(values) >= 12:
                break
        if values:
            break
    values.sort(key=lambda row: (not bool(row.get("_preferred")), str(row.get("creator") or "")))
    return values


def sogou_candidates(recipe_name: str) -> list[dict]:
    """Read Sogou's public image result page without consuming an API allowance."""

    query = parse.urlencode({"query": recipe_name})
    req = request.Request(
        f"https://pic.sogou.com/pics?{query}",
        headers={"User-Agent": IMAGE_USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
    )
    with request.urlopen(req, timeout=12) as response:
        page = response.read(3 * 1024 * 1024).decode("utf-8", "ignore")
    marker = '"searchList":{"query"'
    marker_index = page.find(marker)
    if marker_index < 0:
        return []
    object_start = page.find("{", marker_index)
    payload, _ = json.JSONDecoder().raw_decode(page[object_start:])
    values: list[dict] = []
    seen: set[str] = set()
    for row in payload.get("searchList") or []:
        title = str(row.get("title") or "")
        if not is_recipe_page_title(recipe_name, title):
            continue
        if int(row.get("biaoqing") or 0) != 0 or float(row.get("pic_porn_score") or 0) > 0.2:
            continue
        reported_width = int(row.get("width") or row.get("thumbWidth") or 0)
        reported_height = int(row.get("height") or row.get("thumbHeight") or 0)
        if reported_width and reported_height and (reported_width < MIN_WIDTH or reported_height < MIN_HEIGHT):
            continue
        image_url = str(row.get("picUrl") or row.get("oriPicUrl") or row.get("locImageLink") or "")
        thumbnail_url = str(row.get("thumbUrl") or row.get("locImageLink") or "")
        page_url = str(row.get("url") or row.get("link") or "")
        if image_url.startswith("http://"):
            image_url = "https://" + image_url.removeprefix("http://")
        if page_url.startswith("http://"):
            page_url = "https://" + page_url.removeprefix("http://")
        if not image_url or image_url in seen or not _safe_https_url(image_url):
            continue
        if not page_url or not _safe_https_url(page_url):
            page_url = f"https://pic.sogou.com/pics?{query}"
        seen.add(image_url)
        values.append({
            "title": title,
            "url": image_url,
            "thumbnail": thumbnail_url,
            "foreign_landing_url": page_url,
            "creator": str(row.get("ch_site_name") or row.get("author") or "原网页"),
            "license": "source-retained",
            "license_url": page_url,
            "_provider": "sogou_recipe_page",
            "_preferred": normalized_title(title) == normalized_title(recipe_name),
        })
        if len(values) >= 6:
            break
    values.sort(key=lambda row: not bool(row.get("_preferred")))
    return values


def load_manifest() -> dict[str, dict]:
    try:
        value = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_manifest(manifest: dict[str, dict]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=1, help="从第几道菜开始（从 1 计数）")
    parser.add_argument("--limit", type=int, default=0, help="最多检查多少道菜；0 表示全部")
    parser.add_argument("--sleep", type=float, default=0.35, help="每道菜之间的礼貌等待秒数")
    parser.add_argument("--replace", action="store_true", help="重新下载已有封面")
    parser.add_argument("--reject-name", action="append", default=[], help="按菜名撤下误配封面，可重复")
    args = parser.parse_args()

    all_recipes = load_builtin_recipes()
    recipes = all_recipes[max(args.start - 1, 0):]
    if args.limit > 0:
        recipes = recipes[: args.limit]
    manifest = load_manifest()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.reject_name:
        quarantine = ROOT / "tmp" / "rejected-recipe-covers"
        quarantine.mkdir(parents=True, exist_ok=True)
        rejected = 0
        for recipe_id, item in list(manifest.items()):
            if str(item.get("recipe_name") or "") not in set(args.reject_name):
                continue
            source = OUTPUT_DIR / f"{recipe_id}.jpg"
            destination = quarantine / source.name
            if source.exists():
                digest = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
                destination = quarantine / f"{source.stem}-{digest}{source.suffix}"
                source.replace(destination)
            manifest.pop(recipe_id, None)
            rejected += 1
            print(f"REJECT {item.get('recipe_name')} -> {destination}")
        save_manifest(manifest)
        print(json.dumps({"rejected": rejected, "manifest": len(manifest)}, ensure_ascii=False))
        return 0
    used_hashes = {
        str(item.get("perceptual_hash")): recipe_id
        for recipe_id, item in manifest.items()
        if item.get("perceptual_hash")
    }
    rejected_hashes: set[str] = set()
    quarantine = ROOT / "tmp" / "rejected-recipe-covers"
    for rejected_file in quarantine.glob("*.jpg") if quarantine.exists() else []:
        try:
            rejected_hashes.add(encode_cover(rejected_file.read_bytes())[1])
        except ValueError:
            continue
    saved = missed = skipped = failed = 0

    for local_index, recipe in enumerate(recipes, 1):
        index = max(args.start, 1) + local_index - 1
        recipe_id = str(recipe["id"])
        recipe_name = str(recipe["name"])
        output_path = OUTPUT_DIR / f"{recipe_id}.jpg"
        if not args.replace and recipe_id in manifest and output_path.exists():
            skipped += 1
            print(f"SKIP {index:03d}/{len(all_recipes):03d} {recipe_name}", flush=True)
            continue
        selected = None
        errors = []
        providers = (
            ("reviewed_recipe_page", reviewed_exact_candidates),
            # Sogou is queried first because its domestic cache is reachable and
            # does not consume a configured API allowance. Openverse remains the
            # license-preferred fallback when no exact domestic result is usable.
            ("sogou_recipe_page", sogou_candidates),
            ("openverse", openverse_candidates),
            ("bing_recipe_page", bing_candidates),
        )
        # Query providers lazily. Once a high-confidence image is saved, later
        # search engines are not contacted at all.
        search_names = [recipe_name, *SEARCH_ALIASES.get(recipe_name, [])]
        for provider_name, provider in providers:
            for search_name in search_names:
                try:
                    candidate_group = provider(search_name)
                except Exception as exc:
                    errors.append(f"{provider_name} 查询失败：{exc}")
                    continue
                for candidate in candidate_group[:4]:
                    local_path = Path(str(candidate.get("_local_path") or ""))
                    sources: list[tuple[str, bytes | None]] = []
                    if local_path.is_file():
                        sources.append((str(candidate.get("url") or ""), local_path.read_bytes()))
                    sources.extend((image_url, None) for image_url in candidate_download_urls(candidate))
                    for image_url, local_data in sources:
                        try:
                            encoded, image_hash, original_width, original_height = encode_cover(
                                local_data if local_data is not None else download_image(image_url)
                            )
                            if image_hash in rejected_hashes:
                                raise ValueError("图片曾在人工复核中被否决")
                            duplicate_of = used_hashes.get(image_hash)
                            if duplicate_of and duplicate_of != recipe_id:
                                raise ValueError(f"图片已被 {duplicate_of} 使用")
                            selected = (candidate, encoded, image_hash, original_width, original_height, provider_name, search_name)
                            break
                        except Exception as exc:
                            errors.append(str(exc))
                    if selected is not None:
                        break
                if selected is not None:
                    break
            if selected is not None:
                break
        if selected is None:
            missed += 1
            suffix = f"（候选失败：{'；'.join(errors[:2])}）" if errors else ""
            print(f"MISS {index:03d}/{len(all_recipes):03d} {recipe_name}{suffix}", flush=True)
            time.sleep(max(args.sleep, 0))
            continue
        candidate, encoded, image_hash, original_width, original_height, provider_name, search_name = selected
        output_path.write_bytes(encoded)
        license_code = str(candidate.get("license") or "").lower()
        creator = str(candidate.get("creator") or "未知作者")
        manifest[recipe_id] = {
            "recipe_name": recipe_name,
            "search_name": search_name,
            "url": f"/static/assets/recipe-covers/{recipe_id}.jpg",
            "provider": provider_name,
            "source_title": str(candidate.get("title") or ""),
            "source_url": str(candidate.get("url") or ""),
            "source_page": str(candidate.get("foreign_landing_url") or ""),
            "creator": creator,
            "license": license_code,
            "license_url": str(candidate.get("license_url") or ""),
            "attribution": (
                f"{creator} · {license_code.upper()} · Openverse"
                if provider_name == "openverse"
                else f"{creator} · 原食谱页（个人自用）"
            ),
            "match_rule": (
                "manually_reviewed_exact_recipe"
                if provider_name == "reviewed_recipe_page"
                else (
                    "curated_equivalent_dish_alias"
                    if search_name != recipe_name
                    else ("normalized_exact_title" if provider_name == "openverse" else "complete_dish_name_in_recipe_page_title")
                )
            ),
            "source_width": original_width,
            "source_height": original_height,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "perceptual_hash": image_hash,
        }
        used_hashes[image_hash] = recipe_id
        save_manifest(manifest)
        saved += 1
        print(
            f"SAVE {index:03d}/{len(all_recipes):03d} {recipe_name} <- {candidate.get('title')} "
            f"[{license_code}] {original_width}×{original_height}",
            flush=True,
        )
        time.sleep(max(args.sleep, 0))

    print(json.dumps({"saved": saved, "skipped": skipped, "missed": missed, "failed": failed, "manifest": len(manifest)}, ensure_ascii=False))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
