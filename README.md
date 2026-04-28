# nowplaying

WLCB now-playing processor. Runs on LXC 161 (10.101.0.161) on proxmox1, hostname `nowplaying`.

## What it does

Every 20 seconds, the processor:

1. Determines what show is currently scheduled (from `schedule.json`)
2. Asks PlayIt Live for the current track
3. Falls back to autopo.st if PlayIt Live can't identify the song
4. Composes a unified payload and writes `now.json`
5. If `now.json` changed, commits + pushes to this repo

The lakesradio.org website fetches this `now.json` directly from `raw.githubusercontent.com`.

## now.json schema

```json
{
  "generated":         "2026-04-27T18:30:00-05:00",
  "source":            "playitlive" | "autopost" | "fallback",
  "show": {
    "name":      "The Mark Ricky Radio Show",
    "automated": false,
    "starts":    "09:00",
    "ends":      "12:00",
    "genre":     "Rock and Roll"
  },
  "track": {
    "title":            "Show Me Love",
    "artist":           "Robin S",
    "album":            "Show Me Love",
    "artwork_url":      "https://...",
    "started_at":       "2026-04-27T18:30:43Z",
    "duration_seconds": 270
  },
  "next_poll_seconds": 20
}
```

`track` may be `null` when no track is identifiable. `show` may be `null` outside scheduled hours.

## Deployment

The LXC was created from `ubuntu-24.04-standard` with these settings:

- VMID 161, hostname `nowplaying`, 1 core, 512 MB RAM, 4 GB disk
- IP 10.101.0.161/21 on vmbr0
- User `nowplaying` with an ed25519 deploy key registered on this repo (write access)

Setup steps performed once:

```bash
# As root inside the LXC
apt-get update && apt-get install -y python3-pip python3-venv git curl

# As nowplaying
sudo -u nowplaying -i
cd ~
git clone git@github.com:WLCB-LPFM/nowplaying.git
python3 -m venv venv
source venv/bin/activate
pip install requests
deactivate

# Copy env template, fill in API key
cp nowplaying/env.example .env
chmod 600 .env
# edit .env, set PLAYIT_API_KEY

# Install systemd service
sudo cp nowplaying/nowplaying.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nowplaying
```

## Updating the script

The processor pulls itself from this repo. To deploy a change:

```bash
sudo -u nowplaying bash -lc 'cd ~/nowplaying && git pull'
sudo systemctl restart nowplaying
```

## PlayIt Live notes

PlayIt Live runs as VM 101 on proxmox1 (Windows). Its API listens on the LAN.
Windows Firewall blocks inbound from non-localhost by default; allow inbound
from 10.101.0.161 on whatever port PlayIt Live's API is bound to.

To confirm the listening port, on the PlayIt Live VM run:

```
netstat -ano | findstr LISTEN
```

Update `PLAYIT_BASE_URL` in `.env` accordingly.

## Testing

Run once and dump JSON to stdout:

```bash
cd ~/nowplaying && ../venv/bin/python nowplaying.py --once
```

## Logs

```bash
journalctl -u nowplaying -f
tail -f ~/nowplaying.log
```
