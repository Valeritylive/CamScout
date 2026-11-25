"""RTSP live preview for Dahua and Hikvision cameras (GStreamer)."""

from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
try:
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
except (ImportError, ValueError):
    Gst = None
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from dahua_discovery import Device  # noqa: E402
from devices import rtsp_path  # noqa: E402
from i18n import tr as _  # noqa: E402
from ui_common import label, password_entry  # noqa: E402


class LiveViewWindow(Gtk.Window):
    """RTSP live view; credentials stay out of the stream URL."""

    def __init__(self, parent: Gtk.Window, device: Device, username: str = "admin", password: str = ""):
        super().__init__(title=_("Live view") + f" · {device.brand} · {device.ip}", transient_for=parent)
        self.set_default_size(980, 700)
        self.device = device
        self.pipeline = None
        self.bus = None
        self.bus_handler = 0
        self._frame_lock = threading.Lock()
        self._pending_frame = None
        self._frame_dispatch_scheduled = False

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_margin_top(14)
        root.set_margin_bottom(14)
        root.set_margin_start(14)
        root.set_margin_end(14)
        self.set_child(root)

        controls = Gtk.Grid(column_spacing=12, row_spacing=8)
        root.append(controls)
        controls.attach(label(f"{device.brand} · {device.model or device.device_class} · {device.ip}",
                              css="heading"), 0, 0, 4, 1)
        controls.attach(label(_("User")), 0, 1, 1, 1)
        self.username = Gtk.Entry(text=username or "admin")
        self.username.set_hexpand(True)
        controls.attach(self.username, 1, 1, 1, 1)
        controls.attach(label(_("Password")), 2, 1, 1, 1)
        self.password = password_entry()
        self.password.set_text(password)
        self.password.connect("activate", self.connect_stream)
        controls.attach(self.password, 3, 1, 1, 1)
        controls.attach(label(_("RTSP port")), 0, 2, 1, 1)
        self.port = Gtk.Entry(text=str(device.rtsp_port or 554))
        self.port.set_input_purpose(Gtk.InputPurpose.DIGITS)
        controls.attach(self.port, 1, 2, 1, 1)
        controls.attach(label(_("Channel")), 2, 2, 1, 1)
        self.channel = Gtk.SpinButton.new_with_range(1, 256, 1)
        controls.attach(self.channel, 3, 2, 1, 1)
        controls.attach(label(_("Stream")), 0, 3, 1, 1)
        self.stream = Gtk.DropDown.new_from_strings([_("Main stream"), _("Sub stream")])
        controls.attach(self.stream, 1, 3, 1, 1)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.connect_button = Gtk.Button(label=_("Connect"))
        self.connect_button.add_css_class("suggested-action")
        self.connect_button.connect("clicked", self.connect_stream)
        actions.append(self.connect_button)
        self.stop_button = Gtk.Button(label=_("Stop"))
        self.stop_button.set_sensitive(False)
        self.stop_button.connect("clicked", lambda *_a: self.stop_stream())
        actions.append(self.stop_button)
        self.status = label(_("Enter the camera credentials"), css="dim-label")
        self.status.set_hexpand(True)
        actions.append(self.status)
        root.append(actions)

        self.picture = Gtk.Picture()
        self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.picture.set_hexpand(True)
        self.picture.set_vexpand(True)
        root.append(self.picture)
        self.connect("close-request", self.on_close_request)

        if Gst is None:
            self.connect_button.set_sensitive(False)
            self.status.set_text(_("Install GStreamer and python3-gst-1.0 for live view."))

    def stream_path(self) -> str:
        return rtsp_path(self.device, int(self.channel.get_value()), self.stream.get_selected() == 1)

    def connect_stream(self, *_args) -> None:
        if Gst is None:
            return
        username = self.username.get_text().strip()
        password = self.password.get_text()
        try:
            port = int(self.port.get_text().strip())
            if not 1 <= port <= 65535:
                raise ValueError(_("the port must be 1 to 65535"))
        except ValueError as exc:
            self.status.set_text(_("Check the RTSP port: {error}").format(error=exc))
            return
        if not username or not password:
            self.status.set_text(_("Enter the camera user name and password"))
            return

        self.stop_stream(clear_picture=False)
        playbin = Gst.ElementFactory.make("playbin", "camscout-live-view")
        video_sink = Gst.ElementFactory.make("appsink", "gtk-video-frames")
        audio_sink = Gst.ElementFactory.make("fakesink", "discard-audio")
        if not playbin or not video_sink or not audio_sink:
            self.status.set_text(_("GStreamer playbin/appsink plugins not found."))
            return

        video_sink.set_property("caps", Gst.Caps.from_string("video/x-raw,format=RGB,framerate=15/1"))
        video_sink.set_property("emit-signals", True)
        video_sink.set_property("sync", False)
        video_sink.set_property("max-buffers", 1)
        video_sink.set_property("drop", True)
        video_sink.connect("new-sample", self.on_new_sample)
        playbin.set_property("video-sink", video_sink)
        playbin.set_property("audio-sink", audio_sink)
        playbin.connect("source-setup", self.configure_rtsp_source, username, password)
        path = self.stream_path()
        playbin.set_property("uri", f"rtsp://{self.device.ip}:{port}{path}")

        self.pipeline = playbin
        self.bus = playbin.get_bus()
        self.bus.add_signal_watch()
        self.bus_handler = self.bus.connect("message", self.on_bus_message)
        self.status.set_text(_("Connecting to {path}…").format(path=path))
        self.connect_button.set_sensitive(False)
        self.stop_button.set_sensitive(True)
        result = playbin.set_state(Gst.State.PLAYING)
        if result == Gst.StateChangeReturn.FAILURE:
            self.status.set_text(_("GStreamer could not start the stream."))
            self.stop_stream(clear_picture=False)

    @staticmethod
    def configure_rtsp_source(_playbin, source, username: str, password: str) -> None:
        # RTSP credentials are properties on rtspsrc, never embedded in the URI.
        for name, value in (("user-id", username), ("user-pw", password),
                            ("protocols", 4), ("latency", 200)):
            if source.find_property(name) is not None:
                source.set_property(name, value)

    def on_new_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        structure = sample.get_caps().get_structure(0)
        width = structure.get_value("width")
        height = structure.get_value("height")
        buffer = sample.get_buffer()
        mapped_ok, mapped = buffer.map(Gst.MapFlags.READ)
        if not mapped_ok or not height:
            return Gst.FlowReturn.ERROR
        try:
            pixels = bytes(mapped.data)
        finally:
            buffer.unmap(mapped)
        if len(pixels) % height:
            return Gst.FlowReturn.ERROR
        stride = len(pixels) // height
        if stride < width * 3:
            return Gst.FlowReturn.ERROR
        with self._frame_lock:
            self._pending_frame = (width, height, stride, pixels)
            if not self._frame_dispatch_scheduled:
                self._frame_dispatch_scheduled = True
                GLib.idle_add(self.display_latest_frame)
        return Gst.FlowReturn.OK

    def display_latest_frame(self) -> bool:
        with self._frame_lock:
            frame = self._pending_frame
            self._pending_frame = None
            self._frame_dispatch_scheduled = False
        if frame:
            width, height, stride, pixels = frame
            texture = Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.R8G8B8,
                                            GLib.Bytes.new(pixels), stride)
            self.picture.set_paintable(texture)
            self.status.set_text(_("Live · {width}×{height}").format(width=width, height=height))
        return GLib.SOURCE_REMOVE

    def on_bus_message(self, _bus, message) -> None:
        if message.type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            self.status.set_text(_("RTSP error: {error}").format(error=error.message))
            self.stop_stream(clear_picture=False)
        elif message.type == Gst.MessageType.EOS:
            self.status.set_text(_("The stream ended"))
            self.stop_stream(clear_picture=False)

    def stop_stream(self, clear_picture: bool = True) -> None:
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
        if self.bus is not None:
            if self.bus_handler:
                self.bus.disconnect(self.bus_handler)
                self.bus_handler = 0
            self.bus.remove_signal_watch()
        self.pipeline = None
        self.bus = None
        self.connect_button.set_sensitive(Gst is not None)
        self.stop_button.set_sensitive(False)
        if clear_picture:
            self.picture.set_paintable(None)
            self.status.set_text(_("Preview stopped"))
        with self._frame_lock:
            self._pending_frame = None

    def on_close_request(self, _window) -> bool:
        self.stop_stream()
        return False
