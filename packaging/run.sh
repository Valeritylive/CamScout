#!/usr/bin/env bash
# Launcher shared by the AppImage (AppRun) and the .deb (/usr/bin wrapper).
#
# The bundled Conda prefix is built in CI under /home/runner/..., and libraries
# such as fontconfig and xkbcommon have that build path compiled in as their
# default data location. Outside the build machine those paths do not exist:
# fontconfig finds no config and xkbcommon finds no keymaps, which makes GTK
# crash on Wayland. Every such location is therefore pointed at the prefix the
# application actually runs from.
set -e
DIR="$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")"
PREFIX="${CAMSCOUT_PREFIX:-$DIR/conda}"
APP="${CAMSCOUT_APP:-$DIR/app.py}"

if [[ ! -x "$PREFIX/bin/python" ]]; then
  echo "Не найден встроенный Python в $PREFIX. Переустановите программу." >&2
  exit 1
fi

# Remember what the desktop session had, so the app can hand a clean
# environment to the web browser it opens (see clean_launch_context in app.py).
export CAMSCOUT_ORIG_PATH="$PATH"
export CAMSCOUT_ORIG_XDG_DATA_DIRS="${XDG_DATA_DIRS:-}"
export CAMSCOUT_SET_VARS="LD_LIBRARY_PATH GI_TYPELIB_PATH GSETTINGS_SCHEMA_DIR GTK_DATA_PREFIX GTK_EXE_PREFIX XKB_CONFIG_ROOT XLOCALEDIR FONTCONFIG_FILE FONTCONFIG_PATH GDK_PIXBUF_MODULEDIR GDK_PIXBUF_MODULE_FILE GST_PLUGIN_PATH_1_0 GST_PLUGIN_SYSTEM_PATH_1_0 GST_PLUGIN_SCANNER_1_0 GST_REGISTRY_1_0"
[[ -n "${LD_LIBRARY_PATH:-}" ]] && export CAMSCOUT_ORIG_LD_LIBRARY_PATH="$LD_LIBRARY_PATH"

CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/camscout"
mkdir -p -- "$CACHE" 2>/dev/null || CACHE="$(mktemp -d)"

export PATH="$PREFIX/bin:$PATH"
export LD_LIBRARY_PATH="$PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export GI_TYPELIB_PATH="$PREFIX/lib/girepository-1.0${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
export GSETTINGS_SCHEMA_DIR="$PREFIX/share/glib-2.0/schemas"
export XDG_DATA_DIRS="$PREFIX/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
export GTK_DATA_PREFIX="$PREFIX"
export GTK_EXE_PREFIX="$PREFIX"

# GStreamer for RTSP live view: only the bundled plugins, with a registry of their own.
export GST_PLUGIN_PATH_1_0="$PREFIX/lib/gstreamer-1.0"
export GST_PLUGIN_SYSTEM_PATH_1_0="$PREFIX/lib/gstreamer-1.0"
[[ -x "$PREFIX/libexec/gstreamer-1.0/gst-plugin-scanner" ]] && export GST_PLUGIN_SCANNER_1_0="$PREFIX/libexec/gstreamer-1.0/gst-plugin-scanner"
export GST_REGISTRY_1_0="$CACHE/gst-registry.bin"

# Keyboard maps (xkbcommon) and compose tables: without them GTK aborts on Wayland.
if [[ -d "$PREFIX/share/xkeyboard-config-2" ]]; then
  export XKB_CONFIG_ROOT="$PREFIX/share/xkeyboard-config-2"
elif [[ -d "$PREFIX/share/X11/xkb" ]]; then
  export XKB_CONFIG_ROOT="$PREFIX/share/X11/xkb"
elif [[ -d /usr/share/X11/xkb ]]; then
  export XKB_CONFIG_ROOT=/usr/share/X11/xkb
fi
[[ -d "$PREFIX/share/X11/locale" ]] && export XLOCALEDIR="$PREFIX/share/X11/locale"

# Fonts: use the system fontconfig setup (system fonts, user settings); fall back
# to a generated config listing the bundled fonts.
if [[ -z "${FONTCONFIG_FILE:-}" ]]; then
  if [[ -f /etc/fonts/fonts.conf ]]; then
    # FONTCONFIG_PATH too: relative <include>s (conf.d) resolve against it, and
    # without it fontconfig looks in the build prefix and loses every alias.
    export FONTCONFIG_FILE=/etc/fonts/fonts.conf
    export FONTCONFIG_PATH=/etc/fonts
  else
    cat > "$CACHE/fonts.conf" <<FONTS
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<fontconfig>
  <dir>/usr/share/fonts</dir>
  <dir>/usr/local/share/fonts</dir>
  <dir>$PREFIX/share/fonts</dir>
  <dir>$PREFIX/fonts</dir>
  <dir prefix="xdg">fonts</dir>
  <cachedir>$CACHE/fontconfig</cachedir>
</fontconfig>
FONTS
    export FONTCONFIG_FILE="$CACHE/fonts.conf"
    export FONTCONFIG_PATH="$CACHE"
  fi
fi

# Image loaders (SVG icons and others): scan the bundled loader directory.
LOADERS="$(echo "$PREFIX"/lib/gdk-pixbuf-2.0/*/loaders)"
if [[ -d "$LOADERS" ]]; then
  export GDK_PIXBUF_MODULEDIR="$LOADERS"
  if [[ -x "$PREFIX/bin/gdk-pixbuf-query-loaders" ]]; then
    "$PREFIX/bin/gdk-pixbuf-query-loaders" > "$CACHE/loaders.cache" 2>/dev/null \
      && export GDK_PIXBUF_MODULE_FILE="$CACHE/loaders.cache"
  fi
fi

exec "$PREFIX/bin/python" "$APP" "$@"
