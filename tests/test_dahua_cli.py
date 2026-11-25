import argparse
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dahua_cli as cli  # noqa: E402
from dahua_discovery import Device  # noqa: E402

DEVICES = [Device(ip="10.0.0.5", mac="00:11:22:33:44:55", serial="SN1", init_status=2),
           Device(ip="10.0.0.6", mac="00:11:22:33:44:66", serial="SN2", init_status=1)]


def fake_discover(_iface, timeout=3.0, unicast_targets=()):
    return list(DEVICES)


class CliTests(unittest.TestCase):
    def args(self, **kwargs):
        defaults = dict(interface=None, timeout=1, range=None, no_discover=False, http_port=80)
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_invocation_detection(self):
        self.assertTrue(cli.is_cli_invocation(["discover"]))
        self.assertTrue(cli.is_cli_invocation(["--cli", "interfaces"]))
        self.assertFalse(cli.is_cli_invocation([]))

    def test_parse_range(self):
        self.assertEqual(cli.parse_range("10.0.0.1-3"), ["10.0.0.1", "10.0.0.2", "10.0.0.3"])
        self.assertEqual(len(cli.parse_range("10.0.0.0/30")), 2)
        with self.assertRaises(cli.CliError):
            cli.parse_range("bad")

    def test_resolve_targets(self):
        cli.local_interfaces = lambda: [("eth0", "10.0.0.2", "255.255.255.0")]
        found = cli.resolve_targets(["00-11-22-33-44-66", "SN1"], self.args(), fake_discover)
        self.assertEqual([d.ip for d in found], ["10.0.0.6", "10.0.0.5"])
        with self.assertRaises(cli.CliError):
            cli.resolve_targets(["unknown-serial"], self.args(), fake_discover)

    def test_no_discover_requires_ip(self):
        with self.assertRaises(cli.CliError):
            cli.resolve_targets(["SN1"], self.args(no_discover=True))
        found = cli.resolve_targets(["10.0.0.9"], self.args(no_discover=True, http_port=8080))
        self.assertEqual((found[0].ip, found[0].http_port), ("10.0.0.9", 8080))

    def test_password_from_env(self):
        os.environ["TEST_DAHUA_PW"] = "Admin1234"
        try:
            args = argparse.Namespace(password_file=None, password_env="TEST_DAHUA_PW")
            self.assertEqual(cli.read_password(args), "Admin1234")
        finally:
            del os.environ["TEST_DAHUA_PW"]

    def test_parser_has_all_commands(self):
        parser = cli.build_parser()
        for command in cli.COMMANDS:
            if command == "interfaces":
                parser.parse_args([command])
        self.assertEqual(cli.main(["set-ip"]), cli.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
