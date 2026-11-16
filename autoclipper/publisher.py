"""Publisher: pick finished clips, let Gemini write the post details, publish to YouTube / TikTok.

Double-click publisher.bat - it opens http://127.0.0.1:8765 in your browser.
Only your own computer can open this page (it listens on 127.0.0.1 only).
"""
from __future__ import annotations

import json
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import config as config_mod
from . import library
from . import runtime
from . import screening
from .app import BlockedChannels
from .config import BLOCKED_PATH, ROOT
from .twitch import Twitch
from .ai import Gemini
from .media import video_duration
from .social import PORT, TIKTOK_REDIRECT, YOUTUBE_REDIRECT, SocialError, TikTok, YouTube

PAGE = Path(__file__).with_name("publisher.html")
HOME = f"http://127.0.0.1:{PORT}/"
class SettingsError(Exception):
    def __init__(self, fields: dict):
        super().__init__("Some settings need fixing.")
        self.fields = fields


ALLOWED_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}


class Publisher:
    def __init__(self):
        self.cfg = config_mod.load() or config_mod.Config()
        self.yt = YouTube()
        self.tt = TikTok(lambda: (self.cfg.tiktok_client_key, self.cfg.tiktok_client_secret))
        self.jobs: dict[str, dict] = {}
        self._starting_until = 0.0
        self._start_requested_at = 0.0
        self._restart_pending = False
        self._twitch_pending: dict | None = None
        self._twitch_error = ""
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    # ---------------------------------------------------------- data
    def gemini(self) -> Gemini:
        return Gemini(self.cfg.gemini_api_key, self.cfg.gemini_model, log=lambda m: None)

    def clip_info(self, v: Path) -> dict:
        d = library.load(v)
        cid = library.clip_id(v)
        return {"id": cid, "url": "/video/" + quote(cid), "name": v.stem, "date": v.parent.name,
                "time": v.stem[:8].replace("-", ":"), "size_mb": round(v.stat().st_size / 1e6, 1),
                "meta": d["meta"], "post": d["post"], "youtube": d["youtube"], "tiktok": d["tiktok"],
                "jobs": {p: self.jobs.get(f"{cid}|{p}") for p in ("youtube", "tiktok")}}

    def status(self) -> dict:
        return {"youtube": self.yt.status(), "tiktok": self.tt.status(),
                "gemini": bool(self.cfg.gemini_api_key), "privacy": self.cfg.youtube_privacy,
                "tiktok_username": self.cfg.tiktok_username,
                "youtube_redirect": YOUTUBE_REDIRECT, "tiktok_redirect": TIKTOK_REDIRECT,
                "busy": any(j and j.get("state") in ("queued", "working") for j in self.jobs.values()),
                "jobs": self.jobs}

    def _need(self, cid: str) -> Path:
        v = library.video_path(cid)
        if not v:
            raise SocialError("That video isn't in the final folder any more.")
        return v

    def generate(self, cid: str) -> dict:
        v = self._need(cid)
        m = library.load(v)["meta"]
        channel = m.get("channel") or (v.stem.split("_")[1] if v.stem.count("_") >= 1 else "")
        post, err = self.gemini().post_details(m.get("streamer") or channel, channel, m.get("game", ""),
                                               m.get("hook", ""), m.get("transcript", ""), m.get("stream_title", ""))
        if not post:
            raise SocialError(err)
        library.update(v, "post", post)
        return post

    def save_post(self, cid: str, post: dict) -> None:
        keep = {"youtube_title", "youtube_description", "youtube_tags", "tiktok_caption"}
        clean = {k: v for k, v in post.items() if k in keep}
        if isinstance(clean.get("youtube_tags"), str):
            clean["youtube_tags"] = [t.strip().lstrip("#") for t in clean["youtube_tags"].split(",") if t.strip()]
        library.update(self._need(cid), "post", clean)

    def tiktok_creator(self) -> dict:
        info = self.tt.creator_info()
        if info.get("username") and not self.cfg.tiktok_username:
            self.cfg.tiktok_username = info["username"]   # lets us build links to posted TikToks
            config_mod.save(self.cfg)
        return info

    def tiktok_direct(self, cid: str, opts: dict) -> None:
        """One clip, posted with the choices the user made on the Post to TikTok screen."""
        v = self._need(cid)
        if not opts.get("consent"):
            raise SocialError("Click Post on the Post to TikTok screen to confirm.")
        if not opts.get("privacy"):
            raise SocialError("Choose who can view this video.")
        if opts.get("disclose") and not (opts.get("brand_organic") or opts.get("brand_content")):
            raise SocialError("You need to indicate if your content promotes yourself, a third party, or both.")
        caption = str(opts.get("caption", ""))[:2200]
        library.update(v, "post", {"tiktok_caption": caption})
        key = f"{cid}|tiktok"
        if (self.jobs.get(key) or {}).get("state") in ("queued", "working"):
            raise SocialError("This clip is already being sent to TikTok.")
        self.jobs[key] = {"state": "queued", "msg": "Waiting in line..."}
        self.q.put((cid, "tiktok", {
            "caption": caption, "privacy": opts["privacy"],
            "allow_comment": bool(opts.get("allow_comment")), "allow_duet": bool(opts.get("allow_duet")),
            "allow_stitch": bool(opts.get("allow_stitch")),
            "brand_organic": bool(opts.get("disclose") and opts.get("brand_organic")),
            "brand_content": bool(opts.get("disclose") and opts.get("brand_content"))}))

    def publish(self, cids: list[str], platforms: list[str], privacy: str,
                tiktok_mode: str = "inbox", tiktok_privacy: str = "") -> int:
        n = 0
        for cid in cids:
            for p in platforms:
                if p not in ("youtube", "tiktok"):
                    continue
                key = f"{cid}|{p}"
                if (self.jobs.get(key) or {}).get("state") in ("queued", "working"):
                    continue
                self.jobs[key] = {"state": "queued", "msg": "Waiting in line..."}
                self.q.put((cid, p, privacy if p == "youtube" else "inbox"))   # direct posts go via tiktok_direct()
                n += 1
        return n

    def _worker(self):
        while True:
            cid, p, privacy = self.q.get()
            key = f"{cid}|{p}"
            try:
                v = self._need(cid)
                d = library.load(v)
                post, m = d["post"], d["meta"]
                if not post.get("youtube_title") and self.cfg.gemini_api_key:
                    self.jobs[key] = {"state": "working", "msg": "Writing details with Gemini..."}
                    try:
                        post = self.generate(cid)
                    except SocialError:
                        pass  # fall back to the clip's own title
                self.jobs[key] = {"state": "working", "msg": f"Uploading to {'YouTube' if p == 'youtube' else 'TikTok'}..."}
                if p == "youtube":
                    title = post.get("youtube_title") or m.get("post_title") or m.get("hook") or v.stem
                    desc = post.get("youtube_description") or (
                        f"{m.get('hook', '')}\n\nCredit: twitch.tv/{m.get('channel', '')}\n#shorts")
                    res = self.yt.upload(v, title, desc, post.get("youtube_tags", []), privacy)
                    res["privacy"] = privacy
                    msg = f"Posted on YouTube ({privacy})"
                elif isinstance(privacy, dict):   # direct post from the Post to TikTok screen
                    o = privacy
                    tpriv = o["privacy"]
                    res = self.tt.direct_post(v, o["caption"], tpriv, o.get("allow_comment", False),
                                              o.get("allow_duet", False), o.get("allow_stitch", False),
                                              o.get("brand_organic", False), o.get("brand_content", False),
                                              video_duration(v))
                    if res.get("username") and not self.cfg.tiktok_username:
                        self.cfg.tiktok_username = res["username"]
                        config_mod.save(self.cfg)
                    res["status"] = "PROCESSING_UPLOAD"
                    msg = (f"Published to TikTok ({TikTok.PRIVACY_NAMES.get(tpriv, tpriv)}). It may take a few "
                           "minutes for your video to process and appear on your profile.")
                else:
                    res = self.tt.upload_to_inbox(v)
                    res["mode"] = "inbox"
                    msg = "Sent to your TikTok inbox - open TikTok and tap the notification to post it"
                res["when"] = time.strftime("%Y-%m-%d %H:%M")
                library.update(v, p, res)
                self.jobs[key] = {"state": "done", "msg": msg}
            except Exception as e:
                self.jobs[key] = {"state": "error", "msg": str(e)[:300]}

    # ---------------------------------------------------------- posted links
    def tiktok_url(self, post_id: str) -> str:
        user = self.cfg.tiktok_username.strip().lstrip("@")
        return f"https://www.tiktok.com/@{user}/video/{post_id}" if (user and post_id) else ""

    def refresh_tiktok(self) -> int:
        """Asks TikTok what happened to inbox uploads that are still waiting. Returns how many are still waiting."""
        if not self.tt.status()["connected"]:
            return 0
        waiting = 0
        for v in library.all_videos():
            t = library.load(v)["tiktok"]
            if not t.get("publish_id") or t.get("status") in ("PUBLISH_COMPLETE", "FAILED"):
                if t.get("post_id") and not t.get("url") and self.tiktok_url(t["post_id"]):
                    library.update(v, "tiktok", {"url": self.tiktok_url(t["post_id"])})
                continue
            try:
                info = self.tt.check(t["publish_id"])
            except Exception:
                continue
            upd = {"status": info["status"], "checked": time.strftime("%Y-%m-%d %H:%M")}
            if info["post_id"]:
                upd["post_id"] = info["post_id"]
                upd["url"] = t.get("url") or self.tiktok_url(info["post_id"])
            if info["fail_reason"]:
                upd["fail_reason"] = info["fail_reason"]
            library.update(v, "tiktok", upd)
            if info["status"] not in ("PUBLISH_COMPLETE", "FAILED"):
                waiting += 1
        return waiting

    def set_link(self, cid: str, platform: str, url: str):
        url = url.strip()
        if url and not re.match(r"^https://(www\.|vm\.|m\.)?(tiktok|youtube)\.com/", url) and not url.startswith("https://youtu.be/"):
            raise SocialError("Paste the full link, starting with https://www.tiktok.com/ or https://youtube.com/")
        upd = {"url": url}
        if platform == "tiktok" and url:
            upd["status"] = "PUBLISH_COMPLETE"
        library.update(self._need(cid), platform, upd)

    def set_username(self, name: str):
        self.cfg.tiktok_username = name.strip().lstrip("@")
        config_mod.save(self.cfg)
        self.refresh_tiktok()

    # ---------------------------------------------------------- clipper control
    def clipper_status(self) -> dict:
        st = runtime.read_status()
        # "Starting" only until the new clipper reports in (or it has already reported and stopped)
        if st.get("alive") or st.get("heartbeat", 0) > self._start_requested_at:
            self._starting_until = 0
        if self._starting_until > time.time() and not st.get("alive"):
            st = {"state": "starting", "alive": True, "events": st.get("events", []), "channels": []}
        st["restart_pending"] = self._restart_pending
        return st

    def start_clipper(self, test_mode: bool = False):
        if runtime.read_status().get("alive"):
            raise SocialError("The clipper is already running.")
        if not Twitch(self.cfg.twitch_id).has_login():
            raise SocialError("Connect Twitch first (Settings → Accounts).")
        runtime.clear_stop_request()
        flags = 0
        if sys.platform == "win32":   # runs on its own: keeps going if this window closes
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        args = [sys.executable, "-m", "autoclipper"] + (["--test-mode"] if test_mode else [])
        subprocess.Popen(args, cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        self._start_requested_at = time.time()
        self._starting_until = time.time() + 45

    def stop_clipper(self, mode: str = "graceful"):
        if not runtime.read_status().get("alive"):
            raise SocialError("The clipper isn't running.")
        runtime.request_stop("now" if mode == "now" else "graceful")

    def restart_clipper(self):
        """Stop (waiting for any clip in progress), then start again with the new settings."""
        if self._restart_pending:
            return
        self._restart_pending = True
        test = bool(runtime.read_status().get("test_mode"))

        def go():
            try:
                if runtime.read_status().get("alive"):
                    runtime.request_stop("graceful")
                    deadline = time.time() + 900
                    while runtime.read_status().get("alive") and time.time() < deadline:
                        time.sleep(1)
                self.start_clipper(test)
            except Exception:
                pass
            finally:
                self._restart_pending = False
        threading.Thread(target=go, daemon=True).start()

    # ---------------------------------------------------------- settings
    SETTINGS = {   # field: (type, min, max)
        "mode": ("choice", ("top", "game", "channels")), "game": ("str",), "channels": ("list",),
        "language": ("str",), "max_streams": ("int", 1, 20), "min_viewers": ("int", 0, 1_000_000),
        "exclude_channels": ("list",), "clip_seconds": ("int", 5, 60), "max_clips_per_hour": ("int", 1, 60),
        "cooldown_minutes": ("int", 0, 120), "sensitivity": ("choice", ("low", "medium", "high")),
        "make_vertical": ("bool",), "captions": ("bool",), "title_text": ("bool",),
        "streamer_credit": ("bool",), "youtube_privacy": ("choice", ("public", "unlisted", "private")),
        "screen_streams": ("bool",),
    }

    def get_settings(self) -> dict:
        self.cfg = config_mod.load() or self.cfg
        out = {k: getattr(self.cfg, k) for k in self.SETTINGS}
        out["gemini_key_set"] = bool(self.cfg.gemini_api_key)
        out["blocked"] = [{"channel": c, **v} for c, v in sorted(BlockedChannels().entries.items())]
        out["decisions"] = [{"channel": c, **v} for c, v in sorted(screening.load_decisions().items())]
        return out

    def save_settings(self, data: dict) -> dict:
        cfg = config_mod.load() or self.cfg
        errors = {}
        for k, spec in self.SETTINGS.items():
            if k not in data:
                continue
            v = data[k]
            kind = spec[0]
            try:
                if kind == "int":
                    v = int(float(v))
                    if not spec[1] <= v <= spec[2]:
                        raise ValueError(f"Use a number from {spec[1]} to {spec[2]}.")
                elif kind == "bool":
                    v = bool(v)
                elif kind == "choice":
                    if v not in spec[1]:
                        raise ValueError("Pick one of the options.")
                elif kind == "list":
                    if isinstance(v, str):
                        v = re.split(r"[,\s]+", v)
                    v = [x.strip().lower().lstrip("@").split("/")[-1] for x in v if str(x).strip()]
                else:
                    v = str(v).strip()
                    if k == "language":
                        v = v.lower()[:2]
            except (TypeError, ValueError) as e:
                msg = str(e)
                errors[k] = msg if msg.startswith(("Use ", "Pick ")) else "Use a number."
                continue
            setattr(cfg, k, v)
        if cfg.mode == "game" and not cfg.game:
            errors["game"] = "Type a game name."
        if cfg.mode == "channels" and not cfg.channels:
            errors["channels"] = "Add at least one channel."
        if "gemini_api_key" in data and data["gemini_api_key"] is not None:
            cfg.gemini_api_key = str(data["gemini_api_key"]).strip()
        if errors:
            raise SettingsError(errors)
        config_mod.save(cfg)
        self.cfg = cfg
        running = runtime.read_status().get("alive")
        return {"ok": True, "applies_on_restart": bool(running)}

    def find_game(self, name: str) -> dict:
        tw = Twitch(self.cfg.twitch_id)
        if not tw.has_login():
            raise SocialError("Connect Twitch first to look up games.")
        g = tw.find_game(name.strip()) if name.strip() else None
        if not g:
            raise SocialError(f"No game called \"{name}\" on Twitch. Check the spelling.")
        return {"name": g["name"], "id": g["id"]}

    def unblock(self, channel: str):
        b = BlockedChannels()
        b.entries.pop(channel.lower(), None)
        BLOCKED_PATH.write_text(json.dumps(b.entries, indent=2), encoding="utf-8")

    # ---------------------------------------------------------- Twitch login (device code)
    def twitch_status(self) -> dict:
        tw = Twitch(self.cfg.twitch_id)
        return {"connected": tw.has_login(), "login": tw.user_login or "",
                "pending": self._twitch_pending, "error": self._twitch_error}

    def twitch_login(self):
        if self._twitch_pending and self._twitch_pending.get("active"):
            return self._twitch_pending
        self._twitch_error = ""
        self._twitch_pending = {"active": True, "code": "", "uri": ""}
        tw = Twitch(self.cfg.twitch_id)

        def show(code, uri):
            self._twitch_pending.update(code=code, uri=uri)

        def go():
            try:
                tw.device_login(show)
            except Exception as e:
                self._twitch_error = getattr(e, "message", str(e))
            finally:
                self._twitch_pending = None
        threading.Thread(target=go, daemon=True).start()
        for _ in range(50):   # wait briefly for Twitch to give us the code
            if self._twitch_pending is None or self._twitch_pending.get("code"):
                break
            time.sleep(0.1)
        return self._twitch_pending or {}

    def twitch_logout(self):
        if runtime.read_status().get("alive"):
            raise SocialError("Stop the clipper before disconnecting Twitch.")
        from .config import TOKENS_PATH
        TOKENS_PATH.unlink(missing_ok=True)

    # ---------------------------------------------------------- settings
    def set_tiktok_keys(self, key: str, secret: str):
        key, secret = key.strip(), secret.strip()
        if not key or not secret:
            raise SocialError("Paste both the Client key and the Client secret.")
        self.cfg.tiktok_client_key, self.cfg.tiktok_client_secret = key, secret
        config_mod.save(self.cfg)


APP: Publisher | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "AutoClipper"

    def log_message(self, *a):  # keep the console quiet
        pass

    # ---------------------------------------------------------- helpers
    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def _html(self, text, code=200):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _message_page(self, title, text):
        self._html(f"""<!doctype html><meta charset=utf-8><title>{title}</title>
<body style="font-family:system-ui;background:#0f1115;color:#e8e8ea;display:grid;place-items:center;height:100vh;margin:0">
<div style="max-width:520px;padding:24px"><h2>{title}</h2><p style="color:#aab">{text}</p>
<p><a style="color:#8ab4ff" href="{HOME}">Back to the publisher</a></p></div>""")

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if 0 < n < 20_000_000 else b""

    # ---------------------------------------------------------- GET
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/" and "state" in q and q["state"].startswith("yt_"):
                if "error" in q:
                    return self._message_page("YouTube not connected", f"Google said: {q['error']}")
                name = APP.yt.finish(q.get("code", ""), q["state"])
                return self._redirect(HOME + "?connected=youtube&name=" + quote(name))
            if u.path == "/":
                return self._html(PAGE.read_text(encoding="utf-8"))
            if u.path.rstrip("/") == "/oauth/tiktok":
                if "error" in q:
                    return self._message_page("TikTok not connected",
                                              f"TikTok said: {q.get('error_description') or q['error']}")
                name = APP.tt.finish(q.get("code", ""), q.get("state", ""))
                return self._redirect(HOME + "?connected=tiktok&name=" + quote(name))
            if u.path == "/connect/youtube":
                return self._redirect(APP.yt.auth_url())
            if u.path == "/connect/tiktok":
                return self._redirect(APP.tt.auth_url())
            if u.path == "/api/clips":
                return self._json([APP.clip_info(v) for v in library.all_videos()])
            if u.path == "/api/status":
                return self._json(APP.status())
            if u.path == "/api/clipper/status":
                return self._json(APP.clipper_status())
            if u.path == "/api/clipper/log":
                return self._json({"lines": runtime.tail_log(int(q.get("lines", 200)))})
            if u.path == "/api/settings":
                return self._json(APP.get_settings())
            if u.path == "/api/twitch/status":
                return self._json(APP.twitch_status())
            if u.path == "/api/twitch/game":
                return self._json(APP.find_game(q.get("name", "")))
            if u.path == "/api/tiktok-creator":
                return self._json(APP.tiktok_creator())
            if u.path.startswith("/video/"):
                return self._video(unquote(u.path[len("/video/"):]))
            self.send_error(404)
        except SocialError as e:
            if u.path.startswith("/api/"):
                return self._json({"error": str(e)}, 400)
            self._message_page("Something needs fixing", str(e))
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    def _video(self, cid):
        p = library.video_path(cid)
        if not p:
            return self.send_error(404)
        size = p.stat().st_size
        start, end = 0, size - 1
        rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if rng and (rng[1] or rng[2]):
            if rng[1]:
                start = int(rng[1])
                end = min(int(rng[2]), size - 1) if rng[2] else size - 1
            else:
                start = max(0, size - int(rng[2]))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(p, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(262144, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    # ---------------------------------------------------------- POST
    def do_POST(self):
        origin = self.headers.get("Origin")
        if origin and origin not in ALLOWED_ORIGINS:   # other websites can't use this page
            return self._json({"error": "not allowed"}, 403)
        u = urlparse(self.path)
        try:
            if u.path == "/api/youtube-client":
                cid = YouTube.save_client_file(self._body())
                return self._json({"ok": True, "client_id": cid[:20] + "..."})
            data = json.loads(self._body() or b"{}")
            if u.path == "/api/generate":
                return self._json({"post": APP.generate(data["id"])})
            if u.path == "/api/save":
                APP.save_post(data["id"], data.get("post", {}))
                return self._json({"ok": True})
            if u.path == "/api/publish":
                n = APP.publish(data.get("ids", []), data.get("platforms", []), data.get("privacy", "public"),
                                data.get("tiktok_mode", "inbox"), data.get("tiktok_privacy", ""))
                return self._json({"queued": n})
            if u.path == "/api/tiktok-keys":
                APP.set_tiktok_keys(data.get("key", ""), data.get("secret", ""))
                return self._json({"ok": True})
            if u.path == "/api/disconnect":
                (APP.yt if data.get("platform") == "youtube" else APP.tt).disconnect()
                return self._json({"ok": True})
            if u.path == "/api/clipper/start":
                APP.start_clipper(bool(data.get("test_mode")))
                return self._json({"ok": True})
            if u.path == "/api/clipper/stop":
                APP.stop_clipper(data.get("mode", "graceful"))
                return self._json({"ok": True})
            if u.path == "/api/clipper/restart":
                APP.restart_clipper()
                return self._json({"ok": True})
            if u.path == "/api/settings":
                return self._json(APP.save_settings(data))
            if u.path == "/api/channel-decision":
                dec = data.get("decision")
                if dec not in ("allow", "skip", "forget"):
                    raise SocialError("Choose allow, skip or forget.")
                screening.set_decision(data.get("channel", ""), None if dec == "forget" else dec, data.get("note", ""))
                return self._json({"ok": True})
            if u.path == "/api/unblock":
                APP.unblock(data.get("channel", ""))
                return self._json({"ok": True})
            if u.path == "/api/twitch/login":
                return self._json(APP.twitch_login())
            if u.path == "/api/twitch/logout":
                APP.twitch_logout()
                return self._json({"ok": True})
            if u.path == "/api/tiktok-direct":
                APP.tiktok_direct(data["id"], data)
                return self._json({"ok": True})
            if u.path == "/api/refresh-tiktok":
                return self._json({"waiting": APP.refresh_tiktok()})
            if u.path == "/api/set-link":
                APP.set_link(data["id"], data.get("platform", "tiktok"), data.get("url", ""))
                return self._json({"ok": True})
            if u.path == "/api/tiktok-username":
                APP.set_username(data.get("name", ""))
                return self._json({"ok": True})
            if u.path == "/api/clear-job":
                APP.jobs.pop(f"{data.get('id')}|{data.get('platform')}", None)
                return self._json({"ok": True})
            self._json({"error": "unknown"}, 404)
        except SettingsError as e:
            self._json({"error": str(e), "fields": e.fields}, 400)
        except SocialError as e:
            self._json({"error": str(e)}, 400)
        except (KeyError, ValueError) as e:
            self._json({"error": f"bad request ({e})"}, 400)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def main():
    global APP
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    APP = Publisher()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"Port {PORT} is busy - the publisher is probably already open. Opening it in your browser.")
        webbrowser.open(HOME)
        return 1
    server.daemon_threads = True
    print(f"AutoClipper is running at {HOME}")
    print("Keep this window open while you publish. Clipping keeps going even if you close it.")
    threading.Timer(0.8, lambda: webbrowser.open(HOME)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nPublisher closed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
