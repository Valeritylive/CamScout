#!/usr/bin/env python3
"""Native SADP commands over UDP 37020 (multicast 239.255.255.250).

Discovery already works over this transport. This module adds the commands that
the official SADP tool sends over the same channel, so a device can be activated
or re-addressed even when it sits in a foreign subnet (for example the factory
default 192.0.0.64) and its HTTP port is unreachable.

Nothing here is based on an official specification. What is known:

* Element names of the request templates (``update``, ``activate``,
  ``exchangecode``, ``reset``, ``getcode``) were recovered from Sadp.dll by
  third-party reverse engineering.
* A real capture of the official tool shows the activation flow
  ``exchangecode`` -> ``activate``, where the tool's ``<Code>`` is a base64
  DER PKCS#1 RSA-1024 public key and ``<Password>`` is 32 bytes of base64.

Everything that is inferred rather than observed is marked ``GUESS:`` below.
Use ``python3 sadp_protocol.py --dump ...`` against a real device to see the
raw packets and confirm or correct these guesses.
"""

from __future__ import annotations

from i18n import tr
import argparse
import base64
import ipaddress
import re
import secrets
import socket
import sys
import time
import uuid as uuidlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, Iterable

PORT = 37020
GROUP = "239.255.255.250"

PASSWORD_MODES = ("auto", "aes", "plain")


class SadpError(RuntimeError):
    pass


class SadpTimeout(SadpError):
    """The device sent no matching reply. Nothing was accepted, so another transport may be tried."""


# --------------------------------------------------------------------------- packets

def normalize_mac(mac: str) -> str:
    """Return the MAC as SADP prints it in ProbeMatch: lowercase, dash separated."""
    digits = re.sub(r"[^0-9a-fA-F]", "", mac or "")
    if len(digits) != 12:
        raise SadpError(tr("Invalid MAC address: {mac}").format(mac=repr(mac)))
    return "-".join(digits[i:i + 2] for i in range(0, 12, 2)).lower()


def new_uuid() -> str:
    # The official tool sends upper-case UUIDs with dashes.
    return str(uuidlib.uuid4()).upper()


def build_probe(types: str, fields: Iterable[tuple[str, str]] = (), *, uuid: str | None = None,
                mac: str | None = None) -> tuple[str, bytes]:
    """Build a ``<Probe>`` request. Returns (uuid, packet bytes).

    Element order follows the captured exchangecode packet: Uuid, MAC, Types, rest.
    """
    uid = uuid or new_uuid()
    root = ET.Element("Probe")
    ET.SubElement(root, "Uuid").text = uid
    if mac:
        ET.SubElement(root, "MAC").text = normalize_mac(mac)
    ET.SubElement(root, "Types").text = types
    for name, value in fields:
        ET.SubElement(root, name).text = value
    body = ET.tostring(root, encoding="unicode")
    return uid, f'<?xml version="1.0" encoding="utf-8"?>{body}'.encode("utf-8")


def parse_packet(data: bytes) -> dict[str, str] | None:
    """Flatten an SADP XML packet into {lowercase tag: text}. Root tag is under ``_root``."""
    try:
        root = ET.fromstring(data.decode("utf-8", errors="replace"))
    except ET.ParseError:
        return None
    fields = {"_root": root.tag.rsplit("}", 1)[-1]}
    for node in root.iter():
        if node is root:
            continue
        fields[node.tag.rsplit("}", 1)[-1].lower()] = (node.text or "").strip()
    return fields


# GUESS: the reply carries <Result>, and these are its success / failure words.
# Captures of the reply body have not been published. If a device answers with a
# different element we fall back to "no explicit failure means success" and keep
# the raw reply for the user to inspect.
_SUCCESS_WORDS = {"success", "succ", "succeed", "ok", "true", "0"}
_FAILURE_WORDS = {"failed", "fail", "failure", "false", "error", "denied"}
_FAILURE_HINTS = {
    # GUESS: wording seen in SADP UI error messages; mapping to wire values unknown.
    "passworderror": "wrong password",
    "pwerror": "wrong password",
    "locked": "the device is locked after wrong attempts",
    "riskpassword": "the password is too simple",
    "weakpassword": "the password is too simple",
}


def reply_result(reply: dict[str, str]) -> tuple[bool, str]:
    """Interpret a command reply. Returns (ok, human readable reason)."""
    for key in ("result", "status", "statuscode", "retcode"):
        value = reply.get(key)
        if value is None:
            continue
        low = value.strip().lower()
        if low in _SUCCESS_WORDS:
            return True, value
        hint = _FAILURE_HINTS.get(low.replace(" ", "").replace("_", ""))
        detail = reply.get("description") or reply.get("errordescription") or reply.get("reason") or ""
        if low:
            text = tr(hint) if hint else value
            return False, f"{text}{(': ' + detail) if detail else ''}"
    for key in reply:
        hint = _FAILURE_HINTS.get(key)
        if hint and reply[key].strip().lower() in ("true", "1"):
            return False, tr(hint)
    return True, tr("reply without a Result field")


def is_reply_to(reply: dict[str, str], uid: str, types: str, mac: str | None) -> bool:
    """Decide whether a received packet answers our request.

    GUESS: the device echoes our Uuid and Types in a ProbeMatch. Our own request,
    which comes back through multicast loopback, has root ``Probe`` and is skipped.
    """
    if reply.get("_root", "").lower() == "probe":
        return False
    if reply.get("uuid", "").upper() == uid.upper():
        return True
    if mac and reply.get("mac") and reply.get("types", "").lower() == types.lower():
        try:
            return normalize_mac(reply["mac"]) == normalize_mac(mac)
        except SadpError:
            return False
    return False


# --------------------------------------------------------------------------- passwords

# English source text, shown through tr().
PASSWORD_LEVELS = ("Risky", "Weak", "Medium", "Strong")


def password_strength(password: str, username: str = "admin") -> int:
    """Password level as the Windows SADP tool shows it (0 risky .. 3 strong).

    Rules from Hikvision manuals: risky if shorter than 8, one character class,
    or equal to / containing the user name; weak for digits + one letter case;
    medium for other two-class mixes; strong for three or more classes.
    """
    if len(password) < 8 or (username and username.lower() in password.lower()):
        return 0
    classes = {
        "digit": any(c.isdigit() for c in password),
        "lower": any(c.islower() for c in password),
        "upper": any(c.isupper() for c in password),
        "special": any(not c.isalnum() for c in password),
    }
    count = sum(classes.values())
    if count <= 1:
        return 0
    if count >= 3:
        return 3
    if classes["digit"] and (classes["lower"] or classes["upper"]):
        return 1
    return 2


def check_new_password(password: str, confirm: str | None = None, username: str = "admin") -> None:
    """Same acceptance rules as Windows SADP activation: 8-16 chars, not risky."""
    if not 8 <= len(password) <= 16:
        raise SadpError(tr("The password must be 8 to 16 characters long."))
    if password_strength(password, username) == 0:
        raise SadpError(tr("Risky password: use at least two kinds of characters (digits, lower case, "
                           "upper case, symbols) and do not include the user name."))
    if confirm is not None and password != confirm:
        raise SadpError(tr("Passwords do not match."))


# --------------------------------------------------------------------------- crypto

@dataclass
class KeyExchange:
    """RSA-1024 key pair used for the ``exchangecode`` step."""

    private_key: object = field(repr=False)

    @classmethod
    def generate(cls) -> "KeyExchange":
        from cryptography.hazmat.primitives.asymmetric import rsa
        return cls(rsa.generate_private_key(public_exponent=65537, key_size=1024))

    def public_code(self) -> str:
        """Base64 DER PKCS#1 RSAPublicKey (begins ``MIGJAoGB``), as in the captured packet."""
        from cryptography.hazmat.primitives import serialization
        der = self.private_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.PKCS1)
        return base64.b64encode(der).decode("ascii")

    def decrypt_code(self, code: str) -> bytes:
        """Decrypt the device's ``<Code>`` reply into an AES key.

        GUESS: the device returns base64 RSA-PKCS#1 v1.5 ciphertext of a random
        session key, the same way ISAPI /Security/challenge does. If the plain
        text is a hex string, the key is the decoded bytes; otherwise the raw
        bytes are used. The key is cut to 16/24/32 bytes.
        """
        from cryptography.hazmat.primitives.asymmetric import padding
        try:
            blob = base64.b64decode(code, validate=False)
        except ValueError as exc:
            raise SadpError(tr("The device returned an invalid Code (not base64).")) from exc
        try:
            plain = self.private_key.decrypt(blob, padding.PKCS1v15())
        except ValueError:
            try:
                plain = self.private_key.decrypt(blob, padding.OAEP(
                    mgf=padding.MGF1(algorithm=_sha1()), algorithm=_sha1(), label=None))
            except ValueError as exc:
                raise SadpError(tr("Could not decrypt the exchange key from the device reply.")) from exc
        return derive_aes_key(plain)


def _sha1():
    from cryptography.hazmat.primitives import hashes
    return hashes.SHA1()


def derive_aes_key(material: bytes) -> bytes:
    text = material.strip(b"\0").strip()
    if re.fullmatch(rb"[0-9a-fA-F]{32,64}", text) and len(text) % 2 == 0:
        material = bytes.fromhex(text.decode("ascii"))
    for size in (32, 24, 16):
        if len(material) >= size:
            return material[:size]
    raise SadpError(tr("The exchange key from the device is too short ({size} bytes).").format(size=len(material)))


def encrypt_password(password: str, key: bytes) -> str:
    """Encrypt the admin password with the exchanged key, return base64.

    Observed: "HikVision" (9 bytes) became 32 bytes of ciphertext.
    GUESS: AES-ECB over a 16-byte random prefix + password, zero padded. That is
    the ISAPI activation scheme and yields exactly 32 bytes for a 9-char password.
    A plain zero-padded password would have produced 16 bytes, so the prefix (or
    an equivalent second block) is very likely. The device is expected to drop
    the first 16 bytes after decryption.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    data = secrets.token_bytes(16) + password.encode("utf-8")
    data += b"\0" * ((-len(data)) % 16)
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return base64.b64encode(encryptor.update(data) + encryptor.finalize()).decode("ascii")


# --------------------------------------------------------------------------- transport

DumpFn = Callable[[str, bytes, str], None]


class SadpTransport:
    """UDP socket bound to 37020 on one interface; sends to multicast and broadcast."""

    def __init__(self, address: str, netmask: str = "255.255.255.0", *, dump: DumpFn | None = None):
        self.address = address
        self.netmask = netmask
        self.dump = dump
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            # Devices answer to the multicast group on 37020, so we must own that port.
            self.sock.bind(("", PORT))
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            try:
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                     socket.inet_aton(GROUP) + socket.inet_aton(address))
            except OSError:
                pass
        except OSError:
            self.sock.close()
            raise
        self.targets = [GROUP, "255.255.255.255"]
        try:
            bcast = str(ipaddress.IPv4Network(f"{address}/{netmask}", strict=False).broadcast_address)
            if bcast not in self.targets:
                self.targets.append(bcast)
        except ValueError:
            pass

    def close(self) -> None:
        self.sock.close()

    def __enter__(self) -> "SadpTransport":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def send(self, packet: bytes) -> None:
        if self.dump:
            self.dump(">>", packet, ",".join(self.targets))
        for target in self.targets:
            try:
                self.sock.sendto(packet, (target, PORT))
            except OSError:
                continue

    def receive(self, timeout: float) -> tuple[bytes, str] | None:
        self.sock.settimeout(max(0.01, timeout))
        try:
            packet, peer = self.sock.recvfrom(65535)
        except socket.timeout:
            return None
        if self.dump:
            self.dump("<<", packet, peer[0])
        return packet, peer[0]


def transact(transport, types: str, fields: Iterable[tuple[str, str]], *, mac: str | None,
             timeout: float = 4.0, retries: int = 3) -> dict[str, str]:
    """Send one request, retransmit a few times, return the first matching reply."""
    uid, packet = build_probe(types, list(fields), mac=mac)
    interval = timeout / max(1, retries)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        transport.send(packet)
        resend_at = min(deadline, time.monotonic() + interval)
        while (left := resend_at - time.monotonic()) > 0:
            got = transport.receive(left)
            if not got:
                break
            reply = parse_packet(got[0])
            if reply and is_reply_to(reply, uid, types, mac):
                reply["_sender"] = got[1]
                return reply
    raise SadpTimeout(tr("The device {mac} did not answer the SADP command “{command}” within {seconds} s.").format(
        mac=mac or "", command=types, seconds=f"{timeout:.0f}").replace("  ", " "))


# --------------------------------------------------------------------------- commands

def exchange_key(transport, mac: str, *, timeout: float = 4.0) -> bytes:
    keys = KeyExchange.generate()
    reply = transact(transport, "exchangecode", [("Code", keys.public_code())], mac=mac, timeout=timeout)
    code = reply.get("code")
    if not code:
        ok, reason = reply_result(reply)
        raise SadpError(tr("The device did not send an exchange key ({reason}).").format(reason=reason))
    return keys.decrypt_code(code)


def activate(transport, mac: str, password: str, *, timeout: float = 4.0) -> dict[str, str]:
    """exchangecode -> activate with AES-encrypted password (flow observed in a capture)."""
    check_new_password(password)
    key = exchange_key(transport, mac, timeout=timeout)
    reply = transact(transport, "activate", [("Password", encrypt_password(password, key))],
                     mac=mac, timeout=timeout)
    ok, reason = reply_result(reply)
    if not ok:
        raise SadpError(tr("SADP activation rejected: {reason}").format(reason=reason))
    return reply


@dataclass
class NetworkParams:
    ipv4: str = ""
    mask: str = ""
    gateway: str = ""
    dhcp: bool = False
    command_port: int = 8000
    http_port: int = 80
    ipv6: str = "::"
    ipv6_gateway: str = "::"
    ipv6_prefix: int = 64

    def validate(self) -> None:
        if not 1 <= self.command_port <= 65535 or not 1 <= self.http_port <= 65535:
            raise SadpError(tr("The port must be in the range 1–65535."))
        if not 0 <= self.ipv6_prefix <= 128:
            raise SadpError(tr("The IPv6 prefix length must be 0 to 128."))
        for label, value in ((tr("IPv6 address"), self.ipv6), (tr("IPv6 gateway"), self.ipv6_gateway)):
            if value and value != "::":
                try:
                    ipaddress.IPv6Address(value)
                except ValueError as exc:
                    raise SadpError(tr("Invalid {field}: {error}").format(field=label, error=exc)) from exc
        if self.dhcp:
            return
        try:
            ip = ipaddress.IPv4Address(self.ipv4)
            net = ipaddress.IPv4Network(f"{ip}/{self.mask}", strict=False)
        except ValueError as exc:
            raise SadpError(tr("Check the IPv4 address and mask: {error}").format(error=exc)) from exc
        if net.prefixlen < 31 and ip in (net.network_address, net.broadcast_address):
            raise SadpError(tr("The network or broadcast address cannot be assigned."))
        if self.gateway:
            try:
                gw = ipaddress.IPv4Address(self.gateway)
            except ValueError as exc:
                raise SadpError(tr("Invalid gateway: {error}").format(error=exc)) from exc
            if gw not in net:
                raise SadpError(tr("The gateway must be in the same subnet as the IPv4 address."))


def update_fields(params: NetworkParams, password_value: str) -> list[tuple[str, str]]:
    """Fields of the ``update`` template, in the order recovered from Sadp.dll.

    GUESS: with DHCP on, the tool still sends the address fields; we send the
    current/zero values ("0.0.0.0") because the template has no optional parts.
    """
    ipv4 = params.ipv4 if not params.dhcp else (params.ipv4 or "0.0.0.0")
    mask = params.mask if not params.dhcp else (params.mask or "0.0.0.0")
    gateway = params.gateway or "0.0.0.0"
    return [
        ("Password", password_value),
        ("IPv4Address", ipv4),
        ("CommandPort", str(params.command_port)),
        ("HttpPort", str(params.http_port)),
        ("IPv4SubnetMask", mask),
        ("IPv4Gateway", gateway),
        ("IPv6Address", params.ipv6 or "::"),
        ("IPv6Gateway", params.ipv6_gateway or "::"),
        ("IPv6MaskLen", str(params.ipv6_prefix)),
        ("DHCP", "true" if params.dhcp else "false"),
    ]


def wants_encryption(probe_fields: dict[str, str] | None) -> bool:
    """GUESS: newer firmware advertises <Salt>/<Encrypt> in ProbeMatch and expects
    an exchangecode-encrypted password; older firmware takes the plain password."""
    if not probe_fields:
        return True
    if probe_fields.get("salt"):
        return True
    return probe_fields.get("encrypt", "").strip().lower() in ("true", "1")


def modify_network(transport, mac: str, password: str, params: NetworkParams, *,
                   password_mode: str = "auto", probe_fields: dict[str, str] | None = None,
                   timeout: float = 4.0) -> dict[str, str]:
    """Send the ``update`` command.

    No automatic retry with another password encoding: repeated wrong attempts
    can lock the admin account, so the mode is chosen once.
    """
    if password_mode not in PASSWORD_MODES:
        raise SadpError(tr("Unknown password mode: {mode}").format(mode=password_mode))
    if not password:
        raise SadpError(tr("Enter the administrator password."))
    params.validate()
    mode = password_mode
    if mode == "auto":
        mode = "aes" if wants_encryption(probe_fields) else "plain"
    if mode == "aes":
        value = encrypt_password(password, exchange_key(transport, mac, timeout=timeout))
    else:
        value = password
    reply = transact(transport, "update", update_fields(params, value), mac=mac, timeout=timeout)
    ok, reason = reply_result(reply)
    if not ok:
        raise SadpError(tr("The device rejected the SADP network change: {reason}").format(reason=reason))
    return reply


# --------------------------------------------------------------------------- CLI

def _dump(direction: str, packet: bytes, peer: str) -> None:
    print(f"{direction} {peer}\n{packet.decode('utf-8', errors='replace')}\n", file=sys.stderr)


def _interface_for(address: str | None) -> tuple[str, str]:
    if address:
        if "/" in address:
            iface = ipaddress.IPv4Interface(address)
            return str(iface.ip), str(iface.netmask)
        return address, "255.255.255.0"
    raise SadpError("Укажите --local <IP интерфейса>[/маска], например --local 192.168.1.10/24")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Нативные SADP-команды по UDP 37020 (экспериментально).")
    parser.add_argument("--local", required=True, help="IPv4 локального интерфейса, можно с маской: 192.168.1.10/24")
    parser.add_argument("--dump", action="store_true", help="печатать сырые XML-пакеты в stderr")
    parser.add_argument("--timeout", type=float, default=4.0)
    sub = parser.add_subparsers(dest="command", required=True)

    act = sub.add_parser("activate", help="активировать устройство")
    act.add_argument("--mac", required=True)
    act.add_argument("--password", required=True)

    net = sub.add_parser("network", help="изменить сетевые параметры")
    net.add_argument("--mac", required=True)
    net.add_argument("--password", required=True)
    net.add_argument("--ip", default="")
    net.add_argument("--mask", default="")
    net.add_argument("--gateway", default="")
    net.add_argument("--dhcp", action="store_true")
    net.add_argument("--port", type=int, default=8000, help="порт SDK (CommandPort)")
    net.add_argument("--http-port", type=int, default=80)
    net.add_argument("--password-mode", choices=PASSWORD_MODES, default="auto")

    raw = sub.add_parser("raw", help="отправить произвольный Probe и показать ответ")
    raw.add_argument("--mac")
    raw.add_argument("types")
    raw.add_argument("fields", nargs="*", help="Имя=значение")

    args = parser.parse_args(argv)
    address, netmask = _interface_for(args.local)
    try:
        with SadpTransport(address, netmask, dump=_dump if args.dump else None) as transport:
            if args.command == "activate":
                reply = activate(transport, args.mac, args.password, timeout=args.timeout)
            elif args.command == "network":
                params = NetworkParams(ipv4=args.ip, mask=args.mask, gateway=args.gateway, dhcp=args.dhcp,
                                       command_port=args.port, http_port=args.http_port)
                reply = modify_network(transport, args.mac, args.password, params,
                                       password_mode=args.password_mode, timeout=args.timeout)
            else:
                fields = [tuple(item.split("=", 1)) for item in args.fields]
                reply = transact(transport, args.types, fields, mac=args.mac, timeout=args.timeout)
    except (SadpError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    for key, value in reply.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
