"""Twitch login (device code flow - no password typing, no secret) and the few API calls we need."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import requests

from .config import TOKENS_PATH

AUTH_URL = "https://id.twitch.tv/oauth2"
API_URL = "https://api.twitch.tv/helix"
SCOPES = ["clips:edit", "chat:read"]


class TwitchError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Twitch error {status}: {message}")
        self.status = status
        self.message = message


class NotLoggedIn(Exception):
    pass


def _msg(r: requests.Response) -> str:
    try:
        d = r.json()
        return str(d.get("message") or d.get("error") or r.text)
    except ValueError:
        return r.text[:200]


class Twitch:
    def __init__(self, client_id: str, token_path: Path = TOKENS_PATH):
        self.client_id = client_id.strip()
        self.token_path = Path(token_path)
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.tokens = self._load()
        self.user_login: str | None = self.tokens.get("login")
        self.user_id: str | None = self.tokens.get("user_id")

    # ---------- token storage ----------
    def _load(self) -> dict:
        try:
            return json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.token_path.write_text(json.dumps(self.tokens, indent=2), encoding="utf-8")

    def _store(self, data: dict) -> None:
        self.tokens.update({
            "client_id": self.client_id,
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", self.tokens.get("refresh_token")),
        })
        self._save()

    @property
    def access_token(self) -> str | None:
        if self.tokens.get("client_id") != self.client_id:
            return None
        return self.tokens.get("access_token")

    def has_login(self) -> bool:
        return bool(self.access_token and self.tokens.get("refresh_token"))

    # ---------- login ----------
    def device_login(self, on_code) -> dict:
        """Shows a code; the user approves it in the browser. Returns validate() info."""
        r = self.session.post(f"{AUTH_URL}/device",
                              data={"client_id": self.client_id, "scopes": " ".join(SCOPES)}, timeout=20)
        if r.status_code != 200:
            raise TwitchError(r.status_code, _msg(r))
        d = r.json()
        on_code(d["user_code"], d["verification_uri"])
        interval = d.get("interval", 5)
        deadline = time.time() + d.get("expires_in", 1800)
        while time.time() < deadline:
            time.sleep(interval)
            t = self.session.post(f"{AUTH_URL}/token", data={
                "client_id": self.client_id,
                "scopes": " ".join(SCOPES),
                "device_code": d["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            }, timeout=20)
            if t.status_code == 200:
                self._store(t.json())
                return self.validate()
            m = _msg(t)
            if "authorization_pending" in m:
                continue
            if "slow_down" in m:
                interval += 2
                continue
            raise TwitchError(t.status_code, m)
        raise TwitchError(408, "Login timed out - run setup again.")

    def refresh(self, failed_token: str | None = None) -> None:
        with self._lock:
            # Another thread may already have refreshed (Twitch refresh tokens are single-use).
            if failed_token and self.access_token and self.access_token != failed_token:
                return
            rt = self.tokens.get("refresh_token")
            if not rt or self.tokens.get("client_id") != self.client_id:
                raise NotLoggedIn("Not logged in to Twitch.")
            r = self.session.post(f"{AUTH_URL}/token", data={
                "client_id": self.client_id, "grant_type": "refresh_token", "refresh_token": rt,
            }, timeout=20)
            if r.status_code != 200:
                raise NotLoggedIn(f"Twitch login expired ({_msg(r)}).")
            self._store(r.json())

    def validate(self) -> dict:
        for attempt in range(2):
            token = self.access_token
            if not token:
                raise NotLoggedIn("Not logged in to Twitch.")
            r = self.session.get(f"{AUTH_URL}/validate", headers={"Authorization": f"OAuth {token}"}, timeout=20)
            if r.status_code == 401 and attempt == 0:
                self.refresh(token)
                continue
            if r.status_code != 200:
                raise NotLoggedIn(f"Twitch login is not valid ({_msg(r)}).")
            info = r.json()
            missing = [s for s in SCOPES if s not in info.get("scopes", [])]
            if missing:
                raise NotLoggedIn(f"Login is missing permissions: {', '.join(missing)}.")
            self.user_login, self.user_id = info["login"], info["user_id"]
            self.tokens.update({"login": self.user_login, "user_id": self.user_id})
            self._save()
            return info
        raise NotLoggedIn("Twitch login is not valid.")

    # ---------- API ----------
    def api(self, method: str, path: str, params=None, retry_5xx: bool = False) -> dict:
        waits_5xx = [2, 5, 15] if retry_5xx else []
        refreshed = False
        attempt_5xx = 0
        for _ in range(10):
            token = self.access_token
            if not token:
                raise NotLoggedIn("Not logged in to Twitch.")
            try:
                r = self.session.request(method, API_URL + path, params=params, timeout=20, headers={
                    "Client-Id": self.client_id, "Authorization": f"Bearer {token}"})
            except requests.RequestException as e:
                if attempt_5xx < len(waits_5xx):
                    time.sleep(waits_5xx[attempt_5xx]); attempt_5xx += 1
                    continue
                raise TwitchError(0, f"network problem: {e}") from e
            if r.status_code == 401 and not refreshed:
                self.refresh(token); refreshed = True
                continue
            if r.status_code == 429:
                reset = float(r.headers.get("Ratelimit-Reset", time.time() + 5))
                time.sleep(max(1.0, min(60.0, reset - time.time())))
                continue
            if r.status_code >= 500 and attempt_5xx < len(waits_5xx):
                time.sleep(waits_5xx[attempt_5xx]); attempt_5xx += 1
                continue
            if r.status_code >= 400:
                raise TwitchError(r.status_code, _msg(r))
            return r.json() if r.content else {}
        raise TwitchError(429, "Twitch kept rate-limiting the request.")

    def get_users(self, logins: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(logins), 100):
            out += self.api("GET", "/users", [("login", l) for l in logins[i:i + 100]]).get("data", [])
        return out

    def get_streams(self, user_logins: list[str] | None = None, game_id: str | None = None,
                    language: str | None = None, first: int = 100) -> list[dict]:
        params: list[tuple[str, str]] = [("type", "live"), ("first", str(first))]
        if user_logins:
            params += [("user_login", l) for l in user_logins[:100]]
        if game_id:
            params.append(("game_id", game_id))
        if language:
            params.append(("language", language))
        return self.api("GET", "/streams", params).get("data", [])

    def find_game(self, name: str) -> dict | None:
        data = self.api("GET", "/games", {"name": name}).get("data", [])
        if data:
            return data[0]
        data = self.api("GET", "/search/categories", {"query": name, "first": 1}).get("data", [])
        return data[0] if data else None

    def create_clip(self, broadcaster_id: str, duration: int) -> str:
        d = self.api("POST", "/clips", {"broadcaster_id": broadcaster_id, "duration": duration}, retry_5xx=True)
        return d["data"][0]["id"]

    def get_clip(self, clip_id: str) -> dict | None:
        data = self.api("GET", "/clips", {"id": clip_id}).get("data", [])
        return data[0] if data else None
