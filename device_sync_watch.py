#!/usr/bin/env python3
"""
device_sync_watch.py - Watch ~/current for changes and trigger device_sync.py

Uses inotifywait to monitor the sync folder(s) and runs device_sync.py
after a quiet period following any change. This avoids the systemd
shutdown ordering problems on LMDE 7.

Behaviour:
  - On startup: runs device_sync.py once (pull from hub, then push)
  - While running: watches for file changes, waits for quiet period,
    then syncs again
  - Ignores temp files, swap files, conflict copies etc.
  - Logs to ~/.local/share/device-sync/watch.log

Usage:
  python3 device_sync_watch.py            # Normal run
  python3 device_sync_watch.py --quiet    # No console output (for autostart)
  python3 device_sync_watch.py --delay 60 # Wait 60s quiet before syncing (default 30)

Prerequisites:
  sudo apt install inotify-tools

David Torrens, 2026
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────

APP_NAME = "device-sync"
CONFIG_FILE = Path.home() / ".config" / APP_NAME / "sync.json"
LOG_DIR = Path.home() / ".local" / "share" / APP_NAME
WATCH_LOG = LOG_DIR / "watch.log"

# File patterns that should NOT trigger a sync
# These match inotifywait's filename output
IGNORE_PATTERNS = [
    "(from ",       # conflict copies — must never trigger re-sync
    ".tmp",
    ".swp",
    "~",            # editor backup files (nano, gedit etc.)
    ".DS_Store",
    "__pycache__",
    ".pyc",
    ".part",        # partial downloads
    ".crdownload",  # Chrome partial downloads
]

# inotifywait events that indicate a real file change
# CLOSE_WRITE: file was written and closed (not mid-write)
# MOVED_TO:    file moved/renamed into the watched folder
# DELETE:      file deleted
WATCH_EVENTS = "close_write,moved_to,delete"


# ── Logging ────────────────────────────────────────────────────────────────

def setup_logging(quiet: bool) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("sync-watch")
    logger.setLevel(logging.DEBUG)

    fh = logging.FileHandler(WATCH_LOG, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(fh)

    if not quiet:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(ch)

    return logger


# ── Config ─────────────────────────────────────────────────────────────────

def get_watch_paths(logger: logging.Logger) -> list:
    """Read sync paths from device_sync config."""
    if not CONFIG_FILE.exists():
        logger.error(f"No config at {CONFIG_FILE} — run device_sync.py --init first")
        sys.exit(1)

    with open(CONFIG_FILE) as f:
        config = json.load(f)

    device_name = config.get("this_device", {}).get("name", "")

    # The hub (aiserver) has nothing to watch — it is the hub
    if device_name == "aiserver":
        logger.info("This device is the hub (aiserver) — nothing to watch.")
        logger.info("Watcher is only needed on spoke devices (laptop, shed-pc).")
        sys.exit(0)

    paths = []
    for sset in config.get("sync_sets", []):
        if not sset.get("enabled", True):
            continue
        local_path = sset.get("local_path", "")
        if local_path:
            p = Path(local_path)
            if p.exists():
                paths.append(str(p))
                logger.info(f"Watching: {p}")
            else:
                logger.warning(f"Watch path does not exist (skipping): {p}")

    if not paths:
        logger.error("No valid watch paths found in config.")
        sys.exit(1)

    return paths


# ── Helpers ─────────────────────────────────────────────────────────────────

def should_ignore(filename: str) -> bool:
    """Return True if this filename should not trigger a sync."""
    for pattern in IGNORE_PATTERNS:
        if pattern in filename:
            return True
    return False


def check_inotifywait(logger: logging.Logger) -> None:
    """Check inotifywait is installed."""
    result = subprocess.run(
        ["which", "inotifywait"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        logger.error("inotifywait not found.")
        logger.error("Install it with:  sudo apt install inotify-tools")
        sys.exit(1)


def sync_now(logger: logging.Logger) -> bool:
    """Run device_sync.py and return True on success."""
    sync_script = Path(__file__).parent / "device_sync.py"
    if not sync_script.exists():
        # Try on PATH as 'device-sync' (installed via install.sh)
        sync_script = "device-sync"

    logger.info("Running sync...")
    try:
        result = subprocess.run(
            [sys.executable, str(sync_script)]
            if str(sync_script) != "device-sync"
            else ["device-sync"],
            timeout=300  # 5 minute timeout
        )
        if result.returncode == 0:
            logger.info("Sync completed OK.")
            return True
        else:
            logger.warning(f"Sync finished with exit code {result.returncode} — check sync.log")
            return False
    except subprocess.TimeoutExpired:
        logger.error("Sync timed out after 5 minutes.")
        return False
    except FileNotFoundError:
        logger.error(f"Cannot find device_sync.py or device-sync command.")
        return False


# ── Main Watch Loop ─────────────────────────────────────────────────────────

def watch_loop(watch_paths: list, delay: int, logger: logging.Logger) -> None:
    """
    Run inotifywait in a subprocess, read its output line by line.
    When a real change is detected, wait for 'delay' seconds of quiet,
    then run sync.

    inotifywait --monitor outputs one line per event:
      /path/to/dir/ EVENT filename
    """
    cmd = [
        "inotifywait",
        "--monitor",          # keep running, one event per line
        "--recursive",        # watch subdirectories
        "--quiet",            # suppress startup messages
        "--format", "%w%f %e",  # output: fullpath EVENT
        "--event", WATCH_EVENTS,
    ] + watch_paths

    logger.info(f"Starting inotifywait (quiet period: {delay}s before sync)...")
    logger.debug(f"Command: {' '.join(cmd)}")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1  # line buffered
        )
    except FileNotFoundError:
        logger.error("inotifywait not found — install inotify-tools")
        sys.exit(1)

    pending_sync = False
    last_event_time = 0

    logger.info("Watching for changes. Press Ctrl+C to stop.")

    try:
        while True:
            # Check if inotifywait died
            if proc.poll() is not None:
                logger.error(f"inotifywait exited unexpectedly (code {proc.returncode})")
                err = proc.stderr.read()
                if err:
                    logger.error(f"inotifywait stderr: {err.strip()}")
                break

            # Non-blocking read with a short timeout
            # We use select to avoid blocking forever
            import select
            readable, _, _ = select.select([proc.stdout], [], [], 1.0)

            if readable:
                line = proc.stdout.readline()
                if not line:
                    break  # EOF

                line = line.strip()
                if not line:
                    continue

                # Parse: "/path/to/file EVENT" or "/path/to/dir/ EVENT"
                parts = line.rsplit(" ", 1)
                if len(parts) == 2:
                    filepath, event = parts
                else:
                    filepath, event = line, "UNKNOWN"

                filename = os.path.basename(filepath)

                if should_ignore(filename):
                    logger.debug(f"Ignored: {filepath} [{event}]")
                    continue

                logger.info(f"Change detected: {filepath} [{event}]")
                pending_sync = True
                last_event_time = time.time()

            # Check if quiet period has elapsed
            if pending_sync:
                quiet_for = time.time() - last_event_time
                if quiet_for >= delay:
                    logger.info(f"Quiet for {quiet_for:.0f}s — triggering sync")
                    sync_now(logger)
                    pending_sync = False
                    last_event_time = 0

    except KeyboardInterrupt:
        logger.info("Stopped by user (Ctrl+C).")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ── Entry Point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Watch sync folders and trigger device_sync.py on changes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 device_sync_watch.py              Watch with 30s quiet period
  python3 device_sync_watch.py --delay 60   Wait 60s quiet before syncing
  python3 device_sync_watch.py --quiet      No console output (for autostart)
  python3 device_sync_watch.py --no-startup Skip initial sync on startup

Logs: ~/.local/share/device-sync/watch.log
        """
    )
    parser.add_argument("--delay", type=int, default=30,
                        help="Seconds of quiet before triggering sync (default: 30)")
    parser.add_argument("--quiet", action="store_true",
                        help="No console output (log to file only)")
    parser.add_argument("--no-startup", action="store_true",
                        help="Skip the initial sync on startup")
    args = parser.parse_args()

    logger = setup_logging(args.quiet)

    logger.info("=" * 50)
    logger.info("device_sync_watch starting")
    logger.info("=" * 50)

    check_inotifywait(logger)
    watch_paths = get_watch_paths(logger)

    # Initial sync on startup (pull from hub to get any changes made elsewhere)
    if not args.no_startup:
        logger.info("Running startup sync (pull from hub)...")
        sync_now(logger)

    # Enter the watch loop
    watch_loop(watch_paths, args.delay, logger)


if __name__ == "__main__":
    main()
