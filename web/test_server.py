import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import quote

from .server import create_server


class WebServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = create_server("127.0.0.1", 0, root=self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def call(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(self.base + path, method=method, data=data)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        with urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_schema_and_data_crud(self):
        schema = {
            "head": {"chn_name": "用户表", "eng_name": "users"},
            "columns": [
                {"chn_name": "编号", "eng_name": "id", "primary_key": True, "data_type": "int", "disp_type": "数字", "disp_menu": ""},
                {"chn_name": "姓名", "eng_name": "name", "primary_key": False, "data_type": "str", "disp_type": "文本", "disp_menu": ""},
            ],
        }
        self.assertEqual(self.call("POST", "/api/schemas", schema)[0], 201)
        self.assertEqual(self.call("GET", "/api/schemas")[1]["schemas"][0]["name"], "users")
        self.assertEqual(self.call("PUT", "/api/schemas/users", schema)[0], 200)
        self.assertEqual(self.call("POST", "/api/data/users", {"id": 1, "name": "Alice"})[0], 201)
        self.assertEqual(self.call("POST", "/api/data/users", {"rows": [{"id": 2, "name": "Bob"}]})[1]["inserted"], 1)
        self.assertEqual(self.call("GET", "/api/data/users?order_by=id%20DESC")[1]["rows"][0]["id"], 2)
        self.assertEqual(self.call("PUT", "/api/data/users", {"where": {"id": 1}, "values": {"name": "Alicia"}})[1]["updated"], 1)
        self.assertEqual(self.call("GET", "/api/data/users/0")[1]["row"]["name"], "Alicia")
        self.assertEqual(self.call("PUT", "/api/data/users/0", {"id": 1, "name": "Alice-2"})[1]["row"]["name"], "Alice-2")
        self.assertEqual(self.call("DELETE", "/api/data/users", {"where": {"id": 2}})[1]["deleted"], 1)
        self.assertEqual(self.call("DELETE", "/api/data/users/0")[1]["row"]["id"], 1)
        self.assertEqual(self.call("DELETE", "/api/schemas/users")[1]["deleted"], True)

    def test_html_and_invalid_definition(self):
        status, body = self.call("GET", "/api/health")
        self.assertEqual((status, body["ok"]), (200, True))
        with urlopen(self.base + "/", timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("RTDB", response.read().decode("utf-8"))
        with urlopen(self.base + "/static/app.css", timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(".topbar", response.read().decode("utf-8"))
        invalid = {"head": {"chn_name": "旧", "en_name": "bad"}, "columns": []}
        with self.assertRaises(HTTPError) as raised:
            self.call("POST", "/api/schemas", invalid)
        self.assertEqual(raised.exception.code, 400)

    def test_composite_primary_key_data_crud(self):
        schema = {
            "head": {"chn_name": "关系表", "eng_name": "links"},
            "columns": [
                {"chn_name": "站点", "eng_name": "site", "primary_key": True, "data_type": "str", "disp_type": "文本", "disp_menu": ""},
                {"chn_name": "设备", "eng_name": "device", "primary_key": True, "data_type": "int", "disp_type": "数字", "disp_menu": ""},
                {"chn_name": "状态", "eng_name": "state", "primary_key": False, "data_type": "str", "disp_type": "文本", "disp_menu": ""},
            ],
        }
        self.assertEqual(self.call("POST", "/api/schemas", schema)[0], 201)
        self.assertEqual(
            self.call("POST", "/api/data/links", {"rows": [
                {"site": "a", "device": 1, "state": "up"},
                {"site": "a", "device": 2, "state": "up"},
            ]})[1]["inserted"],
            2,
        )
        where = quote(json.dumps({"site": "a", "device": 2}, ensure_ascii=False), safe="")
        self.assertEqual(self.call("GET", f"/api/data/links?where={where}")[1]["rows"], [{"site": "a", "device": 2, "state": "up"}])
        self.assertEqual(
            self.call("PUT", "/api/data/links", {"where": {"site": "a", "device": 2}, "values": {"state": "down"}})[1]["updated"],
            1,
        )
        self.assertEqual(self.call("DELETE", "/api/data/links", {"where": {"site": "a", "device": 1}})[1]["deleted"], 1)


if __name__ == "__main__":
    unittest.main()

