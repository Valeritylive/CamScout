"""Tests for the headless CLI. Run: python3 -m unittest discover -s tests"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sadp_cli  # noqa: E402
from sadp_discovery import Device, parse_response, write_devices_csv  # noqa: E402

PROBE_MATCH = b"""<?xml version="1.0" encoding="UTF-8"?>
<ProbeMatch><Uuid>x</Uuid><Types>inquiry</Types>
<DeviceType>139</DeviceType><DeviceDescription>DS-2CD2043G2-I</DeviceDescription>
<DeviceSN>DS-2CD2043G2-I20220101AAWRJ00000001</DeviceSN><CommandPort>8000</CommandPort>
<HttpPort>80</HttpPort><MAC>44-19-b6-00-00-01</MAC><IPv4Address>192.0.0.64</IPv4Address>
<IPv4SubnetMask>255.255.255.0</IPv4SubnetMask><IPv4Gateway>192.0.0.1</IPv4Gateway>
<SoftwareVersion>V5.7.3build 220112</SoftwareVersion><Activated>false</Activated>
</ProbeMatch>"""

CAM_A = Device(ip="192.168.1.10", mac="44-19-b6-00-00-01", model="DS-2CD2043G2-I",
               serial="SN0001", activated=False)
CAM_B = Device(ip="192.168.1.11", mac="44-19-b6-00-00-02", model="DS-7608NI",
               serial="SN0002", activated=True, http_port=8080)


class FakeApi:
    class HikvisionError(RuntimeError):
        pass

    def __init__(self, fail_ips=()):
        self.fail_ips = set(fail_ips)
        self.calls = []

    def activate_device(self, ip, port, password, service_port):
        self.calls.append(("activate", ip, port, password, service_port))
        if ip in self.fail_ips:
            raise self.HikvisionError("HTTP 401")

    def get_network_interfaces(self, ip, port, user, password):
        return [{"id": "1", "ip": ip}]

    def set_ipv4(self, ip, port, user, password, interface_id, new_ip, mask, gateway, dns1, dns2, dhcp):
        self.calls.append(("set_ipv4", ip, port, interface_id, new_ip, mask, gateway, dhcp))
        if ip in self.fail_ips:
            raise self.HikvisionError("HTTP 401")


def run(argv, api=None, devices=(CAM_A, CAM_B), env=None):
    """Run the CLI with discovery and the ISAPI layer replaced by fakes."""
    out, err = io.StringIO(), io.StringIO()
    patches = [
        mock.patch.object(sadp_cli, "local_interfaces", return_value=[("eth0", "192.168.1.2", "255.255.255.0")]),
        mock.patch.object(sadp_cli, "discover", return_value=list(devices)),
        mock.patch.dict(os.environ, env or {}, clear=False),
    ]
    if api is not None:
        args = sadp_cli.build_parser().parse_args(argv)
        target = {"activate": sadp_cli.cmd_activate, "set-ip": sadp_cli.cmd_set_ip}[args.command]
        patches.append(mock.patch.object(args, "func", lambda a: target(a, api=api), create=True))
    with patches[0], patches[1], patches[2], redirect_stdout(out), redirect_stderr(err):
        if api is not None:
            with patches[3]:
                try:
                    code = args.func(args)
                except sadp_cli.CliError as exc:
                    print(f"Ошибка: {exc}", file=sys.stderr)
                    code = sadp_cli.EXIT_USAGE
        else:
            code = sadp_cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class DiscoveryTests(unittest.TestCase):
    def test_parse_probe_match(self):
        device = parse_response(PROBE_MATCH, "10.0.0.5")
        self.assertEqual(device.ip, "192.0.0.64")
        self.assertEqual(device.mac, "44-19-b6-00-00-01")
        self.assertFalse(device.activated)
        self.assertEqual(device.device_port, 8000)
        # Real ProbeMatch names: DeviceDescription is the model, DeviceSN the serial
        # (DeviceType is only a numeric class code).
        self.assertEqual(device.model, "DS-2CD2043G2-I")
        self.assertEqual(device.serial, "DS-2CD2043G2-I20220101AAWRJ00000001")

    def test_command_port_is_the_sdk_port(self):
        device = parse_response(PROBE_MATCH.replace(b"<CommandPort>8000", b"<CommandPort>8010"), "10.0.0.5")
        self.assertEqual(device.device_port, 8010)

    def test_csv_matches_gui_columns(self):
        buffer = io.StringIO()
        self.assertEqual(write_devices_csv([CAM_A, CAM_B], buffer), 2)
        lines = buffer.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("ID,Модель,Статус,IPv4"))
        self.assertIn("192.168.1.11", lines[2])


class CommandTests(unittest.TestCase):
    def test_discover_json_and_filter(self):
        code, out, _ = run(["discover", "--format", "json", "--inactive"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual([d["ip"] for d in data], ["192.168.1.10"])
        self.assertNotIn("raw", data[0])

    def test_discover_table(self):
        code, out, err = run(["discover"])
        self.assertEqual(code, 0)
        self.assertIn("192.168.1.11", out)
        self.assertIn("Найдено устройств: 2", err)

    def test_export_csv_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "devices.csv")
            code, _, _ = run(["export", str(path)])
            self.assertEqual(code, 0)
            self.assertTrue(path.read_bytes().startswith("﻿".encode()))
            self.assertEqual(len(path.read_text(encoding="utf-8-sig").splitlines()), 3)

    def test_unknown_interface(self):
        code, _, err = run(["discover", "-i", "wlan9"])
        self.assertEqual(code, 2)
        self.assertIn("wlan9", err)

    def test_activate_all_inactive_uses_env_password(self):
        api = FakeApi()
        code, out, _ = run(["activate", "--all-inactive"], api=api, env={"HIK_PASSWORD": "Str0ngPass"})
        self.assertEqual(code, 0)
        self.assertEqual(api.calls, [("activate", "192.168.1.10", 80, "Str0ngPass", 8000)])
        self.assertIn("OK", out)

    def test_activate_by_mac_any_separator_and_failure_exit_code(self):
        api = FakeApi(fail_ips={"192.168.1.10"})
        code, out, _ = run(["activate", "4419B6000001", "--password", "Str0ngPass", "--json"], api=api)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)[0]["ok"], False)

    def test_activate_skips_already_active(self):
        api = FakeApi()
        code, _, _ = run(["activate", "SN0002", "--password", "Str0ngPass"], api=api)
        self.assertEqual(code, 1)
        self.assertEqual(api.calls, [])

    def test_activate_rejects_short_password(self):
        code, _, err = run(["activate", "192.168.1.10", "--password", "short"], api=FakeApi())
        self.assertEqual(code, 2)
        self.assertIn("8", err)

    def test_activate_unknown_target(self):
        code, _, err = run(["activate", "nope", "--password", "Str0ngPass"], api=FakeApi())
        self.assertEqual(code, 2)
        self.assertIn("nope", err)

    def test_set_ip_batch_consecutive(self):
        api = FakeApi()
        code, _, err = run(["set-ip", "192.168.1.10", "SN0002", "--ip", "10.0.0.50", "--mask", "255.255.255.0",
                            "--gateway", "10.0.0.1", "--password", "pw", "--yes"], api=api)
        self.assertEqual(code, 0, err)
        self.assertEqual([c[4] for c in api.calls], ["10.0.0.50", "10.0.0.51"])
        self.assertEqual(api.calls[1][2], 8080)

    def test_set_ip_requires_confirmation_when_not_tty(self):
        api = FakeApi()
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            code, _, err = run(["set-ip", "192.168.1.10", "--dhcp", "--password", "pw"], api=api)
        self.assertEqual(code, 2)
        self.assertIn("--yes", err)
        self.assertEqual(api.calls, [])

    def test_set_ip_dry_run_and_validation(self):
        api = FakeApi()
        code, _, err = run(["set-ip", "192.168.1.10", "--ip", "10.0.0.5", "--mask", "255.255.255.0",
                            "--dry-run"], api=api)
        self.assertEqual(code, 0)
        self.assertIn("10.0.0.5", err)
        self.assertEqual(api.calls, [])
        code, _, err = run(["set-ip", "192.168.1.10", "--ip", "10.0.0.5", "--mask", "255.255.255.0",
                            "--gateway", "10.1.0.1", "--dry-run"], api=api)
        self.assertEqual(code, 2)

    def test_no_discover_requires_ip(self):
        code, _, err = run(["activate", "--no-discover", "SN0001", "--password", "Str0ngPass"], api=FakeApi())
        self.assertEqual(code, 2)

    def test_plan_rejects_broadcast(self):
        with self.assertRaises(sadp_cli.CliError):
            sadp_cli.plan_addresses(2, "10.0.0.254", "255.255.255.0", "")


ETH0 = ("eth0", "192.168.1.2", "255.255.255.0")


class NativeSadpTests(unittest.TestCase):
    """activate/set-ip go over SADP first, like the Windows tool and the GUI."""

    def setUp(self):
        from dataclasses import replace
        self.cam_a = replace(CAM_A, interface=ETH0)
        self.cam_b = replace(CAM_B, interface=ETH0)
        self.native = []

    def _run(self, argv, api, side_effect=None):
        def fake(device, password, *rest):
            self.native.append((device.mac, password, *rest))
            if side_effect:
                raise side_effect
        with mock.patch.object(sadp_cli, "native_activate", side_effect=fake), \
                mock.patch.object(sadp_cli, "native_set_network", side_effect=fake):
            return run(argv, api=api, devices=(self.cam_a, self.cam_b))

    def test_activate_uses_sadp_and_skips_isapi(self):
        api = FakeApi()
        code, _, err = self._run(["activate", "192.168.1.10", "--password", "Str0ngPass"], api)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.native, [("44-19-b6-00-00-01", "Str0ngPass")])
        self.assertEqual(api.calls, [])

    def test_activate_falls_back_to_isapi(self):
        from sadp_protocol import SadpTimeout
        api = FakeApi()
        code, _, err = self._run(["activate", "192.168.1.10", "--password", "Str0ngPass"], api,
                                 SadpTimeout("no reply"))
        self.assertEqual(code, 0, err)
        self.assertEqual([c[0] for c in api.calls], ["activate"])

    def test_activate_rejects_risky_password_like_sadp(self):
        code, _, err = self._run(["activate", "192.168.1.10", "--password", "abcdefgh"], FakeApi())
        self.assertEqual(code, 2)
        self.assertEqual(self.native, [])

    def test_set_ip_over_sadp_keeps_ports(self):
        api = FakeApi()
        code, _, err = self._run(["set-ip", "SN0002", "--ip", "10.0.0.50", "--mask", "255.255.255.0",
                                  "--gateway", "10.0.0.1", "--password", "pw", "--yes"], api)
        self.assertEqual(code, 0, err)
        self.assertEqual(api.calls, [])
        params = self.native[0][2]
        self.assertEqual((params.ipv4, params.gateway, params.http_port), ("10.0.0.50", "10.0.0.1", 8080))

    def test_set_ip_wrong_password_is_not_retried_over_isapi(self):
        from sadp_protocol import SadpError
        api = FakeApi()
        code, out, _ = self._run(["set-ip", "SN0002", "--dhcp", "--password", "pw", "--yes"], api,
                                 SadpError("неверный пароль"))
        self.assertEqual(code, 1)
        self.assertEqual(api.calls, [])
        self.assertIn("SADP", out)

    def test_set_ip_timeout_falls_back_to_isapi(self):
        from sadp_protocol import SadpTimeout
        api = FakeApi()
        code, _, err = self._run(["set-ip", "SN0002", "--dhcp", "--password", "pw", "--yes"], api,
                                 SadpTimeout("no reply"))
        self.assertEqual(code, 0, err)
        self.assertEqual([c[0] for c in api.calls], ["set_ipv4"])

    def test_dns_forces_isapi(self):
        api = FakeApi()
        code, _, err = self._run(["set-ip", "SN0002", "--dhcp", "--dns1", "8.8.8.8", "--password", "pw",
                                  "--yes"], api)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.native, [])

    def test_method_sadp_needs_discovery(self):
        code, _, err = self._run(["activate", "--no-discover", "10.0.0.9", "--method", "sadp",
                                  "--password", "Str0ngPass"], FakeApi())
        self.assertEqual(code, 2)


class EntryPointTests(unittest.TestCase):
    def test_app_py_cli_does_not_need_gtk(self):
        # A poisoned `gi` module proves the CLI path never imports GTK.
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "gi.py").write_text("raise ImportError('GTK must not be imported in CLI mode')\n")
            env = dict(os.environ, PYTHONPATH=tmp)
            result = subprocess.run([sys.executable, str(ROOT / "app.py"), "hikvision", "discover", "--help"],
                                    capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--format", result.stdout)


if __name__ == "__main__":
    unittest.main()
