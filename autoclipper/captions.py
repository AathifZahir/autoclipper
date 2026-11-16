"""Speech-to-text (free, runs on your laptop) and the on-screen text for the vertical video.

Layout of the 1080x1920 vertical video:
    top    : hook title in a white box, just above the gameplay
    middle : gameplay (1080x608, y 656-1264)
    under  : small streamer credit, e.g. twitch.tv/oatley
    bottom : big word-by-word captions, the spoken word highlighted in yellow
"""
from __future__ import annotations

import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from .media import ffmpeg_path

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
_model = None
_model_name = None
_lock = threading.Lock()


@dataclass
class Word:
    text: str
    start: float
    end: float


def load_model(name: str = "small"):
    """Loads the speech model once (the first time it downloads ~250-500 MB)."""
    global _model, _model_name
    if _model is None or _model_name != name:
        from faster_whisper import WhisperModel
        _model = WhisperModel(name, device="cpu", compute_type="int8")
        _model_name = name
    return _model


def transcribe(video: Path, model_name: str = "small", language: str = "") -> list[Word]:
    # Decode audio with ffmpeg ourselves (more reliable than faster-whisper's built-in decoder).
    import numpy as np
    raw = subprocess.run([ffmpeg_path(), "-loglevel", "error", "-i", str(video), "-vn", "-ac", "1",
                          "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, check=True, creationflags=_NO_WINDOW).stdout
    audio = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    if audio.size == 0:
        return []
    with _lock:  # one transcription at a time keeps the laptop responsive
        model = load_model(model_name)
        segments, _ = model.transcribe(audio, word_timestamps=True, vad_filter=True,
                                       language=language or None)
        return [Word(w.word.strip(), w.start, w.end) for s in segments for w in (s.words or []) if w.word.strip()]


# ---------------------------------------------------------------- ASS subtitle file
def clean(text: str) -> str:
    """Remove emoji/symbols the font can't draw, and characters that mean something in ASS."""
    text = "".join(ch for ch in text if ord(ch) < 0x2000 or 0x3000 <= ord(ch) < 0xD800)
    text = text.replace("{", "(").replace("}", ")").replace("\\", "/").replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def chunk_words(words: list[Word], max_words: int = 3, max_gap: float = 0.6) -> list[list[Word]]:
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w.start - cur[-1].end > max_gap or cur[-1].text[-1:] in ".?!"):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial Black,84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,2,60,60,400,1
Style: Title,Arial Black,60,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,16,0,2,90,90,1325,1
Style: Credit,Arial,38,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,1,0,1,3,0,8,60,60,1285,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
YELLOW = r"{\c&H00E5FF&}"   # ASS colours are BGR
WHITE = r"{\c&HFFFFFF&}"


def build_ass(words: list[Word], duration: float, title: str = "", credit: str = "") -> str:
    lines = [HEADER]
    end = _ts(duration + 1)
    if title:
        lines.append(f"Dialogue: 1,0:00:00.00,{end},Title,,0,0,0,,{clean(title)}")
    if credit:
        lines.append(f"Dialogue: 1,0:00:00.00,{end},Credit,,0,0,0,,{clean(credit)}")
    for chunk in chunk_words(words):
        texts = [clean(w.text).upper() for w in chunk]
        for i, w in enumerate(chunk):
            start = w.start
            stop = chunk[i + 1].start if i + 1 < len(chunk) else max(w.end, w.start + 0.25)
            body = " ".join(f"{YELLOW}{t}{WHITE}" if j == i else t for j, t in enumerate(texts))
            pop = r"{\fscx108\fscy108\t(0,80,\fscx100\fscy100)}" if i == 0 else ""
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(stop)},Caption,,0,0,0,,{pop}{body}")
    return "\n".join(lines) + "\n"
