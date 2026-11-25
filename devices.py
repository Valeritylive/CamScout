"""One device model for both brands.

Dahua devices come from DHIP discovery as ``dahua_discovery.Device``. Hikvision
devices answer SADP; ``from_sadp`` turns them into the same ``Device`` with
``brand="Hikvision"`` and keeps the original SADP record in ``device.sadp`` for
the SADP and ISAPI commands. Kept free of GTK.
"""

from __future__ import annotations

import csv
import ipaddress
from typing import Any, Iterable, TextIO

from dahua_discovery import INIT_NO, INIT_YES, Device, normalize_mac
from i18n import tr
import sadp_discovery

DAHUA = "Dahua"
HIKVISION = "Hikvision"
BRANDS = (DAHUA, HIKVISION)


def _raw(device: Any, *names: str) -> str:
    raw = getattr(device, "raw", None) or {}
    for name in names:
        value = raw.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _int(text: str, default: int = 0) -> int:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return default


def _bool(text: str) -> bool | None:
    lowered = (text or "").strip().lower()
    if lowered in ("true", "1", "yes", "enable", "enabled", "on"):
        return True
    if lowered in ("false", "0", "no", "disable", "disabled", "off"):
        return False
    return None


def hikvision_model(device: Any) -> str:
    """SADP sends a numeric DeviceType code and the model in DeviceDescription."""
    description = _raw(device, "devicedescription")
    if description:
        return description
    model = getattr(device, "model", "") or ""
    return "" if model.isdigit() else model


def hikvision_class(model: str) -> str:
    """Device class (IPC, NVR, DVR, SD, VTO, VTH) guessed from a Hikvision model name."""
    name = (model or "").upper()
    for prefix in ("IDS-", "DS-"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    else:
        return ""
    if name.startswith(("2DE", "2DF", "2DY", "2SE", "2DC")):
        return "SD"
    if name.startswith(("2CD", "2XD", "2CE", "2TD", "2XS", "2CV", "2DP")):
        return "IPC"
    if name.startswith(("KV", "KD", "KB", "KIS")):
        return "VTO"
    if name.startswith("KH"):
        return "VTH"
    if name.startswith(("7", "8", "9")) or name.startswith("NVR"):
        if "NI" in name or "NXI" in name or "NVR" in name:
            return "NVR"
        return "DVR"
    if name.startswith("TCG") or name.startswith("2CD"):
        return "IPC"
    return ""


def from_sadp(found: sadp_discovery.Device) -> Device:
    """Hikvision SADP record -> the common Device row."""
    model = hikvision_model(found)
    if found.activated is True:
        init_status: int | None = INIT_YES
    elif found.activated is False:
        init_status = INIT_NO
    else:
        init_status = None
    analog = _int(_raw(found, "analogchannelnum", "channelnum"))
    digital = _int(_raw(found, "digitalchannelnum"))
    ipv6 = found.ipv6 if found.ipv6 not in ("", "::") else ""
    ipv6_gateway = found.ipv6_gateway if found.ipv6_gateway not in ("", "::") else ""
    return Device(
        ip=found.ip,
        mac=normalize_mac(found.mac) if found.mac else "",
        model=model,
        device_class=hikvision_class(model),
        serial=_raw(found, "devicesn", "serialno", "serialnumber") or found.serial,
        firmware=found.firmware or _raw(found, "softwareversion"),
        vendor=HIKVISION,
        subnet_mask=found.subnet_mask,
        gateway=found.gateway,
        dhcp=_bool(_raw(found, "dhcp")),
        ipv6=ipv6,
        http_port=found.http_port or 80,
        tcp_port=found.device_port or 8000,
        init_status=init_status,
        video_inputs=analog,
        remote_inputs=digital,
        protocol="SADP",
        raw=found.raw,
        brand=HIKVISION,
        rtsp_port=found.rtsp_port or 554,
        ipv6_gateway=ipv6_gateway,
        enhanced_sdk_port=found.enhanced_sdk_port,
        sadp=found,
        interface=found.interface,
    )


def sadp_record(device: Device) -> sadp_discovery.Device:
    """The SADP record for SADP/ISAPI commands; built from the row if it was imported."""
    if device.sadp is not None:
        return device.sadp
    record = sadp_discovery.Device(
        ip=device.ip, mac=device.mac.replace(":", "-"), model=device.model, serial=device.serial,
        firmware=device.firmware, http_port=device.http_port or 80, rtsp_port=device.rtsp_port or 554,
        device_port=device.tcp_port or 8000, gateway=device.gateway, subnet_mask=device.subnet_mask,
        ipv6=device.ipv6, ipv6_gateway=device.ipv6_gateway, enhanced_sdk_port=device.enhanced_sdk_port,
        activated=device.initialized, raw=dict(device.raw or {}), interface=device.interface)
    return record


def status_text(device: Device) -> str:
    """Dahua says initialized/uninitialized, Hikvision active/inactive."""
    initialized = device.initialized
    if device.brand == HIKVISION:
        if initialized is True:
            return tr("Active")
        if initialized is False:
            return tr("Inactive")
        return (device.sadp.status if device.sadp is not None else "") or "—"
    if initialized is True:
        return tr("Initialized")
    if initialized is False:
        return tr("Uninitialized")
    return "—"


def rtsp_path(device: Device, channel: int = 1, substream: bool = False) -> str:
    """Path of the live stream on the device's RTSP server."""
    channel = max(1, channel)
    if device.brand == HIKVISION:
        return f"/Streaming/Channels/{channel}0{2 if substream else 1}"
    return f"/cam/realmonitor?channel={channel}&subtype={1 if substream else 0}"


# ---------------------------------------------------------------- CSV import/export

CSV_FIELDS = ["No", "Brand", "Status", "Type", "Model", "IP", "Mask", "Gateway", "DHCP", "MAC",
              "Version", "Serial number", "Device name", "Vendor", "TCP port", "HTTP port", "HTTPS port",
              "RTSP port", "IPv6", "Channels", "Protocol"]


def write_devices_csv(devices: Iterable[Device], output: TextIO) -> int:
    devices = list(devices)
    writer = csv.writer(output)
    writer.writerow(CSV_FIELDS)
    for number, device in enumerate(devices, 1):
        writer.writerow([number, device.brand, status_text(device), device.device_class, device.model, device.ip,
                         device.subnet_mask, device.gateway,
                         "" if device.dhcp is None else ("yes" if device.dhcp else "no"),
                         device.mac, device.firmware, device.serial, device.machine_name, device.vendor,
                         device.tcp_port or "", device.http_port or "", device.https_port or "",
                         device.rtsp_port or "", device.ipv6, device.video_inputs or "", device.protocol])
    return len(devices)


def read_devices_csv(source: TextIO) -> list[Device]:
    """Read a list exported by CamScout or by the earlier Dahua/Hikvision programs."""
    reader = csv.DictReader(source)
    devices: list[Device] = []

    def pick(row: dict[str, str], *names: str) -> str:
        for name in names:
            if row.get(name):
                return row[name]
        return ""

    for row in reader:
        row = {(key or "").strip(): (value or "").strip() for key, value in row.items()}
        ip = pick(row, "IP", "IPv4", "IP адрес", "ip", "IP Address", "IPv4 адрес")
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        brand = pick(row, "Brand", "Бренд")
        if brand.lower().startswith("hik"):
            brand = HIKVISION
        elif not brand and ("Порт устройства" in row or "Порт Enhanced SDK" in row):
            brand = HIKVISION
        else:
            brand = DAHUA
        default_tcp = 8000 if brand == HIKVISION else 37777
        devices.append(Device(
            ip=ip, mac=normalize_mac(pick(row, "MAC")), brand=brand,
            model=pick(row, "Model", "Модель"), device_class=pick(row, "Type", "Тип"),
            serial=pick(row, "Serial number", "Серийный номер", "SN"),
            firmware=pick(row, "Version", "Версия", "Прошивка"),
            subnet_mask=pick(row, "Mask", "Маска", "Маска IPv4"),
            gateway=pick(row, "Gateway", "Шлюз", "Шлюз IPv4"),
            machine_name=pick(row, "Device name", "Имя устройства"),
            vendor=pick(row, "Vendor", "Производитель") or brand,
            tcp_port=_int(pick(row, "TCP port", "TCP порт", "Порт устройства"), default_tcp) or default_tcp,
            http_port=_int(pick(row, "HTTP port", "HTTP порт", "Port"), 80) or 80,
            rtsp_port=_int(pick(row, "RTSP port", "RTSP порт"), 0),
            ipv6=pick(row, "IPv6"), protocol="CSV",
        ))
    return devices
