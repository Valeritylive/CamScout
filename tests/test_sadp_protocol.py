"""Offline tests for sadp_protocol with a simulated device (no network)."""

import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import sadp_protocol as sp

MAC = "54-c4-15-10-8e-91"


class FakeDevice:
    """Plays the device side of the guessed protocol over an in-memory transport."""

    def __init__(self, password=None, reply_result="success", echo_uuid=True):
        self.password = password
        self.session_key = os.urandom(16).hex().encode()  # 32 hex chars
        self.reply_result = reply_result
        self.echo_uuid = echo_uuid
        self.sent = []
        self.inbox = []
        self.last_update = None

    # transport API
    def send(self, packet):
        self.sent.append(packet)
        req = sp.parse_packet(packet)
        # simulate multicast loopback of our own request
        self.inbox.append((packet, "192.168.1.10"))
        fields = [("Uuid", req["uuid"] if self.echo_uuid else "OTHER"), ("MAC", MAC), ("Types", req["types"])]
        if req["types"] == "exchangecode":
            pub = serialization.load_der_public_key(base64.b64decode(req["code"]))
            code = base64.b64encode(pub.encrypt(self.session_key, padding.PKCS1v15())).decode()
            fields.append(("Code", code))
        elif req["types"] == "activate":
            self.password = self._decrypt(req["password"])
            fields.append(("Result", self.reply_result))
        elif req["types"] == "update":
            self.last_update = req
            fields.append(("Result", self.reply_result))
        xml = "<ProbeMatch>" + "".join(f"<{k}>{v}</{k}>" for k, v in fields) + "</ProbeMatch>"
        self.inbox.append((xml.encode(), "192.0.0.64"))

    def receive(self, timeout):
        return self.inbox.pop(0) if self.inbox else None

    def _decrypt(self, value):
        key = bytes.fromhex(self.session_key.decode())
        d = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        plain = d.update(base64.b64decode(value)) + d.finalize()
        return plain[16:].rstrip(b"\0").decode()


class PacketTests(unittest.TestCase):
    def test_build_probe_order_and_mac(self):
        uid, packet = sp.build_probe("activate", [("Password", "x")], mac="54:C4:15:10:8E:91", uuid="U")
        text = packet.decode()
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="utf-8"?><Probe><Uuid>U</Uuid>'
                                        '<MAC>54-c4-15-10-8e-91</MAC><Types>activate</Types>'))
        self.assertEqual(uid, "U")

    def test_bad_mac(self):
        with self.assertRaises(sp.SadpError):
            sp.normalize_mac("12:34")

    def test_public_code_is_pkcs1_rsa1024(self):
        code = sp.KeyExchange.generate().public_code()
        self.assertTrue(code.startswith("MIGJAoGB"))
        self.assertTrue(code.endswith("AgMBAAE="))

    def test_password_ciphertext_length_matches_capture(self):
        # Capture: "HikVision" -> 44 base64 chars (32 bytes)
        value = sp.encrypt_password("HikVision", os.urandom(16))
        self.assertEqual(len(value), 44)

    def test_reply_result(self):
        self.assertEqual(sp.reply_result({"result": "success"})[0], True)
        self.assertEqual(sp.reply_result({"result": "failed", "description": "x"}), (False, "failed: x"))
        self.assertEqual(sp.reply_result({"result": "PasswordError"})[1], "wrong password")
        self.assertTrue(sp.reply_result({"types": "update"})[0])

    def test_own_request_is_not_a_reply(self):
        uid, packet = sp.build_probe("update", mac=MAC)
        self.assertFalse(sp.is_reply_to(sp.parse_packet(packet), uid, "update", MAC))

    def test_derive_key(self):
        self.assertEqual(sp.derive_aes_key(b"00" * 16), b"\0" * 16)
        self.assertEqual(len(sp.derive_aes_key(os.urandom(40))), 32)


class PasswordTests(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(sp.password_strength("abcdefgh"), 0)
        self.assertEqual(sp.password_strength("Admin1234"), 0)  # contains user name
        self.assertEqual(sp.password_strength("abcd1234"), 1)
        self.assertEqual(sp.password_strength("abcdEFGH"), 2)
        self.assertEqual(sp.password_strength("abcd!@#$"), 2)
        self.assertEqual(sp.password_strength("abCD1234"), 3)

    def test_check_new_password(self):
        sp.check_new_password("abCD1234", "abCD1234")
        for bad in ("short1A", "a" * 8, "abCD1234abCD12345"):
            with self.assertRaises(sp.SadpError):
                sp.check_new_password(bad)
        with self.assertRaises(sp.SadpError):
            sp.check_new_password("abCD1234", "other")


class FlowTests(unittest.TestCase):
    def test_activate_roundtrip(self):
        dev = FakeDevice()
        sp.activate(dev, MAC, "Secret123", timeout=1)
        self.assertEqual(dev.password, "Secret123")
        self.assertEqual([sp.parse_packet(p)["types"] for p in dev.sent], ["exchangecode", "activate"])

    def test_activate_rejected(self):
        with self.assertRaises(sp.SadpError):
            sp.activate(FakeDevice(reply_result="failed"), MAC, "Secret123", timeout=1)

    def test_short_password(self):
        with self.assertRaises(sp.SadpError):
            sp.activate(FakeDevice(), MAC, "short", timeout=1)

    def test_ipv6_validation(self):
        with self.assertRaises(sp.SadpError):
            sp.NetworkParams(dhcp=True, ipv6="not-an-ip").validate()
        sp.NetworkParams(dhcp=True, ipv6="fe80::1", ipv6_prefix=64).validate()

    def test_match_by_mac_when_uuid_differs(self):
        sp.activate(FakeDevice(echo_uuid=False), MAC, "Secret123", timeout=1)

    def test_modify_network_plain(self):
        dev = FakeDevice()
        params = sp.NetworkParams(ipv4="192.168.1.64", mask="255.255.255.0", gateway="192.168.1.1")
        sp.modify_network(dev, MAC, "Secret123", params, password_mode="plain", timeout=1)
        req = dev.last_update
        self.assertEqual(req["password"], "Secret123")
        self.assertEqual(req["ipv4address"], "192.168.1.64")
        self.assertEqual(req["dhcp"], "false")
        self.assertEqual(len(dev.sent), 1)

    def test_modify_network_auto_uses_aes_with_salt(self):
        dev = FakeDevice()
        params = sp.NetworkParams(dhcp=True)
        sp.modify_network(dev, MAC, "Secret123", params, probe_fields={"salt": "abc"}, timeout=1)
        self.assertEqual(dev._decrypt(dev.last_update["password"]), "Secret123")
        self.assertEqual(dev.last_update["dhcp"], "true")

    def test_modify_validates_gateway(self):
        params = sp.NetworkParams(ipv4="192.168.1.64", mask="255.255.255.0", gateway="10.0.0.1")
        with self.assertRaises(sp.SadpError):
            sp.modify_network(FakeDevice(), MAC, "x", params, password_mode="plain", timeout=1)

    def test_no_reply_times_out(self):
        class Silent:
            def send(self, packet): pass
            def receive(self, timeout): return None
        with self.assertRaises(sp.SadpError):
            sp.transact(Silent(), "inquiry", [], mac=MAC, timeout=0.05)


if __name__ == "__main__":
    unittest.main()
