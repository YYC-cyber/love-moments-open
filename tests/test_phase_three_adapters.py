from __future__ import annotations

import json
import base64
import hashlib
import hmac
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlsplit

from love_moments.maps import AmapProvider, GeoapifyProvider, MockMapProvider
from love_moments.travel_assistant_store import TravelAssistantMixin
from love_moments.ocr import AlibabaCloudOcrProvider, HttpOcrProvider, MockOcrProvider, ocr_provider_from_env, parse_chat_text
from love_moments.quotes import AuthorizedHttpQuoteProvider, JuheTrainQuoteProvider, MockQuoteProvider, SerpApiQuoteProvider
from love_moments.storage import LocalMediaStorage, QiniuKodoMediaStorage, QiniuMirroredMediaStorage, SupabaseMediaStorage, storage_from_env


class FakeResponse:
    def __init__(self, payload: bytes = b"{}", status: int = 200):
        self.payload, self.status = payload, status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self.payload


class PhaseThreeAdapterTests(unittest.TestCase):
    def test_local_storage_roundtrip_and_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            storage = LocalMediaStorage(folder)
            stored = storage.put("images/a.png", b"hello", "image/png")
            self.assertEqual(storage.get(stored.key), b"hello")
            self.assertEqual(len(stored.checksum), 64)
            with self.assertRaises(ValueError):
                storage.put("../secret", b"x", "text/plain")
            storage.delete(stored.key)
            with self.assertRaises(KeyError):
                storage.get(stored.key)

    def test_qiniu_adapter_signs_put_get_and_delete(self) -> None:
        requests = []

        def open_request(req, timeout=0, context=None):
            del context
            requests.append((req, timeout))
            return FakeResponse(b"stored" if req.method == "GET" else b"")

        storage = QiniuKodoMediaStorage(
            "access",
            "secret",
            "love-moments",
            "cn-east-1",
            "https://s3.cn-east-1.qiniucs.com",
        )
        with patch("love_moments.storage.request.urlopen", side_effect=open_request):
            stored = storage.put("images/a.png", b"image", "image/png")
            self.assertEqual(storage.get(stored.key), b"stored")
            storage.delete(stored.key)
        self.assertEqual([item[0].method for item in requests], ["PUT", "GET", "DELETE"])
        self.assertTrue(all("AWS4-HMAC-SHA256" in item[0].headers["Authorization"] for item in requests))
        self.assertTrue(all("/cn-east-1/s3/aws4_request" in item[0].headers["Authorization"] for item in requests))
        self.assertTrue(all(item[0].full_url.startswith("https://s3.cn-east-1.qiniucs.com/love-moments/") for item in requests))

    def test_qiniu_env_selection_and_endpoint_validation(self) -> None:
        values = {
            "MEDIA_STORAGE_PROVIDER": "qiniu",
            "QINIU_ACCESS_KEY": "access",
            "QINIU_SECRET_KEY": "secret",
            "QINIU_BUCKET": "love-moments",
            "QINIU_S3_REGION": "cn-south-1",
            "QINIU_S3_ENDPOINT": "https://s3.cn-south-1.qiniucs.com",
        }
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", values, clear=True):
            storage = storage_from_env(folder)
        self.assertIsInstance(storage, QiniuMirroredMediaStorage)
        self.assertEqual(storage.remote.region, "cn-south-1")
        with self.assertRaisesRegex(ValueError, "官方 HTTPS"):
            QiniuKodoMediaStorage("access", "secret", "love-moments", "cn-east-1", "https://example.com")

        values["QINIU_SECRET_KEY"] = ""
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", values, clear=True):
            self.assertIsInstance(storage_from_env(folder), LocalMediaStorage)

    def test_qiniu_mirror_requires_both_copies_and_reads_local_first(self) -> None:
        class Remote:
            name = "qiniu"

            def __init__(self):
                self.objects = {}
                self.fail_put = False

            def put(self, key, data, content_type):
                del content_type
                if self.fail_put:
                    raise RuntimeError("remote failed")
                self.objects[key] = data
                return type("Stored", (), {
                    "key": key,
                    "size": len(data),
                    "checksum": hashlib.sha256(data).hexdigest(),
                })()

            def get(self, key):
                if key not in self.objects:
                    raise KeyError(key)
                return self.objects[key]

            def delete(self, key):
                self.objects.pop(key, None)

        with tempfile.TemporaryDirectory() as folder:
            local = LocalMediaStorage(folder)
            remote = Remote()
            storage = QiniuMirroredMediaStorage(local, remote)
            stored = storage.put("images/a.png", b"both", "image/png")
            self.assertEqual(stored.provider, "qiniu")
            self.assertEqual(local.get(stored.key), b"both")
            self.assertEqual(remote.objects[stored.key], b"both")
            remote.objects[stored.key] = b"cloud-changed"
            self.assertEqual(storage.get(stored.key), b"both")

            remote.fail_put = True
            with self.assertRaisesRegex(RuntimeError, "remote failed"):
                storage.put("images/failed.png", b"not-saved", "image/png")
            with self.assertRaises(KeyError):
                local.get("images/failed.png")

    def test_supabase_adapter_uses_authenticated_storage_endpoint(self) -> None:
        requests = []

        def open_request(req, timeout=0, context=None):
            requests.append(req)
            return FakeResponse(b"file" if req.method == "GET" else b"{}")

        storage = SupabaseMediaStorage("https://demo.supabase.co", "anon-key", "love-moments")
        with patch("love_moments.storage.request.urlopen", side_effect=open_request):
            stored = storage.put("audio/call.ogg", b"voice", "audio/ogg")
            self.assertEqual(storage.get(stored.key), b"file")
            storage.delete(stored.key)
        self.assertTrue(all(req.headers["Apikey"] == "anon-key" for req in requests))
        self.assertIn("/storage/v1/object/love-moments/audio/call.ogg", requests[0].full_url)

    def test_amap_contract_and_offline_map_are_normalized(self) -> None:
        responses = iter([
            {"status": "1", "geocodes": [{"formatted_address": "杭州西湖", "location": "120.1,30.2"}]},
            {"status": "1", "geocodes": [{"formatted_address": "杭州灵隐寺", "location": "120.2,30.3"}]},
            {"status": "1", "route": {"paths": [{"distance": "1234", "duration": "900", "steps": [{"instruction": "向前走", "distance": "1234"}]}]}},
        ])

        def open_request(url, timeout=0, context=None):
            return FakeResponse(json.dumps(next(responses)).encode())

        with patch("love_moments.maps.request.urlopen", side_effect=open_request):
            route = AmapProvider("key").route("西湖", "灵隐寺", "walking", "杭州")
        self.assertFalse(route["mock"])
        self.assertEqual(route["distance_meters"], 1234)
        offline = MockMapProvider().route("A", "B")
        self.assertTrue(offline["mock"])
        self.assertGreater(offline["duration_seconds"], 0)

    def test_amap_route_between_saved_pois_skips_geocoding(self) -> None:
        requests = []
        payload = {"status": "1", "route": {"paths": [{"distance": "850", "duration": "620", "steps": []}]}}

        def open_request(url, timeout=0, context=None):
            requests.append(url)
            return FakeResponse(json.dumps(payload).encode())

        origin = {"name": "景点甲", "provider_place_id": "A1", "latitude": 30.20, "longitude": 120.10}
        destination = {"name": "景点乙", "provider_place_id": "A2", "latitude": 30.21, "longitude": 120.11}
        with patch("love_moments.maps.request.urlopen", side_effect=open_request):
            route = AmapProvider("key").route_between(origin, destination, "walking", "杭州")
        self.assertEqual(route["distance_meters"], 850)
        self.assertEqual(len(requests), 1)
        self.assertIn("/v3/direction/walking", requests[0])
        self.assertFalse(any("/geocode/" in url for url in requests))

    def test_amap_bicycling_uses_route_planning_v5(self) -> None:
        requests = []
        payload = {"status": "1", "route": {"paths": [{"distance": "1200", "duration": "480", "steps": []}]}}

        def open_request(url, timeout=0, context=None):
            requests.append(url)
            return FakeResponse(json.dumps(payload).encode())

        origin = {"name": "迪士尼小镇", "provider_place_id": "A1", "latitude": 31.141, "longitude": 121.657}
        destination = {"name": "星愿湖", "provider_place_id": "A2", "latitude": 31.145, "longitude": 121.653}
        with patch("love_moments.maps.request.urlopen", side_effect=open_request):
            route = AmapProvider("key").route_between(origin, destination, "bicycling", "上海")
        self.assertEqual(route["duration_seconds"], 480)
        self.assertEqual(len(requests), 1)
        self.assertIn("/v5/direction/bicycling", requests[0])
        self.assertFalse(any("/geocode/" in url for url in requests))

    def test_amap_food_search_returns_real_shop_details_and_navigation(self) -> None:
        payload = {"status": "1", "pois": [{
            "id": "B001", "name": "建新园过桥米线", "address": "北京路 123 号", "location": "102.71,25.04",
            "adname": "盘龙区", "tel": "0871-123456", "type": "餐饮服务;中餐厅",
            "biz_ext": {"rating": "4.6", "cost": "32", "open_time": "07:00-21:00"},
        }]}
        with patch("love_moments.maps.request.urlopen", return_value=FakeResponse(json.dumps(payload, ensure_ascii=False).encode())):
            shops = AmapProvider("key").search_food("过桥米线|饵丝", "昆明", 5)
        self.assertEqual(shops[0]["name"], "建新园过桥米线")
        self.assertEqual(shops[0]["average_cost_cents"], 3200)
        self.assertEqual(shops[0]["rating"], 4.6)
        self.assertTrue(shops[0]["navigation_url"].startswith("https://uri.amap.com/marker?"))
        self.assertEqual(MockMapProvider().search_food("早餐", "昆明"), [])

    def test_amap_nearby_food_reuses_geocode_and_limits_radius(self) -> None:
        requests = []
        geocode = {"status": "1", "geocodes": [{"formatted_address": "望仙谷", "location": "117.81,28.68"}]}
        places = {"status": "1", "pois": []}
        responses = iter([geocode, places, places])

        def open_request(url, timeout=0, context=None):
            requests.append(url)
            return FakeResponse(json.dumps(next(responses), ensure_ascii=False).encode())

        provider = AmapProvider("key")
        with patch("love_moments.maps.request.urlopen", side_effect=open_request):
            provider.search_food("铅山烫粉|上饶米粉", "上饶市", 5, "望仙谷")
            provider.search_food("上饶土菜|江西小炒", "上饶市", 5, "望仙谷")
        self.assertEqual(sum("/v3/geocode/geo" in url for url in requests), 1)
        self.assertEqual(sum("/v3/place/around" in url for url in requests), 2)
        self.assertTrue(all("radius=5000" in url for url in requests if "/v3/place/around" in url))

    def test_amap_nearby_food_accepts_confirmed_coordinates_without_geocoding(self) -> None:
        requests = []
        payload = {"status": "1", "pois": [{
            "id": "B009", "name": "旅行中的真实餐厅", "address": "附近 8 号",
            "location": "121.501,31.201", "distance": "430", "type": "餐饮服务;中餐厅",
            "biz_ext": {"rating": "4.7", "cost": "88", "open_time": "10:00-22:00"},
        }]}

        def open_request(url, timeout=0, context=None):
            del timeout, context
            requests.append(url)
            return FakeResponse(json.dumps(payload, ensure_ascii=False).encode())

        with patch("love_moments.maps.request.urlopen", side_effect=open_request):
            shops = AmapProvider("key").search_food_nearby("本地特色", 31.20, 121.50, limit=5)
        self.assertEqual(shops[0]["name"], "旅行中的真实餐厅")
        self.assertEqual(shops[0]["distance_meters"], 430)
        self.assertEqual(len(requests), 1)
        self.assertIn("/v3/place/around", requests[0])
        self.assertIn("location=121.5%2C31.2", requests[0])
        self.assertFalse(any("/geocode/" in url for url in requests))
        self.assertEqual(MockMapProvider().search_food_nearby("本地特色", 31.2, 121.5), [])

    def test_quote_source_metadata_never_blurs_live_cache_manual_and_mock(self) -> None:
        cases = [
            ({"source_mode": "live", "mock": False, "fetched_at": "2026-10-27T00:00:00+00:00"}, "real_time", "实时查询"),
            ({"source_mode": "cached", "stale": True, "fetched_at": "2026-10-26T00:00:00+00:00"}, "cache", "缓存结果"),
            ({"source_mode": "manual", "fetched_at": "2026-10-25T00:00:00+00:00"}, "manual", "手工录入"),
            ({"source_mode": "mock", "mock": True, "fetched_at": "2026-10-24T00:00:00+00:00"}, "mock", "模拟数据"),
        ]
        for row, expected_kind, expected_label in cases:
            with self.subTest(expected_kind=expected_kind):
                value = TravelAssistantMixin._quote_source_metadata(row)
                self.assertEqual(value["data_kind"], expected_kind)
                self.assertEqual(value["source_label"], expected_label)
                self.assertEqual(value["fetched_at"], row["fetched_at"])
        unknown = TravelAssistantMixin._quote_source_metadata({"source_mode": "unexpected"})
        self.assertEqual((unknown["data_kind"], unknown["source_label"]), ("unknown", "来源待确认"))

    def test_geoapify_search_geocode_and_route_are_normalized(self) -> None:
        place_rows = {
            "results": [{
                "place_id": "place-eiffel", "name": "埃菲尔铁塔",
                "formatted": "5 Avenue Anatole France, Paris, France",
                "country": "法国", "city": "巴黎", "lat": 48.85826, "lon": 2.29450,
                "result_type": "amenity",
            }],
        }
        route_rows = {
            "features": [{
                "properties": {
                    "distance": 2350.4, "time": 1280.2,
                    "legs": [{"steps": [{"instruction": {"text": "Continue straight"}, "distance": 120.4}]}],
                },
            }],
        }
        responses = iter([place_rows, place_rows, route_rows])
        operations = []

        def open_request(req, timeout=0, context=None):
            return FakeResponse(json.dumps(next(responses), ensure_ascii=False).encode())

        provider = GeoapifyProvider("key", before_request=operations.append)
        with patch("love_moments.maps.request.urlopen", side_effect=open_request):
            candidates = provider.search_places("Eiffel Tower", "Paris, France", 5)
            destination = provider.geocode("Louvre Museum", "Paris")
            route = provider.route_between(candidates[0], destination, "walking")
        self.assertEqual(candidates[0]["provider"], "geoapify")
        self.assertEqual(candidates[0]["city"], "巴黎")
        self.assertEqual(route["provider"], "geoapify")
        self.assertEqual(route["distance_meters"], 2350)
        self.assertEqual(route["duration_seconds"], 1280)
        self.assertEqual(len(operations), 3)

    def test_ocr_parsing_mock_and_http_contract(self) -> None:
        self.assertEqual([item["speaker"] for item in parse_chat_text("12:00 我：好\n12:01 对方：嗯")], ["me", "partner"])
        flattened = parse_chat_text("12：30 我：今晚吃火锅 12：31 对方：好呀")
        self.assertEqual([(item["time"], item["speaker"]) for item in flattened], [("12:30", "me"), ("12:31", "partner")])
        raw, rows = MockOcrProvider().extract(b"", "image/png")
        self.assertIn("演示", raw)
        self.assertGreaterEqual(len(rows), 2)
        payload = {"text": "我：真实识别", "items": [{"speaker": "me", "content": "真实识别", "confidence": .99}]}
        with patch("love_moments.ocr.request.urlopen", return_value=FakeResponse(json.dumps(payload, ensure_ascii=False).encode())):
            raw, rows = HttpOcrProvider("https://ocr.test", "secret").extract(b"png", "image/png")
        self.assertEqual(rows[0]["content"], "真实识别")
        self.assertEqual(raw, "我：真实识别")

    def test_aliyun_ocr_signs_binary_request_and_normalizes_content(self) -> None:
        captured = []
        payload = {"RequestId": "request-1", "Data": {"Content": "12:00 我：到家啦\n12:01 对方：路上小心"}}

        def open_request(req, timeout=0, context=None):
            captured.append((req, timeout))
            return FakeResponse(json.dumps(payload, ensure_ascii=False).encode())

        provider = AlibabaCloudOcrProvider("access-id", "access-secret", timeout=17)
        fixed_url = provider._signed_url(nonce="nonce-1", timestamp="2026-10-14T01:02:03Z")
        query = parse_qs(urlsplit(fixed_url).query)
        signature = query.pop("Signature")[0]
        flat = {key: values[0] for key, values in query.items()}
        encode = lambda value: quote(str(value), safe="-_.~")
        canonical = "&".join(f"{encode(key)}={encode(flat[key])}" for key in sorted(flat))
        expected = base64.b64encode(hmac.new(b"access-secret&", f"POST&%2F&{encode(canonical)}".encode(), hashlib.sha1).digest()).decode()
        self.assertEqual(signature, expected)
        self.assertEqual(flat["Action"], "RecognizeAllText")
        self.assertEqual(flat["Version"], "2021-07-07")
        self.assertEqual(flat["Type"], "General")

        with patch.object(provider, "_signed_url", return_value=fixed_url), patch("love_moments.ocr.request.urlopen", side_effect=open_request):
            raw, rows = provider.extract(b"png-bytes", "image/png")
        self.assertEqual(raw, payload["Data"]["Content"])
        self.assertEqual([row["speaker"] for row in rows], ["me", "partner"])
        self.assertEqual(captured[0][0].data, b"png-bytes")
        self.assertEqual(captured[0][0].headers["Content-type"], "application/octet-stream")
        self.assertEqual(captured[0][1], 17)

    def test_aliyun_ocr_env_selection_and_boundaries(self) -> None:
        values = {
            "OCR_PROVIDER": "auto",
            "ALIBABA_CLOUD_OCR_ENABLED": "true",
            "ALIBABA_CLOUD_ACCESS_KEY_ID": "access-id",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "access-secret",
            "ALIBABA_CLOUD_OCR_ENDPOINT": "ocr-api.cn-hangzhou.aliyuncs.com",
            "ALIBABA_CLOUD_OCR_TYPE": "General",
            "OCR_TIMEOUT_SECONDS": "30",
            "OCR_API_URL": "",
            "OCR_API_KEY": "",
        }
        with patch.dict("os.environ", values, clear=True):
            provider = ocr_provider_from_env()
        self.assertIsInstance(provider, AlibabaCloudOcrProvider)
        with self.assertRaisesRegex(ValueError, "10MB"):
            provider.extract(b"x" * (10 * 1024 * 1024 + 1), "image/png")
        with self.assertRaisesRegex(ValueError, "不支持"):
            provider.extract(b"image", "text/plain")

        values["ALIBABA_CLOUD_OCR_ENABLED"] = "false"
        with patch.dict("os.environ", values, clear=True):
            self.assertIsInstance(ocr_provider_from_env(), MockOcrProvider)

    def test_quote_mock_and_authorized_contract(self) -> None:
        self.assertTrue(MockQuoteProvider().search({"destination": "杭州"})[0]["mock"])
        payload = {"quotes": [
            {"provider": "授权平台", "title": "湖景房", "amount_cents": 56800, "booking_url": "https://booking.example/item"},
            {"provider": "异常平台", "title": "危险链接", "amount_cents": "bad", "booking_url": "javascript:alert(1)"},
        ]}
        with patch("love_moments.quotes.request.urlopen", return_value=FakeResponse(json.dumps(payload, ensure_ascii=False).encode())):
            rows = AuthorizedHttpQuoteProvider("https://quotes.test", "secret").search({"type": "hotel"})
        self.assertFalse(rows[0]["mock"])
        self.assertEqual(rows[0]["amount_cents"], 56800)
        self.assertEqual(len(rows), 1)

    def test_real_provider_guards_run_immediately_before_network(self) -> None:
        operations = []
        payload = {"quotes": [{"provider": "授权平台", "title": "报价", "amount_cents": 10000}]}
        provider = AuthorizedHttpQuoteProvider(
            "https://quotes.test", "secret", before_request=operations.append
        )
        with patch("love_moments.quotes.request.urlopen", return_value=FakeResponse(json.dumps(payload).encode())) as opened:
            provider.search({"type": "hotel"})
        self.assertEqual(operations, ["authorized quote search"])
        self.assertEqual(opened.call_count, 1)

        def blocked(_operation):
            raise RuntimeError("blocked before network")

        provider.before_request = blocked
        with patch("love_moments.quotes.request.urlopen") as unopened, self.assertRaisesRegex(RuntimeError, "blocked"):
            provider.search({"type": "hotel"})
        unopened.assert_not_called()

    def test_serpapi_hotel_and_flight_results_are_normalized_for_selection(self) -> None:
        hotel_payload = {"properties": [{
            "name": "海边酒店", "description": "步行到海滩", "hotel_class": "5-star hotel",
            "overall_rating": 4.7, "reviews": 88,
            "gps_coordinates": {"latitude": 18.3001, "longitude": 109.4001},
            "rate_per_night": {"extracted_lowest": 568, "extracted_before_taxes_fees": 500},
            "prices": [{"source": "示例预订平台"}], "link": "https://hotel.example/book",
        }]}
        flight_payload = {"best_flights": [{
            "price": 1280, "total_duration": 210,
            "flights": [{"airline": "测试航司", "flight_number": "TS 100", "departure_airport": {"id": "PEK", "time": "2026-10-20 08:00"}, "arrival_airport": {"id": "SYX", "time": "2026-10-20 11:30"}}],
        }], "other_flights": []}
        provider = SerpApiQuoteProvider("secret", monthly_limit=0)
        responses = [hotel_payload, flight_payload]
        with patch("love_moments.quotes.request.urlopen", side_effect=lambda *_args, **_kwargs: FakeResponse(json.dumps(responses.pop(0), ensure_ascii=False).encode())):
            hotels = provider.search({"type": "hotel", "destination": "三亚", "destination_latitude": 18.3, "destination_longitude": 109.4, "check_in": "2026-10-20", "check_out": "2026-10-22", "adults": 2})
            flights = provider.search({"type": "flight", "origin": "北京", "destination": "三亚", "outbound_date": "2026-10-20", "adults": 2})
        self.assertEqual(hotels[0]["amount_cents"], 56800)
        self.assertEqual(hotels[0]["source_mode"], "aggregate")
        self.assertIn("评分 4.7", hotels[0]["terms"])
        self.assertIn("距已确认地点约", hotels[0]["terms"])
        self.assertIsInstance(hotels[0]["details"]["distance_meters"], int)
        self.assertEqual(flights[0]["title"], "TS 100")
        self.assertIn("直飞", flights[0]["terms"])
        self.assertTrue(flights[0]["booking_url"].startswith("https://"))

    def test_domestic_hotel_falls_back_to_a_chinese_booking_page(self) -> None:
        payload = {"properties": [{
            "name": "Phoenix Residence Qiandao Lake",
            "rate_per_night": {"extracted_lowest": 440},
            "prices": [{"source": "Bluepillow", "link": "https://www.bluepillow.com/hotel/foreign"}],
            "link": "https://www.bluepillow.com/hotel/foreign",
        }]}
        provider = SerpApiQuoteProvider("secret", monthly_limit=0)
        with patch("love_moments.quotes.request.urlopen", return_value=FakeResponse(json.dumps(payload).encode())):
            row = provider.search({
                "type": "hotel", "destination": "Qiandao Lake domestic fallback 20261027", "destination_country": "中国",
                "check_in": "2026-11-06", "check_out": "2026-11-08", "adults": 2,
            })[0]
        self.assertEqual(row["booking_url"], "https://hotels.ctrip.com/list")
        self.assertEqual(row["details"]["booking_mode"], "domestic_search")
        self.assertEqual(row["details"]["booking_label"], "复制酒店名 · 去携程查询")
        self.assertEqual(row["details"]["original_title"], "Phoenix Residence Qiandao Lake")

    def test_domestic_hotel_prefers_a_real_chinese_ota_link_but_foreign_trip_keeps_provider_link(self) -> None:
        domestic_link = "https://hotels.ctrip.com/hotels/detail/?hotelId=123"
        foreign_link = "https://hotel.example/book"
        responses = [
            {"properties": [{
                "name": "千岛湖测试酒店", "rate_per_night": {"extracted_lowest": 500},
                "prices": [{"source": "携程", "link": domestic_link}, {"source": "海外平台", "link": foreign_link}],
                "link": foreign_link,
            }]},
            {"properties": [{
                "name": "Paris Test Hotel", "rate_per_night": {"extracted_lowest": 800},
                "prices": [{"source": "International provider", "link": foreign_link}], "link": foreign_link,
            }]},
        ]
        provider = SerpApiQuoteProvider("secret", monthly_limit=0)
        with patch("love_moments.quotes.request.urlopen", side_effect=lambda *_args, **_kwargs: FakeResponse(json.dumps(responses.pop(0)).encode())):
            domestic = provider.search({
                "type": "hotel", "destination": "Qiandao Lake test domestic", "destination_country": "China",
                "check_in": "2026-11-06", "check_out": "2026-11-08",
            })[0]
            foreign = provider.search({
                "type": "hotel", "destination": "Paris test foreign", "destination_country": "France",
                "check_in": "2026-11-06", "check_out": "2026-11-08",
            })[0]
        self.assertEqual(domestic["booking_url"], domestic_link)
        self.assertEqual(domestic["details"]["booking_mode"], "domestic_direct")
        self.assertEqual(domestic["details"]["booking_label"], "去携程查看 / 购买")
        self.assertEqual(foreign["booking_url"], foreign_link)
        self.assertEqual(foreign["details"]["booking_mode"], "international")

    def test_serpapi_cache_keeps_original_fetch_time_and_is_labeled_cached(self) -> None:
        payload = {"properties": [{
            "name": "缓存标识酒店", "rate_per_night": {"extracted_lowest": 500},
            "prices": [{"source": "测试平台"}],
        }]}
        provider = SerpApiQuoteProvider("secret", monthly_limit=0)
        query = {
            "type": "hotel", "destination": "缓存测试目的地-20261027",
            "check_in": "2026-11-20", "check_out": "2026-11-21", "adults": 2,
        }
        with patch("love_moments.quotes.request.urlopen", return_value=FakeResponse(json.dumps(payload).encode())) as opened:
            first = provider.search(query)
            second = provider.search(query)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(first[0]["source_mode"], "aggregate")
        self.assertEqual(second[0]["source_mode"], "cached")
        self.assertEqual(second[0]["fetched_at"], first[0]["fetched_at"])

    def test_serpapi_resolves_unlisted_flight_city_with_provider_autocomplete(self) -> None:
        provider = SerpApiQuoteProvider("secret", monthly_limit=0)
        with patch.object(provider, "_fetch", return_value={"suggestions": [{"id": "KWE", "name": "贵阳龙洞堡国际机场"}]}) as fetched:
            self.assertEqual(provider._airport("贵阳", "出发地"), "KWE")
        self.assertEqual(fetched.call_args.args[0]["engine"], "google_flights_autocomplete")

    def test_juhe_train_result_exposes_times_all_seats_and_official_purchase_link(self) -> None:
        travel_day = (date.today() + timedelta(days=1)).isoformat()
        payload = {"error_code": 0, "reason": "success.", "result": [{
            "train_no": "G100", "departure_station": "杭州东", "arrival_station": "上海虹桥",
            "departure_time": "08:00", "arrival_time": "08:45", "duration": "00:45", "enable_booking": "Y",
            "prices": [{"seat_name": "二等座", "price": 73, "num": "有"}, {"seat_name": "一等座", "price": 117, "num": "3"}],
            "train_flags": ["复兴号", "静音车厢"],
        }]}
        with patch("love_moments.quotes.request.urlopen", return_value=FakeResponse(json.dumps(payload, ensure_ascii=False).encode())):
            rows = JuheTrainQuoteProvider("secret").search({"type": "train", "origin": "杭州东", "destination": "上海虹桥", "date": travel_day})
        self.assertEqual(rows[0]["amount_cents"], 7300)
        self.assertEqual(len(rows[0]["details"]["seats"]), 2)
        self.assertIn("12306", rows[0]["booking_url"])
        self.assertIn("二等座", rows[0]["terms"])

    def test_juhe_blank_inventory_is_unknown_and_not_preferred_as_available(self) -> None:
        travel_day = (date.today() + timedelta(days=1)).isoformat()
        payload = {"error_code": 0, "result": [{
            "train_no": "G1", "departure_station": "杭州东", "arrival_station": "上海虹桥",
            "departure_time": "08:00", "arrival_time": "09:00", "duration": "01:00", "enable_booking": "Y",
            "prices": [{"seat_name": "特等座", "price": 71, "num": ""}, {"seat_name": "二等座", "price": 80, "num": "有"}],
        }]}
        with patch("love_moments.quotes.request.urlopen", return_value=FakeResponse(json.dumps(payload, ensure_ascii=False).encode())):
            rows = JuheTrainQuoteProvider("secret").search({"type": "train", "origin": "杭州东", "destination": "上海虹桥", "date": travel_day})
        self.assertEqual(rows[0]["amount_cents"], 8000)
        self.assertEqual(rows[0]["details"]["seats"][0]["remaining"], "未知")


if __name__ == "__main__":
    unittest.main()
