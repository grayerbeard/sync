# Device Sync

Two-way file sync between your Linux devices using rsync over Tailscale SSH.
The defaults in these files assume the "aiserver" acts as the hub; and other devices sync with it.
Edit the config file to suite device names.
Install on all devices that you want to sync to the hub device.

## How It Works

```
  acer-laptop ──────↔────── AI Server (aiserver) ──────↔────── shed-pc
                              (hub — always on)
```

Each spoke device does a **pull then push** with the hub:
1. **Pull** — rsync remote → local (gets changes made on any other device via the hub)
2. **Push** — rsync local → remote (sends your changes to the hub)

Changes propagate between spoke devices in two hops. Edit a file on the
laptop, sync, then sync the shed PC — it gets the update.

## Conflict Handling

If the same file was modified on both sides since the last sync, both
versions are preserved with clear origin labels (OneDrive style):

Example: if `Pulse Records.docx` was edited on both the laptop and AI Server:
```
Pulse Records.docx                                  ← newest version (live file)
Pulse Records (from acer-laptop 2026-03-16).docx    ← laptop's version
Pulse Records (from aiserver 2026-03-16).docx       ← AI Server's version
```

Nothing is ever lost. You compare the two labelled versions, keep what
you want, and delete the `(from ...)` files when resolved.

Use `device-sync --resolve` to list all unresolved conflict files.

## Quick Start

### On the AI Server (one-time setup)

```bash
mkdir -p /home/david/current
```

No need to install device-sync on the AI Server — it is the hub and
spoke devices sync with it.

### On each spoke device (laptop, shed PC)

```bash
# 1. Copy the files to the device (scp from AI Server is most reliable)
scp david@aiserver:~/current/SyncCode/device_sync.py ~/current/SyncCode/
scp david@aiserver:~/current/SyncCode/install.sh ~/current/SyncCode/

# 2. Run the installer (handles SSH key setup too)
bash install.sh

# 3. Edit config — set device name
nano ~/.config/device-sync/sync.json

# 4. Test
device-sync --dry-run

# 5. Real sync
device-sync
```

The installer will check for SSH key auth and offer to set it up.
Passwordless SSH to the AI Server is required — without it, the
systemd services cannot run unattended.

## Configuration

Config: `~/.config/device-sync/sync.json`

### Minimal changes needed

```json
{
    "this_device": {
        "name": "acer-laptop"
    },
    "sync_sets": [
        {
            "name": "current",
            "local_path": "/home/david/current",
            "hub_path": "/home/david/current"
        }
    ]
}
```

Set `this_device.name` to match the device: `acer-laptop`, `shed-pc`, etc.

### Adding more folders later

Add entries to `sync_sets`:

```json
{
    "name": "charity-files",
    "enabled": true,
    "local_path": "/home/david/charity",
    "hub_path": "/home/david/charity"
}
```

## Usage

```
device-sync                 Sync now
device-sync --dry-run       Preview (nothing transferred)
device-sync --status        Last sync result
device-sync --resolve       List unresolved conflict files
device-sync --verbose       Detailed output
device-sync --init          Create config
```

## Automatic Triggers

| Service | When | Enable |
|---------|------|--------|
| `device-sync-login.service` | 10s after login | `systemctl --user enable device-sync-login.service` |
| `device-sync-shutdown.service` | On shutdown/reboot | `systemctl --user enable device-sync-shutdown.service` |
| `device-sync.timer` | Daily at noon | `systemctl --user enable --now device-sync.timer` |

For the laptop, login + shutdown triggers make most sense.
For the shed PC, login trigger is probably sufficient.

## Logs

- `~/.local/share/device-sync/sync.log` — detailed log
- `~/.local/share/device-sync/last_status.json` — machine-readable last result
- `journalctl --user -u device-sync.service` — systemd journal

## Prerequisites

- rsync (pre-installed on LMDE 7 and RPi OS)
- Tailscale running on all devices
- SSH key auth from each spoke to the AI Server

The installer handles SSH key setup, but if doing it manually:
```bash
ssh-keygen -t ed25519           # Accept defaults, no passphrase
ssh-copy-id david@aiserver      # Enter password once
ssh -o BatchMode=yes david@aiserver echo ok   # Should print "ok" with no prompt
```

## What About the Desktop?

Since the AI Server is now the primary desktop, the big Desktop machine
is occasional-use only. Options:
- Add it as another spoke when needed (install device-sync, set name to "desktop")
- Access `/home/david/current` on the AI Server directly via Tailscale
  (e.g. `sshfs david@aiserver:/home/david/current ~/current`)

## Cleaning Up After First Sync

The first sync on a new device will flag many conflicts because there is
no previous manifest to compare against. This is normal — review the
`(from ...)` files, keep what you need, delete the rest.

Also clean up any Syncthing remnants if present:
```bash
find /home/david/current -name ".stfolder" -type d
# Remove once confirmed Syncthing is no longer managing this folder
```

## Known Issues and Tips

- **Download corruption:** Downloading `.py` files from Claude chat can
  corrupt line breaks. Always copy via `scp` between devices after
  verifying the file compiles on one machine:
  `python3 -c "import py_compile; py_compile.compile('device_sync.py')"`
- **First dry run:** Always run `--dry-run` before the first real sync
  on a new device to review what will be transferred.
- **Hub hostname:** The config uses `aiserver` as the Tailscale hostname.
  Check this matches `tailscale status` output on your network.
