# lanmouse

`lanmouse` is a small Linux LAN mouse-forwarding tool for one physical mouse.
The keyboard decides where the mouse goes: when the Bluetooth keyboard profile
is connected to `laptop`, `desktop`, or another receiver, that machine reports its
keyboard presence to the sender and the sender forwards mouse events there.
When the keyboard is local on the sender, the mouse is ungrabbed and behaves
normally on the laptop.

The sender reads the physical evdev mouse, grabs it only while forwarding to a
remote receiver, and streams safe mouse events over TCP. Receivers authenticate
the sender, create a `/dev/uinput` virtual mouse, and inject the received
events.

## Behavior

The sender keeps keyboard presence for `local` and every receiver. A target is
changed only when exactly one host has been present for `settle_seconds`
(default `1.2`); no-present and multi-present states leave the previous target
sticky. When a remote target is selected, the sender grabs the mouse and opens
the receiver connection immediately, reverting to local if connection setup
keeps failing for about three seconds. Sender and receiver both release pressed
mouse buttons on stream changes to avoid stuck buttons.

## Files and install paths

Install:

- `lanmouse.py` to `/usr/local/lib/lanmouse/lanmouse.py`
- one systemd unit to `/etc/systemd/system/lanmouse-sender.service` or
  `/etc/systemd/system/lanmouse-receiver.service`
- config to `/etc/lanmouse/config.json`

Both units run as root system services because evdev grabs and uinput require
root or equivalent device permissions.

Enable one role per machine:

```sh
systemctl daemon-reload
systemctl enable --now lanmouse-sender.service
systemctl enable --now lanmouse-receiver.service
```

## Sender config

Example for the Arch laptop with the physical `Logitech M215` mouse:

```json
{
  "role": "sender",
  "name": "laptop",
  "token": "change-this-long-random-token",
  "keyboard_match": "Designer Compact Keyboard",
  "mouse_match": "Logitech M215",
  "control_port": 47701,
  "receivers": [
    {"name": "laptop", "host": "laptop.local", "port": 47702},
    {"name": "desktop", "host": "desktop.local", "port": 47702}
  ]
}
```

Required common keys:

- `role`: `sender` or `receiver`
- `name`: this machine's lanmouse name
- `token`: shared secret used for HTTP and TCP authentication
- `keyboard_match`: substring matched against `N: Name=` lines in
  `/proc/bus/input/devices`

Sender-only keys:

- `mouse_match`: substring matched against `/sys/class/input/event*/device/name`
- `control_port`: HTTP control port, default `47701`
- `settle_seconds`: keyboard presence settle delay, default `1.2`
- `receivers`: list of `{ "name", "host", "port" }`

## Receiver config

Example for `laptop`:

```json
{
  "role": "receiver",
  "name": "laptop",
  "token": "change-this-long-random-token",
  "keyboard_match": "Designer Compact Keyboard",
  "listen_port": 47702,
  "sender_host": "laptop.local",
  "sender_control_port": 47701
}
```

Receiver-only keys:

- `listen_port`: TCP mouse stream port, default `47702`
- `sender_host`: sender hostname or IP
- `sender_control_port`: sender HTTP control port, default `47701`

## Firewall

Open only the needed LAN paths:

- sender: allow TCP `47701` from receiver machines
- receivers: allow TCP `47702` from the sender machine

The TCP stream starts with the shared token and the HTTP control endpoint
requires `X-Lanmouse-Token`, but the protocol is not encrypted. Use this only on
a trusted LAN or inside a private VPN.

## Manual checks

Status on the sender:

```sh
curl -H 'X-Lanmouse-Token: change-this-long-random-token' \
  http://127.0.0.1:47701/status
```

Run in debug mode with a temporary config:

```sh
python3 /usr/local/lib/lanmouse/lanmouse.py --config /etc/lanmouse/config.json --debug
```

## Managing services

`tools/lanmouse-ctl.sh` restarts and inspects the system unit and the user
indicator unit on every lanmouse machine, so one command covers `laptop`
(sender), `laptop`, and `desktop` (receivers):

```sh
tools/lanmouse-ctl.sh status all
tools/lanmouse-ctl.sh restart laptop
tools/lanmouse-ctl.sh logs desktop -n 20
```

Hosts are reached through the `laptop` and `desktop` entries in `~/.ssh/config`.
Remote system units are restarted with `ssh -t` so `sudo` can prompt in the
terminal, and an unreachable host is reported without aborting the other hosts.
The script only controls services; it does not deploy code.

Add a shell alias to reach it from anywhere. For fish, create
`~/.config/fish/functions/lm.fish`:

```fish
function lm --description 'lanmouse service control'
    /home/user/lanmouse/tools/lanmouse-ctl.sh $argv
end
```

Then `lm status all`, `lm restart desktop`, and so on.

## Indicator

`lanmouse-indicator.py` is an optional user-session Tk indicator for each host.
It polls the sender's `/status` endpoint and shows a small always-on-top marker
in the bottom-left corner: green for this machine, gray for another target, and
orange when the sender cannot be reached.

Install it as a user service:

```sh
mkdir -p ~/.local/lib/lanmouse-indicator ~/.config/systemd/user
cp lanmouse-indicator.py ~/.local/lib/lanmouse-indicator/
cp lanmouse-indicator.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now lanmouse-indicator.service
```

Config lives at `~/.config/lanmouse-indicator/config.json` unless `--config` is
used:

```json
{
  "self": "laptop",
  "status_url": "http://192.168.1.3:47701/status",
  "token": "change-this-long-random-token",
  "poll_interval": 1.0,
  "margin": 8,
  "labels": {"local": "laptop"}
}
```

Use `"self": "local"` on the sender laptop. If the service starts before the
display environment is available, uncomment the matching `DISPLAY` and
`XAUTHORITY` lines in the user unit.
