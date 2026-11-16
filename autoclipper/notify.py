"""Optional phone alerts through a free Telegram bot."""
from __future__ import annotations

from pathlib import Path

import requests

TG = "https://api.telegram.org/bot{token}/{method}"
MAX_UPLOAD = 48 * 1024 * 1024  # Telegram bots can send files up to 50 MB


class Telegram:
    def __init__(self, token: str, chat_id: str, log=print):
        self.token, self.chat_id, self.log = token.strip(), str(chat_id).strip(), log

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _call(self, method: str, timeout=30, **kw) -> bool:
        if not self.enabled:
            return False
        try:
            r = requests.post(TG.format(token=self.token, method=method), timeout=timeout, **kw)
            if r.status_code != 200:
                self.log(f"  Telegram: {r.text[:150]}")
            return r.status_code == 200
        except requests.RequestException as e:
            self.log(f"  Telegram not reachable: {e}")
            return False

    def send(self, text: str) -> bool:
        return self._call("sendMessage", data={"chat_id": self.chat_id, "text": text,
                                               "disable_web_page_preview": True})

    def send_video(self, path: Path, caption: str) -> bool:
        if path.stat().st_size > MAX_UPLOAD:
            return self.send(caption + f"\n(file too big for Telegram - saved at {path})")
        with open(path, "rb") as f:
            return self._call("sendVideo", timeout=180, files={"video": (path.name, f, "video/mp4")},
                              data={"chat_id": self.chat_id, "caption": caption[:1000],
                                    "supports_streaming": "true"})

    @staticmethod
    def find_chat_id(token: str) -> str | None:
        """Returns the chat id of whoever last messaged the bot."""
        r = requests.get(TG.format(token=token.strip(), method="getUpdates"), timeout=20)
        r.raise_for_status()
        for upd in reversed(r.json().get("result", [])):
            msg = upd.get("message") or upd.get("channel_post")
            if msg:
                return str(msg["chat"]["id"])
        return None
