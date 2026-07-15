import importlib.util
import base64
import hashlib
import json
import os
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_server(read_only="true", token="a-secure-test-token-that-is-long", oauth=False):
    env = {
        "MCP_READ_ONLY": read_only,
        "MCP_ACCESS_TOKEN": token,
        "NETEASE_COOKIE": "MUSIC_U=test; __csrf=test",
        "MCP_PUBLIC_URL": "https://music.example.test" if oauth else "",
        "MCP_OAUTH_PASSWORD": "a-different-oauth-password" if oauth else "",
    }
    with mock.patch.dict(os.environ, env, clear=False):
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
