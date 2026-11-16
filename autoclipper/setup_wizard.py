"""Step-by-step setup. Run again any time to change settings (press Enter to keep a value)."""
from __future__ import annotations

import re
import sys
import webbrowser

from . import config as config_mod
from .config import Config
from .notify import Telegram
from .twitch import NotLoggedIn, Twitch, TwitchError

LINE = "-" * 64


def title(text: str):
    print(f"\n{LINE}\n {text}\n{LINE}")


def ask(prompt: str, default="") -> str:
    shown = f" [{default}]" if default not in ("", None) else ""
    ans = input(f"{prompt}{shown}: ").strip()
    return ans if ans else str(default)


def ask_yes(prompt: str, default: bool = True) -> bool:
    while True:
        ans = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        if not ans:
            return default
        if ans in ("y", "yes", "n", "no"):
            return ans.startswith("y")


def ask_int(prompt: str, default: int, lo: int, hi: int) -> int:
    while True:
        try:
            v = int(float(ask(prompt, default)))
            if lo <= v <= hi:
                return v
        except ValueError:
            pass
        print(f"  Please enter a number from {lo} to {hi}.")


def ask_choice(prompt: str, options: list[tuple[str, str]], default: str) -> str:
    for i, (key, label) in enumerate(options, 1):
        print(f"  {i}) {label}")
    keys = [k for k, _ in options]
    d = str(keys.index(default) + 1) if default in keys else "1"
    while True:
        ans = ask(prompt, d)
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return keys[int(ans) - 1]
        print("  Please type one of the numbers above.")


def split_names(text: str) -> list[str]:
    return [c.strip().lower().lstrip("@").split("/")[-1] for c in re.split(r"[,\s]+", text) if c.strip()]


# ---------------------------------------------------------------- steps
def step_client_id(cfg: Config, force_own: bool = False):
    title("Step 1 of 6 - Twitch app")
    from .defaults import TWITCH_CLIENT_ID
    if not force_own:
        if cfg.client_id and cfg.client_id != TWITCH_CLIENT_ID:
            if ask_yes(f"Keep using your own Twitch app ({cfg.client_id[:6]}...)?"):
                return
        print("  AutoClipper has a built-in Twitch app, so you don't need to make one.")
        if ask_yes("Use the built-in Twitch app? (recommended)"):
            cfg.client_id = ""
            return
    print("""
  1. A page will open: dev.twitch.tv (log in with any Twitch account).
     If asked, turn on two-factor authentication - Twitch requires it for apps.
  2. Click "Register Your Application" and fill in:
       Name ................ anything unique, e.g. MyClipper12345
       OAuth Redirect URLs . http://localhost
       Category ............ Other
       Client Type ......... Public    <-- important
  3. Click Create, then "Manage", and copy the Client ID.
""")
    if ask_yes("Open the Twitch developer page now?"):
        webbrowser.open("https://dev.twitch.tv/console/apps/create")
    while True:
        cid = ask("Paste the Client ID here").strip()
        if re.fullmatch(r"[a-z0-9]{20,40}", cid):
            cfg.client_id = cid
            return
        print("  That doesn't look like a Client ID (about 30 letters/numbers). Try again.")


def step_login(cfg: Config) -> Twitch:
    title("Step 2 of 6 - Log in to Twitch")
    while True:
        tw = Twitch(cfg.twitch_id)
        if tw.has_login():
            try:
                info = tw.validate()
                if ask_yes(f"Already logged in as '{info['login']}'. Keep this account?"):
                    return tw
            except (NotLoggedIn, TwitchError):
                pass
        print("""
  Clips you make will show this account's name on Twitch.
  TIP: use a separate 'clipper' account, not your main one. If the browser is
  already logged in to a different account, use a private/incognito window.
""")

        def show(code, uri):
            print(f"  Opening {uri}")
            print(f"  If it asks for a code, type:   {code}")
            print("  Then click Authorize. Waiting for you...")
            webbrowser.open(uri)

        try:
            info = tw.device_login(show)
            print(f"\n  Logged in as '{info['login']}'.")
            return tw
        except TwitchError as e:
            print(f"\n  Login failed: {e.message}")
            if "client" in e.message.lower():
                print("  The Client ID is wrong, or the app's Client Type isn't 'Public'.")
                step_client_id(cfg, force_own=bool(cfg.client_id))
            elif not ask_yes("Try again?"):
                sys.exit(1)


def step_targets(cfg: Config, tw: Twitch):
    title("Step 3 of 6 - What should it watch?")
    cfg.mode = ask_choice("Choose", [
        ("game", "A game - automatically watch its biggest live streams (best for new releases)"),
        ("top", "The most-watched live streams on Twitch right now (any game)"),
        ("channels", "Specific streamers I choose"),
    ], cfg.mode)

    if cfg.mode in ("game", "top"):
        while cfg.mode == "game":
            name = ask("Game name (as on Twitch)", cfg.game)
            game = tw.find_game(name) if name else None
            if game and ask_yes(f"  Found '{game['name']}'. Use this?"):
                cfg.game = game["name"]
                break
            if not game:
                print("  Not found on Twitch. Check the spelling.")
        cfg.max_streams = ask_int("How many streams to watch at once (1-20)", cfg.max_streams, 1, 20)
        cfg.min_viewers = ask_int("Skip streams with fewer viewers than", cfg.min_viewers, 0, 1_000_000)
        lang = ask("Only one language? e.g. en, es, pt (Enter = any)", cfg.language or "")
        cfg.language = lang.strip().lower()[:2] if lang.strip() else ""
        ex = ask("Streamers to never clip (comma separated, Enter = none)", ", ".join(cfg.exclude_channels))
        cfg.exclude_channels = split_names(ex)
    else:
        while True:
            raw = ask("Twitch channel names (comma separated)", ", ".join(cfg.channels))
            names = split_names(raw)
            if not names:
                continue
            found = {u["login"].lower() for u in tw.get_users(names)}
            missing = [n for n in names if n not in found]
            if missing:
                print(f"  Not found on Twitch: {', '.join(missing)}")
            if found and (not missing or ask_yes("  Continue with the ones that were found?")):
                cfg.channels = [n for n in names if n in found]
                break


def step_limits(cfg: Config):
    title("Step 4 of 6 - Clip settings")
    cfg.clip_seconds = ask_int("Clip length in seconds (5-60)", cfg.clip_seconds, 5, 60)
    cfg.max_clips_per_hour = ask_int("Maximum clips per hour (all channels together)", cfg.max_clips_per_hour, 1, 60)
    cfg.cooldown_minutes = ask_int("Minutes to wait before clipping the same streamer again", int(cfg.cooldown_minutes), 0, 120)
    print("\n  How easily should a moment count?")
    cfg.sensitivity = ask_choice("Choose", [
        ("low", "Low - only big moments (fewer clips)"),
        ("medium", "Medium - recommended"),
        ("high", "High - more clips, some boring ones"),
    ], cfg.sensitivity)
    cfg.make_vertical = ask_yes("Also make a vertical 9:16 version for Shorts / TikTok?", cfg.make_vertical)


def step_text(cfg: Config):
    title("Step 5 of 6 - Text on the video")
    if not cfg.make_vertical:
        print("  Text is added to the vertical version, which is turned off - skipping.")
        return
    cfg.captions = ask_yes("Add captions (subtitles of what the streamer says)?", cfg.captions)
    cfg.title_text = ask_yes("Add a title at the top of the video?", cfg.title_text)
    cfg.streamer_credit = ask_yes("Show the streamer's name (twitch.tv/name) under the gameplay?", cfg.streamer_credit)

    if cfg.title_text:
        print("""
  Optional: let Google's Gemini AI write a catchy title for each clip (free).
  Without it, the title is just "Streamer | Game".
    1. Open aistudio.google.com/apikey and sign in with your Google account.
    2. Click "Create API key" and copy it.""")
        if ask_yes("Use AI titles?", bool(cfg.gemini_api_key)):
            if cfg.gemini_api_key and ask_yes(f"Keep the current key ({cfg.gemini_api_key[:6]}...)?"):
                pass
            else:
                if ask_yes("Open the Google AI Studio page now?"):
                    webbrowser.open("https://aistudio.google.com/apikey")
                cfg.gemini_api_key = ask("Paste the API key here").strip()
            if cfg.gemini_api_key:
                from .ai import Gemini
                print("  Testing the key...")
                out = Gemini(cfg.gemini_api_key, cfg.gemini_model, log=lambda m: print(m)).write_text(
                    "TestStreamer", "Fortnite", "late night wins", "no way he hit that shot", ["LMAO", "W", "CLIP IT"])
                if out:
                    print(f"  Works! Example title: \"{out['hook']}\"")
                else:
                    print("  The key didn't work. Clips will use simple titles until you fix it (run setup again).")
        else:
            cfg.gemini_api_key = ""

    if cfg.captions:
        print(f"\n  Getting the captions model ready (first time downloads ~250-500 MB)...")
        try:
            from .captions import load_model
            load_model(cfg.caption_model)
            print("  Captions ready.")
        except Exception as e:
            print(f"  Couldn't prepare captions now ({e}). It will try again when making the first clip.")


def step_telegram(cfg: Config):
    title("Step 6 of 6 - Phone alerts (optional)")
    print("  Get every clip sent to your phone through a free Telegram bot.")
    if not ask_yes("Set up Telegram alerts?", bool(cfg.telegram_bot_token)):
        cfg.telegram_bot_token = cfg.telegram_chat_id = ""
        return
    if cfg.telegram_bot_token and cfg.telegram_chat_id and ask_yes("Keep the current Telegram bot?"):
        return
    print("""
  1. In Telegram, open a chat with  @BotFather  and send:  /newbot
  2. Give it any name and a username ending in 'bot'.
  3. BotFather replies with a token like 123456:ABC-xyz... - paste it below.
""")
    while True:
        token = ask("Bot token").strip()
        if not re.fullmatch(r"\d+:[\w-]{20,}", token):
            print("  That doesn't look like a bot token. Try again.")
            continue
        input("  Now open your new bot in Telegram, press Start (or send 'hi'), then press Enter here...")
        try:
            chat_id = Telegram.find_chat_id(token)
        except Exception as e:
            print(f"  Couldn't reach Telegram with that token ({e}).")
            chat_id = None
        if not chat_id:
            print("  No message found yet. Make sure you sent your bot a message.")
            if not ask_yes("Try again?"):
                return
            continue
        tg = Telegram(token, chat_id)
        if tg.send("AutoClipper is connected. Clips will arrive here."):
            print("  Test message sent - check Telegram.")
            cfg.telegram_bot_token, cfg.telegram_chat_id = token, chat_id
            return
        print("  Sending the test message failed.")
        if not ask_yes("Try again?"):
            return


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"\n{LINE}\n  AutoClipper setup - about 5 minutes. Press Enter to accept [defaults].\n{LINE}")
    cfg = config_mod.load() or Config()
    try:
        step_client_id(cfg)
        config_mod.save(cfg)
        tw = step_login(cfg)
        config_mod.save(cfg)
        step_targets(cfg, tw)
        step_limits(cfg)
        config_mod.save(cfg)
        step_text(cfg)
        config_mod.save(cfg)
        step_telegram(cfg)
    except (KeyboardInterrupt, EOFError):
        print("\n\nSetup cancelled. Nothing else was changed.")
        return 1
    config_mod.save(cfg)
    title("All set!")
    print("""  Double-click  start.bat      to start clipping.
  Double-click  test-mode.bat  to only SHOW moments without making clips (good first try).
  Double-click  test-clip.bat  to make one clip right now and check everything works.

  Settings are saved in config.toml - run setup.bat again to change them.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
