import io
import json
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import dahua_discovery as dd  # noqa: E402
from fake_device import DEVICE_INFO, MAC, FakeDevice  # noqa: E402

LOOPBACK = ("lo", "127.0.0.1", "255.0.0.0")


class FrameTests(unittest.TestCase):
    def test_header_layout_matches_device_frames(self):
        frame = dd.build_frame({"method": "DHDiscover.search", "params": {"mac": "", "uni": 1}}, request_id=0)
        self.assertEqual(struct.unpack_from("<I", frame)[0], 32)
        self.assertEqual(frame[4:8], b"DHIP")  # not "PIHD": a byte-reversed magic silences most devices
        length = len(frame) - 32
        self.assertEqual(struct.unpack_from("<QQ", frame, 16), (length, length))
        self.assertEqual(json.loads(frame[32:])["method"], "DHDiscover.search")

    def test_parse_reply(self):
        frame = dd.build_frame({"method": "client.notifyDevInfo", "params": {"deviceInfo": DEVICE_INFO}})
        device = dd.parse_dhip_reply(frame + b"\x00\x00", "10.1.1.1")
        self.assertEqual(device.ip, "192.168.1.108")
        self.assertEqual(device.mac, MAC)
        self.assertEqual(device.model, "IPC-HFW2431S-S-S2")
        self.assertEqual(device.device_class, "IPC")
        self.assertEqual(device.http_port, 80)
        self.assertEqual(device.tcp_port, 37777)
        self.assertIs(device.initialized, False)
        self.assertEqual(device.ipv6, "")
        self.assertEqual(device.reset_ways_text(), "e-mail")
        self.assertEqual(dd.status_text(device), "Uninitialized")

    def test_init_status_values(self):
        for value, expected in ((0, True), (1, False), (2, True)):
            info = dict(DEVICE_INFO, Init=value)
            self.assertIs(dd.device_from_info(info).initialized, expected)
        info = dict(DEVICE_INFO)
        info.pop("Init")
        self.assertIsNone(dd.device_from_info(info).initialized)

    def test_garbage_is_ignored(self):
        self.assertIsNone(dd.parse_dhip_reply(b"\x00" * 40, "1.1.1.1"))
        self.assertIsNone(dd.parse_dhip_reply(dd.build_frame({"method": "x", "params": {}}), "1.1.1.1"))

    def test_mac_normalization(self):
        self.assertEqual(dd.normalize_mac("3C-EF-8C-12-34-56"), MAC)
        self.assertEqual(dd.normalize_mac("3cef8c123456"), MAC)

    def test_dvrip_reply(self):
        body = bytearray(200)
        struct.pack_into("<HHHH", body, 0, 3, 200, 1, 5)
        body[8:14] = b"DVR-01"
        body[24:28] = bytes([10, 0, 0, 5])
        body[28:32] = bytes([255, 255, 255, 0])
        body[32:36] = bytes([10, 0, 0, 1])
        struct.pack_into("<H", body, 62, 8080)
        struct.pack_into("<H", body, 66, 37777)
        tail = b"SerialNo:ABC123\r\nDeviceType:DH-XVR5104\r\nMAC:00:11:22:33:44:55\r\n"
        body[88:88 + len(tail)] = tail
        packet = bytes([0xB3]) + bytes(31) + bytes(body)
        device = dd.parse_dvrip_reply(packet, "10.0.0.5")
        self.assertEqual((device.ip, device.subnet_mask, device.gateway), ("10.0.0.5", "255.255.255.0", "10.0.0.1"))
        self.assertEqual((device.http_port, device.tcp_port), (8080, 37777))
        self.assertEqual((device.serial, device.model, device.mac), ("ABC123", "DH-XVR5104", "00:11:22:33:44:55"))
        self.assertEqual(device.protocol, "DVRIP")

    def test_ip_range(self):
        self.assertEqual(dd.ip_range("10.0.0.254", "10.0.1.1"), ["10.0.0.254", "10.0.0.255", "10.0.1.0", "10.0.1.1"])
        with self.assertRaises(ValueError):
            dd.ip_range("10.0.0.0", "10.1.0.0")

    def test_csv_round_trip(self):
        device = dd.device_from_info(DEVICE_INFO)
        buffer = io.StringIO()
        self.assertEqual(dd.write_devices_csv([device], buffer), 1)
        buffer.seek(0)
        loaded = dd.read_devices_csv(buffer)
        self.assertEqual(len(loaded), 1)
        self.assertEqual((loaded[0].ip, loaded[0].mac, loaded[0].model), (device.ip, device.mac, device.model))


class LiveDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDevice()

    def tearDown(self):
        self.fake.close()

    def test_unicast_search_finds_fake_device(self):
        found = dd.discover(LOOPBACK, timeout=1.0, unicast_targets=["127.0.0.1"], legacy=False)
        self.assertEqual([d.mac for d in found], [MAC])
        self.assertEqual(found[0].interface, LOOPBACK)

    def test_discover_all_merges_interfaces(self):
        found = dd.discover_all([LOOPBACK, LOOPBACK], timeout=0.8, unicast_targets=["127.0.0.1"], legacy=False)
        self.assertEqual([d.mac for d in found], [MAC])

    def test_wait_for_device_by_ip(self):
        device = dd.wait_for_device(MAC, [LOOPBACK], "127.0.0.1", timeout=5)
        self.assertIsNotNone(device)
        self.assertEqual(device.mac, MAC)

    def test_wait_for_device_gives_up(self):
        start = __import__("time").monotonic()
        self.assertIsNone(dd.wait_for_device("00:11:22:33:44:55", [LOOPBACK], "127.0.0.1", timeout=1))
        self.assertLess(__import__("time").monotonic() - start, 8)

    def test_packet_trace_masks_passwords(self):
        entries = []
        dd.packet_trace = lambda d, peer, data: entries.append(dd.format_packet(d, peer, data))
        try:
            dd.probe_ip("127.0.0.1", timeout=1.0)
        finally:
            dd.packet_trace = None
        self.assertTrue(any("→" in e for e in entries) and any("←" in e for e in entries))
        frame = dd.build_frame({"method": "DHDiscover.setConfig",
                                "params": {"userName": "admin", "password": "Secret123", "PwdResetWay": 3}})
        text = dd.format_packet("→", "239.255.255.251", frame)
        self.assertNotIn("Secret123", text)
        self.assertIn('"password": "***"', text)
        self.assertIn('"PwdResetWay": 3', text)

    def test_probe_ip(self):
        device = dd.probe_ip("127.0.0.1", timeout=1.0)
        self.assertIsNotNone(device)
        self.assertEqual(device.serial, "6J0A1B2PAZ12345")


if __name__ == "__main__":
    unittest.main()
