"""Connecting to YouTube and TikTok and uploading videos.

YouTube: uses your own free Google Cloud "Desktop app" OAuth client (youtube_client.json).
         Uploads are real posts (public / unlisted / private - your choice). ~100 uploads/day.
TikTok:  uses your own TikTok developer app (client key + secret). Videos are sent to your
         TikTok INBOX as drafts; you open TikTok, tap the notification and post. (TikTok only
         allows direct public posting for apps that passed TikTok's audit.)
Logins are saved in social_tokens.json - keep that file private.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

from .config import ROOT
from .defaults import LOGIN_HELPER_URL, TIKTOK_CLIENT_KEY

TOKENS_FILE = ROOT / "social_tokens.json"
YOUTUBE_CLIENT_FILE = ROOT / "youtube_client.json"
PORT = 8765
YOUTUBE_REDIRECT = f"http://127.0.0.1:{PORT}/"
TIKTOK_REDIRECT = f"http://localhost:{PORT}/oauth/tiktok/"

_lock = threading.Lock()


class SocialError(Exception):
    pass


def _load_tokens() -> dict:
    try:
        return json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_tokens(platform: str, data: dict | None):
    with _lock:
        all_ = _load_tokens()
        if data is None:
            all_.pop(platform, None)
        else:
            all_[platform] = data
        TOKENS_FILE.write_text(json.dumps(all_, indent=2), encoding="utf-8")


def _err(r: requests.Response) -> str:
    try:
        d = r.json()
        e = d.get("error")
        if isinstance(e, dict):
            return str(e.get("message") or e.get("code") or e)
        return str(d.get("error_description") or e or d)[:300]
    except ValueError:
        return r.text[:300]


# =============================================================== YouTube
class YouTube:
    AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN = "https://oauth2.googleapis.com/token"
    SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"

    def __init__(self):
        self._pending: dict[str, str] = {}   # state -> PKCE verifier

    # ---- setup state
    def client(self) -> tuple[str, str] | None:
        try:
            data = json.loads(YOUTUBE_CLIENT_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        c = data.get("installed") or data.get("web") or {}
        if c.get("client_id") and c.get("client_secret"):
            return c["client_id"], c["client_secret"]
        return None

    @staticmethod
    def save_client_file(raw: bytes) -> str:
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise SocialError("That isn't the JSON file from Google Cloud.")
        c = data.get("installed") or data.get("web")
        if not c or not c.get("client_id"):
            raise SocialError("That JSON has no OAuth client in it. Download it from Google Cloud > Clients.")
        if "installed" not in data:
            raise SocialError("This is a 'Web application' client. Create a 'Desktop app' client instead.")
        YOUTUBE_CLIENT_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return c["client_id"]

    def status(self) -> dict:
        t = _load_tokens().get("youtube", {})
        return {"configured": self.client() is not None, "connected": bool(t.get("refresh_token")),
                "name": t.get("channel_name", "")}

    # ---- login
    def auth_url(self) -> str:
        cid = self.client()
        if not cid:
            raise SocialError("YouTube isn't set up yet - add the Google client file first.")
        verifier = secrets.token_urlsafe(64)
        state = "yt_" + secrets.token_urlsafe(16)
        self._pending[state] = verifier
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        return self.AUTH + "?" + urlencode({
            "client_id": cid[0], "redirect_uri": YOUTUBE_REDIRECT, "response_type": "code",
            "scope": self.SCOPES, "access_type": "offline", "prompt": "consent", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})

    def owns_state(self, state: str) -> bool:
        return state in self._pending

    def finish(self, code: str, state: str) -> str:
        verifier = self._pending.pop(state, None)
        cid = self.client()
        if not verifier or not cid:
            raise SocialError("Login expired - click Connect again.")
        r = requests.post(self.TOKEN, timeout=30, data={
            "client_id": cid[0], "client_secret": cid[1], "code": code, "code_verifier": verifier,
            "grant_type": "authorization_code", "redirect_uri": YOUTUBE_REDIRECT})
        if r.status_code != 200:
            raise SocialError(f"Google login failed: {_err(r)}")
        d = r.json()
        tok = {"access_token": d["access_token"], "refresh_token": d.get("refresh_token", ""),
               "expires_at": time.time() + d.get("expires_in", 3600) - 60}
        _save_tokens("youtube", tok)
        name = ""
        try:
            ch = requests.get("https://www.googleapis.com/youtube/v3/channels", timeout=20,
                              params={"part": "snippet", "mine": "true"},
                              headers={"Authorization": f"Bearer {tok['access_token']}"}).json()
            items = ch.get("items") or []
            name = items[0]["snippet"]["title"] if items else "(no YouTube channel on this account!)"
        except Exception:
            pass
        tok["channel_name"] = name
        _save_tokens("youtube", tok)
        return name

    def disconnect(self):
        _save_tokens("youtube", None)

    def _token(self) -> str:
        t = _load_tokens().get("youtube", {})
        if not t.get("refresh_token"):
            raise SocialError("YouTube isn't connected.")
        if time.time() < t.get("expires_at", 0):
            return t["access_token"]
        cid = self.client()
        if not cid:
            raise SocialError("youtube_client.json is missing.")
        r = requests.post(self.TOKEN, timeout=30, data={
            "client_id": cid[0], "client_secret": cid[1], "refresh_token": t["refresh_token"],
            "grant_type": "refresh_token"})
        if r.status_code != 200:
            raise SocialError(f"YouTube login expired - click Connect again ({_err(r)}).")
        d = r.json()
        t.update(access_token=d["access_token"], expires_at=time.time() + d.get("expires_in", 3600) - 60)
        _save_tokens("youtube", t)
        return t["access_token"]

    # ---- upload
    def upload(self, video: Path, title: str, description: str, tags: list[str], privacy: str) -> dict:
        size = video.stat().st_size
        meta = {
            "snippet": {"title": title[:100] or video.stem, "description": description[:5000],
                        "tags": tags[:30], "categoryId": "20"},  # 20 = Gaming
            "status": {"privacyStatus": privacy if privacy in ("public", "unlisted", "private") else "private",
                       "selfDeclaredMadeForKids": False},
        }
        r = requests.post("https://www.googleapis.com/upload/youtube/v3/videos", timeout=60,
                          params={"uploadType": "resumable", "part": "snippet,status"},
                          headers={"Authorization": f"Bearer {self._token()}",
                                   "X-Upload-Content-Type": "video/mp4",
                                   "X-Upload-Content-Length": str(size)},
                          json=meta)
        if r.status_code != 200 or "Location" not in r.headers:
            raise SocialError(f"YouTube refused the upload: {_err(r)}")
        url = r.headers["Location"]
        for attempt in range(3):
            try:
                with open(video, "rb") as f:
                    up = requests.put(url, data=f, timeout=600,
                                      headers={"Content-Type": "video/mp4", "Content-Length": str(size)})
            except requests.RequestException as e:
                if attempt == 2:
                    raise SocialError(f"Upload to YouTube failed: {e}")
                time.sleep(5)
                continue
            if up.status_code in (200, 201):
                vid = up.json()["id"]
                return {"video_id": vid, "url": f"https://youtube.com/shorts/{vid}"}
            if up.status_code < 500 or attempt == 2:
                raise SocialError(f"YouTube upload failed: {_err(up)}")
            time.sleep(5)
        raise SocialError("YouTube upload failed.")


# =============================================================== TikTok
class TikTok:
    AUTH = "https://www.tiktok.com/v2/auth/authorize/"
    TOKEN = "https://open.tiktokapis.com/v2/oauth/token/"
    SCOPES = "user.info.basic,video.upload,video.publish"
    PRIVACY_NAMES = {"PUBLIC_TO_EVERYONE": "Everyone", "MUTUAL_FOLLOW_FRIENDS": "Friends",
                     "FOLLOWER_OF_CREATOR": "Followers", "SELF_ONLY": "Only me"}
    MAX_SINGLE = 64 * 1024 * 1024
    CHUNK = 10 * 1024 * 1024

    def __init__(self, get_keys):
        self._get_keys = get_keys            # () -> (client_key, client_secret)
        self._pending: dict[str, str] = {}

    def keys(self) -> tuple[str, str] | None:
        """(client_key, client_secret). Your own app from config.toml if both are filled in;
        otherwise the built-in AutoClipper app, whose secret stays on the login helper ('' here)."""
        k = self._get_keys()
        if k and k[0] and k[1]:
            return k[0], k[1]
        return (TIKTOK_CLIENT_KEY, "") if TIKTOK_CLIENT_KEY else None

    @property
    def own_app(self) -> bool:
        k = self._get_keys()
        return bool(k and k[0] and k[1])

    def status(self) -> dict:
        t = _load_tokens().get("tiktok", {})
        return {"configured": self.keys() is not None, "connected": bool(t.get("refresh_token")),
                "name": t.get("display_name", ""), "can_direct": "video.publish" in t.get("scopes", "")}

    def _post(self, path: str, body: dict | None = None) -> dict:
        r = requests.post(f"https://open.tiktokapis.com/v2/post/publish/{path}", timeout=60, json=body or {},
                          headers={"Authorization": f"Bearer {self._token()}",
                                   "Content-Type": "application/json; charset=UTF-8"})
        d = r.json() if r.content else {}
        err = d.get("error") or {}
        if r.status_code != 200 or err.get("code") not in (None, "", "ok"):
            code = err.get("code", str(r.status_code))
            raise SocialError(self._explain(code, err.get("message") or _err(r)))
        return d.get("data") or {}

    @staticmethod
    def _explain(code: str, msg: str) -> str:
        return {
            "scope_not_authorized": "TikTok login doesn't include this permission yet - click Reconnect next to TikTok.",
            "unaudited_client_can_only_post_to_private_accounts":
                "TikTok only lets apps that haven't passed TikTok's audit post to PRIVATE accounts. Either set your "
                "TikTok account to private (TikTok > Settings > Privacy), or use 'Send to inbox' instead.",
            "privacy_level_option_mismatch": "That 'who can view' choice isn't allowed for this account - pick another.",
            "spam_risk_too_many_posts": "TikTok's daily posting limit for this account is reached. Try again tomorrow.",
            "spam_risk_too_many_pending_share": "TikTok allows only 5 videos waiting in your inbox per day. Post the ones there first.",
            "reached_active_user_cap": "Your TikTok app reached its daily posting limit. Try again tomorrow.",
            "spam_risk_user_banned_from_posting": "TikTok has blocked this account from posting right now.",
        }.get(code, f"TikTok said: {msg} ({code})")

    def creator_info(self) -> dict:
        """Who can view options, max video length etc. for the connected account (needed for direct posting)."""
        try:
            d = self._post("creator_info/query/")
        except SocialError as e:
            if "limit" in str(e).lower() or "spam" in str(e).lower() or "blocked" in str(e).lower():
                raise SocialError(f"This TikTok account can't post right now. Please try again later. ({e})")
            raise
        opts = d.get("privacy_level_options") or []
        return {"nickname": d.get("creator_nickname", ""), "username": d.get("creator_username", ""),
                "avatar": d.get("creator_avatar_url", ""),
                "privacy_options": [{"value": o, "label": self.PRIVACY_NAMES.get(o, o)} for o in opts],
                "max_seconds": d.get("max_video_post_duration_sec", 0),
                "comment_disabled": d.get("comment_disabled", False),
                "duet_disabled": d.get("duet_disabled", False), "stitch_disabled": d.get("stitch_disabled", False)}

    def _send_file(self, video: Path, upload_url: str, chunk: int, count: int, size: int):
        with open(video, "rb") as f:
            for i in range(count):
                start = i * chunk
                end = size - 1 if i == count - 1 else start + chunk - 1
                f.seek(start)
                body = f.read(end - start + 1)
                up = requests.put(upload_url, data=body, timeout=600, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(len(body)),
                    "Content-Range": f"bytes {start}-{end}/{size}"})
                if up.status_code not in (200, 201, 206):
                    raise SocialError(f"TikTok upload failed at part {i + 1}/{count}: {up.status_code} {up.text[:150]}")

    def direct_post(self, video: Path, caption: str, privacy: str, allow_comment: bool = False,
                    allow_duet: bool = False, allow_stitch: bool = False, brand_organic: bool = False,
                    brand_content: bool = False, duration: float = 0) -> dict:
        """Posts straight to the profile (no inbox step), with the choices the user made on the
        Post to TikTok screen. Re-checks TikTok's rules here too, in case the page was out of date."""
        info = self.creator_info()   # latest creator info, as TikTok requires
        allowed = [o["value"] for o in info["privacy_options"]]
        if privacy not in allowed:
            raise SocialError(f"'{self.PRIVACY_NAMES.get(privacy, privacy)}' isn't allowed for this account. "
                              f"Allowed: {', '.join(self.PRIVACY_NAMES.get(a, a) for a in allowed) or 'none'}.")
        if brand_content and privacy == "SELF_ONLY":
            raise SocialError("Branded content visibility cannot be set to private.")
        if duration and info.get("max_seconds") and duration > info["max_seconds"]:
            raise SocialError(f"This video is {int(duration)}s long, but this account can post up to "
                              f"{info['max_seconds']}s.")
        size = video.stat().st_size
        chunk, count = self.plan_chunks(size)
        d = self._post("video/init/", {
            "post_info": {"title": caption[:2200], "privacy_level": privacy,
                          "disable_comment": bool(info["comment_disabled"]) or not allow_comment,
                          "disable_duet": bool(info["duet_disabled"]) or not allow_duet,
                          "disable_stitch": bool(info["stitch_disabled"]) or not allow_stitch,
                          "brand_content_toggle": bool(brand_content), "brand_organic_toggle": bool(brand_organic),
                          "video_cover_timestamp_ms": 1000},
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk,
                            "total_chunk_count": count}})
        self._send_file(video, d["upload_url"], chunk, count, size)
        return {"publish_id": d["publish_id"], "mode": "direct", "privacy": privacy,
                "username": info.get("username", "")}

    # ---- token calls: your own app's secret if you have one, otherwise the AutoClipper login helper
    def _token_call(self, path: str, fields: dict) -> tuple[int, dict]:
        """path: 'token' or 'revoke'. Returns (http_status, json)."""
        key, secret = self.keys()
        if secret:   # own TikTok app: talk to TikTok directly
            r = requests.post(f"https://open.tiktokapis.com/v2/oauth/{path}/", timeout=30,
                              data={"client_key": key, "client_secret": secret, **fields})
        else:        # built-in app: the helper adds the secret (it never reaches this computer)
            if "SUBDOMAIN" in LOGIN_HELPER_URL:
                raise SocialError("The AutoClipper login helper isn't set up in this version.")
            if path == "revoke":
                helper_path, body = "/tiktok/revoke", {"token": fields["token"]}
            elif fields.get("grant_type") == "refresh_token":
                helper_path, body = "/tiktok/refresh", {"refresh_token": fields["refresh_token"]}
            else:
                helper_path, body = "/tiktok/token", {"code": fields["code"], "code_verifier": fields["code_verifier"],
                                                      "redirect_uri": fields["redirect_uri"]}
            try:
                r = requests.post(LOGIN_HELPER_URL + helper_path, json=body, timeout=30)
            except requests.RequestException as e:
                raise SocialError(f"Couldn't reach the AutoClipper login helper ({e}). Check your internet.")
        try:
            return r.status_code, (r.json() if r.content else {})
        except ValueError:
            return r.status_code, {"error_description": r.text[:200]}

    def auth_url(self) -> str:
        k = self.keys()
        if not k:
            raise SocialError("TikTok isn't set up yet.")
        verifier = secrets.token_urlsafe(64)[:100]
        state = "tt_" + secrets.token_urlsafe(16)
        self._pending[state] = verifier
        challenge = hashlib.sha256(verifier.encode()).hexdigest()   # TikTok desktop wants HEX, not base64
        return self.AUTH + "?" + urlencode({
            "client_key": k[0], "response_type": "code", "scope": self.SCOPES,
            "redirect_uri": TIKTOK_REDIRECT, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})

    def finish(self, code: str, state: str) -> str:
        verifier = self._pending.pop(state, None)
        k = self.keys()
        if not verifier or not k:
            raise SocialError("Login expired - click Connect again.")
        status, d = self._token_call("token", {
            "code": code, "grant_type": "authorization_code", "redirect_uri": TIKTOK_REDIRECT,
            "code_verifier": verifier})
        if status != 200 or "access_token" not in d:
            raise SocialError(f"TikTok login failed: {d.get('error_description') or d.get('error') or status}")
        if "video.upload" not in d.get("scope", ""):
            raise SocialError("TikTok didn't grant 'video.upload'. Add the Content Posting API to your app "
                              "and tick video.upload, then connect again.")
        tok = {"access_token": d["access_token"], "refresh_token": d["refresh_token"],
               "open_id": d.get("open_id", ""), "scopes": d.get("scope", ""),
               "expires_at": time.time() + d.get("expires_in", 86400) - 120}
        name = ""
        try:
            u = requests.get("https://open.tiktokapis.com/v2/user/info/", timeout=20,
                             params={"fields": "display_name"},
                             headers={"Authorization": f"Bearer {tok['access_token']}"}).json()
            name = u.get("data", {}).get("user", {}).get("display_name", "")
        except Exception:
            pass
        tok["display_name"] = name
        _save_tokens("tiktok", tok)
        return name

    def disconnect(self):
        """Revokes AutoClipper's access at TikTok (best effort), then deletes the saved login."""
        t = _load_tokens().get("tiktok", {})
        if t.get("access_token") and self.keys():
            try:
                self._token_call("revoke", {"token": t["access_token"]})
            except Exception:
                pass
        _save_tokens("tiktok", None)

    def _token(self) -> str:
        t = _load_tokens().get("tiktok", {})
        if not t.get("refresh_token"):
            raise SocialError("TikTok isn't connected.")
        if time.time() < t.get("expires_at", 0):
            return t["access_token"]
        status, d = self._token_call("token", {"grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
        if "access_token" not in d:
            raise SocialError(f"TikTok login expired - click Connect again ({d.get('error_description') or d.get('error') or status}).")
        t.update(access_token=d["access_token"], refresh_token=d.get("refresh_token", t["refresh_token"]),
                 expires_at=time.time() + d.get("expires_in", 86400) - 120)
        _save_tokens("tiktok", t)
        return t["access_token"]

    @classmethod
    def plan_chunks(cls, size: int) -> tuple[int, int]:
        """TikTok rules: one chunk up to 64 MB; otherwise 5-64 MB chunks, leftovers go in the last one."""
        if size <= cls.MAX_SINGLE:
            return size, 1
        return cls.CHUNK, size // cls.CHUNK

    def upload_to_inbox(self, video: Path) -> dict:
        size = video.stat().st_size
        chunk, count = self.plan_chunks(size)
        r = requests.post("https://open.tiktokapis.com/v2/post/publish/inbox/video/init/", timeout=60,
                          headers={"Authorization": f"Bearer {self._token()}",
                                   "Content-Type": "application/json; charset=UTF-8"},
                          json={"source_info": {"source": "FILE_UPLOAD", "video_size": size,
                                                "chunk_size": chunk, "total_chunk_count": count}})
        d = r.json() if r.content else {}
        code = (d.get("error") or {}).get("code", "")
        if r.status_code != 200 or code not in ("", "ok"):
            msg = (d.get("error") or {}).get("message") or _err(r)
            if "spam_risk_too_many_pending_share" in code:
                msg = "TikTok allows only 5 videos waiting in your inbox per day. Post the ones there first."
            raise SocialError(f"TikTok refused the upload: {msg}")
        publish_id, upload_url = d["data"]["publish_id"], d["data"]["upload_url"]
        with open(video, "rb") as f:
            for i in range(count):
                start = i * chunk
                end = size - 1 if i == count - 1 else start + chunk - 1
                f.seek(start)
                body = f.read(end - start + 1)
                up = requests.put(upload_url, data=body, timeout=600, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(len(body)),
                    "Content-Range": f"bytes {start}-{end}/{size}"})
                if up.status_code not in (200, 201, 206):
                    raise SocialError(f"TikTok upload failed at part {i + 1}/{count}: {up.status_code} {up.text[:150]}")
        return {"publish_id": publish_id}

    def check(self, publish_id: str) -> dict:
        """Status of an inbox upload: SEND_TO_USER_INBOX (waiting for you in the app),
        PUBLISH_COMPLETE (posted), FAILED, or still processing."""
        r = requests.post("https://open.tiktokapis.com/v2/post/publish/status/fetch/", timeout=30,
                          headers={"Authorization": f"Bearer {self._token()}",
                                   "Content-Type": "application/json; charset=UTF-8"},
                          json={"publish_id": publish_id})
        d = (r.json().get("data") or {}) if r.content else {}
        ids = d.get("publicaly_available_post_id") or d.get("publicly_available_post_id") or []
        return {"status": d.get("status", ""), "post_id": str(ids[0]) if ids else "",
                "fail_reason": d.get("fail_reason", "")}
