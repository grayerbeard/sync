#!/usr/bin/env python3
"""
device_sync.py - Two-way file sync between devices via AI Server (hub)

Synchronises /home/david/current (or other configured folders) between
devices using rsync over Tailscale SSH. The AI Server acts as the hub;
spoke devices (laptop, shed PC) sync with it, never directly with each other.

Conflict handling: If the same file was modified on both sides since the
last sync, both versions are kept — the incoming version is renamed with
a .conflict-<device>-<timestamp> suffix so nothing is ever lost.

Usage:
    device_sync.py                  # Run sync
    device_sync.py --dry-run        # Preview what would happen
    device_sync.py --status         # Show last sync result
    device_sync.py --init           # Create default config
    device_sync.py --resolve        # List unresolved conflicts

David Torrens, 2026
"""

import argparse
import datetime
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────

APP_NAME = "device-sync"
CONFIG_DIR = Path.home() / ".config" / APP_NAME
CONFIG_FILE = CONFIG_DIR / "sync.json"
LOG_DIR = Path.home() / ".local" / "share" / APP_NAME
LOG_FILE = LOG_DIR / "sync.log"
STATUS_FILE = LOG_DIR / "last_status.json"
TIMESTAMP_DIR = LOG_DIR / "timestamps"

# ── Default Configuration ──────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "_comment": "Device Sync Configuration",
    "this_device": {
        "name": "CHANGEME",
        "_comment": "Set to: aiserver, laptop, or shed-pc"
    },
    "hub": {
        "tailscale_hostname": "n150",
        "user": "david",
        "_comment": "The AI Server is always the hub. Spoke devices sync with it."
    },
    "rsync_options": {
        "compress": True,
        "partial": True,
        "bandwidth_limit_kbps": 0,
        "exclude_patterns": [
            ".cache",
            "__pycache__",
            "*.pyc",
            ".thumbnails",
            "node_modules",
            ".Trash*",
            "*.tmp",
            "*.swp",
            "*~",
            ".DS_Store",
            "*(from *"
        ],
        "_comment": "conflict copies (from ...) are excluded from sync to avoid loops"
    },
    "sync_sets": [
        {
            "name": "current",
            "enabled": True,
            "local_path": "/home/david/current",
            "hub_path": "/home/david/current",
            "_comment": "Main working folder. Paths may differ between devices."
        }
    ]
}


# ── Logging ────────────────────────────────────────────────────────────────

def setup_logging(verbose: bool = False) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.DEBUG)

    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(ch)

    return logger


# ── Config ─────────────────────────────────────────────────────────────────

def init_config() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        print(f"Config already exists: {CONFIG_FILE}")
        print("Delete it and run --init again, or edit it manually.")
        return

    with open(CONFIG_FILE, "w") as f:
        json.dump(DEFAULT_CONFIG, f, indent=2)

    print(f"Created default config: {CONFIG_FILE}")
    print()
    print("IMPORTANT — edit before first sync:")
    print(f"  nano {CONFIG_FILE}")
    print()
    print("Set 'this_device.name' to one of: aiserver, laptop, shed-pc")
    print("Review sync_sets paths for this device.")


def load_config(logger: logging.Logger) -> dict:
    if not CONFIG_FILE.exists():
        logger.error(f"No config found at {CONFIG_FILE}")
        logger.error("Run with --init first.")
        sys.exit(1)

    with open(CONFIG_FILE) as f:
        config = json.load(f)

    name = config.get("this_device", {}).get("name", "")
    if name == "CHANGEME" or not name:
        logger.error("Device name not set. Edit 'this_device.name' in config.")
        sys.exit(1)

    return config


# ── Connectivity ───────────────────────────────────────────────────────────

def check_reachable(hostname: str, user: str, logger: logging.Logger) -> bool:
    logger.info(f"Checking connectivity to {hostname}...")
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
             f"{user}@{hostname}", "echo ok"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            logger.info(f"  {hostname} reachable.")
            return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    logger.error(f"  Cannot reach {hostname}. Is Tailscale running?")
    return False


# ── File Listing and Conflict Detection ────────────────────────────────────

def get_file_manifest(path: str, is_remote: bool = False,
                      remote_spec: str = "", logger: logging.Logger = None) -> dict:
    """
    Get a dict of {relative_path: mtime_epoch} for all files under path.
    Uses rsync --list-only for remote, or os.walk for local.
    """
    manifest = {}

    if is_remote:
        # Use rsync to list remote files with modification times
        cmd = [
            "rsync", "--list-only", "-r",
            "--exclude", "*.conflict-*",
            "-e", "ssh -o ConnectTimeout=10 -o BatchMode=yes",
            f"{remote_spec}/"
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                if logger:
                    logger.warning(f"  Remote listing failed: {result.stderr.strip()}")
                return manifest

            for line in result.stdout.strip().split("\n"):
                # rsync --list-only format:
                # -rw-r--r--    1,234 2026/03/06 14:23:45 path/to/file
                line = line.strip()
                if not line or line.startswith("d"):  # skip dirs
                    continue
                parts = line.split(None, 4)
                if len(parts) >= 5:
                    try:
                        dt_str = f"{parts[2]} {parts[3]}"
                        dt = datetime.datetime.strptime(dt_str, "%Y/%m/%d %H:%M:%S")
                        mtime = dt.timestamp()
                        filepath = parts[4]
                        manifest[filepath] = mtime
                    except (ValueError, IndexError):
                        continue
        except subprocess.TimeoutExpired:
            if logger:
                logger.warning("  Remote listing timed out.")

    else:
        base = Path(path)
        if not base.exists():
            return manifest
        for root, dirs, files in os.walk(base):
            # Skip excluded directories
            dirs[:] = [d for d in dirs if d not in (
                ".cache", "__pycache__", "node_modules", ".Trash")]
            for fname in files:
                if "(from " in fname or fname.endswith((".tmp", ".swp", "~")):
                    continue
                fpath = Path(root) / fname
                try:
                    rel = str(fpath.relative_to(base))
                    manifest[rel] = fpath.stat().st_mtime
                except (OSError, ValueError):
                    continue

    return manifest


def load_last_manifest(sync_name: str) -> dict:
    """Load the file manifest from the last successful sync."""
    TIMESTAMP_DIR.mkdir(parents=True, exist_ok=True)
    manifest_file = TIMESTAMP_DIR / f"{sync_name}.json"
    if manifest_file.exists():
        with open(manifest_file) as f:
            return json.load(f)
    return {}


def save_manifest(sync_name: str, manifest: dict) -> None:
    """Save the combined manifest after a successful sync."""
    TIMESTAMP_DIR.mkdir(parents=True, exist_ok=True)
    manifest_file = TIMESTAMP_DIR / f"{sync_name}.json"
    with open(manifest_file, "w") as f:
        json.dump(manifest, f)


def detect_conflicts(local_manifest: dict, remote_manifest: dict,
                     last_manifest: dict, logger: logging.Logger) -> list:
    """
    Find files modified on BOTH sides since last sync.
    Returns list of relative paths that are in conflict.
    """
    conflicts = []
    common_files = set(local_manifest.keys()) & set(remote_manifest.keys())

    for filepath in common_files:
        local_mtime = local_manifest[filepath]
        remote_mtime = remote_manifest[filepath]
        last_mtime = last_manifest.get(filepath, 0)

        # Both modified since last sync?
        local_changed = abs(local_mtime - last_mtime) > 1.0
        remote_changed = abs(remote_mtime - last_mtime) > 1.0

        if local_changed and remote_changed:
            # And they differ from each other?
            if abs(local_mtime - remote_mtime) > 1.0:
                conflicts.append(filepath)
                logger.warning(f"  CONFLICT: {filepath}")
                logger.warning(f"    Local modified:  {_fmt_time(local_mtime)}")
                logger.warning(f"    Remote modified: {_fmt_time(remote_mtime)}")

    return conflicts


def _fmt_time(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


# ── Conflict Resolution (rename strategy) ─────────────────────────────────

def handle_conflicts(local_path: str, conflicts: list,
                     local_manifest: dict, remote_manifest: dict,
                     device_name: str, hub_name: str,
                     logger: logging.Logger) -> int:
    """
    For each conflicting file, preserve BOTH versions labelled with their
    origin device (OneDrive style). The newest version stays as the live file.

    Example: if report.docx was edited on both acer-laptop and aiserver:
      report.docx                                    ← newest version (kept as-is)
      report (from acer-laptop 2026-03-06).docx      ← older version
    
    Both versions are always preserved so nothing is lost.
    """
    handled = 0
    ts = datetime.datetime.now().strftime("%Y-%m-%d")

    for filepath in conflicts:
        src = Path(local_path) / filepath
        if not src.exists():
            continue

        local_mtime = local_manifest.get(filepath, 0)
        remote_mtime = remote_manifest.get(filepath, 0)

        stem = src.stem
        suffix = src.suffix
        parent = src.parent

        # The local version is here now. We need to save it with an origin label
        # BEFORE rsync pulls the remote version (which may overwrite it).
        # 
        # Label it with THIS device's name since that's where it was edited.
        local_label = f"{stem} (from {device_name} {ts}){suffix}"
        local_dest = parent / local_label

        try:
            shutil.copy2(str(src), str(local_dest))
            logger.info(f"  Saved: {local_label}")
            handled += 1
        except OSError as e:
            logger.error(f"  Failed to preserve local {filepath}: {e}")
            continue

        # After the pull step, rsync will bring in the remote version.
        # If remote is newer, rsync --update will overwrite the original.
        # If local is newer, rsync --update will skip it.
        #
        # To ensure BOTH labelled copies exist regardless, we also need to
        # handle the case where the local version is newer (rsync won't 
        # overwrite it, so we won't get the remote version automatically).
        # 
        # We handle this by removing --update for conflict files and letting
        # rsync always pull them. The local is already safely copied above.
        # This is done in sync_set() by removing the original before pull.

        # Delete the original so rsync pull will definitely bring the remote version
        try:
            src.unlink()
        except OSError:
            pass  # If we can't delete, rsync --update will handle it

    return handled


# ── rsync Execution ────────────────────────────────────────────────────────

def build_rsync_cmd(source: str, dest: str, config: dict,
                    exclude_files: list = None,
                    dry_run: bool = False) -> list:
    opts = config.get("rsync_options", {})

    cmd = ["rsync", "-a", "--update"]  # -a for archive, --update skips newer files

    if opts.get("compress", True):
        cmd.append("-z")
    if opts.get("partial", True):
        cmd.append("--partial")
    if dry_run:
        cmd.append("--dry-run")

    cmd.extend(["--info=progress2,stats2", "--human-readable"])

    bw = opts.get("bandwidth_limit_kbps", 0)
    if bw and bw > 0:
        cmd.append(f"--bwlimit={bw}")

    for pattern in opts.get("exclude_patterns", []):
        cmd.extend(["--exclude", pattern])

    # Exclude specific conflicting files from this transfer
    if exclude_files:
        for f in exclude_files:
            cmd.extend(["--exclude", f])

    cmd.extend(["-e", "ssh -o ConnectTimeout=10 -o BatchMode=yes"])

    if not source.endswith("/"):
        source += "/"
    cmd.append(source)
    cmd.append(dest)

    return cmd


def run_rsync(cmd: list, label: str, logger: logging.Logger) -> bool:
    logger.debug(f"  {label}: {' '.join(cmd)}")
    start = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        elapsed = time.time() - start

        if proc.returncode == 0:
            logger.info(f"  {label}: completed in {elapsed:.1f}s")
            for line in proc.stdout.strip().split("\n")[-4:]:
                line = line.strip()
                if line:
                    logger.debug(f"    {line}")
            return True
        else:
            logger.error(f"  {label}: failed (exit {proc.returncode})")
            logger.error(f"    {proc.stderr.strip()}")
            return False

    except subprocess.TimeoutExpired:
        logger.error(f"  {label}: timed out after 1 hour")
        return False


# ── Sync Orchestration ─────────────────────────────────────────────────────

def sync_set(sync_set_config: dict, config: dict,
             dry_run: bool, logger: logging.Logger) -> dict:
    """
    Two-way sync for one sync set.

    Strategy:
      1. List files on both sides
      2. Compare with last-sync manifest to detect conflicts
      3. Rename local copies of conflicting files (preserving them)
      4. Pull: rsync remote → local (gets remote changes + resolves conflicts)
      5. Push: rsync local → remote (sends local changes to hub)
      6. Save new manifest

    The --update flag on rsync means "skip files that are newer on the
    destination", which handles the simple case where only one side changed.
    Conflicts (both sides changed) are handled by the rename step.
    """
    name = sync_set_config["name"]
    local_path = sync_set_config["local_path"]
    hub_path = sync_set_config["hub_path"]
    device_name = config["this_device"]["name"]
    hub = config["hub"]

    result = {
        "name": name,
        "success": False,
        "skipped": False,
        "conflicts": 0,
        "message": "",
        "started": datetime.datetime.now().isoformat(),
        "duration_seconds": 0
    }

    if not sync_set_config.get("enabled", True):
        logger.info(f"  [{name}] Skipped (disabled)")
        result["skipped"] = True
        result["message"] = "Disabled"
        return result

    # On the AI Server itself, this is the hub — nothing to sync
    if device_name == "aiserver":
        logger.info(f"  [{name}] This IS the hub. Nothing to sync.")
        result["skipped"] = True
        result["message"] = "This device is the hub"
        return result

    # Check local path exists (create if not)
    if not os.path.exists(local_path):
        logger.info(f"  [{name}] Creating local path: {local_path}")
        if not dry_run:
            os.makedirs(local_path, exist_ok=True)

    remote_spec = f"{hub['user']}@{hub['tailscale_hostname']}:{hub_path}"

    prefix = "[DRY RUN] " if dry_run else ""
    logger.info(f"  {prefix}[{name}] {local_path} ↔ {remote_spec}")

    start_time = time.time()

    # Ensure remote directory exists
    if not dry_run:
        try:
            subprocess.run(
                ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
                 f"{hub['user']}@{hub['tailscale_hostname']}",
                 f"mkdir -p '{hub_path}'"],
                capture_output=True, text=True, timeout=15, check=True
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.error(f"  [{name}] Cannot create remote dir: {e}")
            result["message"] = f"Remote dir creation failed: {e}"
            return result

    # Step 1-2: Detect conflicts
    logger.info(f"  [{name}] Scanning for changes...")
    local_manifest = get_file_manifest(local_path, logger=logger)
    remote_manifest = get_file_manifest(
        hub_path, is_remote=True,
        remote_spec=remote_spec,
        logger=logger
    )
    last_manifest = load_last_manifest(name)

    conflicts = detect_conflicts(
        local_manifest, remote_manifest, last_manifest, logger
    )

    # Step 3: Handle conflicts
    if conflicts:
        result["conflicts"] = len(conflicts)
        logger.info(f"  [{name}] {len(conflicts)} conflict(s) detected")
        if not dry_run:
            hub_name = hub.get("tailscale_hostname", "aiserver")
            handled = handle_conflicts(
                local_path, conflicts,
                local_manifest, remote_manifest,
                device_name, hub_name, logger
            )
            logger.info(f"  [{name}] {handled} conflict(s) — local versions saved with origin labels")
        else:
            logger.info(f"  [{name}] (dry run — both versions would be preserved with origin labels)")

    # Step 4: Pull remote → local
    pull_cmd = build_rsync_cmd(
        source=remote_spec,
        dest=local_path,
        config=config,
        dry_run=dry_run
    )
    pull_ok = run_rsync(pull_cmd, f"[{name}] PULL", logger)

    # After pull, label the remote versions of conflicted files
    if conflicts and not dry_run and pull_ok:
        ts = datetime.datetime.now().strftime("%Y-%m-%d")
        hub_name = hub.get("tailscale_hostname", "aiserver")
        for filepath in conflicts:
            pulled = Path(local_path) / filepath
            if pulled.exists():
                stem = pulled.stem
                suffix = pulled.suffix
                remote_label = f"{stem} (from {hub_name} {ts}){suffix}"
                remote_dest = pulled.parent / remote_label
                try:
                    shutil.copy2(str(pulled), str(remote_dest))
                    logger.info(f"  Saved: {remote_label}")
                except OSError as e:
                    logger.error(f"  Failed to label remote version of {filepath}: {e}")

    # Step 5: Push local → remote
    push_cmd = build_rsync_cmd(
        source=local_path,
        dest=remote_spec,
        config=config,
        dry_run=dry_run
    )
    push_ok = run_rsync(push_cmd, f"[{name}] PUSH", logger)

    elapsed = time.time() - start_time
    result["duration_seconds"] = round(elapsed, 1)
    result["success"] = pull_ok and push_ok
    result["message"] = "OK" if result["success"] else "Sync error (check log)"

    # Step 6: Save manifest for next conflict detection
    if result["success"] and not dry_run:
        # Re-scan local (which now has the merged state)
        merged_manifest = get_file_manifest(local_path, logger=logger)
        save_manifest(name, merged_manifest)

    return result


def run_sync(config: dict, dry_run: bool, logger: logging.Logger) -> dict:
    device_name = config["this_device"]["name"]
    hub_host = config["hub"]["tailscale_hostname"]
    prefix = "DRY RUN - " if dry_run else ""

    logger.info("=" * 60)
    logger.info(f"{prefix}Device Sync: {device_name} ↔ {hub_host}")
    logger.info(f"Started: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # Don't check connectivity if we ARE the hub
    if device_name != "aiserver":
        if not check_reachable(hub_host, config["hub"]["user"], logger):
            return {
                "device": device_name, "hub": hub_host,
                "success": False, "message": "Hub unreachable",
                "started": datetime.datetime.now().isoformat(), "sets": []
            }

    results = []
    for sset in config.get("sync_sets", []):
        status = sync_set(sset, config, dry_run, logger)
        results.append(status)

    attempted = [r for r in results if not r.get("skipped")]
    succeeded = [r for r in attempted if r.get("success")]
    failed = [r for r in attempted if not r.get("success")]
    total_conflicts = sum(r.get("conflicts", 0) for r in results)

    logger.info("-" * 60)
    logger.info(
        f"Complete: {len(succeeded)} OK, {len(failed)} failed, "
        f"{len(results) - len(attempted)} skipped"
    )
    if total_conflicts:
        logger.info(f"  {total_conflicts} conflict(s) — look for '(from ...)' files")
    if failed:
        for r in failed:
            logger.warning(f"  FAILED: {r['name']} — {r['message']}")
    logger.info("=" * 60)

    overall = {
        "device": device_name, "hub": hub_host,
        "dry_run": dry_run,
        "success": len(failed) == 0 and len(succeeded) > 0,
        "started": datetime.datetime.now().isoformat(),
        "summary": f"{len(succeeded)} OK, {len(failed)} failed, {total_conflicts} conflicts",
        "sets": results
    }

    if not dry_run:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATUS_FILE, "w") as f:
            json.dump(overall, f, indent=2)

    return overall


# ── Status & Conflict Listing ──────────────────────────────────────────────

def show_status(logger: logging.Logger) -> None:
    if not STATUS_FILE.exists():
        logger.info("No sync has been run yet.")
        return

    with open(STATUS_FILE) as f:
        status = json.load(f)

    logger.info(f"Last sync: {status.get('started', 'unknown')}")
    logger.info(f"Device: {status.get('device', '?')} ↔ {status.get('hub', '?')}")
    logger.info(f"Result: {status.get('summary', '?')}")
    logger.info("")

    for s in status.get("sets", []):
        if s.get("skipped"):
            flag = "SKIP"
        elif s.get("success"):
            flag = " OK "
        else:
            flag = "FAIL"
        conflicts = f"  ({s['conflicts']} conflicts)" if s.get("conflicts") else ""
        logger.info(
            f"  [{flag}] {s['name']:<20} "
            f"{s.get('duration_seconds', 0):>6.1f}s  "
            f"{s.get('message', '')}{conflicts}"
        )


def list_conflicts(config: dict, logger: logging.Logger) -> None:
    """Find all '(from ...)' conflict files in sync set paths."""
    found = 0
    for sset in config.get("sync_sets", []):
        local_path = Path(sset["local_path"])
        if not local_path.exists():
            continue

        for fpath in local_path.rglob("*(from *"):
            if found == 0:
                logger.info("Unresolved conflict files:")
                logger.info("")
            rel = fpath.relative_to(local_path)
            mtime = datetime.datetime.fromtimestamp(fpath.stat().st_mtime)
            logger.info(f"  {sset['name']}/{rel}  ({mtime.strftime('%Y-%m-%d %H:%M')})")
            found += 1

    if found == 0:
        logger.info("No conflict files found. All clean.")
    else:
        logger.info("")
        logger.info(f"{found} conflict file(s). Compare with originals and delete when resolved.")


# ── Entry Point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Two-way sync with AI Server via rsync + Tailscale",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  device_sync.py --init            Create config (first time)
  device_sync.py --dry-run         Preview sync without changes
  device_sync.py                   Run the sync
  device_sync.py --status          Last sync result
  device_sync.py --resolve         List unresolved conflict files

Config: ~/.config/device-sync/sync.json
Logs:   ~/.local/share/device-sync/sync.log
        """
    )
    parser.add_argument("--init", action="store_true",
                        help="Create default config file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview only — no files transferred")
    parser.add_argument("--status", action="store_true",
                        help="Show last sync result")
    parser.add_argument("--resolve", action="store_true",
                        help="List unresolved conflict files")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Detailed output")

    args = parser.parse_args()

    if args.init:
        init_config()
        return

    logger = setup_logging(verbose=args.verbose)

    if args.status:
        show_status(logger)
        return

    config = load_config(logger)

    if args.resolve:
        list_conflicts(config, logger)
        return

    result = run_sync(config, dry_run=args.dry_run, logger=logger)
    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    main()
