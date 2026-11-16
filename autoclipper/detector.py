"""Finds 'moments' from chat alone - no AI.

A moment is when, in the last 10 seconds:
  * chat is N times faster than its normal speed (median of the previous 5 minutes), with
    enough messages and enough *different* people (so one spammer can't trigger it), OR
  * several different people type "clip" / "clip it".
"""
from __future__ import annotations

import re
from collections import Counter, deque
from dataclasses import dataclass, field

WINDOW = 10        # seconds looked at "right now"
HISTORY = 300      # seconds used to learn the normal chat speed
MAX_PER_USER = 3   # one person can add at most this many messages to a 10-second window

HYPE_WORDS = {
    "kekw", "lul", "lulw", "omegalul", "lmao", "lmfao", "lol", "xd", "icant", "kek", "aware",
    "pog", "pogchamp", "poggers", "pogu", "w", "ww", "www", "dub", "goated", "insane", "holy",
    "monkas", "monkaw", "wtf", "nooo", "noooo", "sadge", "omg", "o7", "ez", "clutch", "deadge",
    "wow", "bro", "gg", "rip",
}
CLIP_RE = re.compile(r"\bclip+(\s*(it|that|this))?\b", re.IGNORECASE)


def is_hype(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if set(t) <= set("?!") and len(t) >= 2:          # "????" / "!!!"
        return True
    words = {w.strip(".,!?:").lower() for w in t.split()}
    return bool(words & HYPE_WORDS)


@dataclass
class Moment:
    channel: str
    reason: str
    score: float
    messages: int
    chatters: int
    normal: float      # normal messages per 10s
    hype_share: float
    chat: list[str] = field(default_factory=list)  # what chat said, for AI titles


class ChannelDetector:
    def __init__(self, channel: str, cfg, now: float):
        self.channel = channel
        self.cfg = cfg
        self.started = now
        self.msgs: deque[tuple[float, str, bool, bool, str]] = deque()

    def add(self, ts: float, user: str, text: str):
        self.msgs.append((ts, user, is_hype(text), bool(CLIP_RE.search(text)), text[:120]))

    def check(self, now: float) -> Moment | None:
        while self.msgs and self.msgs[0][0] < now - HISTORY:
            self.msgs.popleft()
        if now - self.started < self.cfg.warmup_seconds:
            return None

        recent = [m for m in self.msgs if now - WINDOW <= m[0] <= now]
        if not recent:
            return None
        per_user = Counter(m[1] for m in recent)
        n = sum(min(c, MAX_PER_USER) for c in per_user.values())  # spammers count only a little
        chatters = len(per_user)
        clip_callers = len({m[1] for m in recent if m[3]})
        hype_share = sum(m[2] for m in recent) / len(recent)

        history_start = max(self.started, now - HISTORY)
        buckets = int((now - WINDOW - history_start) // WINDOW)
        if buckets < 3:
            return None
        counts = [0] * buckets
        for ts, *_ in self.msgs:
            if ts < now - WINDOW:
                i = int((now - WINDOW - ts) // WINDOW)
                if i < buckets:
                    counts[i] += 1
        normal = max(float(sorted(counts)[len(counts) // 2]), 2.0)
        ratio = n / normal

        reason = None
        if n >= self.cfg.min_messages and chatters >= self.cfg.min_chatters and ratio >= self.cfg.multiplier:
            reason = f"chat x{ratio:.1f} faster than normal"
        elif clip_callers >= self.cfg.clip_calls:
            reason = f"{clip_callers} people typed 'clip'"
        if not reason:
            return None
        return Moment(self.channel, reason, round(ratio * (1 + hype_share), 2), n, chatters, normal,
                      round(hype_share, 2), [m[4] for m in recent[-30:]])
