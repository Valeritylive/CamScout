"""Dahua HTTP API (CGI) client: the documented interface behind ConfigTool's
"System settings", time sync, reboot, factory reset and firmware upgrade.

Requests use HTTP digest authentication. Responses are ``key=value`` lines
(``table.`` prefix for configuration), ``OK`` for set operations, ``Error`` plus
a reason otherwise. No GTK imports, so the CLI can use it too.
"""

from __future__ import annotations

from i18n import tr
import io
import os
import time
from datetime import datetime
from typing import Any, Callable
from urllib.parse import quote

import requests
from requests.auth import HTTPDigestAuth

try:  # devices ship self-signed certificates; do not spam warnings for them
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except (ImportError, AttributeError):
    pass

# Several wrong passwords in a row lock the account on Dahua devices for ~30 min.
LOCK_HINT = "Several wrong passwords in a row lock the Dahua account for about 30 minutes."


class DahuaHttpError(Exception):
    def __init__(self, message: str, status: int | None = None, offline: bool = False):
        super().__init__(message)
        self.status = status
        self.offline = offline  # no connection at all (the device may be rebooting)

    @property
    def auth_failed(self) -> bool:
        return self.status in (401, 403)


def parse_table(text: str) -> dict[str, str]:
    """Parse ``key=value`` lines; strips the ``table.`` prefix of configManager."""
    values: dict[str, str] = {}
    for line in text.replace("\r", "").split("\n"):
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("table."):
            key = key[6:]
        values[key] = value.strip()
    return values


def _query(pairs: list[tuple[str, Any]]) -> str:
    # Dahua firmwares expect literal brackets and dots in names like
    # Network.eth0.DnsServers[0]; percent-encoding them breaks some models.
    return "&".join(f"{quote(str(k), safe='[].')}={quote(str(v), safe='')}" for k, v in pairs)


class _ProgressReader(io.BytesIO):
    """Rewindable upload body (digest auth may resend it) with a progress callback."""

    def __init__(self, data: bytes, callback: Callable[[float], None] | None):
        super().__init__(data)
        self.total = len(data)
        self.callback = callback

    def read(self, size: int | None = -1) -> bytes:  # type: ignore[override]
        chunk = super().read(size if size is not None and size > 0 else 64 * 1024)
        if self.callback and self.total:
            fraction = self.tell() / self.total
            # http.client reads 8 KiB at a time; report whole percents only.
            if int(fraction * 100) != getattr(self, "_last_percent", -1) or fraction >= 1:
                self._last_percent = int(fraction * 100)
                self.callback(fraction)
        return chunk


class DahuaHttp:
    def __init__(self, host: str, port: int = 80, username: str = "admin", password: str = "",
                 https: bool = False, timeout: float = 8.0):
        self.host = host
        self.port = port or (443 if https else 80)
        self.username = username
        self.password = password
        self.https = https
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = False  # devices ship self-signed certificates
        self.auth = HTTPDigestAuth(username, password)

    @property
    def base_url(self) -> str:
        scheme = "https" if self.https else "http"
        default = 443 if self.https else 80
        port = "" if self.port == default else f":{self.port}"
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{scheme}://{host}{port}"

    # -- transport ---------------------------------------------------------
    def request(self, path: str, pairs: list[tuple[str, Any]] | None = None, method: str = "GET",
                timeout: float | None = None, **kwargs) -> str:
        url = f"{self.base_url}{path}"
        if pairs:
            url += ("&" if "?" in url else "?") + _query(pairs)
        try:
            response = self.session.request(method, url, auth=self.auth,
                                            timeout=timeout or self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise DahuaHttpError(tr("No connection to {host}:{port}: {error}").format(
                host=self.host, port=self.port, error=exc.__class__.__name__), offline=True) from exc
        if response.status_code == 401:
            raise DahuaHttpError(tr("Wrong user name or password.") + " " + tr(LOCK_HINT), 401)
        if response.status_code == 403:
            raise DahuaHttpError(tr("Access denied (the account is locked or has no rights)"), 403)
        if response.status_code == 404:
            raise DahuaHttpError(tr("The device does not support this function (HTTP 404)"), 404)
        text = response.content.decode("utf-8", errors="replace")
        if response.status_code >= 400 or text.strip().lower().startswith("error"):
            reason = " ".join(text.split())[:200] or f"HTTP {response.status_code}"
            raise DahuaHttpError(tr("The device returned an error: {reason}").format(reason=reason), response.status_code)
        return text

    def get_table(self, path: str, pairs: list[tuple[str, Any]] | None = None) -> dict[str, str]:
        return parse_table(self.request(path, pairs))

    def expect_ok(self, path: str, pairs: list[tuple[str, Any]] | None = None, **kwargs) -> None:
        text = self.request(path, pairs, **kwargs)
        if "ok" not in text.strip().lower()[:8]:
            raise DahuaHttpError(tr("Unexpected device reply: {reply}").format(reply=' '.join(text.split())[:120]))

    # -- information -------------------------------------------------------
    def system_info(self) -> dict[str, str]:
        info = self.get_table("/cgi-bin/magicBox.cgi", [("action", "getSystemInfo")])
        for action, key in (("getSoftwareVersion", "version"), ("getDeviceType", "type"),
                            ("getMachineName", "name"), ("getHardwareVersion", "version"),
                            ("getVendor", "vendor")):
            try:
                table = self.get_table("/cgi-bin/magicBox.cgi", [("action", action)])
            except DahuaHttpError as exc:
                if exc.auth_failed:
                    raise  # never keep trying after a rejected login (account lockout)
                continue
            value = table.get(key) or next(iter(table.values()), "")
            info.setdefault(action[3:], value)
        return info

    def get_config(self, name: str) -> dict[str, str]:
        return self.get_table("/cgi-bin/configManager.cgi", [("action", "getConfig"), ("name", name)])

    def set_config(self, values: dict[str, Any]) -> None:
        if not values:
            return
        pairs: list[tuple[str, Any]] = [("action", "setConfig")]
        for key, value in values.items():
            if isinstance(value, bool):
                value = "true" if value else "false"
            pairs.append((key, value))
        self.expect_ok("/cgi-bin/configManager.cgi", pairs)

    def video_inputs(self) -> int:
        """Number of video channels (devVideoInput.cgi getCollect: result=N)."""
        table = self.get_table("/cgi-bin/devVideoInput.cgi", [("action", "getCollect")])
        try:
            return int(table.get("result", "0"))
        except ValueError:
            return 0

    def existing_configs(self, names: list[str]) -> dict[str, dict[str, str]]:
        """Read each section in *names*; returns only those the device has.

        A wrong password stops the probe at once (no repeated login attempts).
        """
        found: dict[str, dict[str, str]] = {}
        for name in names:
            try:
                table = self.get_config(name)
            except DahuaHttpError as exc:
                if exc.auth_failed:
                    raise
                continue
            if table:
                found[name] = table
        return found

    # -- P2P (Easy4ip / DMSS cloud) ---------------------------------------
    def get_p2p(self) -> dict[str, str]:
        """P2P settings: the ``T2UServer`` section (Enable, server, port...)."""
        return self.get_config("T2UServer")

    def set_p2p(self, enable: bool) -> None:
        self.set_config({"T2UServer.Enable": bool(enable)})

    # -- network -----------------------------------------------------------
    def get_network(self) -> tuple[str, dict[str, str]]:
        """Return (default interface, flat Network table)."""
        table = self.get_config("Network")
        iface = table.get("Network.DefaultInterface") or "eth0"
        if not any(key.startswith(f"Network.{iface}.") for key in table):
            names = sorted({key.split(".")[1] for key in table
                            if key.count(".") >= 2 and key.split(".")[2] == "IPAddress"})
            iface = names[0] if names else iface
        return iface, table

    def set_network(self, ip: str, mask: str, gateway: str, dhcp: bool = False,
                    dns: tuple[str, str] | None = None, iface: str | None = None) -> None:
        if iface is None:
            iface, _table = self.get_network()
        values: dict[str, Any] = {f"Network.{iface}.DhcpEnable": bool(dhcp)}
        if not dhcp:
            values.update({f"Network.{iface}.IPAddress": ip, f"Network.{iface}.SubnetMask": mask,
                           f"Network.{iface}.DefaultGateway": gateway})
        if dns:
            for index, server in enumerate(dns):
                if server:
                    values[f"Network.{iface}.DnsServers[{index}]"] = server
        try:
            self.set_config(values)
        except DahuaHttpError as exc:
            # The device often drops the connection while applying a new address.
            if exc.offline:
                return
            raise

    # -- time --------------------------------------------------------------
    def get_time(self) -> str:
        table = self.get_table("/cgi-bin/global.cgi", [("action", "getCurrentTime")])
        return table.get("result", "")

    def set_time(self, moment: datetime | None = None) -> None:
        moment = moment or datetime.now()
        self.expect_ok("/cgi-bin/global.cgi", [("action", "setCurrentTime"),
                                               ("time", moment.strftime("%Y-%m-%d %H:%M:%S"))])

    def get_ntp(self) -> dict[str, str]:
        return self.get_config("NTP")

    def set_ntp(self, enable: bool, address: str, port: int = 123, period_min: int = 60,
                timezone: str | None = None) -> None:
        values: dict[str, Any] = {"NTP.Enable": enable, "NTP.Address": address, "NTP.Port": port,
                                  "NTP.UpdatePeriod": period_min}
        if timezone is not None:
            values["NTP.TimeZone"] = timezone
        self.set_config(values)

    # -- maintenance -------------------------------------------------------
    def reboot(self) -> None:
        try:
            self.expect_ok("/cgi-bin/magicBox.cgi", [("action", "reboot")])
        except DahuaHttpError as exc:
            if exc.offline:
                return  # rebooted before answering
            raise

    def factory_reset(self, keep_network: bool = True) -> None:
        """Restore defaults; by default keeps network settings like ConfigTool."""
        if keep_network:
            self.expect_ok("/cgi-bin/configManager.cgi",
                           [("action", "restoreExcept"), ("names[0]", "Network")])
        else:
            self.expect_ok("/cgi-bin/magicBox.cgi", [("action", "resetSystemEx"), ("type", 0)])

    def change_password(self, username: str, old_password: str, new_password: str) -> None:
        self.expect_ok("/cgi-bin/userManager.cgi", [("action", "modifyPassword"), ("name", username),
                                                     ("pwd", new_password), ("pwdOld", old_password)])

    def users(self) -> list[dict[str, str]]:
        table = self.get_table("/cgi-bin/userManager.cgi", [("action", "getUserInfoAll")])
        users: dict[str, dict[str, str]] = {}
        for key, value in table.items():
            # users[0].Name=admin
            if key.startswith("users[") and "]." in key:
                index, field_name = key[6:].split("].", 1)
                users.setdefault(index, {})[field_name] = value
        return [users[index] for index in sorted(users, key=lambda i: int(i) if i.isdigit() else 0)]

    def logs(self, start: datetime, end: datetime, limit: int = 1000) -> list[dict[str, str]]:
        token = self.get_table("/cgi-bin/log.cgi", [
            ("action", "startFind"),
            ("condition.StartTime", start.strftime("%Y-%m-%d %H:%M:%S")),
            ("condition.EndTime", end.strftime("%Y-%m-%d %H:%M:%S")),
        ]).get("token", "")
        if not token:
            raise DahuaHttpError(tr("The device did not start the log search"))
        records: list[dict[str, str]] = []
        try:
            while len(records) < limit:
                table = self.get_table("/cgi-bin/log.cgi", [("action", "doFind"), ("token", token),
                                                             ("count", 100)])
                batch: dict[str, dict[str, str]] = {}
                for key, value in table.items():
                    if key.startswith("items[") and "]." in key:
                        index, field_name = key[6:].split("].", 1)
                        batch.setdefault(index, {})[field_name] = value
                if not batch:
                    break
                records.extend(batch[i] for i in sorted(batch, key=lambda i: int(i) if i.isdigit() else 0))
                if len(batch) < 100:
                    break
        finally:
            try:
                self.request("/cgi-bin/log.cgi", [("action", "stopFind"), ("token", token)])
            except DahuaHttpError:
                pass
        return records[:limit]

    # -- firmware ----------------------------------------------------------
    def upgrade_state(self) -> dict[str, str]:
        return self.get_table("/cgi-bin/upgrader.cgi", [("action", "getState")])

    def upgrade(self, firmware_path: str, progress: Callable[[str, float], None] | None = None,
                poll_timeout: float = 900.0) -> str:
        """Upload a firmware .bin and wait for the result; returns the final state.

        Format from the Dahua HTTP API: POST upgrader.cgi?action=uploadFirmware with
        a single ``application/octet-stream`` part in a multipart/x-mixed-replace body,
        then poll upgrader.cgi?action=getState (state/progress).
        """
        with open(firmware_path, "rb") as handle:
            data = handle.read()
        if not data:
            raise DahuaHttpError(tr("The firmware file is empty"))
        # Authenticate with a cheap request first so the big body is sent once.
        self.request("/cgi-bin/magicBox.cgi", [("action", "getDeviceType")])
        boundary = "----DahuaDeviceManager" + os.urandom(8).hex()
        head = (f"--{boundary}\r\nContent-Type: application/octet-stream\r\n"
                f"Content-Length: {len(data)}\r\n\r\n").encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        body = _ProgressReader(head + data + tail,
                               (lambda f: progress("Uploading", f * 100)) if progress else None)
        headers = {"Content-Type": f"multipart/x-mixed-replace; boundary={boundary}"}
        try:
            self.request("/cgi-bin/upgrader.cgi", [("action", "uploadFirmware")], method="POST",
                         data=body, headers=headers, timeout=max(120.0, len(data) / 200_000))
        except DahuaHttpError as exc:
            if exc.status is not None:
                raise
            # Some firmwares close the socket after the upload instead of answering.
        deadline = time.monotonic() + poll_timeout
        last_state = "Upgrading"
        seen_progress = 0.0
        failures = 0
        while time.monotonic() < deadline:
            time.sleep(2)
            try:
                state = self.upgrade_state()
                failures = 0
            except DahuaHttpError:
                failures += 1
                # The device reboots at the end of a successful upgrade.
                if seen_progress >= 99 or failures >= 5:
                    return "Rebooting" if seen_progress >= 99 or last_state == "Succeeded" else last_state
                continue
            last_state = state.get("state", last_state)
            try:
                seen_progress = float(state.get("progress", seen_progress))
            except ValueError:
                pass
            if progress:
                progress(last_state, seen_progress)
            if last_state in ("Succeeded", "Failed", "Invalid", "NotEnoughMemory", "FileUnmatch"):
                if last_state != "Succeeded":
                    raise DahuaHttpError(tr("Upgrade failed: {state}").format(state=tr(UPGRADE_STATES.get(last_state, last_state))))
                return last_state
        raise DahuaHttpError(tr("Timed out waiting for the upgrade"))


# Device upgrade states -> English source text (shown through tr()).
UPGRADE_STATES = {
    "Preparing": "Preparing",
    "Downloading": "Downloading",
    "Uploading": "Uploading",
    "Upgrading": "Upgrading",
    "Succeeded": "Succeeded",
    "Failed": "Failed",
    "Invalid": "Invalid file",
    "NotEnoughMemory": "Not enough memory",
    "FileUnmatch": "File does not match",
    "Rebooting": "Rebooting",
}
