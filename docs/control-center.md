# AutoClipper control center: design notes

One local page (`AutoClipper.bat` → http://127.0.0.1:8765) that replaces the terminal windows and `.bat` files for everyday use.

## Context

- **Who:** a solo clip-channel creator on a Windows laptop. Not a developer. Uses it in bursts (launch days, evenings) and glances at it while doing other things.
- **Goal:** start clipping the right streams, trust that it's working, adjust it, then review and post.
- **The failure this design prevents:** on 16 Nov 2026, closing a window cut off a YouTube upload and nothing showed it. So:
  1. What's in progress is always visible.
  2. Stopping never silently interrupts a clip or an upload.
- **Constraints:** local only (127.0.0.1); a single HTML file served by the existing Python server; no build step. The clipper must keep running if the page or window closes.
- **Ethics:** no dark patterns. No fake urgency. Plain language. Destructive actions say what they'll do. Streamers are credited. TikTok's posting rules are followed.

## Structure

Three tabs, in the order you use them. They're linkable as `#clipper`, `#clips` and `#settings`.

| Tab | Answers | Contains |
|---|---|---|
| **Clipper** (default) | Is it running? What is it doing right now? | Big status line, Start/Stop, what it's watching, clips this hour, live channel list, activity feed |
| **Clips** | What did it make? What should I post? | The existing review and publish page |
| **Settings** | How is it set up? | What to watch, clip limits, text on video, publishing default, accounts (Twitch, YouTube, TikTok, Gemini) |

Account connection status stays in the header on every tab, because posting depends on it.

## Clipper states (each one is shown in words, not only colour)

| State | Shown as | Main action |
|---|---|---|
| Stopped | "Not clipping" | **Start clipping** |
| Starting | "Starting…" (Twitch login check, picking streams) | (disabled) |
| Learning | "Watching N streams, learning chat speed (60 s)" | Stop |
| Clipping | "Clipping: watching N streams" | Stop |
| Limit reached | "Hourly limit reached. Next clip possible in ~N min" | Stop |
| Working on a clip | Channel row says "Making clip / Downloading / Captions / Rendering" | Stop waits for it |
| Stopping | "Stopping after the current clip…" | **Stop now** (says that it discards the clip in progress) |
| Not set up | "Connect Twitch to start" | Connect Twitch (device code shown on the page) |

## Key decisions

1. **Stop waits by default.** Stop finishes any clip in progress first. "Stop now" exists but names what it throws away. *Why:* the 16 Nov upload loss.
2. **The clipper is its own process.** The page starts it, and it keeps running if the page or window closes. The page re-attaches by reading `runtime/status.json`. *Why:* real conditions (closed tabs, crashed browsers) shouldn't stop clipping.
3. **Settings save immediately but apply on the next start.** If the clipper is running, the page offers "Restart to apply", which also waits for clips in progress. *Why:* transparent consequences, no hidden half-applied state.
4. **Activity feed in plain language, newest first, capped.** No raw logs, no red badges, no sounds. *Why:* glanceable status without attention-bait.
5. **Twitch login on the page** (device code shown, no password typed into AutoClipper). *Why:* a new user can go from download to clipping without the terminal wizard.
6. **Leaving with uploads running asks first** (the browser's own "leave page?" prompt). The header shows "Uploading…" while any upload runs.

## Checks after building

These are the Nielsen heuristics most at risk here, plus WCAG 2.2 AA basics: keyboard reachable, visible focus, labels on every control, states not by colour alone, `aria-live` on the status line.

## Evaluation after building (16 Nov 2026)

What was tested, and what was fixed:

| Check | Result |
|---|---|
| Start in test mode from the page | ✅ "Starting…", then streams listed with "Learning chat (53s)", and a plain-language activity feed |
| Stop with nothing in progress | ✅ Exits cleanly; no stop file left; process gone |
| Stop while a clip is being made (simulated) | ✅ New clips blocked, waits for the clip, then stops. "Stop now" stops at once and says what it drops. |
| Two clippers at once | ✅ Prevented: a second start reports that one is already running |
| Settings validation | ✅ Bad values are rejected per field, in plain words, and the config is untouched. **Fixed:** one raw Python error message. |
| Game lookup | ✅ "VALORANT" found; an unknown name gives a clear message |
| Fields for other modes hidden | **Fixed:** a layout rule overrode `hidden`; added a global `[hidden]` rule |
| "Starting…" shown after stopping | **Fixed:** the page now trusts the clipper's own report |
| Labels | 49 of 51 controls labelled. **Fixed:** the 2 TikTok key fields |
| Keyboard | Tabs work with ←/→, every control is reachable, focus is visible |
| Phone width (375 px) | ✅ No sideways scrolling; buttons stack |
| Screen readers | The status line, upload banner and toasts are live regions; tabs use tab roles |

Not tested live: a real graceful stop during a real clip (simulated instead), and the Twitch device login from the page (the existing login was kept).
