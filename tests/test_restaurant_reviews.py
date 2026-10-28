from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from love_moments.restaurant_reviews import RestaurantReviewSearch, _platform_for_url


class _Response:
    def __init__(self, value: dict):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.value, ensure_ascii=False).encode("utf-8")


class RestaurantReviewSearchTests(unittest.TestCase):
    def test_only_concrete_supported_platform_results_are_kept(self) -> None:
        payload = {
            "organic_results": [
                {"title": "老店评价", "link": "https://www.dianping.com/shop/123", "snippet": "具体公开评价"},
                {"title": "团购详情", "link": "https://i.meituan.com/deal/456", "snippet": "套餐信息"},
                {"title": "旅行实拍", "link": "https://www.xiaohongshu.com/explore/789", "snippet": "用户公开笔记"},
                {"title": "未知来源", "link": "https://example.com/shop", "snippet": "不应保留"},
            ]
        }
        calls = []
        provider = RestaurantReviewSearch("key", before_request=calls.append)
        with patch("love_moments.restaurant_reviews.request.urlopen", return_value=_Response(payload)):
            rows = provider.search("迪士尼小镇", ["高分餐厅"])
        self.assertEqual([row["platform"] for row in rows], ["大众点评", "美团", "小红书"])
        self.assertTrue(all(row["url"].startswith("https://") for row in rows))
        self.assertEqual(calls, ["restaurant public review search"])

    def test_domains_cannot_be_spoofed(self) -> None:
        self.assertEqual(_platform_for_url("https://www.dianping.com/shop/1"), "大众点评")
        self.assertEqual(_platform_for_url("https://dianping.com.evil.example/shop/1"), "")
        self.assertEqual(_platform_for_url("javascript:alert(1)"), "")

    def test_missing_area_or_restaurants_spends_no_request(self) -> None:
        calls = []
        provider = RestaurantReviewSearch("key", before_request=calls.append)
        self.assertEqual(provider.search("", ["店铺"]), [])
        self.assertEqual(provider.search("景区", []), [])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
