#!/usr/bin/env python3
"""
Feed
====
Keeps `sources/` stocked, forever, with footage that is free to publish.

Three libraries are used. None of them wants an account, an API key or money:

  * Internet Archive  — millions of public-domain / Creative Commons films.
    Public-domain newsreels, educational films and classic TV are *narrated*,
    which is what makes captions and hooks possible at all.
  * Wikimedia Commons — CC BY, CC0 and public-domain video.
  * NASA              — public-domain space footage. Off by default: most of it
    has music but no speech, and the pipeline builds captions from speech.

Only items that declare a licence in their own metadata are accepted, and
non-commercial, no-derivatives and share-alike licences are refused by default,
because the channel is monetised and every Short is an edit.

Each download gets a `<file>.license.json` sidecar holding its source page,
licence and credit line. The pipeline reads that sidecar and puts the credit in
the video description, which is exactly what CC BY requires.

Run it on its own (`python feed.py --count 3`) or let pipeline.py call it.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
UA = "shorts-autopilot/1.0 (public-domain and Creative Commons footage fetcher)"
STATE_FILE = ROOT / "state" / "feed.json"
VIDEO_EXT = {".mp4", ".m4v", ".mov", ".webm", ".ogv", ".mkv", ".mpg", ".mpeg"}

# Public domain only by default. A self-declared 'CC BY' on a random upload is
# worth very little: the uploader may not own the footage at all. Public-domain
# marks in these vetted collections are the safest thing a machine can check.
DEFAULT_ALLOWED = ["public-domain", "cc0"]

# Curated public-domain collections that contain speech. A generic subject search
# pulls in junk, so every default query names a collection.
DEFAULT_IA_QUERIES = [
    "collection:(classic_tv)",
    "collection:(prelinger)",
    "collection:(universal_newsreels)",
    "collection:(feature_films)",
]

DEFAULT_COMMONS_QUERIES = [
    "interview",
    "documentary",
    "press conference",
    "speech",
    "news report",
]

DEFAULT_NASA_QUERIES = ["apollo", "space station"]

# Smallest derivative first: these are 10-80 MB, which keeps a run fast.
IA_FILE_PRIORITY = ("h.264 ia", "h.264", "512kb mpeg4", "ogg video", "webm", "matroska", "mpeg4")


class Skip(Exception):
    """A candidate that cannot be used; the next one is tried."""


def log(msg: str) -> None:
    print(f"[feed] {msg}", flush=True)


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

def http_json(url: str, timeout: int = 45, tries: int = 3) -> dict:
    last: Exception | None = None
    for attempt in range(tries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except Exception as exc:  # network hiccup, rate limit, truncated body
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"request failed ({last.__class__.__name__ if last else 'unknown'}): {url}")


def ffmpeg_bin() -> str:
    if os.environ.get("FFMPEG_BIN"):
        return os.environ["FFMPEG_BIN"]
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def probe_media(path: Path) -> tuple[float, bool]:
    """(duration_seconds, has_audio_track) using ffmpeg alone."""
    text = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-i", str(path)], capture_output=True, text=True
    ).stderr
    match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", text)
    duration = 0.0
    if match:
        duration = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
    return duration, "Audio:" in text


def strip_html(text) -> str:
    text = re.sub(r"<[^>]+>", " ", str(text or ""))
    text = text.replace("&amp;", "&").replace("&quot;", '"').replace("&#039;", "'")
    return re.sub(r"\s+", " ", text).strip()


def safe_name(text, limit: int = 40) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-._")
    return cleaned[:limit].strip("-._") or "clip"


def first(value) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value if v)
    return str(value or "")


def licence_kind(url: str, extra: str = "") -> str:
    """public-domain / cc0 / cc-by / cc-by-sa / nc / unknown."""
    text = f"{url} {extra}".lower()
    if any(k in text for k in ("by-nc", "by-nd", "nc-sa", "noncommercial", "non-commercial")):
        return "nc"
    if any(k in text for k in ("publicdomain", "public domain", "public_domain", "/zero/", "cc0")):
        return "public-domain"
    if "by-sa" in text or "share-alike" in text or "share alike" in text:
        return "cc-by-sa"
    if re.search(r"licenses?/by(/|$)", text) or "attribution" in text or "cc by" in text:
        return "cc-by"
    return "unknown"


def licence_label(kind: str, url: str) -> str:
    if kind == "public-domain":
        return "public domain"
    match = re.search(r"licenses/([a-z\-]+)/([\d.]+)", url or "", re.I)
    if match:
        return f"CC {match.group(1).upper()} {match.group(2)}"
    return {"cc-by": "CC BY", "cc0": "CC0", "cc-by-sa": "CC BY-SA"}.get(kind, "free licence")


def credit_line(candidate: dict) -> str:
    author = (candidate.get("author") or "unknown creator").strip()
    title = (candidate.get("title") or "").strip()
    line = f"Footage: {author}"
    if title and title.lower() not in author.lower():
        line += f" — {title[:90]}"
    line += f" ({candidate.get('licence') or 'free licence'})"
    if candidate.get("page"):
        line += f" {candidate['page']}"
    return line[:400]


# --------------------------------------------------------------------------
# Internet Archive
# --------------------------------------------------------------------------

_META_CACHE: dict[str, dict] = {}


def ia_item(ident: str) -> dict:
    """Full metadata document for one item, cached for this run."""
    if ident not in _META_CACHE:
        url = "https://archive.org/metadata/" + urllib.parse.quote(ident)
        _META_CACHE[ident] = http_json(url, tries=2)
    return _META_CACHE[ident]


def ia_search(query: str, rows: int, rng: random.Random) -> list[dict]:
    fields = "&fl%5B%5D=identifier&fl%5B%5D=title&fl%5B%5D=licenseurl"
    base = (
        "https://archive.org/advancedsearch.php?q="
        + urllib.parse.quote(query)
        + fields
        + f"&rows={rows}&output=json"
    )
    for suffix in (
        "&page=1&sort%5B%5D=random",
        f"&page={rng.randint(1, 12)}&sort%5B%5D=downloads+desc",
    ):
        try:
            response = http_json(base + suffix, tries=1).get("response") or {}
        except Exception:
            continue
        docs = response.get("docs") or []
        if docs:
            return docs
    return []


def ia_candidates(query: str, limit: int, max_bytes: int, allowed: set, rng: random.Random):
    if allowed & {"cc-by", "cc-by-sa", "nc"}:
        licence_clause = "licenseurl:(*)"
    else:
        licence_clause = "(licenseurl:*publicdomain* OR licenseurl:*zero*)"
    full = f"mediatype:(movies) AND {licence_clause} AND {query}"
    for doc in ia_search(full, limit, rng):
        ident = str(doc.get("identifier") or "")
        if not ident:
            continue
        try:
            item = ia_item(ident)
        except Exception:
            continue

        meta = item.get("metadata") or {}
        if str(meta.get("sound") or "").lower() == "silent":
            continue  # silent film: nothing to caption

        licence_url = first(meta.get("licenseurl")) or first(doc.get("licenseurl"))
        kind = licence_kind(licence_url, first(meta.get("rights")))
        if kind not in allowed:
            continue

        chosen = None
        files = item.get("files") or []
        for wanted in IA_FILE_PRIORITY:
            for entry in files:
                if str(entry.get("format") or "").lower() != wanted:
                    continue
                try:
                    size = int(entry.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0
                name = str(entry.get("name") or "")
                if not size or size > max_bytes:
                    continue
                if Path(name).suffix.lower() not in VIDEO_EXT:
                    continue
                chosen = {"name": name, "size": size}
                break
            if chosen:
                break
        if not chosen:
            continue

        ext = Path(chosen["name"]).suffix.lower()
        yield {
            "uid": f"internet_archive:{ident}:{chosen['name']}",
            "provider": "internet_archive",
            "title": strip_html(first(meta.get("title"))) or ident,
            "author": strip_html(first(meta.get("creator"))) or "Internet Archive",
            "licence": licence_label(kind, licence_url),
            "page": "https://archive.org/details/" + ident,
            "url": (
                "https://archive.org/download/"
                + urllib.parse.quote(ident)
                + "/"
                + urllib.parse.quote(chosen["name"])
            ),
            "ext": ext,
            "bytes": chosen["size"],
            "filename": f"ia-{safe_name(ident)}{ext}",
        }


# --------------------------------------------------------------------------
# Wikimedia Commons
# --------------------------------------------------------------------------

def commons_candidates(query: str, limit: int, max_bytes: int, allowed: set, rng: random.Random):
    search = (
        "https://commons.wikimedia.org/w/api.php?action=query&format=json&list=search"
        "&srsearch=" + urllib.parse.quote(f"filetype:video {query}")
        + f"&srnamespace=6&srlimit={limit}"
    )
    try:
        hits = (http_json(search).get("query") or {}).get("search") or []
    except Exception:
        return
    titles = [h["title"] for h in hits if h.get("title")]
    if not titles:
        return

    info_url = (
        "https://commons.wikimedia.org/w/api.php?action=query&format=json"
        "&prop=imageinfo&iiprop=url%7Csize%7Cmime%7Cextmetadata&titles="
        + urllib.parse.quote("|".join(titles[:20]))
    )
    try:
        pages = (http_json(info_url).get("query") or {}).get("pages") or {}
    except Exception:
        return

    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        url = str(info.get("url") or "").split("?")[0]
        if not url:
            continue
        ext = Path(url).suffix.lower()
        if ext not in VIDEO_EXT:
            continue
        try:
            size = int(info.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if not size or size > max_bytes:
            continue

        fields = info.get("extmetadata") or {}
        short = strip_html((fields.get("LicenseShortName") or {}).get("value", ""))
        licence_url = strip_html((fields.get("LicenseUrl") or {}).get("value", ""))
        kind = licence_kind(licence_url, short)
        if kind not in allowed:
            continue

        page_title = str(page.get("title") or Path(url).stem)
        yield {
            "uid": "wikimedia:" + page_title,
            "provider": "wikimedia",
            "title": strip_html((fields.get("ObjectName") or {}).get("value", "")) or Path(url).stem,
            "author": strip_html((fields.get("Artist") or {}).get("value", ""))[:120]
                      or "Wikimedia Commons",
            "licence": short or licence_label(kind, licence_url),
            "page": "https://commons.wikimedia.org/wiki/"
                    + urllib.parse.quote(page_title.replace(" ", "_")),
            "url": url,
            "ext": ext,
            "bytes": size,
            "filename": f"wc-{safe_name(page.get('pageid') or Path(url).stem)}{ext}",
        }


# --------------------------------------------------------------------------
# NASA
# --------------------------------------------------------------------------

def nasa_candidates(query: str, limit: int, max_bytes: int, allowed: set, rng: random.Random):
    if not ({"public-domain", "cc0"} & allowed):
        return
    search = (
        "https://images-api.nasa.gov/search?media_type=video"
        f"&page_size={limit}&q=" + urllib.parse.quote(query)
    )
    try:
        items = (http_json(search).get("collection") or {}).get("items") or []
    except Exception:
        return

    for item in items:
        data = (item.get("data") or [{}])[0]
        nasa_id = str(data.get("nasa_id") or "")
        href = str(item.get("href") or "")
        if not nasa_id or not href:
            continue
        try:
            assets = http_json(href)
        except Exception:
            continue
        chosen = ""
        for wanted in ("~small.mp4", "~mobile.mp4", "orig.mp4", ".mp4"):
            for asset in assets:
                if str(asset).lower().endswith(wanted):
                    chosen = str(asset)
                    break
            if chosen:
                break
        if not chosen:
            continue
        yield {
            "uid": "nasa:" + nasa_id,
            "provider": "nasa",
            "title": strip_html(data.get("title")) or nasa_id,
            "author": "NASA",
            "licence": "public domain (NASA)",
            "page": "https://images.nasa.gov/details-" + urllib.parse.quote(nasa_id),
            "url": urllib.parse.quote(chosen, safe=":/?&=~"),
            "ext": ".mp4",
            "bytes": 0,
            "filename": f"nasa-{safe_name(nasa_id)}.mp4",
        }


PROVIDERS = {
    "internet_archive": ia_candidates,
    "wikimedia": commons_candidates,
    "nasa": nasa_candidates,
}


# --------------------------------------------------------------------------
# download + licence sidecar
# --------------------------------------------------------------------------

def download(url: str, dest: Path, max_bytes: int, timeout: int = 300) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        declared = int(response.headers.get("Content-Length") or 0)
        if declared and declared > max_bytes:
            raise Skip(f"{declared / 1e6:.0f} MB is over the size cap")
        written = 0
        with open(dest, "wb") as handle:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise Skip("larger than the size cap")
                handle.write(chunk)
    if written < 10_000:
        raise Skip("download was empty")
    return written


def sources_dir(cfg: dict) -> Path:
    folder = Path((cfg.get("source") or {}).get("dir", "sources"))
    return folder if folder.is_absolute() else ROOT / folder


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("feed history unreadable, starting a fresh one")
    return {"seen": [], "log": []}


def save_state(state: dict) -> None:
    state["seen"] = list(dict.fromkeys(state.get("seen") or []))[-5000:]
    state["log"] = (state.get("log") or [])[-500:]
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def fetch(candidate: dict, folder: Path, max_bytes: int, min_seconds: float) -> Path:
    part = folder / (candidate["filename"] + ".part")
    try:
        size = download(candidate["url"], part, max_bytes)
        duration, has_audio = probe_media(part)
        if duration < min_seconds:
            raise Skip(f"only {duration:.0f}s long")
        if not has_audio:
            raise Skip("no audio track to caption")

        final = folder / candidate["filename"]
        part.replace(final)
        (folder / (final.name + ".license.json")).write_text(
            json.dumps(
                {
                    "uid": candidate["uid"],
                    "provider": candidate["provider"],
                    "title": candidate["title"],
                    "author": candidate["author"],
                    "licence": candidate["licence"],
                    "page": candidate["page"],
                    "url": candidate["url"],
                    "credit": credit_line(candidate),
                    "seconds": round(duration, 1),
                    "fetched_at": datetime.now().isoformat(timespec="seconds"),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        log(f"  + {final.name}  {size / 1e6:.0f} MB  {duration:.0f}s  [{candidate['licence']}]")
        return final
    finally:
        part.unlink(missing_ok=True)


def prune(cfg: dict, folder: Path) -> None:
    """Delete downloads once they have had time to be used, so the disk never fills."""
    hours = float((cfg.get("feed") or {}).get("keep_hours", 6))
    cutoff = datetime.now().timestamp() - hours * 3600
    for sidecar in folder.glob("*.license.json"):
        try:
            info = json.loads(sidecar.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(str(info.get("fetched_at"))).timestamp()
        except (OSError, ValueError, json.JSONDecodeError):
            fetched_at = 0
        if fetched_at >= cutoff:
            continue
        media = folder / sidecar.name[: -len(".license.json")]
        if media.exists():
            media.unlink()
            log(f"  - pruned {media.name}")
        sidecar.unlink(missing_ok=True)


def _tasks(cfg: dict) -> list[tuple[str, str]]:
    feed = cfg.get("feed") or {}
    default_queries = {
        "internet_archive": feed.get("queries") or DEFAULT_IA_QUERIES,
        "wikimedia": feed.get("wikimedia_queries") or DEFAULT_COMMONS_QUERIES,
        "nasa": feed.get("nasa_queries") or DEFAULT_NASA_QUERIES,
    }
    tasks: list[tuple[str, str]] = []
    for provider in feed.get("providers") or ["internet_archive", "wikimedia"]:
        if provider not in PROVIDERS:
            continue
        tasks.extend((provider, str(q)) for q in default_queries[provider])
    return tasks


def ensure_footage(cfg: dict, needed: int, seed: int | None = None) -> int:
    """Download up to `needed` fresh, licence-cleared clips. Returns how many arrived."""
    feed = cfg.get("feed") or {}
    if not feed.get("enabled", True) or needed <= 0:
        return 0

    tasks = _tasks(cfg)
    if not tasks:
        return 0

    folder = sources_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    prune(cfg, folder)

    state = load_state()
    seen = set(state.get("seen") or [])
    max_bytes = int(float(feed.get("max_download_mb", 150)) * 1_000_000)
    allowed = set(feed.get("allowed_licenses") or DEFAULT_ALLOWED)
    min_seconds = float(feed.get("min_source_seconds", 60))
    rng = random.Random(seed if seed is not None else int(time.time()))
    rng.shuffle(tasks)

    fetched = 0
    for provider, query in tasks:
        if fetched >= needed:
            break
        log(f"searching {provider}: {query}")
        try:
            candidates = list(itertools.islice(
                PROVIDERS[provider](query, 12, max_bytes, allowed, rng), 12
            ))
        except Exception as exc:
            log(f"  search failed ({exc.__class__.__name__})")
            continue

        for candidate in candidates:
            if fetched >= needed:
                break
            if candidate["uid"] in seen:
                continue
            seen.add(candidate["uid"])
            try:
                fetch(candidate, folder, max_bytes, min_seconds)
            except Skip as exc:
                log(f"  skip {candidate['title'][:44]}: {exc}")
                continue
            except Exception as exc:
                log(f"  skip {candidate['title'][:44]}: {exc.__class__.__name__}")
                continue
            fetched += 1
            state.setdefault("log", []).append(
                {
                    "uid": candidate["uid"],
                    "title": candidate["title"],
                    "licence": candidate["licence"],
                    "page": candidate["page"],
                    "credit": credit_line(candidate),
                    "at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            state["seen"] = list(seen)
            save_state(state)

    state["seen"] = list(seen)
    save_state(state)
    log(f"{fetched}/{needed} new clip(s) fetched")
    return fetched


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch free-to-publish footage into sources/.")
    parser.add_argument("--config", default=str(ROOT / "config.yml"))
    parser.add_argument("--count", type=int, default=2, help="how many clips to fetch")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="show what it would fetch")
    args = parser.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}

    if args.dry_run:
        allowed = set((cfg.get("feed") or {}).get("allowed_licenses") or DEFAULT_ALLOWED)
        rng = random.Random(args.seed or int(time.time()))
        for provider, query in _tasks(cfg):
            log(f"{provider}: {query}")
            try:
                for candidate in itertools.islice(
                    PROVIDERS[provider](query, 3, 150_000_000, allowed, rng), 3
                ):
                    log(f"  would fetch {candidate['title'][:60]} [{candidate['licence']}]")
            except Exception as exc:
                log(f"  failed ({exc.__class__.__name__})")
            break
        return

    if ensure_footage(cfg, args.count, args.seed) == 0:
        log("nothing fetched; check the connection or the queries in config.yml")
        sys.exit(1)


if __name__ == "__main__":
    main()
