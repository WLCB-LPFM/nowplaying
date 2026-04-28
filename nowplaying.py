#!/usr/bin/env python3
"""
WLCB Now Playing processor.

Polls PlayIt Live + autopo.st, picks the best source for the current show
context, and emits a unified now.json to a public GitHub repo. The website
reads it via raw.githubusercontent.com.

Source selection:
  - During automated music blocks (schedule.automated == true):
      PlayIt Live is authoritative — it drives the playout log.
      autopo.st is used only to enrich with artwork.
  - During live shows (schedule.automated == false or absent):
      autopo.st is authoritative — it fingerprints the actual audio output,
      including songs played by the live host that PlayIt Live never sees.
      PlayIt Live's currentItem is unreliable (empty / stale / showing a bed).
  - Fallback: schedule-only (show name, no track info).
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests

# ---------- Config ----------
PLAYIT_BASE_URL   = os.environ.get("PLAYIT_BASE_URL",  "https://10.101.0.101:25433")
PLAYIT_API_KEY    = os.environ.get("PLAYIT_API_KEY",   "")
PLAYIT_VERIFY_TLS = os.environ.get("PLAYIT_VERIFY_TLS", "0") == "1"
AUTOPOST_URL      = os.environ.get(
    "AUTOPOST_URL",
    "https://widgets.autopo.st/fingerprinting/public/WLCB/nowplaying.json",
)
REPO_DIR          = Path(os.environ.get("REPO_DIR", "/home/nowplaying/nowplaying"))
OUTPUT_FILE       = REPO_DIR / "now.json"
POLL_INTERVAL_S   = int(os.environ.get("POLL_INTERVAL_S", "20"))
HTTP_TIMEOUT_S    = 4
SCHEDULE_PATH     = Path(os.environ.get("SCHEDULE_PATH", "/home/nowplaying/nowplaying/schedule.json"))

# Suppress urllib3 self-signed cert warnings (cert is self-signed on PlayIt Live)
if not PLAYIT_VERIFY_TLS:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[{now_iso()}] {msg}", flush=True)


# ---------- PlayIt Live ----------
def _playit_get(path: str):
    """GET against PlayIt Live API with Bearer auth. Returns parsed JSON or None on failure."""
    if not PLAYIT_API_KEY:
        return None
    try:
        url = urljoin(PLAYIT_BASE_URL.rstrip("/") + "/", path.lstrip("/"))
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {PLAYIT_API_KEY}", "Accept": "application/json"},
            timeout=HTTP_TIMEOUT_S,
            verify=PLAYIT_VERIFY_TLS,
        )
        if r.status_code == 200:
            return r.json()
        log(f"PlayIt {path} -> HTTP {r.status_code}")
    except requests.RequestException as e:
        log(f"PlayIt {path} -> {e.__class__.__name__}")
    return None


def get_playit_track():
    """
    Fetch the currently-playing item from PlayIt Live.

    Returns a dict with title/artist/started_at/duration_seconds, or None if:
      - PlayIt Live is unreachable
      - Current item isn't a music track (jingle, advert, voice track, etc.)
      - Item lacks a trackGuid (live mic, aux input, etc.)
    """
    current = _playit_get("/api/control/liveAssist/playoutLog/currentItem")
    if not isinstance(current, dict):
        return None

    # Only handle music tracks. Other types (jingle, advertBlock, voiceTrack, auxInput,
    # remoteUrl, breakNote, hookSequence) won't have meaningful artist/title info.
    if current.get("type") != "track":
        return None

    track_guid = current.get("trackGuid")
    if not track_guid:
        return None

    # Fetch full track metadata for separate artist/title fields
    track = _playit_get(f"/api/control/tracks/{track_guid}")
    if not isinstance(track, dict):
        return None

    artist = (track.get("artist") or "").strip()
    title  = (track.get("title")  or "").strip()
    if not title:
        return None

    duration = current.get("duration") or current.get("fullDuration") or track.get("activeDuration")
    return {
        "title":            title,
        "artist":           artist,
        "album":            track.get("album", "") or "",
        "artwork_url":      "",  # PlayIt Live doesn't expose artwork; enriched from autopo.st
        "started_at":       current.get("startTime") or now_iso(),
        "duration_seconds": int(duration) if duration else None,
        "year":             track.get("year") or current.get("year") or "",
    }


# ---------- autopo.st ----------
def get_autopost_track():
    """
    Fetch current track from autopo.st fingerprinting service.
    Returns dict with title/artist/album/artwork_url/started_at/duration_seconds, or None.
    """
    try:
        r = requests.get(AUTOPOST_URL, timeout=HTTP_TIMEOUT_S)
        if r.status_code != 200:
            return None
        d = r.json()
    except (requests.RequestException, ValueError):
        return None

    title = (d.get("title") or d.get("track_title") or d.get("track_mix_title") or "").strip()
    artist = (d.get("artist") or d.get("track_mix_artist") or "").strip()
    if not title:
        return None

    # Validate freshness — drop if track started >duration+60s ago (we're behind autopo.st update)
    start_iso = d.get("start")
    try:
        started = datetime.fromisoformat(start_iso.replace("Z", "+00:00")) if start_iso else None
    except Exception:
        started = None
    try:
        length = int(d.get("track_length") or 0)
    except Exception:
        length = 0
    if started:
        age = (datetime.now(timezone.utc) - started).total_seconds()
        if age < -10 or age > (length or 300) + 60:
            return None

    return {
        "title":            title,
        "artist":           artist,
        "album":            d.get("album") or "",
        "artwork_url":      d.get("artwork_url") or "",
        "started_at":       start_iso or now_iso(),
        "duration_seconds": length or None,
    }


def _norm(s: str) -> str:
    """Lowercase, strip non-alphanumerics for fuzzy matching."""
    return "".join(c.lower() for c in (s or "") if c.isalnum())


def enrich_with_autopost(playit_track):
    """
    If PlayIt Live track matches autopo.st by artist+title (fuzzy), copy the artwork_url
    and album. PlayIt Live doesn't expose artwork directly, but autopo.st does.
    """
    if not playit_track:
        return playit_track
    ap = get_autopost_track()
    if not ap:
        return playit_track
    pl_key = _norm(playit_track["artist"]) + _norm(playit_track["title"])
    ap_key = _norm(ap["artist"]) + _norm(ap["title"])
    if pl_key and pl_key == ap_key:
        if ap.get("artwork_url"):
            playit_track["artwork_url"] = ap["artwork_url"]
        if ap.get("album") and not playit_track.get("album"):
            playit_track["album"] = ap["album"]
    return playit_track


# ---------- Schedule ----------
def get_scheduled_show():
    try:
        with open(SCHEDULE_PATH) as f:
            sched = json.load(f)
    except Exception:
        return None
    days = ["sunday","monday","tuesday","wednesday","thursday","friday","saturday"]
    now = datetime.now()
    js_day_idx = (now.weekday() + 1) % 7  # JS-style: Sunday=0..Saturday=6
    day = days[js_day_idx]
    mins = now.hour * 60 + now.minute
    for s in sched.get(day, []):
        sh, sm = [int(x) for x in s["start"].split(":")]
        eh, em = [int(x) for x in s["end"].split(":")]
        if sh*60+sm <= mins < eh*60+em:
            return {
                "name":      s["show"],
                "automated": bool(s.get("automated")),
                "starts":    s["start"],
                "ends":      s["end"],
                "genre":     s.get("genre"),
            }
    return None


# ---------- Compose payload ----------
def build_payload():
    show = get_scheduled_show()
    is_automated = bool(show and show.get("automated"))

    track  = None
    source = "fallback"

    if is_automated:
        # PlayIt Live is driving playout — it's authoritative
        pl = get_playit_track()
        if pl:
            track  = enrich_with_autopost(pl)
            source = "playitlive"
        else:
            # PlayIt Live unreachable — autopo.st as backup
            ap = get_autopost_track()
            if ap:
                track  = ap
                source = "autopost"
    else:
        # Live show — autopo.st is fingerprinting the actual audio output, only it knows
        ap = get_autopost_track()
        if ap:
            track  = ap
            source = "autopost"
        # Note: no PlayIt Live fallback during live shows; its data is unreliable

    return {
        "generated":         now_iso(),
        "source":            source,
        "show":              show,
        "track":             track,
        "next_poll_seconds": POLL_INTERVAL_S,
    }


# ---------- Output / git ----------
def load_last_payload():
    try:
        with open(OUTPUT_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def payload_changed(new, old) -> bool:
    """Compare meaningful fields, ignoring `generated` timestamp."""
    if old is None:
        return True
    keys = ("source", "show", "track")
    return any(new.get(k) != old.get(k) for k in keys)


def git_commit_and_push(message: str) -> bool:
    cwd = REPO_DIR
    try:
        subprocess.run(["git", "add", "now.json"], cwd=cwd, check=True, capture_output=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=cwd)
        if diff.returncode == 0:
            return False
        subprocess.run(["git", "commit", "-m", message], cwd=cwd, check=True, capture_output=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=cwd, check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError as e:
        log(f"git error: {e.stderr.decode(errors='ignore')}")
        return False


def write_and_publish(payload):
    last = load_last_payload()
    OUTPUT_FILE.write_text(json.dumps(payload, indent=2) + "\n")
    if payload_changed(payload, last):
        track = payload.get("track") or {}
        msg_track = f"{track.get('title','?')} - {track.get('artist','?')}" if track else "(no track)"
        msg = f"now: {payload.get('source')} | {msg_track}"
        if git_commit_and_push(msg):
            log(f"pushed: {msg}")
        else:
            log("change detected but git push skipped/failed")


# ---------- Main loop ----------
def main():
    log(f"starting nowplaying processor; poll={POLL_INTERVAL_S}s")
    while True:
        try:
            payload = build_payload()
            write_and_publish(payload)
        except Exception as e:
            log(f"unexpected error: {e!r}")
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    if "--once" in sys.argv:
        log("running single iteration (--once)")
        try:
            payload = build_payload()
            print(json.dumps(payload, indent=2))
            write_and_publish(payload)
        except Exception as e:
            log(f"error: {e!r}")
            sys.exit(1)
    else:
        main()
