import os
import sys
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dahua_api import DahuaHttp, DahuaHttpError, _query, parse_table  # noqa: E402

RESPONSES = {
    "/cgi-bin/magicBox.cgi?action=getSystemInfo": "deviceType=IPC-HFW2431S\r\nserialNumber=6J0A\r\nprocessor=X",
    "/cgi-bin/magicBox.cgi?action=getSoftwareVersion": "version=2.800.0000000.25.R",
    "/cgi-bin/configManager.cgi?action=getConfig&name=Network":
        "table.Network.DefaultInterface=eth0\r\ntable.Network.eth0.IPAddress=192.168.1.108\r\n"
        "table.Network.eth0.SubnetMask=255.255.255.0\r\ntable.Network.TCPPort=37777",
    "/cgi-bin/global.cgi?action=getCurrentTime": "result=2026-11-25 10:00:00",
    "/cgi-bin/userManager.cgi?action=getUserInfoAll":
        "users[0].Name=admin\r\nusers[0].Group=admin\r\nusers[1].Name=guest\r\nusers[1].Group=user",
    "/cgi-bin/upgrader.cgi?action=getState": "state=Succeeded\r\nprogress=100",
    "/cgi-bin/devVideoInput.cgi?action=getCollect": "result=16",
    "/cgi-bin/configManager.cgi?action=getConfig&name=T2UServer": "table.T2UServer.Enable=true",
    "/cgi-bin/configManager.cgi?action=getConfig&name=SIPServer":
        "table.SIPServer.Enable=true\r\ntable.SIPServer.IP=10.0.0.2",
}


class Handler(BaseHTTPRequestHandler):
    requests: list[str] = []
    bodies: list[bytes] = []

    def log_message(self, *args):
        pass

    def answer(self, text, status=200):
        data = text.encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = unquote(self.path)
        Handler.requests.append(path)
        if "denied" in self.headers.get("X-Test", ""):
            return self.answer("", 401)
        if path in RESPONSES:
            return self.answer(RESPONSES[path])
        if "action=setConfig" in path or "action=setCurrentTime" in path or "action=reboot" in path \
                or "modifyPassword" in path:
            return self.answer("OK\r\n")
        if "getDeviceType" in path:
            return self.answer("type=IPC")
        return self.answer("Error\r\nBad Request!", 400)

    def do_POST(self):
        Handler.requests.append(unquote(self.path))
        length = int(self.headers.get("Content-Length", "0"))
        Handler.bodies.append(self.rfile.read(length))
        self.answer("OK\r\n")


class ParseTests(unittest.TestCase):
    def test_parse_table(self):
        table = parse_table("table.NTP.Enable=true\r\ntable.NTP.Address=pool.ntp.org\r\nnoise\r\n")
        self.assertEqual(table, {"NTP.Enable": "true", "NTP.Address": "pool.ntp.org"})

    def test_query_keeps_brackets(self):
        self.assertEqual(_query([("Network.eth0.DnsServers[0]", "8.8.8.8"), ("time", "2026-01-01 10:00:00")]),
                         "Network.eth0.DnsServers[0]=8.8.8.8&time=2026-01-01%2010%3A00%3A00")


class FakeServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Handler.requests.clear()
        Handler.bodies.clear()
        self.client = DahuaHttp("127.0.0.1", self.server.server_port, "admin", "Admin1234", timeout=3)

    def test_system_info(self):
        info = self.client.system_info()
        self.assertEqual(info["deviceType"], "IPC-HFW2431S")
        self.assertEqual(info["SoftwareVersion"], "2.800.0000000.25.R")

    def test_network(self):
        iface, table = self.client.get_network()
        self.assertEqual(iface, "eth0")
        self.client.set_network("10.0.0.5", "255.255.255.0", "10.0.0.1", dns=("8.8.8.8", ""), iface=iface)
        sent = Handler.requests[-1]
        self.assertIn("Network.eth0.IPAddress=10.0.0.5", sent)
        self.assertIn("Network.eth0.DhcpEnable=false", sent)
        self.assertIn("Network.eth0.DnsServers[0]=8.8.8.8", sent)

    def test_time_and_users(self):
        self.assertEqual(self.client.get_time(), "2026-11-25 10:00:00")
        self.client.set_time(datetime(2026, 1, 2, 3, 4, 5))
        self.assertIn("time=2026-01-02 03:04:05", Handler.requests[-1])
        self.assertEqual([u["Name"] for u in self.client.users()], ["admin", "guest"])

    def test_errors(self):
        with self.assertRaises(DahuaHttpError) as ctx:
            self.client.get_config("Missing")
        self.assertEqual(ctx.exception.status, 400)

    def test_video_inputs_and_p2p(self):
        self.assertEqual(self.client.video_inputs(), 16)
        self.assertEqual(self.client.get_p2p(), {"T2UServer.Enable": "true"})
        self.client.set_p2p(False)
        self.assertIn("T2UServer.Enable=false", Handler.requests[-1])

    def test_existing_configs_skips_missing(self):
        found = self.client.existing_configs(["Missing", "SIPServer"])
        self.assertEqual(list(found), ["SIPServer"])
        self.assertEqual(found["SIPServer"]["SIPServer.IP"], "10.0.0.2")

    def test_fetch_details_fills_only_empty_fields(self):
        import dahua_ops
        from dahua_discovery import Device
        device = Device(ip="127.0.0.1", http_port=self.server.server_port, model="KEEP", protocol="HTTP")
        updated = dahua_ops.fetch_details(device, "admin", "Admin1234")
        self.assertEqual(updated.model, "KEEP")
        self.assertEqual(updated.serial, "6J0A")
        self.assertEqual(updated.firmware, "2.800.0000000.25.R")
        self.assertEqual(updated.subnet_mask, "255.255.255.0")
        self.assertEqual(updated.video_inputs, 16)
        self.assertEqual(device.serial, "")  # the original is not modified

    def test_rejected_login_stops_at_once(self):
        import dahua_ops
        from dahua_discovery import Device
        self.client.session.headers["X-Test"] = "denied"
        with self.assertRaises(DahuaHttpError) as ctx:
            self.client.system_info()
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(len(Handler.requests), 1)
        Handler.requests.clear()
        original = dahua_ops.http_client
        dahua_ops.http_client = lambda *a, **k: self.client
        try:
            with self.assertRaises(DahuaHttpError):
                dahua_ops.fetch_details(Device(ip="127.0.0.1"), "admin", "bad")
        finally:
            dahua_ops.http_client = original
        self.assertEqual(len(Handler.requests), 1)

    def test_upgrade(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as handle:
            handle.write(b"FIRMWARE" * 1000)
        try:
            import dahua_api
            original = dahua_api.time.sleep
            dahua_api.time.sleep = lambda _s: None
            try:
                states = []
                result = self.client.upgrade(handle.name, lambda s, p: states.append((s, p)))
            finally:
                dahua_api.time.sleep = original
        finally:
            os.unlink(handle.name)
        self.assertEqual(result, "Succeeded")
        self.assertIn("/cgi-bin/upgrader.cgi?action=uploadFirmware", Handler.requests)
        self.assertIn(b"FIRMWARE" * 1000, Handler.bodies[-1])
        self.assertEqual(states[-1], ("Succeeded", 100.0))


if __name__ == "__main__":
    unittest.main()
