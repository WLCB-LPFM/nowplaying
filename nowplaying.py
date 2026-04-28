#!/usr/bin/env python3
"""
WLCB Now Playing processor.

Polls PlayIt Live + autopo.st, picks the best source for the current show
context, and emits a unified now.json to a public GitHub repo. The website
reads it via raw.githubusercontent.com.

Authoritative-source decision tree:

  Read PlayIt Live's playoutMode.automationOn; combine with schedule.automated:

    automationOn = true,  schedule.automated = true   -> Music block.
        PlayIt Live is driving playout; trust its currentItem for the track.
        Use autopo.st only to enrich with artwork. show = null (no host context).

    automationOn = true,  schedule.automated = false  -> Pre-recorded rebroadcast
        of a normally-live show. PlayIt Live's currentItem is the show audio
        file (often a 26-minute "track" named after the show), not the songs
        playing inside it. Use autopo.st (it fingerprints actual audio output).
        show = the program name + hosts so the website can display it after each
        song expires.

    automationOn = false                              -> Live host on the mic.
        PlayIt Live has no idea what's playing. Use autopo.st only.
        show = the program name + hosts (same display behavior as rebroadcast).

  If automationOn is unknown (PlayIt Live unreachable), fall back to using
  schedule.automated alone, with the same logic as if automationOn matched it.

Track expiration:
  Every candidate track is checked against `started_at + duration_seconds`.
  Once a track has run past (its end - 15s lead), it's dropped -- the website
  then shows "<show> / <hosts>" or the WLCB station fallback rather than a
  stale song name. The 15s lead expires the display slightly before the song
  actually ends to mask end-of-song silence and beat-mixed transitions.

Schedule source:
  The schedule is fetched from https://lakesradio-org.pages.dev/schedule.json,
  which is generated automatically at every site deploy from schedule.js.
  NOTE: update SCHEDULE_URL to https://lakesradio.org/schedule.json once the
  domain cutover from WordPress to the new site happens.
  The schedule is cached in memory for SCHEDULE_CACHE_TTL_S seconds (default:
  300) to avoid a network round-trip on every 20-second poll cycle. On fetch
  failure the last cached copy is used; if no copy exists, get_scheduled_show()
  returns None and the producer falls back gracefully.
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

# Schedule is fetched from the website (canonical source of truth).
# TODO: change to https://lakesradio.org/schedule.json after domain cutover.
SCHEDULE_URL      = os.environ.get(
    "SCHEDULE_URL",
    "https://lakesradio-org.pages.dev/schedule.json",
)
# Re-fetch the schedule at most every N seconds.
SCHEDULE_CACHE_TTL_S = int(os.environ.get("SCHEDULE_CACHE_TTL_S", "300"))

PLAYIT_MAX_REASONABLE_DURATION_S = int(os.environ.get("PLAYIT_MAX_REASONABLE_DURATION_S", "600"))
TRACK_EXPIRATION_LEAD_S = 15

if not PLAYIT_VERIFY_TLS:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[{now_iso()}] {msg}", flush=True)


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def track_is_fresh(track):
    if not track:
        return False
    started = parse_iso(track.get("started_at"))
    if not started:
        return False
    duration = track.get("duration_seconds") or 360
    age = (datetime.now(timezone.utc) - started).total_seconds()
    if age < -10:
        return False
    return age <= duration - TRACK_EXPIRATION_LEAD_S


# ---------- PlayIt Live ----------
def _playit_get(path: str):
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


def get_playit_automation_on():
    data = _playit_get("/api/control/liveAssist/playoutMode")
    if isinstance(data, dict) and "automationOn" in data:
        return bool(data["automationOn"])
    return None


def get_playit_track():
    current = _playit_get("/api/control/liveAssist/playoutLog/currentItem")
    if not isinstance(current, dict):
        return None
    if current.get("type") != "track":
        return None
    track_guid = current.get("trackGuid")
    if not track_guid:
        return None
    track = _playit_get(f"/api/control/tracks/{track_guid}")
    if not isinstance(track, dict):
        return None
    artist = (track.get("artist") or "").strip()
    title  = (track.get("title")  or "").strip()
    if not title:
        return None
    duration_raw = current.get("duration") or current.get("fullDuration") or track.get("activeDuration")
    duration = int(duration_raw) if duration_raw else None
    if duration and duration > PLAYIT_MAX_REASONABLE_DURATION_S:
        log(f"PlayIt: rejecting {title!r} (duration {duration}s > {PLAYIT_MAX_REASONABLE_DURATION_S}s \u2014 likely show container)")
        return None
    candidate = {
        "title":            title,
        "artist":           artist,
        "album":            track.get("album", "") or "",
        "artwork_url":      "",
        "started_at":       current.get("startTime") or now_iso(),
        "duration_seconds": duration,
        "year":             track.get("year") or current.get("year") or "",
    }
    if not track_is_fresh(candidate):
        return None
    return candidate


# ---------- autopo.st ----------
def get_autopost_track():
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
    try:
        length = int(d.get("track_length") or 0)
    except (TypeError, ValueError):
        length = 0
    candidate = {
        "title":            title,
        "artist":           artist,
        "album":            d.get("album") or "",
        "artwork_url":      d.get("artwork_url") or "",
        "started_at":       d.get("start") or now_iso(),
        "duration_seconds": length or None,
    }
    if not track_is_fresh(candidate):
        return None
    return candidate


def _norm(s: str) -> str:
    return "".join(c.lower() for c in (s or "") if c.isalnum())


def enrich_with_autopost(playit_track):
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
_schedule_cache = None
_schedule_fetched_at = 0.0

def _fetch_schedule():
    global _schedule_cache, _schedule_fetched_at
    try:
        r = requests.get(SCHEDULE_URL, timeout=HTTP_TIMEOUT_S)
        if r.status_code != 200:
            log(f"schedule fetch -> HTTP {r.status_code}")
            return None
        data = r.json()
        # v1 schema wraps schedule under 'schedule' key; tolerate both formats
        sched = data.get("schedule") or data
        _schedule_cache = sched
        _schedule_fetched_at = time.monotonic()
        log(f"schedule refreshed from {SCHEDULE_URL}")
        return sched
    except Exception as e:
        log(f"schedule fetch error: {e.__class__.__name__}: {e}")
        return None

def _get_schedule():
    global _schedule_cache, _schedule_fetched_at
    age = time.monotonic() - _schedule_fetched_at
    if _schedule_cache is None or age >= SCHEDULE_CACHE_TTL_S:
        result = _fetch_schedule()
        if result is None and _schedule_cache is not None:
            log("schedule fetch failed -- using cached copy")
    return _schedule_cache

def get_scheduled_show():
    sched = _get_schedule()
    if not sched:
        return None
    days = ["sunday","monday","tuesday","wednesday","thursday","friday","saturday"]
    now = datetime.now()  # local time (LXC is set to America/Chicago)
    js_day_idx = (now.weekday() + 1) % 7
    day = days[js_day_idx]
    mins = now.hour * 60 + now.minute
    for s in sched.get(day, []):
        sh, sm = [int(x) for x in s["start"].split(":")]
        eh, em = [int(x) for x in s["end"].split(":")]
        if sh*60+sm <= mins < eh*60+em:
            hosts = s.get("hosts")
            if not isinstance(hosts, list):
                hosts = []
            return {
                "name":      s["show"],
                "hosts":     hosts,
                "automated": bool(s.get("automated")),
                "starts":    s["start"],
                "ends":      s["end"],
                "genre":     s.get("genre"),
            }
    return None


# ---------- Compose payload ----------
def classify_mode(automation_on, schedule_automated):
    if automation_on is None:
        return "music_block" if schedule_automated else "live"
    if automation_on and schedule_automated:
        return "music_block"
    if automation_on and not schedule_automated:
        return "rebroadcast"
    return "live"


def build_payload():
    show     = get_scheduled_show()
    sched_a  = bool(show and show.get("automated"))
    auto_on  = get_playit_automation_on()
    mode     = classify_mode(auto_on, sched_a)

    track  = None
    source = "fallback"

    if mode == "music_block":
        pl = get_playit_track()
        if pl:
            track  = enrich_with_autopost(pl)
            source = "playitlive"
        else:
            ap = get_autopost_track()
            if ap:
                track  = ap
                source = "autopost"
    else:
        ap = get_autopost_track()
        if ap:
            track  = ap
            source = "autopost"

    payload_show = None
    if show and mode in ("rebroadcast", "live"):
        payload_show = show

    return {
        "generated":         now_iso(),
        "source":            source,
        "mode":              mode,
        "automation_on":     auto_on,
        "show":              payload_show,
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
    if old is None:
        return True
    keys = ("source", "mode", "automation_on", "show", "track")
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
        msg = f"now: [{payload.get('mode')}] {payload.get('source')} | {msg_track}"
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
