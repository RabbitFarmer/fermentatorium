from __future__ import annotations

import json
from pathlib import Path

# Anchor all runtime files relative to the repo/app directory,
# not the current working directory.
BASE_DIR = Path(__file__).resolve().parent

BATCHES_DIR = BASE_DIR / "batches"
EXPORT_DIR = BASE_DIR / "export"
LOGS_DIR = BASE_DIR / "logs"
TEMP_CONTROL_DIR = BASE_DIR / "temp_control"
CONFIG_DIR = BASE_DIR / "config"

def ensure_dirs() -> None:
    BATCHES_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def batch_jsonl_path(brewid: str) -> str:
    ensure_dirs()
    return str(BATCHES_DIR / f"{brewid}.jsonl")

def append_event_jsonl(path: str, event: dict) -> None:
    ensure_dirs()
    p = Path(path)
    # If caller passes a relative path, interpret it relative to BASE_DIR
    if not p.is_absolute():
        p = BASE_DIR / p
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")

def append_sample(brewid: str, payload: dict) -> None:
    append_event_jsonl(batch_jsonl_path(brewid), {"event": "sample", "payload": payload})

def read_jsonl(path: str, limit: int | None = None) -> list[dict]:
    p = Path(path)
    if not p.is_absolute():
        p = BASE_DIR / p
    if not p.exists():
        return []
    out: list[dict] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
            if limit and len(out) >= limit:
                break
    return out
