"""Settings: loaded from / saved to config.toml next to the start scripts."""
from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
TOKENS_PATH = ROOT / "tokens.json"
CLIPS_DIR = ROOT / "clips"         # original landscape clips from Twitch
FINAL_DIR = ROOT / "final"         # finished vertical videos with subtitles + title, ready to post
LOG_PATH = ROOT / "clips_log.csv"
BLOCKED_PATH = ROOT / "blocked_channels.json"   # channels that don't let us clip (remembered for 7 days)

# How much louder chat has to get (compared with its normal level) to count as a moment.
SENSITIVITY = {"low": 3.5, "medium": 2.5, "high": 1.8}


@dataclass
class Config:
    # --- Twitch app ---
    client_id: str = ""

    # --- What to watch ---
    mode: str = "channels"          # "channels" = a fixed list, "game" = top live streams of a game,
                                    # "top" = the most-watched live streams on Twitch (any game)
    channels: list[str] = field(default_factory=list)
    game: str = ""
    language: str = ""              # e.g. "en". Empty = any language (game mode only)
    max_streams: int = 5            # game mode: how many streams to watch at once
    min_viewers: int = 50           # game mode: ignore streams smaller than this
    exclude_channels: list[str] = field(default_factory=list)
    screen_streams: bool = True     # skip betting/gambling streams; ask about reruns, poker, fake-looking viewers

    # --- Clip limits ---
    clip_seconds: int = 60          # 5-60 (Twitch maximum is 60)
    max_clips_per_hour: int = 6
    cooldown_minutes: float = 5     # per channel
    delay_after_moment: float = 4   # wait this long after chat explodes, so the clip includes the reaction

    # --- Moment detection ---
    sensitivity: str = "medium"     # low / medium / high
    min_messages: int = 8           # messages needed in 10 seconds
    min_chatters: int = 5           # different people needed in 10 seconds
    clip_calls: int = 3             # this many people typing "clip" also triggers a clip
    warmup_seconds: int = 60        # learn a channel's normal chat speed before clipping

    # --- Output ---
    make_vertical: bool = True      # also make a 9:16 version for Shorts/TikTok/Reels

    # --- Text on the vertical video ---
    captions: bool = True           # word-by-word subtitles of what the streamer says (free, on your laptop)
    caption_model: str = "small"    # tiny / base / small / medium  (bigger = more accurate, slower)
    title_text: bool = True         # hook title at the top
    streamer_credit: bool = False   # "twitch.tv/name" under the gameplay (off: TikTok doesn't allow links on videos;
                                    # the credit goes in the caption instead)
    gemini_api_key: str = ""        # optional, free: aistudio.google.com/apikey -> AI-written hook titles
    gemini_model: str = "gemini-flash-lite-latest"

    # --- Publishing (publisher.bat) ---
    youtube_privacy: str = "public"   # public / unlisted / private
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    tiktok_username: str = ""         # your @name, used to build links to posted TikToks

    # --- Phone alerts (optional) ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @property
    def twitch_id(self) -> str:
        """Your own Twitch app's Client ID if you set one, otherwise AutoClipper's built-in one."""
        from .defaults import TWITCH_CLIENT_ID
        return self.client_id.strip() or TWITCH_CLIENT_ID

    @property
    def multiplier(self) -> float:
        return SENSITIVITY.get(self.sensitivity.lower(), SENSITIVITY["medium"])


_SECTIONS = [
    ("Twitch app  (leave client_id empty to use AutoClipper's built-in app)", ["client_id"]),
    ("What to watch  (mode = \"channels\" or \"game\")",
     ["mode", "channels", "game", "language", "max_streams", "min_viewers", "exclude_channels", "screen_streams"]),
    ("Clip limits", ["clip_seconds", "max_clips_per_hour", "cooldown_minutes", "delay_after_moment"]),
    ("Moment detection  (sensitivity = \"low\", \"medium\" or \"high\")",
     ["sensitivity", "min_messages", "min_chatters", "clip_calls", "warmup_seconds"]),
    ("Output", ["make_vertical"]),
    ("Text on the vertical video  (caption_model = \"tiny\", \"base\", \"small\" or \"medium\")",
     ["captions", "caption_model", "title_text", "streamer_credit", "gemini_api_key", "gemini_model"]),
    ("Publishing to YouTube / TikTok (set these up in publisher.bat)",
     ["youtube_privacy", "tiktok_client_key", "tiktok_client_secret", "tiktok_username"]),
    ("Phone alerts via Telegram (optional - leave empty to turn off)",
     ["telegram_bot_token", "telegram_chat_id"]),
]


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v))  # JSON string escapes are valid TOML basic strings


def load(path: Path = CONFIG_PATH) -> Config | None:
    if not path.exists():
        return None
    with open(path, "rb") as f:
        data = tomllib.load(f)
    known = {f.name for f in fields(Config)}
    cfg = Config(**{k: v for k, v in data.items() if k in known})
    cfg.channels = [c.strip().lower().lstrip("@") for c in cfg.channels if c.strip()]
    cfg.exclude_channels = [c.strip().lower().lstrip("@") for c in cfg.exclude_channels if c.strip()]
    cfg.clip_seconds = max(5, min(60, int(cfg.clip_seconds)))
    return cfg


def save(cfg: Config, path: Path = CONFIG_PATH) -> None:
    lines = ["# AutoClipper settings. Run setup.bat to change them with questions,",
             "# or edit this file with Notepad and restart start.bat.", ""]
    for title, keys in _SECTIONS:
        lines.append(f"# --- {title} ---")
        for k in keys:
            lines.append(f"{k} = {_toml_value(getattr(cfg, k))}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
