"""A fake Dahua device answering DHIP on 127.0.0.1:37810, for tests without hardware."""

from __future__ import annotations

import base64
import json
import socket
import threading

from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from dahua_discovery import DHIP_PORT, build_frame, parse_frame

MAC = "3c:ef:8c:12:34:56"

DEVICE_INFO = {
    "AlarmInputChannels": 1, "AlarmOutputChannels": 1, "DeviceClass": "IPC",
    "DeviceType": "IPC-HFW2431S-S-S2", "HttpPort": 80, "HttpsPort": 443,
    "IPv4Address": {"DefaultGateway": "192.168.1.1", "DhcpEnable": False,
                    "IPAddress": "192.168.1.108", "SubnetMask": "255.255.255.0"},
    "IPv6Address": {"DefaultGateway": "", "IPAddress": "/64", "DhcpEnable": False},
    "Init": 1, "MachineName": "IPC", "Manufacturer": "Dahua", "Port": 37777,
    "PwdResetWay": 2, "SerialNo": "6J0A1B2PAZ12345", "Vendor": "Dahua",
    "Version": "2.800.0000000.25.R", "VideoInputChannels": 1, "mac": MAC,
}


class FakeDevice:
    def __init__(self, host: str = "127.0.0.1"):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        self.received: list[dict] = []
        self.decrypted: list[dict] = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, DHIP_PORT))
        self.sock.settimeout(0.2)
        self.running = True
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.running = False
        self.thread.join(2)
        self.sock.close()

    def decrypt(self, params: dict) -> dict:
        aes_key = self.key.decrypt(bytes.fromhex(params["salt"]), padding.PKCS1v15())
        decryptor = Cipher(algorithms.AES(aes_key), modes.ECB()).decryptor()
        plain = decryptor.update(base64.b64decode(params["content"])) + decryptor.finalize()
        return json.loads(plain.rstrip(b"\x00"))

    def reply(self, message: dict) -> dict | None:
        method = message.get("method")
        params = message.get("params") or {}
        if method == "DHDiscover.search":
            return {"method": "client.notifyDevInfo", "params": {"deviceInfo": DEVICE_INFO}, "mac": MAC}
        if params.get("mac") != MAC:
            return None
        if method == "Security.getEncryptInfo":
            numbers = self.key.public_key().public_numbers()
            return {"method": "client.notifyEncryptInfo", "mac": MAC, "params": {
                "result": True, "asymmetric": "RSA", "cipher": ["AES"],
                "pub": f"N:{numbers.n:X},E:{numbers.e:06X}"}}
        if method in ("DevInit.account", "PasswdFind.resetPassword"):
            content = self.decrypt(params)
            self.decrypted.append(content)
            return {"mac": MAC, "result": True, "params": None}
        if method == "DHDiscover.setConfig":
            if params.get("password") != "Admin1234":
                return {"mac": MAC, "result": False, "error": {"code": 268632080, "message": ""}}
            return {"mac": MAC, "result": True}
        if method == "PasswdFind.getDescript":
            return {"mac": MAC, "result": True, "params": {"description": "QRDATA-BASE64=="}}
        return {"mac": MAC, "result": False, "error": {"code": 405, "message": "Method not found"}}

    def loop(self) -> None:
        while self.running:
            try:
                data, peer = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            message = parse_frame(data)
            if not message:
                continue
            self.received.append(message)
            answer = self.reply(message)
            if answer is not None:
                self.sock.sendto(build_frame(answer), peer)
