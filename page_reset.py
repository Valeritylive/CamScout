"""«Password reset» page: Dahua QR code flow and Hikvision request/response files."""

from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

import dahua_protocol  # noqa: E402
from dahua_discovery import Device  # noqa: E402
from devices import HIKVISION  # noqa: E402
from hikvision_api import export_reset_request, reset_password_with_response  # noqa: E402
from i18n import tr as _  # noqa: E402
from ui_common import (  # noqa: E402
    StrengthBar,
    draw_matrix,
    label,
    open_file_dialog,
    password_entry,
    password_problems_for,
    qr_matrix_for,
    qrcode,
    run_in_thread,
    save_file_dialog,
)


def _grid() -> Gtk.Grid:
    grid = Gtk.Grid(row_spacing=8, column_spacing=12)
    grid.set_halign(Gtk.Align.START)
    return grid


class ResetPageMixin:
    """Part of MainWindow: the «Password reset» page."""

    def build_reset_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.add_css_class("cs-toolbar")
        top.append(label(_("Device:")))
        self.reset_keys: list[str] = []
        self.reset_combo = Gtk.DropDown.new_from_strings([_("No devices — run a search")])
        self.reset_combo.set_hexpand(True)
        self.reset_combo.connect("notify::selected", lambda *_a: self.update_reset_info())
        top.append(self.reset_combo)
        page.append(top)
        page.append(Gtk.Separator())
        self.reset_info = label("", css="dim-label", wrap=True, selectable=True)
        self.reset_info.set_margin_start(18)
        self.reset_info.set_margin_top(10)
        page.append(self.reset_info)
        self.reset_stack = Gtk.Stack()
        self.reset_stack.set_vexpand(True)
        self.reset_stack.add_named(self._reset_dahua(), "dahua")
        self.reset_stack.add_named(self._reset_hikvision(), "hikvision")
        page.append(self.reset_stack)
        return page

    def open_reset_for(self, device: Device) -> None:
        self.go_to_page("reset")
        if device.key in self.reset_keys:
            self.reset_combo.set_selected(self.reset_keys.index(device.key))
        self.update_reset_info()

    def update_reset_info(self) -> None:
        device = self.selected_from(self.reset_combo, self.reset_keys)
        if device is None:
            self.reset_info.set_text("")
            return
        self.reset_stack.set_visible_child_name("hikvision" if device.brand == HIKVISION else "dahua")
        text = (f"{device.brand}   " + _("Model: {model}").format(model=device.model) + "   "
                + _("Serial number: {serial}").format(serial=device.serial) + f"   MAC: {device.mac}")
        if device.brand != HIKVISION:
            ways = device.reset_ways_text() or _("not reported by the device")
            text += "\n" + _("Reset methods: {ways}").format(ways=ways)
        self.reset_info.set_text(text)

    # ----------------------------------------------------------------- Dahua
    def _reset_dahua(self) -> Gtk.Widget:
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        body.add_css_class("cs-form")
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        left.set_hexpand(True)
        left.append(label(_("Step 1. Get the QR code"), css="cs-section"))
        left.append(label(_("Scan the code with the DMSS app (Password reset) or send its text to Dahua support. "
                            "You get a security code back. The code is valid for a limited time, and a new request "
                            "invalidates the previous one."), wrap=True))
        get_qr = Gtk.Button(label=_("Get QR code"))
        get_qr.set_halign(Gtk.Align.START)
        get_qr.connect("clicked", lambda *_a: self.request_reset_qr())
        left.append(get_qr)
        self.qr_text = Gtk.Entry()
        self.qr_text.set_editable(False)
        self.qr_text.set_placeholder_text(_("QR code text"))
        self.qr_text.set_hexpand(True)
        qr_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        qr_row.append(self.qr_text)
        copy = Gtk.Button.new_from_icon_name("edit-copy-symbolic")
        copy.set_tooltip_text(_("Copy"))
        copy.connect("clicked", lambda *_a: self.get_clipboard().set_content(
            Gdk.ContentProvider.new_for_value(self.qr_text.get_text())))
        qr_row.append(copy)
        left.append(qr_row)
        left.append(label(_("Step 2. Set a new password"), css="cs-section"))
        grid = _grid()
        self.reset_code = Gtk.Entry()
        self.reset_user = Gtk.Entry(text="admin")
        self.reset_new = password_entry()
        self.reset_confirm = password_entry()
        for index, (title, widget) in enumerate(((_("Security code"), self.reset_code), (_("User"), self.reset_user),
                                                 (_("New password"), self.reset_new),
                                                 (_("Confirm"), self.reset_confirm),
                                                 (_("Strength"), StrengthBar(self.reset_new)))):
            grid.attach(label(title), 0, index, 1, 1)
            widget.set_size_request(280, -1)
            grid.attach(widget, 1, index, 1, 1)
        left.append(grid)
        reset = Gtk.Button(label=_("Reset password"))
        reset.add_css_class("suggested-action")
        reset.set_halign(Gtk.Align.START)
        reset.connect("clicked", lambda *_a: self.do_reset_password())
        left.append(reset)
        self.reset_state = label("", wrap=True)
        left.append(self.reset_state)
        left.append(label(_("If the device cannot be reset over the network, hold its Reset button for 10–15 "
                            "seconds — this restores the factory settings."), css="dim-label", wrap=True))
        body.append(left)
        self.qr_area = Gtk.DrawingArea()
        self.qr_area.set_content_width(260)
        self.qr_area.set_content_height(260)
        self.qr_area.set_valign(Gtk.Align.START)
        self.qr_area.add_css_class("cs-qr")
        self.qr_matrix: list[list[bool]] | None = None
        self.qr_area.set_draw_func(lambda _a, cr, w, h: draw_matrix(cr, w, h, self.qr_matrix, _("QR code")))
        body.append(self.qr_area)
        scroller = Gtk.ScrolledWindow()
        scroller.set_child(body)
        return scroller

    def request_reset_qr(self) -> None:
        device = self.selected_from(self.reset_combo, self.reset_keys)
        if device is None or not device.mac:
            self.reset_state.set_text(_("Choose a discovered device"))
            return
        self.reset_state.set_text(_("Requesting reset data…"))

        def done(result, error):
            if error:
                self.reset_state.set_text(_("Error: {error}").format(error=error))
                self.log(_("Password reset {ip}: {result}").format(ip=device.ip, result=error), error=True)
                return
            text = result.get("qr") or ""
            self.qr_text.set_text(text)
            self.qr_matrix = qr_matrix_for(text)
            self.qr_area.queue_draw()
            hint = "" if qrcode is not None else " " + _("(the qrcode module is not installed — use the text)")
            self.reset_state.set_text((_("QR code received") + hint) if text else
                                      _("The device answered without a QR code: {params}").format(
                                          params=result.get("params")))
        run_in_thread(lambda: dahua_protocol.get_reset_descript(device), done)

    def do_reset_password(self) -> None:
        device = self.selected_from(self.reset_combo, self.reset_keys)
        if device is None:
            self.reset_state.set_text(_("Choose a device"))
            return
        code = self.reset_code.get_text().strip()
        new = self.reset_new.get_text()
        if not code:
            self.reset_state.set_text(_("Enter the security code"))
            return
        problem = password_problems_for(device.brand, new, self.reset_confirm.get_text())
        if problem:
            self.reset_state.set_text(problem)
            return
        user = self.reset_user.get_text().strip() or "admin"
        self.reset_state.set_text(_("Resetting the password…"))

        def done(_r, error):
            if error:
                self.reset_state.set_text(_("Error: {error}").format(error=error))
                self.log(_("Password reset {ip}: {result}").format(ip=device.ip, result=error), error=True)
                return
            self.remember(user, new)
            self.reset_state.set_text(_("Password reset"))
            self.log(_("Password reset {ip}: {result}").format(ip=device.ip, result=_("success")))
        run_in_thread(lambda: dahua_protocol.reset_password(device, code, new, user), done)

    # ------------------------------------------------------------- Hikvision
    def _reset_hikvision(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.add_css_class("cs-form")
        box.append(label(_("The reset takes two steps, as in the SADP tool: export a request file (security code) "
                           "and send it to Hikvision support or an authorized dealer, then import the response file "
                           "you get back and set a new password."), wrap=True))
        box.append(label(_("Preliminary: the exact reset format differs between firmwares and is not verified on "
                           "every model. Fully locked modern firmware may need a security code from the official "
                           "SADP SDK."), css="dim-label", wrap=True))
        box.append(label(_("Step 1. Export the request file"), css="cs-section"))
        box.append(label(_("If the administrator password is known and the device supports GUID files, enter it — a "
                           "verified GUID recovery file is exported. If the password is lost, leave the field empty: "
                           "a request file is built from the device serial number and MAC."),
                         css="dim-label", wrap=True))
        self.hik_reset_password = password_entry()
        self.hik_reset_password.set_property("placeholder-text", _("Administrator password (optional)"))
        self.hik_reset_password.set_hexpand(False)
        self.hik_reset_password.set_size_request(320, -1)
        box.append(self.hik_reset_password)
        export = Gtk.Button(label=_("Export request file…"))
        export.set_halign(Gtk.Align.START)
        export.connect("clicked", lambda *_a: self.export_hik_reset_request())
        box.append(export)
        box.append(label(_("Step 2. Import the response and set a new password"), css="cs-section"))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        choose = Gtk.Button(label=_("Choose response file…"))
        choose.connect("clicked", lambda *_a: self.choose_hik_response())
        row.append(choose)
        self.hik_response_label = label(_("No response file selected"), css="dim-label")
        row.append(self.hik_response_label)
        box.append(row)
        self.hik_response_path = ""
        grid = _grid()
        self.hik_reset_new = password_entry()
        self.hik_reset_confirm = password_entry()
        for index, (title, widget) in enumerate(((_("New password"), self.hik_reset_new),
                                                 (_("Confirm"), self.hik_reset_confirm),
                                                 (_("Strength"), StrengthBar(self.hik_reset_new, HIKVISION)))):
            grid.attach(label(title), 0, index, 1, 1)
            widget.set_size_request(280, -1)
            grid.attach(widget, 1, index, 1, 1)
        box.append(grid)
        apply_button = Gtk.Button(label=_("Import and reset password"))
        apply_button.add_css_class("suggested-action")
        apply_button.set_halign(Gtk.Align.START)
        apply_button.connect("clicked", lambda *_a: self.apply_hik_reset())
        box.append(apply_button)
        self.hik_reset_state = label("", wrap=True)
        box.append(self.hik_reset_state)
        scroller = Gtk.ScrolledWindow()
        scroller.set_child(box)
        return scroller

    def export_hik_reset_request(self) -> None:
        device = self.selected_from(self.reset_combo, self.reset_keys)
        if device is None:
            self.hik_reset_state.set_text(_("Choose a device"))
            return
        password = self.hik_reset_password.get_text()
        fields = {**(device.raw or {}), "serialno": device.serial, "mac": device.mac.replace(":", "-"),
                  "model": device.model, "softwareversion": device.firmware}
        self.hik_reset_state.set_text(_("Preparing the password reset request…"))

        def done(data, error):
            if error:
                self.hik_reset_state.set_text(_("Error: {error}").format(error=error))
                return
            safe = "".join(ch for ch in device.serial if ch.isalnum() or ch in "-_") or device.ip

            def write(path: str):
                try:
                    with open(path, "wb") as handle:
                        handle.write(data)
                except OSError as exc:
                    self.hik_reset_state.set_text(_("Could not save: {error}").format(error=exc))
                    return
                self.hik_reset_state.set_text(_("Request file saved. Send it to Hikvision support or an "
                                                "authorized dealer to get a response file."))
            save_file_dialog(self, _("Save password reset request"), f"{safe}-password-reset-request.xml", write)
        run_in_thread(lambda: export_reset_request(fields, address=device.ip, port=device.http_port or 80,
                                                   username="admin", password=password), done)

    def choose_hik_response(self) -> None:
        def chosen(path: str):
            self.hik_response_path = path
            self.hik_response_label.set_text(os.path.basename(path))
        open_file_dialog(self, _("Choose response file"), chosen)

    def apply_hik_reset(self) -> None:
        device = self.selected_from(self.reset_combo, self.reset_keys)
        if device is None:
            self.hik_reset_state.set_text(_("Choose a device"))
            return
        if not self.hik_response_path:
            self.hik_reset_state.set_text(_("Choose the response file you received from support."))
            return
        new = self.hik_reset_new.get_text()
        problem = password_problems_for(HIKVISION, new, self.hik_reset_confirm.get_text())
        if problem:
            self.hik_reset_state.set_text(problem)
            return
        try:
            with open(self.hik_response_path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            self.hik_reset_state.set_text(_("Could not read the response file: {error}").format(error=exc))
            return
        self.hik_reset_state.set_text(_("Applying the password reset on the device…"))

        def done(_r, error):
            if error:
                self.hik_reset_state.set_text(_("Error: {error}").format(error=error))
                self.log(_("Password reset {ip}: {result}").format(ip=device.ip, result=error), error=True)
                return
            self.remember("admin", new)
            self.hik_reset_state.set_text(_("Reset request sent. If the device accepted the response file, log in "
                                            "with the new password. If not, the response may be outdated — request "
                                            "a new one with a fresh request file."))
            self.log(_("Password reset {ip}: {result}").format(ip=device.ip, result=_("request sent")))
        run_in_thread(lambda: reset_password_with_response(device.ip, device.http_port or 80, data, new,
                                                           username="admin"), done)
