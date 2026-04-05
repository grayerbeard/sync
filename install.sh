#!/bin/bash

# install.sh - Install device-sync on this machine
#
# Usage: bash install.sh

set -e

INSTALL_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/device-sync"
AUTOSTART_DIR="$HOME/.config/autostart"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Device Sync Installer ==="
echo ""

# 1. Install the scripts

mkdir -p "$INSTALL_DIR" "$APP_DIR"

cp "$SCRIPT_DIR/device_sync.py" "$INSTALL_DIR/device_sync.py"
chmod +x "$INSTALL_DIR/device_sync.py"
ln -sf "$INSTALL_DIR/device_sync.py" "$INSTALL_DIR/device-sync"
echo "Installed: device_sync.py -> $INSTALL_DIR/device-sync"

cp "$SCRIPT_DIR/device_sync_watch.py" "$INSTALL_DIR/device_sync_watch.py"
chmod +x "$INSTALL_DIR/device_sync_watch.py"
echo "Installed: device_sync_watch.py -> $INSTALL_DIR/device_sync_watch.py"

if ! echo "$PATH" | grep -q "$INSTALL_DIR"; then
    echo ""
    echo "NOTE: $INSTALL_DIR is not in PATH. Add to ~/.bashrc:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# 2. Check prerequisites

echo ""
echo "--- Checking Prerequisites ---"
echo ""

# Check inotify-tools
if ! command -v inotifywait &>/dev/null; then
    echo "inotify-tools not found. Installing..."
    if command -v apt-get &>/dev/null; then
        sudo apt-get install -y inotify-tools
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y inotify-tools
    elif command -v pacman &>/dev/null; then
        sudo pacman -S --noconfirm inotify-tools
    else
        echo "WARNING: Cannot auto-install inotify-tools."
        echo "Install manually, e.g.:  sudo apt install inotify-tools"
    fi
else
    echo "inotify-tools: OK ($(command -v inotifywait))"
fi

# Check rsync
if command -v rsync &>/dev/null; then
    echo "rsync: OK"
else
    echo "WARNING: rsync not found. Install with:  sudo apt install rsync"
fi

# Check python3
if command -v python3 &>/dev/null; then
    echo "python3: OK"
else
    echo "WARNING: python3 not found — required to run the sync scripts"
fi

# 3. Check/setup SSH key auth to AI Server

echo ""
echo "--- SSH Key Setup ---"
echo ""

# Read hub hostname from existing config, or use default
HUB_HOST="aiserver"
if [ -f "$HOME/.config/device-sync/sync.json" ]; then
    CONFIGURED_HOST=$(grep -o '"tailscale_hostname"[[:space:]]*:[[:space:]]*"[^"]*"' \
        "$HOME/.config/device-sync/sync.json" 2>/dev/null | head -1 | \
        sed 's/.*"tailscale_hostname"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/')
    if [ -n "$CONFIGURED_HOST" ]; then
        HUB_HOST="$CONFIGURED_HOST"
    fi
fi

# Check for SSH key
if ! ls ~/.ssh/id_*.pub >/dev/null 2>&1; then
    echo "No SSH key found. Generating one..."
    echo "(Just press Enter at every prompt — accept defaults, no passphrase)"
    echo ""
    ssh-keygen -t ed25519 -f "$HOME/.ssh/id_ed25519" -N ""
    echo ""
    echo "SSH key created."
else
    echo "SSH key found: $(ls ~/.ssh/id_*.pub | head -1)"
fi

# Check if passwordless SSH to hub already works
echo ""
echo "Testing SSH key auth to $HUB_HOST..."
if ssh -o BatchMode=yes -o ConnectTimeout=5 "david@$HUB_HOST" echo ok >/dev/null 2>&1; then
    echo "  SSH key auth to $HUB_HOST is working."
else
    echo "  SSH key auth not yet set up for $HUB_HOST."
    echo ""
    read -p "  Copy SSH key to $HUB_HOST now? You'll need the password once. [Y/n] " REPLY
    REPLY=${REPLY:-Y}
    if [[ "$REPLY" =~ ^[Yy] ]]; then
        ssh-copy-id "david@$HUB_HOST"
        echo ""
        if ssh -o BatchMode=yes -o ConnectTimeout=5 "david@$HUB_HOST" echo ok >/dev/null 2>&1; then
            echo "  SSH key auth is now working."
        else
            echo "  WARNING: SSH key auth still failing. Sync will not work"
            echo "  until this is resolved. Try manually:"
            echo "    ssh-copy-id david@$HUB_HOST"
        fi
    else
        echo ""
        echo "  Skipped. You'll need to run this before sync will work:"
        echo "    ssh-copy-id david@$HUB_HOST"
    fi
fi

# 4. Note about systemd on LMDE 7
#
# Systemd shutdown-triggered services (Before=shutdown.target) do not fire
# reliably on LMDE 7 / Debian-based systems. The shutdown ordering differs
# from Ubuntu and the user-level systemd services are not invoked. Extensive
# testing confirmed the services were simply never called during shutdown
# (journalctl showed "No entries" for the service after reboot).
#
# The solution used here is:
#   - device_sync_watch.py runs as a desktop autostart item (XDG .desktop file)
#   - It syncs on startup (pull from hub) and watches for changes while running
#   - For shutdown sync, use the desktop shortcut approach (see README)
#
# Systemd service files are NOT installed as they don't work reliably on LMDE 7.
# They may work on other distros — if needed, create them manually per README.

# 5. Set up desktop autostart for the watcher

echo ""
echo "--- Autostart Watcher Setup ---"
echo ""

# Check if this is the hub — watcher not needed on aiserver
DEVICE_NAME=""
if [ -f "$HOME/.config/device-sync/sync.json" ]; then
    DEVICE_NAME=$(grep -o '"name"[[:space:]]*:[[:space:]]*"[^"]*"' \
        "$HOME/.config/device-sync/sync.json" 2>/dev/null | head -1 | \
        sed 's/.*"name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/')
fi

if [ "$DEVICE_NAME" = "aiserver" ]; then
    echo "This device is the hub (aiserver) — watcher autostart not needed."
else
    mkdir -p "$AUTOSTART_DIR"
    cat > "$AUTOSTART_DIR/device-sync-watch.desktop" << EOF
[Desktop Entry]
Type=Application
Name=Device Sync Watcher
Comment=Watch ~/current for changes and sync to AI Server
Exec=python3 $INSTALL_DIR/device_sync_watch.py --quiet
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
EOF
    echo "Autostart entry created: $AUTOSTART_DIR/device-sync-watch.desktop"
    echo "The watcher will start automatically on next login."
    echo ""
    echo "To start the watcher now without rebooting:"
    echo "  python3 $INSTALL_DIR/device_sync_watch.py &"
fi

# 6. Create a shutdown sync desktop shortcut
#
# Because systemd shutdown services don't work reliably on LMDE 7,
# a desktop shortcut is provided to run a final sync before shutdown.

echo ""
echo "--- Shutdown Sync Shortcut ---"
echo ""

DESKTOP_DIR="$HOME/Desktop"
mkdir -p "$DESKTOP_DIR"

cat > "$DESKTOP_DIR/Sync Before Shutdown.desktop" << EOF
[Desktop Entry]
Type=Application
Name=Sync Before Shutdown
Comment=Run device-sync before shutting down
Exec=bash -c 'python3 $INSTALL_DIR/device_sync.py && zenity --info --text="Sync complete - safe to shut down" 2>/dev/null || echo "Sync complete"'
Icon=network-transmit-receive
Terminal=false
EOF
chmod +x "$DESKTOP_DIR/Sync Before Shutdown.desktop"
echo "Desktop shortcut created: $DESKTOP_DIR/Sync Before Shutdown.desktop"
echo "Use this before shutting down to ensure a final sync runs."

# 7. Init config if needed

if [ ! -f "$HOME/.config/device-sync/sync.json" ]; then
    echo ""
    "$INSTALL_DIR/device_sync.py" --init
fi

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Next steps:"
echo ""
echo "  1. Edit config (set this_device.name):"
echo "     nano ~/.config/device-sync/sync.json"
echo ""
echo "  2. Test sync:"
echo "     device-sync --dry-run"
echo ""
echo "  3. First real sync:"
echo "     device-sync"
echo ""
echo "  4. Start the watcher now (or reboot to use autostart):"
echo "     python3 $INSTALL_DIR/device_sync_watch.py &"
echo ""
echo "  5. Check results:"
echo "     device-sync --status"
echo "     device-sync --resolve    # List any conflict files"
echo "     cat ~/.local/share/device-sync/sync.log"
echo "     cat ~/.local/share/device-sync/watch.log"
echo ""
echo "  6. Before shutting down, use the desktop shortcut:"
echo "     'Sync Before Shutdown' on your desktop"
echo ""
