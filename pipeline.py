#!/usr/bin/env python3
"""
Shorts Autopilot
================
Turns a folder of video files you own into two scheduled YouTube Shorts a day,
with no interaction once it is set up.

Per run it: picks an unused source file -> cuts a random window -> transcribes
the speech -> re-cuts to the speech -> burns word-timed captions and a hook
-> writes a title/description -> uploads to YouTube scheduled at the next slots.

Free components only: ffmpeg, faster-whisper, YouTube Data API, a scheduled
runner (GitHub Actions). No paid service is used or required.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".ogv", ".mpg", ".mpeg"}
FONT_URL = "https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip"
FONT_CACHE = Path.home() / ".cache" / "shorts-autopilot" / "fonts"
RTL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"

class NoSpeech(Exception):
    """No usable speech in this window, so there is nothing to caption."""


STOPWORDS = set(
    """the a an and or but if then than that this these those i you he she it we they
    me him her us them my your his its our their is are was were be been being am do
    does did doing have has had having will would can could should shall may might must
    of in on at to from by with about into over after before under again further once
    here there when where why how all any both each few more most other some such no nor
    not only own same so too very s t just don now get got like really know think going
    want one out up what which who whom yours ours theirs mine""".split()
)


# --------------------------------------------------------------------------
# basics
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[autopilot] {msg}", flush=True)


def ffmpeg_bin() -> str:
    if os.environ.get("FFMPEG_BIN"):
        return os.environ["FFMPEG_BIN"]
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg  # bundled static build, MIT-licensed
    return imageio_ffmpeg.get_ffmpeg_exe()


def run(cmd: list, cwd: Path | None = None, timeout: int = 1800) -> str:
    proc = subprocess.run(
        [str(c) for c in cmd], cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "command failed: " + " ".join(str(c) for c in cmd) + "\n" + proc.stderr[-3000:]
        )
    return proc.stderr


def probe(path: Path) -> tuple[float, int, int]:
    """(duration_seconds, width, height) using ffmpeg alone, no ffprobe needed."""
    text = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-i", str(path)], capture_output=True, text=True
    ).stderr
    dur_m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", text)
    size_m = re.search(r"Video:.*?, (\d{2,5})x(\d{2,5})", text)
    if not dur_m or not size_m:
        raise RuntimeError(f"cannot read media info from {path}")
    dur = int(dur_m.group(1)) * 3600 + int(dur_m.group(2)) * 60 + float(dur_m.group(3))
    return dur, int(size_m.group(1)), int(size_m.group(2))


def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg.setdefault("posts_per_day", 2)
    cfg.setdefault("publish_times", ["09:15", "18:45"])
    cfg.setdefault("timezone", "UTC")
    cfg.setdefault("clip_seconds", 32)
    cfg.setdefault("min_speech_ratio", 0.35)
    cfg.setdefault("min_speech_confidence", 0.55)
    cfg.setdefault("max_source_seconds", 900)
    cfg.setdefault("category_id", "24")  # Entertainment
    cfg.setdefault("whisper_model", "base")
    cfg.setdefault("reuse_cooldown_days", 45)
    cfg.setdefault("source_attempts", 3)
    cfg.setdefault("fonts_dir", "")
    cfg.setdefault("title_suffix", "#shorts")
    cfg.setdefault("credit", "")
    cfg.setdefault("tags", [])
    cfg.setdefault("caption", {})
    cfg.setdefault("source", {})
    return cfg


# --------------------------------------------------------------------------
# state: which moments of which files have already gone out
# --------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("state file unreadable, starting a fresh one")
    return {"clips": []}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def recently_used(state: dict, cooldown_days: int) -> set:
    cutoff = datetime.now().timestamp() - cooldown_days * 86400
    used = set()
    for item in state.get("clips", []):
        try:
            when = datetime.fromisoformat(item["at"]).timestamp()
        except (KeyError, ValueError):
            when = 0
        if when >= cutoff:
            used.add((item["file"], round(float(item.get("start", 0)), 1)))
    return used


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

def gather_sources(cfg: dict) -> list[Path]:
    drive_id = cfg["source"].get("drive_folder_id") or os.environ.get("DRIVE_FOLDER_ID", "")
    folder = Path(cfg["source"].get("dir", "sources"))
    if not folder.is_absolute():
        folder = ROOT / folder

    if drive_id and not any(folder.glob("*")):
        try:
            import gdown
        except ImportError:
            log("gdown not installed, skipping Google Drive fetch")
        else:
            log("fetching source footage from Google Drive")
            folder.mkdir(parents=True, exist_ok=True)
            gdown.download_folder(id=drive_id, output=str(folder), quiet=True, use_cookies=False)

    if not folder.exists():
        folder.mkdir(parents=True, exist_ok=True)
        raise SystemExit(
            f"No footage folder at {folder} — I made one for you.\n"
            "Put the video files you have the rights to use in that folder and run again."
        )

    files = sorted(p for p in folder.rglob("*") if p.suffix.lower() in VIDEO_EXT)
    if not files:
        raise SystemExit(
            f"{folder} has no video files in it ({', '.join(sorted(VIDEO_EXT))}).\n"
            "Either put files you have the rights to use in that folder, or enable "
            "feed: in config.yml so free-to-publish footage is fetched automatically."
        )
    log(f"{len(files)} source file(s) available")
    return files


def source_credit(path: Path) -> str:
    """CC BY requires the credit to travel with the video, so read it from the sidecar."""
    sidecar = path.with_name(path.name + ".license.json")
    if not sidecar.exists():
        return ""
    try:
        info = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if info.get("credit"):
        return str(info["credit"])[:400]
    author = str(info.get("author") or "unknown creator")[:80]
    licence = str(info.get("licence") or "free licence")
    page = str(info.get("page") or "")
    return f"Footage: {author} ({licence})" + (f" {page}" if page else "")


def pick_source(files: list[Path], used: set, rng: random.Random,
                skip: set | None = None) -> tuple[Path, float]:
    """A random window from a file that has not already been used in this run."""
    rng.shuffle(files)
    skip = skip or set()
    ordered = [p for p in files if p.name not in skip] + [p for p in files if p.name in skip]
    for path in ordered:
        try:
            dur, _, _ = probe(path)
        except RuntimeError:
            continue
        if dur < 8:
            continue
        start = rng.uniform(0, max(0.0, min(dur, 900) - 40))
        if (path.name, round(start, 1)) not in used:
            return path, start
    return files[0], rng.uniform(0, 30)


# --------------------------------------------------------------------------
# speech
# --------------------------------------------------------------------------

def load_audio(path: Path, sample_rate: int = 16000):
    """Mono float32 samples via ffmpeg, so no external audio decoder is needed."""
    import wave

    import numpy as np

    wav_path = path.with_name(path.stem + "_16k.wav")
    # Old archive film has hiss and rumble; cleaning it first is worth several
    # points of transcription accuracy on the captions.
    run([
        ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-i", path,
        "-vn", "-af", "highpass=f=85,lowpass=f=7000,afftdn=nf=-30,loudnorm=I=-16:TP=-1.5",
        "-ac", "1", "-ar", str(sample_rate), "-f", "wav", wav_path,
    ])
    with wave.open(str(wav_path), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(path: Path, model_size: str) -> list[dict]:
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(
        load_audio(path), word_timestamps=True, vad_filter=True, beam_size=1, language=None
    )
    words: list[dict] = []
    for seg in segments:
        for w in seg.words or []:
            token = (w.word or "").strip()
            if token:
                words.append({
                    "w": token,
                    "s": float(w.start),
                    "e": float(w.end),
                    "p": float(getattr(w, "probability", 1.0)),
                })
    return words


def speech_ratio(words: list[dict], duration: float) -> float:
    if duration <= 0 or not words:
        return 0.0
    return sum(max(0.0, w["e"] - w["s"]) for w in words) / duration


def speech_confidence(words: list[dict]) -> float:
    """How clearly the model heard the speech; low means garbled captions."""
    if not words:
        return 0.0
    return sum(float(w.get("p", 1.0)) for w in words) / len(words)


def speech_bounds(words: list[dict], duration: float) -> tuple[float, float]:
    if not words:
        return 0.0, duration
    start = max(0.0, words[0]["s"] - 0.18)
    end = min(duration, words[-1]["e"] + 0.35)
    return start, end


# --------------------------------------------------------------------------
# captions (ASS is rendered by libass, which ships with ffmpeg)
# --------------------------------------------------------------------------

def clean(text: str) -> str:
    text = text.replace("\\", " ").replace("{", "").replace("}", "")
    text = text.replace("<", "").replace(">", "").replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()


def ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def chunk_words(words: list[dict], max_words=4, max_chars=26, max_gap=0.65) -> list[list[dict]]:
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for w in words:
        if current:
            joined = " ".join(x["w"] for x in current)
            gap = w["s"] - current[-1]["e"]
            if len(current) >= max_words or len(joined) + len(w["w"]) + 1 > max_chars or gap > max_gap:
                chunks.append(current)
                current = []
        current.append(w)
        if re.search(r"[.!?]$", w["w"]):
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks


def build_ass(words: list[dict], hook: str, cfg: dict, offset: float = 0.0, hook_until: float = 3.0) -> str:
    cap = cfg.get("caption", {})
    font = cap.get("font_name", "DejaVu Sans")
    cap_size = int(cap.get("size", 72))
    hook_size = int(cap.get("hook_size", 84))
    cap_colour = cap.get("colour", "&H00FFFFFF")
    hook_colour = cap.get("hook_colour", "&H0000E7FF")

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{cap_size},{cap_colour},{cap_colour},&H00000000,&H50000000,-1,0,0,0,100,100,0,0,1,7,3,2,70,70,420,1
Style: Hook,{font},{hook_size},{hook_colour},{hook_colour},&H00000000,&H70000000,-1,0,0,0,100,100,0,0,1,9,4,8,60,60,300,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    if hook:
        lines.append(
            f"Dialogue: 0,{ass_time(0)},{ass_time(hook_until)},Hook,,0,0,0,,"
            f"{{\\fad(160,220)}}{clean(hook.upper())}\n"
        )
    floor = hook_until if hook else 0.0
    timed = []
    for chunk in chunk_words(words):
        start = max(floor, chunk[0]["s"] - offset - 0.06)
        end = chunk[-1]["e"] - offset + 0.12
        if end > start:
            timed.append([start, end, chunk])
    for index, (start, end, chunk) in enumerate(timed):
        if index + 1 < len(timed):
            end = min(end, timed[index + 1][0] - 0.04)
        if end - start <= 0.05:
            continue
        lines.append(
            f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Cap,,0,0,0,,{clean(' '.join(w['w'] for w in chunk))}\n"
        )
    return "".join(lines)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def cut(src: Path, start: float, duration: float, out: Path) -> None:
    run([
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.2f}", "-t", f"{duration:.2f}", "-i", src,
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart", out,
    ])


def ensure_fonts(cfg: dict, workdir: Path) -> str:
    """libass renders nothing without a font file, so make sure one always exists."""
    candidates = []
    if cfg.get("fonts_dir"):
        candidates.append(Path(cfg["fonts_dir"]))
    candidates.append(ROOT / "assets" / "fonts")
    candidates.append(FONT_CACHE)

    for candidate in candidates:
        if candidate.is_dir() and any(candidate.glob("*.tt[fc]")):
            dest = workdir / "fonts"
            dest.mkdir(exist_ok=True)
            for font in candidate.glob("*.tt[fc]"):
                shutil.copy2(font, dest / font.name)
            return ":fontsdir=fonts"

    FONT_CACHE.mkdir(parents=True, exist_ok=True)
    log("  fetching caption font (one time, then cached)")
    archive = FONT_CACHE / "dejavu.zip"
    try:
        urllib.request.urlretrieve(FONT_URL, archive)
        with zipfile.ZipFile(archive) as bundle:
            for name in bundle.namelist():
                if name.endswith(("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")):
                    with bundle.open(name) as src, open(FONT_CACHE / Path(name).name, "wb") as out:
                        shutil.copyfileobj(src, out)
        archive.unlink(missing_ok=True)
    except Exception as exc:
        log(f"  could not fetch a font ({exc.__class__.__name__}); captions may be missing")
        return ""

    dest = workdir / "fonts"
    dest.mkdir(exist_ok=True)
    for font in FONT_CACHE.glob("*.ttf"):
        shutil.copy2(font, dest / font.name)
    return ":fontsdir=fonts" if any(dest.iterdir()) else ""


def render(clip: Path, ass_text: str, cfg: dict, workdir: Path) -> Path:
    """Crop or blur-pad to 9:16, burn captions, add a short fade-in."""
    _, width, height = probe(clip)
    vertical = (width / height) < 0.72

    (workdir / "captions.ass").write_text(ass_text, encoding="utf-8")

    fonts_arg = ensure_fonts(cfg, workdir)

    if vertical:
        graph = "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920[base]"
    else:
        graph = (
            "[0:v]split=2[bg][fg];"
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
            "gblur=sigma=32,eq=brightness=-0.10:saturation=0.92[bgb];"
            "[fg]scale=1012:-2:flags=lanczos[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:(H-h)/2[base]"
        )
    graph += (
        f";[base]subtitles=captions.ass{fonts_arg},fade=t=in:st=0:d=0.25,format=yuv420p[v]"
    )

    out = workdir / "short.mp4"
    run([
        ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-i", clip,
        "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", out,
    ], cwd=workdir)

    final = workdir / "short_final.mp4"
    if not final.exists():
        shutil.move(out, final)
    return final


# --------------------------------------------------------------------------
# words: titles, descriptions, hooks
# --------------------------------------------------------------------------

def keywords(words: list[dict], limit: int = 10) -> list[str]:
    counts: dict[str, int] = {}
    for w in words:
        token = re.sub(r"[^a-z0-9']", "", w["w"].lower())
        if len(token) > 3 and token not in STOPWORDS:
            counts[token] = counts.get(token, 0) + 1
    return [k for k, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:limit]


def spoken_text(words: list[dict]) -> str:
    text = " ".join(w["w"] for w in words)
    return re.sub(r"\s+", " ", text).strip()


def first_sentence(text: str) -> str:
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    return (match.group(1) if match else text).strip()


def tidy_title(text: str) -> str:
    """Drop the joining words a fragment starts with, then capitalise."""
    text = clean(text)
    if text[:1].isupper():
        return text  # already a proper sentence start, leave the wording alone
    words = text.split()
    lead = {
        "and", "but", "that", "which", "of", "to", "the", "a", "an", "in", "on", "for",
        "with", "between", "from", "as", "when", "while", "because", "so", "it", "its",
        "he", "she", "they", "we", "i", "there", "this", "these", "those", "is", "was",
        "were", "are", "then", "than", "now", "here",
    }
    while len(words) > 3 and words[0].lower().strip(",.;:") in lead:
        words.pop(0)
    text = " ".join(words).strip(" ,.;:-")
    return (text[:1].upper() + text[1:]) if text else text


def best_sentence(text: str) -> str:
    """Prefer a complete sentence, so titles don't start mid-thought."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    for candidate in sentences:
        length = len(candidate.split())
        if candidate[:1].isupper() and 4 <= length <= 16 and candidate.endswith((".", "!", "?")):
            return candidate
    # Whisper lowercases a lot of speech, so accept a well-formed sentence of any case.
    for candidate in sentences:
        if 4 <= len(candidate.split()) <= 16 and candidate.endswith((".", "!", "?")):
            return candidate
    return sentences[0] if sentences else ""


def clip_words(text: str, max_chars: int = 70, max_words: int = 10) -> str:
    """Trim at a word boundary, never mid-word, and never end on a dangling word."""
    words = clean(text).split()
    kept: list[str] = []
    for word in words[:max_words]:
        candidate = " ".join(kept + [word])
        if len(candidate) > max_chars:
            break
        kept.append(word)
    trailing = {
        "a", "an", "the", "and", "or", "but", "that", "of", "to", "in", "on", "at",
        "for", "with", "is", "are", "was", "were", "be", "been", "it", "this", "these",
        "those", "my", "your", "his", "her", "their", "our", "i", "you", "we", "they",
        "he", "she", "as", "than", "then", "so", "if", "from", "by", "me", "us", "them",
        "him", "there", "have", "has", "had", "will", "would", "can", "could",
    }
    while len(kept) > 4 and kept[-1].lower().strip(",.;:") in trailing:
        kept.pop()
    return " ".join(kept).rstrip(" ,.;:-")


def template_metadata(words: list[dict], kws: list[str], cfg: dict) -> dict:
    text = spoken_text(words)
    sentence = tidy_title(best_sentence(text))
    hook_words = clean(sentence).split()[:6]
    hook = " ".join(hook_words).rstrip(",.;:!?")

    if sentence:
        title = clip_words(sentence)
    elif kws:
        title = f"{kws[0].title()} — you have to see this"
    else:
        title = "Wait for it"
    suffix = cfg.get("title_suffix", "")
    if suffix and suffix.lower() not in title.lower():
        title = f"{title} {suffix}".strip()
    title = title[:100]

    hashtags = " ".join(f"#{k.replace(chr(39), '')}" for k in kws[:3]) if kws else ""
    parts = []
    if hook:
        parts.append(hook.upper() if len(hook) < 60 else hook)
    if text:
        parts.append(first_sentence(text)[:220])
    if hashtags:
        parts.append(hashtags)
    parts.append("#Shorts")
    if cfg.get("credit"):
        parts.append(str(cfg["credit"]))
    description = "\n\n".join(parts)[:4900]

    tags = list(dict.fromkeys(kws + [str(t) for t in cfg.get("tags", [])]))[:15]
    return {"title": title, "description": description, "hook": hook, "tags": tags}


def llm_metadata(words: list[dict], kws: list[str], cfg: dict) -> dict | None:
    """Optional. Uses Gemini's free tier if GEMINI_API_KEY is set, else returns None."""
    key = os.environ.get("GEMINI_API_KEY")
    text = spoken_text(words)
    if not key or not text:
        return None
    model = cfg.get("llm_model") or "gemini-2.5-flash"
    prompt = (
        "You write YouTube Shorts packaging. Given a transcript, reply with JSON only: "
        '{"hook": "...", "title": "...", "description": "..."}. '
        "hook = 3-6 punchy words shown on screen at the start, no punctuation, under 40 chars. "
        "title = under 70 chars, curiosity-driven, no clickbait lies, no hashtags. "
        "description = 2 short sentences plus 3 hashtags. "
        f"Transcript:\n{text[:1500]}"
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.9, "responseMimeType": "application/json"},
    }).encode()
    request = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
        data=body, headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.load(response)
        payload = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
    except Exception as exc:  # network, quota, bad JSON: all non-fatal
        log(f"  wording model unavailable ({exc.__class__.__name__}), using built-in wording")
        return None

    out = {}
    if isinstance(payload.get("hook"), str):
        out["hook"] = clean(payload["hook"])[:48]
    if isinstance(payload.get("title"), str):
        out["title"] = clean(payload["title"])[:95]
    if isinstance(payload.get("description"), str):
        out["description"] = payload["description"][:4800]
    return out or None


# --------------------------------------------------------------------------
# schedule + upload
# --------------------------------------------------------------------------

def next_slots(cfg: dict, count: int) -> list[datetime]:
    tz = ZoneInfo(cfg.get("timezone", "UTC"))
    now = datetime.now(tz)
    earliest = now + timedelta(seconds=180)
    slots: list[datetime] = []
    day = 0
    while len(slots) < count and day < 14:
        day_date = now.date() + timedelta(days=day)
        for spec in cfg.get("publish_times", []):
            hh, mm = (int(x) for x in str(spec).split(":"))
            slot = datetime.combine(day_date, time(hh, mm), tzinfo=tz)
            if slot > earliest:
                slots.append(slot)
        day += 1
    return slots[:count]


def upload(path: Path, meta: dict, publish_at: datetime | None, cfg: dict) -> str:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    missing = [k for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN") if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            "Missing YouTube credentials: " + ", ".join(missing) +
            "\nRun setup_auth.py once and add those three values as repository secrets."
        )

    credentials = Credentials(
        token=None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri=RTL,
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=[SCOPE],
    )
    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)

    status = {"privacyStatus": "private", "selfDeclaredMadeForKids": False}
    if publish_at is not None:
        status["publishAt"] = publish_at.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

    body = {
        "snippet": {
            "title": meta["title"],
            "description": meta["description"],
            "tags": meta["tags"],
            "categoryId": str(cfg.get("category_id", "24")),
        },
        "status": status,
    }
    media = MediaFileUpload(str(path), chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        progress, response = request.next_chunk()
        if progress:
            log(f"  uploading… {int(progress.progress() * 100)}%")
    return response["id"]


# --------------------------------------------------------------------------
# one short
# --------------------------------------------------------------------------

def build_one(cfg: dict, files: list[Path], used: set, rng: random.Random, workdir: Path,
              dry_run: bool, publish_at: datetime | None,
              used_files: set | None = None) -> dict:
    source, start = pick_source(files, used, rng, used_files)
    if used_files is not None:
        used_files.add(source.name)  # never build two Shorts from one file in a run
    log(f"source: {source.name} @ {start:.1f}s")

    raw = workdir / "raw.mp4"
    longest = max(float(cfg["clip_seconds"]) * 1.6, float(cfg["clip_seconds"]) + 6)
    cut(source, start, longest, raw)
    duration = probe(raw)[0]

    model = cfg.get("whisper_model", "base")
    log("  transcribing")
    words = transcribe(raw, model)
    ratio = speech_ratio(words, duration)
    confidence = speech_confidence(words)
    log(f"  {len(words)} words, speech ratio {ratio:.2f}, clarity {confidence:.2f}")

    thin = (
        not words
        or ratio < float(cfg["min_speech_ratio"])
        or confidence < float(cfg.get("min_speech_confidence", 0.55))
    )
    if thin:
        # Archive footage has dead air and damaged audio, so try another window
        # and keep whichever one transcribes clearly.
        log("  thin or unclear audio here, trying another window")
        shift = rng.uniform(30, 120)
        if start + shift + longest < probe(source)[0]:
            cut(source, start + shift, longest, raw)
            start += shift
            duration = probe(raw)[0]
            alt = transcribe(raw, model)
            alt_ratio = speech_ratio(alt, duration)
            alt_confidence = speech_confidence(alt)
            log(f"  second window: {len(alt)} words, speech ratio {alt_ratio:.2f}, "
                f"clarity {alt_confidence:.2f}")
            if alt and (alt_ratio >= ratio or alt_confidence > confidence):
                words = alt

    ratio = speech_ratio(words, duration)
    if not words or ratio < float(cfg["min_speech_ratio"]):
        raise NoSpeech(f"no clear speech in {source.name} around {start:.0f}s")

    offset, end = speech_bounds(words, duration)
    offset = min(offset, max(0.0, duration - 6))
    length = min(float(cfg["clip_seconds"]), max(5.0, end - offset))
    clip = workdir / "clip.mp4"
    cut(raw, offset, length, clip)
    words = [
        {"w": w["w"], "s": w["s"] - offset, "e": w["e"] - offset}
        for w in words if w["e"] > offset and w["s"] < offset + length
    ]

    kws = keywords(words)
    meta = template_metadata(words, kws, cfg)
    better = llm_metadata(words, kws, cfg)
    if better:
        meta.update({k: v for k, v in better.items() if v})

    credit = source_credit(source) or str(cfg.get("credit") or "")
    if credit and credit[:40] not in meta["description"]:
        meta["description"] = (meta["description"].rstrip() + "\n\n" + credit)[:4900]

    ass = build_ass(words, meta.get("hook", ""), cfg)
    log("  rendering")
    final = render(clip, ass, cfg, workdir)

    width, height = probe(final)[1], probe(final)[2]
    video_id = ""
    if dry_run:
        log("  dry run: not uploading")
    else:
        video_id = upload(final, meta, publish_at, cfg)
        log(f"  uploaded {video_id}")

    return {
        "file": source.name,
        "start": round(start, 1),
        "video_id": video_id,
        "title": meta["title"],
        "hook": meta.get("hook", ""),
        "credit": credit,
        "scheduled_for": publish_at.isoformat() if publish_at else None,
        "size": f"{width}x{height}",
        "at": datetime.now().isoformat(timespec="seconds"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and schedule YouTube Shorts from your own clips.")
    parser.add_argument("--config", default=str(ROOT / "config.yml"))
    parser.add_argument("--count", type=int, default=None, help="how many Shorts this run")
    parser.add_argument("--dry-run", action="store_true", help="render locally, upload nothing")
    parser.add_argument("--out", default=str(ROOT / "output"), help="where dry-run files are kept")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    count = args.count or int(cfg["posts_per_day"])
    rng = random.Random(args.seed)

    state_path = ROOT / "state" / "used.json"
    state = load_state(state_path)
    used = recently_used(state, int(cfg["reuse_cooldown_days"]))

    slots = next_slots(cfg, count)
    log(f"{count} Short(s) this run; slots: {', '.join(s.isoformat() for s in slots) or 'none left'}")

    try:
        import feed
    except ImportError:
        log("feed.py not found; using whatever is already in the sources folder")
    else:
        try:
            feed.ensure_footage(cfg, count, args.seed)
        except Exception as exc:
            log(f"footage feed unavailable ({exc.__class__.__name__}: {exc}); using the sources folder")

    files = gather_sources(cfg)
    results = []
    used_files: set[str] = set()

    attempts = max(1, int(cfg.get("source_attempts", 3)))

    for index in range(count):
        record = None
        workdir = Path(tempfile.mkdtemp(prefix=f"short{index}-"))
        for take in range(attempts):
            log(f"--- Short {index + 1}/{count}" + (f" (take {take + 1})" if take else "") + " ---")
            try:
                record = build_one(cfg, files, used, rng, workdir, args.dry_run,
                                   slots[index] if index < len(slots) else None, used_files)
                break
            except NoSpeech as exc:
                log(f"  skipping this footage: {exc}")
                continue
            except Exception as exc:
                log(f"  failed: {exc.__class__.__name__}: {exc}")
                break
        if record is None:
            log("  giving up on this slot for now")
            continue

        if args.dry_run:
            out_dir = Path(args.out)
            out_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(workdir / "short_final.mp4", out_dir / f"short{index + 1}.mp4")
            shutil.copy2(workdir / "captions.ass", out_dir / f"short{index + 1}.ass")
        else:
            used.add((record["file"], record["start"]))
            state["clips"].append(record)
            save_state(state_path, state)
        used_files.add(record.get("file", ""))
        results.append(record)
        log(f"  done: {record['title'][:60]}")

    log(f"finished {len(results)}/{count}")
    print(json.dumps(results, indent=2, ensure_ascii=False))
    if not results:
        sys.exit(1)


if __name__ == "__main__":
    main()
