"""Device list columns: Dahua ConfigTool order plus a Brand column and the
extra fields Hikvision SADP reports.

Titles are English source strings; the window shows them through ``tr()``.
Kept free of GTK so the same definitions drive the table, the filter and the
CSV export, and can be unit-tested without a display.
"""

from __future__ import annotations

from typing import Any, Callable

from dahua_discovery import ip_sort_key
from devices import HIKVISION, status_text
from i18n import tr


def _int_key(text: str) -> tuple:
    return (0, int(text)) if text.isdigit() else (1, text)


def _port(value: int) -> str:
    return str(value) if value else ""


def _dhcp(device: Any) -> str:
    if device.dhcp is None:
        return ""
    return tr("On") if device.dhcp else tr("Off")


def _channels(device: Any) -> str:
    if device.remote_inputs:
        return f"{device.video_inputs}+{device.remote_inputs}" if device.video_inputs else str(device.remote_inputs)
    return str(device.video_inputs) if device.video_inputs else ""


def _raw(device: Any, *names: str) -> str:
    raw = device.raw or {}
    for name in names:
        value = raw.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _hik_connect(device: Any) -> str:
    if device.brand != HIKVISION:
        return ""
    supported = _raw(device, "supporthcplatform", "supportezviz").lower()
    if not supported:
        return ""
    if supported in ("false", "0", "no"):
        return tr("No")
    enabled = _raw(device, "hcplatformenable", "ezvizenable").lower()
    if enabled:
        return tr("Yes (on)") if enabled in ("true", "1", "yes") else tr("Yes (off)")
    return tr("Yes")


def _reset_ways(device: Any) -> str:
    return device.reset_ways_text() if device.brand != HIKVISION else ""


# (key, title, getter, sort key, initial width in px, shown by default).
# ConfigTool's list: NO., Status, Type, Model, IP, MAC, Version — with Brand after the number.
# The number column is filled by the window from the discovery order.
Column = tuple[str, str, Callable[[Any], str], Callable[[str], Any], int, bool]

COLUMNS: list[Column] = [
    ("no", "No.", lambda d: "", _int_key, 48, True),
    ("brand", "Brand", lambda d: d.brand, str.lower, 96, True),
    ("status", "Status", status_text, str.lower, 140, True),
    ("type", "Type", lambda d: d.device_class, str.lower, 64, True),
    ("model", "Model", lambda d: d.model, str.lower, 200, True),
    ("ip", "IP", lambda d: d.ip, ip_sort_key, 120, True),
    ("mac", "MAC", lambda d: d.mac, str.lower, 130, True),
    ("version", "Version", lambda d: d.firmware, str.lower, 210, True),
    ("mask", "Mask", lambda d: d.subnet_mask, ip_sort_key, 115, False),
    ("gateway", "Gateway", lambda d: d.gateway, ip_sort_key, 115, False),
    ("dhcp", "DHCP", _dhcp, str, 60, False),
    ("port", "TCP/SDK port", lambda d: _port(d.tcp_port), _int_key, 90, False),
    ("http", "HTTP port", lambda d: _port(d.http_port), _int_key, 75, False),
    ("rtsp", "RTSP port", lambda d: _port(d.rtsp_port), _int_key, 75, False),
    ("serial", "Serial number", lambda d: d.serial, str.lower, 200, False),
    ("name", "Device name", lambda d: d.machine_name, str.lower, 140, False),
    ("vendor", "Vendor", lambda d: d.vendor, str.lower, 110, False),
    ("channels", "Channels", _channels, _int_key, 70, False),
    ("ipv6", "IPv6", lambda d: d.ipv6, str.lower, 160, False),
    ("ipv6_gateway", "IPv6 gateway", lambda d: d.ipv6_gateway, str.lower, 140, False),
    ("enhanced", "Enhanced SDK port", lambda d: _port(d.enhanced_sdk_port), _int_key, 120, False),
    ("dsp", "DSP version", lambda d: _raw(d, "dspversion"), str.lower, 150, False),
    ("boot", "Boot time", lambda d: _raw(d, "boottime"), str, 150, False),
    ("hik_connect", "Hik-Connect", _hik_connect, str, 110, False),
    ("reset", "Password reset methods", _reset_ways, str.lower, 170, False),
    ("protocol", "Protocol", lambda d: d.protocol, str, 80, False),
]

COLUMN_KEYS = [column[0] for column in COLUMNS]

# ConfigTool's type filter: "All" plus the common device classes.
TYPE_FILTERS = ["All", "IPC", "NVR", "DVR", "XVR", "HCVR", "SD", "VTO", "VTH", "Other"]
# Brand filter of the toolbar.
BRAND_FILTERS = ["All brands", "Dahua", "Hikvision"]


def column_value(device: Any, key: str, number: int | None = None) -> str:
    if key == "no":
        return str(number) if number is not None else ""
    for column_key, _title, getter, _sort, _width, _shown in COLUMNS:
        if column_key == key:
            return getter(device) or ""
    raise KeyError(key)


def row_values(device: Any, number: int | None = None) -> list[str]:
    return [column_value(device, key, number) for key in COLUMN_KEYS]


def matches_type(device: Any, type_filter: str) -> bool:
    if type_filter in ("", "All"):
        return True
    device_class = (device.device_class or "").upper()
    if type_filter == "Other":
        known = [t for t in TYPE_FILTERS if t not in ("All", "Other")]
        return not any(device_class.startswith(t) for t in known)
    return device_class.startswith(type_filter.upper())


def matches_brand(device: Any, brand_filter: str) -> bool:
    return brand_filter in ("", "All brands") or device.brand == brand_filter


def matches_filter(device: Any, query: str, number: int | None = None) -> bool:
    """ConfigTool search box: case-insensitive substring over every column."""
    query = query.strip().lower()
    if not query:
        return True
    return any(query in value.lower() for value in row_values(device, number))


def sort_key(key: str) -> Callable[[str], Any]:
    for column_key, _title, _getter, sorter, _width, _shown in COLUMNS:
        if column_key == key:
            return sorter
    raise KeyError(key)
