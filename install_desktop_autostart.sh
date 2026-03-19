#!/usr/bin/env bash
# install_desktop_autostart.sh — one-time setup for Raspberry Pi desktop autostart.
#
# Run this once after cloning the repo to configure the Pi desktop so that
# Fermentatorium launches automatically when you log in and the browser opens
# to the correct port (5001).
#
# Usage:
#   chmod +x ~/fermentatorium/install_desktop_autostart.sh
#   ./install_desktop_autostart.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

AUTOSTART_DIR="$HOME/.config/autostart"
LXDE_AUTOSTART="$HOME/.config/lxsession/LXDE-pi/autostart"

echo "=== Fermentatorium Desktop Autostart Setup ==="
echo ""

# ── 1. Make start.sh executable ───────────────────────────────────────────────
chmod +x "$SCRIPT_DIR/start.sh"
echo "✓ start.sh is executable"

# ── 2. Remove old threecontrol autostart entry (previous app name) ─────────────
if [ -f "$AUTOSTART_DIR/threecontrol.desktop" ]; then
    rm -f "$AUTOSTART_DIR/threecontrol.desktop"
    echo "✓ Removed old threecontrol.desktop autostart entry"
fi

# ── 3. Remove LXDE autostart entries that would cause duplicate launches ───────
# The LXDE session manager processes ~/.config/lxsession/LXDE-pi/autostart AND
# the XDG ~/.config/autostart/*.desktop files at login.  If start.sh (or any
# fermentatorium launcher) is listed in the LXDE file, it will run a second time
# alongside the .desktop entry we install below, causing two app instances to
# start simultaneously.  Remove every such entry here so only the .desktop
# mechanism fires.
if [ -f "$LXDE_AUTOSTART" ]; then
    _needs_backup=false
    if grep -qE ':5000' "$LXDE_AUTOSTART" 2>/dev/null; then
        _needs_backup=true
    fi
    if grep -qE 'start\.sh|fermentatorium' "$LXDE_AUTOSTART" 2>/dev/null; then
        _needs_backup=true
    fi

    if [ "$_needs_backup" = true ]; then
        _backup="${LXDE_AUTOSTART}.bak.$(date +%Y%m%d%H%M%S)"
        cp "$LXDE_AUTOSTART" "$_backup"
        sed -i -E '/:5000/d' "$LXDE_AUTOSTART"
        sed -i -E '/^[[:space:]]*@[[:space:]]*(bash[[:space:]]+)?[^[:space:]]*start\.sh/d' "$LXDE_AUTOSTART"
        sed -i -E '/^[[:space:]]*@[[:space:]]*(bash[[:space:]]+)?[^[:space:]]*fermentatorium/d' "$LXDE_AUTOSTART"
        echo "✓ Removed stale port-5000/fermentatorium entries from LXDE autostart"
        echo "  (backup saved: $_backup)"
    fi
fi

# Also scan ~/.config/autostart/ for .desktop files that open a browser to :5000
mkdir -p "$AUTOSTART_DIR"
shopt -s nullglob
for _df in "$AUTOSTART_DIR/"*.desktop; do
    # Skip the fermentatorium desktop entry
    case "$_df" in *fermentatorium*) continue ;; esac
    if grep -qE 'Exec=.*(chromium|firefox|epiphany|midori|xdg-open).*:5000' "$_df" 2>/dev/null; then
        echo "✓ Removing stale port-5000 browser autostart: $_df"
        rm -f "$_df"
    fi
done
shopt -u nullglob

# ── 4. Install the fermentatorium.desktop autostart entry ──────────────────────
mkdir -p "$AUTOSTART_DIR"

cat > "$AUTOSTART_DIR/fermentatorium.desktop" << EOF
[Desktop Entry]
Type=Application
Name=Fermentatorium
Exec=bash $SCRIPT_DIR/start.sh
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
EOF

echo "✓ Created $AUTOSTART_DIR/fermentatorium.desktop"

# ── 5. Summary ────────────────────────────────────────────────────────────────
echo ""
echo "=== Setup complete! ==="
echo ""
echo "Fermentatorium will start automatically the next time you log in."
echo "The browser will open to http://127.0.0.1:5001/startup"
echo ""
echo "To test immediately (without logging out), run:"
echo "  bash $SCRIPT_DIR/start.sh"
echo ""
echo "NOTE: What was killing port 5000?"
echo "  On Raspberry Pi OS, 'avahi-daemon' and other system services can"
echo "  occasionally grab port 5000.  Flask also defaults to 5000, so any"
echo "  second Flask process will collide.  The app now uses port 5001 to"
echo "  avoid these conflicts entirely."
