#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APPDIR="${APPDIR:-$ROOT_DIR/AppDir}"
TOOLS_DIR="${TOOLS_DIR:-$ROOT_DIR/.appimage-tools}"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT_DIR/dist}"
APP_NAME="CamScout"

mkdir -p "$TOOLS_DIR" "$OUTPUT_DIR" \
  "$APPDIR/usr/share/camscout" \
  "$APPDIR/usr/share/icons/hicolor/scalable/apps"

LINUXDEPLOY="$TOOLS_DIR/linuxdeploy-x86_64.AppImage"
CONDA_PLUGIN="$TOOLS_DIR/linuxdeploy-plugin-conda.sh"

if [[ ! -x "$LINUXDEPLOY" ]]; then
  wget -q --show-progress \
    -O "$LINUXDEPLOY" \
    https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage
  chmod +x "$LINUXDEPLOY"
fi

if [[ ! -x "$CONDA_PLUGIN" ]]; then
  wget -q --show-progress \
    -O "$CONDA_PLUGIN" \
    https://raw.githubusercontent.com/linuxdeploy/linuxdeploy-plugin-conda/master/linuxdeploy-plugin-conda.sh
  chmod +x "$CONDA_PLUGIN"
fi

# The Conda plugin's fresh Miniconda setup writes an Anaconda defaults channel
# config into the prefix. Remove it so noninteractive builds use conda-forge
# only and do not require accepting unrelated Anaconda Terms of Service.
if ! grep -q 'rm -f.*usr/conda/.condarc' "$CONDA_PLUGIN"; then
  sed -i '/bash "\$CONDA_DOWNLOAD_DIR"\/"\$miniconda_installer_filename" -b -p "\$APPDIR"\/usr\/conda -f/a\rm -f "$APPDIR"/usr/conda/.condarc' \
    "$CONDA_PLUGIN"
fi
sed -i '/config --remove channels defaults/d' "$CONDA_PLUGIN"
if ! grep -q 'conda remove -y conda-anaconda-tos' "$CONDA_PLUGIN"; then
  sed -i '/conda config --add channels conda-forge/a\"$APPDIR"/usr/conda/bin/conda remove -y conda-anaconda-tos' \
    "$CONDA_PLUGIN"
fi

install -m 0644 "$ROOT_DIR"/*.py "$APPDIR/usr/share/camscout/"
mkdir -p "$APPDIR/usr/share/camscout/locales" "$APPDIR/usr/share/camscout/data"
install -m 0644 "$ROOT_DIR"/locales/*.json "$APPDIR/usr/share/camscout/locales/"
install -m 0644 "$ROOT_DIR/data/io.github.Valeritylive.CamScout.svg" "$APPDIR/usr/share/camscout/data/"

install -m 0644 "$ROOT_DIR/data/io.github.Valeritylive.CamScout.svg" \
  "$APPDIR/usr/share/icons/hicolor/scalable/apps/io.github.Valeritylive.CamScout.svg"

cat > "$APPDIR/io.github.Valeritylive.CamScout.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=CamScout
Comment=Find, activate and configure Dahua and Hikvision cameras and recorders
Comment[ru]=Поиск, активация и настройка камер и регистраторов Dahua и Hikvision
Exec=AppRun
Icon=io.github.Valeritylive.CamScout
Terminal=false
Categories=Utility;Network;
StartupNotify=true
DESKTOP

cat > "$APPDIR/AppRun" <<'APPRUN'
#!/usr/bin/env bash
set -e
HERE="$(dirname -- "$(readlink -f -- "$0")")"
PREFIX="$HERE/usr/conda"
APP="$HERE/usr/share/camscout/app.py"

if [[ ! -x "$PREFIX/bin/python" ]]; then
  echo "Не найден встроенный Python. Пересоберите AppImage." >&2
  exit 1
fi

export PATH="$PREFIX/bin:$PATH"
export LD_LIBRARY_PATH="$PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export GI_TYPELIB_PATH="$PREFIX/lib/girepository-1.0${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
export GST_PLUGIN_PATH_1_0="$PREFIX/lib/gstreamer-1.0"
export GST_PLUGIN_SYSTEM_PATH_1_0="$PREFIX/lib/gstreamer-1.0"
export GSETTINGS_SCHEMA_DIR="$PREFIX/share/glib-2.0/schemas"
export GST_REGISTRY_1_0="${XDG_CACHE_HOME:-$HOME/.cache}/camscout-gst.bin"

exec "$PREFIX/bin/python" "$APP" "$@"
APPRUN
chmod 0755 "$APPDIR/AppRun"

export CONDA_CHANNELS="conda-forge"
export CONDA_PACKAGES="python=3.12;pygobject;pycairo;gtk4;adwaita-icon-theme;gstreamer;gst-python;gst-plugins-base;gst-plugins-good;gst-plugins-bad;gst-libav;requests;cryptography;qrcode"
export APPIMAGE_EXTRACT_AND_RUN=1

cd "$OUTPUT_DIR"
"$LINUXDEPLOY" --appdir "$APPDIR" \
  --desktop-file "$APPDIR/io.github.Valeritylive.CamScout.desktop" \
  --icon-file "$APPDIR/usr/share/icons/hicolor/scalable/apps/io.github.Valeritylive.CamScout.svg" \
  --plugin conda \
  --output appimage

shopt -s nullglob
images=("$OUTPUT_DIR"/*.AppImage)
if (( ${#images[@]} == 0 )); then
  echo "linuxdeploy завершился без создания AppImage в $OUTPUT_DIR" >&2
  exit 1
fi
latest_image="${images[-1]}"
if [[ "$latest_image" != "$OUTPUT_DIR/$APP_NAME-x86_64.AppImage" ]]; then
  mv -f -- "$latest_image" "$OUTPUT_DIR/$APP_NAME-x86_64.AppImage"
fi
printf 'Готово: %s\n' "$OUTPUT_DIR/$APP_NAME-x86_64.AppImage"
