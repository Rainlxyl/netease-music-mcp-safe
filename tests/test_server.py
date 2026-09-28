import importlib.util
import base64
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timezone
from http import HTTPStatus
from unittest import mock

from PIL import Image

import image_safety
from persistence import PersistentStore, utc_now


ROOT = Path(__file__).resolve().parents[1]


def load_server(
    read_only="true",
    token="a-secure-test-token-that-is-long",
    oauth=False,
    *,
    default_timezone="Asia/Shanghai",
    legacy_preview_env=None,
):
    env = {
        "MCP_READ_ONLY": read_only,
        "MCP_ACCESS_TOKEN": token,
        "NETEASE_COOKIE": "MUSIC_U=test; __csrf=test",
        "MCP_PUBLIC_URL": "https://music.example.test" if oauth else "",
        "MCP_OAUTH_PASSWORD": "a-different-oauth-password" if oauth else "",
        "MCP_STORAGE_PATH": "",
        "MCP_DEFAULT_TIMEZONE": default_timezone,
    }
    with mock.patch.dict(os.environ, env, clear=False):
        for name in (
            "MCP_WRITE_PREVIEW_POLICY",
            "MCP_REQUIRE_WRITE_PREVIEW",
            "MCP_PREVIEW_TTL_SECONDS",
            "MCP_MAX_PENDING_PREVIEWS",
        ):
            os.environ.pop(name, None)
        if legacy_preview_env:
            os.environ.update(legacy_preview_env)
        spec = importlib.util.spec_from_file_location("server_under_test", ROOT / "server.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class ToolTests(unittest.TestCase):
    def recent_play_event(self, default_timezone, played_at_utc):
        module = load_server("true", default_timezone=default_timezone)
        play_time_ms = int(played_at_utc.timestamp() * 1000)
        response = {
            "code": 200,
            "data": {
                "total": 1,
                "list": [
                    {
                        "resourceId": "1",
                        "playTime": play_time_ms,
                        "data": {"id": 1, "name": "Timed", "ar": []},
                    }
                ],
            },
        }
        with mock.patch.object(module, "netease_request", return_value=response):
            payload = json.loads(module.get_recent_plays(1))
        return payload, payload["events"][0]

    def test_read_only_lists_only_read_tools(self):
        module = load_server("true")
        names = {tool["name"] for tool in module.available_tools()}
        self.assertEqual(names, module.READ_TOOL_NAMES | module.SESSION_TOOL_NAMES)
        self.assertTrue(names.isdisjoint(module.WRITE_TOOL_NAMES))

    def test_write_mode_lists_all_tools(self):
        module = load_server("false")
        names = {tool["name"] for tool in module.available_tools()}
        self.assertEqual(
            names,
            module.READ_TOOL_NAMES | module.SESSION_TOOL_NAMES | module.WRITE_TOOL_NAMES,
        )

    def test_every_listed_tool_has_a_call_dispatch_path(self):
        module = load_server("false")
        read_handlers = {
            "search_song": mock.DEFAULT,
            "list_my_playlists": mock.DEFAULT,
            "get_playlist_songs": mock.DEFAULT,
            "get_song_details": mock.DEFAULT,
            "get_play_history": mock.DEFAULT,
            "get_recent_plays": mock.DEFAULT,
            "list_my_subscribed_podcasts": mock.DEFAULT,
            "get_podcast_programs": mock.DEFAULT,
            "get_podcast_program": mock.DEFAULT,
            "get_podcast_program_details": mock.DEFAULT,
            "search_podcasts": mock.DEFAULT,
            "search_podcast_programs": mock.DEFAULT,
            "get_recent_podcast_plays": mock.DEFAULT,
            "daily_recommend": mock.DEFAULT,
            "get_operation_log": mock.DEFAULT,
            "list_interaction_notes": mock.DEFAULT,
            "get_netease_login_status": mock.DEFAULT,
            "start_netease_qr_login": mock.DEFAULT,
            "check_netease_qr_login": mock.DEFAULT,
            "logout_netease": mock.DEFAULT,
        }
        with mock.patch.multiple(module, **read_handlers) as handlers, mock.patch.object(
            module, "_execute_direct_write", return_value="dispatched"
        ):
            for handler in handlers.values():
                handler.return_value = "dispatched"
            for tool in module.available_tools():
                arguments = {"song_id": 1} if tool["name"] == "get_song_details" else {}
                with self.subTest(tool=tool["name"]):
                    self.assertEqual(module.call_tool(tool["name"], arguments), "dispatched")

    def test_tool_annotations_distinguish_reads_and_writes(self):
        module = load_server("false")
        tools = {tool["name"]: tool for tool in module.available_tools()}
        for name in module.READ_TOOL_NAMES:
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"])
        for name in module.WRITE_TOOL_NAMES:
            self.assertFalse(tools[name]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["remove_from_playlist"]["annotations"]["destructiveHint"])
        self.assertTrue(tools["like_song"]["annotations"]["destructiveHint"])
        self.assertFalse(tools["update_playlist"]["annotations"]["destructiveHint"])
        self.assertEqual(tools["update_playlist"]["inputSchema"]["minProperties"], 2)
        self.assertTrue(tools["reorder_playlist_tracks"]["annotations"]["destructiveHint"])
        curated_schema = tools["create_curated_playlist"]["inputSchema"]
        self.assertEqual(
            set(curated_schema["required"]),
            {"name", "description", "privacy", "song_ids", "idempotency_key"},
        )
        self.assertTrue(curated_schema["properties"]["song_ids"]["uniqueItems"])
        self.assertEqual(curated_schema["properties"]["song_ids"]["maxItems"], 50)
        playlist_schema = tools["get_playlist_songs"]["inputSchema"]["properties"]
        self.assertEqual(playlist_schema["limit"]["default"], 50)
        self.assertEqual(playlist_schema["offset"]["default"], 0)

    def test_time_aware_tool_descriptions_explain_model_facing_semantics(self):
        module = load_server("true")
        descriptions = {
            tool["name"]: tool["description"] for tool in module.available_tools()
        }
        recent = descriptions["get_recent_plays"]
        self.assertIn("played_at and played_at_utc are UTC", recent)
        self.assertIn("prefer played_at_local", recent)
        self.assertIn("using timezone", recent)

        podcast = descriptions["get_recent_podcast_plays"]
        self.assertIn("played_at and played_at_utc are UTC", podcast)
        self.assertIn("played_at_local together with timezone", podcast)

        audit = descriptions["get_operation_log"]
        self.assertIn("explicit *_utc fields are UTC", audit)
        self.assertIn("*_local with the matching *_timezone", audit)

        notes = descriptions["list_interaction_notes"]
        self.assertIn("explicit *_utc fields are UTC", notes)
        self.assertIn("*_local with the matching *_timezone", notes)

        daily = descriptions["daily_recommend"]
        self.assertIn("affects only display and date-language context", daily)
        self.assertIn("NetEase supplies the feed", daily)
        self.assertIn("controls its refresh boundary", daily)
        self.assertIn("cannot change that boundary", daily)
        self.assertIn("does not infer today from the deployment host", daily)

    def test_write_mode_advertises_write_oauth_scope(self):
        module = load_server("false", oauth=True)
        self.assertEqual(
            module.OAUTH_SCOPE,
            "netease.read netease.session netease.write",
        )

    def test_read_only_rejects_direct_write_call(self):
        module = load_server("true")
        with self.assertRaises(PermissionError):
            module.call_tool("create_playlist", {"name": "test"})

    def test_search_song_is_read_only_and_formats_results(self):
        module = load_server("true")
        response = {
            "result": {
                "songs": [
                    {"id": 123, "name": "Home", "artists": [{"name": "Depeche Mode"}]}
                ]
            }
        }
        with mock.patch.object(module, "netease_request", return_value=response):
            result = module.search_song("Home")
        self.assertIn("Home - Depeche Mode", result)
        self.assertIn("ID:123", result)

    def test_get_playlist_songs_default_pagination(self):
        module = load_server("true")
        playlist = {
            "playlist": {
                "name": "Signals",
                "trackCount": 3,
                "trackIds": [{"id": 11}, {"id": 22}, {"id": 33}],
            }
        }
        details = {
            "code": 200,
            "songs": [
                {"id": 11, "name": "One", "ar": [{"id": 1, "name": "A"}]},
                {"id": 22, "name": "Two", "ar": [{"id": 2, "name": "B"}]},
                {"id": 33, "name": "Three", "ar": [{"id": 3, "name": "C"}]},
            ],
        }
        with mock.patch.object(module, "netease_request", side_effect=[playlist, details]) as request:
            payload = json.loads(
                module.call_tool("get_playlist_songs", {"playlist_id": 99})
            )
        self.assertEqual(payload["pagination"]["limit"], 50)
        self.assertEqual(payload["pagination"]["offset"], 0)
        self.assertEqual(payload["pagination"]["returned"], 3)
        self.assertEqual(payload["playlist"]["total_tracks"], 3)
        self.assertFalse(payload["pagination"]["has_next"])
        self.assertEqual([song["position"] for song in payload["songs"]], [1, 2, 3])
        self.assertEqual(request.call_count, 2)

    def test_get_playlist_songs_custom_limit_and_offset(self):
        module = load_server("true")
        playlist = {
            "playlist": {
                "name": "Paged",
                "trackCount": 5,
                "trackIds": [{"id": song_id} for song_id in [1, 2, 3, 4, 5]],
            }
        }
        details = {
            "code": 200,
            "songs": [
                {"id": 3, "name": "Three", "ar": []},
                {"id": 4, "name": "Four", "ar": []},
            ],
        }
        with mock.patch.object(module, "netease_request", side_effect=[playlist, details]) as request:
            payload = json.loads(module.get_playlist_songs(99, limit=2, offset=2))
        self.assertEqual([song["song_id"] for song in payload["songs"]], [3, 4])
        self.assertTrue(payload["pagination"]["has_next"])
        self.assertEqual(payload["pagination"]["next_offset"], 4)
        requested = json.loads(request.call_args_list[1].kwargs["data"]["c"])
        self.assertEqual(requested, [{"id": 3}, {"id": 4}])

    def test_get_playlist_songs_rejects_invalid_pagination(self):
        module = load_server("true")
        invalid_calls = [
            {"limit": 0},
            {"limit": 101},
            {"limit": True},
            {"offset": -1},
            {"offset": 1.5},
            {"offset": False},
        ]
        with mock.patch.object(module, "netease_request") as request:
            for kwargs in invalid_calls:
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    module.get_playlist_songs(99, **kwargs)
        request.assert_not_called()

    def test_get_playlist_songs_handles_empty_and_out_of_range_pages(self):
        module = load_server("true")
        empty = {"playlist": {"name": "Empty", "trackCount": 0, "trackIds": []}}
        with mock.patch.object(module, "netease_request", return_value=empty) as request:
            payload = json.loads(module.get_playlist_songs(99))
        self.assertEqual(payload["songs"], [])
        self.assertEqual(payload["playlist"]["total_tracks"], 0)
        request.assert_called_once()

        short = {
            "playlist": {
                "name": "Short",
                "trackCount": 2,
                "trackIds": [{"id": 1}, {"id": 2}],
            }
        }
        with mock.patch.object(module, "netease_request", return_value=short) as request:
            payload = json.loads(module.get_playlist_songs(99, offset=20))
        self.assertEqual(payload["songs"], [])
        self.assertEqual(payload["pagination"]["offset"], 20)
        self.assertFalse(payload["pagination"]["has_next"])
        request.assert_called_once()

    def test_get_song_details_returns_metadata_and_missing_fields(self):
        module = load_server("true")
        response = {
            "code": 200,
            "songs": [
                {
                    "id": 1,
                    "name": "Concert Cut",
                    "ar": [{"id": 10, "name": "Artist"}],
                    "al": {"id": 20, "name": "Album", "tns": ["Translated Album"]},
                    "dt": 123456,
                    "publishTime": 0,
                    "alia": ["Alias"],
                    "tns": ["Translated Song"],
                    "tags": ["Live"],
                    "originCoverType": 0,
                    "version": 7,
                },
                {"id": 2, "name": "Sparse", "ar": []},
            ],
        }
        with mock.patch.object(module, "netease_request", return_value=response):
            payload = json.loads(module.get_song_details([1, 2, 3]))
        full, sparse = payload["songs"]
        self.assertEqual(full["album"]["name"], "Album")
        self.assertEqual(full["duration_ms"], 123456)
        self.assertEqual(full["release_time"], "1970-01-01T00:00:00Z")
        self.assertEqual(full["release_time_utc"], full["release_time"])
        self.assertEqual(
            full["release_time_local"], "1970-01-01T08:00:00+08:00"
        )
        self.assertEqual(full["release_timezone"], "Asia/Shanghai")
        self.assertTrue(full["version_flags"]["live"])
        self.assertIsNone(full["version_flags"]["remix"])
        self.assertEqual(full["version_detection"], "explicit_upstream_metadata_only; title_not_parsed")
        self.assertIsNone(sparse["album"])
        self.assertIsNone(sparse["release_time"])
        self.assertEqual(payload["missing_song_ids"], [3])

    def test_get_recent_plays_preserves_upstream_order_and_timestamps(self):
        module = load_server("true")
        response = {
            "code": 200,
            "data": {
                "total": 2,
                "list": [
                    {
                        "resourceId": "2",
                        "playTime": 2000,
                        "data": {"id": 2, "name": "Newer", "ar": [{"id": 1, "name": "A"}]},
                        "multiTerminalInfo": {"osText": "Web"},
                    },
                    {
                        "resourceId": "1",
                        "playTime": 1000,
                        "data": {"id": 1, "name": "Older", "ar": [{"id": 2, "name": "B"}]},
                    },
                ],
            },
        }
        with mock.patch.object(module, "netease_request", return_value=response):
            payload = json.loads(module.get_recent_plays(2))
        self.assertEqual([event["song_id"] for event in payload["events"]], [2, 1])
        self.assertEqual([event["play_time_ms"] for event in payload["events"]], [2000, 1000])
        self.assertEqual(payload["events"][0]["played_at"], "1970-01-01T00:00:02Z")
        self.assertEqual(
            payload["events"][0]["played_at_utc"], "1970-01-01T00:00:02Z"
        )
        self.assertEqual(
            payload["events"][0]["played_at_local"],
            "1970-01-01T08:00:02+08:00",
        )
        self.assertEqual(payload["events"][0]["timezone"], "Asia/Shanghai")
        self.assertEqual(payload["events"][0]["utc_offset"], "+08:00")
        self.assertEqual(payload["events"][0]["source_device"], "Web")

    def test_recent_play_timezone_utc(self):
        payload, event = self.recent_play_event(
            "UTC", datetime(2024, 1, 1, 12, 30, tzinfo=timezone.utc)
        )
        self.assertEqual(payload["timezone"], "UTC")
        self.assertEqual(event["played_at"], "2024-01-01T12:30:00Z")
        self.assertEqual(event["played_at_utc"], "2024-01-01T12:30:00Z")
        self.assertEqual(event["played_at_local"], "2024-01-01T12:30:00+00:00")
        self.assertEqual(event["timezone"], "UTC")
        self.assertEqual(event["utc_offset"], "+00:00")

    def test_recent_play_timezone_shanghai_crosses_midnight(self):
        _, event = self.recent_play_event(
            "Asia/Shanghai", datetime(2024, 1, 1, 16, 30, tzinfo=timezone.utc)
        )
        self.assertEqual(event["played_at_utc"], "2024-01-01T16:30:00Z")
        self.assertEqual(
            event["played_at_local"], "2024-01-02T00:30:00+08:00"
        )
        self.assertEqual(event["utc_offset"], "+08:00")

    def test_recent_play_timezone_new_york_observes_daylight_saving(self):
        _, summer = self.recent_play_event(
            "America/New_York", datetime(2024, 7, 1, 12, 0, tzinfo=timezone.utc)
        )
        _, winter = self.recent_play_event(
            "America/New_York", datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(summer["played_at_local"], "2024-07-01T08:00:00-04:00")
        self.assertEqual(summer["utc_offset"], "-04:00")
        self.assertEqual(winter["played_at_local"], "2024-01-01T07:00:00-05:00")
        self.assertEqual(winter["utc_offset"], "-05:00")

    def test_daily_recommend_keeps_legacy_text_and_documents_time_semantics(self):
        module = load_server("true", default_timezone="America/New_York")
        response = {
            "data": {
                "dailySongs": [
                    {"id": 1, "name": "Daily", "ar": [{"name": "Artist"}]}
                ]
            }
        }
        with mock.patch.object(module, "netease_request", return_value=response):
            output = module.daily_recommend()
        self.assertEqual(
            output,
            "Today's recommendations:\n1. Daily - Artist (ID:1)",
        )
        tool = next(
            item
            for item in module.available_tools()
            if item["name"] == "daily_recommend"
        )
        self.assertIn("NetEase supplies the feed", tool["description"])
        self.assertIn("cannot change that boundary", tool["description"])

    def test_invalid_default_timezone_is_rejected_at_startup(self):
        for invalid_timezone in ("Not/A_Timezone", "+08:00"):
            with self.subTest(default_timezone=invalid_timezone):
                module = load_server(
                    "true", default_timezone=invalid_timezone
                )
                with self.assertRaisesRegex(SystemExit, "valid IANA timezone"):
                    module.validate_startup()

    def test_get_recent_plays_does_not_fake_aggregate_data_as_events(self):
        module = load_server("true")
        response = {
            "weekData": [
                {"song": {"id": 1, "name": "Grouped", "ar": []}, "playCount": 7}
            ]
        }
        with mock.patch.object(module, "netease_request", return_value=response):
            payload = json.loads(module.get_recent_plays())
        self.assertEqual(payload["record_type"], "aggregated_play_counts")
        self.assertEqual(payload["events"], [])
        self.assertEqual(payload["aggregated_tracks"][0]["play_count"], 7)
        self.assertNotIn("played_at", payload["aggregated_tracks"][0])

    def test_podcast_tool_schemas_keep_radio_and_program_ids_distinct(self):
        module = load_server("true")
        tools = {tool["name"]: tool for tool in module.available_tools()}
        expected = {
            "list_my_subscribed_podcasts",
            "get_podcast_programs",
            "get_podcast_program",
            "get_podcast_program_details",
            "search_podcasts",
            "search_podcast_programs",
            "get_recent_podcast_plays",
        }
        self.assertTrue(expected.issubset(tools))
        for name in expected:
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"])
            self.assertNotIn("song_id", tools[name]["inputSchema"]["properties"])
        self.assertIn("radio_id", tools["get_podcast_programs"]["inputSchema"]["properties"])
        self.assertIn("program_id", tools["get_podcast_program"]["inputSchema"]["properties"])
        detail_schema = tools["get_podcast_program_details"]["inputSchema"]["properties"][
            "program_ids"
        ]
        self.assertEqual(detail_schema["minItems"], 1)
        self.assertEqual(detail_schema["maxItems"], 50)

    def test_list_subscribed_podcasts_uses_pagination_and_public_count_labels(self):
        module = load_server("true")
        response = {
            "code": 200,
            "count": 4,
            "hasMore": True,
            "djRadios": [
                {
                    "id": 101,
                    "name": "Signals",
                    "desc": "Interviews",
                    "playCount": 999,
                    "subCount": 8,
                    "programCount": 12,
                    "dj": {"userId": 7, "nickname": "Host"},
                }
            ],
        }
        with mock.patch.object(module, "netease_request", return_value=response) as request:
            payload = json.loads(module.list_my_subscribed_podcasts(1, 2))
        self.assertEqual(payload["podcasts"][0]["radio_id"], 101)
        self.assertEqual(payload["podcasts"][0]["public_total_play_count"], 999)
        self.assertNotIn("personal_play_count", payload["podcasts"][0])
        self.assertEqual(payload["pagination"]["next_offset"], 3)
        self.assertEqual(
            request.call_args.kwargs["data"],
            {"limit": "1", "offset": "2", "total": "true"},
        )

    def test_get_podcast_programs_normalizes_ids_and_order(self):
        module = load_server("true")
        response = {
            "code": 200,
            "count": 1,
            "more": False,
            "programs": [
                {
                    "id": 501,
                    "name": "Episode one",
                    "radio": {"id": 101, "name": "Signals"},
                    "mainTrackId": 9001,
                    "duration": 123000,
                    "createTime": 2000,
                    "serialNum": 92,
                    "listenerCount": 77,
                }
            ],
        }
        with mock.patch.object(module, "netease_request", return_value=response) as request:
            payload = json.loads(module.get_podcast_programs(101, order="oldest"))
        program = payload["programs"][0]
        self.assertEqual(program["program_id"], 501)
        self.assertEqual(program["radio_id"], 101)
        self.assertEqual(program["main_track_id"], 9001)
        self.assertNotIn("song_id", program)
        self.assertEqual(program["public_listener_count"], 77)
        self.assertEqual(program["serial_number"], 92)
        self.assertEqual(program["published_at"], "1970-01-01T00:00:02Z")
        self.assertEqual(program["published_at_utc"], program["published_at"])
        self.assertEqual(
            program["published_at_local"], "1970-01-01T08:00:02+08:00"
        )
        self.assertEqual(program["published_at_timezone"], "Asia/Shanghai")
        self.assertEqual(request.call_args.kwargs["data"]["asc"], "true")

    def test_search_podcasts_and_programs_parse_current_resource_shape(self):
        module = load_server("true")
        radio_response = {
            "code": 200,
            "data": {
                "totalCount": 1,
                "hasMore": False,
                "resources": [
                    {"resourceType": "voicelist", "resourceId": "101", "baseInfo": {"id": 101, "name": "Radio"}}
                ],
            },
        }
        program_response = {
            "code": 200,
            "data": {
                "totalCount": 1,
                "hasMore": False,
                "resources": [
                    {
                        "resourceType": "voice",
                        "resourceId": "501",
                        "baseInfo": {
                            "name": "Episode",
                            "radio": {"id": 101},
                            "serialNum": 1585109780313,
                        },
                    }
                ],
            },
        }
        with mock.patch.object(
            module, "netease_request", side_effect=[radio_response, program_response]
        ) as request:
            radios = json.loads(module.search_podcasts("  ambient  "))
            programs = json.loads(module.search_podcast_programs("ambient"))
        self.assertEqual(radios["query"], "ambient")
        self.assertEqual(radios["podcasts"][0]["radio_id"], 101)
        self.assertEqual(programs["programs"][0]["program_id"], 501)
        self.assertIsNone(programs["programs"][0]["serial_number"])
        self.assertNotIn("song_id", programs["programs"][0])
        self.assertIn("/api/search/voicelist/get", request.call_args_list[0].args[0])
        self.assertIn("/api/search/voice/get", request.call_args_list[1].args[0])

    def test_get_podcast_program_returns_normalized_detail(self):
        module = load_server("true")
        response = {
            "code": 200,
            "program": {
                "id": 2066305916,
                "name": "Gleichnis",
                "description": "Gleichnis",
                "coverUrl": "https://example.test/cover.jpg",
                "radio": {
                    "id": 794591707,
                    "name": "德语音乐剧浮士德Faust Ⅰ+Ⅱ-Die Rockoper",
                },
                "dj": {"userId": 304462499, "nickname": "慢慢moi_"},
                "mainTrackId": 1433739795,
                "duration": 179722,
                "createTime": 1585109780313,
                "serialNum": 92,
                "listenerCount": 20259,
                "likedCount": 27,
                "commentCount": 6,
                "shareCount": 2,
            },
        }
        with mock.patch.object(module, "netease_request", return_value=response) as request:
            program = json.loads(module.get_podcast_program(2066305916))
        self.assertEqual(program["resource_type"], "podcast_program")
        self.assertEqual(program["program_id"], 2066305916)
        self.assertEqual(program["radio_id"], 794591707)
        self.assertEqual(program["main_track_id"], 1433739795)
        self.assertEqual(
            program["main_track_semantics"],
            "Audio carrier returned by NetEase; it is not exposed as a normal song_id.",
        )
        self.assertNotIn("song_id", program)
        self.assertEqual(program["duration_seconds"], 179.722)
        self.assertEqual(program["published_at"], "2020-03-25T04:16:20.313000Z")
        self.assertEqual(program["published_at_utc"], program["published_at"])
        self.assertEqual(
            program["published_at_local"], "2020-03-25T12:16:20.313000+08:00"
        )
        self.assertEqual(program["published_at_timezone"], "Asia/Shanghai")
        self.assertEqual(program["published_at_utc_offset"], "+08:00")
        self.assertEqual(program["serial_number"], 92)
        self.assertEqual(program["public_listener_count"], 20259)
        self.assertIn("/api/dj/program/detail", request.call_args.args[0])
        self.assertEqual(request.call_args.kwargs["data"], {"id": "2066305916"})

    def test_get_podcast_program_normalizes_missing_fields_to_null(self):
        module = load_server("true")
        response = {"code": 200, "program": {"id": 501}}
        with mock.patch.object(module, "netease_request", return_value=response):
            program = json.loads(module.get_podcast_program(501))
        for field in (
            "radio_id",
            "main_track_id",
            "name",
            "description",
            "cover_url",
            "radio_name",
            "creator",
            "duration_ms",
            "duration_seconds",
            "published_time_ms",
            "published_at",
            "published_at_utc",
            "published_at_local",
            "published_at_utc_offset",
            "serial_number",
            "program_type",
            "public_listener_count",
            "public_liked_count",
            "public_comment_count",
            "public_share_count",
        ):
            with self.subTest(field=field):
                self.assertIsNone(program[field])

    def test_get_podcast_program_not_found_uses_structured_mcp_error(self):
        module = load_server("true")
        with mock.patch.object(
            module, "netease_request", return_value={"code": 200}
        ):
            with self.assertRaises(module.PodcastProgramNotFound) as context:
                module.get_podcast_program(999999999999)
        self.assertEqual(context.exception.code, "podcast_program_not_found")
        response = module.tool_error_response(1, str(context.exception))
        self.assertTrue(response["result"]["isError"])
        self.assertIn("podcast_program_not_found", response["result"]["content"][0]["text"])

    def test_get_podcast_program_details_preserves_order_deduplicates_and_partially_succeeds(
        self,
    ):
        module = load_server("true")

        def response_for(_url, *, data):
            program_id = int(data["id"])
            if program_id == 501:
                return {
                    "code": 200,
                    "program": {
                        "id": 501,
                        "name": "Found",
                        "mainTrackId": 9001,
                    },
                }
            if program_id == 999:
                return {"code": 404}
            raise module.NetEaseError(
                "upstream failed with " + module.NETEASE_COOKIE + " Authorization: secret"
            )

        requested = [501, 999, 501, 502]
        with mock.patch.object(
            module, "netease_request", side_effect=response_for
        ) as request:
            payload = json.loads(module.get_podcast_program_details(requested))
        self.assertEqual(payload["record_type"], "podcast_program_detail_batch")
        self.assertEqual(payload["requested"], 4)
        self.assertEqual(payload["returned"], 2)
        self.assertEqual(
            [item["requested_program_id"] for item in payload["programs"]], requested
        )
        self.assertEqual(
            [item["found"] for item in payload["programs"]],
            [True, False, True, False],
        )
        self.assertEqual(
            payload["programs"][1]["error"]["code"], "podcast_program_not_found"
        )
        self.assertEqual(
            payload["programs"][3]["error"]["code"],
            "podcast_program_lookup_failed",
        )
        self.assertEqual(request.call_count, 3)
        requested_upstream_ids = sorted(
            int(call.kwargs["data"]["id"]) for call in request.call_args_list
        )
        self.assertEqual(requested_upstream_ids, [501, 502, 999])
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("MUSIC_U=test", serialized)
        self.assertNotIn("Authorization: secret", serialized)

    def test_get_podcast_program_details_validates_batch_size_before_network(self):
        module = load_server("true")
        with mock.patch.object(module, "netease_request") as request:
            with self.assertRaises(ValueError):
                module.get_podcast_program_details([])
            with self.assertRaises(ValueError):
                module.get_podcast_program_details(list(range(1, 52)))
        request.assert_not_called()

    def test_recent_podcast_plays_preserve_order_without_faking_timestamps_or_counts(self):
        module = load_server("true")
        response = {
            "code": 200,
            "data": {
                "total": 2,
                "list": [
                    {
                        "resourceId": "502",
                        "playTime": 2000,
                        "data": {"id": 502, "name": "New", "radio": {"id": 101}},
                        "multiTerminalInfo": {"osText": "Web"},
                    },
                    {
                        "resourceId": "501",
                        "data": {
                            "id": 501,
                            "name": "Old",
                            "radio": {"id": 101},
                            "listenerCount": 88,
                        },
                    },
                ],
            },
        }
        with mock.patch.object(module, "netease_request", return_value=response):
            payload = json.loads(module.get_recent_podcast_plays(2))
        self.assertEqual([item["program_id"] for item in payload["records"]], [502, 501])
        self.assertEqual(payload["records"][0]["played_at"], "1970-01-01T00:00:02Z")
        self.assertEqual(
            payload["records"][0]["played_at_utc"],
            payload["records"][0]["played_at"],
        )
        self.assertEqual(
            payload["records"][0]["played_at_local"],
            "1970-01-01T08:00:02+08:00",
        )
        self.assertEqual(payload["records"][0]["timezone"], "Asia/Shanghai")
        self.assertIsNone(payload["records"][1]["played_at"])
        self.assertIsNone(payload["records"][1]["played_at_local"])
        self.assertIsNone(payload["records"][1]["utc_offset"])
        self.assertEqual(payload["records"][1]["public_listener_count"], 88)
        self.assertFalse(payload["personal_play_count_supported"])
        self.assertFalse(payload["limitations"]["complete_event_stream_guaranteed"])

    def test_recent_podcast_plays_rejects_unknown_shape_instead_of_faking_events(self):
        module = load_server("true")
        response = {"code": 200, "weekData": [{"listenerCount": 900}]}
        with mock.patch.object(module, "netease_request", return_value=response):
            payload = json.loads(module.get_recent_podcast_plays())
        self.assertEqual(payload["response_shape"], "unsupported")
        self.assertEqual(payload["records"], [])
        self.assertFalse(payload["personal_play_count_supported"])

    def test_podcast_tools_validate_before_network_calls(self):
        module = load_server("true")
        calls = [
            lambda: module.list_my_subscribed_podcasts(0, 0),
            lambda: module.list_my_subscribed_podcasts(1, -1),
            lambda: module.get_podcast_programs(0),
            lambda: module.get_podcast_programs(1, order="random"),
            lambda: module.get_podcast_program(0),
            lambda: module.get_podcast_program_details([]),
            lambda: module.get_podcast_program_details([1] * 51),
            lambda: module.search_podcasts(" "),
            lambda: module.search_podcast_programs("x", limit=51),
            lambda: module.get_recent_podcast_plays(True),
        ]
        with mock.patch.object(module, "netease_request") as request:
            for call in calls:
                with self.subTest(call=call), self.assertRaises(ValueError):
                    call()
        request.assert_not_called()

    def test_podcast_upstream_failure_is_redacted(self):
        module = load_server("true")
        response = {"code": 500, "message": "failed for " + module.NETEASE_COOKIE}
        with mock.patch.object(module, "netease_request", return_value=response):
            with self.assertRaises(module.NetEaseError) as context:
                module.search_podcasts("ambient")
        self.assertNotIn("MUSIC_U=test", str(context.exception))
        self.assertIn("[REDACTED]", str(context.exception))

    def test_upstream_failure_is_redacted(self):
        module = load_server("true")
        response = {"code": 500, "message": "failed for " + module.NETEASE_COOKIE}
        with mock.patch.object(module, "netease_request", return_value=response):
            with self.assertRaises(module.NetEaseError) as context:
                module.get_recent_plays()
        self.assertNotIn("MUSIC_U=test", str(context.exception))
        self.assertIn("[REDACTED]", str(context.exception))

    def test_song_ids_reject_string_input(self):
        module = load_server("false")
        with self.assertRaises(ValueError):
            module._song_ids("1,2")

    def test_update_playlist_requires_a_change(self):
        module = load_server("false")
        with self.assertRaises(ValueError):
            module.update_playlist(123)

    def test_update_playlist_updates_name_and_description(self):
        module = load_server("false")
        responses = [{"code": 200}, {"code": 200}]
        with mock.patch.object(module, "netease_request", side_effect=responses) as request:
            result = module.update_playlist(123, "  Night Signals  ", "For late listening.")
        self.assertEqual(result, "Updated playlist 123: name, description.")
        self.assertEqual(request.call_count, 2)
        self.assertIn("/api/playlist/update/name", request.call_args_list[0].args[0])
        self.assertEqual(
            request.call_args_list[0].kwargs["data"],
            {"id": "123", "name": "Night Signals"},
        )
        self.assertIn("/api/playlist/desc/update", request.call_args_list[1].args[0])
        self.assertEqual(
            request.call_args_list[1].kwargs["data"],
            {"id": "123", "desc": "For late listening."},
        )

    def test_update_playlist_can_clear_description(self):
        module = load_server("false")
        with mock.patch.object(module, "netease_request", return_value={"code": 200}) as request:
            result = module.update_playlist(123, description="")
        self.assertEqual(result, "Updated playlist 123: description.")
        self.assertEqual(request.call_args.kwargs["data"], {"id": "123", "desc": ""})

    def test_reorder_playlist_tracks_rejects_invalid_complete_orders(self):
        module = load_server("false")
        with self.assertRaises(ValueError):
            module._validate_complete_track_order([1, 2, 3], [1, 1, 3])
        with self.assertRaises(ValueError):
            module._validate_complete_track_order([1, 2, 3], [1, 2])
        with self.assertRaises(ValueError):
            module._validate_complete_track_order([1, 2, 3], [1, 2, 4])

    def test_reorder_playlist_tracks_rejects_collected_playlist(self):
        module = load_server("false")
        responses = [
            {"account": {"id": 7}},
            {
                "playlist": {
                    "creator": {"userId": 8},
                    "trackCount": 2,
                    "trackIds": [{"id": 1}, {"id": 2}],
                }
            },
        ]
        with mock.patch.object(module, "netease_request", side_effect=responses) as request:
            with self.assertRaises(PermissionError):
                module.reorder_playlist_tracks(99, [2, 1])
        self.assertEqual(request.call_count, 2)

    def test_reorder_playlist_tracks_verifies_applied_order(self):
        module = load_server("false")
        responses = [
            {"account": {"id": 7}},
            {
                "playlist": {
                    "creator": {"userId": 7},
                    "trackCount": 3,
                    "trackIds": [{"id": 1}, {"id": 2}, {"id": 3}],
                }
            },
            {"code": 200},
            {
                "playlist": {
                    "creator": {"userId": 7},
                    "trackCount": 3,
                    "trackIds": [{"id": 3}, {"id": 2}, {"id": 1}],
                }
            },
        ]
        with mock.patch.object(module, "netease_request", side_effect=responses) as request:
            payload = json.loads(module.reorder_playlist_tracks(99, [3, 2, 1]))
        self.assertTrue(payload["success"])
        self.assertTrue(payload["verified"])
        self.assertEqual(payload["playlist_id"], 99)
        self.assertEqual(request.call_args_list[2].kwargs["data"]["op"], "update")
        self.assertEqual(request.call_args_list[2].kwargs["data"]["trackIds"], "[3,2,1]")


class NetEaseSessionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.module = load_server("true")

    def tearDown(self):
        self.module.SESSION_MANAGER = None
        self.module.STORE_INSTANCE = None
        self.tempdir.cleanup()

    def configure_storage(self):
        self.module.STORAGE_PATH = str(Path(self.tempdir.name) / "session.sqlite3")
        self.module.STORE_INSTANCE = None
        self.module.SESSION_MANAGER = None
        return self.module._store()

    def add_qr_attempt(self, login_id, *, created_monotonic=None, **overrides):
        context = self.module.create_web_qr_context(now_ms=1_700_000_000_000)
        attempt = {
            "key": "synthetic-key",
            **context,
            "qr_payload": (
                "https://music.163.com/st/platform/scanlogin?"
                "codekey=synthetic-key&chainId=synthetic-chain"
            ),
            "status": "waiting",
            "created_monotonic": (
                self.module.time.monotonic()
                if created_monotonic is None
                else created_monotonic
            ),
            "last_checked_monotonic": 0.0,
            "checking": False,
            **overrides,
        }
        self.module.QR_LOGIN_ATTEMPTS[login_id] = attempt
        return attempt

    def test_environment_cookie_remains_a_supported_fallback(self):
        manager = self.module._session_manager()
        session = manager.current()
        self.assertEqual(session["source"], "environment")
        self.assertEqual(manager.get_csrf(), "test")
        self.assertIn("MUSIC_U=test", manager.get_cookie())

    def test_environment_cookie_accepts_legacy_extra_fields_without_storage(self):
        self.module.NETEASE_COOKIE = (
            "os=pc; MUSIC_U=legacy-synthetic; NMTID=ignored; "
            "__csrf=old; __csrf=current; rememberLogin=true"
        )
        self.module.STORAGE_PATH = ""
        self.module.SESSION_MANAGER = None
        self.module.validate_startup()
        manager = self.module._session_manager()
        self.assertFalse(manager.storage_configured)
        self.assertEqual(
            manager.get_cookie(),
            "MUSIC_U=legacy-synthetic; __csrf=current",
        )
        self.assertEqual(manager.get_csrf(), "current")

    def test_persistent_session_is_saved_loaded_and_preferred(self):
        store = self.configure_storage()
        store.save_netease_session(
            {
                "cookie": "MUSIC_U=persisted; __csrf=persisted-csrf",
                "csrf": "persisted-csrf",
                "status": "active",
                "source": "qr_login",
                "updated_at": utc_now(),
                "last_verified_at": None,
                "user_id": None,
            }
        )
        loaded = store.load_netease_session()
        self.assertEqual(loaded["status"], "active")
        self.assertEqual(loaded["csrf"], "persisted-csrf")
        manager = self.module._session_manager()
        self.assertEqual(manager.current()["source"], "qr_login")
        self.assertEqual(manager.get_csrf(), "persisted-csrf")

    def test_csrf_parsing_uses_the_last_duplicate(self):
        from netease_session import extract_csrf, normalize_session_cookie

        raw = "__csrf=old; MUSIC_U=synthetic; __csrf=new"
        self.assertEqual(extract_csrf(raw), "new")
        self.assertEqual(
            normalize_session_cookie(raw),
            "MUSIC_U=synthetic; __csrf=new",
        )

    def test_explicit_login_failure_invalidates_the_current_session(self):
        with mock.patch.object(
            self.module,
            "_raw_netease_request",
            return_value=({"code": 301, "message": "需要登录"}, {}),
        ):
            with self.assertRaises(self.module.NetEaseAuthExpired) as context:
                self.module.verify_netease_session(force=True)
        self.assertIn("NETEASE_AUTH_EXPIRED", str(context.exception))
        self.assertIsNone(self.module._session_manager().current())

    def test_plain_403_is_not_misclassified_as_auth_expired(self):
        account = {
            "code": 200,
            "profile": {"userId": 7},
            "account": {"id": 7},
        }
        side_effect = [
            self.module.NetEaseHTTPFailure(403, {"code": 403, "message": "illegal request"}),
            (account, {}),
        ]
        with mock.patch.object(
            self.module, "_raw_netease_request", side_effect=side_effect
        ):
            with self.assertRaises(self.module.NetEaseRequestRejected) as context:
                self.module.netease_request("https://music.163.com/api/test")
        self.assertIn("NETEASE_REQUEST_REJECTED", str(context.exception))
        self.assertNotIsInstance(context.exception, self.module.NetEaseAuthExpired)
        self.assertIsNotNone(self.module._session_manager().current())

    def test_qr_status_transitions_waiting_scanned_and_expired(self):
        self.configure_storage()
        cases = {801: "waiting", 802: "scanned", 800: "expired"}
        for index, (code, expected) in enumerate(cases.items(), 1):
            login_id = f"synthetic-login-id-{index:02d}"
            self.add_qr_attempt(login_id, key=f"synthetic-key-{index}")
            with mock.patch.object(
                self.module,
                "_netease_qr_request",
                return_value=({"code": code}, []),
            ):
                result = json.loads(self.module.check_netease_qr_login(login_id))
            self.assertEqual(result["status"], expected)

    def test_start_qr_login_returns_only_safe_rendering_data(self):
        self.configure_storage()
        self.module.PUBLIC_URL = "https://music.example.test"
        with mock.patch.object(
            self.module,
            "_netease_qr_request",
            return_value=(
                {"code": 200, "unikey": "synthetic-qr-key"},
                ["NMTID=temporary-secret; Path=/"],
            ),
        ) as request:
            raw_result = self.module.start_netease_qr_login()
        result = json.loads(raw_result)
        self.assertEqual(result["status"], "waiting")
        parsed = urllib.parse.urlsplit(result["qr_payload"])
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/st/platform/scanlogin")
        self.assertEqual(query["codekey"], ["synthetic-qr-key"])
        self.assertEqual(query["hdw_device"], ["web"])
        self.assertEqual(query["hdw_appid"], ["web"])
        self.assertEqual(query["hitExp"], ["1"])
        self.assertTrue(query["chainId"][0].startswith("v1_unknown-"))
        self.assertIn("login_id", result)
        self.assertEqual(
            result["login_url"],
            f"https://music.example.test/netease/login/{result['login_id']}",
        )
        self.assertEqual(request.call_args.args[1], {"type": 1, "noCheckToken": True})
        attempt = self.module.QR_LOGIN_ATTEMPTS[result["login_id"]]
        self.assertEqual(attempt["chain_id"], query["chainId"][0])
        self.assertIn("NMTID=temporary-secret", attempt["temporary_cookie"])
        self.assertNotIn("MUSIC_U", raw_result)
        self.assertNotIn("__csrf", raw_result)
        self.assertNotIn("temporary-secret", raw_result)

    def test_start_qr_login_mcp_result_contains_text_url_and_png(self):
        self.configure_storage()
        self.module.PUBLIC_URL = "https://music.example.test"
        with mock.patch.object(
            self.module,
            "_netease_qr_request",
            return_value=({"code": 200, "unikey": "synthetic-image-key"}, []),
        ):
            response = self.module.handle_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": 91,
                    "method": "tools/call",
                    "params": {
                        "name": "start_netease_qr_login",
                        "arguments": {},
                    },
                }
            )
        content = response["result"]["content"]
        self.assertEqual([item["type"] for item in content], ["text", "image"])
        text_result = json.loads(content[0]["text"])
        self.assertEqual(text_result["qr_url"], text_result["qr_payload"])
        self.assertTrue(text_result["login_url"].startswith("https://music.example.test/"))
        self.assertTrue(text_result["qr_url"].startswith("https://music.163.com/"))
        self.assertEqual(content[1]["mimeType"], "image/png")
        image_data = base64.b64decode(content[1]["data"], validate=True)
        with Image.open(io.BytesIO(image_data)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertGreater(image.width, 100)

    def test_weapi_qr_payload_encryption_has_expected_form_fields(self):
        from netease_session import weapi_encrypt

        encrypted = weapi_encrypt('{"type":3}', "abcdefghijklmnop")
        self.assertEqual(set(encrypted), {"params", "encSecKey"})
        self.assertTrue(encrypted["params"])
        self.assertEqual(len(encrypted["encSecKey"]), 256)
        int(encrypted["encSecKey"], 16)

    def test_confirmed_qr_login_persists_session_without_returning_it(self):
        store = self.configure_storage()
        login_id = "synthetic-confirmed-login-id"
        self.add_qr_attempt(
            login_id,
            temporary_cookie="NMTID=temp-only; MUSIC_U=temporary-old; __csrf=old-csrf",
        )
        headers = [
            "MUSIC_U=qr-secret; Path=/; HttpOnly",
            "__csrf=header-csrf; Path=/; Secure",
        ]
        with mock.patch.object(
            self.module,
            "_netease_qr_request",
            return_value=({"code": 803, "cookie": "__csrf=body-csrf"}, headers),
        ), mock.patch.object(
            self.module,
            "verify_netease_session",
            return_value={"authenticated": True, "user_id": 7, "cached": False},
        ):
            raw_result = self.module.check_netease_qr_login(login_id)
        result = json.loads(raw_result)
        self.assertEqual(result["status"], "confirmed")
        self.assertTrue(result["session_persisted"])
        persisted = store.load_netease_session()
        self.assertIn("MUSIC_U=qr-secret", persisted["cookie"])
        self.assertEqual(persisted["csrf"], "body-csrf")
        self.assertNotIn("NMTID", persisted["cookie"])
        self.assertNotIn(login_id, self.module.QR_LOGIN_ATTEMPTS)
        self.assertNotIn("qr-secret", raw_result)
        self.assertNotIn("body-csrf", raw_result)

    def test_web_qr_check_reuses_chain_cookie_and_type(self):
        self.configure_storage()
        self.module.PUBLIC_URL = "https://music.example.test"
        responses = [
            (
                {"code": 200, "unikey": "continuity-key"},
                ["NMTID=server-temporary; Path=/"],
            ),
            ({"code": 801}, ["WEVNSM=2.0.0; Path=/"]),
        ]
        with mock.patch.object(
            self.module, "_netease_qr_request", side_effect=responses
        ) as request:
            started = json.loads(self.module.start_netease_qr_login())
            login_id = started["login_id"]
            before = dict(self.module.QR_LOGIN_ATTEMPTS[login_id])
            checked = json.loads(self.module.check_netease_qr_login(login_id))
        self.assertEqual(checked["status"], "waiting")
        key_call, check_call = request.call_args_list
        self.assertEqual(key_call.args[1], {"type": 1, "noCheckToken": True})
        self.assertEqual(check_call.args[1]["type"], 1)
        self.assertTrue(check_call.args[1]["noCheckToken"])
        self.assertEqual(check_call.args[1]["ydDeviceToken"], "")
        self.assertNotIn("secureCaptcha", check_call.args[1])
        self.assertTrue(check_call.kwargs["checking"])
        self.assertEqual(
            check_call.args[2]["chain_id"], before["chain_id"]
        )
        self.assertIn("NMTID=server-temporary", check_call.args[2]["temporary_cookie"])
        after = self.module.QR_LOGIN_ATTEMPTS[login_id]
        self.assertEqual(after["chain_id"], before["chain_id"])
        self.assertIn("WEVNSM=2.0.0", after["temporary_cookie"])

    def test_web_qr_request_uses_browser_headers_and_chain(self):
        class Headers:
            def get_all(self, _name):
                return []

        class Response:
            headers = Headers()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b'{"code":801}'

        context = self.module.create_web_qr_context(now_ms=1_700_000_000_000)
        with mock.patch.object(
            self.module,
            "weapi_encrypt",
            return_value={"params": "safe", "encSecKey": "safe"},
        ) as encrypt, mock.patch.object(
            self.module.urllib.request, "urlopen", return_value=Response()
        ) as urlopen:
            response, _ = self.module._netease_qr_request(
                "/api/login/qrcode/client/login",
                {"key": "synthetic", "type": 1, "noCheckToken": True},
                context,
                checking=True,
            )
        self.assertEqual(response["code"], 801)
        payload = json.loads(encrypt.call_args.args[0])
        self.assertEqual(payload["type"], 1)
        self.assertTrue(payload["noCheckToken"])
        request = urlopen.call_args.args[0]
        headers = {name.casefold(): value for name, value in request.header_items()}
        self.assertEqual(headers["origin"], "https://music.163.com")
        self.assertEqual(headers["referer"], "https://music.163.com/")
        self.assertEqual(headers["x-os"], "web")
        self.assertEqual(headers["x-loginmethod"], "QrCode")
        self.assertEqual(headers["x-login-chain-id"], context["chain_id"])
        self.assertEqual(headers["cookie"], context["temporary_cookie"])

    def test_security_verification_pauses_and_preserves_attempt(self):
        self.configure_storage()
        login_id = "synthetic-security-login-id"
        before = self.add_qr_attempt(login_id)
        with mock.patch.object(
            self.module,
            "_netease_qr_request",
            return_value=(
                {"code": 8821, "message": "security verification"},
                ["WEVNSM=2.0.0; Path=/"],
            ),
        ) as request:
            result = json.loads(self.module.check_netease_qr_login(login_id))
            paused = json.loads(self.module.check_netease_qr_login(login_id))
        self.assertEqual(result["status"], "verification_required")
        self.assertEqual(result["upstream_code"], 8821)
        self.assertTrue(result["polling_paused"])
        self.assertEqual(paused["status"], "verification_required")
        self.assertTrue(paused["polling_paused"])
        self.assertEqual(request.call_count, 1)
        after = self.module.QR_LOGIN_ATTEMPTS[login_id]
        self.assertEqual(after["key"], before["key"])
        self.assertEqual(after["chain_id"], before["chain_id"])
        self.assertIn("WEVNSM=2.0.0", after["temporary_cookie"])

    def test_security_proof_resumes_same_attempt_with_fresh_device_token(self):
        self.configure_storage()
        login_id = "synthetic-proof-resume-id"
        attempt = self.add_qr_attempt(login_id, status="verification_required")
        proof = "genuine-browser-validate-value"
        device_token = "genuine-browser-device-token"
        with mock.patch.object(
            self.module,
            "_netease_qr_request",
            return_value=({"code": 802}, []),
        ) as request:
            result = self.module._check_netease_qr_login(
                login_id,
                secure_captcha=proof,
                yd_device_token=device_token,
            )
        self.assertEqual(result["status"], "scanned")
        payload = request.call_args.args[1]
        context = request.call_args.args[2]
        self.assertEqual(payload["secureCaptcha"], proof)
        self.assertEqual(payload["ydDeviceToken"], device_token)
        self.assertEqual(payload["key"], attempt["key"])
        self.assertEqual(context["chain_id"], attempt["chain_id"])
        self.assertEqual(context["temporary_cookie"], attempt["temporary_cookie"])
        self.assertNotIn("secure_captcha", self.module.QR_LOGIN_ATTEMPTS[login_id])
        self.assertNotIn("yd_device_token", result)

    def test_browser_device_token_is_request_scoped_not_cached_in_attempt(self):
        self.configure_storage()
        login_id = "synthetic-fresh-device-token-id"
        attempt = self.add_qr_attempt(login_id)
        with mock.patch.object(
            self.module,
            "_netease_qr_request",
            side_effect=[({"code": 801}, []), ({"code": 802}, [])],
        ) as request:
            self.module._check_netease_qr_login(
                login_id, yd_device_token="browser-device-token-one"
            )
            attempt["last_checked_monotonic"] = 0.0
            self.module._check_netease_qr_login(
                login_id, yd_device_token="browser-device-token-two"
            )
        self.assertEqual(
            [call.args[1]["ydDeviceToken"] for call in request.call_args_list],
            ["browser-device-token-one", "browser-device-token-two"],
        )
        self.assertEqual(attempt["yd_device_token"], "")

    def test_security_proof_is_not_submitted_twice(self):
        self.configure_storage()
        login_id = "synthetic-proof-once-id"
        self.add_qr_attempt(login_id, status="verification_required")
        proof = "single-use-validate-value"
        with mock.patch.object(
            self.module,
            "_netease_qr_request",
            return_value=({"code": 8821}, []),
        ) as request:
            first = self.module._check_netease_qr_login(
                login_id, secure_captcha=proof, yd_device_token="first-device-token"
            )
            second = self.module._check_netease_qr_login(
                login_id, secure_captcha=proof, yd_device_token="second-device-token"
            )
        self.assertEqual(first["status"], "verification_required")
        self.assertTrue(second["challenge_retry_required"])
        self.assertEqual(request.call_count, 1)
        self.assertNotIn(proof, json.dumps(second))

    def test_security_proof_is_rejected_before_8821(self):
        login_id = "synthetic-early-proof-id"
        self.add_qr_attempt(login_id, status="waiting")
        with mock.patch.object(self.module, "_netease_qr_request") as request:
            with self.assertRaises(ValueError) as context:
                self.module._check_netease_qr_login(
                    login_id,
                    secure_captcha="early-private-proof",
                    yd_device_token="browser-device-token",
                )
        request.assert_not_called()
        self.assertNotIn("early-private-proof", str(context.exception))

    def test_security_proof_confirmation_persists_session_and_cleans_attempt(self):
        store = self.configure_storage()
        login_id = "synthetic-proof-confirm-id"
        self.add_qr_attempt(login_id, status="verification_required")
        with mock.patch.object(
            self.module,
            "_netease_qr_request",
            return_value=(
                {"code": 803, "cookie": "__csrf=proof-csrf"},
                ["MUSIC_U=proof-session; Path=/; HttpOnly"],
            ),
        ), mock.patch.object(
            self.module,
            "verify_netease_session",
            return_value={"authenticated": True, "user_id": 7, "cached": False},
        ):
            raw = json.dumps(
                self.module._check_netease_qr_login(
                    login_id,
                    secure_captcha="browser-validate",
                    yd_device_token="browser-device-token",
                )
            )
        self.assertNotIn(login_id, self.module.QR_LOGIN_ATTEMPTS)
        self.assertIn("MUSIC_U=proof-session", store.load_netease_session()["cookie"])
        self.assertNotIn("proof-session", raw)
        self.assertNotIn("browser-validate", raw)
        self.assertNotIn("browser-device-token", raw)

    def test_8830_stops_and_cleans_up_attempt(self):
        self.configure_storage()
        login_id = "synthetic-risk-status-id"
        self.add_qr_attempt(login_id, status="verification_required")
        with mock.patch.object(
            self.module,
            "_netease_qr_request",
            return_value=({"code": 8830, "message": "more verification"}, []),
        ):
            result = self.module._check_netease_qr_login(
                login_id,
                secure_captcha="browser-validate",
                yd_device_token="browser-device-token",
            )
        self.assertEqual(
            result["status"], "additional_security_verification_required"
        )
        self.assertEqual(result["upstream_code"], 8830)
        self.assertTrue(result["polling_stopped"])
        self.assertNotIn(login_id, self.module.QR_LOGIN_ATTEMPTS)

    def test_explicit_qr_cancel_cleans_ephemeral_context(self):
        login_id = "synthetic-explicit-cancel-id"
        attempt = self.add_qr_attempt(
            login_id,
            status="verification_required",
            last_secure_captcha_digest="synthetic-digest",
        )
        result = self.module._cancel_netease_qr_login(login_id)
        self.assertEqual(result["status"], "cancelled")
        self.assertNotIn(login_id, self.module.QR_LOGIN_ATTEMPTS)
        self.assertEqual(attempt["temporary_cookie"], "")
        self.assertEqual(attempt["last_secure_captcha_digest"], "")

    def test_unknown_qr_status_preserves_sanitized_code_and_message(self):
        self.configure_storage()
        login_id = "synthetic-unknown-login-id"
        self.add_qr_attempt(login_id)
        with mock.patch.object(
            self.module,
            "_netease_qr_request",
            return_value=(
                {
                    "code": 8999,
                    "message": (
                        "changed; MUSIC_U=not-for-clients; "
                        "WNMCID=internal-device-value"
                    ),
                },
                [],
            ),
        ):
            raw = self.module.check_netease_qr_login(login_id)
        result = json.loads(raw)
        self.assertEqual(result["status"], "upstream_unknown")
        self.assertEqual(result["upstream_code"], 8999)
        self.assertTrue(result["polling_stopped"])
        self.assertNotIn("not-for-clients", raw)
        self.assertNotIn("internal-device-value", raw)

    def test_expired_attempt_cleanup_removes_ephemeral_context(self):
        login_id = "synthetic-expired-login-id"
        attempt = self.add_qr_attempt(
            login_id,
            created_monotonic=self.module.time.monotonic()
            - self.module.QR_LOGIN_TTL_SECONDS
            - 1,
        )
        secret_cookie = attempt["temporary_cookie"]
        with self.module.QR_LOGIN_LOCK:
            self.module._cleanup_qr_attempts(self.module.time.monotonic())
        self.assertNotIn(login_id, self.module.QR_LOGIN_ATTEMPTS)
        self.assertEqual(attempt["temporary_cookie"], "")
        self.assertNotEqual(secret_cookie, "")

    def test_logout_clears_runtime_cookie_and_does_not_expose_secrets(self):
        store = self.configure_storage()
        self.module._session_manager().save(
            "MUSIC_U=logout-secret; __csrf=logout-csrf",
            source="qr_login",
        )
        result = self.module.logout_netease()
        persisted = store.load_netease_session()
        self.assertEqual(persisted["status"], "logged_out")
        self.assertEqual(persisted["cookie"], "")
        self.assertIsNone(self.module._session_manager().current())
        self.assertNotIn("logout-secret", result)
        self.assertNotIn("logout-csrf", result)

    def test_status_and_redaction_never_return_session_values(self):
        self.configure_storage()
        self.module._session_manager().save(
            "MUSIC_U=private-value; __csrf=private-csrf",
            source="qr_login",
        )
        with mock.patch.object(
            self.module,
            "verify_netease_session",
            return_value={"authenticated": True, "user_id": 7, "cached": True},
        ):
            status = self.module.get_netease_login_status()
        redacted = self.module._redact_secrets(
            "MUSIC_U=private-value; __csrf=private-csrf"
        )
        for secret in ("private-value", "private-csrf"):
            self.assertNotIn(secret, status)
            self.assertNotIn(secret, redacted)
        self.assertIn("[REDACTED]", redacted)


class PersistenceAuditAndCoverTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.module = load_server("false")
        self.module.STORAGE_PATH = str(Path(self.tempdir.name) / "state.sqlite3")
        self.module.STORE_INSTANCE = None

    def tearDown(self):
        self.module.STORE_INSTANCE = None
        self.tempdir.cleanup()

    def seed_operation(self, operation, arguments, before, after, reversible=True):
        operation_id = str(__import__("uuid").uuid4())
        store = self.module._store()
        store.start_operation(
            {
                "operation_id": operation_id,
                "user_id": 7,
                "operation": operation,
                "sanitized_arguments": arguments,
                "target": {"resource_type": "test"},
                "created_at": utc_now(),
                "before_state": before,
                "reversible": reversible,
                "parent_operation_id": None,
            }
        )
        store.finish_operation(
            operation_id,
            status="success",
            after_state=after,
            result={"status": "success"},
        )
        return operation_id

    def test_write_schemas_are_single_call_and_preview_tool_is_removed(self):
        tools = {tool["name"]: tool for tool in self.module.available_tools()}
        self.assertEqual(
            tools["update_playlist_cover"]["_meta"]["openai/fileParams"], ["image"]
        )
        self.assertNotIn("preview_operation", tools)
        self.assertTrue(tools["undo_operation"]["annotations"]["destructiveHint"])
        for name in self.module.WRITE_TOOL_NAMES:
            self.assertIn("idempotency_key", tools[name]["inputSchema"]["properties"])
            self.assertNotIn("preview_token", tools[name]["inputSchema"]["properties"])

        with self.assertRaises(ValueError) as context:
            self.module._execute_operation_action(
                "update_playlist_cover",
                {"playlist_id": 1},
                {},
                7,
                "operation-id",
                lambda: None,
            )
        self.assertIn("attach the image again and retry", str(context.exception))
        self.assertNotIn("preview", str(context.exception).lower())

    def test_write_mode_startup_requires_persistent_storage_path(self):
        self.module.STORAGE_PATH = ""
        with self.assertRaisesRegex(SystemExit, "MCP_STORAGE_PATH"):
            self.module.validate_startup()
        self.module.STORAGE_PATH = str(Path(self.tempdir.name) / "state.sqlite3")
        self.module.validate_startup()

    def test_direct_write_is_persistent_audited_and_idempotent(self):
        before = {
            "playlist_id": 1,
            "creator_id": 7,
            "name": "Before",
            "description": "Old",
        }
        after = {**before, "name": "After"}
        arguments = {
            "playlist_id": 1,
            "name": "After",
            "idempotency_key": "update-playlist-1",
        }
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_current_state_for_operation", side_effect=[before, after]
        ), mock.patch.object(
            self.module,
            "_execute_operation_action",
            return_value={"success": True},
        ) as action, mock.patch.object(
            self.module, "_after_state_for_operation", return_value=after
        ):
            first = json.loads(self.module.call_tool("update_playlist", arguments))
            replay = json.loads(self.module.call_tool("update_playlist", arguments))
        self.assertEqual(first["status"], "success")
        self.assertEqual(first["operation_id"], replay["operation_id"])
        self.assertTrue(replay["idempotent_replay"])
        action.assert_called_once()
        rows = self.module._store().query_operations(7, limit=10, offset=0)
        self.assertEqual(len(rows), 1)

    def test_legacy_preview_token_is_accepted_and_ignored(self):
        before = {"playlist_id": 1, "creator_id": 7, "name": "A", "description": ""}
        after = {**before, "name": "B"}
        arguments = {
            "playlist_id": 1,
            "name": "B",
            "preview_token": "obsolete-client-token-that-is-not-checked",
        }
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_current_state_for_operation", side_effect=[before, after]
        ), mock.patch.object(
            self.module, "update_playlist", return_value="updated"
        ) as write:
            result = json.loads(self.module.call_tool("update_playlist", arguments))
        self.assertEqual(result["status"], "success")
        write.assert_called_once()

    def test_failure_and_partial_success_are_sanitized_in_log(self):
        before = {"playlist_id": 1, "creator_id": 7, "name": "A", "description": ""}
        partial = {**before, "name": "B"}
        args = {"playlist_id": 1, "name": "B", "description": "C"}
        secret_error = self.module.NetEaseError("upstream echoed " + self.module.NETEASE_COOKIE)

        def fail_after_start(*call_args):
            call_args[-1]()
            raise secret_error

        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module,
            "_current_state_for_operation",
            side_effect=[before, partial],
        ), mock.patch.object(
            self.module, "_execute_operation_action", side_effect=fail_after_start
        ):
            with self.assertRaises(self.module.NetEaseError) as context:
                self.module.call_tool("update_playlist", args)
        self.assertNotIn("MUSIC_U=test", str(context.exception))
        rows = self.module._store().query_operations(7, limit=10, offset=0)
        self.assertEqual(rows[0]["status"], "partial_success")
        self.assertNotIn("MUSIC_U=test", rows[0]["error_summary"])
        self.assertIn("[REDACTED]", rows[0]["error_summary"])

    def test_direct_write_rejects_playlist_without_write_permission(self):
        playlist = {
            "creator": {"userId": 8},
            "name": "Collected",
            "description": "",
            "trackCount": 0,
            "trackIds": [],
        }
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_playlist_detail", return_value=playlist
        ):
            with self.assertRaises(PermissionError):
                self.module.call_tool(
                    "update_playlist", {"playlist_id": 1, "name": "No"}
                )

    def test_like_undo_restores_state_and_cannot_repeat(self):
        before = {"song_id": 9, "liked": False}
        after = {"song_id": 9, "liked": True}
        original_id = self.seed_operation(
            "like_song", {"song_id": 9, "like": True}, before, after
        )
        with mock.patch.object(
            self.module, "_liked_song_state", side_effect=[after, before]
        ), mock.patch.object(self.module, "like_song", return_value="Unliked") as like:
            result = self.module._perform_undo(original_id, 7, "undo-1")
        self.assertTrue(result["success"])
        like.assert_called_once_with(9, False)
        self.assertEqual(
            self.module._store().get_operation(original_id, 7)["undo_status"], "succeeded"
        )
        with self.assertRaisesRegex(ValueError, "already"):
            self.module._perform_undo(original_id, 7, "undo-2")

    def test_formal_undo_is_direct_and_logged_as_its_own_operation(self):
        before = {"song_id": 9, "liked": False}
        after = {"song_id": 9, "liked": True}
        original_id = self.seed_operation(
            "like_song", {"song_id": 9, "like": True}, before, after
        )
        undo_args = {"operation_id": original_id}
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module,
            "_liked_song_state",
            side_effect=[after, after, before],
        ), mock.patch.object(self.module, "like_song", return_value="Unliked"):
            result = json.loads(self.module.call_tool("undo_operation", undo_args))
        self.assertEqual(result["status"], "success")
        rows = self.module._store().query_operations(
            7, limit=10, offset=0, operation="undo_operation"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["parent_operation_id"], original_id)
        self.assertFalse(rows[0]["reversible"])

    def test_playlist_undo_paths_restore_metadata_membership_and_order(self):
        metadata_before = {
            "playlist_id": 1,
            "creator_id": 7,
            "name": "Before",
            "description": "Old",
        }
        metadata_after = {**metadata_before, "name": "After"}
        update_id = self.seed_operation(
            "update_playlist",
            {"playlist_id": 1, "name": "After"},
            metadata_before,
            metadata_after,
        )
        with mock.patch.object(
            self.module,
            "_current_state_for_logged_operation",
            side_effect=[metadata_after, metadata_before],
        ), mock.patch.object(self.module, "update_playlist", return_value="restored") as update:
            self.module._perform_undo(update_id, 7, "undo-update")
        update.assert_called_once_with(1, "Before", "Old")

        tracks_before = {"playlist_id": 1, "creator_id": 7, "track_ids": [1, 2]}
        tracks_after_add = {"playlist_id": 1, "creator_id": 7, "track_ids": [1, 2, 3]}
        add_id = self.seed_operation(
            "add_to_playlist",
            {"playlist_id": 1, "song_ids": [3]},
            tracks_before,
            tracks_after_add,
        )
        with mock.patch.object(
            self.module,
            "_current_state_for_logged_operation",
            side_effect=[tracks_after_add, tracks_before],
        ), mock.patch.object(self.module, "manipulate_playlist", return_value="removed") as manipulate:
            self.module._perform_undo(add_id, 7, "undo-add")
        manipulate.assert_called_once_with("del", 1, [3])

        tracks_before_remove = {
            "playlist_id": 1,
            "creator_id": 7,
            "track_ids": [1, 2, 3],
        }
        tracks_after_remove = {"playlist_id": 1, "creator_id": 7, "track_ids": [1, 3]}
        remove_id = self.seed_operation(
            "remove_from_playlist",
            {"playlist_id": 1, "song_ids": [2]},
            tracks_before_remove,
            tracks_after_remove,
        )
        with mock.patch.object(
            self.module,
            "_current_state_for_logged_operation",
            side_effect=[tracks_after_remove, tracks_before_remove],
        ), mock.patch.object(
            self.module, "manipulate_playlist", return_value="added"
        ) as manipulate, mock.patch.object(
            self.module, "reorder_playlist_tracks", return_value="{}"
        ) as reorder:
            self.module._perform_undo(remove_id, 7, "undo-remove")
        manipulate.assert_called_once_with("add", 1, [2])
        reorder.assert_called_once_with(1, [1, 2, 3])

        reordered = {"playlist_id": 1, "creator_id": 7, "track_ids": [2, 1]}
        reorder_id = self.seed_operation(
            "reorder_playlist_tracks",
            {"playlist_id": 1, "song_ids": [2, 1]},
            tracks_before,
            reordered,
        )
        with mock.patch.object(
            self.module,
            "_current_state_for_logged_operation",
            side_effect=[reordered, tracks_before],
        ), mock.patch.object(
            self.module, "reorder_playlist_tracks", return_value="{}"
        ) as reorder:
            self.module._perform_undo(reorder_id, 7, "undo-reorder")
        reorder.assert_called_once_with(1, [1, 2])

    def test_undo_failure_is_recorded(self):
        before = {"playlist_id": 1, "creator_id": 7, "track_ids": [1, 2]}
        after = {"playlist_id": 1, "creator_id": 7, "track_ids": [2, 1]}
        original_id = self.seed_operation(
            "reorder_playlist_tracks", {"playlist_id": 1, "song_ids": [2, 1]}, before, after
        )
        with mock.patch.object(
            self.module, "_current_state_for_logged_operation", return_value=after
        ), mock.patch.object(
            self.module,
            "reorder_playlist_tracks",
            side_effect=self.module.NetEaseError("upstream failed"),
        ):
            with self.assertRaises(self.module.NetEaseError):
                self.module._perform_undo(original_id, 7, "undo-failed")
        self.assertEqual(
            self.module._store().get_operation(original_id, 7)["undo_status"], "failed"
        )

    def test_private_notes_persist_list_with_song_and_detect_conflict(self):
        args = {
            "playlist_id": 1,
            "song_id": 9,
            "author": "Rain",
            "content": "For the night drive",
        }
        accessible = (7, {"name": "Private mix"}, [9])
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_accessible_playlist", return_value=accessible
        ):
            created = json.loads(self.module.call_tool("create_interaction_note", args))
        note = created["after_state"]
        self.assertEqual(note["version"], 1)

        song = {"id": 9, "name": "Signal", "ar": [{"id": 2, "name": "Artist"}]}
        with mock.patch.object(
            self.module, "_accessible_playlist", return_value=accessible
        ), mock.patch.object(self.module, "_fetch_song_records", return_value=[song]):
            listed = json.loads(self.module.list_interaction_notes(1))
        self.assertFalse(listed["notes"][0]["stale"])
        self.assertEqual(listed["notes"][0]["current_song"]["name"], "Signal")
        with mock.patch.object(
            self.module, "_accessible_playlist", return_value=(7, {"name": "Private mix"}, [])
        ), mock.patch.object(self.module, "_fetch_song_records", return_value=[song]):
            stale = json.loads(self.module.list_interaction_notes(1))
        self.assertTrue(stale["notes"][0]["stale"])

        update_args = {"note_id": note["note_id"], "version": 1, "content": "Revised"}
        self.module._store().update_note(note["note_id"], 7, 1, content="Manual edit")
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_accessible_playlist", return_value=accessible
        ):
            with self.assertRaisesRegex(ValueError, "version is stale"):
                self.module.call_tool("update_interaction_note", update_args)
        rows = self.module._store().query_operations(
            7, limit=10, offset=0, operation="update_interaction_note"
        )
        self.assertEqual(rows[0]["status"], "failed_before_upstream")

    def test_note_soft_delete_and_undo_restore_content_with_new_version(self):
        note = self.module._store().create_note(
            {
                "note_id": "note-delete-test",
                "user_id": 7,
                "playlist_id": 1,
                "song_id": None,
                "author": "Rain",
                "content": "Keep this",
                "visibility": "private",
                "created_at": utc_now(),
            }
        )
        delete_args = {"note_id": note["note_id"], "version": 1}
        accessible = (7, {"name": "Private mix"}, [])
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_accessible_playlist", return_value=accessible
        ):
            deleted = json.loads(self.module.call_tool("delete_interaction_note", delete_args))
        self.assertIsNotNone(deleted["after_state"]["deleted_at"])
        original_id = deleted["operation_id"]

        with mock.patch.object(self.module, "get_uid", return_value=7):
            restored = json.loads(
                self.module.call_tool(
                    "undo_operation", {"operation_id": original_id}
                )
            )
        self.assertIsNone(restored["after_state"]["deleted_at"])
        self.assertEqual(restored["after_state"]["content"], "Keep this")
        self.assertGreater(restored["after_state"]["version"], 1)

    def test_cover_image_validation_and_normalization(self):
        source = io.BytesIO()
        Image.new("RGB", (640, 320), (10, 20, 30)).save(
            source, format="PNG", pnginfo=None
        )
        reference = {
            "download_url": "https://files.example.test/image",
            "file_id": "file_test",
            "mime_type": "image/png",
            "file_name": "cover.png",
        }
        with mock.patch.object(
            image_safety, "download_file_reference", return_value=source.getvalue()
        ):
            output, metadata = image_safety.normalize_cover_image(
                reference, max_bytes=5_000_000, max_pixels=25_000_000
            )
        with Image.open(io.BytesIO(output)) as result:
            self.assertEqual(result.format, "JPEG")
            self.assertEqual(result.size, (300, 300))
            self.assertFalse(result.getexif())
        self.assertTrue(metadata["center_cropped"])
        self.assertTrue(metadata["metadata_removed"])

        mismatched = {**reference, "file_name": "cover.jpg"}
        with mock.patch.object(
            image_safety, "download_file_reference", return_value=source.getvalue()
        ):
            with self.assertRaisesRegex(ValueError, "extension"):
                image_safety.normalize_cover_image(
                    mismatched, max_bytes=5_000_000, max_pixels=25_000_000
                )
        with mock.patch.object(
            image_safety, "download_file_reference", return_value=source.getvalue()
        ):
            with self.assertRaisesRegex(ValueError, "file-size"):
                image_safety.normalize_cover_image(
                    reference, max_bytes=10, max_pixels=25_000_000
                )
        with self.assertRaisesRegex(ValueError, "public HTTPS"):
            image_safety._validate_public_https_url("http://127.0.0.1/private.png")
        with mock.patch.object(
            image_safety, "download_file_reference", return_value=source.getvalue()
        ):
            with self.assertRaisesRegex(ValueError, "safe PNG or JPEG"):
                image_safety.normalize_cover_image(
                    reference, max_bytes=5_000_000, max_pixels=1_000
                )

    def test_cover_direct_write_is_irreversible_and_upload_uses_nos_token_privately(self):
        before = {"playlist_id": 1, "creator_id": 7, "cover_image_id": 10, "cover_image_url": None}
        after = {"playlist_id": 1, "creator_id": 7, "cover_image_id": 55, "cover_image_url": None}
        args = {
            "playlist_id": 1,
            "image": {
                "download_url": "https://files.example.test/image",
                "file_id": "file_cover",
                "mime_type": "image/png",
                "file_name": "cover.png",
            },
        }
        meta = {
            "final_format": "JPEG",
            "final_mime_type": "image/jpeg",
            "final_width": 300,
            "final_height": 300,
        }
        action_result = {
            "success": True,
            "reversible": False,
            "upstream_image_id": 55,
            "image": meta,
        }
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_current_state_for_operation", side_effect=[before, after]
        ), mock.patch.object(
            self.module, "normalize_cover_image", return_value=(b"jpeg", meta)
        ), mock.patch.object(
            self.module, "_upload_playlist_cover", return_value=action_result
        ):
            direct = json.loads(self.module.call_tool("update_playlist_cover", args))
        self.assertEqual(direct["status"], "success")
        self.assertFalse(direct["reversible"])
        record = self.module._store().get_operation(direct["operation_id"], 7)
        self.assertNotIn("download_url", json.dumps(record["sanitized_arguments"]))

        allocation = {
            "code": 200,
            "result": {"objectKey": "safe/key", "token": "temporary-nos-token", "docId": "55"},
        }
        with mock.patch.object(
            self.module, "netease_request", side_effect=[allocation, {"code": 200}]
        ), mock.patch.object(
            self.module, "netease_binary_request", return_value={"code": 200}
        ) as upload:
            result = self.module._upload_playlist_cover(1, b"jpeg", meta)
        self.assertTrue(result["success"])
        self.assertFalse(result["reversible"])
        self.assertEqual(upload.call_args.args[2]["x-nos-token"], "temporary-nos-token")
        self.assertNotIn("temporary-nos-token", json.dumps(result))

    def test_operation_log_survives_store_reopen_and_filters(self):
        operation_id = self.seed_operation(
            "like_song",
            {"song_id": 9, "like": True},
            {"song_id": 9, "liked": False},
            {"song_id": 9, "liked": True},
        )
        self.module.STORE_INSTANCE = None
        self.assertIsInstance(
            PersistentStore(self.module.STORAGE_PATH).get_operation(operation_id, 7), dict
        )
        with mock.patch.object(self.module, "get_uid", return_value=7):
            payload = json.loads(
                self.module.get_operation_log(operation="like_song", status="success")
            )
        self.assertEqual(payload["returned"], 1)
        operation = payload["operations"][0]
        self.assertEqual(operation["operation_id"], operation_id)
        self.assertEqual(payload["timestamp_storage"], "UTC")
        self.assertEqual(payload["timezone"], "Asia/Shanghai")
        self.assertEqual(operation["created_at"], operation["created_at_utc"])
        self.assertEqual(operation["completed_at"], operation["completed_at_utc"])
        self.assertEqual(operation["created_at_timezone"], "Asia/Shanghai")
        self.assertEqual(operation["created_at_utc_offset"], "+08:00")
        self.assertTrue(operation["created_at_local"].endswith("+08:00"))


class DirectWriteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.module = load_server("false")
        self.module.STORAGE_PATH = str(Path(self.tempdir.name) / "direct.sqlite3")
        self.module.STORE_INSTANCE = None

    def tearDown(self):
        self.module.STORE_INSTANCE = None
        self.tempdir.cleanup()

    def curated_arguments(self, song_ids=None, key="curated-playlist-1"):
        return {
            "name": "Night Signals",
            "description": "A deliberate late-night sequence.",
            "privacy": 10,
            "song_ids": [11, 22, 33] if song_ids is None else song_ids,
            "idempotency_key": key,
        }

    @staticmethod
    def curated_state(arguments, song_ids=None, *, description=None, name=None):
        ids = list(arguments["song_ids"] if song_ids is None else song_ids)
        return {
            "playlist_id": 901,
            "creator_id": 7,
            "name": arguments["name"] if name is None else name,
            "description": (
                arguments["description"] if description is None else description
            ),
            "privacy": arguments["privacy"],
            "track_ids": ids,
            "track_count": len(ids),
        }

    def call_successful_curated(self, arguments, snapshot_side_effect):
        with mock.patch.object(
            self.module, "get_uid", return_value=7
        ), mock.patch.object(
            self.module,
            "_fetch_song_records",
            return_value=[{"id": song_id} for song_id in arguments["song_ids"]],
        ), mock.patch.object(
            self.module,
            "netease_request",
            return_value={
                "code": 200,
                "playlist": {
                    "id": 901,
                    "name": arguments["name"],
                    "description": arguments["description"],
                },
            },
        ) as create, mock.patch.object(
            self.module, "manipulate_playlist", return_value="added"
        ) as add, mock.patch.object(
            self.module,
            "_curated_playlist_snapshot",
            side_effect=snapshot_side_effect,
        ), mock.patch.object(
            self.module, "reorder_playlist_tracks", return_value='{"success": true}'
        ) as reorder, mock.patch.object(
            self.module, "update_playlist", return_value="updated"
        ) as update:
            result = json.loads(
                self.module.call_tool("create_curated_playlist", arguments)
            )
        return result, create, add, reorder, update

    def test_create_curated_playlist_success(self):
        arguments = self.curated_arguments()
        state = self.curated_state(arguments)
        result, create, add, reorder, update = self.call_successful_curated(
            arguments, [state, state]
        )

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["result"]["success"])
        self.assertEqual(result["result"]["stage"], "completed")
        self.assertEqual(result["result"]["playlist_id"], 901)
        self.assertTrue(all(result["result"]["completed"].values()))
        self.assertEqual(result["after_state"]["track_ids"], arguments["song_ids"])
        create.assert_called_once()
        add.assert_called_once_with("add", 901, arguments["song_ids"])
        reorder.assert_not_called()
        update.assert_called_once_with(
            901, description=arguments["description"]
        )
        record = self.module._store().get_operation(result["operation_id"], 7)
        self.assertEqual(record["operation"], "create_curated_playlist")
        self.assertEqual(record["status"], "success")
        self.assertFalse(record["reversible"])
        self.assertEqual(
            record["sanitized_arguments"]["song_ids"], arguments["song_ids"]
        )
        self.assertTrue(record["created_at"].endswith("Z"))
        self.assertTrue(record["completed_at"].endswith("Z"))
        self.assertNotIn("created_at_local", record)
        with mock.patch.object(self.module, "get_uid", return_value=7):
            audit = json.loads(
                self.module.get_operation_log(
                    operation="create_curated_playlist", status="success"
                )
            )
        audited = audit["operations"][0]
        self.assertEqual(audited["created_at"], audited["created_at_utc"])
        self.assertEqual(audited["completed_at"], audited["completed_at_utc"])
        self.assertEqual(audited["created_at_timezone"], "Asia/Shanghai")
        self.assertTrue(audited["created_at_local"].endswith("+08:00"))

    def test_create_curated_playlist_restores_reversed_add_order(self):
        arguments = self.curated_arguments()
        reversed_state = self.curated_state(
            arguments, list(reversed(arguments["song_ids"]))
        )
        final_state = self.curated_state(arguments)
        result, _, _, reorder, _ = self.call_successful_curated(
            arguments, [reversed_state, final_state]
        )

        self.assertTrue(result["result"]["reordered_after_add"])
        self.assertEqual(result["after_state"]["track_ids"], arguments["song_ids"])
        reorder.assert_called_once_with(901, arguments["song_ids"])

    def test_create_curated_playlist_updates_description_when_create_ignored_it(self):
        arguments = self.curated_arguments()
        after_add = self.curated_state(arguments, description="")
        final_state = self.curated_state(arguments)
        result, _, _, _, update = self.call_successful_curated(
            arguments, [after_add, final_state]
        )

        self.assertTrue(result["result"]["verification"]["description"])
        update.assert_called_once_with(
            901, description=arguments["description"]
        )

    def test_create_curated_playlist_reports_add_failure_and_retains_playlist(self):
        arguments = self.curated_arguments()
        with mock.patch.object(
            self.module, "get_uid", return_value=7
        ), mock.patch.object(
            self.module,
            "_fetch_song_records",
            return_value=[{"id": song_id} for song_id in arguments["song_ids"]],
        ), mock.patch.object(
            self.module,
            "netease_request",
            return_value={"code": 200, "playlist": {"id": 901}},
        ) as create, mock.patch.object(
            self.module,
            "manipulate_playlist",
            side_effect=self.module.NetEaseError("add failed"),
        ) as add:
            with self.assertRaises(self.module.NetEaseError) as context:
                self.module.call_tool("create_curated_playlist", arguments)
            with self.assertRaises(self.module.NetEaseError) as replay_context:
                self.module.call_tool("create_curated_playlist", arguments)

        failure = json.loads(str(context.exception))
        replay = json.loads(str(replay_context.exception))
        self.assertFalse(failure["success"])
        self.assertEqual(failure["status"], "partial_success")
        self.assertEqual(failure["stage"], "adding_songs")
        self.assertEqual(failure["playlist_id"], 901)
        self.assertTrue(failure["completed"]["playlist_created"])
        self.assertFalse(failure["completed"]["songs_added"])
        self.assertTrue(failure["recovery"]["playlist_retained"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertTrue(replay["retry_suppressed"])
        create.assert_called_once()
        add.assert_called_once()
        partial = self.module._store().get_operation_by_idempotency(
            7,
            "create_curated_playlist",
            self.module._idempotency_key_hash(arguments["idempotency_key"]),
        )
        self.assertEqual(partial["status"], "partial_success")
        self.assertTrue(partial["created_at"].endswith("Z"))
        self.assertTrue(partial["completed_at"].endswith("Z"))
        self.assertNotIn("created_at_local", partial)

    def test_create_curated_playlist_reports_reorder_failure(self):
        arguments = self.curated_arguments()
        reversed_state = self.curated_state(
            arguments, list(reversed(arguments["song_ids"]))
        )
        with mock.patch.object(
            self.module, "get_uid", return_value=7
        ), mock.patch.object(
            self.module,
            "_fetch_song_records",
            return_value=[{"id": song_id} for song_id in arguments["song_ids"]],
        ), mock.patch.object(
            self.module,
            "netease_request",
            return_value={"code": 200, "playlist": {"id": 901}},
        ), mock.patch.object(
            self.module, "manipulate_playlist", return_value="added"
        ), mock.patch.object(
            self.module, "_curated_playlist_snapshot", return_value=reversed_state
        ), mock.patch.object(
            self.module,
            "reorder_playlist_tracks",
            side_effect=self.module.NetEaseError("reorder failed"),
        ):
            with self.assertRaises(self.module.NetEaseError) as context:
                self.module.call_tool("create_curated_playlist", arguments)

        failure = json.loads(str(context.exception))
        self.assertEqual(failure["stage"], "reordering_tracks")
        self.assertTrue(failure["completed"]["songs_added"])
        self.assertFalse(failure["completed"]["order_restored"])
        self.assertEqual(
            failure["after_state"]["track_ids"],
            list(reversed(arguments["song_ids"])),
        )

    def test_create_curated_playlist_reports_description_update_failure(self):
        arguments = self.curated_arguments()
        after_add = self.curated_state(arguments, description="")
        with mock.patch.object(
            self.module, "get_uid", return_value=7
        ), mock.patch.object(
            self.module,
            "_fetch_song_records",
            return_value=[{"id": song_id} for song_id in arguments["song_ids"]],
        ), mock.patch.object(
            self.module,
            "netease_request",
            return_value={"code": 200, "playlist": {"id": 901}},
        ), mock.patch.object(
            self.module, "manipulate_playlist", return_value="added"
        ), mock.patch.object(
            self.module, "_curated_playlist_snapshot", return_value=after_add
        ), mock.patch.object(
            self.module,
            "update_playlist",
            side_effect=self.module.NetEaseError("description failed"),
        ):
            with self.assertRaises(self.module.NetEaseError) as context:
                self.module.call_tool("create_curated_playlist", arguments)

        failure = json.loads(str(context.exception))
        self.assertEqual(failure["stage"], "updating_description")
        self.assertTrue(failure["completed"]["order_restored"])
        self.assertFalse(failure["completed"]["description_updated"])
        self.assertEqual(
            failure["recovery"]["suggested_tools"],
            ["update_playlist", "get_playlist_songs"],
        )

    def test_create_curated_playlist_reports_final_verification_failure(self):
        arguments = self.curated_arguments()
        after_add = self.curated_state(arguments, description="")
        wrong_final = self.curated_state(
            arguments,
            [11, 33],
            description="wrong",
            name="wrong",
        )
        with mock.patch.object(
            self.module, "get_uid", return_value=7
        ), mock.patch.object(
            self.module,
            "_fetch_song_records",
            return_value=[{"id": song_id} for song_id in arguments["song_ids"]],
        ), mock.patch.object(
            self.module,
            "netease_request",
            return_value={"code": 200, "playlist": {"id": 901}},
        ), mock.patch.object(
            self.module, "manipulate_playlist", return_value="added"
        ), mock.patch.object(
            self.module,
            "_curated_playlist_snapshot",
            side_effect=[after_add, wrong_final],
        ), mock.patch.object(
            self.module, "update_playlist", return_value="updated"
        ):
            with self.assertRaises(self.module.NetEaseError) as context:
                self.module.call_tool("create_curated_playlist", arguments)

        failure = json.loads(str(context.exception))
        self.assertEqual(failure["stage"], "verifying_final_state")
        self.assertTrue(failure["completed"]["description_updated"])
        self.assertFalse(failure["completed"]["final_state_verified"])
        self.assertIn("name", failure["error_summary"])
        self.assertIn("description", failure["error_summary"])
        self.assertIn("song_set", failure["error_summary"])
        self.assertIn("song_count", failure["error_summary"])
        self.assertIn("song_order", failure["error_summary"])

    def test_create_curated_playlist_idempotent_retry_does_not_create_twice(self):
        arguments = self.curated_arguments(key="curated-retry-1")
        state = self.curated_state(arguments)
        with mock.patch.object(
            self.module, "get_uid", return_value=7
        ), mock.patch.object(
            self.module,
            "_fetch_song_records",
            return_value=[{"id": song_id} for song_id in arguments["song_ids"]],
        ), mock.patch.object(
            self.module,
            "netease_request",
            return_value={"code": 200, "playlist": {"id": 901}},
        ) as create, mock.patch.object(
            self.module, "manipulate_playlist", return_value="added"
        ) as add, mock.patch.object(
            self.module, "_curated_playlist_snapshot", side_effect=[state, state]
        ), mock.patch.object(
            self.module, "update_playlist", return_value="updated"
        ):
            first = json.loads(
                self.module.call_tool("create_curated_playlist", arguments)
            )
            replay = json.loads(
                self.module.call_tool("create_curated_playlist", arguments)
            )

        self.assertEqual(first["operation_id"], replay["operation_id"])
        self.assertTrue(replay["idempotent_replay"])
        create.assert_called_once()
        add.assert_called_once()
        records = self.module._store().query_operations(
            7,
            limit=10,
            offset=0,
            operation="create_curated_playlist",
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["operation_id"], first["operation_id"])
        self.assertTrue(records[0]["created_at"].endswith("Z"))
        self.assertTrue(records[0]["completed_at"].endswith("Z"))

    def test_create_curated_playlist_rejects_duplicate_song_ids(self):
        arguments = self.curated_arguments([11, 22, 11])
        with mock.patch.object(self.module, "netease_request") as upstream:
            with self.assertRaisesRegex(ValueError, "duplicates"):
                self.module.call_tool("create_curated_playlist", arguments)
        upstream.assert_not_called()

    def test_create_curated_playlist_preserves_nontrivial_input_order(self):
        arguments = self.curated_arguments([33, 11, 22], key="curated-order-1")
        original_order = list(arguments["song_ids"])
        state = self.curated_state(arguments)
        result, _, add, _, _ = self.call_successful_curated(
            arguments, [state, state]
        )

        self.assertEqual(arguments["song_ids"], original_order)
        self.assertEqual(result["after_state"]["track_ids"], original_order)
        add.assert_called_once_with("add", 901, original_order)

    def test_create_curated_playlist_validates_empty_limits_ids_and_key(self):
        cases = {
            "empty": self.curated_arguments([]),
            "too many": self.curated_arguments(list(range(1, 52))),
            "non-positive": self.curated_arguments([1, 0]),
        }
        for label, arguments in cases.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.module.call_tool("create_curated_playlist", arguments)
        missing_key = self.curated_arguments()
        missing_key.pop("idempotency_key")
        with self.assertRaisesRegex(ValueError, "idempotency_key is required"):
            self.module.call_tool("create_curated_playlist", missing_key)
        for required_field in ("description", "privacy"):
            missing_field = self.curated_arguments()
            missing_field.pop(required_field)
            with self.subTest(required_field=required_field), self.assertRaisesRegex(
                ValueError, required_field
            ):
                self.module.call_tool("create_curated_playlist", missing_field)

        arguments = self.curated_arguments(key="curated-invalid-song-1")
        with mock.patch.object(
            self.module, "get_uid", return_value=7
        ), mock.patch.object(
            self.module, "_fetch_song_records", return_value=[{"id": 11}, {"id": 33}]
        ), mock.patch.object(self.module, "netease_request") as upstream:
            with self.assertRaises(self.module.NetEaseError) as context:
                self.module.call_tool("create_curated_playlist", arguments)
        failure = json.loads(str(context.exception))
        self.assertEqual(failure["status"], "failed_before_upstream")
        self.assertEqual(failure["stage"], "validating_songs")
        self.assertIsNone(failure["playlist_id"])
        upstream.assert_not_called()

    def test_deprecated_preview_environment_is_ignored(self):
        module = load_server(
            "false",
            legacy_preview_env={
                "MCP_WRITE_PREVIEW_POLICY": "invalid-old-value",
                "MCP_REQUIRE_WRITE_PREVIEW": "true",
                "MCP_PREVIEW_TTL_SECONDS": "not-an-integer",
                "MCP_MAX_PENDING_PREVIEWS": "not-an-integer",
            },
        )
        module.STORAGE_PATH = str(Path(self.tempdir.name) / "deprecated.sqlite3")
        with self.assertLogs(module.LOG, level="WARNING") as captured:
            module.validate_startup()
        message = " ".join(captured.output)
        self.assertIn("deprecated write-preview", message)
        self.assertIn("MCP_WRITE_PREVIEW_POLICY", message)
        with mock.patch.object(
            module, "_execute_direct_write", return_value="direct"
        ) as execute:
            self.assertEqual(module.call_tool("create_playlist", {"name": "Synthetic"}), "direct")
        execute.assert_called_once()

    def test_every_write_tool_routes_to_one_call_without_preview(self):
        with mock.patch.object(
            self.module, "_execute_direct_write", return_value="direct"
        ) as execute:
            for name in sorted(self.module.WRITE_TOOL_NAMES):
                with self.subTest(name=name):
                    self.assertEqual(self.module.call_tool(name, {}), "direct")
        self.assertEqual(execute.call_count, len(self.module.WRITE_TOOL_NAMES))
        self.assertTrue(
            all(
                call.args[0] in self.module.WRITE_TOOL_NAMES
                for call in execute.call_args_list
            )
        )

    def test_create_and_add_enter_mock_write_flow_without_preview_token(self):
        with mock.patch.object(
            self.module, "_execute_direct_write", return_value="direct"
        ) as execute:
            self.assertEqual(
                self.module.call_tool("create_playlist", {"name": "Synthetic playlist"}),
                "direct",
            )
            self.assertEqual(
                self.module.call_tool(
                    "add_to_playlist", {"playlist_id": 9, "song_ids": [1]}
                ),
                "direct",
            )
        self.assertEqual(
            execute.call_args_list,
            [
                mock.call("create_playlist", {"name": "Synthetic playlist"}),
                mock.call("add_to_playlist", {"playlist_id": 9, "song_ids": [1]}),
            ],
        )

    def test_remove_and_reorder_are_direct_and_post_verified(self):
        before_remove = {"playlist_id": 9, "creator_id": 7, "track_ids": [1, 2]}
        after_remove = {**before_remove, "track_ids": [1]}
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module,
            "_current_state_for_operation",
            side_effect=[before_remove, after_remove],
        ), mock.patch.object(
            self.module, "manipulate_playlist", return_value="removed"
        ) as remove:
            removed = json.loads(
                self.module.call_tool(
                    "remove_from_playlist", {"playlist_id": 9, "song_ids": [2]}
                )
            )
        self.assertEqual(removed["status"], "success")
        remove.assert_called_once_with("del", 9, [2])

        before_reorder = {"playlist_id": 9, "creator_id": 7, "track_ids": [1, 2]}
        after_reorder = {**before_reorder, "track_ids": [2, 1]}
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module,
            "_current_state_for_operation",
            side_effect=[before_reorder, after_reorder],
        ), mock.patch.object(
            self.module,
            "reorder_playlist_tracks",
            return_value='{"success": true}',
        ) as reorder:
            reordered = json.loads(
                self.module.call_tool(
                    "reorder_playlist_tracks",
                    {"playlist_id": 9, "song_ids": [2, 1]},
                )
            )
        self.assertEqual(reordered["status"], "success")
        reorder.assert_called_once_with(9, [2, 1])

    def test_owned_add_one_and_fifty_songs_succeed_and_are_logged(self):
        for count in (1, 50):
            song_ids = list(range(1, count + 1))
            before = {"playlist_id": 9, "creator_id": 7, "track_ids": []}
            after = {**before, "track_ids": song_ids}
            with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
                self.module, "_current_state_for_operation", side_effect=[before, after]
            ), mock.patch.object(
                self.module,
                "_fetch_song_records",
                return_value=[{"id": song_id} for song_id in song_ids],
            ), mock.patch.object(
                self.module, "manipulate_playlist", return_value="added"
            ) as write:
                result = json.loads(
                    self.module.call_tool(
                        "add_to_playlist",
                        {
                            "playlist_id": 9,
                            "song_ids": song_ids,
                            "idempotency_key": f"add-count-{count}",
                        },
                    )
                )
            self.assertEqual(result["status"], "success")
            self.assertTrue(result["upstream_action_started"])
            self.assertTrue(result["reversible"])
            write.assert_called_once_with("add", 9, song_ids)
            record = self.module._store().get_operation(result["operation_id"], 7)
            self.assertEqual(record["status"], "success")
            self.assertTrue(record["upstream_action_started"])

    def test_add_succeeds_when_upstream_inserts_new_track_first(self):
        before = {"playlist_id": 9, "creator_id": 7, "track_ids": [10, 20]}
        after = {**before, "track_ids": [30, 10, 20]}
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_current_state_for_operation", side_effect=[before, after]
        ), mock.patch.object(
            self.module, "_fetch_song_records", return_value=[{"id": 30}]
        ), mock.patch.object(
            self.module, "manipulate_playlist", return_value="added"
        ):
            result = json.loads(
                self.module.call_tool(
                    "add_to_playlist",
                    {
                        "playlist_id": 9,
                        "song_ids": [30],
                        "idempotency_key": "add-at-front-1",
                    },
                )
            )

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["result"]["order_changed_by_upstream"])
        self.assertEqual(result["after_state"]["track_ids"], [30, 10, 20])
        record = self.module._store().get_operation(result["operation_id"], 7)
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["before"]["track_ids"], [10, 20])
        self.assertEqual(record["after"]["track_ids"], [30, 10, 20])

    def test_add_verifier_rejects_membership_count_and_read_anomalies(self):
        before = {"playlist_id": 9, "creator_id": 7, "track_ids": [10, 20]}
        expected = {**before, "track_ids": [10, 20, 30, 40]}
        cases = {
            "partial targets": {**before, "track_ids": [30, 10, 20]},
            "original missing": {**before, "track_ids": [30, 40, 10]},
            "target duplicated": {**before, "track_ids": [30, 30, 40, 10, 20]},
            "state unavailable": None,
        }
        for name, after in cases.items():
            with self.subTest(name=name), self.assertRaises(self.module.NetEaseError):
                self.module._verify_added_playlist_tracks(
                    before, expected, after, [30, 40]
                )

    def test_add_limits_ownership_duplicates_and_existing_tracks(self):
        with self.assertRaisesRegex(ValueError, "duplicates"):
            self.module.call_tool(
                "add_to_playlist", {"playlist_id": 9, "song_ids": [1, 1]}
            )

        collected = {
            "creator": {"userId": 8},
            "trackCount": 0,
            "trackIds": [],
        }
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_playlist_detail", return_value=collected
        ):
            with self.assertRaises(PermissionError):
                self.module.call_tool(
                    "add_to_playlist", {"playlist_id": 9, "song_ids": [1]}
                )

        existing = {"playlist_id": 9, "creator_id": 7, "track_ids": [1]}
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_current_state_for_operation", side_effect=[existing, existing]
        ), mock.patch.object(self.module, "manipulate_playlist") as write:
            result = json.loads(
                self.module.call_tool(
                    "add_to_playlist", {"playlist_id": 9, "song_ids": [1]}
                )
            )
        self.assertFalse(result["result"]["changed"])
        self.assertFalse(result["upstream_action_started"])
        write.assert_not_called()

    def test_add_failure_boundaries_are_distinct(self):
        before = {"playlist_id": 9, "creator_id": 7, "track_ids": []}
        changed = {**before, "track_ids": [1]}

        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_current_state_for_operation", return_value=before
        ), mock.patch.object(self.module, "_fetch_song_records", return_value=[]):
            with self.assertRaisesRegex(ValueError, "did not recognize"):
                self.module.call_tool(
                    "add_to_playlist",
                    {
                        "playlist_id": 9,
                        "song_ids": [1],
                        "idempotency_key": "before-upstream-1",
                    },
                )
        failed = self.module._store().get_operation_by_idempotency(
            7,
            "add_to_playlist",
            self.module._idempotency_key_hash("before-upstream-1"),
        )
        self.assertEqual(failed["status"], "failed_before_upstream")
        self.assertFalse(failed["upstream_action_started"])

        def unknown_after_start(*call_args):
            call_args[-1]()
            raise self.module.UpstreamOutcomeUnknown("timeout")

        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_current_state_for_operation", side_effect=[before, before]
        ), mock.patch.object(
            self.module, "_execute_operation_action", side_effect=unknown_after_start
        ) as action:
            arguments = {
                "playlist_id": 9,
                "song_ids": [1],
                "idempotency_key": "unknown-result-1",
            }
            with self.assertRaises(self.module.NetEaseError):
                self.module.call_tool("add_to_playlist", arguments)
            with self.assertRaisesRegex(self.module.NetEaseError, "not send another write"):
                self.module.call_tool("add_to_playlist", arguments)
        self.assertEqual(action.call_count, 1)
        unknown = self.module._store().get_operation_by_idempotency(
            7,
            "add_to_playlist",
            self.module._idempotency_key_hash("unknown-result-1"),
        )
        self.assertEqual(unknown["status"], "unknown")
        self.assertTrue(unknown["upstream_action_started"])

        def partial_after_start(*call_args):
            call_args[-1]()
            raise self.module.NetEaseError("confirmed failure after partial change")

        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_current_state_for_operation", side_effect=[before, changed]
        ), mock.patch.object(
            self.module, "_execute_operation_action", side_effect=partial_after_start
        ) as action:
            arguments = {
                "playlist_id": 9,
                "song_ids": [1],
                "idempotency_key": "partial-result-1",
            }
            with self.assertRaises(self.module.NetEaseError):
                self.module.call_tool("add_to_playlist", arguments)
            with self.assertRaisesRegex(self.module.NetEaseError, "not send another write"):
                self.module.call_tool("add_to_playlist", arguments)
        self.assertEqual(action.call_count, 1)
        partial = self.module._store().get_operation_by_idempotency(
            7,
            "add_to_playlist",
            self.module._idempotency_key_hash("partial-result-1"),
        )
        self.assertEqual(partial["status"], "partial_success")

    def test_like_and_unlike_are_both_direct(self):
        before = {"song_id": 3, "liked": False}
        after = {"song_id": 3, "liked": True}
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_current_state_for_operation", side_effect=[before, after]
        ), mock.patch.object(self.module, "like_song", return_value="liked"):
            result = json.loads(
                self.module.call_tool(
                    "like_song",
                    {
                        "song_id": 3,
                        "like": True,
                        "idempotency_key": "like-song-3",
                    },
                )
            )
        self.assertTrue(result["reversible"])
        record = self.module._store().get_operation(result["operation_id"], 7)
        self.assertEqual(record["before"], before)
        self.assertEqual(record["after"], after)
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_current_state_for_operation", side_effect=[after, before]
        ), mock.patch.object(self.module, "like_song", return_value="unliked") as unlike:
            unliked = json.loads(
                self.module.call_tool("like_song", {"song_id": 3, "like": False})
            )
        self.assertEqual(unliked["status"], "success")
        unlike.assert_called_once_with(3, False)

    def test_private_note_direct_validation_readback_and_idempotency(self):
        accessible = (7, {"name": "Private list"}, [11])
        base = {
            "playlist_id": 9,
            "song_id": 11,
            "author": "Rain",
            "content": "A private recommendation",
            "visibility": "private",
        }
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_accessible_playlist", return_value=accessible
        ) as access:
            first = json.loads(
                self.module.call_tool(
                    "create_interaction_note",
                    {**base, "idempotency_key": "private-note-1"},
                )
            )
            replay = json.loads(
                self.module.call_tool(
                    "create_interaction_note",
                    {**base, "idempotency_key": "private-note-1"},
                )
            )
            with self.assertRaisesRegex(ValueError, "different arguments"):
                self.module.call_tool(
                    "create_interaction_note",
                    {
                        **base,
                        "content": "Different intent",
                        "idempotency_key": "private-note-1",
                    },
                )
            second = json.loads(
                self.module.call_tool(
                    "create_interaction_note",
                    {**base, "idempotency_key": "private-note-2"},
                )
            )
        self.assertEqual(first["operation_id"], replay["operation_id"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertNotEqual(first["operation_id"], second["operation_id"])
        self.assertEqual(access.call_count, 2)
        notes = self.module._store().list_notes(
            7, 9, song_id=11, author=None, limit=10, offset=0
        )
        self.assertEqual(len(notes), 2)
        self.assertTrue(first["reversible"])
        with mock.patch.object(
            self.module, "_accessible_playlist", return_value=accessible
        ), mock.patch.object(
            self.module,
            "_fetch_song_records",
            return_value=[{"id": 11, "name": "Synthetic", "ar": []}],
        ):
            listed = json.loads(
                self.module.list_interaction_notes(9, song_id=11)
            )
        self.assertEqual(listed["returned"], 2)
        note = listed["notes"][0]
        self.assertEqual(note["created_at"], note["created_at_utc"])
        self.assertEqual(note["created_at_timezone"], "Asia/Shanghai")
        self.assertEqual(note["created_at_utc_offset"], "+08:00")
        self.assertTrue(note["created_at_local"].endswith("+08:00"))

        with self.assertRaisesRegex(ValueError, "supports only private"):
            self.module.call_tool(
                "create_interaction_note", {**base, "visibility": "public"}
            )
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_accessible_playlist", return_value=(7, {}, []),
        ):
            with self.assertRaisesRegex(ValueError, "not currently in"):
                self.module.call_tool("create_interaction_note", base)
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module,
            "_accessible_playlist",
            side_effect=PermissionError("playlist is not accessible"),
        ):
            with self.assertRaises(PermissionError):
                self.module.call_tool("create_interaction_note", base)

    def test_existing_sqlite_schema_migrates_automatically(self):
        path = Path(self.tempdir.name) / "old.sqlite3"
        interrupted_at = datetime.fromtimestamp(
            time.time() - 7200, timezone.utc
        ).isoformat().replace("+00:00", "Z")
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "CREATE TABLE operations (operation_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
                "operation TEXT NOT NULL, sanitized_arguments_json TEXT NOT NULL, target_json TEXT, "
                "created_at TEXT NOT NULL, completed_at TEXT, status TEXT NOT NULL, before_json TEXT, "
                "after_json TEXT, reversible INTEGER NOT NULL, undo_status TEXT NOT NULL DEFAULT "
                "'not_requested', undo_operation_id TEXT, error_summary TEXT, result_json TEXT, "
                "parent_operation_id TEXT)"
            )
            connection.execute(
                "INSERT INTO operations (operation_id, user_id, operation, "
                "sanitized_arguments_json, created_at, status, reversible) "
                "VALUES (?, ?, ?, ?, ?, 'started', 1)",
                (
                    "legacy-started",
                    "7",
                    "like_song",
                    '{"like":true,"song_id":1}',
                    interrupted_at,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        store = PersistentStore(str(path))
        store.initialize()
        connection = sqlite3.connect(path)
        try:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(operations)")
            }
            indexes = {
                row[1] for row in connection.execute("PRAGMA index_list(operations)")
            }
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            legacy_row = connection.execute(
                "SELECT operation, sanitized_arguments_json FROM operations "
                "WHERE operation_id='legacy-started'"
            ).fetchone()
        finally:
            connection.close()
        self.assertIn("upstream_action_started", columns)
        self.assertIn("idempotency_key", columns)
        self.assertIn("operations_idempotency_idx", indexes)
        self.assertIn("netease_session", tables)
        self.assertEqual(legacy_row, ("like_song", '{"like":true,"song_id":1}'))
        self.assertIsNone(store.load_netease_session())

        for operation_id in ("before-crash", "after-crash"):
            store.start_operation(
                {
                    "operation_id": operation_id,
                    "user_id": 7,
                    "operation": "like_song",
                    "sanitized_arguments": {"song_id": 1, "like": True},
                    "target": {"song_id": 1},
                    "created_at": interrupted_at,
                    "before_state": {"song_id": 1, "liked": False},
                    "reversible": True,
                    "parent_operation_id": None,
                }
            )
        store.mark_upstream_action_started("after-crash")
        store.cleanup()
        self.assertEqual(
            store.get_operation("before-crash", 7)["status"],
            "failed_before_upstream",
        )
        self.assertEqual(store.get_operation("after-crash", 7)["status"], "unknown")
        self.assertEqual(store.get_operation("legacy-started", 7)["status"], "unknown")


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.module = load_server("true")
        self.httpd = self.module.ThreadingHTTPServer(("127.0.0.1", 0), self.module.MCPHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def post(self, payload, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            self.base + "/mcp",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=2)

    def add_browser_login_attempt(self, login_id):
        context = self.module.create_web_qr_context(now_ms=1_700_000_000_000)
        self.module.QR_LOGIN_ATTEMPTS[login_id] = {
            "key": "browser-synthetic-key",
            **context,
            "qr_payload": (
                "https://music.163.com/st/platform/scanlogin?"
                "codekey=browser-synthetic-key&chainId=browser-chain"
            ),
            "status": "waiting",
            "created_monotonic": self.module.time.monotonic(),
            "last_checked_monotonic": 0.0,
            "checking": False,
        }

    def test_health_does_not_expose_secrets(self):
        with urllib.request.urlopen(self.base + "/health", timeout=2) as response:
            body = response.read().decode()
            self.assertIsNone(response.headers.get("Mcp-Session-Id"))
        self.assertIn('"status": "ok"', body)
        self.assertIn('"timezone": "Asia/Shanghai"', body)
        self.assertNotIn("MUSIC_U", body)

    def test_browser_login_page_is_capability_scoped_and_hides_cookies(self):
        login_id = "browser-login-token-1234567890"
        self.add_browser_login_attempt(login_id)
        attempt = self.module.QR_LOGIN_ATTEMPTS[login_id]
        attempt["temporary_cookie"] += "; MUSIC_U=browser-hidden; __csrf=hidden-csrf"
        with urllib.request.urlopen(
            self.base + "/netease/login/" + login_id, timeout=2
        ) as response:
            body = response.read().decode()
        self.assertIn("data:image/png;base64,", body)
        self.assertIn("等待扫码", body)
        self.assertNotIn("browser-hidden", body)
        self.assertNotIn("hidden-csrf", body)
        self.assertNotIn("browser-synthetic-key", body)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")

    def test_browser_login_page_uses_official_challenge_and_device_sdks(self):
        login_id = "browser-sdk-contract-token-1234"
        self.add_browser_login_attempt(login_id)
        with urllib.request.urlopen(
            self.base + "/netease/login/" + login_id, timeout=2
        ) as response:
            body = response.read().decode()
            csp = response.headers["Content-Security-Policy"]
        self.assertIn("https://cstaticdun.126.net/load.min.js", body)
        self.assertIn(
            "https://st.music.163.com/device/signature/create/deviceid.js", body
        )
        self.assertIn(self.module.NETEASE_YIDUN_CAPTCHA_ID, body)
        self.assertIn(self.module.NETEASE_DEVICE_APP_ID, body)
        self.assertIn("window.initNECaptcha", body)
        self.assertIn("window.createNEFingerprint", body)
        self.assertIn("result.token", body)
        self.assertIn("data.validate", body)
        self.assertIn("secure_captcha", body)
        self.assertIn("yd_device_token", body)
        self.assertIn("captcha_init_timeout", body)
        self.assertIn("sdk_load_failed", body)
        self.assertIn("sdk_unavailable", body)
        self.assertIn("captcha_init_failed", body)
        self.assertIn("captcha_runtime_error", body)
        self.assertIn("}},30000);", body)
        self.assertNotIn("}},10000);", body)
        self.assertIn("onClose", body)
        self.assertIn("onError", body)
        self.assertIn("current!==flowId", body)
        self.assertIn("destroyCaptcha()", body)
        self.assertIn("https://cstaticdun.126.net", csp)
        self.assertIn("https://st.music.163.com", csp)
        self.assertIn("https://acstatic-dun.126.net", csp)
        self.assertIn("https://cstaticdun1.126.net", csp)
        self.assertIn("https://necaptcha.nosdn.127.net", csp)
        self.assertIn("https://necaptcha1.nosdn.127.net", csp)
        self.assertIn("https://nos.netease.com", csp)
        self.assertIn("https://c.dun.163.com", csp)
        directives = {
            parts[0]: parts[1:]
            for directive in csp.split(";")
            if (parts := directive.strip().split())
        }
        self.assertIn("https://c.dun.163.com", directives["script-src"])
        self.assertIn("https://c.dun.163yun.com", directives["script-src"])
        self.assertIn("https://*.nstool.netease.com", directives["script-src"])
        self.assertIn("https://fp-upload.dun.163.com", directives["connect-src"])
        all_sources = {
            source for sources in directives.values() for source in sources
        }
        self.assertNotIn("*", all_sources)
        self.assertNotIn("https:", all_sources)
        self.assertNotIn("https://*.163.com", all_sources)
        self.assertNotIn("https://*.netease.com", all_sources)

    def test_browser_login_script_has_valid_javascript_syntax(self):
        if shutil.which("node") is None:
            self.skipTest("Node.js is not available for JavaScript syntax validation")
        login_id = "browser-js-syntax-token-123456"
        self.add_browser_login_attempt(login_id)
        page = self.module._qr_login_page(login_id)
        script = page.rsplit("<script>", 1)[1].split("</script>", 1)[0]
        result = subprocess.run(
            ["node", "--check", "-"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_browser_challenge_timeout_retry_and_stale_callback_guards(self):
        if shutil.which("node") is None:
            self.skipTest("Node.js is not available for browser-flow validation")
        login_id = "browser-challenge-timing-token"
        self.add_browser_login_attempt(login_id)
        page = self.module._qr_login_page(login_id)
        script = page.rsplit("<script>", 1)[1].split("</script>", 1)[0]
        harness = r"""
const listeners={};
const pageListeners={};
const nodes={
  status:{textContent:''},hint:{textContent:''},captcha:{},
  verify:{hidden:true,addEventListener:(name,fn)=>{listeners.verify=fn;}},
  cancel:{hidden:false,addEventListener:(name,fn)=>{listeners.cancel=fn;}}
};
let virtualNow=0,nextTimer=1;
const pendingTimers=new Map();
global.window=global;
global.location={pathname:'/netease/login/browser-challenge-timing-token'};
global.document={
  getElementById:(id)=>nodes[id],
  createElement:()=>({remove(){this.removed=true;}}),
  head:{appendChild:()=>{}}
};
global.addEventListener=(name,fn)=>{pageListeners[name]=fn;};
global.setTimeout=(fn,delay)=>{const id=nextTimer++;pendingTimers.set(id,{fn,at:virtualNow+delay});return id;};
global.clearTimeout=(id)=>{pendingTimers.delete(id);};
const requests=[];
global.fetch=(url,options)=>{requests.push({url,options});return Promise.resolve({json:()=>Promise.resolve({status:'cancelled'})});};
const warnings=[];
console.warn=(...args)=>{warnings.push(args);};
function assert(condition,message){if(!condition)throw new Error(message);}
async function flush(){await Promise.resolve();await Promise.resolve();}
async function advance(milliseconds){
  virtualNow+=milliseconds;
  while(true){
    const due=[...pendingTimers.entries()].filter(([,item])=>item.at<=virtualNow).sort((a,b)=>a[1].at-b[1].at);
    if(!due.length)break;
    for(const [id,item] of due){if(pendingTimers.delete(id)){item.fn();await flush();}}
  }
}
"""
        checks = r"""
if(timer){clearTimeout(timer);timer=null;}
const invocations=[];
window.initNECaptcha=(options,success,failure)=>{invocations.push({options,success,failure});};
(async()=>{
  const runtimeFailure=openChallenge();await flush();
  assert(invocations.length===1,'challenge was not initialized');
  invocations[0].options.onError({code:'E_RUNTIME',message:'MUSIC_U=must-not-log'});
  await runtimeFailure;
  assert(statusNode.textContent==='安全验证运行失败','SDK onerror was not classified immediately');
  assert(verifyButton.hidden===false&&!stopped,'runtime failure did not remain retryable');
  assert(JSON.stringify(warnings).includes('captcha_runtime_error'),'runtime category was not diagnosed');
  assert(!JSON.stringify(warnings).includes('must-not-log'),'unsafe SDK message reached console');

  const delayed=openChallenge();await flush();
  assert(invocations.length===2,'retry did not initialize challenge');
  await advance(10000);
  assert(statusNode.textContent!=='安全验证初始化超时','10 seconds caused a premature timeout');
  await advance(10000);
  let delayedPopup=0;
  invocations[1].success({popUp:()=>{delayedPopup++;},destroy:()=>{}});
  await delayed;
  assert(delayedPopup===1&&statusNode.textContent==='请完成网易安全验证','20-second initialization did not succeed');
  invocations[1].options.onClose();

  const timedOut=openChallenge();await flush();
  assert(invocations.length===3,'timeout scenario did not initialize challenge');
  await advance(29999);
  assert(statusNode.textContent!=='安全验证初始化超时','challenge timed out before 30 seconds');
  await advance(1);await timedOut;
  assert(statusNode.textContent==='安全验证初始化超时','challenge did not time out at 30 seconds');
  assert(verifyButton.hidden===false&&!stopped,'timeout did not preserve a retryable attempt');

  const retry=listeners.verify();await flush();
  assert(invocations.length===4,'timeout retry button did not reopen challenge');
  let stalePopup=0,staleDestroyed=0;
  listeners.cancel();
  invocations[3].success({popUp:()=>{stalePopup++;},destroy:()=>{staleDestroyed++;}});
  await retry;
  assert(stopped&&stalePopup===0&&staleDestroyed===1,'stale callback revived a cancelled flow');
  assert(requests.length===1,'challenge failures unexpectedly cancelled the server attempt');
})().catch(error=>{process.stderr.write(error.stack+'\n');process.exitCode=1;});
"""
        result = subprocess.run(
            ["node", "-"],
            input=harness + script + checks,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_browser_verification_post_uses_shared_check_and_never_echoes_proof(self):
        login_id = "browser-proof-post-token-12345"
        self.add_browser_login_attempt(login_id)
        shared_result = {
            "login_id": login_id,
            "status": "scanned",
            "retry_after_seconds": 2,
        }
        proof = "browser-private-validate"
        device_token = "browser-private-device-token"
        request = urllib.request.Request(
            self.base + f"/netease/login/{login_id}/status",
            data=json.dumps(
                {
                    "secure_captcha": proof,
                    "yd_device_token": device_token,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with mock.patch.object(
            self.module, "_check_netease_qr_login", return_value=shared_result
        ) as shared:
            with self.assertLogs(self.module.LOG, level="INFO") as logs:
                with urllib.request.urlopen(request, timeout=2) as response:
                    raw = response.read().decode()
        self.assertEqual(json.loads(raw), shared_result)
        shared.assert_called_once_with(
            login_id,
            secure_captcha=proof,
            yd_device_token=device_token,
        )
        self.assertNotIn(proof, raw)
        self.assertNotIn(device_token, raw)
        joined = "\n".join(logs.output)
        self.assertNotIn(proof, joined)
        self.assertNotIn(device_token, joined)

    def test_browser_cancel_post_cleans_attempt(self):
        login_id = "browser-explicit-cancel-token-12"
        self.add_browser_login_attempt(login_id)
        request = urllib.request.Request(
            self.base + f"/netease/login/{login_id}/status",
            data=b'{"cancel":true}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            result = json.loads(response.read())
        self.assertEqual(result["status"], "cancelled")
        self.assertNotIn(login_id, self.module.QR_LOGIN_ATTEMPTS)

    def test_browser_status_rejects_invalid_ephemeral_proof_without_echoing_it(self):
        login_id = "browser-invalid-proof-token-123"
        self.add_browser_login_attempt(login_id)
        secret = "s" * 8193
        request = urllib.request.Request(
            self.base + f"/netease/login/{login_id}/status",
            data=json.dumps({"secure_captcha": secret}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=2)
        raw = context.exception.read().decode()
        self.assertEqual(context.exception.code, HTTPStatus.BAD_REQUEST)
        self.assertEqual(json.loads(raw)["status"], "invalid_request")
        self.assertNotIn(secret, raw)

    def test_browser_status_and_mcp_use_the_same_check_function(self):
        login_id = "browser-shared-state-token-1234"
        self.add_browser_login_attempt(login_id)
        shared_result = {
            "login_id": login_id,
            "status": "scanned",
            "retry_after_seconds": 2,
        }
        with mock.patch.object(
            self.module, "_check_netease_qr_login", return_value=shared_result
        ) as shared:
            with urllib.request.urlopen(
                self.base + f"/netease/login/{login_id}/status", timeout=2
            ) as response:
                browser_result = json.loads(response.read())
            mcp_result = json.loads(self.module.check_netease_qr_login(login_id))
        self.assertEqual(browser_result, shared_result)
        self.assertEqual(mcp_result, shared_result)
        self.assertEqual(shared.call_args_list, [mock.call(login_id), mock.call(login_id)])

    def test_invalid_or_expired_browser_login_token_is_rejected(self):
        cases = {
            "browser-invalid-token-12345678": False,
            "browser-expired-token-1234567": True,
        }
        for login_id, create_expired in cases.items():
            if create_expired:
                self.add_browser_login_attempt(login_id)
                self.module.QR_LOGIN_ATTEMPTS[login_id]["created_monotonic"] -= (
                    self.module.QR_LOGIN_TTL_SECONDS + 1
                )
            with self.subTest(login_id=login_id), self.assertRaises(
                urllib.error.HTTPError
            ) as context:
                urllib.request.urlopen(
                    self.base + f"/netease/login/{login_id}/status", timeout=2
                )
            self.assertEqual(context.exception.code, HTTPStatus.GONE)

    def test_browser_login_token_is_redacted_from_access_log(self):
        login_id = "browser-log-secret-token-123456"
        self.add_browser_login_attempt(login_id)
        with self.assertLogs(self.module.LOG, level="INFO") as logs:
            with urllib.request.urlopen(
                self.base + "/netease/login/" + login_id, timeout=2
            ):
                pass
        joined = "\n".join(logs.output)
        self.assertNotIn(login_id, joined)
        self.assertIn("/netease/login/[REDACTED]", joined)

    def test_stateless_mcp_get_returns_method_not_allowed(self):
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(self.base + "/mcp", timeout=2)
        self.assertEqual(context.exception.code, 405)
        self.assertEqual(context.exception.headers.get("Allow"), "POST, OPTIONS")

    def test_mcp_requires_bearer_token(self):
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(context.exception.code, 401)

    def test_authorized_tools_list(self):
        with self.post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            "a-secure-test-token-that-is-long",
        ) as response:
            body = json.loads(response.read())
        names = {tool["name"] for tool in body["result"]["tools"]}
        self.assertEqual(
            names,
            self.module.READ_TOOL_NAMES | self.module.SESSION_TOOL_NAMES,
        )

    def test_tool_failure_is_returned_as_mcp_error_content(self):
        with mock.patch.object(
            self.module,
            "netease_request",
            side_effect=self.module.NetEaseError("Temporary NetEase failure."),
        ):
            with self.post(
                {
                    "jsonrpc": "2.0",
                    "id": 27,
                    "method": "tools/call",
                    "params": {"name": "search_song", "arguments": {"query": "Home"}},
                },
                "a-secure-test-token-that-is-long",
            ) as response:
                self.assertEqual(response.status, 200)
                body = json.loads(response.read())
        self.assertEqual(body["id"], 27)
        self.assertTrue(body["result"]["isError"])
        self.assertIn("Temporary NetEase failure", body["result"]["content"][0]["text"])

    def test_tool_failure_does_not_leak_credentials_to_response_or_log(self):
        secret_message = "upstream echoed " + self.module.NETEASE_COOKIE
        with self.assertLogs(self.module.LOG, level="WARNING") as logs:
            with mock.patch.object(
                self.module,
                "netease_request",
                side_effect=self.module.NetEaseError(secret_message),
            ):
                with self.post(
                    {
                        "jsonrpc": "2.0",
                        "id": 28,
                        "method": "tools/call",
                        "params": {"name": "search_song", "arguments": {"query": "Home"}},
                    },
                    "a-secure-test-token-that-is-long",
                ) as response:
                    body = response.read().decode()
        joined_logs = "\n".join(logs.output)
        self.assertNotIn("MUSIC_U=test", body)
        self.assertNotIn("MUSIC_U=test", joined_logs)
        self.assertIn("[REDACTED]", body)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class OAuthHTTPTests(unittest.TestCase):
    def setUp(self):
        self.module = load_server("true", oauth=True)
        self.module.PUBLIC_URL = "https://music.example.test"
        self.httpd = self.module.ThreadingHTTPServer(("127.0.0.1", 0), self.module.MCPHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def post_json(self, path, payload, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=2)

    def post_form(self, path, payload, opener=None):
        request = urllib.request.Request(
            self.base + path,
            data=urllib.parse.urlencode(payload).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        if opener:
            return opener.open(request, timeout=2)
        return urllib.request.urlopen(request, timeout=2)

    def register(self):
        with self.post_json(
            "/register", {"redirect_uris": ["https://client.example/callback"]}
        ) as response:
            return json.loads(response.read())

    def test_oauth_metadata_and_challenge(self):
        with urllib.request.urlopen(
            self.base + "/.well-known/oauth-protected-resource", timeout=2
        ) as response:
            metadata = json.loads(response.read())
        self.assertEqual(metadata["resource"], "https://music.example.test/mcp")
        self.assertEqual(metadata["authorization_servers"], ["https://music.example.test"])
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.post_json("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(context.exception.code, 401)
        self.assertIn("resource_metadata=", context.exception.headers["WWW-Authenticate"])

        client = self.register()
        verifier = "v" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        query = urllib.parse.urlencode(
            {
                "client_id": client["client_id"],
                "redirect_uri": "https://client.example/callback",
                "response_type": "code",
                "scope": "netease.read netease.write",
                "state": "state-write-description",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": "https://music.example.test/mcp",
            }
        )
        with mock.patch.object(self.module, "OAUTH_SCOPE", "netease.read netease.write"):
            with urllib.request.urlopen(self.base + "/authorize?" + query, timeout=2) as response:
                page = response.read().decode()
                authorization_csp = response.headers["Content-Security-Policy"]
        self.assertIn("Writes execute as a single audited call", page)
        self.assertNotIn("matching preview", page.lower())
        self.assertEqual(
            authorization_csp,
            "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )

    def test_authorization_code_pkce_and_refresh_flow(self):
        client = self.register()
        verifier = "v" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        auth = {
            "client_id": client["client_id"],
            "redirect_uri": "https://client.example/callback",
            "response_type": "code",
            "scope": "netease.read",
            "state": "state-123",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": "https://music.example.test/mcp",
            "password": "a-different-oauth-password",
        }
        opener = urllib.request.build_opener(NoRedirect())
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.post_form("/authorize", auth, opener)
        self.assertEqual(context.exception.code, 302)
        location = context.exception.headers["Location"]
        callback = urllib.parse.urlsplit(location)
        query = urllib.parse.parse_qs(callback.query)
        self.assertEqual(query["state"], ["state-123"])
        code = query["code"][0]

        with self.post_form(
            "/token",
            {
                "grant_type": "authorization_code",
                "client_id": client["client_id"],
                "redirect_uri": "https://client.example/callback",
                "code": code,
                "code_verifier": verifier,
            },
        ) as response:
            tokens = json.loads(response.read())
        self.assertIn("refresh_token", tokens)

        with self.post_json(
            "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            tokens["access_token"],
        ) as response:
            body = json.loads(response.read())
        self.assertEqual(
            {tool["name"] for tool in body["result"]["tools"]},
            self.module.READ_TOOL_NAMES | self.module.SESSION_TOOL_NAMES,
        )

        with self.post_form(
            "/token",
            {
                "grant_type": "refresh_token",
                "client_id": client["client_id"],
                "refresh_token": tokens["refresh_token"],
            },
        ) as response:
            refreshed = json.loads(response.read())
        self.assertIn("access_token", refreshed)
        with self.post_json(
            "/mcp",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            refreshed["access_token"],
        ) as response:
            retried = json.loads(response.read())
        self.assertEqual(
            {tool["name"] for tool in retried["result"]["tools"]},
            self.module.READ_TOOL_NAMES | self.module.SESSION_TOOL_NAMES,
        )

    def test_session_scope_is_advertised_issued_enforced_and_refreshable(self):
        advertised = set(self.module.OAUTH_SCOPE.split())
        self.assertEqual(advertised, {"netease.read", "netease.session"})
        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-authorization-server",
        ):
            with urllib.request.urlopen(self.base + path, timeout=2) as response:
                metadata = json.loads(response.read())
            self.assertEqual(set(metadata["scopes_supported"]), advertised)

        client = self.register()
        verifier = "s" * 64
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        params = {
            "client_id": client["client_id"],
            "redirect_uri": "https://client.example/callback",
            "response_type": "code",
            "scope": "netease.read netease.session",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": "https://music.example.test/mcp",
        }
        code = self.module.issue_authorization_code(params)
        with self.post_form(
            "/token",
            {
                "grant_type": "authorization_code",
                "client_id": client["client_id"],
                "redirect_uri": "https://client.example/callback",
                "code": code,
                "code_verifier": verifier,
            },
        ) as response:
            tokens = json.loads(response.read())
        self.assertEqual(set(tokens["scope"].split()), advertised)

        with self.post_form(
            "/token",
            {
                "grant_type": "refresh_token",
                "client_id": client["client_id"],
                "refresh_token": tokens["refresh_token"],
            },
        ) as response:
            refreshed = json.loads(response.read())
        self.assertEqual(set(refreshed["scope"].split()), advertised)

        safe_result = json.dumps(
            {
                "login_id": "synthetic-oauth-login-id",
                "status": "waiting",
                "qr_payload": "https://music.163.com/login?codekey=synthetic",
                "qr_url": "https://music.163.com/login?codekey=synthetic",
            }
        )
        tool_call = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "start_netease_qr_login", "arguments": {}},
        }
        with mock.patch.object(
            self.module, "start_netease_qr_login", return_value=safe_result
        ):
            with self.post_json("/mcp", tool_call, refreshed["access_token"]) as response:
                allowed = json.loads(response.read())
        self.assertFalse(allowed["result"].get("isError", False))
        self.assertEqual(
            [item["type"] for item in allowed["result"]["content"]],
            ["text", "image"],
        )

        legacy_token = self.module._token_pair(
            client["client_id"], "netease.read", include_refresh=False
        )["access_token"]
        with self.post_json("/mcp", tool_call, legacy_token) as response:
            rejected = json.loads(response.read())
        self.assertTrue(rejected["result"]["isError"])
        self.assertIn("netease.session scope is required", rejected["result"]["content"][0]["text"])
        with self.post_json(
            "/mcp",
            {"jsonrpc": "2.0", "id": 8, "method": "tools/list"},
            legacy_token,
        ) as response:
            legacy_list = json.loads(response.read())
        self.assertIn("tools", legacy_list["result"])

    def test_authorization_code_cannot_be_reused(self):
        client = self.register()
        verifier = "x" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {
            "client_id": client["client_id"],
            "redirect_uri": "https://client.example/callback",
            "response_type": "code",
            "scope": "netease.read",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": "https://music.example.test/mcp",
        }
        code = self.module.issue_authorization_code(params)
        form = {
            "grant_type": "authorization_code",
            "client_id": client["client_id"],
            "redirect_uri": "https://client.example/callback",
            "code": code,
            "code_verifier": verifier,
        }
        with self.post_form("/token", form):
            pass
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.post_form("/token", form)
        self.assertEqual(context.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
