"""The main loop: find live streams -> listen to chat -> spot moments -> clip -> download -> edit -> alert."""
from __future__ import annotations

import asyncio
import csv
import json
import re
import sys
import time
from collections import deque
import os
from datetime import datetime

from . import config as config_mod
from . import library
from . import runtime
from . import screening
from .chat import TwitchChat
from .config import BLOCKED_PATH, CLIPS_DIR, FINAL_DIR, LOG_PATH, Config
from .detector import ChannelDetector, Moment
from .ai import Gemini
from .captions import build_ass, transcribe
from .media import download_clip, make_vertical
from .notify import Telegram
from .twitch import NotLoggedIn, Twitch, TwitchError


def log(msg: str):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    try:
        print(line, flush=True)
    except (OSError, ValueError):   # no console (started from the control center)
        pass
    runtime.append_log(line)


def keep_awake(on: bool):
    """Stops Windows from sleeping while AutoClipper runs (closing the lid can still sleep it)."""
    if sys.platform != "win32":
        return
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


class Limiter:
    def __init__(self, per_hour: int, cooldown_s: float):
        self.per_hour, self.cooldown_s = per_hour, cooldown_s
        self.times: deque[float] = deque()
        self.last: dict[str, float] = {}

    def allow(self, channel: str, now: float) -> tuple[bool, str]:
        while self.times and self.times[0] < now - 3600:
            self.times.popleft()
        if len(self.times) >= self.per_hour:
            wait = int((self.times[0] + 3600 - now) / 60) + 1
            return False, f"hourly limit reached ({self.per_hour}/hour, frees up in ~{wait} min)"
        since = now - self.last.get(channel, -1e9)
        if since < self.cooldown_s:
            return False, f"cooldown for this channel ({int((self.cooldown_s - since) / 60) + 1} min left)"
        return True, ""

    def record(self, channel: str, now: float):
        self.times.append(now)
        self.last[channel] = now

    def refund(self, stamp: float):
        """A clip that Twitch refused shouldn't use up the hourly allowance."""
        try:
            self.times.remove(stamp)
        except ValueError:
            pass

    @property
    def used(self) -> int:
        now = time.time()
        return sum(1 for t in self.times if t >= now - 3600)


def slug(text: str, max_len: int = 50) -> str:
    """'Wait is the marathon ending?!' -> 'Wait-is-the-marathon-ending' (safe as a Windows file name)."""
    words = re.sub(r"[^A-Za-z0-9 ]+", "", text).split()
    return "-".join(words)[:max_len].strip("-")


def write_info_file(path, info: dict, channel: str, url: str, reason: str, text: dict, with_text: bool):
    """A .txt next to each final video with everything needed to post it."""
    lines = [
        f"Title on video : {text['hook'] or '-'}",
        f"Post title     : {text['post_title'] or text['hook'] or '-'}",
        f"AI score       : {text['ai_score']}/10" if text["ai_score"] != "" else "AI score       : -",
        f"Streamer       : {info.get('user_name', channel)}  (twitch.tv/{channel})",
        f"Game           : {info.get('game_name', '')}",
        f"Twitch clip    : {url}",
        f"Why clipped    : {reason}",
        f"Subtitles/title: {'yes' if with_text else 'NO - text could not be added to this one'}",
        "",
        "Suggested description:",
        f"{text['post_title'] or text['hook']}",
        f"Credit: twitch.tv/{channel}",
        f"#{re.sub(r'[^A-Za-z0-9]', '', info.get('game_name', '')).lower()} #twitch #shorts",
        "",
        "What was said:",
        text["transcript"] or "-",
    ]
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


class BlockedChannels:
    """Channels where Twitch refused our clips (followers/subs-only clipping or clips turned off).
    Saved to blocked_channels.json so later runs skip them. Each entry expires after 7 days,
    in case the streamer changes their settings or you follow them on your clipper account."""
    DAYS = 7

    def __init__(self, path=BLOCKED_PATH):
        self.path = path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        cutoff = time.time() - self.DAYS * 86400
        self.entries: dict[str, dict] = {k: v for k, v in data.items()
                                         if isinstance(v, dict) and v.get("since", 0) > cutoff}

    def __contains__(self, channel: str) -> bool:
        return channel.lower() in self.entries

    def names(self) -> set[str]:
        return set(self.entries)

    def add(self, channel: str, reason: str):
        self.entries[channel.lower()] = {"since": time.time(), "reason": reason,
                                         "date": f"{datetime.now():%Y-%m-%d %H:%M}"}
        try:
            self.path.write_text(json.dumps(self.entries, indent=2), encoding="utf-8")
        except OSError:
            pass


def write_log_row(row: dict):
    if LOG_PATH.exists():  # columns changed in an update -> keep the old file, start a new one
        with open(LOG_PATH, encoding="utf-8-sig") as f:
            if f.readline().strip() != ",".join(row):
                f.close()
                LOG_PATH.replace(LOG_PATH.with_name(f"clips_log_old_{int(time.time())}.csv"))
    new = not LOG_PATH.exists()
    with open(LOG_PATH, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


class AutoClipper:
    def __init__(self, cfg: Config, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.tw = Twitch(cfg.twitch_id)
        self.tg = Telegram(cfg.telegram_bot_token, cfg.telegram_chat_id, log)
        self.ai = Gemini(cfg.gemini_api_key, cfg.gemini_model, log)
        self.chat = TwitchChat(self.tw, self.on_message, log)
        self.limiter = Limiter(cfg.max_clips_per_hour, cfg.cooldown_minutes * 60)
        self.live: dict[str, dict] = {}            # login -> stream info
        self.detectors: dict[str, ChannelDetector] = {}
        self.last_moment: dict[str, float] = {}
        self.blocked = BlockedChannels()           # channels where we're not allowed to clip
        self._restore_recent_clips()
        self.game_id: str | None = None
        self.clips_made = 0
        self._tasks: set[asyncio.Task] = set()
        # --- for the control center ---
        self.state = "starting"              # starting / running / stopping / stopped / error
        self.error = ""
        self.started_at = time.time()
        self.account = ""
        self.mode_summary = ""
        self.activity: dict[str, str] = {}   # channel -> what it's doing right now ("Downloading")
        self.events: deque[dict] = deque(maxlen=80)
        self.skipped = 0
        self.stop_mode: str | None = None
        self.stop_event: asyncio.Event | None = None
        self.screened: dict[str, dict] = {}       # streams skipped or waiting for your OK
        self.quiet: dict[str, str] = {}           # streams with a suspiciously quiet chat (this run)
        self._decisions_seen = 0.0

    # ---------- status for the control center ----------
    def ev(self, kind: str, text: str, channel: str = "", clip: str = ""):
        self.events.append({"t": time.time(), "kind": kind, "text": text, "channel": channel, "clip": clip})

    def status(self) -> dict:
        now = time.time()
        while self.limiter.times and self.limiter.times[0] < now - 3600:
            self.limiter.times.popleft()
        used = len(self.limiter.times)
        free_in = int(self.limiter.times[0] + 3600 - now) if used >= self.cfg.max_clips_per_hour and self.limiter.times else 0
        chans = []
        for login, s in sorted(self.live.items(), key=lambda kv: -kv[1].get("viewer_count", 0)):
            d = self.detectors.get(login)
            learning = max(0, int(self.cfg.warmup_seconds - (now - d.started))) if d else 0
            cooldown = max(0, int(self.limiter.last.get(login, -1e9) + self.limiter.cooldown_s - now))
            chans.append({"login": login, "name": s.get("user_name", login), "game": s.get("game_name", ""),
                          "viewers": s.get("viewer_count", 0), "title": s.get("title", "")[:90],
                          "learning": learning, "cooldown": cooldown, "activity": self.activity.get(login, "")})
        return {
            "pid": os.getpid(), "heartbeat": now, "started_at": self.started_at, "state": self.state,
            "error": self.error, "test_mode": self.dry_run, "account": self.account, "mode": self.cfg.mode,
            "mode_summary": self.mode_summary,
            "limits": {"per_hour": self.cfg.max_clips_per_hour, "used": used, "next_free_in": free_in,
                       "cooldown_min": self.cfg.cooldown_minutes, "clip_seconds": self.cfg.clip_seconds},
            "clips_saved": self.clips_made, "skipped": self.skipped, "working": len(self.activity),
            "stop_mode": self.stop_mode, "channels": chans, "events": list(self.events)[-60:],
            "screened": list(self.screened.values()),
        }

    def write_status(self):
        try:
            runtime.write_json(runtime.STATUS_PATH, self.status())
        except Exception:
            pass

    async def status_writer(self):
        while True:
            self.write_status()
            req = runtime.read_stop_request()
            if req and self.stop_event and not self.stop_event.is_set():
                if req == "now":
                    log("Stop requested: stopping now.")
                    self.ev("stop", "Stopped right away (any clip in progress was dropped).")
                    self.stop_event.set()
                elif self.stop_mode is None:
                    self.stop_mode = "graceful"
                    self.state = "stopping"
                    log("Stop requested: no new clips; finishing the clip in progress first.")
                    self.ev("stop", "Stopping: no new clips will be started.")
                if self.stop_mode == "graceful" and not self.activity:
                    self.stop_event.set()
            await asyncio.sleep(2)

    def _restore_recent_clips(self):
        """Counts clips from the last hour (from clips_log.csv), so restarting doesn't reset the
        clips-per-hour limit or the per-channel cooldowns."""
        if not LOG_PATH.exists():
            return
        try:
            with open(LOG_PATH, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    try:
                        ts = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S").timestamp()
                    except (KeyError, ValueError):
                        continue
                    if time.time() - ts < 3600 and row.get("why") != "manual test clip":
                        self.limiter.record(row.get("channel", ""), ts)
        except OSError:
            pass

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # ---------- chat ----------
    def on_message(self, channel: str, user: str, text: str):
        d = self.detectors.get(channel)
        if d:
            d.add(time.time(), user, text)

    # ---------- which streams to watch ----------
    async def pick_streams(self) -> list[dict]:
        cfg = self.cfg
        if cfg.mode in ("game", "top"):   # "top" = the most-watched live streams on Twitch, any game
            game_id = self.game_id if cfg.mode == "game" else None
            streams = await asyncio.to_thread(self.tw.get_streams, None, game_id, cfg.language or None, 100)
            skip = set(cfg.exclude_channels) | self.blocked.names()
            streams = [s for s in streams if s["viewer_count"] >= cfg.min_viewers and s["user_login"].lower() not in skip]
            return self.screen_streams(streams)
        streams = await asyncio.to_thread(self.tw.get_streams, cfg.channels)
        return [s for s in streams if s["user_login"].lower() not in self.blocked]

    def screen_streams(self, streams: list[dict]) -> list[dict]:
        """Keeps betting/gambling streams out, and holds unclear ones until you decide (see screening.py).
        Only the streams that would otherwise have been picked are listed, so the list stays short."""
        try:
            self._decisions_seen = screening.DECISIONS_PATH.stat().st_mtime
        except OSError:
            self._decisions_seen = 0.0
        decisions = screening.load_decisions()
        picked, screened = [], {}
        for s in streams:
            if len(picked) >= self.cfg.max_streams:
                break
            login = s["user_login"].lower()
            choice = decisions.get(login, {}).get("decision")
            if choice == "skip":
                continue
            if choice == "allow" or not self.cfg.screen_streams:
                picked.append(s)
                continue
            verdict, reasons = screening.screen(s)
            if login in self.quiet:
                verdict, reasons = "ask", reasons + [self.quiet[login]]
            if verdict == "ok":
                picked.append(s)
                continue
            screened[login] = {"login": login, "name": s.get("user_name", login), "game": s.get("game_name", ""),
                               "viewers": s.get("viewer_count", 0), "title": s.get("title", "")[:90],
                               "verdict": verdict, "reasons": reasons}
            if login not in self.screened:
                if verdict == "skip":
                    log(f"Skipped {login}: {reasons[0]}")
                    self.ev("screen", f"Skipped {s.get('user_name', login)}: {reasons[0]}", login)
                else:
                    log(f"Needs your OK before clipping {login}: {reasons[0]}")
                    self.ev("ask", f"{s.get('user_name', login)} needs your OK: {reasons[0]}", login)
        self.screened = screened
        return picked

    def check_quiet_chats(self):
        """Huge viewer count but nearly silent chat = possibly fake viewers (or chat is off). Ask first."""
        if not self.cfg.screen_streams or self.cfg.mode == "channels":
            return False
        decisions = screening.load_decisions()
        now, changed = time.time(), False
        for login, d in list(self.detectors.items()):
            if now - d.started < 300 or login in self.quiet or decisions.get(login, {}).get("decision") == "allow":
                continue
            viewers = self.live.get(login, {}).get("viewer_count", 0)
            why = screening.quiet_chat_reason(viewers, len(d.msgs))
            if why:
                self.quiet[login] = why
                changed = True
        return changed

    async def refresh_now(self):
        streams = await self.pick_streams()
        now_live = {s["user_login"].lower(): s for s in streams}
        for c in now_live.keys() - self.live.keys():
            s = now_live[c]
            log(f"Watching {s['user_name']} - {s['viewer_count']} viewers - {s['game_name']} - \"{s['title'][:60]}\"")
            self.ev("watch", f"Started watching {s['user_name']} ({s['game_name']}, {s['viewer_count']:,} viewers)", c)
            self.detectors[c] = ChannelDetector(c, self.cfg, time.time())
        for c in self.live.keys() - now_live.keys():
            why = ("doesn't allow clips" if c in self.blocked else
                   "needs your OK first" if self.screened.get(c, {}).get("verdict") == "ask" else
                   "skipped (betting or gambling)" if c in self.screened else "offline or no longer in the top list")
            log(f"Stopped watching {c} ({why})")
            self.ev("unwatch", f"Stopped watching {c}: {why}", c)
            self.detectors.pop(c, None)
        self.live = now_live
        await self.chat.set_channels(now_live.keys())

    async def watch_targets(self):
        every = 180 if self.cfg.mode in ("game", "top") else 60
        first = True
        if self.blocked.names():
            log(f"Skipping channels that don't allow our clips: {', '.join(sorted(self.blocked.names()))} "
                f"(delete blocked_channels.json to retry them)")
        while True:
            try:
                await self.refresh_now()
                now_live = self.live
                if first and not now_live:
                    log("Nobody is live right now - checking again every "
                        f"{every // 60} min. You can leave this running.")
                first = False
            except NotLoggedIn:
                raise
            except Exception as e:
                log(f"Could not check live streams ({e}). Trying again soon.")
            await asyncio.sleep(every)

    # ---------- moments ----------
    async def watch_moments(self):
        tick = 0
        while True:
            await asyncio.sleep(1)
            now = time.time()
            tick += 1
            if tick % 2 == 0:   # you clicked Allow / Never clip in the control center: apply it right away
                try:
                    mt = screening.DECISIONS_PATH.stat().st_mtime
                except OSError:
                    mt = 0.0
                if mt != self._decisions_seen and self.cfg.mode != "channels":
                    self._decisions_seen = mt          # refresh_now re-reads the decisions
                    self._spawn(self.refresh_now())
            if tick % 60 == 0 and self.check_quiet_chats():
                self._spawn(self.refresh_now())
            for c, d in list(self.detectors.items()):
                m = d.check(now)
                if m and now - self.last_moment.get(c, 0) > 30:
                    self.last_moment[c] = now
                    self.on_moment(m, now)

    def on_moment(self, m: Moment, now: float):
        if self.stop_mode:
            return   # stopping: don't start new clips
        ok, why = self.limiter.allow(m.channel, now)
        if not ok:
            log(f"Moment in {m.channel} ({m.reason}) - skipped: {why}")
            self.skipped += 1
            return
        self.limiter.record(m.channel, now)
        log(f"MOMENT in {m.channel}: {m.reason} ({m.messages} msgs from {m.chatters} people, score {m.score})")
        self.ev("moment", f"Moment in {m.channel}: {m.reason}", m.channel)
        if self.dry_run:
            log("  Test mode: not creating a clip.")
            self._spawn(asyncio.to_thread(
                self.tg.send, f"[test mode] Moment in {m.channel}: {m.reason} - twitch.tv/{m.channel}"))
            return
        self._spawn(self.clip_moment(m.channel, m, now))

    async def clip_moment(self, channel: str, m: Moment | None, stamp: float):
        """Makes one clip; the control center shows each step while it runs."""
        self.activity[channel] = "Creating the Twitch clip"
        try:
            return await self._clip_moment(channel, m, stamp)
        except Exception as e:
            log(f"  Something went wrong making the {channel} clip: {str(e)[:150]}")
            self.ev("fail", f"Couldn't finish the {channel} clip: {str(e)[:120]}", channel)
            return False
        finally:
            self.activity.pop(channel, None)

    async def _clip_moment(self, channel: str, m: Moment | None, stamp: float):
        info = self.live.get(channel)
        if not info:
            return False
        await asyncio.sleep(self.cfg.delay_after_moment if m else 0)
        try:
            clip_id = await asyncio.to_thread(self.tw.create_clip, info["user_id"], self.cfg.clip_seconds)
        except TwitchError as e:
            self.limiter.refund(stamp)
            if e.status == 403:
                self.blocked.add(channel, e.message)
                log(f"  Can't clip {channel}: {e.message}. Skipping this channel for the next {BlockedChannels.DAYS} days.")
                self.ev("blocked", f"{channel} doesn't let us clip (followers/subs only, or clips off). Skipped for {BlockedChannels.DAYS} days.", channel)
                log("  (Usually the streamer only allows followers/subs to clip, or turned clips off.)")
                if channel in self.live and self.cfg.mode in ("game", "top"):
                    self._spawn(self.refresh_now())  # swap in the next stream straight away
            elif e.status == 401:
                log(f"  Twitch login problem while clipping ({e.message}). Will try on the next moment.")
            elif e.status == 404:
                log(f"  {channel} just went offline - no clip.")
            else:
                log(f"  Twitch didn't make the clip ({e.message}). Will try on the next moment.")
                self.ev("fail", f"Twitch didn't make the {channel} clip ({e.message}).", channel)
            return False
        url = f"https://clips.twitch.tv/{clip_id}"
        log(f"  Clip created: {url} - downloading...")

        stamp_dt = datetime.now()
        folder = CLIPS_DIR / f"{stamp_dt:%Y-%m-%d}"
        base = f"{stamp_dt:%H-%M-%S}_{channel}"
        raw = folder / f"{base}.mp4"
        self.activity[channel] = "Downloading the clip"
        got = await asyncio.to_thread(download_clip, clip_id, raw, log)
        vertical = None
        with_text = False
        text = {"transcript": "", "hook": "", "post_title": "", "ai_score": ""}
        reason = m.reason if m else "manual test clip"
        if got and self.cfg.make_vertical:
            ass = None
            self.activity[channel] = "Writing captions and title"
            try:
                ass, text = await asyncio.to_thread(self.make_text, raw, channel, info, m)
            except Exception as e:
                log(f"  Text/captions skipped: {str(e)[:150]}")
            # Finished videos go to their own folder: final\<date>\<time>_<streamer>_<title>.mp4
            final_folder = FINAL_DIR / f"{stamp_dt:%Y-%m-%d}"
            final_folder.mkdir(parents=True, exist_ok=True)
            vertical = final_folder / f"{base}_{slug(text['hook'])}.mp4".replace("_.mp4", ".mp4")
            self.activity[channel] = "Making the vertical video"
            try:
                await asyncio.to_thread(make_vertical, raw, vertical, ass)
                with_text = bool(ass)
            except Exception as e:
                if ass:  # try once more without text so we still get a usable video
                    log(f"  Adding text failed ({str(e)[:100]}) - making it without text.")
                    try:
                        await asyncio.to_thread(make_vertical, raw, vertical, None)
                    except Exception as e2:
                        log(f"  Vertical version failed: {e2}")
                        vertical = None
                else:
                    log(f"  Vertical version failed: {e}")
                    vertical = None
            if vertical:
                write_info_file(vertical.with_suffix(".txt"), info, channel, url, reason, text, with_text)
                library.save(vertical, {"meta": {
                    "streamer": info.get("user_name", channel), "channel": channel,
                    "game": info.get("game_name", ""), "stream_title": info.get("title", ""),
                    "clip_url": url, "why": reason, "hook": text["hook"], "post_title": text["post_title"],
                    "ai_score": text["ai_score"], "transcript": text["transcript"], "with_text": with_text,
                    "created": f"{stamp_dt:%Y-%m-%d %H:%M:%S}"}})
        if got:
            self.clips_made += 1
            if vertical:
                log(f"  FINAL VIDEO: {vertical}")
                self.ev("clip", f"New video from {info.get('user_name', channel)}: \"{text['hook'] or vertical.stem}\"",
                        channel, library.clip_id(vertical))
            log(f"  Original saved: {raw}")
        else:
            log(f"  Couldn't download it yet - it's still on Twitch: {url}")
            self.ev("fail", f"The {channel} clip was made on Twitch but couldn't be downloaded yet: {url}", channel)

        write_log_row({
            "time": f"{stamp_dt:%Y-%m-%d %H:%M:%S}", "channel": channel, "game": info.get("game_name", ""),
            "stream_title": info.get("title", ""), "why": reason, "score": m.score if m else "",
            "hook": text["hook"], "post_title": text["post_title"], "ai_score": text["ai_score"],
            "transcript": text["transcript"],
            "clip_url": url, "file": str(raw) if got else "", "final_file": str(vertical or ""),
        })
        caption = f"{info.get('user_name', channel)} - {info.get('game_name', '')}\n{reason}\n{url}"
        if text["post_title"]:
            caption = f"{text['post_title']}\n(AI score {text['ai_score']}/10)\n\n{caption}"
        if got:
            await asyncio.to_thread(self.tg.send_video, vertical or raw, caption)
        else:
            await asyncio.to_thread(self.tg.send, caption)
        return got

    def make_text(self, raw, channel: str, info: dict, m: Moment | None):
        """Captions + top title + streamer credit. Runs in a worker thread."""
        cfg = self.cfg
        streamer = info.get("user_name", channel)
        words = []
        if cfg.captions:
            log("  Writing captions...")
            words = transcribe(raw, cfg.caption_model, cfg.language)
        transcript = " ".join(w.text for w in words)
        hook, post_title, ai_score = "", "", ""
        if cfg.title_text and self.ai.enabled:
            out = self.ai.write_text(streamer, info.get("game_name", ""), info.get("title", ""),
                                     transcript, m.chat if m else [])
            if out:
                hook, post_title, ai_score = out["hook"], out["post_title"], out["score"]
                log(f"  AI title: \"{hook}\" (score {ai_score}/10)")
        if cfg.title_text and not hook:
            hook = f"{streamer} | {info.get('game_name', '')}".strip(" |")
        credit = f"twitch.tv/{channel}" if cfg.streamer_credit else ""
        ass = build_ass(words, float(cfg.clip_seconds), hook if cfg.title_text else "", credit)
        return ass, {"transcript": transcript, "hook": hook, "post_title": post_title, "ai_score": ai_score}

    # ---------- housekeeping ----------
    async def status_loop(self):
        while True:
            await asyncio.sleep(300)
            names = ", ".join(sorted(self.live)) or "nobody live"
            log(f"Status: watching {len(self.live)} ({names}) | clips this hour {self.limiter.used}/"
                f"{self.cfg.max_clips_per_hour} | clips saved this session {self.clips_made}")

    async def validate_loop(self):
        while True:
            await asyncio.sleep(3600)  # Twitch asks apps to re-check their login every hour
            try:
                await asyncio.to_thread(self.tw.validate)
            except NotLoggedIn:
                raise
            except Exception as e:
                log(f"Login check failed ({e}) - will retry in an hour.")

    # ---------- start ----------
    async def startup(self) -> bool:
        info = await asyncio.to_thread(self.tw.validate)
        self.account = info["login"]
        log(f"Logged in to Twitch as {info['login']} (clips will show this name).")
        if self.cfg.mode == "game":
            game = await asyncio.to_thread(self.tw.find_game, self.cfg.game)
            if not game:
                log(f"Couldn't find the game \"{self.cfg.game}\" on Twitch. Run setup.bat to fix it.")
                self.error = f"Couldn't find the game \"{self.cfg.game}\" on Twitch. Pick it again in Settings."
                return False
            self.game_id = game["id"]
            lang = f", language: {self.cfg.language}" if self.cfg.language else ""
            self.mode_summary = f"Top {self.cfg.max_streams} live streams of {game['name']}"
            log(f"Mode: top {self.cfg.max_streams} live streams of \"{game['name']}\" "
                f"(min {self.cfg.min_viewers} viewers{lang})")
        elif self.cfg.mode == "top":
            lang = f", language: {self.cfg.language}" if self.cfg.language else ""
            self.mode_summary = f"The {self.cfg.max_streams} most-watched live streams on Twitch"
            log(f"Mode: the {self.cfg.max_streams} most-watched live streams on Twitch, any game "
                f"(min {self.cfg.min_viewers} viewers{lang})")
        else:
            self.mode_summary = "Your channels: " + ", ".join(self.cfg.channels)
            log(f"Mode: channels {', '.join(self.cfg.channels)}")
        log(f"Limits: {self.cfg.max_clips_per_hour} clips/hour, {self.cfg.cooldown_minutes:g} min cooldown per "
            f"channel, {self.cfg.clip_seconds}s clips, sensitivity {self.cfg.sensitivity}")
        if self.dry_run:
            log("TEST MODE: moments are shown but no clips are created.")
        CLIPS_DIR.mkdir(exist_ok=True)
        return True

    async def run(self):
        self.stop_event = asyncio.Event()
        writer = asyncio.create_task(self.status_writer())
        try:
            if not await self.startup():
                self.state = "error"
                return
            self.state = "running"
            self.ev("start", "Clipping started: " + self.mode_summary + (" (test mode, no clips made)" if self.dry_run else ""))
            await asyncio.to_thread(self.tg.send, "AutoClipper started" + (" (test mode)" if self.dry_run else ""))
            log(f"Each channel is learned for {self.cfg.warmup_seconds}s before clipping. Press Ctrl+C to stop.")
            workers = [asyncio.create_task(c) for c in (self.chat.run(), self.watch_targets(), self.watch_moments(),
                                                         self.status_loop(), self.validate_loop())]
            stopper = asyncio.create_task(self.stop_event.wait())
            done, _ = await asyncio.wait([*workers, stopper], return_when=asyncio.FIRST_COMPLETED)
            for t in [*workers, *self._tasks]:
                t.cancel()
            for t in done:   # surface a real error (e.g. Twitch login expired)
                if t is not stopper and not t.cancelled() and t.exception():
                    raise t.exception()
            log("Stopped.")
        finally:
            if self.state != "error":
                self.state = "stopped"
            self.activity.clear()
            writer.cancel()
            runtime.clear_stop_request()
            self.write_status()

    async def clip_now(self, channel: str):
        """Makes one clip right away - for checking that everything works."""
        if not await self.startup():
            return
        channel = channel.lower().lstrip("@")
        streams = await asyncio.to_thread(self.tw.get_streams, [channel])
        if not streams:
            log(f"{channel} is not live right now. Pick a channel that is live and try again.")
            return
        self.live = {channel: streams[0]}
        log(f"Making a test clip of {channel}...")
        if await self.clip_moment(channel, None, time.time()) is False:
            log("No clip made. Try test-clip.bat with another live channel.")
        else:
            log("Done. Check the clips folder" + (" and Telegram." if self.tg.enabled else "."))


def main(dry_run: bool = False, clip_now: str | None = None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = config_mod.load()
    if not cfg:
        print("AutoClipper isn't set up yet. Double-click setup.bat first.")
        return 1
    if not clip_now:
        other = runtime.read_status()
        if other.get("alive") and other.get("pid") != os.getpid():
            print("AutoClipper is already clipping (started from the control center or another window). "
                  "Open AutoClipper.bat to see it.")
            return 1
        runtime.clear_stop_request()
    app = AutoClipper(cfg, dry_run=dry_run)
    if not app.tw.has_login():
        print("You're not logged in to Twitch yet. Open AutoClipper.bat and connect Twitch in Settings.")
        app.state, app.error = "error", "Not logged in to Twitch. Connect Twitch in Settings."
        app.write_status()
        return 1
    keep_awake(True)
    try:
        asyncio.run(app.clip_now(clip_now) if clip_now else app.run())
    except NotLoggedIn as e:
        print(f"\n{e}\nOpen AutoClipper.bat and connect Twitch again in Settings.")
        app.state, app.error = "error", f"{e} Connect Twitch again in Settings."
        app.write_status()
        return 1
    except Exception as e:
        import traceback
        traceback.print_exc()
        app.state, app.error = "error", f"The clipper crashed: {str(e)[:200]}"
        app.write_status()
        return 2  # start.bat restarts automatically on code 2
    except KeyboardInterrupt:
        print("\nStopped. Your clips are in the 'clips' folder.")
        try:
            app.tg.send("AutoClipper stopped")
        except Exception:
            pass
    finally:
        keep_awake(False)
    return 0
