#!/bin/bash
# Install per-user desktop integration for the Splash TimePix UI.
#
# What this does (per-user, no root, idempotent):
#
#   1. Copies the bundled icon into the freedesktop hicolor theme tree:
#        ~/.local/share/icons/hicolor/256x256/apps/splash_timepix.png
#
#   2. Writes a launcher into the GNOME applications directory:
#        ~/.local/share/applications/splash_timepix.desktop
#      with StartupWMClass=splash_timepix so the dock pairs the running
#      window to this entry instead of falling back to a generic gear.
#
#   3. Drops a clickable shortcut on the user's Desktop (locale-aware via
#      xdg-user-dir) pointing at the same launcher.
#
#   4. Refreshes the icon and desktop-file caches.
#
# Re-running is safe — every output is overwritten.
#
# Why a script: paths inside .desktop files have to be absolute, so a fresh
# clone on a new machine cannot inherit the values used here. Re-deriving
# everything from this script's location keeps the install reproducible
# regardless of where the repo is cloned.
#
# Usage:
#   ./scripts/install_desktop_integration.sh           # install everything
#   ./scripts/install_desktop_integration.sh --uninstall

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

LAUNCHER="$PROJECT_DIR/scripts/tpxui_launcher.sh"
ICON_SRC="$PROJECT_DIR/src/splash_timepix/ui/assets/icon.png"

APP_ID="splash_timepix"
APPS_DIR="$HOME/.local/share/applications"
ICONS_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
APPS_FILE="$APPS_DIR/${APP_ID}.desktop"
ICONS_FILE="$ICONS_DIR/${APP_ID}.png"

# xdg-user-dir resolves the localized Desktop folder (e.g. Bureau, Schreibtisch).
# Fall back to ~/Desktop if the helper is missing (rare on Ubuntu/GNOME).
if command -v xdg-user-dir >/dev/null 2>&1; then
    DESKTOP_DIR="$(xdg-user-dir DESKTOP)"
else
    DESKTOP_DIR="$HOME/Desktop"
fi
DESKTOP_FILE="$DESKTOP_DIR/${APP_ID}.desktop"

uninstall() {
    echo "Removing desktop integration files..."
    rm -fv "$APPS_FILE" "$ICONS_FILE" "$DESKTOP_FILE"
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$APPS_DIR" 2>/dev/null || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -f "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
    fi
    echo "Done."
}

if [ "${1-}" = "--uninstall" ]; then
    uninstall
    exit 0
fi

if [ ! -f "$LAUNCHER" ]; then
    echo "Error: launcher script not found: $LAUNCHER" >&2
    exit 1
fi
if [ ! -f "$ICON_SRC" ]; then
    echo "Error: icon source not found: $ICON_SRC" >&2
    exit 1
fi

# .desktop files require Exec= and Icon= to be absolute paths or installed
# names. We use an absolute icon path for the Desktop shortcut (so it works
# even before the hicolor cache is rebuilt) and the bare app id for the
# installed launcher (so it's themable / DPI-aware).

mkdir -p "$APPS_DIR" "$ICONS_DIR" "$DESKTOP_DIR"

cp -f "$ICON_SRC" "$ICONS_FILE"

cat > "$APPS_FILE" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Splash TimePix
GenericName=TimePix3 Acquisition UI
Comment=Launch TimePix3 data acquisition UI
Exec=$LAUNCHER
Icon=$APP_ID
Terminal=false
Categories=Science;Development;
StartupNotify=true
StartupWMClass=$APP_ID
EOF

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Splash TimePix
Comment=Launch TimePix3 data acquisition UI
Exec=$LAUNCHER
Icon=$ICON_SRC
Terminal=false
Categories=Science;Development;
StartupNotify=true
StartupWMClass=$APP_ID
EOF

# GNOME / Nautilus require the desktop-file's executable bit to honor it as
# a launcher rather than treat it as a text file. The 'metadata::trusted'
# attribute is set the first time the user clicks "Allow Launching", so we
# can't pre-set it from a script — that part is interactive on first click.
chmod +x "$DESKTOP_FILE"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_DIR" 2>/dev/null || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
fi

cat <<EOF

Installed:
  $ICONS_FILE
  $APPS_FILE
  $DESKTOP_FILE

Next steps:
  - Restart any running Splash TimePix UI so the new WM_CLASS / app_id is set.
  - On Wayland-GNOME with Dash-to-Dock, you may need to log out and back in
    (or restart the extension) before the dock picks up the new launcher.
  - First click on the Desktop shortcut: GNOME asks "Allow Launching"; click it.

Uninstall with: $0 --uninstall
EOF
