"""Files the clipper and the control center use to talk to each other.

The clipper runs as its own process (so it keeps going if the control page or its window
closes). It writes runtime/status.json every 2 seconds, and checks for runtime/stop.request.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .config import ROOT

RUNTIME_DIR = ROOT / "runtime"
STATUS_PATH = RUNTIME_DIR / "status.json"
STOP_PATH = RUNTIME_DIR / "stop.request"     # contents: "graceful" or "now"
LOG_PATH = RUNTIME_DIR / "clipper.log"
MAX_LOG_BYTES = 2_000_000
HEARTBEAT_MAX_AGE = 10                        # seconds without an update = not running

_log_lock = threading.Lock()


def ensure_dir():
    RUNTIME_DIR.mkdir(exist_ok=True)


def write_json(path: Path, data: dict):
    ensure_dir()
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    for _ in range(5):           # another process may be reading it; retry briefly
        try:
            tmp.replace(path)
            return
        except PermissionError:
            time.sleep(0.05)
    tmp.unlink(missing_ok=True)


def read_status() -> dict:
    """The clipper's last status, plus 'alive' (True only if it updated in the last few seconds)."""
    try:
        d = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "stopped", "alive": False}
    alive = time.time() - d.get("heartbeat", 0) < HEARTBEAT_MAX_AGE and d.get("state") not in ("stopped", "error")
    d["alive"] = alive
    if not alive and d.get("state") not in ("error",):
        d["state"] = "stopped"
    return d


def append_log(line: str):
    try:
        ensure_dir()
        with _log_lock:
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_LOG_BYTES:
                keep = LOG_PATH.read_bytes()[-MAX_LOG_BYTES // 2:]
                LOG_PATH.write_bytes(keep)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass


def tail_log(lines: int = 200) -> list[str]:
    try:
        data = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return data[-lines:]


def request_stop(mode: str = "graceful"):
    ensure_dir()
    STOP_PATH.write_text("now" if mode == "now" else "graceful", encoding="utf-8")


def read_stop_request() -> str | None:
    try:
        return STOP_PATH.read_text(encoding="utf-8").strip() or "graceful"
    except OSError:
        return None


def clear_stop_request():
    STOP_PATH.unlink(missing_ok=True)
