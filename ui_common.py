"""GTK helpers shared by the CamScout windows: style, small widgets, forms."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

import dahua_ops  # noqa: E402
import sadp_protocol  # noqa: E402
from dahua_api import DahuaHttpError  # noqa: E402
from dahua_protocol import DahuaProtocolError  # noqa: E402
from dahua_protocol import password_strength as dahua_strength  # noqa: E402
from devices import HIKVISION  # noqa: E402
from hikvision_api import HikvisionError  # noqa: E402
from i18n import tr as _  # noqa: E402
from sadp_protocol import SadpError  # noqa: E402

try:
    import qrcode  # optional: draws the password-reset and P2P QR codes
except ImportError:  # pragma: no cover - depends on the system
    qrcode = None

# Lime / light green accent («салатовый»). Dark text keeps it readable on the accent.
LIME = "#8cc63f"
LIME_HOVER = "#9dd45a"
LIME_ACTIVE = "#79b22c"
LIME_BORDER = "#6c9f2b"
LIME_TEXT = "#4f8a10"
ON_LIME = "#15230a"

APP_CSS = f"""
@define-color accent_bg_color {LIME};
@define-color accent_fg_color {ON_LIME};
@define-color accent_color {LIME_TEXT};
@define-color theme_selected_bg_color {LIME};
@define-color theme_selected_fg_color {ON_LIME};

.cs-nav {{ background-color: alpha({LIME}, 0.09); }}
.cs-nav row {{ padding: 10px 6px; border-radius: 8px; margin: 2px 6px; }}
.cs-nav row:selected {{ background-color: {LIME}; color: {ON_LIME}; }}
.cs-nav row:selected image, .cs-nav row:selected label {{ color: {ON_LIME}; }}
.cs-nav-title {{ font-weight: bold; font-size: 0.85em; }}
.cs-logo {{ font-weight: 800; font-size: 1.3em; color: {LIME_TEXT}; }}
.cs-toolbar {{ padding: 8px 10px; }}
.cs-statusbar {{ padding: 4px 10px; }}
.cs-section {{ font-weight: bold; font-size: 1.05em; margin-top: 6px; }}
.cs-table {{ font-size: 0.95em; }}
.cs-form {{ padding: 16px 18px; }}
.cs-qr {{ background-color: white; }}
.cs-ok {{ color: #3d8b17; }}
.cs-bad {{ color: #c62828; }}
.cs-warn {{ color: #b86e00; }}
.cs-badge {{ border-radius: 6px; padding: 1px 8px; font-weight: bold; font-size: 0.85em; }}
.cs-badge-dahua {{ background-color: alpha({LIME}, 0.30); }}
.cs-badge-hikvision {{ background-color: alpha(#7d8590, 0.28); }}

button.suggested-action {{
  background-image: none; background-color: {LIME}; color: {ON_LIME}; border-color: {LIME_BORDER};
}}
button.suggested-action:hover {{ background-color: {LIME_HOVER}; }}
button.suggested-action:active, button.suggested-action:checked {{ background-color: {LIME_ACTIVE}; }}
button.suggested-action:disabled {{ background-color: alpha({LIME}, 0.45); color: alpha({ON_LIME}, 0.55); }}
button.suggested-action label, button.suggested-action image {{ color: {ON_LIME}; }}
check:checked, radio:checked {{
  background-image: none; background-color: {LIME}; color: {ON_LIME}; border-color: {LIME_BORDER};
}}
switch:checked {{ background-color: {LIME}; }}
levelbar block.filled {{ background-color: {LIME}; }}
levelbar block.filled.low {{ background-color: #e0a030; }}
progressbar progress {{ background-color: {LIME}; border-color: {LIME_BORDER}; }}
columnview row:selected, listview row:selected, list row:selected {{
  background-color: alpha({LIME}, 0.35); color: inherit;
}}
notebook header tab:checked {{ box-shadow: inset 0 -3px {LIME}; }}
entry:focus-within, spinbutton:focus-within, dropdown:focus-within > button {{
  outline-color: alpha({LIME}, 0.75);
}}
link, button.link {{ color: {LIME_TEXT}; }}
"""

ASSET_DIR = Path(__file__).resolve().parent / "data"
ICON_FILE = ASSET_DIR / "io.github.Valeritylive.CamScout.svg"

# Errors shown to the user instead of a silent thread death.
KNOWN_ERRORS = (DahuaHttpError, DahuaProtocolError, dahua_ops.OperationError, HikvisionError, SadpError,
                OSError, ValueError)


def install_css() -> None:
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    if hasattr(provider, "load_from_string"):  # GTK >= 4.12
        provider.load_from_string(APP_CSS)
    else:
        provider.load_from_data(APP_CSS.encode())
    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def run_in_thread(work: Callable[[], Any], done: Callable[[Any, str | None], None]) -> None:
    """Run *work* off the GTK thread, then call done(result, error) on it."""
    def worker():
        try:
            result, error = work(), None
        except KNOWN_ERRORS as exc:
            result, error = None, str(exc)
        except Exception as exc:  # noqa: BLE001
            result, error = None, f"{exc.__class__.__name__}: {exc}"
        GLib.idle_add(lambda: (done(result, error), False)[1])
    threading.Thread(target=worker, daemon=True).start()


def clean_launch_context() -> Gio.AppLaunchContext:
    """Launch context without the bundled-runtime variables set by the launchers.

    The browser must not inherit LD_LIBRARY_PATH and friends pointing into the
    packaged Conda prefix, or it loads our libraries instead of its own.
    """
    context = Gio.AppLaunchContext()
    for name in os.environ.get("CAMSCOUT_SET_VARS", "").split():
        context.unsetenv(name)
    if os.environ.get("CAMSCOUT_ORIG_LD_LIBRARY_PATH"):
        context.setenv("LD_LIBRARY_PATH", os.environ["CAMSCOUT_ORIG_LD_LIBRARY_PATH"])
    if "CAMSCOUT_ORIG_PATH" in os.environ:
        context.setenv("PATH", os.environ["CAMSCOUT_ORIG_PATH"])
    if "CAMSCOUT_ORIG_XDG_DATA_DIRS" in os.environ:
        if os.environ["CAMSCOUT_ORIG_XDG_DATA_DIRS"]:
            context.setenv("XDG_DATA_DIRS", os.environ["CAMSCOUT_ORIG_XDG_DATA_DIRS"])
        else:
            context.unsetenv("XDG_DATA_DIRS")
    return context


def label(text: str = "", xalign: float = 0.0, css: str | None = None, wrap: bool = False,
          selectable: bool = False) -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=xalign)
    widget.set_wrap(wrap)
    if wrap:
        widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    widget.set_selectable(selectable)
    if css:
        widget.add_css_class(css)
    return widget


def password_entry() -> Gtk.PasswordEntry:
    widget = Gtk.PasswordEntry()
    widget.set_show_peek_icon(True)
    widget.set_hexpand(True)
    return widget


def flat_button(text: str, callback: Callable[[], None]) -> Gtk.Button:
    button = Gtk.Button(label=text)
    button.add_css_class("flat")
    button.get_child().set_xalign(0)
    button.connect("clicked", lambda *_: callback())
    return button


def qr_matrix_for(text: str) -> list[list[bool]] | None:
    if not text or qrcode is None:
        return None
    code = qrcode.QRCode(border=2)
    code.add_data(text)
    code.make(fit=True)
    return code.get_matrix()


def draw_matrix(cr, width: int, height: int, matrix: list[list[bool]] | None, placeholder: str = "QR") -> None:
    cr.set_source_rgb(1, 1, 1)
    cr.paint()
    if not matrix:
        cr.set_source_rgb(0.6, 0.6, 0.6)
        cr.select_font_face("Sans")
        cr.set_font_size(14)
        cr.move_to(max(8, width / 2 - 4 * len(placeholder)), height / 2)
        cr.show_text(placeholder)
        return
    size = len(matrix)
    cell = min(width, height) / size
    cr.set_source_rgb(0, 0, 0)
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                cr.rectangle(x * cell, y * cell, cell + 0.3, cell + 0.3)
    cr.fill()


def changed_values(text: str, original: dict[str, str]) -> dict[str, str]:
    """Lines «name=value» of an editor that differ from what was read."""
    changed: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key and original.get(key) != value:
            changed[key] = value
    return changed


def password_rules(brand: str) -> str:
    if brand == HIKVISION:
        return _("8–16 characters, at least two kinds: digits, lower case, upper case, symbols. "
                 "A strong password has three kinds or more.")
    return _("8–32 characters, at least two kinds: digits, letters, symbols. Not allowed: ' \" ; : &")


def password_problems_for(brand: str, password: str, confirm: str | None = None) -> str | None:
    """Brand rules for a new password; returns the problem text or None."""
    if confirm is not None and password != confirm:
        return _("Passwords do not match")
    if brand == HIKVISION:
        try:
            sadp_protocol.check_new_password(password)
        except SadpError as exc:
            return str(exc)
        return None
    from dahua_protocol import password_problems
    problems = password_problems(password)
    if problems:
        return _("Unsuitable password:") + " " + "; ".join(problems)
    return None


class StrengthBar(Gtk.Box):
    """Password strength meter; follows Dahua or Hikvision rules."""

    def __init__(self, entry: Gtk.PasswordEntry, brand: str = "Dahua"):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.brand = brand
        self.bar = Gtk.LevelBar(min_value=0, max_value=3)
        self.bar.set_hexpand(True)
        self.text = label("", css="dim-label")
        self.append(self.bar)
        self.append(self.text)
        entry.connect("changed", self.update)

    def update(self, entry) -> None:
        text = entry.get_text()
        if self.brand == HIKVISION:
            if not text:
                self.bar.set_value(0)
                self.text.set_text("")
                return
            level = sadp_protocol.password_strength(text)
            self.bar.set_value(level)
            self.text.set_text(_(sadp_protocol.PASSWORD_LEVELS[level]))
            return
        value, words = dahua_strength(text)
        self.bar.set_value(value)
        self.text.set_text(words)


class FormWindow(Gtk.Window):
    """Small modal form with OK/Cancel, an error line and a busy state."""

    def __init__(self, parent: Gtk.Window, title: str, ok_label: str = "OK", width: int = 440,
                 cancel: bool = True):
        super().__init__(title=title, transient_for=parent, modal=True)
        self.set_default_size(width, -1)
        self.set_resizable(True)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.add_css_class("cs-form")
        self.set_child(outer)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.content.set_vexpand(True)
        outer.append(self.content)
        self.grid = Gtk.Grid(row_spacing=8, column_spacing=12)
        self.content.append(self.grid)
        self.row = 0
        self.error = label("", css="error", wrap=True)
        self.error.set_visible(False)
        outer.append(self.error)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        self.spinner = Gtk.Spinner()
        buttons.append(self.spinner)
        if cancel:
            self.cancel_button = Gtk.Button(label=_("Cancel"))
            self.cancel_button.connect("clicked", lambda *_a: self.close())
            buttons.append(self.cancel_button)
        self.ok_button = Gtk.Button(label=ok_label)
        self.ok_button.add_css_class("suggested-action")
        self.ok_button.connect("clicked", lambda *_a: self.on_ok() if self.on_ok else self.close())
        buttons.append(self.ok_button)
        outer.append(buttons)
        self.on_ok: Callable[[], None] | None = None
        self.set_default_widget(self.ok_button)

    def add_row(self, text: str, widget: Gtk.Widget) -> Gtk.Widget:
        caption = label(text)
        caption.set_valign(Gtk.Align.CENTER)
        self.grid.attach(caption, 0, self.row, 1, 1)
        widget.set_hexpand(True)
        self.grid.attach(widget, 1, self.row, 1, 1)
        self.row += 1
        return widget

    def add_wide(self, widget: Gtk.Widget) -> Gtk.Widget:
        self.grid.attach(widget, 0, self.row, 2, 1)
        self.row += 1
        return widget

    def entry(self, text: str, value: str = "", placeholder: str = "") -> Gtk.Entry:
        entry = Gtk.Entry(text=value)
        entry.set_activates_default(True)
        if placeholder:
            entry.set_placeholder_text(placeholder)
        return self.add_row(text, entry)

    def password(self, text: str, value: str = "") -> Gtk.PasswordEntry:
        secret = password_entry()
        secret.set_text(value)
        secret.set_property("activates-default", True)
        return self.add_row(text, secret)

    def show_error(self, text: str | None) -> None:
        self.error.set_text(text or "")
        self.error.set_visible(bool(text))

    def set_busy(self, busy: bool) -> None:
        self.ok_button.set_sensitive(not busy)
        self.grid.set_sensitive(not busy)
        if busy:
            self.spinner.start()
            self.show_error(None)
        else:
            self.spinner.stop()


def save_file_dialog(parent: Gtk.Window, title: str, name: str, on_path: Callable[[str], None]) -> None:
    chooser = Gtk.FileChooserNative.new(title, parent, Gtk.FileChooserAction.SAVE, _("Save"), _("Cancel"))
    chooser.set_current_name(name)

    def response(ch, response_id):
        if response_id == Gtk.ResponseType.ACCEPT and ch.get_file() is not None:
            on_path(ch.get_file().get_path())
        ch.destroy()
    chooser.connect("response", response)
    chooser.show()


def open_file_dialog(parent: Gtk.Window, title: str, on_path: Callable[[str], None],
                     patterns: tuple[str, ...] = (), filter_name: str = "") -> None:
    chooser = Gtk.FileChooserNative.new(title, parent, Gtk.FileChooserAction.OPEN, _("Open"), _("Cancel"))
    if patterns:
        file_filter = Gtk.FileFilter()
        file_filter.set_name(filter_name or " ".join(patterns))
        for pattern in patterns:
            file_filter.add_pattern(pattern)
        chooser.add_filter(file_filter)

    def response(ch, response_id):
        if response_id == Gtk.ResponseType.ACCEPT and ch.get_file() is not None:
            on_path(ch.get_file().get_path())
        ch.destroy()
    chooser.connect("response", response)
    chooser.show()
