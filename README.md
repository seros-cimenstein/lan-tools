# lan-tools

Small Linux tools for a few machines that share one desk and one trusted home LAN:
a shared clipboard, a mouse that follows the keyboard, and a dashboard for all of them.
I wrote them for my own setup (a sway laptop, other Linux PCs and a Raspberry Pi).
Each folder is independent and has its own README.

| Tool | What it does |
|---|---|
| [lanclip](lanclip/) | LAN universal clipboard daemon: copies text, PNG and JPEG between machines over token-protected HTTP, with an optional on-screen or swaybar indicator showing which peers are reachable. |
| [kbfollow](kbfollow/) | One physical mouse for several machines: the mouse is forwarded (evdev to uinput over TCP) to whichever machine the Bluetooth keyboard is currently connected to. Formerly `lanmouse`. |
| [grand_viewer](grand_viewer/) | Agentless web dashboard for every machine on the LAN: disks and storage, CPU/memory, systemd services and timers, docker containers, ports and logs, gathered over ssh. |

## Requirements

- Linux with Python 3 (standard library only for the daemons).
- systemd for the provided units (user units for lanclip and grand_viewer, system units
  for kbfollow because evdev grabs and `/dev/uinput` need root).
- lanclip: `wl-clipboard` on Wayland or `xclip` on X11.
- Indicators (lanclip, kbfollow): GTK 3 with gtk-layer-shell through PyGObject on Wayland,
  Tk otherwise; or swaybar mode with no GUI toolkit.
- grand_viewer: key-based ssh to each monitored host (`BatchMode=yes`) and `python3` there;
  `grand-viewer` opens the page in Chromium.

Install and configuration steps are in each tool's README.

## Status and limitations

- Personal tools, tested only on my own machines (Arch Linux with sway, Linux Mint, Raspberry Pi OS).
- Host names in the examples and in the `tools/*-ctl.sh` scripts (`laptop`, `laptop2`,
  `desktop`, `pi`) are placeholders for my machines; edit them for yours.
- Not security-audited. lanclip and kbfollow authenticate with a shared token but do not
  encrypt traffic; use them only on a trusted LAN or inside a VPN. grand_viewer has no login
  and binds to loopback by default.

## License

MIT, see [LICENSE](LICENSE).
