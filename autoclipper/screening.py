"""Which live streams AutoClipper should not clip without asking you first.

Two levels, by how sure we are:
  SKIP - clear-cut gambling/betting streams. Skipped automatically, but always listed with the
         reason and an "Allow anyway" button, so nothing is hidden.
  ASK  - unclear cases (reruns, 24/7 replays, poker, case opening, huge viewer counts with almost no
         chat). Not clipped until you choose "Allow clips" or "Never clip".
Your choices are saved in channel_decisions.json and always win over these rules.
Only used for "game" and "most-watched" modes: streamers you list yourself are never screened.
"""
from __future__ import annotations

import json
import re
import threading
import time

from .config import ROOT

DECISIONS_PATH = ROOT / "channel_decisions.json"
_lock = threading.Lock()

GAMBLING_CATEGORIES = {"slots", "virtual casino"}
ASK_CATEGORIES = {"poker"}

# Betting / casino / gambling brands that sponsor streams (matched as whole words, any case)
BETTING_BRANDS = [
    "1win", "stake", "stake.com", "stake.us", "roobet", "gamdom", "rollbit", "bc.game", "bcgame", "duelbits",
    "betway", "bet365", "betfury", "shuffle.com", "rainbet", "chips.gg", "500casino", "csgoroll", "csgoempire",
    "clash.gg", "hypedrop", "parimatch", "1xbet", "melbet", "mostbet", "pin-up", "pinup", "ggbet", "gg.bet",
    "vavada", "casino", "slots", "sportsbook", "betting", "gambling", "bonus buy", "bonusbuy",
]
ASK_WORDS = {
    "rerun": "Says it's a rerun",
    "re-run": "Says it's a rerun",
    "replay": "Says it's a replay",
    "rebroadcast": "Says it's a rebroadcast",
    "restream": "Says it's a restream",
    "24/7": "A 24/7 stream (often a loop or replay)",
    "vod": "Looks like an old video (VOD), not live",
    "case opening": "Case opening (gambling-like)",
    "case battle": "Case opening (gambling-like)",
}
_brand_re = re.compile(r"(?<![\w.])(" + "|".join(re.escape(b) for b in BETTING_BRANDS) + r")(?![\w])", re.I)

# "Huge audience, silent chat" = possibly fake viewers, or chat that's off/followers-only
QUIET_MIN_VIEWERS = 2000
QUIET_MAX_MSGS_5MIN = 5


def screen(stream: dict) -> tuple[str, list[str]]:
    """Returns ("skip" | "ask" | "ok", reasons)."""
    login = stream.get("user_login", "").lower()
    title = stream.get("title", "") or ""
    game = (stream.get("game_name", "") or "").lower()
    tags = [t.lower() for t in (stream.get("tags") or [])]
    skip, ask = [], []

    if game in GAMBLING_CATEGORIES:
        skip.append(f"Gambling category ({stream.get('game_name')})")
    for where, text in (("channel name", login), ("title", title)):
        m = _brand_re.search(text.replace("_", " ").replace("-", " ") if where == "channel name" else text)
        if m:
            skip.append(f"Betting or casino words in the {where} (\"{m.group(1)}\")")
            break

    if game in ASK_CATEGORIES:
        ask.append(f"{stream.get('game_name')}: real-money gambling is common here")
    if login.endswith(("_vod", "vod", "_replay", "_rerun", "tv247", "_247")):
        ask.append("The channel name suggests old videos or replays")
    low = title.lower()
    for word, why in ASK_WORDS.items():
        if (word in low if not word.isalpha() else re.search(rf"\b{re.escape(word)}\b", low)) and why not in ask:
            ask.append(why)
    if "rerun" in tags:
        ask.append("Twitch marks it as a rerun")

    if skip:
        return "skip", skip
    if ask:
        return "ask", ask
    return "ok", []


def quiet_chat_reason(viewers: int, msgs_5min: int) -> str | None:
    if viewers >= QUIET_MIN_VIEWERS and msgs_5min <= QUIET_MAX_MSGS_5MIN:
        return (f"{viewers:,} viewers but only {msgs_5min} chat messages in 5 minutes: possibly fake viewers, "
                f"or chat is off or followers-only")
    return None


# ---------------------------------------------------------------- your decisions
def load_decisions() -> dict:
    try:
        return json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def set_decision(channel: str, decision: str | None, note: str = ""):
    """decision: 'allow', 'skip', or None to forget."""
    channel = channel.lower().strip().lstrip("@")
    with _lock:
        d = load_decisions()
        if decision is None:
            d.pop(channel, None)
        else:
            d[channel] = {"decision": decision, "date": time.strftime("%Y-%m-%d %H:%M"), "note": note[:200]}
        DECISIONS_PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")
