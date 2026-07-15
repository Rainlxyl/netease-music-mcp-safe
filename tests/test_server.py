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
        self.assertIn('"status": "ok"', body)
        self.assertNotIn("MUSIC_U", body)

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
