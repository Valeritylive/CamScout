#!/usr/bin/env python3
"""Native ConfigTool commands over DHIP multicast (UDP 37810).

Like ConfigTool, these work even when the device is in another subnet (for example
the factory address 192.168.1.108), because they are addressed by MAC over the
multicast group rather than by IP.

There is no public specification. Method names come from public reverse
engineering of Dahua firmware (DahuaConsole, Midnight Blue's Lorex 2K write-up):

    Security.getEncryptInfo   -> {"asymmetric": "RSA", "pub": "N:<hex>,E:<hex>", "cipher": ["AES", ...]}
    DevInit.account           initialize (set the admin password of a new device)
    DHDiscover.setConfig      change IPv4 settings: userName, password, deviceConfig
    PasswdFind.getDescript    QR-code text for the password reset
    PasswdFind.resetPassword  reset with the security code from Dahua support / app

Sensitive requests use an envelope ``{"cipher": "AES", "salt": RSA(key),
"content": AES-ECB(zero-padded JSON)}``, as documented for PasswdFind.resetPassword.
Places that are reconstructed without a capture from a real device are marked
``GUESS:`` so they can be checked with ``--dump`` against hardware.
"""

from __future__ import annotations

from i18n import tr
import argparse
import base64
import json
import secrets
import select
import socket
import sys
import time
from dataclasses import dataclass
from typing import Any

from dahua_discovery import (
    DHIP_GROUP,
    DHIP_PORT,
    DISCOVERY_LOCK,
    Device,
    build_frame,
    normalize_mac,
    parse_frame,
    trace,
)

DAHUA_ERRORS = {
    268632079: "re-authorization required",
    268632080: "wrong user name or password",
    268632081: "the account is locked",
    268632082: "the account is blacklisted",
    268632083: "the account is already in use",
    268894210: "insufficient rights",
    285409284: "the account does not exist",
    285409285: "the account already exists",
    405: "the device does not support this method",
    403: "access denied",
}

# Characters the Dahua web interface and ConfigTool refuse in passwords.
FORBIDDEN_PASSWORD_CHARS = "'\";:&"


class DahuaProtocolError(Exception):
    def __init__(self, message: str, code: int | None = None, password_rejected: bool = False):
        super().__init__(message)
        self.code = code
        self.password_rejected = password_rejected


def error_text(error: Any) -> str:
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message") or ""
        known = tr(DAHUA_ERRORS[code]) if isinstance(code, int) and code in DAHUA_ERRORS else None
        parts = [p for p in (known, message, tr("code {code}").format(code=code) if code is not None else "") if p]
        return ", ".join(parts) or tr("device error")
    return str(error or tr("device error"))


def password_problems(password: str) -> list[str]:
    """ConfigTool rules: 8–32 chars, at least two of digits/letters/symbols."""
    problems = []
    if not 8 <= len(password) <= 32:
        problems.append(tr("length 8–32 characters"))
    kinds = sum((any(c.isdigit() for c in password),
                 any(c.isalpha() for c in password),
                 any(not c.isalnum() for c in password)))
    if kinds < 2:
        problems.append(tr("at least two kinds: digits, letters, symbols"))
    bad = sorted({c for c in password if c in FORBIDDEN_PASSWORD_CHARS})
    if bad:
        problems.append(tr("forbidden characters:") + " " + " ".join(bad))
    return problems


def password_strength(password: str) -> tuple[int, str]:
    """0..3 and a label, like the strength bar of the Dahua init dialog."""
    if not password:
        return 0, ""
    kinds = sum((any(c.isdigit() for c in password), any(c.islower() for c in password),
                 any(c.isupper() for c in password), any(not c.isalnum() for c in password)))
    if password_problems(password):
        return 0, tr("Weak")
    if kinds >= 3 and len(password) >= 10:
        return 3, tr("Strong")
    if kinds >= 3 or len(password) >= 12:
        return 2, tr("Medium")
    return 1, tr("Weak")


@dataclass
class EncryptInfo:
    modulus: int
    exponent: int
    ciphers: list[str]

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "EncryptInfo":
        pub = str(params.get("pub") or "")
        parts = dict(part.split(":", 1) for part in pub.split(",") if ":" in part)
        try:
            modulus = int(parts["N"].strip(), 16)
            exponent = int(parts.get("E", "010001").strip(), 16)
        except (KeyError, ValueError) as exc:
            raise DahuaProtocolError(tr("Invalid device public key: {key}").format(key=pub[:40])) from exc
        ciphers = params.get("cipher") or ["AES"]
        if isinstance(ciphers, str):
            ciphers = [ciphers]
        return cls(modulus, exponent, [str(c) for c in ciphers])


def _zero_pad(data: bytes, block: int = 16) -> bytes:
    remainder = len(data) % block
    return data + b"\x00" * ((block - remainder) % block)


def encrypt_envelope(content: dict[str, Any], info: EncryptInfo, key: bytes | None = None) -> dict[str, str]:
    """Wrap *content* as {cipher, salt, content} for the device.

    GUESS: the AES key is 16 random hex characters used as raw AES-128 key bytes,
    salt is the RSA-PKCS#1 v1.5 encryption of that key as lowercase hex, and content
    is base64 of AES-ECB with zero padding (the Dahua web client does the same for
    its encrypted RPC calls). Check with --dump on a real device.
    """
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = key or secrets.token_hex(8).encode("ascii")
    public_key = rsa.RSAPublicNumbers(info.exponent, info.modulus).public_key()
    salt = public_key.encrypt(key, padding.PKCS1v15()).hex()
    plaintext = _zero_pad(json.dumps(content, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    encrypted = encryptor.update(plaintext) + encryptor.finalize()
    return {"cipher": "AES", "salt": salt, "content": base64.b64encode(encrypted).decode("ascii")}


# Requests that change nothing on the device; only these are retransmitted and
# sent to several destinations at once. Anything carrying a password goes out
# exactly once to one destination: every copy the device receives counts as a
# login attempt, and Dahua locks the account after a few failures.
READ_ONLY_METHODS = frozenset({"Security.getEncryptInfo", "PasswdFind.getDescript", "DHDiscover.search"})


class MulticastRpc:
    """One request/response exchange with a device addressed by MAC."""

    # Tests point this at a fake device on 127.0.0.1 (loopback has no multicast).
    single_target_override: str | None = None

    def __init__(self, interface: tuple[str, str, str] | None, timeout: float = 4.0, dump: bool = False):
        self.interface = interface
        self.timeout = timeout
        self.dump = dump

    def _log(self, direction: str, peer: str, data: bytes) -> None:
        trace(direction, peer, data)
        self._log_dump(direction, peer, data)

    def _log_dump(self, direction: str, peer: str, data: bytes) -> None:
        if not self.dump:
            return
        message = parse_frame(data)
        text = json.dumps(message, ensure_ascii=False, indent=2) if message else data[:200].hex()
        print(f"--- {direction} {peer} ({len(data)} bytes)\n{text}", file=sys.stderr)

    def call(self, method: str, mac: str, params: dict[str, Any] | None = None,
             device_ip: str = "") -> dict[str, Any]:
        """Send *method* to the device with *mac*; return the reply message.

        Replies come back either unicast to our port or to the multicast group, so
        both are read. Only messages carrying the same MAC are accepted.
        """
        mac = normalize_mac(mac)
        body = {"method": method, "params": {"mac": mac, "uni": 1, **(params or {})}}
        frame = build_frame(body)
        local_ip = self.interface[1] if self.interface else "0.0.0.0"
        read_only = method in READ_ONLY_METHODS
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sender.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                sender.bind((local_ip, 0))
            except OSError:
                sender.bind(("", 0))
            if local_ip != "0.0.0.0":
                try:
                    sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
                except OSError:
                    pass
            sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sender.setblocking(False)
        except OSError:
            sender.close()
            raise
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sockets = [sender]
        try:
            listener.bind(("", DHIP_PORT))
            try:
                listener.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                    socket.inet_aton(DHIP_GROUP) + socket.inet_aton(local_ip))
            except OSError:
                pass
            listener.setblocking(False)
            sockets.append(listener)
        except OSError:
            listener.close()
        with DISCOVERY_LOCK:
            try:
                if self.single_target_override:
                    targets = [self.single_target_override]
                elif read_only:
                    targets = [DHIP_GROUP, "255.255.255.255"] + ([device_ip] if device_ip else [])
                else:
                    targets = [DHIP_GROUP]
                deadline = time.monotonic() + self.timeout
                next_send = 0.0
                sends = 0
                max_sends = 3 if read_only else 1
                while time.monotonic() < deadline:
                    if time.monotonic() >= next_send and sends < max_sends:
                        for target in targets:
                            try:
                                sender.sendto(frame, (target, DHIP_PORT))
                                self._log("→", target, frame)
                            except OSError:
                                pass
                        sends += 1
                        next_send = time.monotonic() + 1.2
                    readable, _w, _x = select.select(sockets, [], [], 0.1)
                    for sock in readable:
                        while True:
                            try:
                                packet, peer = sock.recvfrom(65535)
                            except (BlockingIOError, InterruptedError, OSError):
                                break
                            message = parse_frame(packet)
                            if message is None or message.get("method") != method:
                                trace("←", peer[0], packet)
                            if not message or message.get("method") == method:
                                continue  # our own request looped back by multicast
                            reply_mac = normalize_mac(str(message.get("mac")
                                                          or (message.get("params") or {}).get("mac") or ""))
                            if reply_mac != mac:
                                continue
                            params_reply = message.get("params") or {}
                            if isinstance(params_reply, dict) and "deviceInfo" in params_reply \
                                    and "result" not in message and "result" not in params_reply:
                                continue  # a discovery answer to somebody's search
                            if self.dump:
                                self._log_dump("←", peer[0], packet)
                            return message
                raise DahuaProtocolError(tr("No reply from device {mac} to {method}").format(mac=mac, method=method))
            finally:
                for sock in sockets:
                    sock.close()


def _result_ok(message: dict[str, Any]) -> tuple[bool, Any]:
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    result = message.get("result", params.get("result"))
    error = message.get("error") or params.get("error")
    if result is None and error is None:
        return True, None
    return bool(result) and not error, error


def get_encrypt_info(rpc: MulticastRpc, device: Device) -> EncryptInfo:
    message = rpc.call("Security.getEncryptInfo", device.mac, device_ip=device.ip)
    ok, error = _result_ok(message)
    params = message.get("params") or {}
    if not ok or not params.get("pub"):
        raise DahuaProtocolError(tr("The device did not provide an encryption key:") + " " + error_text(error))
    return EncryptInfo.from_params(params)


def _encrypted_call(rpc: MulticastRpc, device: Device, method: str, content: dict[str, Any]) -> dict[str, Any]:
    info = get_encrypt_info(rpc, device)
    envelope = encrypt_envelope(content, info)
    message = rpc.call(method, device.mac, envelope, device_ip=device.ip)
    ok, error = _result_ok(message)
    if not ok:
        code = error.get("code") if isinstance(error, dict) else None
        raise DahuaProtocolError(f"{method}: {error_text(error)}", code)
    return message


def init_device(device: Device, password: str, email: str = "", phone: str = "",
                username: str = "admin", timeout: float = 4.0, dump: bool = False) -> None:
    """Initialize (activate) a new device: set the admin password and reset contact.

    GUESS: content field names follow NetSDK NET_IN_INIT_DEVICE_ACCOUNT
    (szMac, szUserName, szPwd, szCellPhone, szMail, byPwdResetWay).
    """
    problems = password_problems(password)
    if problems:
        raise DahuaProtocolError(tr("Unsuitable password:") + " " + "; ".join(problems), password_rejected=True)
    way = (0x1 if phone else 0) | (0x2 if email else 0)
    content = {"mac": normalize_mac(device.mac), "userName": username, "password": password,
               "pwdResetWay": way, "mail": email, "cellPhone": phone}
    _encrypted_call(MulticastRpc(device.interface, timeout, dump), device, "DevInit.account", content)


def set_network(device: Device, username: str, password: str, ip: str, mask: str, gateway: str,
                dhcp: bool = False, timeout: float = 4.0, dump: bool = False) -> None:
    """Change IPv4 settings by MAC, as ConfigTool does for devices in another subnet.

    The device handler reads params.userName, params.password and
    params.deviceConfig (seen in decompiled firmware); deviceConfig mirrors the
    RemoteIPModified event: IPv4Address {IPAddress, SubnetMask, DefaultGateway, DhcpEnable}.
    GUESS: the password is sent as is. That means it crosses the LAN in clear
    text inside one multicast datagram, so the GUI and README say so; devices in
    our own subnet are configured over HTTP digest instead. Firmwares that
    require encryption answer with an error, and the caller then falls back to
    the HTTP API.
    """
    params = {
        "userName": username,
        "password": password,
        "deviceConfig": {"IPv4Address": {"IPAddress": ip, "SubnetMask": mask,
                                         "DefaultGateway": gateway, "DhcpEnable": bool(dhcp)}},
    }
    rpc = MulticastRpc(device.interface, timeout, dump)
    message = rpc.call("DHDiscover.setConfig", device.mac, params, device_ip=device.ip)
    ok, error = _result_ok(message)
    if not ok:
        code = error.get("code") if isinstance(error, dict) else None
        raise DahuaProtocolError(tr("IP change rejected:") + " " + error_text(error), code)


def get_reset_descript(device: Device, timeout: float = 4.0, dump: bool = False) -> dict[str, Any]:
    """Return the password-reset description: QR text and, if given, contact info.

    PasswdFind.getDescript answers with a base64 blob (encrypted with Dahua's
    support key) that ConfigTool shows as a QR code; the DMSS app or Dahua support
    turn it into a security code. GUESS: reply keys vary ("description",
    "content", "QRCode"); every string field is kept.
    """
    rpc = MulticastRpc(device.interface, timeout, dump)
    message = rpc.call("PasswdFind.getDescript", device.mac, device_ip=device.ip)
    ok, error = _result_ok(message)
    if not ok:
        raise DahuaProtocolError(tr("The device did not provide reset data:") + " " + error_text(error))
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    qr = ""
    for key in ("description", "Description", "content", "QRCode", "qrCode", "descript"):
        if isinstance(params.get(key), str) and params.get(key):
            qr = params[key]
            break
    return {"qr": qr, "params": params}


def reset_password(device: Device, security_code: str, new_password: str, username: str = "admin",
                   timeout: float = 4.0, dump: bool = False) -> None:
    """Reset the admin password with the security code (PasswdFind.resetPassword).

    GUESS: content keys {"userName", "pwd", "securityCode"}; the envelope format
    is documented, the decrypted content is not.
    """
    problems = password_problems(new_password)
    if problems:
        raise DahuaProtocolError(tr("Unsuitable password:") + " " + "; ".join(problems), password_rejected=True)
    content = {"mac": normalize_mac(device.mac), "userName": username, "pwd": new_password,
               "securityCode": security_code.strip()}
    _encrypted_call(MulticastRpc(device.interface, timeout, dump), device, "PasswdFind.resetPassword", content)


def _device_for_cli(args: argparse.Namespace) -> Device:
    interface = None
    if args.local:
        address, _, prefix = args.local.partition("/")
        import ipaddress
        mask = str(ipaddress.IPv4Network(f"0.0.0.0/{prefix or 24}").netmask)
        interface = ("cli", address, mask)
    return Device(mac=args.mac, ip=args.ip_hint or "", interface=interface)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Команды DHIP-мультикаста для отладки на реальном устройстве")
    parser.add_argument("--local", help="локальный адрес/префикс, например 192.168.1.10/24")
    parser.add_argument("--dump", action="store_true", help="печатать отправленные и полученные пакеты")
    parser.add_argument("--timeout", type=float, default=4.0)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("encrypt-info", "init", "network", "descript", "reset", "raw"):
        p = sub.add_parser(name)
        p.add_argument("--mac", required=True)
        p.add_argument("--ip-hint", help="текущий IP устройства (дополнительно шлётся unicast)")
        if name in ("init", "network", "reset"):
            p.add_argument("--password", required=True)
            p.add_argument("--user", default="admin")
        if name == "init":
            p.add_argument("--email", default="")
            p.add_argument("--phone", default="")
        if name == "network":
            p.add_argument("--ip", default="")
            p.add_argument("--mask", default="255.255.255.0")
            p.add_argument("--gateway", default="")
            p.add_argument("--dhcp", action="store_true")
        if name == "reset":
            p.add_argument("--code", required=True)
        if name == "raw":
            p.add_argument("method")
            p.add_argument("--params", default="{}", help="JSON-параметры")
    args = parser.parse_args(argv)
    device = _device_for_cli(args)
    try:
        if args.command == "encrypt-info":
            info = get_encrypt_info(MulticastRpc(device.interface, args.timeout, args.dump), device)
            print(f"RSA {info.modulus.bit_length()} бит, e={info.exponent}, шифры: {', '.join(info.ciphers)}")
        elif args.command == "init":
            init_device(device, args.password, args.email, args.phone, args.user, args.timeout, args.dump)
            print("Устройство инициализировано")
        elif args.command == "network":
            set_network(device, args.user, args.password, args.ip, args.mask, args.gateway, args.dhcp,
                        args.timeout, args.dump)
            print("Сетевые параметры отправлены")
        elif args.command == "descript":
            print(json.dumps(get_reset_descript(device, args.timeout, args.dump), ensure_ascii=False, indent=2))
        elif args.command == "reset":
            reset_password(device, args.code, args.password, args.user, args.timeout, args.dump)
            print("Пароль сброшен")
        elif args.command == "raw":
            message = MulticastRpc(device.interface, args.timeout, True).call(
                args.method, device.mac, json.loads(args.params), device_ip=device.ip)
            print(json.dumps(message, ensure_ascii=False, indent=2))
    except (DahuaProtocolError, OSError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
