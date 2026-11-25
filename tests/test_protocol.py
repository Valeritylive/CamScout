import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import dahua_ops  # noqa: E402
import dahua_protocol as dp  # noqa: E402
from dahua_discovery import Device  # noqa: E402
from fake_device import MAC, FakeDevice  # noqa: E402

LOOPBACK = ("lo", "127.0.0.1", "255.0.0.0")


def fake_target(**kwargs) -> Device:
    return Device(ip="127.0.0.1", mac=MAC, interface=LOOPBACK, init_status=1, **kwargs)


class PasswordRuleTests(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(dp.password_problems("Admin1234"), [])
        self.assertTrue(dp.password_problems("short1"))
        self.assertTrue(dp.password_problems("onlyletters"))
        self.assertTrue(dp.password_problems("Admin12;34"))
        self.assertTrue(dp.password_problems("A1" * 17))

    def test_strength(self):
        self.assertEqual(dp.password_strength("")[0], 0)
        self.assertEqual(dp.password_strength("abcdefg1")[1], "Weak")
        self.assertEqual(dp.password_strength("Abcdefg1!x")[1], "Strong")

    def test_error_text(self):
        self.assertIn("wrong", dp.error_text({"code": 268632080, "message": ""}))


class EncryptInfoTests(unittest.TestCase):
    def test_parse_pub(self):
        info = dp.EncryptInfo.from_params({"pub": "N:00C1,E:010001", "cipher": ["AES"]})
        self.assertEqual((info.modulus, info.exponent, info.ciphers), (0xC1, 65537, ["AES"]))
        with self.assertRaises(dp.DahuaProtocolError):
            dp.EncryptInfo.from_params({"pub": "garbage"})


class FakeDeviceRpcTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDevice()
        dp.MulticastRpc.single_target_override = "127.0.0.1"

    def tearDown(self):
        dp.MulticastRpc.single_target_override = None
        self.fake.close()

    def test_init_sends_encrypted_password(self):
        dp.init_device(fake_target(), "Admin1234", email="me@example.com", timeout=2)
        content = self.fake.decrypted[-1]
        self.assertEqual(content["password"], "Admin1234")
        self.assertEqual(content["userName"], "admin")
        self.assertEqual(content["mail"], "me@example.com")
        self.assertEqual(content["pwdResetWay"], 2)
        methods = [m["method"] for m in self.fake.received]
        self.assertEqual(methods[-2:], ["Security.getEncryptInfo", "DevInit.account"])
        sent = self.fake.received[-1]["params"]
        self.assertEqual(sent["cipher"], "AES")
        self.assertNotIn("Admin1234", str(sent))

    def test_init_rejects_weak_password_before_sending(self):
        with self.assertRaises(dp.DahuaProtocolError):
            dp.init_device(fake_target(), "123", timeout=1)
        self.assertEqual(self.fake.received, [])

    def test_set_network_by_mac(self):
        dp.set_network(fake_target(), "admin", "Admin1234", "10.0.0.5", "255.255.255.0", "10.0.0.1", timeout=2)
        params = self.fake.received[-1]["params"]
        self.assertEqual(params["deviceConfig"]["IPv4Address"],
                         {"IPAddress": "10.0.0.5", "SubnetMask": "255.255.255.0",
                          "DefaultGateway": "10.0.0.1", "DhcpEnable": False})

    def test_wrong_password_is_reported(self):
        with self.assertRaises(dp.DahuaProtocolError) as ctx:
            dp.set_network(fake_target(), "admin", "Wrong1234", "10.0.0.5", "255.255.255.0", "10.0.0.1",
                           timeout=2)
        self.assertEqual(ctx.exception.code, 268632080)

    def test_modify_ip_does_not_retry_after_auth_failure(self):
        original = dahua_ops.same_subnet
        dahua_ops.same_subnet = lambda *_a: False  # other subnet: multicast first
        try:
            with self.assertRaises(dahua_ops.OperationError) as ctx:
                dahua_ops.modify_ip(fake_target(), "admin", "Wrong1234",
                                    dahua_ops.NetworkSettings("10.0.0.5", "255.255.255.0", "10.0.0.1"))
        finally:
            dahua_ops.same_subnet = original
        self.assertNotIn("HTTP", str(ctx.exception))

    def test_password_is_sent_once(self):
        with self.assertRaises(dp.DahuaProtocolError):
            dp.set_network(fake_target(), "admin", "Wrong1234", "10.0.0.5", "255.255.255.0", "10.0.0.1",
                           timeout=2)
        sent = [m for m in self.fake.received if m["method"] == "DHDiscover.setConfig"]
        self.assertEqual(len(sent), 1)

    def test_auth_requests_go_to_one_destination(self):
        self.assertNotIn("DHDiscover.setConfig", dp.READ_ONLY_METHODS)
        self.assertNotIn("PasswdFind.resetPassword", dp.READ_ONLY_METHODS)

    def test_reset_flow(self):
        result = dp.get_reset_descript(fake_target(), timeout=2)
        self.assertEqual(result["qr"], "QRDATA-BASE64==")
        dp.reset_password(fake_target(), " 123456 ", "Newpass123", timeout=2)
        self.assertEqual(self.fake.decrypted[-1]["securityCode"], "123456")
        self.assertEqual(self.fake.decrypted[-1]["pwd"], "Newpass123")

    def test_no_answer_for_other_mac(self):
        with self.assertRaises(dp.DahuaProtocolError):
            dp.MulticastRpc(LOOPBACK, timeout=1).call("Security.getEncryptInfo", "00:00:00:00:00:01",
                                                      device_ip="127.0.0.1")


class OpsTests(unittest.TestCase):
    def test_network_validation(self):
        with self.assertRaises(dahua_ops.OperationError):
            dahua_ops.NetworkSettings("10.0.0.5", "255.255.255.0", "10.0.1.1").validate()
        dahua_ops.NetworkSettings(dhcp=True).validate()

    def test_sequential_ips(self):
        self.assertEqual(dahua_ops.sequential_ips("10.0.0.10", 3), ["10.0.0.10", "10.0.0.11", "10.0.0.12"])
        with self.assertRaises(dahua_ops.OperationError):
            dahua_ops.sequential_ips("10.0.0.254", 3)


if __name__ == "__main__":
    unittest.main()
