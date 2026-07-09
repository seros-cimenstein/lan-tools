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
    {"name": "desktop", "host": "192.168.1.5", "port": 47700}
  ],
  "poll_interval": 0.7,
  "max_bytes": 10000000
}
```

Required keys: `name`, `token`, `peers`.

Defaults: `listen_port` is `47700`, `poll_interval` is `0.7`, and `max_bytes` is `10000000`.

Use the same `token` on every machine. Put the other machines in `peers`; do not list the local machine as its own peer.

## Protocol

Peers send `POST /clip` with `X-Lanclip-Token` and a JSON body:

```json
{"name": "laptop", "mime": "text/plain", "data": "base64-data"}
```

Supported MIME types are `text/plain`, `image/png`, and `image/jpeg`. `GET /ping` uses the same token header and returns the local node name.

## Install

```sh
mkdir -p ~/.local/lib/lanclip ~/.config/lanclip ~/.config/systemd/user
cp lanclip.py ~/.local/lib/lanclip/
cp lanclip.service ~/.config/systemd/user/
$EDITOR ~/.config/lanclip/config.json
```

Install clipboard tools:

```sh
# Arch / Wayland
sudo pacman -S wl-clipboard

# Linux Mint / X11
sudo apt install xclip
```

Edit `~/.config/systemd/user/lanclip.service` if the host needs explicit `DISPLAY`, `XAUTHORITY`, `WAYLAND_DISPLAY`, or `XDG_RUNTIME_DIR` environment lines.

Enable and start:

```sh
systemctl --user daemon-reload
systemctl --user enable --now lanclip.service
journalctl --user -u lanclip.service -f
```

Open `47700/tcp` from the LAN on each host firewall.
