#!/usr/bin/env python3
"""
Purge over-frequent tilt readings from JSONL data files.

Background
----------
The system stores two types of interval-based data:

  1. Tilt readings (TEMPERATURE + GRAVITY) — written by log_tilt_reading()
     Every active Tilt device should be sampled at the "Tilt Reading Logging
     Interval" (system_cfg['tilt_logging_interval_minutes'], default 15 min).

  2. Temperature-control readings (TEMPERATURE only) — written by
     log_periodic_temp_reading() into the in-memory buffer at the
     "Update Interval" (system_cfg['update_interval'], default 2 min).

A previous version of the code incorrectly used update_interval (e.g. 2 min)
for tilt readings belonging to a temperature-controlled brew instead of
tilt_logging_interval_minutes (e.g. 15 min).  This script retroactively
enforces the correct interval on existing data files so that charts show
the right density of points.

What the script does
--------------------
  * Reads system_cfg to obtain the configured intervals.
  * For every batch JSONL file (batches/*.jsonl):
      - Keeps the *first* sample entry within each tilt_logging_interval window
        and discards subsequent entries that fall inside the same window.
  * For every per-color temp-control log (temp_control/<color>_log.jsonl):
      - Applies the same interval filter to 'tilt_reading' / 'SAMPLE' events
        using tilt_logging_interval_minutes.
      - 'TEMP CONTROL READING' entries (temperature-only) are left untouched;
        they were already written at update_interval and are correct.
  * Creates a timestamped backup of each modified file before overwriting.
  * A --dry-run flag prints what would be changed without writing anything.

Usage (run from the repository root directory)
----------------------------------------------
    # Preview what would be removed (no files changed):
    python3 utils/purge_excess_tilt_readings.py --dry-run

    # Apply using intervals from config/system_config.json:
    python3 utils/purge_excess_tilt_readings.py

    # Override intervals on the command line:
    python3 utils/purge_excess_tilt_readings.py --tilt-interval 15 --update-interval 2

    # Process only specific files:
    python3 utils/purge_excess_tilt_readings.py --batch-files batches/myBrew.jsonl
"""

import argparse
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (resolved relative to this script's location → repo root)
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

SYSTEM_CFG_PATH = REPO_ROOT / "config" / "system_config.json"
BATCHES_DIR = REPO_ROOT / "batches"
TEMP_CONTROL_DIR = REPO_ROOT / "temp_control"

DEFAULT_TILT_INTERVAL = 15   # minutes
DEFAULT_UPDATE_INTERVAL = 2  # minutes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_system_config(path: Path) -> dict:
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            print(f"[WARN] Could not read {path}: {exc}")
    return {}


def parse_ts(ts_str) -> datetime | None:
    """Parse an ISO-8601 timestamp string into a UTC-aware datetime."""
    if not ts_str:
        return None
    s = str(ts_str).strip()
    # Handle trailing Z
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Try common fallback format
        try:
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def make_backup(path: Path) -> Path:
    """Copy *path* to *path*.<timestamp>.bak and return the backup path."""
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_suffix(f".{ts}.bak")
    shutil.copy2(path, backup)
    return backup


# ---------------------------------------------------------------------------
# Core filtering logic
# ---------------------------------------------------------------------------

def filter_tilt_sample_lines(lines: list[str], interval_minutes: int,
                              ts_extractor) -> tuple[list[str], int]:
    """
    Return (kept_lines, removed_count).

    *ts_extractor(obj)* should return the timestamp string for an entry that
    should be subject to rate-limiting; return None to always keep the entry.
    """
    kept: list[str] = []
    removed = 0
    last_kept_ts: dict[str, datetime] = {}   # key → last accepted timestamp

    for raw in lines:
        stripped = raw.rstrip("\n")
        if not stripped:
            kept.append(raw)
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            kept.append(raw)
            continue

        ts_str, rate_key = ts_extractor(obj)
        if ts_str is None or rate_key is None:
            # Not subject to rate limiting — always keep
            kept.append(raw)
            continue

        dt = parse_ts(ts_str)
        if dt is None:
            kept.append(raw)
            continue

        prev_dt = last_kept_ts.get(rate_key)
        if prev_dt is not None:
            elapsed_minutes = (dt - prev_dt).total_seconds() / 60.0
            if elapsed_minutes < interval_minutes:
                removed += 1
                continue   # Drop this reading — within the quiet window

        last_kept_ts[rate_key] = dt
        kept.append(raw)

    return kept, removed


# ---------------------------------------------------------------------------
# Batch file processing
# ---------------------------------------------------------------------------

def ts_extractor_batch(obj: dict):
    """
    Extract (timestamp_str, rate_key) for a batch JSONL entry.

    Batch files contain lines like:
      {"event": "sample", "payload": {"timestamp": ..., "tilt_color": ..., ...}}
    or legacy direct format:
      {"timestamp": ..., "tilt_color": ..., ...}

    Returns (None, None) for entries that should always be kept (e.g. metadata).
    """
    event = obj.get("event", "")
    if event == "batch_metadata":
        return None, None
    if event == "sample":
        payload = obj.get("payload", {})
        ts = payload.get("timestamp")
        color = str(payload.get("tilt_color", "")).lower() or "unknown"
        mac = str(payload.get("mac", ""))
        rate_key = f"{color}:{mac}" if mac else color
        return ts, rate_key
    # Legacy direct format — treat any entry with a timestamp as a sample
    if "timestamp" in obj or "gravity" in obj:
        ts = obj.get("timestamp")
        color = str(obj.get("tilt_color", "")).lower() or "unknown"
        mac = str(obj.get("mac", ""))
        rate_key = f"{color}:{mac}" if mac else color
        return ts, rate_key
    # Unknown / non-sample event — keep as-is
    return None, None


def process_batch_file(path: Path, interval_minutes: int,
                       dry_run: bool) -> tuple[int, int]:
    """Process one batch JSONL file. Returns (kept, removed)."""
    with path.open("r", encoding="utf-8") as f:
        lines = f.readlines()

    kept_lines, removed = filter_tilt_sample_lines(
        lines, interval_minutes, ts_extractor_batch
    )

    if removed == 0:
        print(f"  {path.name}: no excess readings found.")
        return len(kept_lines), 0

    print(f"  {path.name}: {removed} reading(s) removed, {len(kept_lines)} kept.")
    if not dry_run:
        backup = make_backup(path)
        print(f"    Backup saved: {backup.name}")
        with path.open("w", encoding="utf-8") as f:
            f.writelines(kept_lines)

    return len(kept_lines), removed


# ---------------------------------------------------------------------------
# Temp-control log processing
# ---------------------------------------------------------------------------

TILT_SAMPLE_EVENTS = {"tilt_reading", "SAMPLE"}

def ts_extractor_control_log(obj: dict):
    """
    Extract (timestamp_str, rate_key) for a temp-control JSONL entry.

    Only applies rate-limiting to tilt_reading / SAMPLE events.
    All other events (state changes, errors, TEMP CONTROL READING, etc.)
    are left untouched.
    """
    event = obj.get("event", "")
    if event not in TILT_SAMPLE_EVENTS:
        return None, None

    # Flat format used by append_control_log()
    ts = obj.get("timestamp")
    color = str(obj.get("tilt_color", "")).lower() or "unknown"
    mac = str(obj.get("mac", ""))
    rate_key = f"{color}:{mac}" if mac else color
    return ts, rate_key


def find_temp_control_logs() -> list[Path]:
    """Return all *_log.jsonl files in the temp_control directory."""
    if not TEMP_CONTROL_DIR.exists():
        return []
    return sorted(TEMP_CONTROL_DIR.glob("*_log.jsonl"))


def process_control_log(path: Path, interval_minutes: int,
                        dry_run: bool) -> tuple[int, int]:
    """Process one temp-control log file. Returns (kept, removed)."""
    with path.open("r", encoding="utf-8") as f:
        lines = f.readlines()

    kept_lines, removed = filter_tilt_sample_lines(
        lines, interval_minutes, ts_extractor_control_log
    )

    if removed == 0:
        print(f"  {path.name}: no excess tilt readings found.")
        return len(kept_lines), 0

    print(f"  {path.name}: {removed} tilt reading(s) removed, {len(kept_lines)} kept.")
    if not dry_run:
        backup = make_backup(path)
        print(f"    Backup saved: {backup.name}")
        with path.open("w", encoding="utf-8") as f:
            f.writelines(kept_lines)

    return len(kept_lines), removed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Purge over-frequent tilt readings from Fermentatorium JSONL files."
    )
    parser.add_argument(
        "--tilt-interval",
        type=int,
        default=None,
        help=(
            "Tilt Reading Logging Interval in minutes "
            f"(default: read from system_config.json, fallback {DEFAULT_TILT_INTERVAL})"
        ),
    )
    parser.add_argument(
        "--update-interval",
        type=int,
        default=None,
        help=(
            "Update Interval in minutes — only used to report the configured value; "
            "TEMP CONTROL READING entries are not filtered by this script "
            f"(default: read from system_config.json, fallback {DEFAULT_UPDATE_INTERVAL})"
        ),
    )
    parser.add_argument(
        "--batch-files",
        nargs="*",
        default=None,
        help="Specific batch JSONL files to process (default: all files in batches/).",
    )
    parser.add_argument(
        "--no-batch",
        action="store_true",
        help="Skip batch JSONL files.",
    )
    parser.add_argument(
        "--no-control-logs",
        action="store_true",
        help="Skip temp-control log files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be changed without writing any files.",
    )
    args = parser.parse_args()

    # ---- Load system config -----------------------------------------------
    sys_cfg = load_system_config(SYSTEM_CFG_PATH)

    if args.tilt_interval is not None:
        tilt_interval = args.tilt_interval
    else:
        try:
            tilt_interval = int(sys_cfg.get("tilt_logging_interval_minutes", DEFAULT_TILT_INTERVAL))
        except (ValueError, TypeError):
            tilt_interval = DEFAULT_TILT_INTERVAL

    if args.update_interval is not None:
        update_interval = args.update_interval
    else:
        try:
            update_interval = int(sys_cfg.get("update_interval", DEFAULT_UPDATE_INTERVAL))
        except (ValueError, TypeError):
            update_interval = DEFAULT_UPDATE_INTERVAL

    print("=" * 60)
    print("Fermentatorium — Purge Excess Tilt Readings")
    print("=" * 60)
    print(f"  Tilt Reading Logging Interval : {tilt_interval} min")
    print(f"  Update Interval               : {update_interval} min  (info only)")
    if args.dry_run:
        print("  *** DRY RUN — no files will be modified ***")
    print()

    total_removed = 0

    # ---- Batch files -------------------------------------------------------
    if not args.no_batch:
        if args.batch_files:
            batch_paths = [Path(p) for p in args.batch_files]
        else:
            batch_paths = sorted(BATCHES_DIR.glob("*.jsonl")) if BATCHES_DIR.exists() else []

        if batch_paths:
            print(f"Processing {len(batch_paths)} batch file(s) "
                  f"(interval = {tilt_interval} min):")
            for bp in batch_paths:
                if not bp.exists():
                    print(f"  {bp}: not found, skipping.")
                    continue
                _, removed = process_batch_file(bp, tilt_interval, args.dry_run)
                total_removed += removed
        else:
            print("No batch files found.")
        print()

    # ---- Temp-control logs -------------------------------------------------
    if not args.no_control_logs:
        control_logs = find_temp_control_logs()
        if control_logs:
            print(f"Processing {len(control_logs)} temp-control log(s) "
                  f"(tilt_reading interval = {tilt_interval} min):")
            for cp in control_logs:
                _, removed = process_control_log(cp, tilt_interval, args.dry_run)
                total_removed += removed
        else:
            print("No temp-control log files found.")
        print()

    # ---- Summary -----------------------------------------------------------
    action = "would be removed" if args.dry_run else "removed"
    print(f"Total readings {action}: {total_removed}")
    if args.dry_run and total_removed > 0:
        print("Re-run without --dry-run to apply these changes.")


if __name__ == "__main__":
    main()
