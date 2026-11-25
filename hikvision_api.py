"""ISAPI operations used by the activation and recovery setup dialogs."""

from __future__ import annotations

from i18n import tr
import base64
import ctypes
import hashlib
import ipaddress
import json
import os
import secrets
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


ISAPI_NS = "http://www.isapi.org/ver20/XMLSchema"


class HikvisionError(RuntimeError):
    pass


def _check(response: requests.Response) -> bytes:
    if response.status_code >= 400:
        raise HikvisionError(tr("The device answered HTTP {status}: {text}").format(status=response.status_code, text=response.text[:300]))
    body = response.content
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return body
    status = next((node.text for node in root.iter()
                   if node.tag.rsplit("}", 1)[-1].lower() == "statuscode"), None)
    if status and status not in ("0", "1", "200", "OK"):
        message = next((node.text for node in root.iter()
                        if node.tag.rsplit("}", 1)[-1].lower() in ("statusstring", "substatuscode")), "")
        raise HikvisionError(tr("The device rejected the request ({status}): {message}").format(
            status=status, message=message or tr("check the password and ISAPI support")))
    return body


def _check_json_status(body: bytes) -> None:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HikvisionError(tr("The device returned an invalid JSON reply.")) from exc
    status = payload.get("ResponseStatus", payload) if isinstance(payload, dict) else None
    if not isinstance(status, dict) or "statusCode" not in status:
        raise HikvisionError(tr("The device did not return the operation status."))
    code = str(status.get("statusCode", "")).strip()
    if code not in ("0", "1", "200", "OK"):
        detail = status.get("subStatusCode") or status.get("statusString") or tr("device error")
        raise HikvisionError(tr("The device rejected the operation ({code}): {detail}").format(code=code, detail=detail))


class HikvisionClient:
    def __init__(self, address: str, port: int, username: str, password: str):
        self.base_url = f"http://{address}:{port}"
        self.session = requests.Session()
        self.session.auth = requests.auth.HTTPDigestAuth(username, password)
        self.session.headers.update({"User-Agent": "CamScout"})

    def request(self, method: str, path: str, *, params: dict | None = None,
                body: bytes | None = None, content_type: str | None = None) -> bytes:
        headers = {"Content-Type": content_type} if content_type else {}
        try:
            response = self.session.request(method, self.base_url + path, params=params,
                                            data=body, headers=headers, timeout=6)
        except requests.RequestException as exc:
            raise HikvisionError(tr("Could not reach the device: {error}").format(error=exc)) from exc
        return _check(response)


def _activate_via_hcnetsdk(address: str, service_port: int, password: str) -> None:
    """Use the bundled HCNetSDK activation entry point when ISAPI activation fails."""
    sdk_lib = Path(__file__).resolve().parent / "EN-HCNetSDKV6.1.9.4_build20220412_linux64/lib/libhcnetsdk.so"
    configured_lib = os.environ.get("HIKVISION_HCNETSDK_LIBRARY")
    candidates = [Path(configured_lib)] if configured_lib else []
    candidates.append(sdk_lib)
    library_path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if library_path is None:
        raise HikvisionError(tr("Local HCNetSDK not found."))
    password_bytes = password.encode("utf-8")
    if len(password_bytes) > 16:
        raise HikvisionError(tr("HCNetSDK allows activation passwords of up to 16 bytes."))
    class ActivateConfig(ctypes.Structure):
        _fields_ = [("dwSize", ctypes.c_uint32), ("sPassword", ctypes.c_ubyte * 16),
                    ("byLoginMode", ctypes.c_ubyte), ("byHttps", ctypes.c_ubyte),
                    ("byRes", ctypes.c_ubyte * 106)]
    try:
        sdk = ctypes.CDLL(str(library_path))
    except OSError as exc:
        raise HikvisionError(tr("Could not load HCNetSDK: {error}").format(error=exc)) from exc
    sdk.NET_DVR_Init.argtypes = []
    sdk.NET_DVR_Init.restype = ctypes.c_int
    sdk.NET_DVR_Cleanup.argtypes = []
    sdk.NET_DVR_Cleanup.restype = ctypes.c_int
    sdk.NET_DVR_GetLastError.argtypes = []
    sdk.NET_DVR_GetLastError.restype = ctypes.c_uint32
    sdk.NET_DVR_ActivateDevice.argtypes = [ctypes.c_char_p, ctypes.c_uint16,
                                           ctypes.POINTER(ActivateConfig)]
    sdk.NET_DVR_ActivateDevice.restype = ctypes.c_int
    if not sdk.NET_DVR_Init():
        raise HikvisionError(tr("HCNetSDK could not initialize (code {code}).").format(code=sdk.NET_DVR_GetLastError()))
    try:
        config = ActivateConfig()
        config.dwSize = ctypes.sizeof(config)
        ctypes.memmove(ctypes.addressof(config) + ActivateConfig.sPassword.offset,
                       password_bytes.ljust(16, b"\0"), 16)
        config.byLoginMode = 0
        config.byHttps = 0
        if not sdk.NET_DVR_ActivateDevice(address.encode("ascii"), service_port, ctypes.byref(config)):
            raise HikvisionError(tr("HCNetSDK did not activate the device (code {code}).").format(code=sdk.NET_DVR_GetLastError()))
    finally:
        sdk.NET_DVR_Cleanup()


def activate_device(address: str, port: int, password: str, service_port: int = 8000) -> None:
    """Initialize an inactive unit using its local ISAPI activation endpoint."""
    root = ET.Element("ActivateInfo")
    ET.SubElement(root, "password").text = password
    body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    try:
        response = requests.put(f"http://{address}:{port}/ISAPI/System/activate",
                               data=body, headers={"Content-Type": "application/xml"}, timeout=8)
    except requests.RequestException as exc:
        isapi_error = HikvisionError(tr("Could not reach the device over ISAPI: {error}").format(error=exc))
        try:
            _activate_via_hcnetsdk(address, service_port, password)
        except HikvisionError as sdk_error:
            raise HikvisionError(f"ISAPI: {isapi_error}\nHCNetSDK: {sdk_error}") from sdk_error
        return
    try:
        _check(response)
    except HikvisionError as isapi_error:
        try:
            _activate_via_hcnetsdk(address, service_port, password)
        except HikvisionError as sdk_error:
            raise HikvisionError(f"ISAPI: {isapi_error}\nHCNetSDK: {sdk_error}") from sdk_error


def _xml_value(root: ET.Element, name: str, default: str = "") -> str:
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].lower() == name.lower():
            return (element.text or "").strip()
    return default


def _hex_bytes(text: str) -> bytes:
    try:
        return bytes.fromhex(text)
    except ValueError as exc:
        raise HikvisionError(tr("The device sent invalid encryption parameters.")) from exc


def _make_key(password: str, iterations: int, *, username: str = "admin",
              salt: str = "", irreversible: bool = False, key_size: int = 16) -> bytes:
    if iterations < 1 or iterations > 10000:
        raise HikvisionError(tr("The device sent an invalid number of encryption iterations."))
    if irreversible and salt:
        salt_bytes = salt.encode("utf-8")
        start = (username.encode() + salt_bytes.ljust(64, b"\0")[:64] + password.encode()).decode("utf-8", errors="replace")
        seed = hashlib.sha256(start.encode("utf-8")).hexdigest()
    else:
        seed = password[:64]
    value = seed + "AaBbCcDd1234!@#$"
    for _ in range(iterations):
        value = hashlib.sha256(value.encode("utf-8")).hexdigest()
    if key_size not in (16, 32):
        raise HikvisionError(tr("The device requested an unsupported encryption key length."))
    # securityVersion 1 uses AES-128 and version 2 uses AES-256.
    return value.encode("ascii")[:key_size]


def _encrypt_sensitive(value: str, key: bytes, iv: bytes) -> str:
    encoded = base64.b64encode(value.encode("utf-8"))
    padded = encoded + b"\0" * ((-len(encoded)) % 16)
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return ciphertext.hex()


def set_recovery_email(address: str, port: int, username: str, password: str, email: str) -> None:
    client = HikvisionClient(address, port, username, password)
    capability_url = "/ISAPI/Security/capabilities"
    try:
        capability = ET.fromstring(client.request("GET", capability_url, params={"username": username}))
    except ET.ParseError as exc:
        raise HikvisionError(tr("Could not read the device security capabilities.")) from exc
    if _xml_value(capability, "isSupportSecurityEmail").lower() != "true":
        raise HikvisionError(tr("This model/firmware does not support a recovery e-mail over ISAPI."))
    try:
        email_cap = json.loads(client.request("GET", "/ISAPI/Security/email/parameter/capabilities",
                                              params={"format": "json"}))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HikvisionError(tr("Could not read the device recovery e-mail settings.")) from exc
    if not email_cap:
        raise HikvisionError(tr("The device did not return the recovery e-mail settings."))

    version = _xml_value(capability, "securityVersion", "1")
    if version not in ("1", "2"):
        raise HikvisionError(tr("The device reports an unsupported ISAPI encryption version: {version}.").format(version=version))
    try:
        iterations = int(_xml_value(capability, "keyIterateNum", "100"))
    except ValueError as exc:
        raise HikvisionError(tr("The device sent wrong encryption parameters.")) from exc
    salt = _xml_value(capability, "salt")
    irreversible = _xml_value(capability, "isIrreversible").lower() == "true"
    key = _make_key(password, iterations, username=username, salt=salt,
                    irreversible=irreversible, key_size=16 if version == "1" else 32)
    iv = secrets.token_bytes(16)
    encrypted_email = _encrypt_sensitive(email, key, iv)
    payload = {"SecurityEmail": {"SecurityInformation": [{"emailAddress": encrypted_email}]}}
    client.request("PUT", "/ISAPI/Security/email/parameter",
                   params={"format": "json", "security": "1", "iv": iv.hex()},
                   body=json.dumps(payload).encode("utf-8"), content_type="application/json")


def export_guid_file(address: str, port: int, username: str, password: str) -> bytes:
    """Download the device's password-recovery GUID file where supported."""
    client = HikvisionClient(address, port, username, password)
    try:
        capabilities = ET.fromstring(client.request("GET", "/ISAPI/Security/capabilities"))
    except ET.ParseError as exc:
        raise HikvisionError(tr("Could not read the device security capabilities.")) from exc
    supported = _xml_value(capabilities, "isSupportGUIDFileDataExport").lower() == "true"
    if not supported:
        raise HikvisionError(tr("This model/firmware does not support GUID file export."))
    try:
        security = ET.fromstring(client.request(
            "GET", "/ISAPI/Security/capabilities", params={"username": username}))
    except ET.ParseError as exc:
        raise HikvisionError(tr("Could not read the GUID export protection settings.")) from exc
    version = _xml_value(security, "securityVersion")
    params = {}
    password_value = password
    if version in ("1", "2"):
        iv = secrets.token_bytes(16)
        key = _make_key(password, int(_xml_value(security, "keyIterateNum", "100")),
                        username=username, salt=_xml_value(security, "salt"),
                        irreversible=_xml_value(security, "isIrreversible").lower() == "true",
                        key_size=16 if version == "1" else 32)
        password_value = _encrypt_sensitive(password, key, iv)
        params = {"security": version, "iv": iv.hex()}
    elif version:
        raise HikvisionError(tr("The device reports an unsupported GUID export protection version: {version}.").format(version=version))
    root = ET.Element("LoginPassword", {"version": "2.0", "xmlns": ISAPI_NS})
    ET.SubElement(root, "password").text = password_value
    try:
        return client.request("POST", "/ISAPI/Security/GUIDFileData", params=params,
                              body=ET.tostring(root, encoding="utf-8", xml_declaration=True),
                              content_type="application/xml")
    except HikvisionError as exc:
        # Some older firmware exposes this read-only operation as GET instead.
        if "HTTP 404" not in str(exc) and "HTTP 405" not in str(exc):
            raise
        return client.request("GET", "/ISAPI/Security/GUIDFileData")


# ---------------------------------------------------------------------------
# Password reset (SADP-style: export request/security code -> import response)
# ---------------------------------------------------------------------------
#
# The official SADP tool resets a forgotten admin password in two steps:
#   1. Export a request file (an "encrypted security code" / XML key) that is
#      bound to the device serial number and the device clock.
#   2. Send that file to Hikvision support or an authorized dealer, who returns
#      a response file; import it and set the new password.
#
# Hikvision exposes this over ISAPI as the "Reset Password by GUID/Code" family
# of endpoints. The exact request/response schemas differ between firmware
# generations and are only partly documented, so the field layout below is a
# best-effort first version. Every place where the wire format is not confirmed
# against a real device is marked with "GUESS:".


def _device_time(client: "HikvisionClient") -> str:
    """Best-effort read of the device clock, used in the reset-request file."""
    try:
        root = ET.fromstring(client.request("GET", "/ISAPI/System/time"))
        return _xml_value(root, "localTime") or _xml_value(root, "timeZone")
    except (HikvisionError, ET.ParseError):
        return ""


def export_reset_request(device_fields: dict[str, str], *, address: str = "", port: int = 80,
                         username: str = "admin", password: str = "") -> bytes:
    """Produce the password-reset request file to hand to Hikvision support.

    Two paths:
      * If the current password is known AND the device supports GUID export,
        return the authenticated ISAPI GUID file (the strongest recovery path).
      * Otherwise build an XML request file from the device's public identifiers
        (serial, MAC, model, firmware) plus the current UTC date. For a fully
        locked-out device, modern firmware also needs a device-provided security
        string that is only retrievable through the official SADP SDK; this
        first version therefore packages the identifiers and marks that gap so
        the dealer/support can still process older devices.
    """
    if password:
        # Authenticated recovery file; this reuses the proven GUID export path.
        return export_guid_file(address, port, username, password)

    serial = (device_fields.get("serialno") or device_fields.get("serialnumber")
              or device_fields.get("deviceid") or "").strip()
    if not serial:
        raise HikvisionError(
            tr("Could not determine the device serial number. Find the device on the network "
               "or enter the administrator password to export a GUID file.")
        )
    mac = (device_fields.get("mac") or device_fields.get("macaddress") or "").strip()
    model = (device_fields.get("deviceproductionname") or device_fields.get("model")
             or device_fields.get("devicetype") or "").strip()
    firmware = (device_fields.get("softwareversion") or device_fields.get("firmwareversion") or "").strip()
    # The reset code Hikvision generates is date-bound; record the date used.
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    root = ET.Element("PasswordResetRequest", {"version": "1.0", "xmlns": ISAPI_NS})
    ET.SubElement(root, "serialNumber").text = serial
    if mac:
        ET.SubElement(root, "macAddress").text = mac
    if model:
        ET.SubElement(root, "model").text = model
    if firmware:
        ET.SubElement(root, "firmwareVersion").text = firmware
    ET.SubElement(root, "requestTime").text = now
    # GUESS: modern firmware binds the reset code to a device-provided security
    # string obtained over SADP UDP; it is not available without the official
    # HCSADPSDK, so it is left empty and flagged here for the operator.
    note = ET.SubElement(root, "note")
    note.text = ("Password reset request file. Send it to Hikvision support or an authorized "
                 "dealer to get a response file. Modern firmware may need a security code "
                 "read by the official SADP SDK of the device.")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def reset_password_with_response(address: str, port: int, response_data: bytes,
                                 new_password: str, *, username: str = "admin") -> None:
    """Import a support-provided response file and set a new admin password.

    Sends the response/key to the device over ISAPI and asks it to apply the new
    password. The device validates the response against its own serial/clock, so
    an expired or mismatched file is rejected by the device itself.
    """
    if len(new_password) < 8:
        raise HikvisionError(tr("The new password must be at least 8 characters long."))
    if not response_data.strip():
        raise HikvisionError(tr("The response file is empty. Load the file you received from support."))

    # Extract the key material from whatever the dealer returned. The response is
    # usually XML; accept a raw code string too.
    code_value = ""
    try:
        resp_root = ET.fromstring(response_data)
        code_value = (_xml_value(resp_root, "GUID") or _xml_value(resp_root, "code")
                      or _xml_value(resp_root, "securityCode") or _xml_value(resp_root, "key")
                      or (resp_root.text or "")).strip()
    except ET.ParseError:
        code_value = response_data.decode("utf-8", errors="replace").strip()
    if not code_value:
        raise HikvisionError(tr("No password reset code found in the response file."))

    # Build the reset request. GUESS: the documented "Reset Password by GUID"
    # endpoint is PUT /ISAPI/Security/GUID carrying the returned code plus the
    # new password. Field names (GUID/password) and the plaintext password form
    # match the GUID-recovery schema but are not confirmed across all firmware.
    root = ET.Element("GUIDFileDataImport", {"version": "2.0", "xmlns": ISAPI_NS})
    ET.SubElement(root, "GUID").text = code_value
    ET.SubElement(root, "password").text = new_password
    body = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    # A locked-out device cannot be authenticated, so the reset endpoint is
    # called without digest credentials.
    endpoints = (
        ("PUT", "/ISAPI/Security/GUID"),          # GUESS: reset-by-GUID import
        ("PUT", "/ISAPI/Security/GUIDFileData"),  # GUESS: some firmware reuses this path
    )
    last_error: HikvisionError | None = None
    for method, path in endpoints:
        try:
            response = requests.request(method, f"http://{address}:{port}{path}",
                                        data=body, headers={"Content-Type": "application/xml"},
                                        timeout=8)
        except requests.RequestException as exc:
            last_error = HikvisionError(tr("Could not reach the device: {error}").format(error=exc))
            continue
        try:
            _check(response)
            return
        except HikvisionError as exc:
            last_error = exc
            # Try the next candidate endpoint only on 404/405 (path not present).
            if "HTTP 404" not in str(exc) and "HTTP 405" not in str(exc):
                raise
    raise last_error or HikvisionError(tr("The device did not accept the password reset file."))


def get_security_questions(address: str, port: int, username: str, password: str) -> list[dict[str, str]]:
    """Return question IDs exposed by a device; Hikvision does not include prompt text."""
    client = HikvisionClient(address, port, username, password)
    try:
        capabilities = ET.fromstring(client.request("GET", "/ISAPI/Security/capabilities"))
        supported = _xml_value(capabilities, "isSupportSecurityQuestionConfig").lower() == "true"
        if not supported:
            raise HikvisionError(tr("This model/firmware does not support security questions over ISAPI."))
        root = ET.fromstring(client.request("GET", "/ISAPI/Security/questionConfiguration"))
    except ET.ParseError as exc:
        raise HikvisionError(tr("The device returned invalid security question data.")) from exc
    result = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].lower() != "question":
            continue
        question_id = _xml_value(node, "id")
        if question_id.isdigit():
            result.append({"id": question_id, "mark": _xml_value(node, "mark").lower()})
    if not result:
        raise HikvisionError(tr("The device did not return the list of available security questions."))
    return result


def set_security_questions(address: str, port: int, username: str, password: str,
                           questions: list[tuple[str, str]]) -> None:
    """Set exactly three recovery answers using the device's advertised question IDs."""
    if len(questions) != 3 or len({question_id for question_id, _ in questions}) != 3:
        raise HikvisionError(tr("Choose three different security questions."))
    if any(not question_id.isdigit() or not answer.strip() for question_id, answer in questions):
        raise HikvisionError(tr("Enter three different questions and an answer for each."))
    client = HikvisionClient(address, port, username, password)
    try:
        capability = ET.fromstring(client.request("GET", "/ISAPI/Security/capabilities"))
    except ET.ParseError as exc:
        raise HikvisionError(tr("Could not read the device security capabilities.")) from exc
    if _xml_value(capability, "isSupportSecurityQuestionConfig").lower() != "true":
        raise HikvisionError(tr("This model/firmware does not support security questions over ISAPI."))
    version = _xml_value(capability, "securityVersion", "1")
    if version not in ("1", "2"):
        raise HikvisionError(tr("The device reports an unsupported ISAPI encryption version: {version}.").format(version=version))
    try:
        iterations = int(_xml_value(capability, "keyIterateNum", "100"))
    except ValueError as exc:
        raise HikvisionError(tr("The device sent wrong encryption parameters.")) from exc
    salt = _xml_value(capability, "salt")
    irreversible = _xml_value(capability, "isIrreversible").lower() == "true"
    key = _make_key(password, iterations, username=username, salt=salt,
                    irreversible=irreversible, key_size=16 if version == "1" else 32)
    iv = secrets.token_bytes(16)
    root = ET.Element("SecurityQuestion", {"version": "2.0", "xmlns": ISAPI_NS})
    listing = ET.SubElement(root, "QuestionList")
    for question_id, answer in questions:
        item = ET.SubElement(listing, "Question")
        ET.SubElement(item, "id").text = question_id
        ET.SubElement(item, "answer").text = _encrypt_sensitive(answer.strip(), key, iv)
        ET.SubElement(item, "mark").text = "true"
    ET.SubElement(root, "password").text = _encrypt_sensitive(password, key, iv)
    client.request("PUT", "/ISAPI/Security/questionConfiguration",
                   params={"security": "1", "iv": iv.hex()},
                   body=ET.tostring(root, encoding="utf-8", xml_declaration=True),
                   content_type="application/xml")


def get_network_interfaces(address: str, port: int, username: str, password: str) -> list[dict[str, str]]:
    client = HikvisionClient(address, port, username, password)
    try:
        root = ET.fromstring(client.request("GET", "/ISAPI/System/Network/interfaces"))
    except ET.ParseError as exc:
        raise HikvisionError(tr("The device returned an invalid list of network interfaces.")) from exc
    result = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].lower() != "networkinterface":
            continue
        def value(parent: ET.Element, name: str, default: str = "") -> str:
            child = next((item for item in parent.iter()
                          if item.tag.rsplit("}", 1)[-1].lower() == name.lower()), None)
            return (child.text or "").strip() if child is not None else default
        ip_node = next((item for item in node.iter()
                        if item.tag.rsplit("}", 1)[-1].lower() == "ipaddress"), None)
        if ip_node is None:
            continue
        # Find IPv4 settings; avoid the nested DefaultGateway.ipAddress node.
        ipv4 = next((item for item in ip_node if item.tag.rsplit("}", 1)[-1].lower() == "ipaddress"), None)
        ipv4_parent = ip_node
        if ipv4 is not None:
            ip_node = ipv4
        gateway_node = next((item for item in ipv4_parent.iter()
                             if item.tag.rsplit("}", 1)[-1].lower() == "defaultgateway"), None)
        primary_dns_node = next((item for item in ipv4_parent.iter()
                                 if item.tag.rsplit("}", 1)[-1].lower() == "primarydns"), None)
        secondary_dns_node = next((item for item in ipv4_parent.iter()
                                   if item.tag.rsplit("}", 1)[-1].lower() == "secondarydns"), None)
        result.append({
            "id": value(node, "id", str(len(result) + 1)),
            "ip": value(ip_node, "ipAddress"),
            "mask": value(ip_node, "subnetMask"),
            "gateway": value(gateway_node, "ipAddress") if gateway_node is not None else "",
            "dns1": value(primary_dns_node, "ipAddress") if primary_dns_node is not None else "",
            "dns2": value(secondary_dns_node, "ipAddress") if secondary_dns_node is not None else "",
            "ipv6": value(ipv4_parent, "ipv6Address"),
            "ipv6_prefix": value(ipv4_parent, "bitMask"),
            "ipv6_gateway": value(gateway_node, "ipv6Address") if gateway_node is not None else "",
            "dhcp": "true" if value(ipv4_parent, "addressingType").lower() == "dynamic" else "false",
        })
    if not result:
        raise HikvisionError(tr("Could not determine the device network interfaces."))
    return result


def get_service_ports(address: str, port: int, username: str, password: str) -> dict[str, str]:
    """Read protocol ports advertised by the device's ISAPI admin-access list."""
    client = HikvisionClient(address, port, username, password)
    try:
        root = ET.fromstring(client.request("GET", "/ISAPI/Security/adminAccesses"))
    except ET.ParseError as exc:
        raise HikvisionError(tr("The device returned an invalid list of network ports.")) from exc
    result: dict[str, str] = {}
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].lower() != "adminaccessprotocol":
            continue
        protocol = next((item.text or "" for item in node
                         if item.tag.rsplit("}", 1)[-1].lower() == "protocol"), "").strip().upper()
        port_number = next((item.text or "" for item in node
                            if item.tag.rsplit("}", 1)[-1].lower() == "portno"), "").strip()
        if protocol and port_number.isdigit():
            result.setdefault(protocol, port_number)
    if not result:
        raise HikvisionError(tr("The device does not announce configurable network ports over ISAPI."))
    return result


def set_service_ports(address: str, port: int, username: str, password: str,
                      ports: dict[str, str]) -> None:
    """Change only requested port numbers, preserving the complete device config."""
    allowed = {"HTTP", "HTTPS", "RTSP", "SDK", "DEV_MANAGE", "SDK_OVER_TLS"}
    normalized: dict[str, int] = {}
    for protocol, value in ports.items():
        name = protocol.strip().upper()
        if name not in allowed:
            raise HikvisionError(tr("Unknown network port type: {protocol}.").format(protocol=protocol))
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise HikvisionError(tr("Enter a numeric port for {name}.").format(name=name)) from exc
        if not 1 <= number <= 65535:
            raise HikvisionError(tr("Port {name} must be 1 to 65535.").format(name=name))
        normalized[name] = number
    if not normalized:
        raise HikvisionError(tr("No ports to change."))

    client = HikvisionClient(address, port, username, password)
    try:
        root = ET.fromstring(client.request("GET", "/ISAPI/Security/adminAccesses"))
    except ET.ParseError as exc:
        raise HikvisionError(tr("The device returned an invalid list of network ports.")) from exc
    found: set[str] = set()
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].lower() != "adminaccessprotocol":
            continue
        protocol_node = next((item for item in node
                              if item.tag.rsplit("}", 1)[-1].lower() == "protocol"), None)
        if protocol_node is None:
            continue
        protocol = (protocol_node.text or "").strip().upper()
        if protocol not in normalized:
            continue
        port_node = next((item for item in node
                          if item.tag.rsplit("}", 1)[-1].lower() == "portno"), None)
        if port_node is None:
            port_node = ET.SubElement(node, "portNo")
        port_node.text = str(normalized[protocol])
        found.add(protocol)
    missing = set(normalized) - found
    if missing:
        raise HikvisionError(tr("The device does not announce these ports:") + " " + ", ".join(sorted(missing)))
    client.request("PUT", "/ISAPI/Security/adminAccesses",
                   body=ET.tostring(root, encoding="utf-8", xml_declaration=True),
                   content_type="application/xml")


def unbind_hik_connect(address: str, port: int, username: str, password: str) -> None:
    """Unbind a device from Hik-Connect where the device advertises support."""
    client = HikvisionClient(address, port, username, password)
    try:
        network_cap = ET.fromstring(client.request("GET", "/ISAPI/System/Network/capabilities"))
    except ET.ParseError as exc:
        raise HikvisionError(tr("Could not read the device network capabilities.")) from exc
    if _xml_value(network_cap, "isSupportEZVIZUnbind").lower() != "true":
        raise HikvisionError(tr("This model/firmware does not support Hik-Connect unbinding over ISAPI."))
    try:
        security_cap = ET.fromstring(client.request(
            "GET", "/ISAPI/Security/capabilities", params={"username": username}))
        cloud_encryption = _xml_value(security_cap, "cloudSensitiveInfoEncryptType")
        if cloud_encryption == "2.1":
            raise HikvisionError(
                tr("The device requires the ISAPI 2.1 cloud encryption scheme; "
                   "it differs from the AES profile the application supports now.")
            )
        version = _xml_value(security_cap, "securityVersion", "1")
        if version not in ("1", "2"):
            raise HikvisionError(tr("The device reports an unsupported ISAPI encryption version: {version}.").format(version=version))
        iterations = int(_xml_value(security_cap, "keyIterateNum", "100"))
    except ET.ParseError as exc:
        raise HikvisionError(tr("Could not read the device encryption settings.")) from exc
    except ValueError as exc:
        raise HikvisionError(tr("The device sent wrong encryption parameters.")) from exc
    salt = _xml_value(security_cap, "salt")
    irreversible = _xml_value(security_cap, "isIrreversible").lower() == "true"
    key = _make_key(password, iterations, username=username, salt=salt,
                    irreversible=irreversible, key_size=16 if version == "1" else 32)
    iv = secrets.token_bytes(16)
    payload = {"EZVIZUnbind": {"password": _encrypt_sensitive(password, key, iv)}}
    response = client.request("PUT", "/ISAPI/System/Network/EZVIZ/unbind",
                              params={"format": "json", "security": version, "iv": iv.hex()},
                              body=json.dumps(payload).encode("utf-8"), content_type="application/json")
    _check_json_status(response)


def set_ipv4(address: str, port: int, username: str, password: str, interface_id: str,
             ip: str, mask: str, gateway: str, dns1: str, dns2: str, dhcp: bool,
             ipv6: str = "", ipv6_prefix: str = "", ipv6_gateway: str = "") -> None:
    if not dhcp:
        try:
            ip_obj = ipaddress.IPv4Address(ip)
            mask_obj = ipaddress.IPv4Address(mask)
            if mask_obj == ipaddress.IPv4Address("0.0.0.0"):
                raise ValueError(tr("The mask cannot be 0.0.0.0"))
            network = ipaddress.IPv4Network(f"{ip_obj}/{mask_obj}", strict=False)
            if gateway:
                gateway_obj = ipaddress.IPv4Address(gateway)
                if gateway_obj not in network:
                    raise ValueError(tr("The gateway must be in the same subnet as the IP"))
            for dns_address in (dns1, dns2):
                if dns_address:
                    ipaddress.IPv4Address(dns_address)
        except ValueError as exc:
            raise HikvisionError(tr("Check the network settings: {error}").format(error=exc)) from exc
    if ipv6:
        try:
            ipaddress.IPv6Address(ipv6)
            prefix = int(ipv6_prefix)
            if not 0 <= prefix <= 128:
                raise ValueError(tr("The IPv6 prefix length must be 0 to 128"))
            if ipv6_gateway:
                ipaddress.IPv6Address(ipv6_gateway)
        except ValueError as exc:
            raise HikvisionError(tr("Check the IPv6 settings: {error}").format(error=exc)) from exc
    client = HikvisionClient(address, port, username, password)
    try:
        root = ET.fromstring(client.request("GET", f"/ISAPI/System/Network/interfaces/{interface_id}/ipAddress"))
    except ET.ParseError as exc:
        raise HikvisionError(tr("The device returned invalid IP interface settings.")) from exc

    def direct(parent: ET.Element, name: str) -> ET.Element | None:
        return next((item for item in parent if item.tag.rsplit("}", 1)[-1].lower() == name.lower()), None)

    def assign(parent: ET.Element, name: str, value: str) -> ET.Element:
        node = direct(parent, name)
        if node is None:
            node = ET.SubElement(parent, name)
        node.text = value
        return node

    root.attrib.setdefault("version", "2.0")
    ip_version = direct(root, "ipVersion")
    if ip_version is None:
        ip_version = ET.Element("ipVersion")
        root.insert(0, ip_version)
    if ipv6:
        ip_version.text = "dual"
    elif not (ip_version.text or "").strip():
        ip_version.text = "v4"
    assign(root, "addressingType", "dynamic" if dhcp else "static")
    if not dhcp:
        assign(root, "ipAddress", ip)
        assign(root, "subnetMask", mask)
        gateway_node = direct(root, "DefaultGateway")
        if gateway_node is None:
            gateway_node = ET.SubElement(root, "DefaultGateway")
        assign(gateway_node, "ipAddress", gateway or "0.0.0.0")
        if dns1:
            dns_node = direct(root, "PrimaryDNS")
            if dns_node is None:
                dns_node = ET.SubElement(root, "PrimaryDNS")
            assign(dns_node, "ipAddress", dns1)
        if dns2:
            dns_node = direct(root, "SecondaryDNS")
            if dns_node is None:
                dns_node = ET.SubElement(root, "SecondaryDNS")
            assign(dns_node, "ipAddress", dns2)
    if ipv6:
        assign(root, "ipv6Address", ipv6)
        assign(root, "bitMask", str(prefix))
        gateway_node = direct(root, "DefaultGateway")
        if gateway_node is None:
            gateway_node = ET.SubElement(root, "DefaultGateway")
        if ipv6_gateway:
            assign(gateway_node, "ipv6Address", ipv6_gateway)
        ipv6_mode = direct(root, "Ipv6Mode")
        if ipv6_mode is None:
            ipv6_mode = ET.SubElement(root, "Ipv6Mode")
        assign(ipv6_mode, "ipV6AddressingType", "manual")
        address_list = direct(ipv6_mode, "ipv6AddressList")
        if address_list is None:
            address_list = ET.SubElement(ipv6_mode, "ipv6AddressList")
        v6_address = direct(address_list, "v6Address")
        if v6_address is None:
            v6_address = ET.SubElement(address_list, "v6Address")
        assign(v6_address, "id", "1")
        assign(v6_address, "type", "manual")
        assign(v6_address, "address", ipv6)
        assign(v6_address, "bitMask", str(prefix))
    payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    client.request("PUT", f"/ISAPI/System/Network/interfaces/{interface_id}/ipAddress",
                   body=payload, content_type="application/xml")
