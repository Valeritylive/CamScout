"""SADP discovery over UDP 37020, shared by the GTK app and the CLI (no GTK imports)."""

from __future__ import annotations

import csv
import ipaddress
import os
import secrets
import socket
import struct
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Iterable, TextIO

PORT = 37020
GROUP = "239.255.255.250"
# Discovery and native SADP commands (sadp_protocol) share UDP 37020; replies may land on
# either socket, so in one process they must not run at the same time.
SADP_SOCKET_LOCK = threading.Lock()


@dataclass
class Device:
    ip: str = ""
    mac: str = ""
    model: str = ""
    serial: str = ""
    firmware: str = ""
    status: str = ""
    http_port: int = 80
    rtsp_port: int = 554
    device_port: int = 8000
    gateway: str = ""
    subnet_mask: str = ""
    ipv6: str = ""
    ipv6_gateway: str = ""
    enhanced_sdk_port: int = 0
    activated: bool | None = None
    raw: dict[str, str] | None = None
    # Local (name, IPv4, netmask) the device answered on; native SADP commands go out there.
    interface: tuple[str, str, str] | None = field(default=None, repr=False, compare=False)


def local_interfaces() -> list[tuple[str, str, str]]:
    """Return (interface, IPv4, netmask) for active non-loopback Linux links."""
    result: list[tuple[str, str, str]] = []
    try:
        names = os.listdir("/sys/class/net")
    except OSError:
        return result
    ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for name in names:
            if name == "lo" or not os.path.exists(f"/sys/class/net/{name}/operstate"):
                continue
            if open(f"/sys/class/net/{name}/operstate", encoding="ascii").read().strip() not in ("up", "unknown"):
                continue
            try:
                req = struct.pack("256s", name.encode()[:15])
                addr = socket.inet_ntoa(fcntl_ioctl(ctl, 0x8915, req)[20:24])
                mask = socket.inet_ntoa(fcntl_ioctl(ctl, 0x891B, req)[20:24])
                result.append((name, addr, mask))
            except OSError:
                continue
    finally:
        ctl.close()
    return result


def fcntl_ioctl(sock: socket.socket, request: int, data: bytes) -> bytes:
    import fcntl
    return fcntl.ioctl(sock.fileno(), request, data)


def parse_response(data: bytes, sender_ip: str) -> Device | None:
    try:
        root = ET.fromstring(data.decode("utf-8", errors="replace"))
    except ET.ParseError:
        return None
    fields = {node.tag.rsplit("}", 1)[-1].lower(): (node.text or "").strip()
              for node in root.iter() if node is not root}
    # SADP replies have appeared as ProbeMatch and as flat DeviceInfo XML.
    ip = fields.get("ipv4address") or fields.get("ipaddress") or sender_ip
    mac = fields.get("mac") or fields.get("macaddress") or ""
    # Real ProbeMatch replies carry DeviceDescription (model), DeviceSN (serial) and
    # CommandPort (SDK port); DeviceType is only a numeric class code, so it comes last.
    model = (fields.get("devicedescription") or fields.get("deviceproductionname") or fields.get("model")
             or fields.get("devicetype") or "")
    serial = (fields.get("devicesn") or fields.get("serialno") or fields.get("serialnumber")
              or fields.get("deviceid") or "")
    firmware = fields.get("softwareversion") or fields.get("firmwareversion") or ""
    status = fields.get("activestatus") or fields.get("activationstatus") or ""
    activation_raw = fields.get("activated") or fields.get("activestatus") or fields.get("activationstatus")
    activated = None
    if activation_raw:
        value = activation_raw.strip().lower()
        if value in ("true", "1", "activated", "active"):
            activated = True
        elif value in ("false", "0", "inactive", "not activated", "notactivated"):
            activated = False
    try:
        http_port = int(fields.get("httpport") or fields.get("httpserviceport") or "80")
    except ValueError:
        http_port = 80
    try:
        rtsp_port = int(fields.get("rtspport") or fields.get("rtspserviceport") or "554")
    except ValueError:
        rtsp_port = 554
    try:
        device_port = int(fields.get("commandport") or fields.get("port") or fields.get("sdkport") or "8000")
    except ValueError:
        device_port = 8000
    gateway = fields.get("ipv4gateway") or fields.get("gateway") or ""
    subnet_mask = fields.get("ipv4subnetmask") or fields.get("subnetmask") or ""
    ipv6 = fields.get("ipv6address") or fields.get("ipv6addr") or ""
    ipv6_gateway = fields.get("ipv6gateway") or ""
    try:
        enhanced_sdk_port = int(fields.get("enhancedsdkport") or fields.get("sdkovertlsport")
                                or fields.get("sdkhttpsport") or "0")
    except ValueError:
        enhanced_sdk_port = 0
    if not any((mac, model, serial)):
        return None
    return Device(ip=ip, mac=mac, model=model, serial=serial, firmware=firmware,
                  status=status, http_port=http_port, rtsp_port=rtsp_port, device_port=device_port,
                  gateway=gateway, subnet_mask=subnet_mask, ipv6=ipv6,
                  ipv6_gateway=ipv6_gateway, enhanced_sdk_port=enhanced_sdk_port,
                  activated=activated, raw=fields)


def discover(interface: tuple[str, str, str], timeout: float = 3.0) -> list[Device]:
    name, address, netmask = interface
    payload = (f'<?xml version="1.0" encoding="utf-8"?>'
               f'<Probe><Uuid>{secrets.token_hex(16)}</Uuid><Types>inquiry</Types></Probe>').encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.25)
    devices: dict[str, Device] = {}
    try:
        sock.bind(("", PORT))
        # Scope multicast to the selected NIC; many devices also listen to local broadcast.
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        group_req = socket.inet_aton(GROUP) + socket.inet_aton(address)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, group_req)
        except OSError:
            pass
        broadcasts = {"255.255.255.255"}
        try:
            network = ipaddress.IPv4Network(f"{address}/{netmask}", strict=False)
            broadcasts.add(str(network.broadcast_address))
        except ValueError:
            pass
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            for target in (GROUP, *broadcasts):
                try:
                    sock.sendto(payload, (target, PORT))
                except OSError:
                    continue
            burst_end = min(until, time.monotonic() + 0.6)
            while time.monotonic() < burst_end:
                try:
                    packet, peer = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                device = parse_response(packet, peer[0])
                if device:
                    device.interface = interface
                    devices[device.mac or device.serial or device.ip] = device
        return sorted(devices.values(), key=lambda d: (d.ip, d.mac))
    finally:
        sock.close()

CSV_COLUMNS = ["ID", "Модель", "Статус", "IPv4", "Маска IPv4", "Шлюз IPv4",
               "IPv6", "Шлюз IPv6", "Порт устройства", "Порт Enhanced SDK",
               "HTTP порт", "RTSP порт", "Прошивка", "Серийный номер", "MAC"]


def write_devices_csv(devices: Iterable[Device], output: TextIO) -> int:
    """Write devices in the GUI export format; returns the number of rows written."""
    devices = list(devices)
    extra_columns = sorted({name for device in devices for name in (device.raw or {})})
    writer = csv.writer(output)
    writer.writerow([*CSV_COLUMNS, *extra_columns])
    for number, device in enumerate(devices, 1):
        raw = device.raw or {}
        writer.writerow([number, device.model, device.status, device.ip,
                         device.subnet_mask, device.gateway, device.ipv6,
                         device.ipv6_gateway, device.device_port,
                         device.enhanced_sdk_port or "", device.http_port,
                         device.rtsp_port, device.firmware, device.serial, device.mac,
                         *(raw.get(name, "") for name in extra_columns)])
    return len(devices)
