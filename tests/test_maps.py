from __future__ import annotations

import unittest

from love_moments.maps import AmapProvider
from love_moments.places import normalize_place_candidate


class AmapPhotoTests(unittest.TestCase):
    def test_search_places_keeps_only_trusted_https_photo(self) -> None:
        provider = AmapProvider("test")
        provider._get = lambda *_args, **_kwargs: {"pois": [{
            "id": "poi-1", "name": "西湖风景名胜区", "location": "120.1,30.2",
            "address": "西湖区", "pname": "浙江省", "cityname": "杭州市", "adname": "西湖区",
            "type": "风景名胜", "photos": [{"url": "http://store.is.autonavi.com/showpic/test"}],
        }]}
        result = provider.search_places("西湖", "杭州市", 3)[0]
        self.assertEqual(result["image_url"], "https://store.is.autonavi.com/showpic/test")
        normalized = normalize_place_candidate(result)
        self.assertEqual(normalized["image_source"], "amap")

    def test_search_places_drops_untrusted_photo_host(self) -> None:
        provider = AmapProvider("test")
        provider._get = lambda *_args, **_kwargs: {"pois": [{
            "id": "poi-1", "name": "测试景点", "location": "120.1,30.2",
            "photos": [{"url": "https://example.invalid/wrong.jpg"}],
        }]}
        self.assertEqual(provider.search_places("测试", "", 3)[0]["image_url"], "")


if __name__ == "__main__":
    unittest.main()
