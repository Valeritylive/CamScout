"""Dahua device discovery shared by the GTK app and the CLI (no GTK imports).

ConfigTool finds devices in two ways, both implemented here:

* DHIP ``DHDiscover.search`` on UDP 37810. The JSON body sits behind a 32-byte
  DHIP header (``<I 32``, ``b"DHIP"``, session/id, ``<QQ`` length twice). The probe
  goes to multicast 239.255.255.251, to the local broadcast and, for the
  "other segment" search, to every address of an IPv4 range (unicast).
* The legacy binary DVRIP probe on UDP 5050 (broadcast), answered by older DVR/NVR
  firmwares that do not speak DHIP.

Bare JSON without the DHIP header gets no reply from current firmwares, so the
frame below mirrors the one the devices send back.
"""

from __future__ import annotations

from i18n import tr
import csv
import ipaddress
import json
import os
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, TextIO

DHIP_PORT = 37810
DHIP_GROUP = "239.255.255.251"
DVRIP_PORT = 5050
DHIP_MAGIC = b"DHIP"
HEADER_SIZE = 32

# Discovery and multicast RPC (dahua_protocol) both read UDP 37810 replies; in one
# process they must not run at the same time or replies land on the wrong socket.
DISCOVERY_LOCK = threading.Lock()

_request_id = 0
_request_lock = threading.Lock()

# Packet trace for debugging on real hardware: when set, every DHIP/DVRIP packet
# sent or received is passed here as (direction "→"/"←", peer IP, raw bytes).
packet_trace: "Callable[[str, str, bytes], None] | None" = None

_SECRET_KEYS = ("password", "passwd", "pwd", "pwdold", "newpwd", "oldpwd")


def _mask_secrets(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: _mask_secrets(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_secrets(v, key) for v in value]
    if isinstance(value, str) and value and key.lower() in _SECRET_KEYS:
        return "***"
    return value


def trace(direction: str, peer: str, data: bytes) -> None:
    hook = packet_trace
    if hook is None:
        return
    try:
        hook(direction, peer, data)
    except Exception:  # a broken trace must never break the operation
        pass


def format_packet(direction: str, peer: str, data: bytes) -> str:
    """Readable trace entry; plain-text passwords are replaced by ***."""
    message = parse_frame(data)
    stamp = time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
    if message is not None:
        text = json.dumps(_mask_secrets(message), ensure_ascii=False, indent=2)
        header = data[:HEADER_SIZE].hex() if data[4:8] == DHIP_MAGIC else "no DHIP header"
        return f"--- {stamp} {direction} {peer} ({len(data)} bytes, header {header})\n{text}\n"
    return f"--- {stamp} {direction} {peer} ({len(data)} bytes, binary)\n{data[:512].hex()}\n"


def next_request_id() -> int:
    global _request_id
    with _request_lock:
        _request_id = (_request_id + 1) & 0xFFFFFFFF
        return _request_id


def build_frame(payload: dict[str, Any], session: int = 0, request_id: int | None = None) -> bytes:
    """Frame a JSON RPC body with the 32-byte DHIP header."""
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    rid = next_request_id() if request_id is None else request_id
    header = (struct.pack("<I", HEADER_SIZE) + DHIP_MAGIC
              + struct.pack("<II", session, rid) + struct.pack("<QQ", len(body), len(body)))
    return header + body


def parse_frame(data: bytes) -> dict[str, Any] | None:
    """Return the JSON body of a DHIP datagram (header length is not trusted)."""
    if not data:
        return None
    start = data.find(b"{")
    if start < 0:
        return None
    text = data[start:].decode("utf-8", errors="replace").rstrip("\x00 \r\n")
    try:
        message = json.loads(text)
    except ValueError:
        # Some firmwares pad with garbage after the JSON; cut at the last brace.
        end = text.rfind("}")
        if end < 0:
            return None
        try:
            message = json.loads(text[:end + 1])
        except ValueError:
            return None
    return message if isinstance(message, dict) else None


SEARCH_PROBE = {"method": "DHDiscover.search", "params": {"mac": "", "uni": 1}}

# Legacy DVRIP "discover" request (first byte 0xa3), answered with 0xb3 frames.
DVRIP_PROBE = (struct.pack(">I", 0xA3010001) + bytes(12)
               + struct.pack(">I", 0x02000000) + bytes(12))


# Init field of the discovery reply (NetSDK byInitStatus, bits 0..1).
# GUESS: 0 = old firmware without initialization (already has a password),
# 1 = not initialized, 2 = initialized. Verified against the NetSDK headers, not
# against every firmware.
INIT_OLD, INIT_NO, INIT_YES = 0, 1, 2

# PwdResetWay bits (NetSDK byPwdResetWay): bit0 phone, bit1 e-mail, bit2 XML file,
# bit3 security questions. GUESS: bits 2/3 from newer SDK documentation.
PWD_RESET_WAYS = ((0x1, "phone"), (0x2, "e-mail"), (0x4, "XML file"), (0x8, "security questions"))


@dataclass
class Device:
    ip: str = ""
    mac: str = ""
    model: str = ""            # DeviceType, e.g. IPC-HFW2431S-S-S2
    device_class: str = ""     # IPC, NVR, DVR, VTO, ...
    serial: str = ""
    firmware: str = ""
    vendor: str = ""
    machine_name: str = ""
    subnet_mask: str = ""
    gateway: str = ""
    dhcp: bool | None = None
    ipv6: str = ""
    http_port: int = 80
    https_port: int = 0
    tcp_port: int = 37777
    udp_port: int = 0
    init_status: int | None = None
    pwd_reset_way: int = 0
    video_inputs: int = 0
    remote_inputs: int = 0
    alarm_inputs: int = 0
    alarm_outputs: int = 0
    protocol: str = "DHIP"     # DHIP, DVRIP, HTTP (added by hand), CSV; SADP for Hikvision
    raw: dict[str, Any] | None = None
    # CamScout shows Dahua and Hikvision devices in one list.
    brand: str = "Dahua"
    rtsp_port: int = 0
    ipv6_gateway: str = ""
    enhanced_sdk_port: int = 0
    # The original sadp_discovery.Device of a Hikvision device (SADP commands use it).
    sadp: Any = field(default=None, repr=False, compare=False)
    # Local (name, IPv4, netmask) the device answered on; multicast RPC goes out there.
    interface: tuple[str, str, str] | None = field(default=None, repr=False, compare=False)

    @property
    def initialized(self) -> bool | None:
        if self.init_status is None:
            return None
        return (self.init_status & 0x3) != INIT_NO

    @property
    def key(self) -> str:
        return (self.mac or self.serial or self.ip).lower()

    def reset_ways_text(self) -> str:
        return ", ".join(tr(name) for bit, name in PWD_RESET_WAYS if self.pwd_reset_way & bit)


def local_interfaces() -> list[tuple[str, str, str]]:
    """Return (interface, IPv4, netmask) for active non-loopback Linux links."""
    result: list[tuple[str, str, str]] = []
    try:
        names = sorted(os.listdir("/sys/class/net"))
    except OSError:
        return result
    ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for name in names:
            if name == "lo":
                continue
            try:
                with open(f"/sys/class/net/{name}/operstate", encoding="ascii") as handle:
                    if handle.read().strip() not in ("up", "unknown"):
                        continue
                req = struct.pack("256s", name.encode()[:15])
                addr = socket.inet_ntoa(_ioctl(ctl, 0x8915, req)[20:24])
                mask = socket.inet_ntoa(_ioctl(ctl, 0x891B, req)[20:24])
                result.append((name, addr, mask))
            except OSError:
                continue
    finally:
        ctl.close()
    return result


def _ioctl(sock: socket.socket, request: int, data: bytes) -> bytes:
    import fcntl
    return fcntl.ioctl(sock.fileno(), request, data)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return None
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def normalize_mac(mac: str) -> str:
    digits = "".join(ch for ch in mac.lower() if ch in "0123456789abcdef")
    if len(digits) != 12:
        return mac.strip().lower()
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


def device_from_info(info: dict[str, Any], sender_ip: str = "", mac: str = "") -> Device | None:
    """Build a Device from params.deviceInfo of a DHDiscover reply."""
    if not isinstance(info, dict):
        return None
    ipv4 = info.get("IPv4Address") if isinstance(info.get("IPv4Address"), dict) else {}
    ipv6 = info.get("IPv6Address") if isinstance(info.get("IPv6Address"), dict) else {}
    mac = normalize_mac(str(info.get("mac") or info.get("MAC") or mac or ""))
    ipv6_addr = str(ipv6.get("IPAddress") or "")
    if "/" in ipv6_addr:
        ipv6_addr = ipv6_addr.split("/", 1)[0]
    device = Device(
        ip=str(ipv4.get("IPAddress") or info.get("IPAddress") or sender_ip),
        mac=mac,
        model=str(info.get("DeviceType") or info.get("Model") or ""),
        device_class=str(info.get("DeviceClass") or ""),
        serial=str(info.get("SerialNo") or info.get("SerialNumber") or ""),
        firmware=str(info.get("Version") or info.get("SoftwareVersion") or ""),
        vendor=str(info.get("Vendor") or info.get("Manufacturer") or ""),
        machine_name=str(info.get("MachineName") or ""),
        subnet_mask=str(ipv4.get("SubnetMask") or ""),
        gateway=str(ipv4.get("DefaultGateway") or ""),
        dhcp=_bool(ipv4.get("DhcpEnable")),
        ipv6="" if ipv6_addr in ("", "::") else ipv6_addr,
        http_port=_int(info.get("HttpPort"), 80) or 80,
        https_port=_int(info.get("HttpsPort")),
        tcp_port=_int(info.get("Port"), 37777) or 37777,
        udp_port=_int(info.get("UdpPort")),
        init_status=_int(info["Init"]) if "Init" in info else None,
        pwd_reset_way=_int(info.get("PwdResetWay")),
        video_inputs=_int(info.get("VideoInputChannels")),
        remote_inputs=_int(info.get("RemoteVideoInputChannels")),
        alarm_inputs=_int(info.get("AlarmInputChannels")),
        alarm_outputs=_int(info.get("AlarmOutputChannels")),
        protocol="DHIP",
        raw=_flatten(info),
    )
    if not any((device.mac, device.serial, device.model)):
        return None
    return device


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            out.update(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        out[prefix] = ", ".join(str(item) for item in value)
    else:
        out[prefix] = value
    return out


def parse_dhip_reply(data: bytes, sender_ip: str) -> Device | None:
    message = parse_frame(data)
    if not message:
        return None
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    info = params.get("deviceInfo")
    if not isinstance(info, dict):
        return None
    return device_from_info(info, sender_ip, str(message.get("mac") or params.get("mac") or ""))


def _ip(data: bytes) -> str:
    return socket.inet_ntoa(data) if len(data) == 4 else ""


def parse_dvrip_reply(data: bytes, sender_ip: str) -> Device | None:
    """Parse the legacy binary 0xb3 discovery answer from UDP 5050.

    Offsets follow the public DahuaConsole decoder (body after the 32-byte header):
    version u16x4, hostname[16], IP, mask, gateway, DNS, ..., web port @62,
    HTTPS @64, TCP @66, UDP @72, then ASCII ``key:value`` lines from offset 88.
    GUESS: the ASCII tail layout differs between firmwares; unknown keys go to raw.
    """
    if len(data) < 32 + 88 or data[0] != 0xB3:
        return None
    body = data[32:]
    u16 = lambda offset: struct.unpack_from("<H", body, offset)[0]  # noqa: E731
    version = ".".join(str(u16(i)) for i in (0, 2, 4, 6))
    hostname = body[8:24].split(b"\x00", 1)[0].decode("latin-1", errors="replace")
    ascii_tail = body[88:].split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    extra: dict[str, str] = {}
    for line in ascii_tail.replace("\r", "\n").split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            extra[key.strip()] = value.strip()
        elif "=" in line:
            key, value = line.split("=", 1)
            extra[key.strip()] = value.strip()
    ip = _ip(body[24:28]) or sender_ip
    device = Device(
        ip=ip if ip != "0.0.0.0" else sender_ip,
        mac=normalize_mac(extra.get("MAC") or extra.get("mac") or ""),
        model=extra.get("DeviceType") or extra.get("Type") or "",
        device_class=extra.get("DeviceClass") or "DVR",
        serial=extra.get("SerialNo") or extra.get("SN") or "",
        firmware=extra.get("Version") or version,
        machine_name=hostname,
        subnet_mask=_ip(body[28:32]),
        gateway=_ip(body[32:36]),
        http_port=u16(62) or 80,
        https_port=u16(64),
        tcp_port=u16(66) or 37777,
        udp_port=u16(72),
        protocol="DVRIP",
        raw={"hostname": hostname, "dns": _ip(body[36:40]), **extra},
    )
    if not (device.mac or device.serial or device.ip):
        return None
    return device


def _open_listener(address: str) -> socket.socket | None:
    """Socket on UDP 37810 joined to the DHIP group, for replies sent by multicast."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind(("", DHIP_PORT))
        try:  # only groups this socket joined on *address* (Linux IP_MULTICAST_ALL)
            sock.setsockopt(socket.IPPROTO_IP, getattr(socket, "IP_MULTICAST_ALL", 49), 0)
        except OSError:
            pass
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            socket.inet_aton(DHIP_GROUP) + socket.inet_aton(address))
        except OSError:
            pass
        sock.setblocking(False)
        return sock
    except OSError:
        sock.close()
        return None


def _open_sender(address: str, port: int = 0) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.bind((address, port))
    except OSError:
        sock.bind(("", port))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setblocking(False)
    return sock


def broadcast_addresses(address: str, netmask: str) -> list[str]:
    result = ["255.255.255.255"]
    try:
        network = ipaddress.IPv4Network(f"{address}/{netmask}", strict=False)
        if str(network.broadcast_address) not in result:
            result.append(str(network.broadcast_address))
    except ValueError:
        pass
    return result


def ip_range(start: str, end: str, limit: int = 4096) -> list[str]:
    first, last = ipaddress.IPv4Address(start), ipaddress.IPv4Address(end)
    if last < first:
        first, last = last, first
    count = int(last) - int(first) + 1
    if count > limit:
        raise ValueError(tr("Range too large: {count} addresses (maximum {limit})").format(count=count, limit=limit))
    return [str(ipaddress.IPv4Address(int(first) + i)) for i in range(count)]


def discover(interface: tuple[str, str, str], timeout: float = 3.0,
             unicast_targets: Iterable[str] = (), legacy: bool = True) -> list[Device]:
    """Search the segment of *interface* (and optionally a list of unicast IPs)."""
    _name, address, netmask = interface
    probe = build_frame(SEARCH_PROBE, request_id=0)
    sender = _open_sender(address)
    listener = _open_listener(address)
    legacy_sock: socket.socket | None = None
    if legacy:
        try:
            legacy_sock = _open_sender(address)
        except OSError:
            legacy_sock = None
    sockets = [s for s in (sender, listener, legacy_sock) if s is not None]
    devices: dict[str, Device] = {}
    targets = list(unicast_targets)
    broadcasts = broadcast_addresses(address, netmask)
    trace("→", f"{DHIP_GROUP}, broadcast ({address})", probe)
    try:
        until = time.monotonic() + timeout
        next_burst = 0.0
        unicast_index = 0
        while time.monotonic() < until:
            now = time.monotonic()
            if now >= next_burst:
                for target in (DHIP_GROUP, *broadcasts):
                    try:
                        sender.sendto(probe, (target, DHIP_PORT))
                    except OSError:
                        pass
                if legacy_sock is not None:
                    for target in broadcasts:
                        try:
                            legacy_sock.sendto(DVRIP_PROBE, (target, DVRIP_PORT))
                        except OSError:
                            pass
                next_burst = now + 1.0
            # Spread unicast probes so a /24 does not flood the NIC in one go; the
            # range is gone through twice (UDP may drop the first probe).
            passes = 2 if time.monotonic() - (until - timeout) >= 1.0 else 1
            batch_end = min(len(targets) * passes, unicast_index + 64)
            for position in range(unicast_index, batch_end):
                target = targets[position % len(targets)]
                try:
                    sender.sendto(probe, (target, DHIP_PORT))
                    if len(targets) <= 16:
                        trace("→", target, probe)
                except OSError as exc:
                    if position < len(targets) and len(targets) <= 16:
                        trace("→", f"{target} (send error: {exc})", probe)
            if targets and unicast_index < len(targets) <= batch_end:
                if len(targets) > 16:
                    trace("→", f"unicast {targets[0]}…{targets[-1]} ({len(targets)} addresses)", probe)
            unicast_index = batch_end
            readable, _w, _x = select.select(sockets, [], [], 0.1)
            for sock in readable:
                while True:
                    try:
                        packet, peer = sock.recvfrom(65535)
                    except (BlockingIOError, InterruptedError):
                        break
                    except OSError:
                        break
                    if packet == probe:
                        continue  # our own search looped back by multicast/broadcast
                    trace("←", peer[0], packet)
                    if sock is legacy_sock:
                        device = parse_dvrip_reply(packet, peer[0])
                    else:
                        device = parse_dhip_reply(packet, peer[0])
                    if device is None:
                        continue
                    device.interface = interface
                    existing = devices.get(device.key)
                    # A DHIP answer carries more than the legacy one; keep the richer.
                    if existing is None or existing.protocol != "DHIP":
                        devices[device.key] = device
        return sorted(devices.values(), key=lambda d: (ip_sort_key(d.ip), d.mac))
    finally:
        for sock in sockets:
            sock.close()


def probe_ip(ip: str, timeout: float = 2.0, interface: tuple[str, str, str] | None = None) -> Device | None:
    """Ask one address to identify itself over unicast DHIP."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        frame = build_frame(SEARCH_PROBE, request_id=0)
        sock.sendto(frame, (ip, DHIP_PORT))
        trace("→", ip, frame)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                packet, peer = sock.recvfrom(65535)
            except socket.timeout:
                return None
            trace("←", peer[0], packet)
            device = parse_dhip_reply(packet, peer[0])
            if device is not None:
                device.interface = interface
                return device
        return None
    except OSError:
        return None
    finally:
        sock.close()


def discover_all(interfaces: Iterable[tuple[str, str, str]], timeout: float = 2.5,
                 unicast_targets: Iterable[str] = (), legacy: bool = True) -> list[Device]:
    """Search every interface in turn; a device seen on several keeps the one in its subnet."""
    devices: dict[str, Device] = {}
    targets = list(unicast_targets)
    for interface in interfaces:
        try:
            found = discover(interface, timeout, targets, legacy)
        except OSError:
            continue
        targets = []  # unicast probes are routed anyway; send them once
        for device in found:
            existing = devices.get(device.key)
            if existing is None or (not same_subnet(existing.ip, existing.interface)
                                    and same_subnet(device.ip, device.interface)):
                devices[device.key] = device
    return sorted(devices.values(), key=lambda d: (ip_sort_key(d.ip), d.mac))


def wait_for_device(mac: str, interfaces: list[tuple[str, str, str]], expected_ip: str = "",
                    timeout: float = 90.0, should_stop: Callable[[], bool] | None = None) -> Device | None:
    """Find the device with *mac* again (e.g. after an IP change or a reboot).

    Asks *expected_ip* directly (works through routers) and searches the local
    segments by multicast (works for any subnet on the same wire). Returns the
    device once it answers from a new address — or from *expected_ip* — or None.
    """
    mac = normalize_mac(mac)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if should_stop and should_stop():
            return None
        if expected_ip:
            device = probe_ip(expected_ip, 1.5, interface_for(expected_ip, interfaces))
            if device is not None and (not mac or normalize_mac(device.mac) == mac):
                return device
        with DISCOVERY_LOCK:
            found = discover_all(interfaces, 2.0, legacy=False)
        for device in found:
            if normalize_mac(device.mac) == mac and (not expected_ip or device.ip == expected_ip):
                return device
    return None


def interface_for(ip: str, interfaces: Iterable[tuple[str, str, str]]) -> tuple[str, str, str] | None:
    """The local interface whose subnet holds *ip*, else the first one."""
    interfaces = list(interfaces)
    for interface in interfaces:
        if same_subnet(ip, interface):
            return interface
    return interfaces[0] if interfaces else None


def ip_sort_key(text: str) -> tuple:
    parts = text.split(".")
    if len(parts) == 4 and all(part.isdigit() for part in parts):
        return (0, *(int(part) for part in parts))
    return (1, text)


def same_subnet(device_ip: str, interface: tuple[str, str, str] | None) -> bool:
    if not interface or not device_ip:
        return False
    try:
        network = ipaddress.IPv4Network(f"{interface[1]}/{interface[2]}", strict=False)
        return ipaddress.IPv4Address(device_ip) in network
    except ValueError:
        return False


def status_text(device: Device) -> str:
    initialized = device.initialized
    if initialized is True:
        return tr("Initialized")
    if initialized is False:
        return tr("Uninitialized")
    return "—"


CSV_COLUMNS = ["№", "Статус", "Тип", "Модель", "IP", "Маска", "Шлюз", "DHCP", "MAC",
               "Версия", "Серийный номер", "Имя устройства", "Производитель",
               "TCP порт", "HTTP порт", "HTTPS порт", "IPv6", "Видеовходы", "Протокол"]


def write_devices_csv(devices: Iterable[Device], output: TextIO) -> int:
    """Write devices like ConfigTool's export; returns the number of rows."""
    devices = list(devices)
    writer = csv.writer(output)
    writer.writerow(CSV_COLUMNS)
    for number, device in enumerate(devices, 1):
        writer.writerow([number, status_text(device), device.device_class, device.model, device.ip,
                         device.subnet_mask, device.gateway,
                         "" if device.dhcp is None else ("Да" if device.dhcp else "Нет"),
                         device.mac, device.firmware, device.serial, device.machine_name,
                         device.vendor, device.tcp_port, device.http_port, device.https_port or "",
                         device.ipv6, device.video_inputs or "", device.protocol])
    return len(devices)


def read_devices_csv(source: TextIO) -> list[Device]:
    """Import a list exported by this program (or a CSV with at least an IP column)."""
    reader = csv.DictReader(source)
    devices: list[Device] = []
    for row in reader:
        row = {(key or "").strip(): (value or "").strip() for key, value in row.items()}
        ip = row.get("IP") or row.get("IP адрес") or row.get("ip") or row.get("IP Address") or ""
        if not ip:
            continue
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        devices.append(Device(
            ip=ip, mac=normalize_mac(row.get("MAC", "")), model=row.get("Модель") or row.get("Model", ""),
            device_class=row.get("Тип") or row.get("Type", ""), serial=row.get("Серийный номер") or row.get("SN", ""),
            firmware=row.get("Версия") or row.get("Version", ""), subnet_mask=row.get("Маска", ""),
            gateway=row.get("Шлюз", ""), machine_name=row.get("Имя устройства", ""),
            vendor=row.get("Производитель", ""), tcp_port=_int(row.get("TCP порт"), 37777) or 37777,
            http_port=_int(row.get("HTTP порт") or row.get("Port"), 80) or 80, protocol="CSV",
        ))
    return devices
