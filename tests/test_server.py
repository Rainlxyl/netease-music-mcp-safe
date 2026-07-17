import importlib.util
import base64
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timezone
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
    preview_policy=None,
    require_preview="false",
):
    env = {
        "MCP_READ_ONLY": read_only,
        "MCP_ACCESS_TOKEN": token,
        "NETEASE_COOKIE": "MUSIC_U=test; __csrf=test",
        "MCP_PUBLIC_URL": "https://music.example.test" if oauth else "",
        "MCP_OAUTH_PASSWORD": "a-different-oauth-password" if oauth else "",
        # Existing direct-write compatibility tests exercise the legacy path.
        # Production defaults to mandatory preview tokens.
        "MCP_STORAGE_PATH": "",
    }
    if require_preview is not None:
        env["MCP_REQUIRE_WRITE_PREVIEW"] = require_preview
    with mock.patch.dict(os.environ, env, clear=False):
        if preview_policy is None:
            os.environ.pop("MCP_WRITE_PREVIEW_POLICY", None)
        else:
            os.environ["MCP_WRITE_PREVIEW_POLICY"] = preview_policy
        if require_preview is None:
            os.environ.pop("MCP_REQUIRE_WRITE_PREVIEW", None)
        spec = importlib.util.spec_from_file_location("server_under_test", ROOT / "server.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class ToolTests(unittest.TestCase):
    def test_read_only_lists_only_read_tools(self):
        module = load_server("true")
        names = {tool["name"] for tool in module.available_tools()}
        self.assertEqual(names, module.READ_TOOL_NAMES)
        self.assertTrue(names.isdisjoint(module.WRITE_TOOL_NAMES))

    def test_write_mode_lists_all_tools(self):
        module = load_server("false")
        names = {tool["name"] for tool in module.available_tools()}
        self.assertEqual(names, module.READ_TOOL_NAMES | module.WRITE_TOOL_NAMES)

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
        playlist_schema = tools["get_playlist_songs"]["inputSchema"]["properties"]
        self.assertEqual(playlist_schema["limit"]["default"], 50)
        self.assertEqual(playlist_schema["offset"]["default"], 0)

    def test_write_mode_advertises_write_oauth_scope(self):
        module = load_server("false", oauth=True)
        self.assertEqual(module.OAUTH_SCOPE, "netease.read netease.write")

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
        self.assertEqual(payload["events"][0]["source_device"], "Web")

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
            "search_podcasts",
            "search_podcast_programs",
            "get_recent_podcast_plays",
        }
        self.assertTrue(expected.issubset(tools))
        for name in expected:
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"])
            self.assertNotIn("song_id", tools[name]["inputSchema"]["properties"])
        self.assertIn("radio_id", tools["get_podcast_programs"]["inputSchema"]["properties"])

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
                        "baseInfo": {"name": "Episode", "radio": {"id": 101}},
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
        self.assertNotIn("song_id", programs["programs"][0])
        self.assertIn("/api/search/voicelist/get", request.call_args_list[0].args[0])
        self.assertIn("/api/search/voice/get", request.call_args_list[1].args[0])

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
        self.assertIsNone(payload["records"][1]["played_at"])
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
            result = module.call_tool(
                "update_playlist", {"playlist_id": 123, "description": ""}
            )
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


class PreviewPersistenceAndCoverTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.module = load_server("false")
        self.module.STORAGE_PATH = str(Path(self.tempdir.name) / "state.sqlite3")
        self.module.STORE_INSTANCE = None
        self.module.REQUIRE_WRITE_PREVIEW = True

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

    def test_new_tool_schemas_and_default_preview_requirement(self):
        tools = {tool["name"]: tool for tool in self.module.available_tools()}
        self.assertEqual(
            tools["update_playlist_cover"]["_meta"]["openai/fileParams"], ["image"]
        )
        self.assertTrue(tools["preview_operation"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["undo_operation"]["annotations"]["destructiveHint"])
        for name in ("add_to_playlist", "like_song", "create_interaction_note"):
            self.assertIn("idempotency_key", tools[name]["inputSchema"]["properties"])
        self.assertNotIn(
            "preview_token", tools["create_interaction_note"]["inputSchema"]["required"]
        )
        with self.assertRaisesRegex(ValueError, "preview_token"):
            self.module.call_tool("create_playlist", {"name": "Needs preview"})

    def test_write_mode_startup_requires_persistent_storage_path(self):
        self.module.STORAGE_PATH = ""
        with self.assertRaisesRegex(SystemExit, "MCP_STORAGE_PATH"):
            self.module.validate_startup()
        self.module.STORAGE_PATH = str(Path(self.tempdir.name) / "state.sqlite3")
        self.module.validate_startup()

    def test_preview_is_read_only_token_bound_persistent_and_idempotent(self):
        before = {
            "playlist_id": 1,
            "creator_id": 7,
            "name": "Before",
            "description": "Old",
        }
        after = {**before, "name": "After"}
        arguments = {"playlist_id": 1, "name": "After"}
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_current_state_for_operation", return_value=before
        ), mock.patch.object(self.module, "update_playlist") as write:
            preview = json.loads(self.module.preview_operation("update_playlist", arguments))
        write.assert_not_called()
        self.assertEqual(preview["before_state"], before)

        # Simulate a process restart by reopening the same SQLite file.
        self.module.STORE_INSTANCE = None
        formal_arguments = {**arguments, "preview_token": preview["preview_token"]}
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_current_state_for_operation", return_value=before
        ), mock.patch.object(
            self.module,
            "_execute_operation_action",
            return_value={"success": True},
        ) as action, mock.patch.object(
            self.module, "_after_state_for_operation", return_value=after
        ):
            first = json.loads(self.module.call_tool("update_playlist", formal_arguments))
            replay = json.loads(self.module.call_tool("update_playlist", formal_arguments))
        self.assertEqual(first["status"], "success")
        self.assertEqual(first["operation_id"], replay["operation_id"])
        self.assertTrue(replay["idempotent_replay"])
        action.assert_called_once()
        rows = self.module._store().query_operations(7, limit=10, offset=0)
        self.assertEqual(len(rows), 1)

    def test_preview_rejects_mismatched_and_expired_tokens(self):
        before = {"playlist_id": 1, "creator_id": 7, "name": "A", "description": ""}
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_current_state_for_operation", return_value=before
        ):
            preview = json.loads(
                self.module.preview_operation(
                    "update_playlist", {"playlist_id": 1, "name": "B"}
                )
            )
        with mock.patch.object(self.module, "get_uid", return_value=7):
            with self.assertRaisesRegex(ValueError, "does not match"):
                self.module.call_tool(
                    "update_playlist",
                    {
                        "playlist_id": 1,
                        "name": "Different",
                        "preview_token": preview["preview_token"],
                    },
                )

        connection = sqlite3.connect(self.module.STORAGE_PATH)
        try:
            connection.execute("UPDATE previews SET expires_at=0")
            connection.commit()
        finally:
            connection.close()
        with mock.patch.object(self.module, "get_uid", return_value=7):
            with self.assertRaisesRegex(ValueError, "expired"):
                self.module.call_tool(
                    "update_playlist",
                    {
                        "playlist_id": 1,
                        "name": "B",
                        "preview_token": preview["preview_token"],
                    },
                )

    def test_state_conflict_is_logged_without_writing(self):
        before = {"playlist_id": 1, "creator_id": 7, "track_ids": [1, 2]}
        changed = {"playlist_id": 1, "creator_id": 7, "track_ids": [1, 2, 3]}
        args = {"playlist_id": 1, "song_ids": [2, 1]}
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_current_state_for_operation", return_value=before
        ):
            preview = json.loads(self.module.preview_operation("reorder_playlist_tracks", args))
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_current_state_for_operation", return_value=changed
        ), mock.patch.object(self.module, "_execute_operation_action") as action:
            with self.assertRaisesRegex(ValueError, "changed after preview"):
                self.module.call_tool(
                    "reorder_playlist_tracks",
                    {**args, "preview_token": preview["preview_token"]},
                )
        action.assert_not_called()
        rows = self.module._store().query_operations(7, limit=10, offset=0)
        self.assertEqual(rows[0]["status"], "conflict")

    def test_failure_and_partial_success_are_sanitized_in_log(self):
        before = {"playlist_id": 1, "creator_id": 7, "name": "A", "description": ""}
        partial = {**before, "name": "B"}
        args = {"playlist_id": 1, "name": "B", "description": "C"}
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_current_state_for_operation", return_value=before
        ):
            preview = json.loads(self.module.preview_operation("update_playlist", args))
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
                self.module.call_tool(
                    "update_playlist", {**args, "preview_token": preview["preview_token"]}
                )
        self.assertNotIn("MUSIC_U=test", str(context.exception))
        rows = self.module._store().query_operations(7, limit=10, offset=0)
        self.assertEqual(rows[0]["status"], "partial_success")
        self.assertNotIn("MUSIC_U=test", rows[0]["error_summary"])
        self.assertIn("[REDACTED]", rows[0]["error_summary"])

    def test_preview_rejects_playlist_without_write_permission(self):
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
                self.module.preview_operation(
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

    def test_formal_undo_is_previewed_and_logged_as_its_own_operation(self):
        before = {"song_id": 9, "liked": False}
        after = {"song_id": 9, "liked": True}
        original_id = self.seed_operation(
            "like_song", {"song_id": 9, "like": True}, before, after
        )
        undo_args = {"operation_id": original_id}
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module,
            "_liked_song_state",
            side_effect=[after, after, after, before],
        ), mock.patch.object(self.module, "like_song", return_value="Unliked"):
            preview = json.loads(self.module.preview_operation("undo_operation", undo_args))
            result = json.loads(
                self.module.call_tool(
                    "undo_operation",
                    {**undo_args, "preview_token": preview["preview_token"]},
                )
            )
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
            preview = json.loads(self.module.preview_operation("create_interaction_note", args))
            created = json.loads(
                self.module.call_tool(
                    "create_interaction_note", {**args, "preview_token": preview["preview_token"]}
                )
            )
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
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_accessible_playlist", return_value=accessible
        ):
            update_preview = json.loads(
                self.module.preview_operation("update_interaction_note", update_args)
            )
        self.module._store().update_note(note["note_id"], 7, 1, content="Manual edit")
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_accessible_playlist", return_value=accessible
        ):
            with self.assertRaisesRegex(ValueError, "version is stale"):
                self.module.call_tool(
                    "update_interaction_note",
                    {**update_args, "preview_token": update_preview["preview_token"]},
                )
        rows = self.module._store().query_operations(
            7, limit=10, offset=0, operation="update_interaction_note"
        )
        self.assertEqual(rows[0]["status"], "conflict")

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
            preview = json.loads(
                self.module.preview_operation("delete_interaction_note", delete_args)
            )
            deleted = json.loads(
                self.module.call_tool(
                    "delete_interaction_note",
                    {**delete_args, "preview_token": preview["preview_token"]},
                )
            )
        self.assertIsNotNone(deleted["after_state"]["deleted_at"])
        original_id = deleted["operation_id"]

        with mock.patch.object(self.module, "get_uid", return_value=7):
            undo_preview = json.loads(
                self.module.preview_operation(
                    "undo_operation", {"operation_id": original_id}
                )
            )
            restored = json.loads(
                self.module.call_tool(
                    "undo_operation",
                    {
                        "operation_id": original_id,
                        "preview_token": undo_preview["preview_token"],
                    },
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

    def test_cover_preview_is_irreversible_and_upload_uses_nos_token_privately(self):
        before = {"playlist_id": 1, "creator_id": 7, "cover_image_id": 10, "cover_image_url": None}
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
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_current_state_for_operation", return_value=before
        ), mock.patch.object(
            self.module, "normalize_cover_image", return_value=(b"jpeg", meta)
        ):
            preview = json.loads(self.module.preview_operation("update_playlist_cover", args))
        self.assertFalse(preview["reversible"])
        self.assertEqual(preview["risk_level"], "high")
        self.assertNotIn("download_url", json.dumps(preview["normalized_arguments"]))

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
        self.assertEqual(payload["operations"][0]["operation_id"], operation_id)


class RiskBasedWritePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.module = load_server(
            "false", preview_policy="risk_based", require_preview="true"
        )
        self.module.STORAGE_PATH = str(Path(self.tempdir.name) / "risk.sqlite3")
        self.module.STORE_INSTANCE = None

    def tearDown(self):
        self.module.STORE_INSTANCE = None
        self.tempdir.cleanup()

    def test_policy_defaults_precedence_and_invalid_value(self):
        default = load_server("false", require_preview=None)
        self.assertEqual(default._effective_write_preview_policy(), "strict")

        legacy = load_server("false", require_preview="false")
        self.assertEqual(legacy._effective_write_preview_policy(), "legacy_direct")

        risk = load_server(
            "false", preview_policy="risk_based", require_preview="true"
        )
        risk.STORAGE_PATH = str(Path(self.tempdir.name) / "risk-policy.sqlite3")
        self.assertEqual(risk._effective_write_preview_policy(), "risk_based")
        with self.assertLogs(risk.LOG, level="WARNING") as captured:
            risk.validate_startup()
        self.assertIn("ignoring MCP_REQUIRE_WRITE_PREVIEW", " ".join(captured.output))

        strict = load_server(
            "false", preview_policy="strict", require_preview="false"
        )
        self.assertEqual(strict._effective_write_preview_policy(), "strict")

        invalid = load_server(
            "false", preview_policy="surprise", require_preview="true"
        )
        invalid.STORAGE_PATH = str(Path(self.tempdir.name) / "invalid.sqlite3")
        with self.assertRaisesRegex(SystemExit, "strict or risk_based"):
            invalid.validate_startup()

    def test_strict_still_requires_preview_for_every_write(self):
        strict = load_server(
            "false", preview_policy="strict", require_preview="false"
        )
        for name in strict.WRITE_TOOL_NAMES:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "preview_token"):
                strict.call_tool(name, {})

    def test_risk_based_high_risk_writes_still_require_preview(self):
        cases = {
            "create_playlist": {"name": "New"},
            "update_playlist": {"playlist_id": 1, "name": "Changed"},
            "remove_from_playlist": {"playlist_id": 1, "song_ids": [1]},
            "reorder_playlist_tracks": {"playlist_id": 1, "song_ids": [1]},
            "like_song": {"song_id": 1, "like": False},
            "update_interaction_note": {
                "note_id": "note-12345",
                "version": 1,
                "content": "Changed",
            },
            "delete_interaction_note": {"note_id": "note-12345", "version": 1},
            "undo_operation": {"operation_id": "operation-12345"},
            "update_playlist_cover": {},
        }
        for name, arguments in cases.items():
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "preview_token"):
                self.module.call_tool(name, arguments)

    def test_owned_add_one_and_ten_songs_succeed_and_are_logged(self):
        for count in (1, 10):
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
            if count == 1:
                with mock.patch.object(
                    self.module, "get_uid", return_value=7
                ), mock.patch.object(
                    self.module, "_current_state_for_operation", return_value=after
                ):
                    undo_preview = json.loads(
                        self.module.preview_operation(
                            "undo_operation", {"operation_id": result["operation_id"]}
                        )
                    )
                self.assertTrue(undo_preview["preview_token"])

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
        with mock.patch.object(self.module, "get_uid") as uid:
            with self.assertRaisesRegex(ValueError, "requires a matching preview_token"):
                self.module.call_tool(
                    "add_to_playlist", {"playlist_id": 9, "song_ids": list(range(1, 12))}
                )
        uid.assert_not_called()

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
        ):
            with self.assertRaises(self.module.NetEaseError):
                self.module.call_tool(
                    "add_to_playlist",
                    {
                        "playlist_id": 9,
                        "song_ids": [1],
                        "idempotency_key": "partial-result-1",
                    },
                )
        partial = self.module._store().get_operation_by_idempotency(
            7,
            "add_to_playlist",
            self.module._idempotency_key_hash("partial-result-1"),
        )
        self.assertEqual(partial["status"], "partial_success")

    def test_preview_is_released_after_failure_before_upstream(self):
        before = {"playlist_id": 9, "creator_id": 7, "track_ids": []}
        after = {**before, "track_ids": [1]}
        args = {"playlist_id": 9, "song_ids": [1]}
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_current_state_for_operation", return_value=before
        ):
            preview = json.loads(self.module.preview_operation("add_to_playlist", args))
        formal = {**args, "preview_token": preview["preview_token"]}
        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_current_state_for_operation", return_value=before
        ), mock.patch.object(self.module, "_fetch_song_records", return_value=[]):
            with self.assertRaises(ValueError):
                self.module.call_tool("add_to_playlist", formal)
        connection = sqlite3.connect(self.module.STORAGE_PATH)
        try:
            status = connection.execute("SELECT status FROM previews").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(status, "pending")

        with mock.patch.object(self.module, "get_uid", return_value=7), mock.patch.object(
            self.module, "_current_state_for_operation", side_effect=[before, after]
        ), mock.patch.object(
            self.module, "_fetch_song_records", return_value=[{"id": 1}]
        ), mock.patch.object(
            self.module, "manipulate_playlist", return_value="added"
        ):
            result = json.loads(self.module.call_tool("add_to_playlist", formal))
        self.assertEqual(result["status"], "success")

    def test_like_true_is_direct_but_unlike_requires_preview(self):
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
            self.module, "_current_state_for_operation", return_value=after
        ):
            undo_preview = json.loads(
                self.module.preview_operation(
                    "undo_operation", {"operation_id": result["operation_id"]}
                )
            )
        self.assertTrue(undo_preview["preview_token"])
        with self.assertRaisesRegex(ValueError, "preview_token"):
            self.module.call_tool("like_song", {"song_id": 3, "like": False})

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

        with self.assertRaisesRegex(ValueError, "preview_token"):
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
        finally:
            connection.close()
        self.assertIn("upstream_action_started", columns)
        self.assertIn("idempotency_key", columns)
        self.assertIn("operations_idempotency_idx", indexes)

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

    def test_health_does_not_expose_secrets(self):
        with urllib.request.urlopen(self.base + "/health", timeout=2) as response:
            body = response.read().decode()
            self.assertIsNone(response.headers.get("Mcp-Session-Id"))
        self.assertIn('"status": "ok"', body)
        self.assertNotIn("MUSIC_U", body)

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
        self.assertEqual(names, self.module.READ_TOOL_NAMES)

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
            {tool["name"] for tool in body["result"]["tools"]}, self.module.READ_TOOL_NAMES
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
            self.module.READ_TOOL_NAMES,
        )

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
