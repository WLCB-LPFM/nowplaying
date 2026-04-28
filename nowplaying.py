#!/usr/bin/env python3
"""
WLCB Now Playing processor.

Polls PlayIt Live for current state, decides whether the station is in
automation (music block) or live programming, and emits a unified now.json
to a public GitHub repo. The website reads it via raw.githubusercontent.com.

Source priority:
  1. PlayIt Live API           - authoritative for current track during automation
                                 AND identifies which "cart" / show is active
  2. autopo.st fingerprinting  - fallback for live programming where PlayIt Live
                                 doesn't know the song (mic + outboard sources)
  3. Schedule-only fallback    - when both fail, emit show name from schedule
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
PLAYIT_BASE_URL  = os.environ.get("PLAYIT_BASE_URL",  "http://10.101.0.101")
PLAYIT_API_KEY   = os.environ.get("PLAYIT_API_KEY",   "")
AUTOPOST_URL     = os.environ.get(
    "AUTOPOST_URL",
    "https://widgets.autopo.st/fingerprinting/public/WLCB/nowplaying.json",
)
REPO_DIR         = Path(os.environ.get("REPO_DIR", "/home/nowplaying/nowplaying"))
OUTPUT_FILE      = REPO_DIR / "now.json"
POLL_INTERVAL_S  = int(os.environ.get("POLL_INTERVAL_S", "20"))
HTTP_TIMEOUT_S   = 4
SCHEDULE_PATH    = Path(os.environ.get("SCHEDULE_PATH", "/home/nowplaying/schedule.json"))


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[{now_iso()}] {msg}", flush=True)


# ---------- PlayIt Live ----------
def _playit_get(path: str):
    """GET against PlayIt Live API. Returns parsed JSON or None on any failure."""
    if not PLAYIT_API_KEY:
        return None
    try:
        url = urljoin(PLAYIT_BASE_URL.rstrip("/") + "/", path.lstrip("/"))
        r = requests.get(
            url,
            headers={"X-API-Key": PLAYIT_API_KEY, "Accept": "application/json"},
            timeout=HTTP_TIMEOUT_S,
        )
        if r.status_code == 200:
            return r.json()
        log(f"PlayIt {path} -> HTTP {r.status_code}")
    except requests.RequestException as e:
        log(f"PlayIt {path} -> {e.__class__.__name__}")
    return None


def get_playit_track():
    """
    Try a couple of well-known PlayIt Live endpoints to find the current track.
    Returns dict with title/artist/started_at/duration_seconds, or None.

    Update this once you confirm the exact endpoint shape with curl
    from this VM (after firewall is open).
    """
    candidates = [
        "/api/v2/system/now-playing",
        "/api/v2/now-playing",
        "/api/v2/track-history?limit=1",
    ]
    for path in candidates:
        data = _playit_get(path)
        if not data:
            continue
        track = data.get("track") if isinstance(data, dict) else None
        if track is None and isinstance(data, dict):
            if "title" in data or "trackTitle" in data or "name" in data:
                track = data
        if track is None and isinstance(data, list) and data:
            track = data[0]
        if not isinstance(track, dict):
            continue

        title  = track.get("title") or track.get("trackTitle") or track.get("name")
        artist = track.get("artist") or track.get("trackArtist") or track.get("artistName")
        if not title:
            continue
        return {
            "title":            title,
            "artist":           artist or "",
            "album":            track.get("album") or track.get("albumName") or "",
            "artwork_url":      track.get("artworkUrl") or track.get("artwork_url") or "",
            "started_at":       track.get("startTime") or track.get("startedAt") or now_iso(),
            "duration_seconds": int(track.get("duration") or track.get("durationSeconds") or 0) or None,
        }
    return None


def get_playit_show():
    """Try to determine the current show / cart from PlayIt Live."""
    data = _playit_get("/api/v2/system/status") or _playit_get("/api/v2/status")
    if isinstance(data, dict):
        for k in ("currentShow", "current_show", "show", "showName"):
            if data.get(k):
                return str(data[k])
    return None


# ---------- autopo.st ----------
def get_autopost_track():
    try:
        r = requests.get(AUTOPOST_URL, timeout=HTTP_TIMEOUT_S)
        if r.status_code != 200:
            return None
        d = r.json()
        title = d.get("title") or d.get("track_title") or d.get("track_mix_title")
        artist = d.get("artist") or d.get("track_mix_artist")
        if not title:
            return None
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
            "artist":           artist or "",
            "album":            d.get("album") or "",
            "artwork_url":      d.get("artwork_url") or "",
            "started_at":       start_iso or now_iso(),
            "duration_seconds": length or None,
        }
    except (requests.RequestException, ValueError):
        return None


# ---------- Schedule ----------
def get_scheduled_show():
    try:
        with open(SCHEDULE_PATH) as f:
            sched = json.load(f)
    except Exception:
        return None
    days = ["sunday","monday","tuesday","wednesday","thursday","friday","saturday"]
    now = datetime.now()
    js_day_idx = (now.weekday() + 1) % 7
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

    # Try PlayIt Live first when on automation; autopo.st first when live
    track  = None
    source = "fallback"

    if show and show.get("automated"):
        track = get_playit_track()
        if track:
            source = "playitlive"
        else:
            track = get_autopost_track()
            if track:
                source = "autopost"
    else:
        track = get_autopost_track()
        if track:
            source = "autopost"
        else:
            track = get_playit_track()
            if track:
                source = "playitlive"

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
