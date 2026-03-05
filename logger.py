from __future__ import annotations

import json
import os
from datetime import datetime

LOG_DIR = "logs"

def _utc() -> str:
    return datetime.utcnow().isoformat() + "Z"

def _append_jsonl(path: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")

def log_error(msg: str) -> None:
    _append_jsonl(os.path.join(LOG_DIR, "error.jsonl"), {"ts": _utc(), "msg": msg})