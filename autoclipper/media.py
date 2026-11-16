"""Downloading finished Twitch clips and making the vertical (9:16) version."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yt_dlp

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class _Quiet:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def download_clip(clip_id: str, out: Path, log, wait_first: float = 12, tries: int = 8) -> bool:
    """Twitch needs a few seconds to finish a new clip, so keep trying for ~1.5 minutes."""
    out.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://clips.twitch.tv/{clip_id}"
    time.sleep(wait_first)
    for i in range(tries):
        try:
            opts = {"outtmpl": str(out), "quiet": True, "no_warnings": True, "noprogress": True,
                    "overwrites": True, "logger": _Quiet(), "format": "best[ext=mp4]/best"}
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            if out.exists() and out.stat().st_size > 10_000:
                return True
        except Exception as e:  # clip still processing, network blip, etc.
            last = e
            if i == tries - 1:
                log(f"  Download failed: {str(last)[:150]}")
        time.sleep(10)
    return False


def ffmpeg_path() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def video_duration(path: Path) -> float:
    """Length of a video in seconds (0 if it can't be read)."""
    r = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                       creationflags=_NO_WINDOW)
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", r.stderr)
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0


VERTICAL_FILTER = (
    "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
    "boxblur=20:2,eq=brightness=-0.08[bg];"
    "[0:v]scale=1080:-2[fg];"
    "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"
)


def make_vertical(src: Path, dst: Path, ass_text: str | None = None) -> None:
    """Gameplay in the middle, blurred copy filling the top and bottom (1080x1920).
    ass_text = optional captions/title (ASS subtitle format) burned into the video."""
    src, dst = Path(src).resolve(), Path(dst).resolve()
    vf = VERTICAL_FILTER
    ass_file = None
    if ass_text:
        # ffmpeg's filter syntax hates Windows paths, so write the file next to the output
        # and run ffmpeg from that folder using just the file name.
        ass_file = dst.with_suffix(".ass")
        ass_file.write_text(ass_text, encoding="utf-8")
        vf = vf.replace("format=yuv420p[v]", f"ass={ass_file.name},format=yuv420p[v]")
    cmd = [ffmpeg_path(), "-y", "-loglevel", "error", "-i", str(src),
           "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW, cwd=dst.parent)
    finally:
        if ass_file:
            ass_file.unlink(missing_ok=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-300:] or "ffmpeg failed")
