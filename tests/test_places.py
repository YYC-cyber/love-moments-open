from __future__ import annotations

import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from love_moments.maps import AmapProvider
from love_moments.places import GoogleMapsPlaceSearch, normalize_place_candidate


class FakeResponse:
    def __init__(self, value: dict):
        self.payload = json.dumps(value, ensure_ascii=False).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.payload


class PlaceResolutionTests(unittest.TestCase):
    def test_amap_place_candidates_keep_structured_scope(self) -> None:
        payload = {"status": "1", "pois": [{
            "id": "B0FFG", "name": "上海迪士尼度假区", "address": "申迪北路753号",
            "pname": "上海市", "cityname": "上海市", "adname": "浦东新区",
            "location": "121.6678,31.1496", "type": "风景名胜;主题公园",
        }]}
        with patch("love_moments.maps.request.urlopen", return_value=FakeResponse(payload)):
            rows = AmapProvider("key").search_places("迪士尼", "上海")
        self.assertEqual(rows[0]["provider_place_id"], "B0FFG")
        self.assertEqual(rows[0]["city"], "上海市")
        self.assertEqual(rows[0]["district"], "浦东新区")
        self.assertAlmostEqual(rows[0]["latitude"], 31.1496)

    def test_google_maps_candidates_do_not_auto_select_or_drop_coordinates(self) -> None:
        captured = []
        payload = {"local_results": [{
            "title": "Tokyo Disneyland", "address": "1-1 Maihama, Urayasu, Chiba, Japan",
            "place_id": "ChIJ123", "type": "Theme park",
            "gps_coordinates": {"latitude": 35.6329, "longitude": 139.8804},
        }]}

        def open_request(req, **_kwargs):
            captured.append(req.full_url)
            return FakeResponse(payload)

        with patch("love_moments.places.request.urlopen", side_effect=open_request):
            rows = GoogleMapsPlaceSearch("key").search("迪士尼", "东京")
        query = parse_qs(urlsplit(captured[0]).query)
        self.assertEqual(query["engine"], ["google_maps"])
        self.assertEqual(query["q"], ["东京 迪士尼"])
        self.assertEqual(rows[0]["name"], "Tokyo Disneyland")
        self.assertEqual(rows[0]["provider_place_id"], "ChIJ123")

    def test_non_map_candidate_cannot_claim_invented_coordinates(self) -> None:
        candidate = normalize_place_candidate({"provider": "manual", "name": "人民公园", "city": "上海"})
        self.assertIsNone(candidate["latitude"])
        with self.assertRaisesRegex(ValueError, "缺少坐标"):
            normalize_place_candidate({"provider": "amap", "name": "人民公园"})


if __name__ == "__main__":
    unittest.main()
