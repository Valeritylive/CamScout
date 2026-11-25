"""High-level ConfigTool operations that pick the transport, shared by GUI and CLI.

Rules (like ConfigTool):
* a device in our subnet is configured over its HTTP API (documented, reliable);
* a device in another subnet (e.g. factory 192.168.1.108) is configured by MAC over
  DHIP multicast;
* if the first way gets no answer, the other one is tried, but never after the
  device rejected the password — repeated wrong passwords lock the account.
"""

from __future__ import annotations

from i18n import tr
import ipaddress
from dataclasses import dataclass
from typing import Callable, Iterable

import dahua_protocol
from dahua_api import DahuaHttp, DahuaHttpError
import dataclasses

from dahua_discovery import Device, same_subnet
from dahua_protocol import DahuaProtocolError

METHODS = ("auto", "multicast", "http")


class OperationError(Exception):
    pass


@dataclass
class NetworkSettings:
    ip: str = ""
    mask: str = "255.255.255.0"
    gateway: str = ""
    dhcp: bool = False
    dns1: str = ""
    dns2: str = ""

    def validate(self) -> None:
        if self.dhcp:
            return
        try:
            address = ipaddress.IPv4Address(self.ip)
            network = ipaddress.IPv4Network(f"0.0.0.0/{self.mask}")
        except ValueError as exc:
            raise OperationError(tr("Invalid IP or mask: {error}").format(error=exc)) from exc
        if self.gateway:
            try:
                gateway = ipaddress.IPv4Address(self.gateway)
            except ValueError as exc:
                raise OperationError(tr("Invalid gateway: {error}").format(error=exc)) from exc
            net = ipaddress.IPv4Network(f"{address}/{network.prefixlen}", strict=False)
            if gateway not in net:
                raise OperationError(tr("Gateway {gateway} is outside subnet {subnet}").format(gateway=gateway, subnet=net))
        for dns in (self.dns1, self.dns2):
            if dns:
                try:
                    ipaddress.IPv4Address(dns)
                except ValueError as exc:
                    raise OperationError(tr("Invalid DNS: {error}").format(error=exc)) from exc


def http_client(device: Device, username: str, password: str, timeout: float = 8.0) -> DahuaHttp:
    return DahuaHttp(device.ip, device.http_port or 80, username, password, timeout=timeout)


def _auth_rejected(exc: Exception) -> bool:
    if isinstance(exc, DahuaHttpError):
        return exc.status in (401, 403)
    if isinstance(exc, DahuaProtocolError):
        return exc.code in (268632080, 268632081, 268632082) or getattr(exc, "password_rejected", False)
    return False


def modify_ip(device: Device, username: str, password: str, settings: NetworkSettings,
              method: str = "auto") -> str:
    """Apply *settings*; returns which transport did it ("HTTP" or "multicast")."""
    settings.validate()
    order: list[str]
    if method == "http":
        order = ["http"]
    elif method == "multicast":
        order = ["multicast"]
    elif same_subnet(device.ip, device.interface) and device.protocol != "DVRIP":
        order = ["http", "multicast"]
    else:
        order = ["multicast", "http"]
    # DNS is not part of DHDiscover.setConfig, so it needs HTTP.
    if (settings.dns1 or settings.dns2) and method == "auto":
        order = ["http"] + [o for o in order if o != "http"]
    errors: list[str] = []
    for transport in order:
        try:
            if transport == "http":
                http_client(device, username, password).set_network(
                    settings.ip, settings.mask, settings.gateway, settings.dhcp,
                    (settings.dns1, settings.dns2) if (settings.dns1 or settings.dns2) else None)
                return "HTTP"
            if not device.mac:
                raise DahuaProtocolError(tr("the device MAC address is unknown"))
            dahua_protocol.set_network(device, username, password, settings.ip, settings.mask,
                                       settings.gateway, settings.dhcp)
            return tr("multicast")
        except (DahuaHttpError, DahuaProtocolError, OSError) as exc:
            errors.append(f"{'HTTP' if transport == 'http' else tr('multicast')}: {exc}")
            if _auth_rejected(exc):
                break
    raise OperationError("; ".join(errors))


# Configuration sections of Dahua intercom devices (VTO door stations, VTH indoor
# monitors) behind ConfigTool's "Building Config". Firmwares differ, so the
# program reads every candidate and shows only the sections the device has.
INTERCOM_SECTIONS = [
    "VideoTalkPhoneGeneral", "VideoTalkPhoneBasic", "SIPServer", "SIP", "Sip", "RegisterServer",
    "VTOInfo", "VTHNumberInfo", "AccessControlGeneral", "AccessControl", "CommGlobal", "VSP_PaaS",
]


def fetch_details(device: Device, username: str, password: str) -> Device:
    """Fill the empty fields of *device* from its HTTP API; returns an updated copy.

    Discovery replies differ between models and firmwares (old DVRs, devices added
    by IP or from CSV carry little), so the table can show gaps; this reads the same
    data ConfigTool shows after login. One login attempt per device.
    """
    client = http_client(device, username, password)
    info = client.system_info()  # raises at once on a wrong password
    updated = dataclasses.replace(device)

    def fill(attr: str, value) -> None:
        if value not in (None, "", 0) and getattr(updated, attr) in (None, "", 0):
            setattr(updated, attr, value)

    fill("model", info.get("deviceType") or info.get("updateSerial", ""))
    fill("serial", info.get("serialNumber", ""))
    fill("firmware", (info.get("SoftwareVersion") or "").split(",")[0].strip())
    fill("device_class", info.get("DeviceType", ""))
    fill("vendor", info.get("Vendor", ""))
    fill("machine_name", info.get("MachineName", ""))
    try:
        iface, network = client.get_network()
        prefix = f"Network.{iface}."
        fill("subnet_mask", network.get(prefix + "SubnetMask", ""))
        fill("gateway", network.get(prefix + "DefaultGateway", ""))
        if updated.dhcp is None and prefix + "DhcpEnable" in network:
            updated.dhcp = network[prefix + "DhcpEnable"].lower() == "true"
        fill("mac", (network.get(prefix + "PhysicalAddress") or "").lower())
        for attr, key in (("tcp_port", "Network.TCPPort"), ("udp_port", "Network.UDPPort"),
                          ("https_port", "Network.HttpsPort")):
            value = network.get(key, "")
            if value.isdigit() and not getattr(updated, attr):
                setattr(updated, attr, int(value))
    except DahuaHttpError as exc:
        if exc.auth_failed:
            raise
    if not updated.video_inputs:
        try:
            updated.video_inputs = client.video_inputs()
        except DahuaHttpError as exc:
            if exc.auth_failed:
                raise
    return updated


def sequential_ips(start: str, count: int) -> list[str]:
    first = ipaddress.IPv4Address(start)
    result = []
    for index in range(count):
        address = first + index
        if str(address).endswith(".255") or str(address).endswith(".0"):
            raise OperationError(tr("The range includes the reserved address {address}").format(address=address))
        result.append(str(address))
    return result


def batch_modify_ip(devices: list[Device], username: str, password: str, start_ip: str, mask: str,
                    gateway: str, dhcp: bool = False, method: str = "auto",
                    on_result: Callable[[Device, str | None, str], None] | None = None) -> int:
    """Give the devices consecutive addresses from *start_ip*; returns the failure count."""
    ips = [""] * len(devices) if dhcp else sequential_ips(start_ip, len(devices))
    failures = 0
    for device, ip in zip(devices, ips):
        try:
            via = modify_ip(device, username, password,
                            NetworkSettings(ip=ip, mask=mask, gateway=gateway, dhcp=dhcp), method)
            if on_result:
                on_result(device, None, f"{ip or 'DHCP'} ({via})")
        except OperationError as exc:
            failures += 1
            if on_result:
                on_result(device, str(exc), ip)
    return failures


def initialize(device: Device, password: str, email: str = "", phone: str = "",
               username: str = "admin") -> None:
    if device.initialized is True:
        raise OperationError(tr("The device is already initialized"))
    if not device.mac:
        raise OperationError(tr("The device MAC address is unknown"))
    try:
        dahua_protocol.init_device(device, password, email, phone, username)
    except (DahuaProtocolError, OSError) as exc:
        raise OperationError(str(exc)) from exc


def for_each(devices: Iterable[Device], action: Callable[[Device], str | None],
             on_result: Callable[[Device, str | None, str], None] | None = None) -> int:
    """Run *action* per device, reporting (device, error, info); returns failures."""
    failures = 0
    for device in devices:
        try:
            info = action(device) or tr("Done")
            if on_result:
                on_result(device, None, info)
        except (DahuaHttpError, DahuaProtocolError, OperationError, OSError) as exc:
            failures += 1
            if on_result:
                on_result(device, str(exc), "")
    return failures
