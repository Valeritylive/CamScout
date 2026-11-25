"""«Device settings» page: Dahua tabs over the HTTP API, Hikvision tabs over ISAPI."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

import dahua_ops  # noqa: E402
from dahua_api import DahuaHttp, DahuaHttpError  # noqa: E402
from dahua_discovery import Device  # noqa: E402
from dahua_protocol import password_problems  # noqa: E402
from devices import HIKVISION  # noqa: E402
from hikvision_api import (  # noqa: E402
    export_guid_file,
    get_network_interfaces,
    get_security_questions,
    get_service_ports,
    set_ipv4,
    set_recovery_email,
    set_security_questions,
    set_service_ports,
    unbind_hik_connect,
)
from i18n import tr as _  # noqa: E402
from ui_common import (  # noqa: E402
    StrengthBar,
    changed_values,
    draw_matrix,
    label,
    password_entry,
    qr_matrix_for,
    qrcode,
    run_in_thread,
    save_file_dialog,
)

CONFIG_SECTIONS = ["General", "Network", "NTP", "Locales", "RTSP", "Encode", "VideoInOptions", "VideoColor",
                   "Email", "DVRIP", "Web", "UPnP", "Multicast", "T2UServer", "MotionDetect", "Snap", "Record",
                   "AlarmServer", "Ftp"]
PORT_NAMES = {"HTTP": "HTTP", "HTTPS": "HTTPS", "RTSP": "RTSP", "SDK": "SDK",
              "DEV_MANAGE": "SDK / Device Management", "SDK_OVER_TLS": "SDK over TLS"}


def _scrolled(content: Gtk.Widget) -> Gtk.ScrolledWindow:
    content.add_css_class("cs-form")
    scroller = Gtk.ScrolledWindow()
    scroller.set_child(content)
    return scroller


def _grid() -> Gtk.Grid:
    grid = Gtk.Grid(row_spacing=8, column_spacing=12)
    grid.set_halign(Gtk.Align.START)
    return grid


def _clear(container: Gtk.Widget) -> None:
    child = container.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        container.remove(child)
        child = following


class SettingsPageMixin:
    """Part of MainWindow: the «Device settings» page."""

    def init_settings_state(self) -> None:
        self.settings_client: DahuaHttp | None = None
        self.settings_device: Device | None = None
        self.config_original: dict[str, str] = {}
        self.intercom_tables: dict[str, dict[str, str]] = {}
        self.intercom_original: dict[str, str] = {}
        self.p2p_matrix: list[list[bool]] | None = None
        self.hik_session: tuple[Device, str, str] | None = None
        self.hik_interfaces: list[dict[str, str]] = []
        self.hik_port_entries: dict[str, Gtk.Entry] = {}
        self.hik_question_ids: list[str] = []

    # ----------------------------------------------------------- dropdowns
    def refresh_device_dropdown(self, dropdown: Gtk.DropDown, keys: list[str]) -> None:
        previous = keys[dropdown.get_selected()] if keys and dropdown.get_selected() < len(keys) else None
        selected_item = self.selection.get_selected_item()
        wanted = previous or (selected_item.key if selected_item is not None else None)
        ordered = sorted(self.devices, key=lambda k: self.numbers.get(k, 0))
        keys[:] = ordered
        model = Gtk.StringList()
        for key in ordered:
            device = self.devices[key]
            model.append(f"[{device.brand}]  {device.ip}  ·  {device.model or device.device_class}  ·  {device.mac}")
        if not ordered:
            model.append(_("No devices — run a search"))
        dropdown.set_model(model)
        if wanted in ordered:
            dropdown.set_selected(ordered.index(wanted))

    def selected_from(self, dropdown: Gtk.DropDown, keys: list[str]) -> Device | None:
        index = dropdown.get_selected()
        if not keys or index >= len(keys):
            return None
        return self.devices.get(keys[index])

    # ---------------------------------------------------------------- page
    def build_settings_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.add_css_class("cs-toolbar")
        top.append(label(_("Device:")))
        self.settings_keys: list[str] = []
        self.settings_combo = Gtk.DropDown.new_from_strings([_("No devices — run a search")])
        self.settings_combo.set_hexpand(True)
        self.settings_combo.connect("notify::selected", lambda *_a: self.on_settings_device_changed())
        top.append(self.settings_combo)
        top.append(label(_("User:")))
        self.settings_user = Gtk.Entry(text="admin")
        self.settings_user.set_width_chars(10)
        top.append(self.settings_user)
        top.append(label(_("Password:")))
        self.settings_password = password_entry()
        self.settings_password.set_hexpand(False)
        self.settings_password.set_size_request(160, -1)
        self.settings_password.connect("activate", lambda *_a: self.settings_login())
        top.append(self.settings_password)
        login = Gtk.Button(label=_("Log in"))
        login.add_css_class("suggested-action")
        login.connect("clicked", lambda *_a: self.settings_login())
        top.append(login)
        page.append(top)
        self.settings_state = label(_("Choose a device and log in to read and change its settings."), css="dim-label")
        self.settings_state.set_margin_start(12)
        self.settings_state.set_margin_bottom(6)
        page.append(self.settings_state)
        page.append(Gtk.Separator())

        self.settings_stack = Gtk.Stack()
        self.settings_stack.set_vexpand(True)
        self.settings_notebook = Gtk.Notebook()
        self.settings_notebook.set_sensitive(False)
        for title, builder in ((_("Info"), self._tab_info), (_("Network"), self._tab_network),
                               (_("Time"), self._tab_time), (_("Users"), self._tab_users),
                               ("P2P", self._tab_p2p), (_("Intercom"), self._tab_intercom),
                               (_("Maintenance"), self._tab_maintenance), (_("Configuration"), self._tab_config)):
            self.settings_notebook.append_page(_scrolled(builder()), Gtk.Label(label=title))
        self.settings_stack.add_named(self.settings_notebook, "dahua")

        self.hik_notebook = Gtk.Notebook()
        self.hik_notebook.set_sensitive(False)
        for title, builder in ((_("Info"), self._hik_tab_info), (_("Network"), self._hik_tab_network),
                               (_("Ports"), self._hik_tab_ports), (_("Recovery"), self._hik_tab_recovery),
                               (_("Maintenance"), self._hik_tab_maintenance)):
            self.hik_notebook.append_page(_scrolled(builder()), Gtk.Label(label=title))
        self.settings_stack.add_named(self.hik_notebook, "hikvision")
        page.append(self.settings_stack)
        return page

    def on_settings_device_changed(self) -> None:
        device = self.selected_from(self.settings_combo, self.settings_keys)
        brand_page = "hikvision" if device is not None and device.brand == HIKVISION else "dahua"
        if self.settings_stack.get_visible_child_name() != brand_page:
            self.settings_stack.set_visible_child_name(brand_page)
        active = (self.settings_device if brand_page == "dahua" else
                  (self.hik_session[0] if self.hik_session else None))
        logged_in = device is not None and active is not None and active.key == device.key
        (self.settings_notebook if brand_page == "dahua" else self.hik_notebook).set_sensitive(logged_in)
        if device is not None and brand_page == "hikvision":
            self.fill_hik_info(device)

    def settings_login(self) -> None:
        device = self.selected_from(self.settings_combo, self.settings_keys)
        if device is None:
            self.settings_state.set_text(_("Choose a device"))
            return
        if device.initialized is False:
            self.settings_state.set_text(_("The device is not activated — set a password first"))
            return
        username = self.settings_user.get_text().strip() or "admin"
        secret = self.settings_password.get_text() or self.password
        if device.brand == HIKVISION:
            self.hik_login(device, username, secret)
            return
        self.settings_stack.set_visible_child_name("dahua")
        client = dahua_ops.http_client(device, username, secret)
        self.settings_state.set_text(_("Connecting to {ip}…").format(ip=device.ip))

        def work():
            info = client.system_info()
            iface, network = client.get_network()
            extras: dict[str, str] = {}
            for name in ("RTSP", "Web", "General", "NTP", "T2UServer"):
                try:
                    extras.update(client.get_config(name))
                except DahuaHttpError:
                    pass
            try:
                current_time = client.get_time()
            except DahuaHttpError:
                current_time = ""
            try:
                users = client.users()
            except DahuaHttpError:
                users = []
            return info, iface, network, extras, current_time, users

        def done(result, error):
            if error:
                self.settings_state.set_text(_("Error: {error}").format(error=error))
                self.settings_notebook.set_sensitive(False)
                return
            self.settings_client, self.settings_device = client, device
            self.remember(username, secret)
            info, iface, network, extras, current_time, users = result
            self.fill_settings(device, info, iface, network, extras, current_time, users)
            self.settings_notebook.set_sensitive(True)
            self.settings_state.set_text(_("Connected: {ip} ({model})").format(
                ip=device.ip, model=info.get("deviceType", device.model)))
        run_in_thread(work, done)

    # ============================================================ Dahua tabs
    def _tab_info(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.info_grid = _grid()
        box.append(self.info_grid)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(label(_("Device name:")))
        self.machine_name = Gtk.Entry()
        self.machine_name.set_width_chars(28)
        row.append(self.machine_name)
        save = Gtk.Button(label=_("Save"))
        save.connect("clicked", lambda *_a: self.settings_apply(
            lambda c: c.set_config({"General.MachineName": self.machine_name.get_text().strip()}),
            _("Device name saved")))
        row.append(save)
        box.append(row)
        return box

    def _tab_network(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        grid = _grid()
        self.net_fields: dict[str, Gtk.Widget] = {}
        self.net_dhcp = Gtk.Switch(halign=Gtk.Align.START)
        rows = [("dhcp", "DHCP", self.net_dhcp)]
        for key, title in (("ip", _("IP address")), ("mask", _("Subnet mask")), ("gateway", _("Gateway")),
                           ("dns1", _("Preferred DNS")), ("dns2", _("Alternate DNS")),
                           ("tcp", _("TCP port")), ("udp", _("UDP port")), ("http", _("HTTP port")),
                           ("https", _("HTTPS port")), ("rtsp", _("RTSP port")), ("mtu", "MTU")):
            entry = Gtk.Entry()
            entry.set_width_chars(20)
            rows.append((key, title, entry))
        self.net_iface = label("", css="dim-label")
        grid.attach(label(_("Interface")), 0, 0, 1, 1)
        grid.attach(self.net_iface, 1, 0, 1, 1)
        for index, (key, title, widget) in enumerate(rows, 1):
            grid.attach(label(title), 0, index, 1, 1)
            grid.attach(widget, 1, index, 1, 1)
            self.net_fields[key] = widget
        self.net_dhcp.connect("notify::active", lambda *_a: [self.net_fields[k].set_sensitive(
            not self.net_dhcp.get_active()) for k in ("ip", "mask", "gateway")])
        box.append(grid)
        box.append(label(_("An empty port field is left unchanged. Changing the IP or ports drops the connection."),
                         css="dim-label", wrap=True))
        apply_button = Gtk.Button(label=_("Apply"))
        apply_button.add_css_class("suggested-action")
        apply_button.set_halign(Gtk.Align.START)
        apply_button.connect("clicked", lambda *_a: self.apply_network_tab())
        box.append(apply_button)
        return box

    def _tab_time(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        grid = _grid()
        self.device_time = label("—", selectable=True)
        self.pc_time = label(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.pc_time_timer = GLib.timeout_add_seconds(
            1, lambda: (self.pc_time.set_text(datetime.now().strftime("%Y-%m-%d %H:%M:%S")), True)[1])
        self.connect("destroy", lambda *_a: GLib.source_remove(self.pc_time_timer))
        grid.attach(label(_("Device time")), 0, 0, 1, 1)
        grid.attach(self.device_time, 1, 0, 1, 1)
        grid.attach(label(_("Computer time")), 0, 1, 1, 1)
        grid.attach(self.pc_time, 1, 1, 1, 1)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        read = Gtk.Button(label=_("Read"))
        read.connect("clicked", lambda *_a: self.settings_apply(
            lambda c: c.get_time(), None, lambda value: self.device_time.set_text(value or "—")))
        buttons.append(read)
        sync = Gtk.Button(label=_("Sync with PC"))
        sync.add_css_class("suggested-action")
        sync.connect("clicked", lambda *_a: self.settings_apply(
            lambda c: (c.set_time(), c.get_time())[1], _("Time synced with the computer"),
            lambda value: self.device_time.set_text(value or "—")))
        buttons.append(sync)
        grid.attach(buttons, 1, 2, 1, 1)
        box.append(grid)
        box.append(label("NTP", css="cs-section"))
        ntp = _grid()
        self.ntp_enable = Gtk.Switch(halign=Gtk.Align.START)
        self.ntp_server = Gtk.Entry(text="pool.ntp.org")
        self.ntp_port = Gtk.Entry(text="123")
        self.ntp_period = Gtk.Entry(text="60")
        self.ntp_timezone = Gtk.Entry()
        self.ntp_timezone.set_tooltip_text(_("The NTP.TimeZone value of the device (Dahua time zone number)"))
        for index, (title, widget) in enumerate(((_("Enable"), self.ntp_enable), (_("Server"), self.ntp_server),
                                                 (_("Port"), self.ntp_port), (_("Period, min"), self.ntp_period),
                                                 (_("Time zone (code)"), self.ntp_timezone))):
            ntp.attach(label(title), 0, index, 1, 1)
            ntp.attach(widget, 1, index, 1, 1)
        box.append(ntp)
        apply_button = Gtk.Button(label=_("Apply NTP"))
        apply_button.set_halign(Gtk.Align.START)
        apply_button.connect("clicked", lambda *_a: self.apply_ntp())
        box.append(apply_button)
        return box

    def _tab_users(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.users_view = Gtk.TextView(editable=False, monospace=True)
        self.users_view.set_size_request(-1, 140)
        box.append(self.users_view)
        box.append(label(_("Change password"), css="cs-section"))
        grid = _grid()
        self.pw_user = Gtk.Entry(text="admin")
        self.pw_old = password_entry()
        self.pw_new = password_entry()
        self.pw_confirm = password_entry()
        for index, (title, widget) in enumerate(((_("User"), self.pw_user), (_("Old password"), self.pw_old),
                                                 (_("New password"), self.pw_new), (_("Confirm"), self.pw_confirm),
                                                 (_("Strength"), StrengthBar(self.pw_new)))):
            grid.attach(label(title), 0, index, 1, 1)
            widget.set_size_request(260, -1)
            grid.attach(widget, 1, index, 1, 1)
        box.append(grid)
        change = Gtk.Button(label=_("Change password"))
        change.add_css_class("suggested-action")
        change.set_halign(Gtk.Align.START)
        change.connect("clicked", lambda *_a: self.change_password())
        box.append(change)
        return box

    def _tab_p2p(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.append(label(_("P2P (Easy4ip): access the device from the DMSS app and the Dahua cloud without port "
                           "forwarding. Turn P2P on and scan the serial number QR code in DMSS."),
                         css="dim-label", wrap=True))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.append(label(_("Enable P2P")))
        self.p2p_enable = Gtk.Switch(valign=Gtk.Align.CENTER)
        row.append(self.p2p_enable)
        save = Gtk.Button(label=_("Save"))
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda *_a: self.apply_p2p())
        row.append(save)
        box.append(row)
        self.p2p_details = label("", css="dim-label", selectable=True, wrap=True)
        box.append(self.p2p_details)
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        self.p2p_qr = Gtk.DrawingArea()
        self.p2p_qr.set_size_request(200, 200)
        self.p2p_qr.add_css_class("cs-qr")
        self.p2p_qr.set_draw_func(lambda _a, cr, w, h: draw_matrix(cr, w, h, self.p2p_matrix, "SN"))
        content.append(self.p2p_qr)
        self.p2p_serial = label("", selectable=True)
        self.p2p_serial.set_valign(Gtk.Align.CENTER)
        content.append(self.p2p_serial)
        box.append(content)
        return box

    def fill_p2p(self, serial: str, table: dict[str, str]) -> None:
        self.p2p_enable.set_active(table.get("T2UServer.Enable", "false").lower() == "true")
        details = [f"{k[len('T2UServer.'):]}: {v}" for k, v in table.items()
                   if k != "T2UServer.Enable" and k.startswith("T2UServer.")]
        if not table:
            details = [_("The device returned no P2P settings (T2UServer section) — it may not support them.")]
        self.p2p_details.set_text("\n".join(details))
        self.p2p_matrix = qr_matrix_for(serial)
        missing = "" if qrcode is not None or not serial else "\n" + _("(the qrcode module is not installed)")
        self.p2p_serial.set_text(_("Serial number:") + f"\n{serial or '—'}" + missing)
        self.p2p_qr.queue_draw()

    def apply_p2p(self) -> None:
        enable = self.p2p_enable.get_active()

        def action(client: DahuaHttp):
            client.set_p2p(enable)
            return client.get_p2p()
        device = self.settings_device
        self.settings_apply(action, _("P2P on") if enable else _("P2P off"),
                            lambda table: self.fill_p2p(device.serial if device else "", table))

    def _tab_intercom(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(label(_("Intercom settings (VTO door stations, VTH monitors): building, unit and apartment "
                           "numbers, SIP server, lock. The sections depend on model and firmware, so the program "
                           "checks the known sections and shows the ones the device has."),
                         css="dim-label", wrap=True))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        find = Gtk.Button(label=_("Find sections"))
        find.connect("clicked", lambda *_a: self.find_intercom_sections())
        row.append(find)
        row.append(label(_("Section:")))
        self.intercom_names: list[str] = []
        self.intercom_combo = Gtk.DropDown.new_from_strings(["—"])
        self.intercom_combo.connect("notify::selected", lambda *_a: self.show_intercom_section())
        row.append(self.intercom_combo)
        apply_button = Gtk.Button(label=_("Apply changes"))
        apply_button.add_css_class("suggested-action")
        apply_button.connect("clicked", lambda *_a: self.apply_intercom())
        row.append(apply_button)
        box.append(row)
        self.intercom_view = Gtk.TextView(monospace=True)
        scroller = Gtk.ScrolledWindow()
        scroller.set_min_content_height(360)
        scroller.set_vexpand(True)
        scroller.set_child(self.intercom_view)
        box.append(scroller)
        return box

    def find_intercom_sections(self) -> None:
        def show(found: dict[str, dict[str, str]]):
            self.intercom_tables = found
            self.intercom_names = list(found)
            self.intercom_combo.set_model(Gtk.StringList.new(self.intercom_names or ["—"]))
            self.intercom_combo.set_selected(0)
            self.show_intercom_section()
            if not found:
                self.settings_state.set_text(_("No intercom sections found — probably not a VTO/VTH"))
        self.settings_apply(lambda c: c.existing_configs(dahua_ops.INTERCOM_SECTIONS),
                            _("Intercom section search finished"), show)

    def show_intercom_section(self) -> None:
        index = self.intercom_combo.get_selected()
        name = self.intercom_names[index] if 0 <= index < len(self.intercom_names) else ""
        table = self.intercom_tables.get(name, {})
        self.intercom_original = dict(table)
        self.intercom_view.get_buffer().set_text("\n".join(f"{k}={v}" for k, v in table.items()))

    def apply_intercom(self) -> None:
        buffer = self.intercom_view.get_buffer()
        changed = changed_values(buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False),
                                 self.intercom_original)
        if not changed:
            self.settings_state.set_text(_("No changes"))
            return

        def action(client: DahuaHttp):
            client.set_config(changed)
            self.intercom_original.update(changed)
            for table in self.intercom_tables.values():
                table.update({k: v for k, v in changed.items() if k in table})
        self.settings_apply(action, _("Parameters changed: {count}").format(count=len(changed)))

    def _maintenance_row(self, box: Gtk.Box, title: str, text: str, callback: Callable[[], None]) -> None:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        btn = Gtk.Button(label=title)
        btn.set_size_request(250, -1)
        btn.connect("clicked", lambda *_a: callback())
        row.append(btn)
        row.append(label(text, css="dim-label", wrap=True))
        box.append(row)

    def _tab_maintenance(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        action = self._maintenance_row
        action(box, _("Reboot"), _("Restart the device, about a minute offline."),
               lambda: self.confirm(_("Reboot"), _("Reboot the device?"), _("Reboot"),
                                    lambda: self.settings_apply(lambda c: c.reboot(), _("The device is rebooting"))))
        action(box, _("Reset (keep network)"), _("Restore factory settings except the network ones."),
               lambda: self.confirm(_("Factory reset"), _("Reset all settings except the network ones?"), _("Reset"),
                                    lambda: self.settings_apply(lambda c: c.factory_reset(True),
                                                                _("Settings reset (network kept)"))))
        action(box, _("Full reset"), _("Complete factory settings: the IP becomes 192.168.1.108 and the device "
                                       "has to be initialized again."),
               lambda: self.confirm(_("Full reset"), _("Reset the device completely? The IP and password are reset."),
                                    _("Reset"), lambda: self.settings_apply(lambda c: c.factory_reset(False),
                                                                             _("The device was reset"))))
        action(box, _("Open web interface"), _("Open the device page in the browser."),
               lambda: self.settings_device and self.open_web(self.settings_device))
        action(box, _("Live view…"), _("Watch the camera stream over RTSP."),
               lambda: self.settings_device and self.open_live_view(self.settings_device))
        action(box, _("Export log…"), _("Save the device log of the last 7 days to CSV."), self.export_device_log)
        return box

    def _tab_config(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(label(_("Section:")))
        self.config_names = list(CONFIG_SECTIONS)
        self.config_name = Gtk.DropDown.new_from_strings(self.config_names)
        row.append(self.config_name)
        self.config_custom = Gtk.Entry()
        self.config_custom.set_placeholder_text(_("or your own section name"))
        row.append(self.config_custom)
        read = Gtk.Button(label=_("Read"))
        read.connect("clicked", lambda *_a: self.read_config())
        row.append(read)
        apply_button = Gtk.Button(label=_("Apply changes"))
        apply_button.add_css_class("suggested-action")
        apply_button.connect("clicked", lambda *_a: self.apply_config())
        row.append(apply_button)
        box.append(row)
        box.append(label(_("Parameters as «name=value». Only changed lines are sent."), css="dim-label"))
        self.config_view = Gtk.TextView(monospace=True)
        self.config_view.set_vexpand(True)
        scroller = Gtk.ScrolledWindow()
        scroller.set_min_content_height(380)
        scroller.set_vexpand(True)
        scroller.set_child(self.config_view)
        box.append(scroller)
        return box

    def fill_settings(self, device: Device, info: dict, iface: str, network: dict, extras: dict,
                      current_time: str, users: list[dict]) -> None:
        _clear(self.info_grid)
        rows = [(_("Model"), info.get("deviceType") or device.model),
                (_("Serial number"), info.get("serialNumber") or device.serial),
                (_("Software version"), info.get("SoftwareVersion") or device.firmware),
                (_("Hardware version"), info.get("hardwareVersion") or info.get("HardwareVersion", "")),
                (_("Type"), info.get("DeviceType") or device.device_class),
                (_("Vendor"), info.get("Vendor") or device.vendor),
                (_("Processor"), info.get("processor", "")),
                ("MAC", device.mac), ("IP", device.ip)]
        for index, (name, value) in enumerate(r for r in rows if r[1]):
            self.info_grid.attach(label(name, css="dim-label"), 0, index, 1, 1)
            self.info_grid.attach(label(str(value), selectable=True), 1, index, 1, 1)
        self.machine_name.set_text(extras.get("General.MachineName") or info.get("MachineName", ""))
        prefix = f"Network.{iface}."
        self.net_iface.set_text(iface)
        self.net_dhcp.set_active(network.get(prefix + "DhcpEnable", "false").lower() == "true")
        values = {
            "ip": network.get(prefix + "IPAddress", ""), "mask": network.get(prefix + "SubnetMask", ""),
            "gateway": network.get(prefix + "DefaultGateway", ""),
            "dns1": network.get(prefix + "DnsServers[0]", ""), "dns2": network.get(prefix + "DnsServers[1]", ""),
            "tcp": network.get("Network.TCPPort", ""), "udp": network.get("Network.UDPPort", ""),
            "http": network.get("Network.HttpPort", "") or extras.get("Web.Port", ""),
            "https": network.get("Network.HttpsPort", "") or extras.get("Web.SSLPort", ""),
            "rtsp": extras.get("RTSP.Port", ""), "mtu": network.get(prefix + "MTU", ""),
        }
        self.net_loaded = (iface, dict(values))
        for key, value in values.items():
            self.net_fields[key].set_text(value)
        self.device_time.set_text(current_time or "—")
        self.ntp_enable.set_active(extras.get("NTP.Enable", "false").lower() == "true")
        self.ntp_server.set_text(extras.get("NTP.Address", "pool.ntp.org"))
        self.ntp_port.set_text(extras.get("NTP.Port", "123"))
        self.ntp_period.set_text(extras.get("NTP.UpdatePeriod", "60"))
        self.ntp_timezone.set_text(extras.get("NTP.TimeZone", ""))
        lines = [f"{_('Name'):16} {_('Group'):10} {_('Description')}"]
        lines += [f"{u.get('Name', ''):16} {u.get('Group', ''):10} {u.get('Memo', '')}" for u in users]
        self.users_view.get_buffer().set_text("\n".join(lines) if users else _("The user list is not available"))
        self.fill_p2p(info.get("serialNumber") or device.serial,
                      {k: v for k, v in extras.items() if k.startswith("T2UServer.")})
        self.intercom_tables, self.intercom_names = {}, []
        self.intercom_combo.set_model(Gtk.StringList.new(["—"]))
        self.intercom_view.get_buffer().set_text("")

    def settings_apply(self, action: Callable[[DahuaHttp], Any], success: str | None,
                       on_value: Callable[[Any], None] | None = None) -> None:
        client = self.settings_client
        if client is None:
            self.settings_state.set_text(_("Log in to the device first"))
            return
        self.settings_state.set_text(_("Working…"))

        def done(value, error):
            if error:
                self.settings_state.set_text(_("Error: {error}").format(error=error))
                self.log(error, error=True)
                return
            if on_value:
                on_value(value)
            self.settings_state.set_text(success or _("Done"))
            if success:
                self.log(f"{client.host}: {success}")
        run_in_thread(lambda: action(client), done)

    def apply_network_tab(self) -> None:
        if self.settings_client is None:
            return
        iface, loaded = getattr(self, "net_loaded", ("eth0", {}))
        current = {key: widget.get_text().strip() for key, widget in self.net_fields.items()
                   if isinstance(widget, Gtk.Entry)}
        dhcp = self.net_dhcp.get_active()
        settings = dahua_ops.NetworkSettings(ip=current["ip"], mask=current["mask"], gateway=current["gateway"],
                                             dhcp=dhcp, dns1=current["dns1"], dns2=current["dns2"])
        try:
            settings.validate()
        except dahua_ops.OperationError as exc:
            self.settings_state.set_text(str(exc))
            return
        values: dict[str, Any] = {}
        port_keys = {"tcp": "Network.TCPPort", "udp": "Network.UDPPort", "http": "Network.HttpPort",
                     "https": "Network.HttpsPort", "rtsp": "RTSP.Port", "mtu": f"Network.{iface}.MTU"}
        for key, config_key in port_keys.items():
            if current[key] and current[key] != loaded.get(key, ""):
                if not current[key].isdigit():
                    self.settings_state.set_text(_("Invalid value: {value}").format(value=current[key]))
                    return
                values[config_key] = current[key]

        def action(client: DahuaHttp):
            if values:
                client.set_config(values)
            client.set_network(settings.ip, settings.mask, settings.gateway, dhcp,
                               (settings.dns1, settings.dns2), iface)
        self.settings_apply(action, _("Network settings sent; run a new search after an IP change"))

    def apply_ntp(self) -> None:
        try:
            port, period = int(self.ntp_port.get_text() or "123"), int(self.ntp_period.get_text() or "60")
        except ValueError:
            self.settings_state.set_text(_("The port and period must be numbers"))
            return
        timezone = self.ntp_timezone.get_text().strip() or None
        self.settings_apply(lambda c: c.set_ntp(self.ntp_enable.get_active(), self.ntp_server.get_text().strip(),
                                                port, period, timezone), _("NTP settings saved"))

    def change_password(self) -> None:
        new = self.pw_new.get_text()
        if new != self.pw_confirm.get_text():
            self.settings_state.set_text(_("Passwords do not match"))
            return
        problems = password_problems(new)
        if problems:
            self.settings_state.set_text(_("Unsuitable password:") + " " + "; ".join(problems))
            return
        user, old = self.pw_user.get_text().strip() or "admin", self.pw_old.get_text()

        def action(client: DahuaHttp):
            client.change_password(user, old, new)
            if user == client.username:
                client.password = new
                client.auth = type(client.auth)(user, new)
                self.password = new
        self.settings_apply(action, _("Password of user {user} changed").format(user=user))

    def export_device_log(self) -> None:
        if self.settings_client is None:
            self.settings_state.set_text(_("Log in to the device first"))
            return

        def save(path: str) -> None:
            end = datetime.now()

            def action(client: DahuaHttp):
                records = client.logs(end - timedelta(days=7), end)
                keys = sorted({key for record in records for key in record})
                with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=keys or ["Time"])
                    writer.writeheader()
                    writer.writerows(records)
                return len(records)
            self.settings_apply(action, None, lambda count: self.settings_state.set_text(
                _("Log records saved: {count} → {path}").format(count=count, path=path)))
        save_file_dialog(self, _("Export log"), f"dahua-log-{self.settings_client.host}.csv", save)

    def config_section(self) -> str:
        return self.config_custom.get_text().strip() or self.config_names[self.config_name.get_selected()]

    def read_config(self) -> None:
        name = self.config_section()

        def show(table):
            self.config_original = dict(table)
            self.config_view.get_buffer().set_text("\n".join(f"{k}={v}" for k, v in table.items()))
        self.settings_apply(lambda c: c.get_config(name), _("Section {name} read").format(name=name), show)

    def apply_config(self) -> None:
        buffer = self.config_view.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        changed = changed_values(text, self.config_original)
        if not changed:
            self.settings_state.set_text(_("No changes"))
            return

        def action(client: DahuaHttp):
            client.set_config(changed)
            self.config_original.update(changed)
        self.settings_apply(action, _("Parameters changed: {count}").format(count=len(changed)))

    # ======================================================== Hikvision tabs
    def hik_login(self, device: Device, username: str, secret: str) -> None:
        self.settings_stack.set_visible_child_name("hikvision")
        self.fill_hik_info(device)
        if not secret:
            self.settings_state.set_text(_("Enter the administrator password"))
            return
        self.settings_state.set_text(_("Connecting to {ip}…").format(ip=device.ip))
        port = device.http_port or 80

        def work():
            interfaces = get_network_interfaces(device.ip, port, username, secret)
            try:
                ports = get_service_ports(device.ip, port, username, secret)
            except Exception:  # noqa: BLE001 - optional capability
                ports = {}
            return interfaces, ports

        def done(result, error):
            if error:
                self.settings_state.set_text(_("Error: {error}").format(error=error))
                self.hik_notebook.set_sensitive(False)
                return
            self.hik_session = (device, username, secret)
            self.remember(username, secret)
            interfaces, ports = result
            self.fill_hik_network(interfaces)
            self.fill_hik_ports(ports)
            self.hik_notebook.set_sensitive(True)
            self.settings_state.set_text(_("Connected: {ip} ({model})").format(ip=device.ip, model=device.model))
        run_in_thread(work, done)

    def hik_apply(self, action: Callable[[Device, str, str], Any], success: str | None,
                  on_value: Callable[[Any], None] | None = None) -> None:
        if self.hik_session is None:
            self.settings_state.set_text(_("Log in to the device first"))
            return
        device, username, secret = self.hik_session
        self.settings_state.set_text(_("Working…"))

        def done(value, error):
            if error:
                self.settings_state.set_text(_("Error: {error}").format(error=error))
                self.log(f"{device.ip}: {error}", error=True)
                return
            if on_value:
                on_value(value)
            self.settings_state.set_text(success or _("Done"))
            if success:
                self.log(f"{device.ip}: {success}")
        run_in_thread(lambda: action(device, username, secret), done)

    def _hik_tab_info(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.append(label(_("Data from the SADP discovery reply."), css="dim-label"))
        self.hik_info_grid = _grid()
        box.append(self.hik_info_grid)
        return box

    def fill_hik_info(self, device: Device) -> None:
        _clear(self.hik_info_grid)
        raw = device.raw or {}
        rows = [(_("Model"), device.model), (_("Type"), device.device_class),
                (_("Serial number"), device.serial), (_("Software version"), device.firmware),
                (_("DSP version"), raw.get("dspversion", "")), (_("Boot time"), raw.get("boottime", "")),
                ("MAC", device.mac), ("IPv4", device.ip), (_("Subnet mask"), device.subnet_mask),
                (_("IPv4 gateway"), device.gateway), ("IPv6", device.ipv6),
                (_("SDK port"), str(device.tcp_port or "")), (_("HTTP port"), str(device.http_port or "")),
                (_("RTSP port"), str(device.rtsp_port or "")),
                (_("Enhanced SDK port"), str(device.enhanced_sdk_port or "")),
                (_("Channels"), f"{device.video_inputs}+{device.remote_inputs}" if device.remote_inputs
                 else str(device.video_inputs or ""))]
        for index, (name, value) in enumerate(r for r in rows if r[1]):
            self.hik_info_grid.attach(label(name, css="dim-label"), 0, index, 1, 1)
            self.hik_info_grid.attach(label(str(value), selectable=True), 1, index, 1, 1)

    def _hik_tab_network(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        grid = _grid()
        self.hik_iface_combo = Gtk.DropDown.new_from_strings(["—"])
        self.hik_iface_combo.connect("notify::selected", lambda *_a: self.load_hik_interface())
        grid.attach(label(_("Interface")), 0, 0, 1, 1)
        grid.attach(self.hik_iface_combo, 1, 0, 1, 1)
        self.hik_dhcp = Gtk.Switch(halign=Gtk.Align.START)
        grid.attach(label("DHCP"), 0, 1, 1, 1)
        grid.attach(self.hik_dhcp, 1, 1, 1, 1)
        self.hik_net: dict[str, Gtk.Entry] = {}
        for index, (key, title) in enumerate((("ip", _("IPv4 address")), ("mask", _("Subnet mask")),
                                              ("gateway", _("IPv4 gateway")), ("dns1", _("Preferred DNS")),
                                              ("dns2", _("Alternate DNS")), ("ipv6", _("IPv6 address (optional)")),
                                              ("ipv6_prefix", _("IPv6 prefix length")),
                                              ("ipv6_gateway", _("IPv6 gateway"))), 2):
            entry = Gtk.Entry()
            entry.set_width_chars(22)
            grid.attach(label(title), 0, index, 1, 1)
            grid.attach(entry, 1, index, 1, 1)
            self.hik_net[key] = entry
        self.hik_dhcp.connect("notify::active", lambda *_a: [self.hik_net[k].set_sensitive(
            not self.hik_dhcp.get_active()) for k in ("ip", "mask", "gateway", "dns1", "dns2")])
        box.append(grid)
        box.append(label(_("After applying, the current connection drops. Find the device again."),
                         css="dim-label", wrap=True))
        apply_button = Gtk.Button(label=_("Apply"))
        apply_button.add_css_class("suggested-action")
        apply_button.set_halign(Gtk.Align.START)
        apply_button.connect("clicked", lambda *_a: self.apply_hik_network())
        box.append(apply_button)
        return box

    def fill_hik_network(self, interfaces: list[dict[str, str]]) -> None:
        self.hik_interfaces = interfaces
        names = [_("Interface {id}").format(id=item.get("id", "")) + f"  ·  {item.get('ip') or '—'}"
                 for item in interfaces] or ["—"]
        self.hik_iface_combo.set_model(Gtk.StringList.new(names))
        self.hik_iface_combo.set_selected(0)
        self.load_hik_interface()

    def load_hik_interface(self) -> None:
        if not self.hik_interfaces:
            return
        current = self.hik_interfaces[min(self.hik_iface_combo.get_selected(), len(self.hik_interfaces) - 1)]
        for key, entry in self.hik_net.items():
            entry.set_text(current.get(key, ""))
        self.hik_dhcp.set_active(current.get("dhcp") == "true")

    def apply_hik_network(self) -> None:
        if not self.hik_interfaces:
            return
        selected = self.hik_interfaces[min(self.hik_iface_combo.get_selected(), len(self.hik_interfaces) - 1)]
        values = {key: entry.get_text().strip() for key, entry in self.hik_net.items()}
        dhcp = self.hik_dhcp.get_active()
        if not dhcp and (not values["ip"] or not values["mask"]):
            self.settings_state.set_text(_("For a static address enter the IPv4 address and subnet mask."))
            return

        def action(device: Device, username: str, secret: str):
            set_ipv4(device.ip, device.http_port or 80, username, secret, selected["id"], values["ip"],
                     values["mask"], values["gateway"], values["dns1"], values["dns2"], dhcp,
                     values["ipv6"], values["ipv6_prefix"], values["ipv6_gateway"])
            self.forget_device(device)
            GLib.idle_add(lambda: (self.start_scan(), False)[1])
        self.hik_apply(action, _("Network settings sent; run a new search after an IP change"))

    def _hik_tab_ports(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.append(label(_("Ports from the device capability list. Changing the HTTP port drops the current "
                           "connection; connect on the new port afterwards."), css="dim-label", wrap=True))
        self.hik_ports_grid = _grid()
        box.append(self.hik_ports_grid)
        apply_button = Gtk.Button(label=_("Apply"))
        apply_button.add_css_class("suggested-action")
        apply_button.set_halign(Gtk.Align.START)
        apply_button.connect("clicked", lambda *_a: self.apply_hik_ports())
        box.append(apply_button)
        return box

    def fill_hik_ports(self, ports: dict[str, str]) -> None:
        _clear(self.hik_ports_grid)
        self.hik_port_entries = {}
        if not ports:
            self.hik_ports_grid.attach(label(_("The device does not announce configurable ports over ISAPI."),
                                             css="dim-label"), 0, 0, 2, 1)
            return
        for row, (protocol, value) in enumerate(ports.items()):
            self.hik_ports_grid.attach(label(PORT_NAMES.get(protocol, protocol)), 0, row, 1, 1)
            entry = Gtk.Entry(text=value)
            entry.set_input_purpose(Gtk.InputPurpose.DIGITS)
            entry.set_width_chars(10)
            self.hik_ports_grid.attach(entry, 1, row, 1, 1)
            self.hik_port_entries[protocol] = entry

    def apply_hik_ports(self) -> None:
        new_ports = {name: entry.get_text().strip() for name, entry in self.hik_port_entries.items()}
        if not new_ports:
            return
        if any(not value for value in new_ports.values()):
            self.settings_state.set_text(_("Enter a port for every protocol shown."))
            return
        self.hik_apply(lambda d, u, p: set_service_ports(d.ip, d.http_port or 80, u, p, new_ports),
                       _("Ports updated. If the HTTP port changed, find the device again."))

    def _hik_tab_recovery(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.append(label(_("Recovery e-mail"), css="cs-section"))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.hik_email = Gtk.Entry()
        self.hik_email.set_placeholder_text("name@example.com")
        self.hik_email.set_width_chars(30)
        row.append(self.hik_email)
        save = Gtk.Button(label=_("Save"))
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda *_a: self.apply_hik_email())
        row.append(save)
        box.append(row)
        box.append(label(_("The device may ask to confirm the address with a QR code, depending on its firmware."),
                         css="dim-label", wrap=True))
        box.append(label(_("Security questions"), css="cs-section"))
        box.append(label(_("ISAPI returns question numbers, not their wording. Check the number against the device's "
                           "own interface."), css="dim-label", wrap=True))
        load = Gtk.Button(label=_("Load questions"))
        load.set_halign(Gtk.Align.START)
        load.connect("clicked", lambda *_a: self.load_hik_questions())
        box.append(load)
        grid = _grid()
        self.hik_questions: list[tuple[Gtk.DropDown, Gtk.PasswordEntry]] = []
        for index in range(3):
            dropdown = Gtk.DropDown.new_from_strings(["—"])
            answer = password_entry()
            answer.set_property("placeholder-text", _("Answer"))
            answer.set_size_request(240, -1)
            grid.attach(label(_("Question {number}").format(number=index + 1)), 0, index, 1, 1)
            grid.attach(dropdown, 1, index, 1, 1)
            grid.attach(answer, 2, index, 1, 1)
            self.hik_questions.append((dropdown, answer))
        box.append(grid)
        save_questions = Gtk.Button(label=_("Save questions"))
        save_questions.add_css_class("suggested-action")
        save_questions.set_halign(Gtk.Align.START)
        save_questions.connect("clicked", lambda *_a: self.apply_hik_questions())
        box.append(save_questions)
        return box

    def apply_hik_email(self) -> None:
        email = self.hik_email.get_text().strip()
        if "@" not in email or email.startswith("@") or email.endswith("@"):
            self.settings_state.set_text(_("Enter a valid recovery e-mail."))
            return
        self.hik_apply(lambda d, u, p: set_recovery_email(d.ip, d.http_port or 80, u, p, email),
                       _("E-mail saved. The device may ask to confirm it with a QR code."))

    def load_hik_questions(self) -> None:
        def show(available: list[dict[str, str]]):
            ids = [item["id"] for item in available]
            self.hik_question_ids = ids
            if len(ids) < 3:
                self.settings_state.set_text(_("The device returned fewer than three questions."))
                return
            preselected = [item["id"] for item in available if item.get("mark") == "true"]
            names = [_("Question No. {id}").format(id=qid) for qid in ids]
            for index, (dropdown, _answer) in enumerate(self.hik_questions):
                dropdown.set_model(Gtk.StringList.new(names))
                wanted = preselected[index] if index < len(preselected) and preselected[index] in ids else ids[index]
                dropdown.set_selected(ids.index(wanted))
        self.hik_apply(lambda d, u, p: get_security_questions(d.ip, d.http_port or 80, u, p),
                       _("Questions loaded"), show)

    def apply_hik_questions(self) -> None:
        ids = self.hik_question_ids
        if len(ids) < 3:
            self.settings_state.set_text(_("Load the questions first"))
            return
        chosen = [(ids[dropdown.get_selected()], answer.get_text().strip()) for dropdown, answer in self.hik_questions]
        if len({qid for qid, _a in chosen}) != 3:
            self.settings_state.set_text(_("Choose three different security questions."))
            return
        if any(not answer for _q, answer in chosen):
            self.settings_state.set_text(_("Enter an answer for every question."))
            return
        self.hik_apply(lambda d, u, p: set_security_questions(d.ip, d.http_port or 80, u, p, chosen),
                       _("Security questions saved on the device."))

    def _hik_tab_maintenance(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        action = self._maintenance_row
        action(box, _("Open web interface"), _("Open the device page in the browser."),
               lambda: self.hik_session and self.open_web(self.hik_session[0]))
        action(box, _("Live view…"), _("Watch the camera stream over RTSP."),
               lambda: self.hik_session and self.open_live_view(self.hik_session[0]))
        action(box, _("Export GUID file…"), _("A recovery file that can help reset the password later. Keep it in a "
                                              "safe place."), self.export_hik_guid)
        action(box, _("Unbind Hik-Connect…"), _("Remove the device from its Hik-Connect account. The device and the "
                                                "computer must be in the same local network."), self.unbind_hik)
        action(box, _("Password reset…"), _("Two-step reset with a request file and a response file from support."),
               lambda: self.hik_session and self.open_reset_for(self.hik_session[0]))
        return box

    def export_hik_guid(self) -> None:
        if self.hik_session is None:
            self.settings_state.set_text(_("Log in to the device first"))
            return
        device = self.hik_session[0]

        def save(data: bytes):
            safe = "".join(ch for ch in device.serial if ch.isalnum() or ch in "-_") or device.ip

            def write(path: str):
                try:
                    with open(path, "wb") as handle:
                        handle.write(data)
                except OSError as exc:
                    self.settings_state.set_text(_("Could not save: {error}").format(error=exc))
                    return
                self.show_result(_("GUID file saved. Keep it in a safe place."))
            save_file_dialog(self, _("Save GUID recovery file"), f"{safe}-recovery.guid", write)
        self.hik_apply(lambda d, u, p: export_guid_file(d.ip, d.http_port or 80, u, p), None, save)

    def unbind_hik(self) -> None:
        if self.hik_session is None:
            self.settings_state.set_text(_("Log in to the device first"))
            return
        self.confirm(_("Unbind Hik-Connect"),
                     _("Unbind the device from its Hik-Connect account? The current account may lose access to the "
                       "device."), _("Unbind"),
                     lambda: self.hik_apply(lambda d, u, p: unbind_hik_connect(d.ip, d.http_port or 80, u, p),
                                            _("The device accepted the Hik-Connect unbind request.")))
