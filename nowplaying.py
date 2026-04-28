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
        show = the program name + host so the website can display it after each
        song expires.

    automationOn = false                              -> Live host on the mic.
        PlayIt Live has no idea what's playing. Use autopo.st only.
        show = the program name + host (same display behavior as rebroadcast).

  If automationOn is unknown (PlayIt Live unreachable), fall back to using
  schedule.automated alone, with the same logic as if automationOn matched it.

Track expiration:
  Every candidate track is checked against `started_at + duration_seconds`.
  Once a track has run past (its end - 15s lead), it's dropped -- the website
  then shows "<show> / <host>" or the WLCB station fallback rather than a
  stale song name. The 15s lead expires the display slightly before the song
  actually ends to mask end-of-song silence and beat-mixed transitions.
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

# Tracks longer than this from PlayIt Live are treated as suspicious (likely a
# show-container audio file rather than a song). 10 minutes covers nearly all
# real songs including extended jazz tracks; show containers tend to be 25-60+.
PLAYIT_MAX_REASONABLE_DURATION_S = int(os.environ.get("PLAYIT_MAX_REASONABLE_DURATION_S", "600"))

# Lead time: drop tracks this many seconds *before* their stated end. Negative
# value because we're expiring early. Hides end-of-song silence and gives the
# website a moment to switch to the show fallback before the next song starts.
TRACK_EXPIRATION_LEAD_S = 15

# Suppress urllib3 self-signed cert warnings (cert is self-signed on PlayIt Live)
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
    """A track is fresh while (now - started_at) < (duration_seconds - lead).
    If we don't have a duration, fall back to a 6-minute window."""
    if not track:
        return False
    started = parse_iso(track.get("started_at"))
    if not started:
        return False
    duration = track.get("duration_seconds") or 360
    age = (datetime.now(timezone.utc) - started).total_seconds()
    if age < -10:  # claims to start in the future = bad data
        return False
    return age <= duration - TRACK_EXPIRATION_LEAD_S


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


def get_playit_automation_on():
    """Returns True/False/None (None when unreachable)."""
    data = _playit_get("/api/control/liveAssist/playoutMode")
    if isinstance(data, dict) and "automationOn" in data:
        return bool(data["automationOn"])
    return None


def get_playit_track():
    """
    Fetch the currently-playing item from PlayIt Live.

    Returns a dict with title/artist/started_at/duration_seconds, or None if:
      - PlayIt Live is unreachable
      - Current item isn't a music track (jingle, advert, voice track, etc.)
      - Item lacks a trackGuid (live mic, aux input, etc.)
      - Track duration is implausibly long (likely a show-container audio file)
      - Track has already expired per its own start + duration
    """
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

    # Sanity check: if PlayIt Live says a "track" is 10+ minutes long, it's
    # almost certainly a show-container file rather than a song. Refuse it.
    if duration and duration > PLAYIT_MAX_REASONABLE_DURATION_S:
        log(f"PlayIt: rejecting {title!r} (duration {duration}s > {PLAYIT_MAX_REASONABLE_DURATION_S}s — likely show container)")
        return None

    candidate = {
        "title":            title,
        "artist":           artist,
        "album":            track.get("album", "") or "",
        "artwork_url":      "",  # PlayIt Live doesn't expose artwork; enriched from autopo.st
        "started_at":       current.get("startTime") or now_iso(),
        "duration_seconds": duration,
        "year":             track.get("year") or current.get("year") or "",
    }

    if not track_is_fresh(candidate):
        return None

    return candidate


# ---------- autopo.st ----------
def get_autopost_track():
    """
    Fetch current track from autopo.st fingerprinting service.
    Returns dict with title/artist/album/artwork_url/started_at/duration_seconds,
    or None if unreachable/empty/expired.
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
    now = datetime.now()  # local time (LXC is set to America/Chicago)
    js_day_idx = (now.weekday() + 1) % 7  # JS-style: Sunday=0..Saturday=6
    day = days[js_day_idx]
    mins = now.hour * 60 + now.minute
    for s in sched.get(day, []):
        sh, sm = [int(x) for x in s["start"].split(":")]
        eh, em = [int(x) for x in s["end"].split(":")]
        if sh*60+sm <= mins < eh*60+em:
            return {
                "name":      s["show"],
                "host":      s.get("host"),
                "automated": bool(s.get("automated")),
                "starts":    s["start"],
                "ends":      s["end"],
                "genre":     s.get("genre"),
            }
    return None


# ---------- Compose payload ----------
def classify_mode(automation_on, schedule_automated):
    """
    Returns one of: 'music_block', 'rebroadcast', 'live'.

    music_block:  PlayIt Live driving an automated music slot. Trust PlayIt Live.
                  Don't display show name (block names like "All Night Jazz" or
                  "The Block Party" aren't what listeners care about).
    rebroadcast:  PlayIt Live driving a re-airing of what is normally a live
                  show. autopo.st knows the songs; PlayIt Live just sees a
                  show-container file. Display show name when track expires.
    live:         No automation. Live host. autopo.st only. Display show name
                  when track expires.

    When PlayIt Live is unreachable (automation_on is None), we fall back to
    schedule.automated alone.
    """
    if automation_on is None:
        return "music_block" if schedule_automated else "live"
    if automation_on and schedule_automated:
        return "music_block"
    if automation_on and not schedule_automated:
        return "rebroadcast"
    return "live"  # automation off


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
        # 'rebroadcast' or 'live': autopo.st is the only reliable source for
        # what's actually being heard right now.
        ap = get_autopost_track()
        if ap:
            track  = ap
            source = "autopost"

    # Decide what to publish for `show`. The website uses this to display
    # "<show> / <host>" when a track has expired.
    #   - music_block:  null (don't surface the block name)
    #   - rebroadcast / live: the program name + host
    #   - no scheduled show: null
    payload_show = None
    if show and mode in ("rebroadcast", "live"):
        payload_show = show

    return {
        "generated":         now_iso(),
        "source":            source,
        "mode":              mode,           # "music_block" | "rebroadcast" | "live"
        "automation_on":     auto_on,        # true | false | null (unknown)
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
    """Compare meaningful fields, ignoring `generated` timestamp."""
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
