"""The finished videos in the final folder and what we know about each one.

Each final video has a sidecar file with the same name + .json:
    meta    - streamer, game, Twitch link, title on the video, what was said...
    post    - the YouTube title/description/tags and TikTok caption (Gemini-written, editable)
    youtube - upload result (video id, url, when)
    tiktok  - upload result (publish id, when)
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from .config import FINAL_DIR

_lock = threading.Lock()

TXT_KEYS = {
    "Title on video": "hook", "Post title": "post_title", "AI score": "ai_score",
    "Twitch clip": "clip_url", "Game": "game", "Why clipped": "why",
}


def clip_id(video: Path) -> str:
    return video.resolve().relative_to(FINAL_DIR.resolve()).as_posix()


def video_path(cid: str) -> Path | None:
    """Turns an id from the web page back into a file - only inside final\\ (no tricks like ..\\)."""
    try:
        p = (FINAL_DIR / cid).resolve()
        p.relative_to(FINAL_DIR.resolve())
    except (ValueError, OSError):
        return None
    return p if p.suffix.lower() == ".mp4" and p.is_file() else None


def _meta_from_txt(video: Path) -> dict:
    """Older clips only have the .txt info file - read what we can from it."""
    meta: dict = {}
    txt = video.with_suffix(".txt")
    if not txt.exists():
        return meta
    lines = txt.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines:
        key, sep, val = line.partition(":")
        if sep and key.strip() in TXT_KEYS:
            meta[TXT_KEYS[key.strip()]] = val.strip().removesuffix("/10").strip(" -")
        if line.startswith("Streamer"):
            m = re.search(r":\s*(.+?)\s+\(twitch\.tv/([\w]+)\)", line)
            if m:
                meta["streamer"], meta["channel"] = m.group(1), m.group(2)
    if "What was said:" in lines:
        meta["transcript"] = " ".join(lines[lines.index("What was said:") + 1:]).strip(" -")
    return meta


def load(video: Path) -> dict:
    side = video.with_suffix(".json")
    data: dict = {}
    if side.exists():
        try:
            data = json.loads(side.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    if not data.get("meta"):
        data["meta"] = _meta_from_txt(video)
    data.setdefault("post", {})
    data.setdefault("youtube", {})
    data.setdefault("tiktok", {})
    return data


def save(video: Path, data: dict) -> None:
    with _lock:
        side = video.with_suffix(".json")
        tmp = side.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(side)


def update(video: Path, section: str, values: dict) -> dict:
    with _lock:
        data = load(video)
        data.setdefault(section, {}).update(values)
    save(video, data)
    return data


def all_videos() -> list[Path]:
    if not FINAL_DIR.exists():
        return []
    vids = [p for p in FINAL_DIR.rglob("*.mp4") if p.is_file()]
    return sorted(vids, key=lambda p: p.stat().st_mtime, reverse=True)
