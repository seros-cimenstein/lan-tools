# lanclip

`lanclip` is a small LAN universal clipboard daemon for Linux machines on the same trusted network. It polls the local clipboard and sends text, PNG images, and JPEG images to configured peers over token-protected HTTP.

It uses only the Python standard library. Clipboard access is delegated to `wl-paste`/`wl-copy` on Wayland and `xclip` on X11.

## Config

Create `~/.config/lanclip/config.json` on each host:

```json
{
  "name": "laptop",
  "listen_port": 47700,
  "token": "hex-shared-secret",
  "peers": [
    {"name": "desktop", "hosts": ["192.168.1.5", "192.168.1.31"], "port": 47700}
  ],
  "poll_interval": 0.7,
  "max_bytes": 10000000
}
```

Required keys: `name`, `token`, `peers`.

Defaults: `listen_port` is `47700`, `poll_interval` is `0.7`, and `max_bytes` is `10000000`.

Use the same `token` on every machine. Put the other machines in `peers`; do not list the local machine as its own peer.

### Peer addresses

A peer takes either a single `"host"` string or a `"hosts"` list of candidate
addresses; both may be given and are merged in order, duplicates dropped. This
is what keeps the mesh working when a DHCP lease moves:

- Addresses are tried in order, so **list the preferred path first** (ethernet
  before wifi). The first one that answers is remembered and used for
  subsequent sends, so the fallback costs a timeout only once.
- Every 60s (`PREFERRED_RECHECK_INTERVAL`) the liveness probe retries from the
  top of the list, so a peer that fell back to wifi returns to ethernet as soon
  as ethernet recovers.
- `GET /status` reports the address currently in use for each peer under
  `peers.<name>.host`, and failover is logged as
  `peer <name> now reachable at <addr>`.

A peer whose addresses are all unreachable is logged once per minute rather
than on every clip.

## Protocol

Peers send `POST /clip` with `X-Lanclip-Token` and a JSON body:

```json
{"name": "laptop", "mime": "text/plain", "data": "base64-data"}
```

Supported MIME types are `text/plain`, `image/png`, and `image/jpeg`. `GET /ping` uses the same token header and returns the local node name.

## Indicator

`lanclip-indicator` polls the local daemon's `GET /status` endpoint and shows a top-left row of machines. Green dots are reachable, gray dots are unreachable, and a blue checked badge marks the machine that most recently supplied clipboard content. An orange `?` appears when the local daemon cannot be reached.

Create `~/.config/lanclip-indicator/config.json` on each host:

```json
{
  "self": "laptop",
  "status_url": "http://127.0.0.1:47700/status",
  "token": "hex-shared-secret",
  "poll_interval": 1.0,
  "margin": 8,
  "font_size": 14,
  "opacity": 0.8,
  "dot_opacity": 1.0,
  "text_opacity": 0.9,
  "labels": {
    "laptop": "Laptop",
    "desktop": "Desktop",
    "laptop": "Laptop"
  }
}
```

Required keys: `self`, `status_url`, `token`. Use the same token as the local `lanclip` daemon. The remaining values shown above are their defaults, except `labels`, which defaults to an empty object and falls back to machine names.

### Swaybar mode

On sway, the indicator can live in the bar instead of a floating window. `--swaybar` turns it into a `status_command` that prints one dot per machine (green reachable, gray unreachable, blue for the latest clipboard source, orange `?` when the daemon is down). Clicking the block toggles machine names. `--clock FORMAT` appends a strftime clock block, since swaybar runs only one status command:

```
bar {
    status_command python3 ~/.local/lib/lanclip-indicator/lanclip-indicator.py --swaybar --clock "'%Y-%m-%d %X'"
}
```

Sway strips one level of quotes, so the format needs the double-then-single quoting shown above. On such hosts disable the floating window with `systemctl --user disable --now lanclip-indicator.service`; the bar process reads the same config.

## Install

```sh
mkdir -p ~/.local/lib/lanclip ~/.local/lib/lanclip-indicator
mkdir -p ~/.config/lanclip ~/.config/lanclip-indicator ~/.config/systemd/user
cp lanclip.py ~/.local/lib/lanclip/
cp lanclip-indicator.py ~/.local/lib/lanclip-indicator/
cp lanclip.service lanclip-indicator.service ~/.config/systemd/user/
$EDITOR ~/.config/lanclip/config.json ~/.config/lanclip-indicator/config.json
```

Install clipboard tools:

```sh
# Arch / Wayland
sudo pacman -S wl-clipboard

# Linux Mint / X11
sudo apt install xclip
```

## Autostart

Both services install as `WantedBy=graphical-session.target`, so they start with the desktop session and inherit the session's `WAYLAND_DISPLAY`/`DISPLAY` from the systemd user environment. On compositors with systemd integration (sway, GNOME, KDE) no environment lines are needed.

Check that the target actually activates on the host:

```sh
systemctl --user is-active graphical-session.target
```

If it stays `inactive` while a session is running, the desktop never activates the target. On such hosts edit both unit files: switch `[Install]` to `WantedBy=default.target` and uncomment the matching `Environment=` lines (X11 or Wayland) in `[Service]`, then re-enable.

Enable and start:

```sh
systemctl --user daemon-reload
systemctl --user enable --now lanclip.service lanclip-indicator.service
journalctl --user -u lanclip.service -u lanclip-indicator.service -f
```

Open `47700/tcp` from the LAN on each host firewall.

If the indicator logs `waiting for wayland display`, the session is not up (or the unit's environment points at a dead socket); it retries for two minutes, then exits so systemd restarts it with a fresh environment. A journal full of `Gtk couldn't be initialized` tracebacks means an old copy of `lanclip-indicator.py` is installed or the unit's `WantedBy`/`Environment` don't match the host.

## Managing services

`tools/lanclip-ctl.sh` restarts and inspects the lanclip user units on every lanclip machine, so one command covers `laptop`, `laptop`, and `desktop`. Each `HOSTS` entry lists that host's units: `laptop` uses swaybar mode and runs only `lanclip`; `laptop` and `desktop` also run `lanclip-indicator`:

```sh
tools/lanclip-ctl.sh status all
tools/lanclip-ctl.sh restart laptop
tools/lanclip-ctl.sh logs desktop -n 20
```

Hosts are reached through the `laptop` and `desktop` entries in `~/.ssh/config`. Both units are user units, so no `sudo` is involved. `systemctl --user` over ssh works because the script exports `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS`. An unreachable host is reported without aborting the other hosts. The script only controls services; it does not deploy code.

Add a shell alias to reach it from anywhere. For fish, create `~/.config/fish/functions/lc.fish`:

```fish
function lc --description 'lanclip service control'
    /home/user/projects/quality_of_life_setups/lanclip/tools/lanclip-ctl.sh $argv
end
```

Then `lc status all`, `lc restart desktop`, and so on.
