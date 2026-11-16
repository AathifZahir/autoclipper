# AutoClipper

AutoClipper is a free, open-source Windows app for clip channels. It watches live Twitch streams and notices when chat suddenly explodes ("KEKW", "????", "CLIP IT"). It clips that moment, turns it into a vertical video with a title and captions, and lets you review and post it to YouTube Shorts and TikTok.

**Website:** [aathifzahir.github.io/autoclipper-site](https://aathifzahir.github.io/autoclipper-site/) · **Download:** [latest release](https://github.com/AathifZahir/autoclipper/releases/latest)

It runs on your own computer. Your logins and videos stay there, and there's no account to create.

Each clip goes through these steps:
1. Chat spikes.
2. Twitch makes the clip.
3. The clip is downloaded to your computer.
4. A vertical 9:16 copy is made, with a title and captions.
5. You review it in the publisher and post it to YouTube or TikTok, or both.

**You need:**
- Windows 10 or 11
- [Python 3.11 or newer](https://www.python.org/downloads/) (tick "Add python.exe to PATH" when installing)
- A Twitch account

The download is about 1 MB. Setup installs about 600 MB of tools the first time.

## Text on the video

The vertical version gets:
- **A title at the top.** By default it's "Streamer | Game". With a free Gemini API key (aistudio.google.com/apikey), AI writes a catchy hook title instead, plus a YouTube post title and a 1–10 score. These are saved in `clips_log.csv` and sent with Telegram alerts.
- **The streamer's name** (twitch.tv/name) under the gameplay. This is off by default, because TikTok doesn't allow links on posted videos. The credit goes in the caption instead.
- **Word-by-word captions** of what the streamer says, with the spoken word in yellow. These are made free on your laptop by faster-whisper. The first time, it downloads a ~500 MB speech model.

Turn any of these on or off with `setup.bat` (step 5), or in `config.toml`.

## First time (about 5 minutes)

1. Double-click **`setup.bat`**. It installs everything and asks you a few questions:
   - **Twitch app:** press Enter to use AutoClipper's built-in app. Advanced users can use their own.
   - **Log in:** approve AutoClipper in your browser. Use a separate "clipper" Twitch account, because the clips show that account's name.
   - **What to watch:** either a **game** (it automatically follows that game's biggest live streams) or a list of **streamers**.
   - **Limits:** clip length, clips per hour, cooldown, and sensitivity.
   - **Telegram (optional):** clips get sent to your phone.
2. Double-click **`test-clip.bat`**, type a channel that is live right now, and check that a clip appears in the `clips` folder. This confirms everything works.

## Everyday use

| Double-click | What it does |
|---|---|
| **`AutoClipper.bat`** | **Opens the control center in your browser.** Start and stop clipping, see what it's watching and doing live, change settings, connect accounts, and review and post clips. All in one page. |
| `start.bat` | Starts clipping in a terminal window instead. Press Ctrl+C to stop. |
| `test-mode.bat` | Shows the moments it *would* clip, without making clips. Good for tuning sensitivity. |
| `test-clip.bat` | Makes one clip right now. |
| `open-final-folder.bat` | Opens your **finished videos** (vertical, with subtitles and title), ready to post. |
| `open-clips-folder.bat` | Opens the original landscape clips from Twitch. |
| `setup.bat` | Changes any setting. Press Enter to keep the current value. |

Finished videos are saved in `final\<date>\` as `time_streamer_Title.mp4`. Next to each one is a `.txt` file with the post title, a description with credit and hashtags, the AI score and what was said. The original landscape clips are kept in `clips\<date>\`. Every clip is also listed in `clips_log.csv`, which opens in Excel or Google Sheets, with the channel, game, reason and Twitch link.

## Publishing to YouTube and TikTok

Double-click **`publisher.bat`**. A page opens in your browser. It runs only on your computer.

1. **Pick clips:** tick the box on each video you want to post. Play them right there.
2. **Write details:** click **✨ Write details for selected**, or **✨ Write with Gemini** on one clip. Gemini writes:
   - the YouTube title (with #shorts)
   - the description, with credit to the streamer
   - tags
   - a TikTok caption

   You can edit anything. It saves as you type.
3. **Publish:** tick YouTube and/or TikTok, choose the YouTube visibility (Public, Unlisted or Private), and click **Publish selected**.

- **YouTube:** the video is posted straight to your channel. Vertical clips under 3 minutes become Shorts. You can post about 100 videos a day.
- **TikTok, two ways:**
  - **Send to inbox** (the TikTok box in the bar): the video goes to your TikTok inbox. Open TikTok, tap the notification, paste the caption (use the **Copy** button) and post. You can have up to 5 videos waiting in your inbox per day.
  - **Post to TikTok…** (button on each clip): opens TikTok's posting screen. You see a preview, edit the title, choose who can view it, set comments/Duet/Stitch, add any content disclosure, and click **Post**.
- The **Posted** tab shows what's already uploaded, with links. The page warns you before posting the same clip twice.

**Connecting (one time):**
- **TikTok:** click **Connect** next to TikTok and approve AutoClipper on TikTok's page. That's all.
- **YouTube:** you need a free Google Cloud "Desktop app" login file. The *Connect YouTube* section on the page walks you through it (about 5 minutes).

**How TikTok login works:** TikTok needs a secret key to finish a login, and a secret can't be shipped inside an app anyone can download. So AutoClipper sends the one-time login code to a small helper ([`login-helper/`](login-helper/), runs on Cloudflare). The helper adds the key, gets your login from TikTok and sends it straight back to your computer. It stores and logs nothing.

Logins are saved in `social_tokens.json` and `youtube_client.json`. Don't share these files.

## Good to know

- **Keep the laptop plugged in with the lid open.** AutoClipper stops Windows from going to sleep while it runs. Closing the lid can still put it to sleep, unless you set "When I close the lid" to *Do nothing* in Windows power settings.
- **It recovers by itself.**
  - Lost Wi-Fi or a dropped chat connection: it reconnects.
  - Twitch hiccups: it retries.
  - An unexpected crash: `start.bat` restarts it after 15 seconds.
  - The downloader updates itself every time you start.
- **Each new channel is "learned" for 60 seconds** before anything is clipped, so it knows that channel's normal chat speed.
- **Some streamers only allow followers or subs to clip.** AutoClipper notices this and skips them. To clip them, follow them on your clipper account and restart.
- **Too many or too few clips?** Change sensitivity in `setup.bat`, or edit `config.toml` in Notepad. Advanced values: `min_messages` (messages needed in 10s), `min_chatters` (different people needed), `clip_calls` (how many people typing "clip" triggers a clip).
- **Don't share `tokens.json`.** It is your Twitch login.

## Before you post

- YouTube gives less reach to plain re-uploaded clips, and needs "meaningful changes" for monetization. Add captions, a hook title or commentary, or make compilations.
- Clip streamers who are OK with it. Many run official clipping programs and some pay clippers.
