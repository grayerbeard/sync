# Device Sync

Two-way file sync between your Linux devices using rsync over Tailscale SSH.
The AI Server acts as the hub; other devices sync with it.

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

## Files

| File | Purpose |
|---|---|
| `device_sync.py` | Core sync script — run manually or called by the watcher |
| `device_sync_watch.py` | File watcher — monitors for changes and triggers sync automatically |
| `install.sh` | Installer — sets up scripts, SSH keys, autostart, and desktop shortcut |

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
scp david@aiserver:~/sync/device_sync.py ~/sync/
scp david@aiserver:~/sync/device_sync_watch.py ~/sync/
scp david@aiserver:~/sync/install.sh ~/sync/

# 2. Run the installer
bash ~/sync/install.sh

# 3. Edit config — set this device's name
nano ~/.config/device-sync/sync.json

# 4. Test
device-sync --dry-run

# 5. First real sync
device-sync
```

The installer handles SSH key setup, inotify-tools installation, autostart
watcher configuration, and the shutdown sync desktop shortcut.

## Conflict Handling

If the same file was modified on both sides since the last sync, both
versions are preserved with clear origin labels (OneDrive style):

Example: if `report.docx` was edited on both the laptop and AI Server:

```
report.docx                                   ← newest version (live file)
report (from acer-laptop 2026-03-16).docx     ← laptop's version
report (from aiserver 2026-03-16).docx        ← AI Server's version
```

Nothing is ever lost. Compare the two labelled versions, keep what
you want, and delete the `(from ...)` files when resolved.

Use `device-sync --resolve` to list all unresolved conflict files.

## Automatic Sync — How It Works on LMDE 7

### Why not systemd?

The obvious approach — systemd user services with `Before=shutdown.target` —
does not work reliably on LMDE 7 (Debian-based). Extensive testing showed
that user-level systemd shutdown services are simply never invoked during
shutdown on LMDE 7. `journalctl` consistently showed "No entries" for the
service after reboot, confirming the services were not called at all.

This is a known difference between Debian and Ubuntu shutdown target ordering.
It affects login-triggered services too — timing and network availability make
them unreliable for this use case.

### The solution: file watcher + desktop autostart

`device_sync_watch.py` uses `inotifywait` to watch the sync folder(s) for
changes. It runs as a desktop autostart item (XDG `.desktop` file in
`~/.config/autostart/`), which works reliably on all desktop environments
that follow the XDG standard — Cinnamon, GNOME, KDE, XFCE, and others.

**What the watcher does:**

1. **On startup** — runs `device_sync.py` once to pull any changes from
   the hub made while this device was off
2. **While running** — watches for file changes using `inotifywait`
3. **On change** — waits 30 seconds of quiet (to catch bursts of saves),
   then triggers a full sync
4. **Ignored files** — lock files, temp files, swap files, and conflict
   copies are all ignored so they don't trigger unnecessary syncs

**What the watcher does NOT do:**

- Run at shutdown — see the shutdown shortcut below
- Watch for changes on the hub (another device syncing to AIServer while
  you're working won't trigger a pull — you'll get those changes on next
  startup or next time you trigger a sync manually)

### Shutdown sync

Because systemd shutdown services don't work on LMDE 7, the installer
creates a desktop shortcut — **"Sync Before Shutdown"** — on your desktop.
Click this before shutting down to run a final sync. It shows a
confirmation dialog when complete so you know it's safe to shut down.

This is deliberately simple. The watcher syncs frequently while you work,
so the shutdown sync is only catching the last few minutes of changes.

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
device-sync --init          Create default config
```

## Watcher Usage

```
python3 device_sync_watch.py              Start watcher (30s quiet period)
python3 device_sync_watch.py --delay 60  Wait 60s quiet before syncing
python3 device_sync_watch.py --quiet     No console output (used by autostart)
python3 device_sync_watch.py --no-startup  Skip initial sync on startup
```

Watcher log: `~/.local/share/device-sync/watch.log`

## Logs

- `~/.local/share/device-sync/sync.log` — detailed sync log
- `~/.local/share/device-sync/watch.log` — watcher events and triggers
- `~/.local/share/device-sync/last_status.json` — machine-readable last result

## Prerequisites

- **rsync** — pre-installed on LMDE 7 and RPi OS
- **inotify-tools** — installed automatically by `install.sh`; or manually:
  `sudo apt install inotify-tools`
- **python3** — pre-installed on all supported systems
- **Tailscale** — running on all devices
- **SSH key auth** — passwordless SSH from each spoke to the AI Server
  (the installer handles this)

## Transfer Speed

Expect around 5–10 MB/s syncing over Tailscale. This is normal — rsync over
SSH adds encryption overhead, and Tailscale adds a second encryption layer on
top. The limiting factor is usually the upload speed of the hub's internet
connection. For a typical home connection with 25 Mbps upload, 7 MB/s
through Tailscale is about right.

## What About the Desktop?

The AI Server (N150 Mini PC) is now the primary always-on hub. The big
Ryzen desktop is occasional-use only. Options:

- Add it as another spoke when needed (install device-sync, set name to `desktop`)
- Access `/home/david/current` on the AI Server directly via Tailscale SSHFS:
  `sshfs david@aiserver:/home/david/current ~/current`

## Cleaning Up After First Sync

The first sync on a new device may flag many conflicts because there is
no previous manifest to compare against. This is normal — review the
`(from ...)` files, keep what you need, delete the rest.

Also clean up any Syncthing remnants if present:

```bash
find /home/david/current -name ".stfolder" -type d
# Remove once confirmed Syncthing is no longer managing this folder
```

## Known Issues and Tips

- **Download corruption:** Downloading `.py` files from a browser can corrupt
  line breaks. Always copy between devices using `scp` after verifying the
  file compiles: `python3 -c "import py_compile; py_compile.compile('device_sync.py')"`
- **First dry run:** Always run `--dry-run` before the first real sync on a
  new device to review what will be transferred.
- **Hub hostname:** The config uses `aiserver` as the Tailscale hostname.
  Verify this matches `tailscale status` output on your network. The actual
  hostname used is `n150` — check your Tailscale admin panel if unsure.
- **Watcher not starting?** Check the autostart entry exists:
  `cat ~/.config/autostart/device-sync-watch.desktop`
  And check the watcher log for errors:
  `cat ~/.local/share/device-sync/watch.log`
