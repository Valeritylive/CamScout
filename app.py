#!/usr/bin/env python3
"""CamScout: discover, activate and configure Dahua and Hikvision devices (GTK 4)."""

from __future__ import annotations

import sys

if __name__ == "__main__":
    # Headless mode: `app.py dahua|hikvision <command> ...` never loads GTK.
    from camscout_cli import is_cli_invocation, main as cli_main
    if is_cli_invocation(sys.argv[1:]):
        raise SystemExit(cli_main(sys.argv[1:]))

import csv
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gio, GLib, GObject, Gtk, Pango  # noqa: E402

import i18n  # noqa: E402

i18n.init_from_settings()

import dahua_discovery  # noqa: E402
import dahua_ops  # noqa: E402
import device_columns  # noqa: E402
import hik_ops  # noqa: E402
from dahua_api import UPGRADE_STATES, DahuaHttp, DahuaHttpError  # noqa: E402
from dahua_discovery import (  # noqa: E402
    DISCOVERY_LOCK,
    Device,
    discover,
    discover_all,
    format_packet,
    interface_for,
    ip_range,
    local_interfaces,
    probe_ip,
    same_subnet,
    wait_for_device,
)
from devices import DAHUA, HIKVISION, from_sadp, read_devices_csv, write_devices_csv  # noqa: E402
from hikvision_sdk import get_nvr_sadp_devices, modify_nvr_camera_ip  # noqa: E402
from i18n import LANGUAGES, tr as _  # noqa: E402
from live_view import LiveViewWindow  # noqa: E402
from page_reset import ResetPageMixin  # noqa: E402
from page_settings import SettingsPageMixin  # noqa: E402
from sadp_discovery import SADP_SOCKET_LOCK  # noqa: E402
from sadp_discovery import discover as sadp_discover  # noqa: E402
from ui_common import (  # noqa: E402
    ICON_FILE,
    FormWindow,
    StrengthBar,
    clean_launch_context,
    install_css,
    label,
    open_file_dialog,
    password_problems_for,
    password_rules,
    run_in_thread,
    save_file_dialog,
)

APP_ID = "io.github.Valeritylive.CamScout"
ICON_NAME = APP_ID
APP_TITLE = "CamScout"
VERSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")


def app_version() -> str:
    try:
        with open(VERSION_FILE, encoding="ascii") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def discover_hikvision(interfaces: list[tuple[str, str, str]], timeout: float = 2.5) -> list[Device]:
    found: list[Device] = []
    with SADP_SOCKET_LOCK:
        for interface in interfaces:
            found += [from_sadp(device) for device in sadp_discover(interface, timeout=timeout)]
    return found


class DeviceItem(GObject.Object):
    """Row of the device table; the cells read the live Device by key."""

    def __init__(self, key: str):
        super().__init__()
        self.key = key


class MainWindow(SettingsPageMixin, ResetPageMixin, Gtk.ApplicationWindow):
    """ConfigTool layout: navigation on the left, pages on the right, interface
    selector and status at the bottom. Dahua and Hikvision devices share one list."""

    PAGES = (
        ("devices", "Devices", "network-wired-symbolic"),
        ("settings", "Device settings", "preferences-system-symbolic"),
        ("upgrade", "Firmware", "software-update-available-symbolic"),
        ("reset", "Password reset", "dialog-password-symbolic"),
        ("log", "Log", "document-open-recent-symbolic"),
        ("prefs", "Preferences", "preferences-desktop-locale-symbolic"),
    )

    def __init__(self, app: Gtk.Application, previous: "MainWindow | None" = None):
        super().__init__(application=app, title=APP_TITLE)
        self.set_default_size(1380, 780)
        self.set_size_request(980, 580)
        self.set_icon_name(ICON_NAME)
        install_css()
        prefs = i18n.load_settings()
        self.interfaces = local_interfaces()
        self.devices: dict[str, Device] = {}
        self.device_seen: dict[str, float] = {}
        self.numbers: dict[str, int] = {}
        self.pinned: set[str] = set()       # added by hand or imported: never expire
        self.checked: set[str] = set()
        self.scanning = False
        self.username = "admin"
        self.password = ""
        self.search_range: tuple[str, str] | None = None
        self.auto_refresh = bool(prefs.get("auto_refresh", True))
        self.scan_brands = {DAHUA: bool(prefs.get("scan_dahua", True)),
                            HIKVISION: bool(prefs.get("scan_hikvision", True))}
        self.firmware_path = ""
        self.upgrade_rows: dict[str, dict[str, Any]] = {}
        self.live_windows: list[Gtk.Window] = []
        self.trace_lock = threading.Lock()
        self.trace_entries: list[str] = []
        self._updating_select_all = False
        self.init_settings_state()
        if previous is not None:  # language switch: keep what the user already has
            for name in ("devices", "device_seen", "numbers", "pinned", "checked", "username", "password",
                         "search_range", "trace_entries"):
                setattr(self, name, getattr(previous, name))

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(root)
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        body.set_vexpand(True)
        root.append(body)

        nav_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        nav_box.add_css_class("cs-nav")
        nav_box.set_size_request(156, -1)
        if ICON_FILE.exists():
            logo = Gtk.Image.new_from_file(str(ICON_FILE))
            logo.set_pixel_size(52)
            logo.set_margin_top(12)
            nav_box.append(logo)
        nav_box.append(label(APP_TITLE, 0.5, "cs-logo"))
        nav_box.append(label("Dahua · Hikvision", 0.5, "dim-label"))
        self.nav = Gtk.ListBox()
        self.nav.add_css_class("navigation-sidebar")
        self.nav.set_margin_top(10)
        self.nav.set_vexpand(True)
        nav_box.append(self.nav)
        body.append(nav_box)
        body.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_hexpand(True)
        body.append(self.stack)

        self.log_buffer = Gtk.TextBuffer()
        if previous is not None:
            buffer = previous.log_buffer
            self.log_buffer.set_text(buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False))
        builders = {
            "devices": self.build_devices_page,
            "settings": self.build_settings_page,
            "upgrade": self.build_upgrade_page,
            "reset": self.build_reset_page,
            "log": self.build_log_page,
            "prefs": self.build_preferences_page,
        }
        for name, title, icon in self.PAGES:
            self.stack.add_named(builders[name](), name)
            row = Gtk.ListBoxRow()
            row.page_name = name
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            image = Gtk.Image.new_from_icon_name(icon)
            image.set_pixel_size(26)
            box.append(image)
            caption = label(_(title), 0.5, "cs-nav-title", wrap=True)
            caption.set_justify(Gtk.Justification.CENTER)
            box.append(caption)
            row.set_child(box)
            self.nav.append(row)
        self.nav.connect("row-selected", self.on_nav_selected)

        root.append(Gtk.Separator())
        statusbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        statusbar.add_css_class("cs-statusbar")
        statusbar.append(label(_("Network interface:")))
        iface_names = [f"{name}  ·  {ip}" for name, ip, _mask in self.interfaces] or [_("No interfaces found")]
        if len(self.interfaces) > 1:
            iface_names.append(_("All interfaces"))
        self.iface_combo = Gtk.DropDown.new_from_strings(iface_names)
        if previous is not None:
            self.iface_combo.set_selected(previous.iface_combo.get_selected())
        self.iface_combo.connect("notify::selected", self.on_interface_changed)
        statusbar.append(self.iface_combo)
        self.status = label(_("Ready to search"), css="dim-label")
        self.status.set_hexpand(True)
        self.status.set_halign(Gtk.Align.END)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        statusbar.append(self.status)
        root.append(statusbar)

        self.nav.select_row(self.nav.get_row_at_index(0))
        self.render_devices()
        self.scan_timer = GLib.timeout_add_seconds(20, self.periodic_scan)
        if previous is None:
            GLib.idle_add(self.start_scan)
        self.connect("close-request", self.on_close_request)

    # ------------------------------------------------------------------ common
    def on_close_request(self, *_args) -> bool:
        if self.scan_timer:
            GLib.source_remove(self.scan_timer)
            self.scan_timer = 0
        for window in list(self.live_windows):
            window.close()
        return False

    def on_nav_selected(self, _box, row) -> None:
        if row is None:
            return
        self.stack.set_visible_child_name(row.page_name)
        if row.page_name == "settings":
            self.refresh_device_dropdown(self.settings_combo, self.settings_keys)
        elif row.page_name == "reset":
            self.refresh_device_dropdown(self.reset_combo, self.reset_keys)

    def go_to_page(self, name: str) -> None:
        for index, (page, _title, _icon) in enumerate(self.PAGES):
            if page == name:
                self.nav.select_row(self.nav.get_row_at_index(index))

    def all_interfaces_selected(self) -> bool:
        return len(self.interfaces) > 1 and self.iface_combo.get_selected() >= len(self.interfaces)

    def scan_interfaces(self) -> list[tuple[str, str, str]]:
        """Interfaces the search runs on: the chosen one or all of them."""
        if not self.interfaces:
            return []
        if self.all_interfaces_selected():
            return list(self.interfaces)
        return [self.interfaces[min(self.iface_combo.get_selected(), len(self.interfaces) - 1)]]

    def current_interface(self, ip: str = "") -> tuple[str, str, str] | None:
        """The chosen interface; with «all interfaces» the one whose subnet holds *ip*."""
        interfaces = self.scan_interfaces()
        if not interfaces:
            return None
        return interface_for(ip, interfaces) if ip else interfaces[0]

    def log(self, text: str, error: bool = False) -> None:
        stamp = time.strftime("%H:%M:%S")
        prefix = _("ERROR:") + " " if error else ""
        self.log_buffer.insert(self.log_buffer.get_end_iter(), f"[{stamp}] {prefix}{text}\n")

    def show_result(self, text: str, error: bool = False) -> None:
        self.status.set_text(text.splitlines()[0] if text else "")
        self.log(text, error)

    def message(self, title: str, text: str) -> None:
        dialog = FormWindow(self, title, _("Close"), cancel=False)
        dialog.add_wide(label(text, wrap=True, selectable=True))
        dialog.present()

    def confirm(self, title: str, text: str, ok_label: str, on_yes: Callable[[], None]) -> None:
        dialog = FormWindow(self, title, ok_label)
        dialog.add_wide(label(text, wrap=True))
        dialog.ok_button.remove_css_class("suggested-action")
        dialog.ok_button.add_css_class("destructive-action")

        def ok():
            dialog.close()
            on_yes()
        dialog.on_ok = ok
        dialog.present()

    def show_batch_results(self, title: str, results: list[tuple[Device, str | None, str]]) -> None:
        failures = sum(1 for _d, error, _i in results if error)
        dialog = FormWindow(self, title, _("Close"), width=640, cancel=False)
        dialog.add_wide(label(_("Succeeded: {ok}, failed: {failed}").format(
            ok=len(results) - failures, failed=failures), css="heading"))
        listbox = Gtk.ListBox()
        listbox.add_css_class("boxed-list")
        for device, error, info in results:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.set_margin_top(6)
            row.set_margin_bottom(6)
            row.set_margin_start(8)
            row.set_margin_end(8)
            icon = Gtk.Image.new_from_icon_name("dialog-error-symbolic" if error else "emblem-ok-symbolic")
            icon.add_css_class("cs-bad" if error else "cs-ok")
            row.append(icon)
            row.append(label(f"{device.brand}  {device.ip or device.mac}  {device.model}"))
            detail = label(error or info, css="dim-label", wrap=True, selectable=True)
            detail.set_hexpand(True)
            row.append(detail)
            listbox.append(row)
        scroller = Gtk.ScrolledWindow()
        scroller.set_min_content_height(min(320, 48 * max(1, len(results))))
        scroller.set_child(listbox)
        dialog.add_wide(scroller)
        dialog.present()
        for device, error, info in results:
            self.log(f"{title}: {device.brand} {device.ip or device.mac} — {error or info}", error is not None)

    def credentials_rows(self, dialog: FormWindow) -> tuple[Gtk.Entry, Gtk.PasswordEntry]:
        user = dialog.entry(_("User name"), self.username)
        password = dialog.password(_("Password"), self.password)
        return user, password

    def remember(self, username: str, password: str) -> None:
        self.username, self.password = username, password

    def open_web(self, device: Device) -> None:
        if not device.ip:
            return
        port = "" if device.http_port in (0, 80) else f":{device.http_port}"
        try:
            Gio.AppInfo.launch_default_for_uri(f"http://{device.ip}{port}/", clean_launch_context())
        except GLib.Error as exc:
            self.show_result(_("Could not open the browser: {error}").format(error=exc.message), error=True)

    def open_live_view(self, device: Device) -> None:
        window = LiveViewWindow(self, device, self.username, self.password)
        self.live_windows.append(window)
        window.connect("close-request", lambda w: (self.live_windows.remove(w) if w in self.live_windows
                                                   else None, False)[1])
        window.present()

    # ------------------------------------------------------------ devices page
    def build_devices_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toolbar.add_css_class("cs-toolbar")

        def button(text: str, icon: str, tooltip: str, callback, suggested: bool = False) -> Gtk.Button:
            btn = Gtk.Button()
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            content.append(Gtk.Image.new_from_icon_name(icon))
            if text:
                content.append(Gtk.Label(label=text))
            btn.set_child(content)
            btn.set_tooltip_text(tooltip)
            btn.connect("clicked", lambda *_a: callback())
            if suggested:
                btn.add_css_class("suggested-action")
            toolbar.append(btn)
            return btn

        self.init_button = button(_("Activate"), "dialog-password-symbolic",
                                  _("Set the admin password on the checked uninitialized/inactive devices"),
                                  self.open_initialize)
        self.batch_ip_button = button(_("Batch IP change"), "network-wired-symbolic",
                                      _("Give the checked devices consecutive IP addresses"), self.open_batch_ip)
        button(_("Add"), "list-add-symbolic", _("Add a device by IP"), self.open_manual_add)
        self.delete_button = button(_("Remove"), "list-remove-symbolic", _("Remove the checked devices from the list"),
                                    self.delete_checked)
        button("", "document-open-symbolic", _("Import: load a device list from CSV"), self.import_csv)
        button("", "document-save-symbolic", _("Export: save the list (checked or all) to CSV"), self.export_csv)

        more = Gtk.MenuButton(label=_("More"))
        more.set_tooltip_text(_("More operations"))
        popover = Gtk.Popover()
        more_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for text, callback in ((_("Sync time with PC") + " (Dahua)", self.batch_sync_time),
                               (_("Reboot") + " (Dahua)", self.batch_reboot),
                               (_("Upgrade firmware…") + " (Dahua)", self.send_checked_to_upgrade),
                               (_("Get details over HTTP…") + " (Dahua)", self.fetch_details_checked),
                               (None, None),
                               (_("Cameras behind an NVR…") + " (Hikvision)", self.open_nvr_sadp)):
            if text is None:
                more_box.append(Gtk.Separator())
                continue
            item = Gtk.Button(label=text)
            item.add_css_class("flat")
            item.get_child().set_xalign(0)

            def clicked(_b, cb=callback):
                popover.popdown()
                cb()
            item.connect("clicked", clicked)
            more_box.append(item)
        popover.set_child(more_box)
        more.set_popover(popover)
        toolbar.append(more)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        toolbar.append(spacer)
        self.brand_filter = Gtk.DropDown.new_from_strings([_(b) for b in device_columns.BRAND_FILTERS])
        self.brand_filter.set_tooltip_text(_("Brand"))
        self.brand_filter.connect("notify::selected", lambda *_a: self.render_devices())
        toolbar.append(self.brand_filter)
        self.type_filter = Gtk.DropDown.new_from_strings([_(t) for t in device_columns.TYPE_FILTERS])
        self.type_filter.set_tooltip_text(_("Device type"))
        self.type_filter.connect("notify::selected", lambda *_a: self.render_devices())
        toolbar.append(self.type_filter)
        self.filter_entry = Gtk.SearchEntry()
        self.filter_entry.set_placeholder_text(_("Search"))
        self.filter_entry.set_width_chars(12)
        self.filter_entry.connect("search-changed", lambda *_a: self.render_devices())
        toolbar.append(self.filter_entry)

        columns_button = Gtk.MenuButton()
        columns_button.set_icon_name("view-list-symbolic")
        columns_button.set_tooltip_text(_("Columns"))
        toolbar.append(columns_button)
        settings_button = Gtk.Button.new_from_icon_name("emblem-system-symbolic")
        settings_button.set_tooltip_text(_("Search settings"))
        settings_button.connect("clicked", lambda *_a: self.open_search_settings())
        toolbar.append(settings_button)
        self.scan_button = button(_("Refresh"), "view-refresh-symbolic", _("Find devices"), self.start_scan,
                                  suggested=True)
        # Longer translations must not widen the window: the toolbar scrolls sideways if it does not fit.
        toolbar_scroll = Gtk.ScrolledWindow()
        toolbar_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        toolbar_scroll.set_propagate_natural_height(True)
        toolbar_scroll.set_child(toolbar)
        page.append(toolbar_scroll)
        page.append(Gtk.Separator())

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_child(self.build_device_table())
        page.append(scroller)

        columns_popover = Gtk.Popover()
        columns_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for key, title, *_rest in device_columns.COLUMNS:
            if key == "no":
                continue
            check = Gtk.CheckButton(label=_(title))
            check.set_active(self.table_columns[key].get_visible())
            check.connect("toggled", lambda c, k=key: self.table_columns[k].set_visible(c.get_active()))
            columns_box.append(check)
        columns_scroller = Gtk.ScrolledWindow()
        columns_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        columns_scroller.set_max_content_height(460)
        columns_scroller.set_propagate_natural_height(True)
        columns_scroller.set_child(columns_box)
        columns_popover.set_child(columns_scroller)
        columns_button.set_popover(columns_popover)

        page.append(Gtk.Separator())
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.add_css_class("cs-statusbar")
        self.select_all = Gtk.CheckButton(label=_("Select all"))
        self.select_all.connect("toggled", self.toggle_select_all)
        footer.append(self.select_all)
        self.total_label = label(_("Devices found: {count}").format(count=0))
        self.total_label.set_hexpand(True)
        footer.append(self.total_label)
        page.append(footer)
        return page

    def build_device_table(self) -> Gtk.ColumnView:
        self.store = Gio.ListStore(item_type=DeviceItem)
        self.column_view = Gtk.ColumnView()
        self.column_view.add_css_class("cs-table")
        self.column_view.set_show_row_separators(True)
        self.column_view.set_show_column_separators(True)
        self.column_view.set_reorderable(True)
        sorted_model = Gtk.SortListModel(model=self.store, sorter=self.column_view.get_sorter())
        self.selection = Gtk.SingleSelection(model=sorted_model)
        self.selection.set_autoselect(False)
        self.selection.set_can_unselect(True)
        self.column_view.set_model(self.selection)
        self.column_view.connect("activate", self.on_row_activated)

        check_factory = Gtk.SignalListItemFactory()

        def setup_check(_f, item):
            check = Gtk.CheckButton()
            check.device_key = None
            check.connect("toggled", self.on_device_checked)
            item.set_child(check)

        def bind_check(_f, item):
            check = item.get_child()
            check.device_key = None
            check.set_active(item.get_item().key in self.checked)
            check.device_key = item.get_item().key
        check_factory.connect("setup", setup_check)
        check_factory.connect("bind", bind_check)
        check_column = Gtk.ColumnViewColumn(title="", factory=check_factory)
        check_column.set_fixed_width(34)
        self.column_view.append_column(check_column)

        self.table_columns: dict[str, Gtk.ColumnViewColumn] = {}
        number_column = None
        for key, title, _getter, _sorter, width, shown in device_columns.COLUMNS:
            factory = Gtk.SignalListItemFactory()
            factory.connect("setup", self._setup_cell, key)
            factory.connect("bind", self._bind_cell, key)
            column = Gtk.ColumnViewColumn(title=_(title), factory=factory)
            column.set_resizable(True)
            column.set_fixed_width(width)
            column.set_visible(shown)
            column.set_sorter(Gtk.CustomSorter.new(self._compare_rows, key))
            self.column_view.append_column(column)
            self.table_columns[key] = column
            if key == "no":
                number_column = column
            if key == "version":
                self.column_view.append_column(self._operations_column())
        if number_column is not None:
            self.column_view.sort_by_column(number_column, Gtk.SortType.ASCENDING)
        return self.column_view

    def _operations_column(self) -> Gtk.ColumnViewColumn:
        """ConfigTool's "Operate" column: edit IP, live view, open web, details."""
        factory = Gtk.SignalListItemFactory()

        def setup(_f, item):
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
            for icon, tooltip, callback in (("document-edit-symbolic", _("Change IP"), self.open_modify_ip),
                                            ("camera-video-symbolic", _("Live view"), self.open_live_view),
                                            ("web-browser-symbolic", _("Open web interface"), self.open_web),
                                            ("dialog-information-symbolic", _("Details"), self.open_details)):
                btn = Gtk.Button.new_from_icon_name(icon)
                btn.add_css_class("flat")
                btn.set_tooltip_text(tooltip)
                btn.device_key = None

                def clicked(b, cb=callback):
                    device = self.devices.get(b.device_key)
                    if device is not None:
                        cb(device)
                btn.connect("clicked", clicked)
                box.append(btn)
            item.set_child(box)

        def bind(_f, item):
            child = item.get_child().get_first_child()
            while child is not None:
                child.device_key = item.get_item().key
                child = child.get_next_sibling()
        factory.connect("setup", setup)
        factory.connect("bind", bind)
        column = Gtk.ColumnViewColumn(title=_("Operations"), factory=factory)
        column.set_fixed_width(150)
        return column

    @staticmethod
    def _setup_cell(_factory, item, key: str) -> None:
        cell = Gtk.Label(xalign=0)
        cell.set_ellipsize(Pango.EllipsizeMode.END)
        cell.set_margin_start(4)
        cell.set_margin_end(4)
        if key == "brand":
            cell.add_css_class("cs-badge")
            cell.set_halign(Gtk.Align.START)
            cell.set_valign(Gtk.Align.CENTER)
        item.set_child(cell)

    def _bind_cell(self, _factory, item, key: str) -> None:
        cell = item.get_child()
        text = self.cell_text(item.get_item().key, key)
        cell.set_text(text)
        cell.set_tooltip_text(text or None)
        device = self.devices.get(item.get_item().key)
        if key == "brand":
            cell.remove_css_class("cs-badge-dahua")
            cell.remove_css_class("cs-badge-hikvision")
            if device is not None:
                cell.add_css_class("cs-badge-hikvision" if device.brand == HIKVISION else "cs-badge-dahua")
        elif key == "status":
            cell.remove_css_class("cs-ok")
            cell.remove_css_class("cs-warn")
            if device is not None and device.initialized is False:
                cell.add_css_class("cs-warn")
            elif device is not None and device.initialized is True:
                cell.add_css_class("cs-ok")

    def cell_text(self, device_key: str, column_key: str) -> str:
        device = self.devices.get(device_key)
        if device is None:
            return ""
        return device_columns.column_value(device, column_key, self.numbers.get(device_key))

    def _compare_rows(self, first, second, column_key: str) -> int:
        to_key = device_columns.sort_key(column_key)
        a = to_key(self.cell_text(first.key, column_key))
        b = to_key(self.cell_text(second.key, column_key))
        return (a > b) - (a < b)

    def add_devices(self, found: list[Device], pin: bool = False) -> None:
        now = time.monotonic()
        for device in found:
            key = device.key
            if not key:
                continue
            if key not in self.numbers:
                self.numbers[key] = max(self.numbers.values(), default=0) + 1
            previous = self.devices.get(key)
            if (previous is not None and device.protocol in ("HTTP", "CSV", "Manual")
                    and previous.protocol in ("DHIP", "DVRIP", "SADP")):
                continue
            # A device added by IP (no MAC yet) is replaced once the search finds it.
            stale = device.ip.lower() if device.mac else ""
            if stale and stale != key and stale in self.devices and not self.devices[stale].mac:
                self.devices.pop(stale)
                self.device_seen.pop(stale, None)
                if stale in self.pinned:
                    self.pinned.discard(stale)
                    self.pinned.add(key)
                if stale in self.checked:
                    self.checked.discard(stale)
                    self.checked.add(key)
                old_number = self.numbers.pop(stale, None)
                if old_number:
                    self.numbers[key] = old_number
            self.devices[key] = device
            self.device_seen[key] = now
            if pin:
                self.pinned.add(key)

    def start_scan(self, *_args) -> bool:
        if self.scanning:
            return False
        interfaces = self.scan_interfaces()
        if not interfaces:
            self.status.set_text(_("No active network interface found"))
            return False
        targets: list[str] = []
        if self.search_range:
            try:
                targets = ip_range(*self.search_range)
            except ValueError as exc:
                self.show_result(str(exc), error=True)
        brands = [brand for brand, enabled in self.scan_brands.items() if enabled]
        if not brands:
            self.status.set_text(_("Both brands are turned off in the search settings"))
            return False
        self.scanning = True
        self.scan_button.set_sensitive(False)
        names = ", ".join(i[0] for i in interfaces)
        self.status.set_text(_("Searching for devices on {interfaces}…").format(interfaces=names))
        errors: list[str] = []

        def dahua_work() -> list[Device]:
            timeout = 3.0 if not targets else 3.0 + len(targets) / 300
            with DISCOVERY_LOCK:
                if len(interfaces) == 1:
                    return discover(interfaces[0], timeout=timeout, unicast_targets=targets)
                return discover_all(interfaces, timeout=2.5, unicast_targets=targets)

        def work():
            jobs = {}
            with ThreadPoolExecutor(max_workers=2) as pool:
                if DAHUA in brands:
                    jobs[DAHUA] = pool.submit(dahua_work)
                if HIKVISION in brands:
                    jobs[HIKVISION] = pool.submit(discover_hikvision, interfaces, 3.0 if len(interfaces) == 1 else 2.5)
            found: list[Device] = []
            for brand, job in jobs.items():
                try:
                    found += job.result()
                except OSError as exc:
                    errors.append(f"{brand}: {exc}")
            return found

        def done(found, error):
            self.scanning = False
            self.scan_button.set_sensitive(True)
            for text in errors + ([error] if error else []):
                self.show_result(_("Search error: {error}").format(error=text), error=True)
            self.add_devices(found or [])
            now = time.monotonic()
            for key in [k for k, seen in self.device_seen.items() if now - seen > 70 and k not in self.pinned]:
                self.devices.pop(key, None)
                self.device_seen.pop(key, None)
                self.checked.discard(key)
            self.render_devices()
            if errors and not found:
                return
            self.status.set_text(_("Search finished at {time}").format(time=time.strftime("%H:%M:%S"))
                                 if self.devices else _("No devices found — check the interface and connection"))
        run_in_thread(work, done)
        return False

    def periodic_scan(self) -> bool:
        if self.auto_refresh and not DISCOVERY_LOCK.locked() and not SADP_SOCKET_LOCK.locked():
            self.start_scan()
        return True

    def visible_devices(self) -> list[tuple[str, Device]]:
        query = self.filter_entry.get_text()
        type_filter = device_columns.TYPE_FILTERS[self.type_filter.get_selected()]
        brand_filter = device_columns.BRAND_FILTERS[self.brand_filter.get_selected()]
        return [(key, device) for key, device in self.devices.items()
                if device_columns.matches_brand(device, brand_filter)
                and device_columns.matches_type(device, type_filter)
                and device_columns.matches_filter(device, query, self.numbers.get(key))]

    def render_devices(self) -> None:
        selected = self.selection.get_selected_item()
        selected_key = selected.key if selected is not None else None
        items = [DeviceItem(key) for key, _device in self.visible_devices()]
        self.store.splice(0, self.store.get_n_items(), items)
        if selected_key is not None:
            for position in range(self.selection.get_n_items()):
                if self.selection.get_item(position).key == selected_key:
                    self.selection.set_selected(position)
                    break
        self.update_buttons()

    def checked_devices(self) -> list[Device]:
        return [self.devices[key] for key in sorted(self.checked & set(self.devices),
                                                    key=lambda k: self.numbers.get(k, 0))]

    def update_buttons(self) -> None:
        checked = self.checked_devices()
        self.init_button.set_sensitive(any(d.initialized is not True for d in checked))
        self.batch_ip_button.set_sensitive(bool(checked))
        self.delete_button.set_sensitive(bool(checked))
        total = len(self.devices)
        dahua = sum(1 for d in self.devices.values() if d.brand == DAHUA)
        uninitialized = sum(1 for d in self.devices.values() if d.initialized is False)
        shown = self.store.get_n_items()
        text = _("Devices found: {count}").format(count=total)
        if total:
            text += f"  (Dahua: {dahua}, Hikvision: {total - dahua})"
        if uninitialized:
            text += "   " + _("Not activated: {count}").format(count=uninitialized)
        if shown != total:
            text += "   " + _("Shown: {count}").format(count=shown)
        text += "   " + _("Checked: {count}").format(count=len(checked))
        self.total_label.set_text(text)
        visible = [self.selection.get_item(i).key for i in range(self.selection.get_n_items())]
        self._updating_select_all = True
        self.select_all.set_active(bool(visible) and all(key in self.checked for key in visible))
        self._updating_select_all = False

    def on_device_checked(self, check: Gtk.CheckButton) -> None:
        key = getattr(check, "device_key", None)
        if key is None:
            return
        if check.get_active():
            self.checked.add(key)
        else:
            self.checked.discard(key)
        self.update_buttons()

    def toggle_select_all(self, check: Gtk.CheckButton) -> None:
        if self._updating_select_all:
            return
        visible = [self.selection.get_item(i).key for i in range(self.selection.get_n_items())]
        if check.get_active():
            self.checked.update(visible)
        else:
            self.checked.difference_update(visible)
        self.render_devices()

    def on_row_activated(self, _view, position: int) -> None:
        item = self.selection.get_item(position)
        device = self.devices.get(item.key) if item is not None else None
        if device is not None:
            self.open_modify_ip(device)

    def on_interface_changed(self, *_args) -> None:
        # Devices from the previous NIC are not reachable through the new one.
        for key in [k for k in self.devices if k not in self.pinned]:
            self.devices.pop(key, None)
            self.device_seen.pop(key, None)
            self.checked.discard(key)
        self.render_devices()
        self.start_scan()

    def selected_device(self) -> Device | None:
        item = self.selection.get_selected_item()
        return self.devices.get(item.key) if item is not None else None

    # ------------------------------------------------------- device dialogs
    def open_search_settings(self) -> None:
        dialog = FormWindow(self, _("Search settings"), _("Save"))
        dialog.add_wide(label(_("Brands"), css="cs-section"))
        brand_checks = {}
        for brand, protocol in ((DAHUA, "DHIP, UDP 37810"), (HIKVISION, "SADP, UDP 37020")):
            check = Gtk.CheckButton(label=f"{brand}  ({protocol})")
            check.set_active(self.scan_brands[brand])
            dialog.add_wide(check)
            brand_checks[brand] = check
        current = Gtk.CheckButton(label=_("Current network segment (multicast and broadcast)"))
        current.set_active(True)
        current.set_sensitive(False)
        dialog.add_wide(current)
        other = Gtk.CheckButton(label=_("Also search another segment (IP range, Dahua)"))
        other.set_active(self.search_range is not None)
        dialog.add_wide(other)
        start = dialog.entry(_("Start IP"), self.search_range[0] if self.search_range else "", "192.168.1.1")
        end = dialog.entry(_("End IP"), self.search_range[1] if self.search_range else "", "192.168.1.254")

        def toggle(*_a):
            start.set_sensitive(other.get_active())
            end.set_sensitive(other.get_active())
        other.connect("toggled", toggle)
        toggle()
        dialog.add_wide(label(_("Default credentials for operations"), css="cs-section"))
        user, password = self.credentials_rows(dialog)
        auto = Gtk.CheckButton(label=_("Refresh the list automatically every 20 seconds"))
        auto.set_active(self.auto_refresh)
        dialog.add_wide(auto)

        def ok():
            if other.get_active():
                try:
                    ip_range(start.get_text().strip(), end.get_text().strip())
                except ValueError as exc:
                    dialog.show_error(_("Range: {error}").format(error=exc))
                    return
                self.search_range = (start.get_text().strip(), end.get_text().strip())
            else:
                self.search_range = None
            self.remember(user.get_text().strip() or "admin", password.get_text())
            self.auto_refresh = auto.get_active()
            self.scan_brands = {brand: check.get_active() for brand, check in brand_checks.items()}
            self.save_preferences()
            dialog.close()
            self.start_scan()
        dialog.on_ok = ok
        dialog.present()

    def save_preferences(self) -> None:
        try:
            i18n.save_settings({"auto_refresh": self.auto_refresh, "scan_dahua": self.scan_brands[DAHUA],
                                "scan_hikvision": self.scan_brands[HIKVISION]})
        except OSError as exc:
            self.log(_("Could not save the settings: {error}").format(error=exc), error=True)

    def open_details(self, device: Device) -> None:
        dialog = FormWindow(self, _("Details") + f" — {device.brand} {device.ip}", _("Close"), width=580,
                            cancel=False)
        grid = Gtk.Grid(row_spacing=4, column_spacing=16)
        rows = [(_(title), device_columns.column_value(device, key))
                for key, title, *_r in device_columns.COLUMNS if key != "no"]
        rows = [row for row in rows if row[1]]
        rows += [(f"· {key}", str(value)) for key, value in sorted((device.raw or {}).items())]
        for index, (name, value) in enumerate(rows):
            grid.attach(label(name, css="dim-label"), 0, index, 1, 1)
            grid.attach(label(value, selectable=True, wrap=True), 1, index, 1, 1)
        scroller = Gtk.ScrolledWindow()
        scroller.set_min_content_height(420)
        scroller.set_child(grid)
        dialog.add_wide(scroller)
        dialog.present()

    def _network_mode(self, dialog: FormWindow, dhcp: bool) -> tuple[Gtk.CheckButton, Gtk.CheckButton]:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        static = Gtk.CheckButton(label=_("Static"))
        dynamic = Gtk.CheckButton(label="DHCP")
        dynamic.set_group(static)
        (dynamic if dhcp else static).set_active(True)
        box.append(static)
        box.append(dynamic)
        dialog.add_row(_("Mode"), box)
        return static, dynamic

    def _method_dropdown(self, dialog: FormWindow, brand: str) -> Gtk.DropDown:
        if brand == HIKVISION:
            dropdown = Gtk.DropDown.new_from_strings([_("SADP (by MAC)"), "ISAPI (HTTP)"])
            dropdown.set_tooltip_text(_("SADP works across subnets, like Hikvision SADP; ISAPI also sets DNS"))
        else:
            dropdown = Gtk.DropDown.new_from_strings([_("Auto"), _("Multicast (by MAC)"), "HTTP API"])
            dropdown.set_tooltip_text(_("Auto: HTTP in your subnet, multicast by MAC in another one, "
                                        "like ConfigTool"))
        return dialog.add_row(_("Method"), dropdown)

    def open_modify_ip(self, device: Device) -> None:
        if device.initialized is False:
            self.message(_("The device is not activated"),
                         _("Activate the device (set a password) first, then change its IP."))
            return
        if device.brand == HIKVISION:
            self.open_modify_ip_hikvision(device)
            return
        dialog = FormWindow(self, _("Change IP") + " — Dahua", "OK")
        dialog.add_row(_("Device"), label(f"{device.model}  ·  {device.mac}", css="dim-label"))
        static, dynamic = self._network_mode(dialog, bool(device.dhcp))
        ip = dialog.entry(_("IP address"), device.ip)
        mask = dialog.entry(_("Subnet mask"), device.subnet_mask or "255.255.255.0")
        gateway = dialog.entry(_("Gateway"), device.gateway)
        dns1 = dialog.entry("DNS 1", "", _("unchanged"))
        dns2 = dialog.entry("DNS 2", "", _("unchanged"))
        user, password = self.credentials_rows(dialog)
        method = self._method_dropdown(dialog, DAHUA)
        if not same_subnet(device.ip, device.interface):
            dialog.add_wide(label(_("The device is in another subnet: the command goes by multicast to its MAC, "
                                    "and the password crosses the local network in clear text."),
                                  css="dim-label", wrap=True))

        def sync(*_a):
            for entry in (ip, mask, gateway):
                entry.set_sensitive(static.get_active())
        static.connect("toggled", sync)
        sync()

        def ok():
            settings = dahua_ops.NetworkSettings(ip=ip.get_text().strip(), mask=mask.get_text().strip(),
                                                 gateway=gateway.get_text().strip(), dhcp=dynamic.get_active(),
                                                 dns1=dns1.get_text().strip(), dns2=dns2.get_text().strip())
            try:
                settings.validate()
            except dahua_ops.OperationError as exc:
                dialog.show_error(str(exc))
                return
            username, secret = user.get_text().strip() or "admin", password.get_text()
            if not secret:
                dialog.show_error(_("Enter the device password"))
                return
            chosen = dahua_ops.METHODS[method.get_selected()]
            dialog.set_busy(True)

            def done(via, error):
                dialog.set_busy(False)
                if error:
                    dialog.show_error(error)
                    return
                self.remember(username, secret)
                dialog.close()
                self.show_result(_("{mac}: IP changed to {ip} ({via})").format(
                    mac=device.mac, ip=settings.ip or "DHCP", via=via))
                self.forget_device(device)
                self.track_device(device, settings.ip if not settings.dhcp else "")
            run_in_thread(lambda: dahua_ops.modify_ip(device, username, secret, settings, chosen), done)
        dialog.on_ok = ok
        dialog.present()

    def open_modify_ip_hikvision(self, device: Device) -> None:
        """Windows SADP's "Modify Network Parameters" as a dialog."""
        dialog = FormWindow(self, _("Change IP") + " — Hikvision", "OK", width=480)
        dialog.add_row(_("Device"), label(f"{device.model}  ·  {device.mac}", css="dim-label"))
        dialog.add_row(_("Serial number"), label(device.serial, css="dim-label", selectable=True))
        static, dynamic = self._network_mode(dialog, bool(device.dhcp))
        ip = dialog.entry(_("IPv4 address"), device.ip)
        mask = dialog.entry(_("Subnet mask"), device.subnet_mask or "255.255.255.0")
        gateway = dialog.entry(_("IPv4 gateway"), device.gateway)
        port = dialog.entry(_("SDK port"), str(device.tcp_port or 8000))
        http = dialog.entry(_("HTTP port"), str(device.http_port or 80))
        ipv6 = dialog.entry(_("IPv6 address"), device.ipv6)
        ipv6_gateway = dialog.entry(_("IPv6 gateway"), device.ipv6_gateway)
        prefix = dialog.entry(_("IPv6 prefix length"), (device.raw or {}).get("ipv6masklen") or "64")
        dns1 = dialog.entry("DNS 1", "", _("unchanged (ISAPI only)"))
        dns2 = dialog.entry("DNS 2", "", _("unchanged (ISAPI only)"))
        user, password = self.credentials_rows(dialog)
        method = self._method_dropdown(dialog, HIKVISION)
        forgot = Gtk.LinkButton(uri="about:blank", label=_("Forgot password?"))
        forgot.set_halign(Gtk.Align.START)
        forgot.connect("activate-link", lambda *_a: (dialog.close(), self.open_reset_for(device), True)[-1])
        dialog.add_wide(forgot)
        dialog.add_wide(label(_("SADP sends the settings to the MAC address over UDP 37020, so the device may be "
                                "in another subnet. Several wrong passwords in a row can lock the device."),
                              css="dim-label", wrap=True))

        def sync(*_a):
            for entry in (ip, mask, gateway):
                entry.set_sensitive(static.get_active())
        static.connect("toggled", sync)
        sync()

        def ok():
            try:
                settings = hik_ops.HikNetwork(
                    ip=ip.get_text().strip(), mask=mask.get_text().strip(), gateway=gateway.get_text().strip(),
                    dhcp=dynamic.get_active(), command_port=int(port.get_text().strip() or 8000),
                    http_port=int(http.get_text().strip() or 80), ipv6=ipv6.get_text().strip(),
                    ipv6_gateway=ipv6_gateway.get_text().strip(), ipv6_prefix=int(prefix.get_text().strip() or 64),
                    dns1=dns1.get_text().strip(), dns2=dns2.get_text().strip())
                settings.validate()
            except Exception as exc:  # noqa: BLE001 - ValueError or SadpError, shown in the form
                dialog.show_error(str(exc))
                return
            username, secret = user.get_text().strip() or "admin", password.get_text()
            if not secret:
                dialog.show_error(_("Enter the administrator password."))
                return
            chosen = hik_ops.METHODS[method.get_selected()]
            interface = self.current_interface(device.ip)
            dialog.set_busy(True)

            def done(via, error):
                dialog.set_busy(False)
                if error:
                    dialog.show_error(error)
                    return
                self.remember(username, secret)
                dialog.close()
                self.show_result(_("{mac}: IP changed to {ip} ({via})").format(
                    mac=device.mac, ip=settings.ip if not settings.dhcp else "DHCP", via=via))
                self.forget_device(device)
                for delay in (3, 10, 25):
                    GLib.timeout_add_seconds(delay, self.start_scan)
            run_in_thread(lambda: hik_ops.modify_network(device, username, secret, settings, chosen, interface),
                          done)
        dialog.on_ok = ok
        dialog.present()

    def track_device(self, device: Device, new_ip: str, timeout: float = 90.0) -> None:
        """After a Dahua IP change, find the device on its new address and update its row."""
        if not device.mac:
            GLib.timeout_add_seconds(4, self.start_scan)
            return
        interfaces = list(self.interfaces) or ([device.interface] if device.interface else [])
        started = time.monotonic()
        self.status.set_text(_("Waiting for {mac} at {ip}…").format(mac=device.mac, ip=new_ip or "DHCP"))

        def work():
            if not new_ip:
                time.sleep(5)  # let the device drop the old address and ask DHCP
            return wait_for_device(device.mac, interfaces, new_ip, timeout)

        def done(found, error):
            if error or found is None:
                self.show_result(_("{mac}: the device did not answer at the new address within {seconds} s. "
                                   "Check the address and subnet; refresh the list later.").format(
                                       mac=device.mac, seconds=int(timeout)), error=True)
                self.start_scan()
                return
            self.add_devices([found])
            self.render_devices()
            self.show_result(_("{mac}: the device is back online at {ip} (after {seconds} s)").format(
                mac=device.mac, ip=found.ip, seconds=int(time.monotonic() - started)))
        run_in_thread(work, done)

    def forget_device(self, device: Device) -> None:
        """After an IP change the old row is stale until the next search."""
        self.device_seen[device.key] = 0.0

    def fetch_details_checked(self) -> None:
        devices = [d for d in self.checked_devices() if d.initialized is not False and d.brand == DAHUA]
        title = _("Details over HTTP")
        if not devices:
            self.message(title, _("Check initialized Dahua devices."))
            return
        dialog = FormWindow(self, f"{title} ({len(devices)})", _("Get"))
        dialog.add_wide(label(_("Empty columns (model, serial number, version, mask, gateway, ports, channels) "
                                "are filled from the device HTTP API. One login attempt per device."),
                              css="dim-label", wrap=True))
        user, password = self.credentials_rows(dialog)

        def ok():
            username, secret = user.get_text().strip() or "admin", password.get_text()
            if not secret:
                dialog.show_error(_("Enter the device password"))
                return
            results: list[tuple[Device, str | None, str]] = []
            updated: list[Device] = []
            dialog.set_busy(True)

            def work():
                for device in devices:
                    try:
                        fresh = dahua_ops.fetch_details(device, username, secret)
                    except (DahuaHttpError, OSError) as exc:
                        results.append((device, str(exc), ""))
                        continue
                    updated.append(fresh)
                    results.append((fresh, None, f"{fresh.model} · SN {fresh.serial or '—'} · {fresh.firmware or '—'}"))

            def done(_r, _error):
                dialog.close()
                self.remember(username, secret)
                self.add_devices(updated)
                self.render_devices()
                self.show_batch_results(title, results)
            run_in_thread(work, done)
        dialog.on_ok = ok
        dialog.present()

    def open_batch_ip(self) -> None:
        devices = [d for d in self.checked_devices() if d.initialized is not False]
        skipped = len(self.checked_devices()) - len(devices)
        title = _("Batch IP change")
        if not devices:
            self.message(title, _("None of the checked devices is activated."))
            return
        dialog = FormWindow(self, f"{title} ({len(devices)})", "OK", width=540)
        if skipped:
            dialog.add_wide(label(_("Devices that are not activated are skipped: {count}").format(count=skipped),
                                  css="dim-label"))
        static, dynamic = self._network_mode(dialog, False)
        first = devices[0]
        start = dialog.entry(_("Start IP"), first.ip)
        mask = dialog.entry(_("Subnet mask"), first.subnet_mask or "255.255.255.0")
        gateway = dialog.entry(_("Gateway"), first.gateway)
        user, password = self.credentials_rows(dialog)
        brands = {d.brand for d in devices}
        dahua_method = self._method_dropdown(dialog, DAHUA) if DAHUA in brands else None
        hik_method = self._method_dropdown(dialog, HIKVISION) if HIKVISION in brands else None
        preview = Gtk.TextView(editable=False, monospace=True)
        scroller = Gtk.ScrolledWindow()
        scroller.set_min_content_height(120)
        scroller.set_child(preview)
        dialog.add_wide(label(_("Preview"), css="cs-section"))
        dialog.add_wide(scroller)

        def addresses() -> list[str]:
            if dynamic.get_active():
                return [""] * len(devices)
            return dahua_ops.sequential_ips(start.get_text().strip(), len(devices))

        def update(*_a):
            for entry in (start, mask, gateway):
                entry.set_sensitive(static.get_active())
            lines = []
            try:
                for device, new_ip in zip(devices, addresses()):
                    lines.append(f"{device.brand:9}  {device.ip:15}  {device.mac}  →  {new_ip or 'DHCP'}")
            except (ValueError, dahua_ops.OperationError) as exc:
                lines.append(str(exc))
            preview.get_buffer().set_text("\n".join(lines))
        start.connect("changed", update)
        static.connect("toggled", update)
        update()

        def ok():
            username, secret = user.get_text().strip() or "admin", password.get_text()
            if not secret:
                dialog.show_error(_("Enter the devices password"))
                return
            try:
                ips = addresses()
                if static.get_active():
                    dahua_ops.NetworkSettings(ip=ips[0], mask=mask.get_text().strip(),
                                              gateway=gateway.get_text().strip()).validate()
            except (ValueError, dahua_ops.OperationError) as exc:
                dialog.show_error(str(exc))
                return
            is_dhcp = dynamic.get_active()
            mask_text, gateway_text = mask.get_text().strip(), gateway.get_text().strip()
            dahua_choice = dahua_ops.METHODS[dahua_method.get_selected()] if dahua_method else "auto"
            hik_choice = hik_ops.METHODS[hik_method.get_selected()] if hik_method else "sadp"
            fallback = self.current_interface()
            results: list[tuple[Device, str | None, str]] = []
            dialog.set_busy(True)

            def work():
                for device, new_ip in zip(devices, ips):
                    try:
                        if device.brand == HIKVISION:
                            via = hik_ops.modify_network(device, username, secret, hik_ops.HikNetwork(
                                ip=new_ip, mask=mask_text, gateway=gateway_text, dhcp=is_dhcp,
                                command_port=device.tcp_port or 8000, http_port=device.http_port or 80,
                                ipv6=device.ipv6, ipv6_gateway=device.ipv6_gateway), hik_choice, fallback)
                        else:
                            via = dahua_ops.modify_ip(device, username, secret, dahua_ops.NetworkSettings(
                                ip=new_ip, mask=mask_text, gateway=gateway_text, dhcp=is_dhcp), dahua_choice)
                        results.append((device, None, f"{new_ip or 'DHCP'} ({via})"))
                    except Exception as exc:  # noqa: BLE001 - reported per device
                        results.append((device, str(exc), new_ip))

            def done(_r, error):
                dialog.close()
                if error:
                    self.show_result(error, error=True)
                self.remember(username, secret)
                for device in devices:
                    self.forget_device(device)
                self.show_batch_results(title, results)
                GLib.timeout_add_seconds(4, self.start_scan)
            run_in_thread(work, done)
        dialog.on_ok = ok
        dialog.present()

    def open_initialize(self) -> None:
        devices = [d for d in self.checked_devices() if d.initialized is not True]
        title = _("Activation")
        if not devices:
            self.message(title, _("Check uninitialized (inactive) devices."))
            return
        brands = sorted({d.brand for d in devices})
        dialog = FormWindow(self, f"{title} ({len(devices)})", _("Activate"), width=540)
        dialog.add_wide(label(_("Set the administrator password. You will need it to log in to the device, "
                                "its web interface and the vendor software."), wrap=True))
        dialog.add_row(_("User name"), label("admin"))
        password = dialog.password(_("Password"))
        confirm = dialog.password(_("Confirm"))
        dialog.add_row(_("Strength"), StrengthBar(password, HIKVISION if brands == [HIKVISION] else DAHUA))
        for brand in brands:
            dialog.add_wide(label(f"{brand}: {password_rules(brand)}", css="dim-label", wrap=True))
        email = phone = None
        if DAHUA in brands:
            dialog.add_wide(label(_("Password reset (optional, Dahua)"), css="cs-section"))
            email = dialog.entry(_("E-mail"), "", _("to receive the reset code"))
            phone = dialog.entry(_("Phone"), "", "")
        if HIKVISION in brands:
            dialog.add_wide(label(_("Hikvision: after activation you can set a recovery e-mail and security "
                                    "questions on the «Device settings» page."), css="dim-label", wrap=True))
        dialog.add_wide(label(_("Devices:") + " " + ", ".join(f"{d.brand} {d.ip} ({d.mac})" for d in devices),
                              css="dim-label", wrap=True))
        fallback = self.current_interface()

        def ok():
            secret = password.get_text()
            for brand in brands:
                problem = password_problems_for(brand, secret, confirm.get_text())
                if problem:
                    dialog.show_error(f"{brand}: {problem}" if len(brands) > 1 else problem)
                    return
            mail = email.get_text().strip() if email else ""
            tel = phone.get_text().strip() if phone else ""
            results: list[tuple[Device, str | None, str]] = []
            dialog.set_busy(True)

            def work():
                for device in devices:
                    try:
                        if device.brand == HIKVISION:
                            hik_ops.activate(device, secret, fallback)
                        else:
                            dahua_ops.initialize(device, secret, mail, tel)
                        results.append((device, None, _("Activated")))
                    except Exception as exc:  # noqa: BLE001 - reported per device
                        results.append((device, str(exc), ""))

            def done(_r, error):
                dialog.close()
                if error:
                    self.show_result(error, error=True)
                if any(e is None for _d, e, _i in results):
                    self.remember("admin", secret)
                for device, failure, _info in results:
                    if failure is None and device.brand == HIKVISION and device.sadp is not None:
                        device.sadp.activated = True
                        device.init_status = dahua_discovery.INIT_YES
                self.render_devices()
                self.show_batch_results(title, results)
                GLib.timeout_add_seconds(2, self.start_scan)
            run_in_thread(work, done)
        dialog.on_ok = ok
        dialog.present()

    def open_manual_add(self) -> None:
        dialog = FormWindow(self, _("Add device"), _("Add"))
        brand = Gtk.DropDown.new_from_strings([_("Auto"), DAHUA, HIKVISION])
        dialog.add_row(_("Brand"), brand)
        ip = dialog.entry(_("IP address"), "", "192.168.1.108")
        port = dialog.entry(_("HTTP port"), "80")
        user, password = self.credentials_rows(dialog)
        dialog.add_wide(label(_("Dahua devices are asked over DHIP (UDP 37810) first, then over the HTTP API with "
                                "the credentials. Hikvision devices are checked over ISAPI with the credentials."),
                              css="dim-label", wrap=True))

        def ok():
            address = ip.get_text().strip()
            try:
                ip_range(address, address)
                http_port = int(port.get_text().strip() or "80")
            except ValueError as exc:
                dialog.show_error(str(exc))
                return
            username, secret = user.get_text().strip() or "admin", password.get_text()
            interface = self.current_interface(address)
            wanted = brand.get_selected()
            dialog.set_busy(True)

            def as_dahua() -> Device:
                device = probe_ip(address, interface=interface)
                if device is not None:
                    return device
                if not secret:
                    raise dahua_ops.OperationError(_("The device did not answer DHIP; enter the password for HTTP"))
                info = DahuaHttp(address, http_port, username, secret).system_info()
                return Device(ip=address, http_port=http_port, model=info.get("deviceType", info.get("DeviceType", "")),
                              serial=info.get("serialNumber", ""), firmware=info.get("SoftwareVersion", ""),
                              machine_name=info.get("MachineName", ""), vendor=info.get("Vendor", ""),
                              init_status=2, protocol="HTTP", interface=interface, raw=info)

            def as_hikvision() -> Device:
                from hikvision_api import get_network_interfaces
                if not secret:
                    raise dahua_ops.OperationError(_("Enter the password to check the device over ISAPI"))
                interfaces = get_network_interfaces(address, http_port, username, secret)
                current = next((item for item in interfaces if item.get("ip") == address), interfaces[0])
                return Device(ip=address, http_port=http_port, brand=HIKVISION, vendor=HIKVISION, init_status=2,
                              subnet_mask=current.get("mask", ""), gateway=current.get("gateway", ""),
                              tcp_port=8000, rtsp_port=554, protocol="Manual", interface=interface,
                              dhcp=current.get("dhcp") == "true")

            def work():
                if wanted == 1:
                    return as_dahua()
                if wanted == 2:
                    return as_hikvision()
                try:
                    return as_dahua()
                except Exception as dahua_error:  # noqa: BLE001 - try the other brand
                    try:
                        return as_hikvision()
                    except Exception as hik_error:  # noqa: BLE001
                        raise dahua_ops.OperationError(f"Dahua: {dahua_error}\nHikvision: {hik_error}") from hik_error

            def done(device, error):
                dialog.set_busy(False)
                if error:
                    dialog.show_error(error)
                    return
                dialog.close()
                self.add_devices([device], pin=True)
                self.render_devices()
                self.show_result(_("Device added: {brand} {ip} ({model})").format(
                    brand=device.brand, ip=device.ip, model=device.model or device.protocol))
            run_in_thread(work, done)
        dialog.on_ok = ok
        dialog.present()

    def delete_checked(self) -> None:
        for key in list(self.checked):
            self.devices.pop(key, None)
            self.device_seen.pop(key, None)
            self.pinned.discard(key)
        self.checked.clear()
        self.render_devices()

    def import_csv(self) -> None:
        def load(path: str) -> None:
            try:
                with open(path, encoding="utf-8-sig", newline="") as handle:
                    devices = read_devices_csv(handle)
            except (OSError, csv.Error, UnicodeDecodeError) as exc:
                self.show_result(_("Could not read {path}: {error}").format(path=path, error=exc), error=True)
                return
            for device in devices:
                device.interface = self.current_interface(device.ip)
            self.add_devices(devices, pin=True)
            self.render_devices()
            self.show_result(_("Devices imported: {count}").format(count=len(devices)))
        open_file_dialog(self, _("Import device list"), load, ("*.csv",), "CSV")

    def export_csv(self) -> None:
        def save(path: str) -> None:
            devices = self.checked_devices() or [d for _k, d in sorted(
                self.devices.items(), key=lambda kv: self.numbers.get(kv[0], 0))]
            try:
                with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                    count = write_devices_csv(devices, handle)
            except OSError as exc:
                self.show_result(_("Could not save: {error}").format(error=exc), error=True)
            else:
                self.show_result(_("Devices saved: {count} → {path}").format(count=count, path=path))
        save_file_dialog(self, _("Export device list"), f"camscout-devices-{time.strftime('%Y%m%d-%H%M')}.csv", save)

    def _batch_http(self, title: str, action: Callable[[DahuaHttp], str | None]) -> None:
        devices = [d for d in self.checked_devices() if d.initialized is not False and d.brand == DAHUA]
        if not devices:
            self.message(title, _("Check initialized Dahua devices."))
            return
        dialog = FormWindow(self, f"{title} ({len(devices)})", _("Run"))
        user, password = self.credentials_rows(dialog)

        def ok():
            username, secret = user.get_text().strip() or "admin", password.get_text()
            results: list[tuple[Device, str | None, str]] = []
            dialog.set_busy(True)

            def work():
                dahua_ops.for_each(devices, lambda d: action(dahua_ops.http_client(d, username, secret)),
                                   lambda d, e, i: results.append((d, e, i)))

            def done(_r, _error):
                dialog.close()
                self.remember(username, secret)
                self.show_batch_results(title, results)
            run_in_thread(work, done)
        dialog.on_ok = ok
        dialog.present()

    def batch_sync_time(self) -> None:
        self._batch_http(_("Time sync"),
                         lambda c: c.set_time() or _("Time set: {time}").format(
                             time=f"{datetime.now():%Y-%m-%d %H:%M:%S}"))

    def batch_reboot(self) -> None:
        count = len([d for d in self.checked_devices() if d.brand == DAHUA])
        self.confirm(_("Reboot"), _("Reboot the checked Dahua devices ({count})?").format(count=count),
                     _("Reboot"), lambda: self._batch_http(_("Reboot"), lambda c: c.reboot() or _("Rebooting")))

    def send_checked_to_upgrade(self) -> None:
        for device in self.checked_devices():
            self.add_upgrade_row(device)
        self.go_to_page("upgrade")

    # ------------------------------------------------- Hikvision: NVR cameras
    def open_nvr_sadp(self) -> None:
        dialog = FormWindow(self, _("Cameras behind an NVR (HCNetSDK)"), _("Get list"))
        selected = self.selected_device()
        address = dialog.entry(_("NVR IP"), selected.ip if selected and selected.brand == HIKVISION else "")
        port = dialog.entry(_("SDK port"), "8000")
        user = dialog.entry(_("User name"), self.username or "admin")
        password = dialog.password(_("Password"), self.password)
        dialog.add_wide(label(_("Needs the official HCNetSDK installed separately (HIKVISION_HCNETSDK_LIBRARY)."),
                              css="dim-label", wrap=True))

        def ok():
            nvr = address.get_text().strip()
            try:
                ip_range(nvr, nvr)
                sdk_port = int(port.get_text().strip() or "8000")
                if not 1 <= sdk_port <= 65535:
                    raise ValueError(_("the port must be 1 to 65535"))
            except ValueError as exc:
                dialog.show_error(str(exc))
                return
            username, secret = user.get_text().strip() or "admin", password.get_text()
            if not secret:
                dialog.show_error(_("Enter the recorder password"))
                return
            dialog.set_busy(True)

            def done(cameras, error):
                dialog.set_busy(False)
                if error:
                    dialog.show_error(error)
                    return
                dialog.close()
                self.show_nvr_cameras(nvr, sdk_port, username, secret, cameras or [])
            run_in_thread(lambda: get_nvr_sadp_devices(nvr, sdk_port, username, secret), done)
        dialog.on_ok = ok
        dialog.present()

    def show_nvr_cameras(self, address: str, port: int, username: str, password: str,
                         cameras: list[dict[str, str]]) -> None:
        if not cameras:
            self.message(_("Cameras behind an NVR"), _("The recorder did not return cameras over the SADP API."))
            return
        dialog = FormWindow(self, _("Cameras behind an NVR") + f" · {address}", _("Close"), width=760, cancel=False)
        dialog.add_wide(label(_("Cameras in the recorder SADP list: {count}").format(count=len(cameras))))
        listbox = Gtk.ListBox()
        listbox.add_css_class("boxed-list")
        for camera in cameras:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.set_margin_top(8)
            box.set_margin_bottom(8)
            box.set_margin_start(12)
            box.set_margin_end(12)
            box.append(label(camera.get("model") or _("IP camera"), css="heading"))
            box.append(label(f"{camera.get('ip', '')}  ·  {camera.get('mac', '')}  ·  {_('port')} "
                             f"{camera.get('port', '')}  ·  {camera.get('serial') or '—'}", css="dim-label"))
            row = Gtk.ListBoxRow()
            row.set_child(box)
            row.camera_info = camera
            listbox.append(row)
        scroller = Gtk.ScrolledWindow()
        scroller.set_min_content_height(360)
        scroller.set_child(listbox)
        dialog.add_wide(scroller)
        edit = Gtk.Button(label=_("Change IP / gateway / DNS / SDK port…"))
        edit.set_sensitive(False)
        edit.set_halign(Gtk.Align.START)
        listbox.connect("row-selected", lambda _b, row: edit.set_sensitive(row is not None))
        edit.connect("clicked", lambda *_a: listbox.get_selected_row() and self.open_nvr_camera_network(
            address, port, username, password, listbox.get_selected_row().camera_info))
        dialog.add_wide(edit)
        dialog.present()

    def open_nvr_camera_network(self, nvr_address: str, nvr_port: int, nvr_username: str, nvr_password: str,
                                camera: dict[str, str]) -> None:
        dialog = FormWindow(self, _("Camera network settings") + f" · {camera.get('ip', '')}",
                            _("Change through the NVR"), width=500)
        dialog.add_wide(label(_("The current camera password is needed for the SADP check. After an IP change the "
                                "recorder may lose the camera."), css="cs-warn", wrap=True))
        static, dynamic = self._network_mode(dialog, camera.get("dhcp") == "true")
        fields = {key: dialog.entry(title, camera.get(key, "")) for key, title in (
            ("ip", _("New IPv4 address")), ("mask", _("Subnet mask")), ("gateway", _("Gateway")),
            ("dns1", "DNS 1"), ("dns2", "DNS 2"), ("port", _("Device port")))}
        camera_password = dialog.password(_("Camera password"))

        def sync(*_a):
            for key in ("ip", "mask", "gateway", "dns1", "dns2"):
                fields[key].set_sensitive(static.get_active())
        static.connect("toggled", sync)
        sync()

        def ok():
            values = {key: entry.get_text().strip() for key, entry in fields.items()}
            secret = camera_password.get_text()
            if not secret:
                dialog.show_error(_("Enter the current camera password"))
                return
            try:
                new_port = int(values["port"])
                if not 1 <= new_port <= 65535:
                    raise ValueError(_("the port must be 1 to 65535"))
                if static.get_active():
                    dahua_ops.NetworkSettings(ip=values["ip"], mask=values["mask"], gateway=values["gateway"],
                                              dns1=values["dns1"], dns2=values["dns2"]).validate()
            except (ValueError, dahua_ops.OperationError) as exc:
                dialog.show_error(str(exc))
                return
            dialog.set_busy(True)

            def done(_r, error):
                dialog.set_busy(False)
                if error:
                    dialog.show_error(error)
                    return
                dialog.close()
                self.show_result(_("The recorder accepted the changes. The camera may appear at its new address."))
            run_in_thread(lambda: modify_nvr_camera_ip(
                nvr_address, nvr_port, nvr_username, nvr_password, camera["mac"], secret, values["ip"],
                values["mask"], values["gateway"], values["dns1"], values["dns2"], dynamic.get_active(), new_port),
                done)
        dialog.on_ok = ok
        dialog.present()

    # ------------------------------------------------------------ upgrade page
    def build_upgrade_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.add_css_class("cs-toolbar")
        add = Gtk.Button(label=_("Add checked"))
        add.set_tooltip_text(_("Add the devices checked on the «Devices» page"))
        add.connect("clicked", lambda *_a: [self.add_upgrade_row(d) for d in self.checked_devices()])
        toolbar.append(add)
        choose = Gtk.Button(label=_("Firmware file…"))
        choose.connect("clicked", lambda *_a: self.choose_firmware())
        toolbar.append(choose)
        self.firmware_label = label(_("No file selected"), css="dim-label")
        self.firmware_label.set_hexpand(True)
        self.firmware_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        toolbar.append(self.firmware_label)
        clear = Gtk.Button(label=_("Clear"))
        clear.connect("clicked", lambda *_a: self.clear_upgrade_rows())
        toolbar.append(clear)
        self.upgrade_button = Gtk.Button(label=_("Upgrade"))
        self.upgrade_button.add_css_class("suggested-action")
        self.upgrade_button.connect("clicked", lambda *_a: self.start_upgrade())
        toolbar.append(self.upgrade_button)
        page.append(toolbar)
        page.append(Gtk.Separator())
        page.append(self._upgrade_grid_row("IP", _("Model"), _("Version"), None, _("Status"), header=True))
        self.upgrade_list = Gtk.ListBox()
        self.upgrade_list.set_selection_mode(Gtk.SelectionMode.NONE)
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_child(self.upgrade_list)
        page.append(scroller)
        note = label(_("Firmware upgrade over the network is available for Dahua devices (.bin). Do not switch off "
                       "power or the network during the upgrade. Up to 4 devices are upgraded at once; after a "
                       "successful upgrade the device reboots."), css="dim-label", wrap=True)
        note.set_margin_start(12)
        note.set_margin_end(12)
        note.set_margin_bottom(8)
        page.append(note)
        return page

    @staticmethod
    def _upgrade_grid_row(ip: str, model: str, version: str, progress: Gtk.Widget | None, state: str,
                          header: bool = False) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.set_margin_start(12)
        row.set_margin_end(12)
        row.set_margin_top(6)
        row.set_margin_bottom(6)
        for text, chars in ((ip, 15), (model, 26), (version, 34)):
            cell = label(text, css="heading" if header else None)
            cell.set_width_chars(chars)
            cell.set_max_width_chars(chars)
            cell.set_ellipsize(Pango.EllipsizeMode.END)
            cell.set_tooltip_text(text or None)
            row.append(cell)
        if progress is None:
            progress = label(_("Progress"), css="heading" if header else None)
        progress.set_size_request(200, -1)
        progress.set_valign(Gtk.Align.CENTER)
        row.append(progress)
        status = label(state, css="heading" if header else None)
        status.set_hexpand(True)
        row.append(status)
        row.status_label = status
        return row

    def add_upgrade_row(self, device: Device) -> None:
        if device.brand != DAHUA:
            self.show_result(_("{ip}: firmware upgrade is available for Dahua devices only").format(ip=device.ip),
                             error=True)
            return
        if device.key in self.upgrade_rows:
            return
        bar = Gtk.ProgressBar()
        bar.set_show_text(True)
        row = self._upgrade_grid_row(device.ip, device.model, device.firmware, bar, _("Waiting"))
        remove = Gtk.Button.new_from_icon_name("window-close-symbolic")
        remove.add_css_class("flat")
        remove.set_tooltip_text(_("Remove from the list"))
        remove.connect("clicked", lambda *_a: self.remove_upgrade_row(device.key))
        row.append(remove)
        self.upgrade_list.append(row)
        self.upgrade_rows[device.key] = {"device": device, "row": row, "bar": bar, "busy": False, "remove": remove}

    def remove_upgrade_row(self, key: str) -> None:
        entry = self.upgrade_rows.get(key)
        if entry is None or entry["busy"]:
            return
        self.upgrade_list.remove(entry["row"].get_parent())
        del self.upgrade_rows[key]

    def clear_upgrade_rows(self) -> None:
        for key in list(self.upgrade_rows):
            self.remove_upgrade_row(key)

    def choose_firmware(self) -> None:
        def chosen(path: str) -> None:
            self.firmware_path = path
            size = os.path.getsize(path) / 1024 / 1024
            self.firmware_label.set_text(f"{path}  ({size:.1f} MB)")
        open_file_dialog(self, _("Firmware file"), chosen, ("*.bin", "*.BIN"), _("Dahua firmware (*.bin)"))

    def start_upgrade(self) -> None:
        pending = [entry for entry in self.upgrade_rows.values() if not entry["busy"]]
        title = _("Firmware upgrade")
        if not pending:
            self.message(title, _("Add devices: check them on the «Devices» page and press «Add checked»."))
            return
        if not self.firmware_path or not os.path.isfile(self.firmware_path):
            self.message(title, _("Choose a firmware file."))
            return
        dialog = FormWindow(self, f"{title} ({len(pending)})", _("Upgrade"))
        dialog.add_wide(label(_("File: {name}").format(name=os.path.basename(self.firmware_path)), wrap=True))
        user, password = self.credentials_rows(dialog)

        def ok():
            username, secret = user.get_text().strip() or "admin", password.get_text()
            dialog.close()
            self.remember(username, secret)
            executor = ThreadPoolExecutor(max_workers=4)
            for entry in pending:
                entry["busy"] = True
                entry["remove"].set_sensitive(False)
                entry["row"].status_label.set_text(_("Queued"))
                entry["bar"].set_fraction(0)
                executor.submit(self.upgrade_worker, entry, username, secret, self.firmware_path)
            executor.shutdown(wait=False)
        dialog.on_ok = ok
        dialog.present()

    def upgrade_worker(self, entry: dict, username: str, secret: str, path: str) -> None:
        device: Device = entry["device"]

        def progress(state: str, percent: float) -> None:
            text = _(UPGRADE_STATES.get(state, state))
            GLib.idle_add(lambda: (entry["bar"].set_fraction(min(1.0, percent / 100)),
                                   entry["bar"].set_text(f"{percent:.0f}%"),
                                   entry["row"].status_label.set_text(text), False)[-1])
        try:
            state = dahua_ops.http_client(device, username, secret, timeout=15).upgrade(path, progress)
            result, error = _(UPGRADE_STATES.get(state, state)), None
        except (DahuaHttpError, OSError) as exc:
            result, error = "", str(exc)

        def finish():
            entry["busy"] = False
            entry["remove"].set_sensitive(True)
            if error:
                entry["row"].status_label.set_text(_("Error: {error}").format(error=error))
                self.log(_("Upgrade {ip}: {result}").format(ip=device.ip, result=error), error=True)
            else:
                entry["bar"].set_fraction(1.0)
                entry["row"].status_label.set_text(result)
                self.log(_("Upgrade {ip}: {result}").format(ip=device.ip, result=result))
            return False
        GLib.idle_add(finish)

    # ---------------------------------------------------------------- log page
    def build_log_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.add_css_class("cs-toolbar")
        toolbar.append(label(_("Operation log"), css="heading"))
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        toolbar.append(spacer)
        self.trace_check = Gtk.CheckButton(label=_("Record DHIP packets"))
        self.trace_check.set_tooltip_text(_("For debugging on a real Dahua device: keeps every search and multicast "
                                            "packet (clear-text passwords are replaced with ***)"))
        self.trace_check.connect("toggled", self.toggle_trace)
        toolbar.append(self.trace_check)
        self.trace_count = label("", css="dim-label")
        toolbar.append(self.trace_count)
        save_trace = Gtk.Button(label=_("Save packets…"))
        save_trace.connect("clicked", lambda *_a: self.save_trace())
        toolbar.append(save_trace)
        clear = Gtk.Button(label=_("Clear"))
        clear.connect("clicked", lambda *_a: self.log_buffer.set_text(""))
        toolbar.append(clear)
        save = Gtk.Button(label=_("Save…"))
        save.connect("clicked", lambda *_a: self.save_log())
        toolbar.append(save)
        page.append(toolbar)
        page.append(Gtk.Separator())
        view = Gtk.TextView(buffer=self.log_buffer, editable=False, monospace=True)
        view.set_left_margin(10)
        view.set_top_margin(6)
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_child(view)
        page.append(scroller)
        return page

    TRACE_LIMIT = 20000

    def on_packet(self, direction: str, peer: str, data: bytes) -> None:
        entry = format_packet(direction, peer, data)  # called from worker threads
        with self.trace_lock:
            self.trace_entries.append(entry)
            if len(self.trace_entries) > self.TRACE_LIMIT:
                del self.trace_entries[: len(self.trace_entries) - self.TRACE_LIMIT]

    def toggle_trace(self, check: Gtk.CheckButton) -> None:
        if check.get_active():
            dahua_discovery.packet_trace = self.on_packet
            self.log(_("DHIP packet recording on"))
            GLib.timeout_add_seconds(1, self.update_trace_count)
        else:
            dahua_discovery.packet_trace = None
            self.log(_("DHIP packet recording off, packets: {count}").format(count=len(self.trace_entries)))
        self.update_trace_count()

    def update_trace_count(self) -> bool:
        with self.trace_lock:
            count = len(self.trace_entries)
        self.trace_count.set_text(_("packets: {count}").format(count=count)
                                  if count or self.trace_check.get_active() else "")
        return self.trace_check.get_active()

    def save_trace(self) -> None:
        with self.trace_lock:
            entries = list(self.trace_entries)
        if not entries:
            self.message(_("DHIP packets"), _("No packets. Turn on «Record DHIP packets», repeat the operation "
                                               "(search, activation, IP change or password reset) and save the file."))
            return

        def save(path: str) -> None:
            try:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.writelines(entries)
            except OSError as exc:
                self.show_result(_("Could not save: {error}").format(error=exc), error=True)
            else:
                self.show_result(_("Packets saved: {count} → {path}").format(count=len(entries), path=path))
        save_file_dialog(self, _("Save DHIP packets"), f"camscout-dhip-{time.strftime('%Y%m%d-%H%M')}.txt", save)

    def save_log(self) -> None:
        def save(path: str) -> None:
            text = self.log_buffer.get_text(self.log_buffer.get_start_iter(), self.log_buffer.get_end_iter(), False)
            try:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(text)
            except OSError as exc:
                self.show_result(_("Could not save: {error}").format(error=exc), error=True)
        save_file_dialog(self, _("Save log"), f"camscout-{time.strftime('%Y%m%d-%H%M')}.log", save)

    # -------------------------------------------------------- preferences page
    def build_preferences_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.add_css_class("cs-form")
        page.append(label(_("Preferences"), css="title-3"))
        grid = Gtk.Grid(row_spacing=10, column_spacing=16)
        grid.attach(label(_("Language")), 0, 0, 1, 1)
        self.language_combo = Gtk.DropDown.new_from_strings([name for _code, name in LANGUAGES])
        codes = [code for code, _name in LANGUAGES]
        self.language_combo.set_selected(codes.index(i18n.language()) if i18n.language() in codes else 0)
        self.language_combo.connect("notify::selected", self.on_language_changed)
        grid.attach(self.language_combo, 1, 0, 1, 1)
        page.append(grid)
        page.append(label(_("The interface switches to the chosen language at once; the choice is saved for the "
                            "next start."), css="dim-label", wrap=True))
        page.append(Gtk.Separator())
        version = app_version()
        page.append(label(f"CamScout {version}".strip(), css="heading"))
        page.append(label(_("Discovery, activation, network settings, password reset and live view for Dahua and "
                            "Hikvision devices. An independent project, not affiliated with the device makers."),
                          css="dim-label", wrap=True))
        page.append(label(_("Settings file: {path}").format(path=i18n.settings_path()), css="dim-label",
                          selectable=True, wrap=True))
        return page

    def on_language_changed(self, combo, _pspec) -> None:
        code = LANGUAGES[combo.get_selected()][0]
        if code == i18n.language():
            return
        try:
            i18n.save_settings({"language": code})
        except OSError as exc:
            self.log(_("Could not save the settings: {error}").format(error=exc), error=True)
        i18n.set_language(code)
        self.get_application().rebuild_window(self)


class CamScoutApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        GLib.set_application_name(APP_TITLE)

    def do_activate(self) -> None:
        window = self.props.active_window or MainWindow(self)
        window.present()

    def rebuild_window(self, old: MainWindow) -> None:
        """Re-create the window in the new language, keeping the device list."""
        new = MainWindow(self, previous=old)
        width, height = old.get_width(), old.get_height()
        if width and height:
            new.set_default_size(width, height)
        if old.is_maximized():
            new.maximize()
        new.go_to_page("prefs")
        new.present()
        old.close()


def main() -> int:
    return CamScoutApp().run([sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main())
