#!/bin/bash
# install.sh - Install device-sync on this machine
#
# Usage: bash install.sh

set -e

INSTALL_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/device-sync"
SYSTEMD_DIR="$HOME/.config/systemd/user"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Device Sync Installer ==="
echo ""

# 1. Install the script
mkdir -p "$INSTALL_DIR" "$APP_DIR"
cp "$SCRIPT_DIR/device_sync.py" "$INSTALL_DIR/device_sync.py"
chmod +x "$INSTALL_DIR/device_sync.py"
ln -sf "$INSTALL_DIR/device_sync.py" "$INSTALL_DIR/device-sync"

echo "Installed to: $INSTALL_DIR/device_sync.py"
echo "Command:      device-sync"

if ! echo "$PATH" | grep -q "$INSTALL_DIR"; then
    echo ""
    echo "NOTE: $INSTALL_DIR not in PATH. Add to ~/.bashrc:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# 2. Check/setup SSH key auth to AI Server
echo ""
echo "--- SSH Key Setup ---"
echo ""

# Read hub hostname from existing config, or use default
HUB_HOST="aiserver"
if [ -f "$HOME/.config/device-sync/sync.json" ]; then
    # Try to extract hostname from config (basic grep, no jq dependency)
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
        # Verify it worked
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

# 3. Create systemd services
mkdir -p "$SYSTEMD_DIR"

# On-demand sync (also used by timer)
cat > "$SYSTEMD_DIR/device-sync.service" << 'EOF'
[Unit]
Description=Device Sync with AI Server
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=%h/.local/bin/device_sync.py
TimeoutStartSec=3600
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

# Sync on shutdown
cat > "$SYSTEMD_DIR/device-sync-shutdown.service" << 'EOF'
[Unit]
Description=Device Sync on Shutdown
DefaultDependencies=no
Before=shutdown.target reboot.target halt.target

[Service]
Type=oneshot
ExecStart=%h/.local/bin/device_sync.py
TimeoutStartSec=300
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=shutdown.target reboot.target halt.target
EOF

# Sync on login (after network is up)
cat > "$SYSTEMD_DIR/device-sync-login.service" << 'EOF'
[Unit]
Description=Device Sync on Login
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStartPre=/bin/sleep 10
ExecStart=%h/.local/bin/device_sync.py
TimeoutStartSec=3600
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

# Optional daily timer
cat > "$SYSTEMD_DIR/device-sync.timer" << 'EOF'
[Unit]
Description=Periodic device sync

[Timer]
OnCalendar=*-*-* 12:00:00
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
EOF

echo ""
echo "Systemd services created."
systemctl --user daemon-reload 2>/dev/null || true

# 4. Init config if needed
if [ ! -f "$HOME/.config/device-sync/sync.json" ]; then
    echo ""
    "$INSTALL_DIR/device_sync.py" --init
fi

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Next steps:"
echo ""
echo "  1. Edit config:"
echo "     nano ~/.config/device-sync/sync.json"
echo ""
echo "  2. Test:"
echo "     device-sync --dry-run"
echo ""
echo "  3. First real sync:"
echo "     device-sync"
echo ""
echo "  4. Enable automatic sync (pick what suits you):"
echo "     systemctl --user enable device-sync-login.service     # On login"
echo "     systemctl --user enable device-sync-shutdown.service   # On shutdown"
echo "     systemctl --user enable --now device-sync.timer        # Daily at noon"
echo ""
echo "  5. Check results:"
echo "     device-sync --status"
echo "     device-sync --resolve        # List any conflict files"
echo "     cat ~/.local/share/device-sync/sync.log"
