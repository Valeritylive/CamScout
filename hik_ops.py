"""Hikvision operations on the common Device row (no GTK).

Like Windows SADP, activation and network changes go over SADP to the device
MAC first, so they work without IP reachability; ISAPI over HTTP is the
fallback (and the only way when the MAC is unknown).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

import sadp_protocol
from dahua_discovery import Device
from devices import sadp_record
from hikvision_api import HikvisionError, activate_device, get_network_interfaces, set_ipv4
from i18n import tr
from sadp_discovery import SADP_SOCKET_LOCK
from sadp_protocol import NetworkParams, SadpError, SadpTransport

METHODS = ("sadp", "isapi")


@dataclass
class HikNetwork:
    ip: str = ""
    mask: str = "255.255.255.0"
    gateway: str = ""
    dhcp: bool = False
    command_port: int = 8000
    http_port: int = 80
    ipv6: str = ""
    ipv6_gateway: str = ""
    ipv6_prefix: int = 64
    dns1: str = ""
    dns2: str = ""

    def params(self) -> NetworkParams:
        return NetworkParams(ipv4=self.ip, mask=self.mask, gateway=self.gateway, dhcp=self.dhcp,
                             command_port=self.command_port, http_port=self.http_port,
                             ipv6=self.ipv6 or "::", ipv6_gateway=self.ipv6_gateway or "::",
                             ipv6_prefix=self.ipv6_prefix)

    def validate(self) -> None:
        self.params().validate()
        for dns in (self.dns1, self.dns2):
            if dns:
                try:
                    ipaddress.IPv4Address(dns)
                except ValueError as exc:
                    raise SadpError(tr("Invalid DNS: {error}").format(error=exc)) from exc


def _interface(device: Device, fallback: tuple[str, str, str] | None):
    interface = device.interface or fallback
    if not interface:
        raise SadpError(tr("No active network interface for SADP."))
    return interface


def activate(device: Device, password: str, fallback_interface: tuple[str, str, str] | None = None) -> None:
    """Activate over SADP, then ISAPI/HCNetSDK over IP if SADP fails."""
    record = sadp_record(device)
    sadp_error = None
    if record.mac:
        try:
            interface = _interface(device, fallback_interface)
            with SADP_SOCKET_LOCK, SadpTransport(interface[1], interface[2]) as transport:
                sadp_protocol.activate(transport, record.mac, password)
            return
        except (SadpError, OSError) as exc:
            sadp_error = exc
    try:
        activate_device(device.ip, device.http_port or 80, password, device.tcp_port or 8000)
    except HikvisionError as exc:
        if sadp_error is None:
            raise
        raise HikvisionError(f"SADP: {sadp_error}\nISAPI: {exc}") from exc


def modify_network(device: Device, username: str, password: str, settings: HikNetwork,
                   method: str = "sadp", fallback_interface: tuple[str, str, str] | None = None) -> str:
    """Apply *settings*; returns which transport did it ("SADP" or "ISAPI")."""
    settings.validate()
    if not password:
        raise SadpError(tr("Enter the administrator password."))
    record = sadp_record(device)
    if method == "sadp" and record.mac and not (settings.dns1 or settings.dns2):
        interface = _interface(device, fallback_interface)
        with SADP_SOCKET_LOCK, SadpTransport(interface[1], interface[2]) as transport:
            sadp_protocol.modify_network(transport, record.mac, password, settings.params(),
                                         probe_fields=record.raw or {})
        return "SADP"
    interfaces = get_network_interfaces(device.ip, device.http_port or 80, username or "admin", password)
    target = next((item for item in interfaces if item.get("ip") == device.ip), interfaces[0])
    set_ipv4(device.ip, device.http_port or 80, username or "admin", password, target["id"],
             "" if settings.dhcp else settings.ip, settings.mask, settings.gateway,
             settings.dns1, settings.dns2, settings.dhcp,
             settings.ipv6, str(settings.ipv6_prefix) if settings.ipv6 else "", settings.ipv6_gateway)
    return "ISAPI"
