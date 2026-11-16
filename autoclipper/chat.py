"""Reads Twitch chat (logged in, read-only) and reconnects by itself if anything drops."""
from __future__ import annotations

import asyncio
import contextlib
import ssl

HOST, PORT = "irc.chat.twitch.tv", 6697


class AuthFailed(Exception):
    pass


def parse_privmsg(line: str):
    """':user!user@user.tmi.twitch.tv PRIVMSG #channel :hello' -> ('channel', 'user', 'hello')."""
    if line.startswith("@"):
        _, _, line = line.partition(" ")
    if not line.startswith(":"):
        return None
    parts = line[1:].split(" ", 2)
    if len(parts) < 3 or parts[1] != "PRIVMSG":
        return None
    channel, _, text = parts[2].partition(" :")
    user = parts[0].split("!", 1)[0]
    return channel.lstrip("#").lower(), user.lower(), text


class TwitchChat:
    def __init__(self, twitch, on_message, log):
        self.twitch = twitch
        self.on_message = on_message
        self.log = log
        self.wanted: set[str] = set()
        self.joined: set[str] = set()
        self._writer: asyncio.StreamWriter | None = None
        self._sync_lock = asyncio.Lock()
        self._ready = False

    async def run(self):
        backoff = 1
        while True:
            try:
                await self._session()
                backoff = 1
            except AuthFailed:
                self.log("Chat login was rejected - refreshing the Twitch login...")
                await asyncio.to_thread(self.twitch.refresh, self.twitch.access_token)
            except (OSError, asyncio.TimeoutError, ConnectionError) as e:
                self.log(f"Chat connection lost ({e or type(e).__name__}). Reconnecting in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                self._writer = None
                self._ready = False
                self.joined.clear()

    async def set_channels(self, channels):
        self.wanted = {c.lower() for c in channels}
        if self._ready:
            await self._sync()

    async def _send(self, msg: str):
        if self._writer:
            self._writer.write((msg + "\r\n").encode("utf-8"))
            await self._writer.drain()

    async def _sync(self):
        async with self._sync_lock:
            for c in sorted(self.wanted - self.joined):
                if not self._ready:
                    return
                await self._send(f"JOIN #{c}")
                self.joined.add(c)
                await asyncio.sleep(0.6)  # stay under Twitch's join rate limit
            for c in sorted(self.joined - self.wanted):
                await self._send(f"PART #{c}")
                self.joined.discard(c)

    async def _session(self):
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(HOST, PORT, ssl=ssl.create_default_context()), timeout=20)
        self._writer = writer
        sync_task = None
        try:
            await self._send(f"PASS oauth:{self.twitch.access_token}")
            await self._send(f"NICK {self.twitch.user_login}")
            while True:
                raw = await asyncio.wait_for(reader.readline(), timeout=400)  # Twitch pings every ~5 min
                if not raw:
                    raise ConnectionError("Twitch closed the connection")
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("PING"):
                    await self._send("PONG" + line[4:])
                    continue
                msg = parse_privmsg(line)
                if msg:
                    self.on_message(*msg)
                    continue
                if " 001 " in line and not self._ready:
                    self._ready = True
                    sync_task = asyncio.create_task(self._sync())
                elif "Login authentication failed" in line or "Improperly formatted auth" in line:
                    raise AuthFailed()
                elif " RECONNECT" in line:
                    raise ConnectionError("Twitch asked us to reconnect")
        finally:
            if sync_task:
                sync_task.cancel()
            with contextlib.suppress(Exception):
                writer.close()
