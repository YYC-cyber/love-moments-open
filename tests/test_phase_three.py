from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from love_moments.intelligent_ui import ConflictError
from love_moments.schema import SCHEMA_VERSION
from love_moments.store import DEMO_PARTNER, DEMO_USER, LoveMomentsStore
from tests import VALID_PNG


class PhaseThreeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.original_amap_key = os.environ.get("AMAP_WEB_KEY")
        self.original_geoapify_enabled = os.environ.get("GEOAPIFY_ENABLED")
        self.original_serpapi_enabled = os.environ.get("SERPAPI_ENABLED")
        os.environ["AMAP_WEB_KEY"] = ""
        os.environ["GEOAPIFY_ENABLED"] = "false"
        os.environ["SERPAPI_ENABLED"] = "false"
        self.store = LoveMomentsStore(Path(self.temp.name) / "phase-three.db", seed_demo_data=True)

    def tearDown(self) -> None:
        if self.original_amap_key is None:
            os.environ.pop("AMAP_WEB_KEY", None)
        else:
            os.environ["AMAP_WEB_KEY"] = self.original_amap_key
        if self.original_geoapify_enabled is None:
            os.environ.pop("GEOAPIFY_ENABLED", None)
        else:
            os.environ["GEOAPIFY_ENABLED"] = self.original_geoapify_enabled
        if self.original_serpapi_enabled is None:
            os.environ.pop("SERPAPI_ENABLED", None)
        else:
            os.environ["SERPAPI_ENABLED"] = self.original_serpapi_enabled
        self.temp.cleanup()

    def test_schema_v17_columns_and_integrity(self) -> None:
        with self.store.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertIn("system_events", tables)
            self.assertIn("system_metrics", tables)
            versions = {row[0] for row in db.execute("SELECT version FROM schema_migrations")}
            self.assertIn(SCHEMA_VERSION, versions)
            self.assertIn("external_api_usage", tables)
            self.assertIn("trip_food_recommendations", {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()})
            self.assertIn("trip_food_review_evidence", tables)
            self.assertIn("place_search_candidates", tables)
            self.assertIn("trip_destination_places", tables)
            self.assertIn("itinerary_item_places", tables)
            self.assertIn("route_cache", tables)
            self.assertIn("storage_provider", {row[1] for row in db.execute("PRAGMA table_info(media_assets)")})
            self.assertTrue({"visibility", "expected_checksum", "chunk_size", "thumbnail_temp_path", "media_id", "updated_at"}.issubset({row[1] for row in db.execute("PRAGMA table_info(upload_sessions)")}))
            self.assertIn("device_name", {row[1] for row in db.execute("PRAGMA table_info(sessions)")})
            ingredient_columns = {row[1] for row in db.execute("PRAGMA table_info(recipe_ingredients)")}
            self.assertTrue({"preparation", "ingredient_group"}.issubset(ingredient_columns))
            step_columns = {row[1] for row in db.execute("PRAGMA table_info(recipe_steps)")}
            self.assertTrue({"title", "heat_level", "key_point"}.issubset(step_columns))
            self.assertIn("details_json", {row[1] for row in db.execute("PRAGMA table_info(itinerary_items)")})
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_arbitrary_trip_route_and_mock_quotes(self) -> None:
        unresolved = self.store.create_trip({"destination": "成都", "start_date": "2026-11-01", "end_date": "2026-11-04", "budget": 5200, "preferences": {"pace": "松弛", "interests": "美食"}}, DEMO_USER)
        with self.assertRaisesRegex(ValueError, "确认具体目的地"):
            self.store.trip_routes(unresolved["id"], {}, DEMO_USER)
        with self.assertRaisesRegex(ValueError, "确认具体目的地"):
            self.store.search_quotes({"trip_id": unresolved["id"], "type": "hotel"}, DEMO_USER)
        candidate = self.store.search_destination_places(
            {"query": "成都", "region_hint": "成都市", "include_global": False}, DEMO_USER,
        )["items"][0]
        trip = self.store.create_trip({"destination": "成都", "destination_candidate_id": candidate["candidate_id"], "start_date": "2026-11-01", "end_date": "2026-11-04", "budget": 5200, "preferences": {"pace": "松弛", "interests": "美食"}}, DEMO_USER)
        self.assertEqual(trip["destination"], "成都")
        self.assertEqual(len(trip["variants"]), 5)
        route = self.store.trip_routes(trip["id"], {}, DEMO_USER)
        self.assertTrue(route["mock"])
        self.assertGreater(route["distance_meters"], 0)
        with patch.dict("os.environ", {"SERPAPI_ENABLED": "false", "JUHE_TRAIN_ENABLED": "false", "BOOKING_API_URL": "", "BOOKING_API_KEY": ""}):
            quotes = self.store.search_quotes({"trip_id": trip["id"], "type": "hotel"}, DEMO_USER)
        self.assertTrue(quotes["mock"])
        self.assertEqual(len(quotes["items"]), 5)
        self.assertIn("成都市", quotes["search_scope"])
        manual = self.store.create_manual_quote({"trip_id": trip["id"], "title": "收藏的民宿", "amount": 499}, DEMO_PARTNER)
        self.assertEqual(manual["amount_cents"], 49900)
        selected = self.store.select_quote(manual["id"], DEMO_USER)
        self.assertIsNotNone(selected["selected_at"])
        partner_trip = self.store.get_trip(trip["id"], user_id=DEMO_PARTNER)
        self.assertEqual(next(item for item in partner_trip["quotes"] if item["selected_at"])["id"], manual["id"])

    def test_serpapi_quotes_use_database_quota_when_legacy_counter_is_unwritable(self) -> None:
        """Production is read-only, so store-backed requests must not write the legacy JSON counter."""
        candidate = self.store.search_destination_places(
            {"query": "千岛湖风景区", "region_hint": "杭州市", "include_global": False}, DEMO_USER,
        )["items"][0]
        trip = self.store.create_trip({
            "destination": "千岛湖风景区", "destination_candidate_id": candidate["candidate_id"],
            "start_date": "2026-11-15", "end_date": "2026-11-18", "budget": 6000,
        }, DEMO_USER)
        blocked_parent = Path(self.temp.name) / "not-a-directory"
        blocked_parent.write_text("read-only deployment sentinel", encoding="utf-8")
        legacy_usage_path = blocked_parent / "serpapi-usage.json"

        class JsonResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return json.dumps({"properties": [{
                    "name": "千岛湖湖景酒店",
                    "description": "靠近已确认目的地",
                    "overall_rating": 4.7,
                    "reviews": 128,
                    "rate_per_night": {"extracted_lowest": 568},
                    "prices": [{"source": "测试预订平台"}],
                }]}, ensure_ascii=False).encode("utf-8")

        env = {
            "SERPAPI_ENABLED": "true",
            "SERPAPI_API_KEY": "test-key",
            "SERPAPI_MONTHLY_LIMIT": "230",
            "SERPAPI_MONTHLY_REQUEST_LIMIT": "230",
            "SERPAPI_USAGE_PATH": str(legacy_usage_path),
            "BOOKING_API_URL": "",
            "BOOKING_API_KEY": "",
        }
        class NoopDestinationResolver:
            @staticmethod
            def resolve_hotel_destination(_place: dict, _requested: str) -> str:
                return ""

        # This regression test isolates the SerpApi request counter. Destination
        # resolution has its own tests and would otherwise add a second HTTP call.
        self.store.travel_assistant = NoopDestinationResolver()
        used_before = self.store.external_api_quota("serpapi")["used"]
        with patch.dict("os.environ", env, clear=False), patch(
            "love_moments.quotes.request.urlopen", return_value=JsonResponse(),
        ) as opened:
            result = self.store.search_quotes({"trip_id": trip["id"], "type": "hotel"}, DEMO_USER)

        self.assertEqual(opened.call_count, 1)
        self.assertFalse(result["mock"])
        self.assertEqual(result["items"][0]["title"], "千岛湖湖景酒店")
        self.assertEqual(self.store.external_api_quota("serpapi")["used"], used_before + 1)
        self.assertFalse(legacy_usage_path.exists())

    def test_serpapi_hotel_search_uses_ai_resolved_international_place_name(self) -> None:
        candidate = self.store.search_destination_places(
            {"query": "千岛湖风景区", "region_hint": "杭州市", "include_global": False}, DEMO_USER,
        )["items"][0]
        trip = self.store.create_trip({
            "destination": "千岛湖风景区", "destination_candidate_id": candidate["candidate_id"],
            "start_date": "2026-11-06", "end_date": "2026-11-08", "budget": 6000,
        }, DEMO_USER)
        # The offline/manual fixture has no authoritative country. Simulate the
        # country returned by the real AMap candidate so this test exercises
        # domestic booking routing without guessing from Chinese hotel text.
        with self.store.connection() as db:
            db.execute("UPDATE trip_destination_places SET country='中国' WHERE trip_id=?", (trip["id"],))

        class TranslatingAssistant:
            name = "ai"

            @staticmethod
            def resolve_hotel_destination(place: dict, requested: str) -> str:
                self.assertEqual(place["name"], "千岛湖风景区")
                self.assertIn("千岛湖风景区", requested)
                return "Qiandao Lake, Hangzhou"

            @staticmethod
            def localize_hotel_names(names: list[str], place: dict) -> dict[str, str]:
                self.assertEqual(place["country"], "中国")
                self.assertEqual(names, ["Phoenix Residence Qiandao Lake", "Tianyu No.1 经典美式山景湖景别墅"])
                return {
                    "Phoenix Residence Qiandao Lake": "千岛湖凤凰公寓",
                    "Tianyu No.1 经典美式山景湖景别墅": "千岛湖天屿1号经典美式山景湖景别墅",
                }

        class JsonResponse:
            def __init__(self, payload: dict):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")

        requested_queries: list[str] = []

        def open_request(req, **_kwargs):
            query = parse_qs(urlsplit(req.full_url).query).get("q", [""])[0]
            requested_queries.append(query)
            if query != "Qiandao Lake, Hangzhou":
                return JsonResponse({"error": "Google Hotels hasn't returned any results for this query."})
            return JsonResponse({"properties": [
                {
                    "name": "Phoenix Residence Qiandao Lake",
                    "rate_per_night": {"extracted_lowest": 217},
                    "prices": [{"source": "Bluepillow", "link": "https://www.bluepillow.com/hotel/foreign"}],
                    "link": "https://www.bluepillow.com/hotel/foreign",
                },
                {
                    "name": "Tianyu No.1 经典美式山景湖景别墅",
                    "rate_per_night": {"extracted_lowest": 7597},
                    "prices": [{"source": "Bluepillow", "link": "https://www.bluepillow.com/hotel/foreign-2"}],
                    "link": "https://www.bluepillow.com/hotel/foreign-2",
                },
            ]})

        self.store.travel_assistant = TranslatingAssistant()
        env = {
            "SERPAPI_ENABLED": "true", "SERPAPI_API_KEY": "test-key",
            "SERPAPI_MONTHLY_REQUEST_LIMIT": "230", "BOOKING_API_URL": "", "BOOKING_API_KEY": "",
        }
        with patch.dict("os.environ", env, clear=False), patch(
            "love_moments.quotes.request.urlopen", side_effect=open_request,
        ):
            result = self.store.search_quotes({"trip_id": trip["id"], "type": "hotel"}, DEMO_USER)

        self.assertEqual(requested_queries, ["Qiandao Lake, Hangzhou"])
        self.assertEqual(result["items"][0]["title"], "千岛湖凤凰公寓")
        self.assertEqual(result["items"][0]["details"]["original_title"], "Phoenix Residence Qiandao Lake")
        self.assertEqual(result["items"][0]["booking_url"], "https://hotels.ctrip.com/list")
        self.assertEqual(result["items"][0]["details"]["booking_copy_text"], "千岛湖凤凰公寓")
        self.assertEqual(result["items"][1]["title"], "千岛湖天屿1号经典美式山景湖景别墅")
        self.assertEqual(result["items"][1]["details"]["original_title"], "Tianyu No.1 经典美式山景湖景别墅")
        self.assertIn("千岛湖风景区", result["search_scope"])

    def test_route_legs_use_saved_coordinates_cache_and_only_invalidate_neighbors(self) -> None:
        candidate = self.store.search_destination_places(
            {"query": "杭州", "region_hint": "杭州市", "include_global": False}, DEMO_USER,
        )["items"][0]
        trip = self.store.create_trip({
            "destination": "杭州", "destination_candidate_id": candidate["candidate_id"],
            "start_date": "2026-12-01", "end_date": "2026-12-03", "budget": 5000,
        }, DEMO_USER)
        active = next(variant for variant in trip["variants"] if variant["style"] == trip["active_variant"])
        items = [day["items"] for day in active["days"] if len(day["items"]) >= 2]
        self.assertGreaterEqual(len(items), 2)
        now = "2024-02-14T00:00:00+00:00"
        with self.store.connection() as db:
            for day_index, day_items in enumerate(items[:2]):
                for item_index, item in enumerate(day_items[:2]):
                    latitude = 30.20 + day_index * .02 + item_index * .004
                    longitude = 120.10 + day_index * .02 + item_index * .004
                    db.execute(
                        "INSERT INTO itinerary_item_places(itinerary_item_id,trip_id,provider,provider_place_id,name,formatted_address,country,province,city,district,latitude,longitude,category,confirmed_by,confirmed_at) VALUES (?,?, 'amap',?,?,?,'中国','浙江省','杭州市','西湖区',?,?, '风景名胜',?,?)",
                        (item["id"], trip["id"], f"poi-{day_index}-{item_index}", item["title"], item["title"], latitude, longitude, DEMO_USER, now),
                    )

        listed = self.store.trip_route_legs(trip["id"], DEMO_USER)
        self.assertGreaterEqual(len(listed["legs"]), 2)
        self.assertTrue(all(leg["resolved"] for leg in listed["legs"][:2]))
        self.assertEqual(listed["legs"][0]["cached_routes"], {})
        first, second = listed["legs"][:2]
        queried = self.store.query_trip_route_leg(trip["id"], first["id"], {}, DEMO_USER)
        self.assertEqual(set(queried["routes"]), {"walking", "transit", "driving"})
        again = self.store.query_trip_route_leg(trip["id"], first["id"], {}, DEMO_USER)
        self.assertEqual(again["cache_hits"], 3)
        self.store.query_trip_route_leg(trip["id"], second["id"], {}, DEMO_USER)
        cycled = self.store.query_trip_route_leg(trip["id"], second["id"], {"modes": ["bicycling"]}, DEMO_USER)
        self.assertEqual(set(cycled["routes"]), {"bicycling"})

        revised = self.store.revise_item(first["origin_item"]["id"], {"title": "新的真实地点待确认"}, user_id=DEMO_USER)
        refreshed = self.store.trip_route_legs(trip["id"], DEMO_USER)
        affected = next(leg for leg in refreshed["legs"] if leg["day_id"] == first["day_id"])
        unaffected = next(leg for leg in refreshed["legs"] if leg["day_id"] == second["day_id"])
        self.assertFalse(affected["resolved"])
        self.assertEqual(set(unaffected["cached_routes"]), {"walking", "transit", "driving", "bicycling"})
        self.assertGreater(revised["revision"], trip["current_revision"])

    def test_route_distance_thresholds_are_local_and_deterministic(self) -> None:
        self.assertEqual(self.store._route_recommendations(1_500), ["walking", "bicycling"])
        self.assertEqual(self.store._route_recommendations(1_501), ["transit", "taxi"])
        self.assertEqual(self.store._route_recommendations(8_000), ["transit", "taxi"])
        self.assertEqual(self.store._route_recommendations(8_001), ["transit", "driving"])

    def test_trip_generation_job_is_durable_and_idempotent(self) -> None:
        payload = {
            "destination": "苏州", "start_date": "2026-12-20", "end_date": "2026-12-22",
            "budget": 4200, "preferences": {"pace": "松弛"},
        }
        first = self.store.create_trip_generation_job(payload, DEMO_USER, "trip-request-1234567890")
        duplicate = self.store.create_trip_generation_job(payload, DEMO_USER, "trip-request-1234567890")
        self.assertEqual(first["id"], duplicate["id"])
        claimed = self.store.claim_trip_generation_job()
        self.assertEqual(claimed["id"], first["id"])
        completed = self.store.run_trip_generation_job(claimed)
        self.assertEqual(completed["status"], "completed")
        result = self.store.get_trip_generation_job(first["id"], DEMO_USER)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["trip"]["variants"]), 5)
        # Re-running an interrupted completed job must return the same trip.
        rerun = self.store.run_trip_generation_job(claimed)
        self.assertEqual(rerun["trip_id"], completed["trip_id"])
        self.assertEqual(len([item for item in self.store.list_trips(DEMO_USER) if item["id"] == completed["trip_id"]]), 1)
        with self.assertRaises(ConflictError):
            self.store.create_trip_generation_job({**payload, "destination": "无锡"}, DEMO_USER, "trip-request-1234567890")

    def test_selected_hotel_coordinates_are_kept_and_added_to_daily_routes(self) -> None:
        trip = self.store.create_trip({
            "destination": "杭州", "start_date": "2026-12-01", "end_date": "2026-12-02", "budget": 5000,
        }, DEMO_USER)
        active = next(item for item in trip["variants"] if item["style"] == trip["active_variant"])
        itinerary_items = [item for day in active["days"] for item in day["items"]]
        now = "2024-02-14T00:00:00+00:00"
        with self.store.connection() as db:
            for index, item in enumerate(itinerary_items):
                db.execute(
                    "INSERT INTO itinerary_item_places(itinerary_item_id,trip_id,provider,provider_place_id,name,formatted_address,country,province,city,district,latitude,longitude,category,confirmed_by,confirmed_at) VALUES (?,?, 'amap',?,?,?,'中国','浙江省','杭州市','西湖区',?,?, '风景名胜',?,?)",
                    (item["id"], trip["id"], f"poi-{index}", item["title"], item["title"], 30.20 + index * .002, 120.10 + index * .002, DEMO_USER, now),
                )
            quote_id = "quote-hotel-route"
            db.execute(
                "INSERT INTO quote_snapshots(id,couple_id,trip_id,quote_type,provider,source_mode,title,subtitle,amount_cents,currency,terms,includes_tax,booking_url,details_json,fetched_at,created_by) VALUES (?,?,?,?,?,?,?,?,?,'CNY','',1,'',?,?,?)",
                (quote_id, "demo-couple", trip["id"], "hotel", "test", "aggregate", "湖边酒店", "西湖区", 68800, json.dumps({"latitude": 30.25, "longitude": 120.15}), now, DEMO_USER),
            )
        selected = self.store.select_quote(quote_id, DEMO_USER, trip["version"])
        self.assertEqual(selected["details"]["latitude"], 30.25)
        refreshed = self.store.get_trip(trip["id"], DEMO_USER)
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM trips WHERE id=?", (trip["id"],)).fetchone()
            legs = self.store._build_route_legs(db, row)
        self.assertTrue(any(leg["origin_item"]["type"] == "hotel" for leg in legs))
        self.assertTrue(any(leg["destination_item"]["type"] == "hotel" for leg in legs))
        self.assertEqual(refreshed["quotes"][0]["details"]["longitude"], 120.15)

    def test_destination_candidate_confirmation_prevents_same_name_guessing(self) -> None:
        result = self.store.search_destination_places(
            {"query": "迪士尼", "region_hint": "上海", "include_global": False}, DEMO_USER,
        )
        self.assertEqual(len(result["items"]), 1)
        candidate = result["items"][0]
        self.assertEqual(candidate["provider"], "manual")
        trip = self.store.create_trip({
            "destination": "迪士尼", "destination_candidate_id": candidate["candidate_id"],
            "departure_city": "杭州", "arrival_city": "上海",
            "start_date": "2026-12-20", "end_date": "2026-12-22", "budget": 5000,
        }, DEMO_USER)
        self.assertEqual(trip["destination"], "迪士尼")
        self.assertFalse(trip["needs_destination_detail"])
        self.assertEqual(trip["destination_place"]["city"], "上海")
        self.assertEqual(trip["destination_place"]["confirmed_by"], DEMO_USER)

    def test_trip_delete_is_shared_version_checked_and_keeps_audit(self) -> None:
        trip = self.store.create_trip({
            "destination": "三亚", "departure_city": "杭州", "arrival_city": "三亚",
            "start_date": "2026-12-03", "end_date": "2026-12-05", "budget": 5000,
            "travelers": 2, "rooms": 1, "hotel_budget": 1800,
        }, DEMO_USER)
        with self.assertRaises(ConflictError):
            self.store.delete_trip(trip["id"], trip["version"] - 1, DEMO_PARTNER)
        deleted = self.store.delete_trip(trip["id"], trip["version"], DEMO_PARTNER)
        self.assertTrue(deleted["deleted"])
        self.assertNotIn(trip["id"], {item["id"] for item in self.store.list_trips(DEMO_USER)})
        with self.assertRaises(KeyError):
            self.store.get_trip(trip["id"], DEMO_USER)
        with self.store.connection() as db:
            self.assertIsNotNone(db.execute("SELECT deleted_at FROM trips WHERE id=?", (trip["id"],)).fetchone()["deleted_at"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_events WHERE action='trip.deleted' AND target_id=?", (trip["id"],)).fetchone()[0], 1)
        with self.assertRaises(ConflictError):
            self.store.restore_trip(trip["id"], deleted["version"] - 1, DEMO_USER)
        restored = self.store.restore_trip(trip["id"], deleted["version"], DEMO_USER)
        self.assertEqual(restored["id"], trip["id"])
        self.assertIn(trip["id"], {item["id"] for item in self.store.list_trips(DEMO_PARTNER)})
        with self.store.connection() as db:
            self.assertIsNone(db.execute("SELECT deleted_at FROM trips WHERE id=?", (trip["id"],)).fetchone()["deleted_at"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_events WHERE action='trip.restored' AND target_id=?", (trip["id"],)).fetchone()[0], 1)

    def test_trip_conflicts_current_ui_context_and_real_reactions(self) -> None:
        first = self.store.create_trip({"destination": "苏州", "start_date": "2026-12-10", "end_date": "2026-12-12", "budget": 3000}, DEMO_USER)
        second = self.store.create_trip({"destination": "无锡", "start_date": "2026-12-11", "end_date": "2026-12-13", "budget": 3200}, DEMO_USER)
        self.assertIn(first["id"], {item["id"] for item in second["date_conflicts"]})
        manifest = self.store.get_ui_manifest("trip", DEMO_PARTNER, current_trip_id=first["id"])
        trip_nodes = [node for node in manifest["sections"] if node.get("resourceRef")]
        self.assertTrue(trip_nodes)
        self.assertEqual({node["resourceRef"] for node in trip_nodes}, {first["id"]})
        moment = self.store.create_moment({"title": "一起散步", "visibility": "shared"}, DEMO_USER)
        self.store.set_moment_reaction(moment["id"], "heart", DEMO_PARTNER)
        shared = next(item for item in self.store.list_moments(user_id=DEMO_USER) if item["id"] == moment["id"])
        self.assertEqual(shared["reactions"][0]["user_id"], DEMO_PARTNER)
        self.store.set_moment_reaction(moment["id"], "none", DEMO_PARTNER)
        self.assertEqual(next(item for item in self.store.list_moments(user_id=DEMO_USER) if item["id"] == moment["id"])["reactions"], [])

    def test_legacy_fabricated_places_are_repaired_without_touching_locked_items(self) -> None:
        trip = self.store.create_trip({"destination": "望仙谷", "start_date": "2026-11-01", "end_date": "2026-11-02", "budget": 3000}, DEMO_USER)
        nature = next(item for item in trip["variants"] if item["style"] == "nature")
        editable_id = nature["days"][0]["items"][0]["id"]
        locked_id = nature["days"][0]["items"][1]["id"]
        with self.store.connection() as db:
            db.execute("UPDATE itinerary_items SET title='城市自然公园' WHERE id=?", (editable_id,))
            db.execute("UPDATE itinerary_items SET title='湖畔慢行',locked=1 WHERE id=?", (locked_id,))
        self.store._repair_legacy_travel_templates()
        with self.store.connection() as db:
            editable = db.execute("SELECT title,note FROM itinerary_items WHERE id=?", (editable_id,)).fetchone()
            locked = db.execute("SELECT title FROM itinerary_items WHERE id=?", (locked_id,)).fetchone()
        self.assertEqual(editable["title"], "望仙谷 · 附近自然地点待确认")
        self.assertIn("未经核实", editable["note"])
        self.assertEqual(locked["title"], "湖畔慢行")

    def test_real_food_recommendations_are_saved_selectable_and_shared(self) -> None:
        class FakeFoodMap:
            name = "amap"
            is_mock = False

            def search_food(self, keyword, city="", limit=5, near=""):
                del limit, near
                slug = str(abs(hash((keyword, city))))
                return [{
                    "amap_poi_id": f"poi-{slug}", "name": f"{city}真实老店", "address": "中心区 1 号",
                    "district": "中心区", "telephone": "0871-1234567", "category": "餐饮服务",
                    "rating": 4.7, "average_cost_cents": 5800, "opening_hours": "08:00-21:00",
                    "latitude": 25.0, "longitude": 102.0,
                    "navigation_url": "https://uri.amap.com/marker?position=102,25", "source": "amap", "mock": False,
                }]

        trip = self.store.create_trip({"destination": "云南", "start_date": "2026-11-13", "end_date": "2026-11-15", "budget": 4000}, DEMO_USER)
        self.store.select_variant(trip["id"], "food", DEMO_USER)
        generated_food = next(item for item in trip["variants"] if item["style"] == "food")
        self.assertTrue(all({_meal["start_time"] for _meal in day["items"]} == {"08:00", "12:00", "18:30"} for day in generated_food["days"]))
        with patch("love_moments.phase_three.map_provider_from_env", return_value=FakeFoodMap()):
            result = self.store.refresh_food_recommendations(
                trip["id"], DEMO_USER, day_areas={"1": "昆明老街", "2": "大理古城", "3": "丽江古城"},
            )
        self.assertTrue(result["configured"])
        self.assertGreaterEqual(result["count"], 4)
        food = next(item for item in result["trip"]["variants"] if item["style"] == "food")
        all_items = [item for day in food["days"] for item in day["items"]]
        self.assertTrue(all(" · " in item["title"] for item in all_items))
        recommendation = all_items[0]["food_recommendations"][0]
        selected = self.store.select_food_recommendation(trip["id"], recommendation["id"], DEMO_USER)
        partner = self.store.get_trip(trip["id"], DEMO_PARTNER)
        partner_food = next(item for item in partner["variants"] if item["style"] == "food")
        partner_rows = [shop for day in partner_food["days"] for item in day["items"] for shop in item["food_recommendations"]]
        self.assertTrue(any(shop["id"] == recommendation["id"] and shop["selected_at"] for shop in partner_rows))
        self.assertEqual(selected["status"], "pending_approval")

    def test_small_scenic_destination_uses_nearby_three_meals_each_day(self) -> None:
        calls = []

        class ScenicFoodMap:
            name = "amap"
            is_mock = False

            def search_food(self, keyword, city="", limit=5, near=""):
                calls.append((keyword, city, limit, near))
                return [{
                    "amap_poi_id": f"poi-{len(calls)}", "name": f"望仙谷附近{keyword}", "address": "望仙乡景区路",
                    "district": "广信区", "telephone": "", "category": "餐饮服务", "rating": 4.5,
                    "average_cost_cents": 3800, "opening_hours": "07:00-21:00", "latitude": 28.6,
                    "longitude": 118.4, "distance_meters": 860,
                    "navigation_url": "https://uri.amap.com/marker?position=118.4,28.6", "source": "amap", "mock": False,
                }]

        trip = self.store.create_trip({"destination": "望仙谷", "start_date": "2026-11-13", "end_date": "2026-11-14", "budget": 3000}, DEMO_USER)
        with patch("love_moments.phase_three.map_provider_from_env", return_value=ScenicFoodMap()):
            self.store.refresh_food_recommendations(trip["id"], DEMO_USER, day_areas={"1": "望仙谷"}, day_number=1)
            result = self.store.refresh_food_recommendations(trip["id"], DEMO_USER, day_areas={"2": "上饶站"}, day_number=2)
        food = next(item for item in result["trip"]["variants"] if item["style"] == "food")
        for day in food["days"]:
            self.assertEqual({_meal["start_time"] for _meal in day["items"]}, {"08:00", "12:00", "18:30"})
            self.assertTrue(all(_meal["food_recommendations"] for _meal in day["items"]))
        self.assertTrue(calls)
        self.assertTrue(all(city == "上饶市" for _, city, _, _ in calls))
        self.assertEqual({near for _, _, _, near in calls}, {"望仙谷", "上饶站"})
        self.assertTrue(all(shop["distance_meters"] <= 5000 for day in food["days"] for meal in day["items"] for shop in meal["food_recommendations"]))

    def test_any_plan_meal_can_query_nearby_high_rated_restaurants(self) -> None:
        calls = []

        class RankedFoodMap:
            name = "amap"
            is_mock = False

            def search_food(self, keyword, city="", limit=5, near=""):
                calls.append((keyword, city, limit, near))
                return [
                    {"amap_poi_id": "near-low", "name": "较近普通餐厅", "address": "1号", "district": "景区", "telephone": "", "category": "餐饮", "rating": 4.1, "average_cost_cents": 3000, "opening_hours": "10:00-20:00", "latitude": 31.1, "longitude": 121.1, "distance_meters": 300, "navigation_url": "https://uri.amap.com/marker?position=121.1,31.1", "source": "amap"},
                    {"amap_poi_id": "rated", "name": "高分餐厅", "address": "2号", "district": "景区", "telephone": "", "category": "餐饮", "rating": 4.9, "average_cost_cents": 6000, "opening_hours": "10:00-21:00", "latitude": 31.2, "longitude": 121.2, "distance_meters": 1200, "navigation_url": "https://uri.amap.com/marker?position=121.2,31.2", "source": "amap"},
                    {"amap_poi_id": "too-far", "name": "远处餐厅", "address": "3号", "district": "远郊", "telephone": "", "category": "餐饮", "rating": 5.0, "average_cost_cents": 9000, "opening_hours": "10:00-21:00", "latitude": 32.0, "longitude": 122.0, "distance_meters": 8000, "navigation_url": "https://uri.amap.com/marker?position=122,32", "source": "amap"},
                ]

        class PublicReviewSearch:
            is_configured = True

            def search(self, area, restaurant_names, limit=8):
                self.query = (area, restaurant_names, limit)
                return [{
                    "platform": "大众点评", "title": "高分餐厅 - 大众点评",
                    "snippet": "游客公开评价摘要", "url": "https://www.dianping.com/shop/123",
                    "source_query": "迪士尼小镇 高分餐厅",
                }]

        trip = self.store.create_trip({"destination": "上海迪士尼", "start_date": "2026-11-13", "end_date": "2026-11-15", "budget": 5000}, DEMO_USER)
        slow = next(variant for variant in trip["variants"] if variant["style"] == "slow")
        item = slow["days"][0]["items"][0]
        with self.store.connection() as db:
            db.execute("UPDATE itinerary_items SET type='meal', title='迪士尼小镇午餐' WHERE id=?", (item["id"],))
        with patch.dict("os.environ", {"SERPAPI_ENABLED": "true", "SERPAPI_API_KEY": "test-key"}), \
             patch("love_moments.phase_three.map_provider_from_env", return_value=RankedFoodMap()), \
             patch("love_moments.phase_three.restaurant_review_search_from_env", return_value=PublicReviewSearch()):
            result = self.store.refresh_food_recommendations(trip["id"], DEMO_USER, search_area="迪士尼小镇", item_id=item["id"])
        refreshed = next(variant for variant in result["trip"]["variants"] if variant["style"] == "slow")["days"][0]["items"][0]
        self.assertEqual(refreshed["title"], "迪士尼小镇午餐")
        self.assertEqual([row["name"] for row in refreshed["food_recommendations"]], ["高分餐厅", "较近普通餐厅"])
        self.assertEqual(refreshed["review_evidence"][0]["platform"], "大众点评")
        self.assertEqual(result["review_count"], 1)
        self.assertEqual(calls[0][3], "迪士尼小镇")
        self.assertEqual(result["trip"]["status"], "pending_approval")

    def test_trip_boundaries(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create_trip({"destination": "", "start_date": "2026-11-01", "end_date": "2026-11-02", "budget": 1}, DEMO_USER)
        with self.assertRaises(ValueError):
            self.store.create_trip({"destination": "成都", "start_date": "2026-11-03", "end_date": "2026-11-02", "budget": 1}, DEMO_USER)
        with self.assertRaises(ValueError):
            self.store.trip_routes("demo-trip", {"mode": "teleport"}, DEMO_USER)

    def test_ocr_stays_draft_until_confirmed_and_is_editable(self) -> None:
        before = len(self.store.list_messages(user_id=DEMO_USER))
        draft = self.store.create_chat_import({"media_id": None, "text": "12:30 我：到家啦\n12:31 对方：路上小心"}, DEMO_USER)
        self.assertFalse(draft["mock"])
        self.assertEqual(draft["provider"], "manual")
        self.assertEqual(draft["source_label"], "粘贴文本")
        self.assertEqual(len(self.store.list_messages(user_id=DEMO_USER)), before)
        first = draft["items"][0]
        updated = self.store.update_chat_import_item(draft["id"], first["id"], {"content": "已经安全到家", "speaker": "me", "selected": True, "expected_version": first["version"]}, DEMO_USER)
        with self.assertRaises(ConflictError):
            self.store.update_chat_import_item(draft["id"], first["id"], {"content": "过期修改", "expected_version": first["version"]}, DEMO_USER)
        result = self.store.finish_chat_import(draft["id"], {}, DEMO_USER)
        self.assertEqual(result["count"], 2)
        self.assertEqual(len(self.store.list_messages(user_id=DEMO_USER)), before + 2)
        self.assertEqual(self.store.finish_chat_import(draft["id"], {}, DEMO_USER)["idempotent"], True)
        self.assertEqual(updated["status"], "draft")

    def test_message_collection_and_trash_restore_flow(self) -> None:
        message = self.store.create_message({"type": "text", "content": "把这一句留下来"}, DEMO_USER)
        moment = self.store.message_to_moment(message["id"], {"visibility": "shared"}, DEMO_PARTNER)
        repeated = self.store.message_to_moment(message["id"], {"visibility": "shared"}, DEMO_PARTNER)
        self.assertEqual(moment["id"], repeated["id"])
        self.store.trash_moment(moment["id"], DEMO_PARTNER)
        trash = self.store.list_moment_trash(DEMO_USER)
        row = next(item for item in trash if item["id"] == moment["id"])
        with self.assertRaises(ConflictError):
            self.store.restore_moment(moment["id"], moment["version"], DEMO_USER)
        restored = self.store.restore_moment(moment["id"], row["version"], DEMO_USER)
        self.assertIsNone(restored["deleted_at"])

    def test_private_trash_is_isolated_and_only_creator_can_purge(self) -> None:
        moment = self.store.create_moment({"title": "只属于我", "visibility": "private"}, DEMO_USER)
        self.store.trash_moment(moment["id"], DEMO_USER)
        self.assertNotIn(moment["id"], {item["id"] for item in self.store.list_moment_trash(DEMO_PARTNER)})
        with self.assertRaises(PermissionError):
            self.store.purge_moment(moment["id"], DEMO_PARTNER)
        self.assertTrue(self.store.purge_moment(moment["id"], DEMO_USER)["purged"])

    def test_trash_items_expire_after_thirty_days_for_the_creator(self) -> None:
        moment = self.store.create_moment({"title": "过期回忆", "visibility": "private"}, DEMO_USER)
        self.store.trash_moment(moment["id"], DEMO_USER)
        with self.store.connection() as db:
            db.execute("UPDATE moments SET deleted_at='2020-01-01T00:00:00+00:00' WHERE id=?", (moment["id"],))
        self.assertNotIn(moment["id"], {item["id"] for item in self.store.list_moment_trash(DEMO_USER)})
        with self.store.connection() as db:
            row = db.execute("SELECT purged_at,body FROM moments WHERE id=?", (moment["id"],)).fetchone()
        self.assertIsNotNone(row["purged_at"])
        self.assertEqual(row["body"], "")

    def test_calendar_todo_anniversary_and_conflict(self) -> None:
        todo = self.store.create_calendar_entry({"entry_type": "todo", "title": "一起买票", "starts_at": "2026-10-20", "all_day": True}, DEMO_USER)
        completed = self.store.complete_calendar_entry(todo["id"], True, todo["version"], DEMO_PARTNER)
        self.assertIsNotNone(completed["completed_at"])
        with self.assertRaises(ConflictError):
            self.store.update_calendar_entry(todo["id"], {"title": "旧版本", "expected_version": todo["version"]}, DEMO_USER)
        anniversary = self.store.create_calendar_entry({"entry_type": "anniversary", "title": "第一次见面", "starts_at": "2026-12-01", "all_day": True, "recurrence": "yearly"}, DEMO_USER)
        self.assertEqual(anniversary["recurrence"], "yearly")
        self.assertEqual(len(self.store.list_calendar_entries(DEMO_USER)), 2)

    def test_preferences_can_be_shared_updated_and_deleted(self) -> None:
        pref = self.store.create_preference({"subject_user_id": DEMO_PARTNER, "category": "口味", "label": "奶茶", "value": "三分糖"}, DEMO_USER)
        updated = self.store.update_preference(pref["id"], {"value": "无糖", "expected_version": pref["version"]}, DEMO_PARTNER)
        self.assertEqual(updated["value"], "无糖")
        with self.assertRaises(ConflictError):
            self.store.delete_preference(pref["id"], pref["version"], DEMO_USER)
        self.store.delete_preference(pref["id"], updated["version"], DEMO_USER)
        self.assertEqual(self.store.list_preferences(DEMO_USER), [])

    def test_month_album_footprints_and_report_do_not_leak_partner_private(self) -> None:
        private = self.store.create_moment({"title": "我的私密", "visibility": "private", "location": "秘密地点", "occurred_at": "2026-10-12T10:00:00+08:00"}, DEMO_USER)
        own_report = self.store.annual_report(2026, DEMO_USER)
        partner_report = self.store.annual_report(2026, DEMO_PARTNER)
        self.assertEqual(own_report["moments"], partner_report["moments"] + 1)
        own_places = self.store.timeline_footprints(DEMO_USER)
        partner_places = self.store.timeline_footprints(DEMO_PARTNER)
        self.assertIn("秘密地点", {item["name"] for item in own_places["items"]})
        self.assertNotIn("秘密地点", {item["name"] for item in partner_places["items"]})
        self.assertTrue(any(group["month"] == "2026-10" for group in self.store.timeline_months(DEMO_USER)))
        self.assertIn(private["id"], {item["id"] for item in self.store.list_moments(user_id=DEMO_USER)})

    def test_devices_revocation_and_complete_zip_export(self) -> None:
        token = self.store.create_session(DEMO_USER, "电脑 · Safari", "test-agent", "127.0.0.1")
        other = self.store.create_session(DEMO_USER, "手机 · Chrome", "test-agent-2", "10.0.0.2")
        devices = self.store.list_devices(DEMO_USER, token)
        current = next(item for item in devices if item["current"])
        target = next(item for item in devices if item["device_name"] == "手机 · Chrome")
        with self.assertRaises(ValueError):
            self.store.revoke_device(current["id"], DEMO_USER, token)
        self.store.revoke_device(target["id"], DEMO_USER, token)
        self.assertIsNone(self.store.user_for_session(other))
        png = VALID_PNG
        media = self.store.save_media(png, "memory.png", "image/png", user_id=DEMO_USER)
        with self.store.connection() as db:
            db.execute("INSERT INTO daily_answers(id,couple_id,question_date,question,user_id,answer,created_at) VALUES ('answer-private-export','demo-couple','2099-01-01','尚未解锁的问题',?,'对方尚未解锁的答案','2099-01-01T00:00:00+00:00')", (DEMO_PARTNER,))
        shared_wish = self.store.create_wishlist_item({"text": "导出里的共同讨论", "note": "测试"}, DEMO_USER)
        shared_comment = self.store.create_shared_comment("wishlist_item", shared_wish["id"], "双方都可见的评论", DEMO_PARTNER)
        hidden_moment = self.store.create_moment({"title": "先共享后私密", "visibility": "shared"}, DEMO_USER)
        self.store.create_shared_comment("moment", hidden_moment["id"], "不应再导出的旧评论", DEMO_PARTNER)
        self.store.update_moment_visibility(hidden_moment["id"], "private", user_id=DEMO_USER)
        archive_bytes, filename = self.store.account_export(DEMO_USER)
        self.assertTrue(filename.endswith(".zip"))
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            self.assertIn("data.json", archive.namelist())
            self.assertIn("manifest.json", archive.namelist())
            exported = json.loads(archive.read("data.json"))
            self.assertNotIn("password_hash", exported["user"])
            self.assertTrue(exported["devices"])
            self.assertNotIn("token_hash", exported["devices"][0])
            self.assertNotIn("对方尚未解锁的答案", {row["answer"] for row in exported["daily_answers"]})
            self.assertIn("date_plans", exported)
            self.assertIn("ui_manifests", exported)
            self.assertIn(shared_comment["id"], {row["id"] for row in exported["shared_comments"]})
            self.assertNotIn("不应再导出的旧评论", {row["body"] for row in exported["shared_comments"]})
            self.assertTrue(any(name.startswith(f"media/{media['id']}-") for name in archive.namelist()))

    def test_unlink_and_account_delete_require_password_and_confirmation(self) -> None:
        with self.assertRaises(ValueError):
            self.store.unlink_couple("test123", "不确认", DEMO_USER)
        result = self.store.unlink_couple("test123", "解除情侣", DEMO_USER)
        self.assertTrue(result["unlinked"])
        self.assertTrue(self.store.bootstrap(DEMO_PARTNER)["needs_pairing"])

        second = LoveMomentsStore(Path(self.temp.name) / "delete.db", seed_demo_data=True)
        token = second.create_session(DEMO_USER)
        with self.assertRaises(ValueError):
            second.delete_account("wrong", "注销账号", DEMO_USER)
        self.assertTrue(second.delete_account("test123", "注销账号", DEMO_USER)["deleted"])
        self.assertIsNone(second.user_for_session(token))
        restarted = LoveMomentsStore(Path(self.temp.name) / "delete.db", seed_demo_data=True)
        with self.assertRaises(ValueError):
            restarted.authenticate("test", "test123")


if __name__ == "__main__":
    unittest.main()
