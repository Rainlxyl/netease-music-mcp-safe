import importlib.util
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_server(read_only="true", token="a-secure-test-token-that-is-long"):
    env = {
        "MCP_READ_ONLY": read_only,
        "MCP_ACCESS_TOKEN": token,
        "NETEASE_COOKIE": "MUSIC_U=test; __csrf=test",
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


if __name__ == "__main__":
    unittest.main()

