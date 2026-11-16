"""Optional AI titles with Google Gemini (free API key from aistudio.google.com/apikey).

Only text is sent (what the streamer said + what chat said), never video, so it stays
well inside the free tier: one small request per clip.
"""
from __future__ import annotations

import json
import time

import requests

API = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-flash-lite-latest"   # ~2s per title; the bigger "flash" models can take 60s+ on the free tier
FALLBACK = "gemini-flash-lite-latest"

PROMPT = """You write text for short vertical gaming clips (YouTube Shorts / TikTok).
Streamer: {streamer}
Game: {game}
Stream title: {stream_title}
What the streamer said in the clip: "{transcript}"
What Twitch chat was typing at that moment: {chat}

Return JSON with:
- "hook": the on-screen text at the top of the video. Max 7 words, punchy, curiosity-driven, no hashtags, no emoji, no quotes.
- "post_title": a YouTube Shorts title, max 80 characters, may include 1-2 hashtags.
- "score": 1-10, how likely this clip is to do well as a Short (be honest; boring talking = low).
Do not invent events that the transcript and chat don't support."""

POST_PROMPT = """You prepare a short vertical gaming clip (a Twitch stream moment) for posting on
YouTube Shorts and TikTok. Write like a popular clip channel: short, punchy, curious, honest.
Streamer: {streamer} (twitch.tv/{channel})
Game: {game}
Stream title: {stream_title}
Text already shown on the video: {hook}
What the streamer said in the clip: "{transcript}"

Return JSON with:
- "youtube_title": max 90 characters, catchy, mention the streamer or game, end with " #shorts".
- "youtube_description": 2-3 short lines about the moment, then a line "Credit: twitch.tv/{channel}",
  then one line of 3-6 hashtags (include #shorts and the game).
- "youtube_tags": 6-12 short search tags (array of strings, no # sign).
- "tiktok_caption": max 150 characters plus 3-6 hashtags (include the game and #fyp), and the
  credit "twitch.tv/{channel}".
Do not invent events the transcript doesn't support. No quotes around the whole text."""


class Gemini:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, log=print):
        self.key, self.model, self.log = api_key.strip(), model.strip() or DEFAULT_MODEL, log
        self._extra: list[str] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def _candidates(self) -> list[str]:
        """Configured model first, then every fast 'flash-lite' model this key can use."""
        if self._extra is None:
            self._extra = []
            try:
                r = requests.get(f"{API}/models", headers={"x-goog-api-key": self.key}, timeout=20,
                                 params={"pageSize": 200})
                if r.ok:
                    names = [m["name"].split("/", 1)[1] for m in r.json().get("models", [])
                             if "generateContent" in m.get("supportedGenerationMethods", [])]
                    self._extra = sorted((n for n in names if "flash-lite" in n and not any(
                        x in n for x in ("image", "tts", "live", "audio", "preview"))), reverse=True)
            except requests.RequestException:
                pass
        out = []
        for m in [self.model, DEFAULT_MODEL, *self._extra, "gemini-flash-latest"]:
            if m not in out:
                out.append(m)
        return out

    def _generate(self, prompt: str, model: str) -> requests.Response:
        return requests.post(f"{API}/models/{model}:generateContent", timeout=25,
                             headers={"x-goog-api-key": self.key},
                             json={"contents": [{"parts": [{"text": prompt}]}],
                                   "generationConfig": {"responseMimeType": "application/json",
                                                        "temperature": 0.8}})

    def write_text(self, streamer: str, game: str, stream_title: str, transcript: str,
                   chat: list[str]) -> dict | None:
        if not self.enabled:
            return None
        prompt = PROMPT.format(streamer=streamer, game=game, stream_title=stream_title,
                               transcript=transcript[:1500] or "(nothing clear)",
                               chat=json.dumps(chat[-25:], ensure_ascii=False)[:1500])
        data, err = self.ask_json(prompt)
        if data is None:
            self.log(f"  AI title skipped - {err}. Using the simple title.")
            return None
        return {"hook": str(data.get("hook", ""))[:80], "post_title": str(data.get("post_title", ""))[:100],
                "score": data.get("score", "")}

    def post_details(self, streamer: str, channel: str, game: str, hook: str, transcript: str,
                     stream_title: str = "") -> tuple[dict | None, str]:
        """Everything needed to publish one clip on YouTube Shorts and TikTok."""
        prompt = POST_PROMPT.format(streamer=streamer, channel=channel, game=game, hook=hook or "-",
                                    stream_title=stream_title or "-",
                                    transcript=transcript[:1500] or "(nothing clear)")
        data, err = self.ask_json(prompt)
        if data is None:
            return None, err
        credit = f"twitch.tv/{channel}"
        yt_title = str(data.get("youtube_title", hook))[:100]
        desc = str(data.get("youtube_description", "")).strip()
        if credit not in desc:
            desc = f"{desc}\n\nCredit: {credit}".strip()
        tags = data.get("youtube_tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        tiktok = str(data.get("tiktok_caption", "")).strip()
        if credit not in tiktok:
            tiktok = f"{tiktok} (via {credit})".strip()
        return {"youtube_title": yt_title, "youtube_description": desc[:4900],
                "youtube_tags": [str(t).strip("# ")[:30] for t in tags if str(t).strip()][:15],
                "tiktok_caption": tiktok[:2200]}, ""

    def ask_json(self, prompt: str) -> tuple[dict | None, str]:
        """Sends a prompt and returns (json_dict, error). Tries several free models if one is busy."""
        if not self.enabled:
            return None, "no Gemini API key in config.toml"
        last = "no answer"
        for attempt in range(2):          # Gemini's free models are sometimes "busy": try them all, twice
            if attempt:
                time.sleep(5)
            for model in self._candidates():
                try:
                    r = self._generate(prompt, model)
                except requests.RequestException as e:
                    last = f"{model}: {type(e).__name__}"
                    continue
                if r.status_code in (400, 401, 403):
                    return None, f"Gemini rejected the API key ({r.status_code}) - check it in config.toml"
                if r.status_code != 200:
                    last = f"{model}: {r.status_code}"
                    continue
                try:
                    data = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
                    if isinstance(data, list):
                        data = data[0]
                    if not isinstance(data, dict):
                        raise ValueError
                except (KeyError, IndexError, ValueError, TypeError):
                    last = f"{model}: unreadable answer"
                    continue
                return data, ""
        return None, f"all Gemini models are busy right now ({last}) - try again in a minute"
