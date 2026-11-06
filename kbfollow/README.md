# kbfollow

`kbfollow` ("keyboard follow") is a small Linux LAN mouse-forwarding tool for
one physical mouse: the mouse follows whichever machine the Bluetooth keyboard
is connected to. When the keyboard profile is connected to `laptop2`, `desktop`,
or another receiver, that machine reports its keyboard presence to the sender
and the sender forwards mouse events there. When the keyboard is local on the
sender, the mouse is ungrabbed and behaves normally on the laptop.

It was called `lanmouse` during development (older commits use that name); it
is unrelated to the [lan-mouse](https://github.com/feschber/lan-mouse) project.

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

- `kbfollow.py` to `/usr/local/lib/kbfollow/kbfollow.py`
- one systemd unit to `/etc/systemd/system/kbfollow-sender.service` or
  `/etc/systemd/system/kbfollow-receiver.service`
- config to `/etc/kbfollow/config.json`

Both units run as root system services because evdev grabs and uinput require
root or equivalent device permissions.

Enable one role per machine:

```sh
systemctl daemon-reload
systemctl enable --now kbfollow-sender.service
systemctl enable --now kbfollow-receiver.service
```

## Sender config

Example for the Arch laptop with the physical `Logitech M215` mouse:

```json
{
  "role": "sender",
  "name": "laptop",
  "token": "change-this-long-random-token",
  "keyboard_match": "Designer Compact Keyboard",
  "mouse_match": ["Logitech M215", "LanPad pointer"],
  "control_port": 47701,
  "receivers": [
    {"name": "laptop2", "host": "laptop2.local", "port": 47702},
    {"name": "desktop", "host": "desktop.local", "port": 47702}
  ]
}
```

Required common keys:

- `role`: `sender` or `receiver`
- `name`: this machine's kbfollow name
- `token`: shared secret used for HTTP and TCP authentication
- `keyboard_match`: substring matched against `N: Name=` lines in
  `/proc/bus/input/devices`

Sender-only keys:

- `mouse_match`: one substring or a non-empty list of substrings matched against
  `/sys/class/input/event*/device/name`. Use a list to forward multiple mice;
  for example, `LanPad pointer` lets LanPad's phone touchpad be forwarded without
  matching the separate `LanPad` touchpad device.
- `control_port`: HTTP control port, default `47701`
- `settle_seconds`: keyboard presence settle delay, default `1.2`
- `receivers`: list of `{ "name", "host", "port" }`

## Receiver config

Example for `laptop2`:

```json
{
  "role": "receiver",
  "name": "laptop2",
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
requires `X-Kbfollow-Token`, but the protocol is not encrypted. Use this only on
a trusted LAN or inside a private VPN.

## Manual checks

Status on the sender:

```sh
curl -H 'X-Kbfollow-Token: change-this-long-random-token' \
  http://127.0.0.1:47701/status
```

Run in debug mode with a temporary config:

```sh
python3 /usr/local/lib/kbfollow/kbfollow.py --config /etc/kbfollow/config.json --debug
```

## Managing services

`tools/kbfollow-ctl.sh` restarts and inspects the system unit and the
indicator on every kbfollow machine, so one command covers `laptop`
(sender), `laptop2`, and `desktop` (receivers). The laptop's indicator runs inside
swaybar (see below), so the script only reports whether it is running and
leaves restarting it to `swaymsg reload`:

```sh
tools/kbfollow-ctl.sh status all
tools/kbfollow-ctl.sh restart laptop2
tools/kbfollow-ctl.sh logs desktop -n 20
```

Hosts are reached through the `laptop2` and `desktop` entries in `~/.ssh/config`;
edit the `HOSTS` array at the top of the script to match your machines.
Remote system units are restarted with `ssh -t` so `sudo` can prompt in the
terminal, and an unreachable host is reported without aborting the other hosts.
The script only controls services; it does not deploy code.

Add a shell alias to reach it from anywhere. For fish, create
`~/.config/fish/functions/kf.fish`:

```fish
function kf --description 'kbfollow service control'
    /path/to/lan-tools/kbfollow/tools/kbfollow-ctl.sh $argv
end
```

Then `kf status all`, `kf restart desktop`, and so on.

## Indicator

`kbfollow-indicator.py` is an optional per-host indicator. It polls the sender's
`/status` endpoint and shows which machine has the keyboard and mouse: green
for this machine, gray for another target, and orange when the sender cannot
be reached. By default it draws a small always-on-top pill at the top center of
the screen (GTK layer-shell on Wayland, Tk otherwise).

Install it as a user service:

```sh
mkdir -p ~/.local/lib/kbfollow-indicator ~/.config/systemd/user
cp kbfollow-indicator.py ~/.local/lib/kbfollow-indicator/
cp kbfollow-indicator.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kbfollow-indicator.service
```

Config lives at `~/.config/kbfollow-indicator/config.json` unless `--config` is
used:

```json
{
  "self": "laptop2",
  "status_url": "http://laptop.local:47701/status",
  "token": "change-this-long-random-token",
  "poll_interval": 1.0,
  "margin": 8,
  "labels": {"local": "laptop"}
}
```

Use `"self": "local"` on the sender laptop. If the service starts before the
display environment is available, uncomment the matching `DISPLAY` and
`XAUTHORITY` lines in the user unit.

### Swaybar mode

On sway hosts the indicator can live in the bar instead of drawing an overlay.
`--swaybar` prints the i3bar JSON protocol to stdout: one `⌨ <target>` block,
green/gray/orange as above. `--clock FORMAT` appends a strftime clock block,
since swaybar's `status_command` replaces the whole status line. Disable the
overlay unit first:

```sh
systemctl --user disable --now kbfollow-indicator.service
```

Alone in the bar:

```
bar {
    status_command python3 ~/.local/lib/kbfollow-indicator/kbfollow-indicator.py --swaybar --clock "'%Y-%m-%d %X'"
}
```

swaybar runs only one status command, so sharing the bar with other producers
(e.g. lanclip-indicator) goes through `tools/swaybar-merge.py`. It runs each
argument with `sh -c`, concatenates their blocks in argument order, and routes
click events to whichever child owns the clicked block. Children must print one
block array per line. Install it and call it from a small wrapper script so
sway's config quoting stays simple:

```sh
install -m 755 tools/swaybar-merge.py ~/.local/bin/swaybar-merge
```

```sh
#!/bin/sh
# ~/.local/bin/sway-status
exec "$HOME/.local/bin/swaybar-merge" \
  "python3 $HOME/.local/lib/kbfollow-indicator/kbfollow-indicator.py --swaybar" \
  "python3 $HOME/.local/lib/lanclip-indicator/lanclip-indicator.py --swaybar --clock '%Y-%m-%d %X'"
```

```
bar {
    status_command ~/.local/bin/sway-status
}
```
