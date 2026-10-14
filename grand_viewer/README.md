# grand_viewer

One web page for every machine on the LAN: disk usage, CPU/memory/load/temperature,
every project you run (systemd services and timers, docker containers), whether
each one is up, failed, or waiting for its next timer run, its CPU/memory and
listening ports, and its logs one click away.

Agentless: nothing runs on the monitored machines. The hub (this machine) ssh-es
into each host on every poll, pipes `probe.py` into the remote `python3 -`, and
turns the JSON it prints into the dashboard. Standard library only, on both ends.

## Install

```sh
./install.sh            # user service + `grand-viewer` fish command
grand-viewer            # opens http://127.0.0.1:47800/ in a Chromium tab
./install.sh uninstall  # config is kept
```

`grand-viewer` starts `grand_viewer.service` if it isn't running, waits for it
to answer, then opens the dashboard as a new tab in the running Chromium (or
starts Chromium).

Files go to `~/.local/lib/grand_viewer/`; the unit is `grand_viewer.service`
(`systemctl --user status grand_viewer`). Without installing:
`./grand_viewer.py --config config.example.json`. `--once` polls every host a
single time and prints the processed snapshots as JSON (handy for debugging a host).

Requirements on each monitored host: key-based ssh from the hub that works with
`BatchMode=yes` (no password prompts), `python3`, systemd. Docker is optional.

## Config

`~/.config/grand_viewer/config.json` (seeded from `config.example.json`):

```json
{
  "listen": "127.0.0.1",
  "port": 47800,
  "interval": 30,
  "allowed_hosts": [],
  "hosts": [
    {"name": "workstation", "local": true},
    {"name": "laptop", "ssh": ["laptop", "laptop-link", "laptop-wifi"]}
  ]
}
```

- `ssh` is one `~/.ssh/config` alias or a list of them. Routes are tried in order
  (list ethernet before wifi); the first that answers is reused, and every 5 minutes
  the poll starts from the top again so a host that fell back to wifi returns to
  ethernet. Connections are multiplexed (`ControlMaster`), so polls and log
  fetches skip the ssh handshake.
- `local: true` probes this machine directly, without ssh.
- `interval` is seconds between polls. **Refresh now** polls immediately.
- `listen`: the dashboard shows logs and has no login, so it binds to loopback by
  default. To open it from other machines set `"listen": "0.0.0.0"`; it is then
  readable by anyone on the network. Requests are only accepted when the `Host`
  header is an IP address, `localhost`, or this machine's hostname (blocks DNS
  rebinding); add other names you use to reach it to `allowed_hosts`.

## What counts as a project

- **systemd units you installed**: `*.service`/`*.timer` files in
  `/etc/systemd/system/` and `~/.config/systemd/user/` (plus
  `~/.local/share/systemd/user/`). Symlinks into `/usr`, `/lib` (distro aliases)
  and masks are skipped; template units are shown per running instance.
  A timer is folded into the service it triggers: the row shows `next in …` /
  `last … ago`, and a failed last run shows as failed.
- **docker containers**, all of them (`docker ps -a`), tagged with their compose project.
- Distro units that have failed are listed separately under *Failed system units*.

Statuses: `running`, `scheduled` (timer waiting), `done` (oneshot that stays
active), `busy` (starting/stopping), `failed`, `stopped` (hidden unless *show stopped*).

## Storage

Each machine lists its drives (model, size, NVMe/USB/SSD/HDD, removable) with
every partition: mount point, filesystem, used/free, inode use, and unmounted or
swap partitions too. Mounts not backed by a listed drive (network shares, fuse)
appear under *Other filesystems*.

**analyze** opens a folder-by-folder breakdown of a filesystem: `du` runs on that
machine one level deep (staying on that filesystem); click a folder to go
deeper, use the breadcrumbs to go back. Scans run in the background on the
hub (a full disk takes about a minute), at most two per machine at once, and
finished scans are cached until **Rescan**. `du` runs as the ssh user, so folders
it can't read are reported as unreadable, and space it can't attribute is shown
as a gap against the filesystem's used total.

## Resource columns

Projects, containers and top processes have **CPU**, **Mem**, **Disk I/O** (read
`r` / write `w` per second) and **Network** (received `↓` / sent `↑` per second)
columns. Click any column header to sort by it: the first click puts the biggest
(or A–Z) first, the next click reverses it; empty cells always go last. Disk I/O
and Network sort by their total. The choice is remembered per table, in the browser.

Each machine's System panel also shows its total disk and network rates (physical
disks and network cards only, so tunnels such as WARP aren't counted twice).

Where the numbers come from, all without root on the monitored machine:

- **CPU** is a share of the whole machine (all cores), for the machine, projects,
  containers and processes alike, so project shares add up to the machine total.
  Hover a cell to see it in cores.
- **Disk I/O**: the service's cgroup `io.stat` where the kernel's io controller covers
  it (system services, containers); otherwise the summed `/proc/<pid>/io` of its
  processes (user services, which run as you). Writes are charged when the kernel
  flushes them; without the memory controller (Raspberry Pi OS) flushes aren't
  charged to a service, so its writes read 0.
- **Network**: the kernel's per-connection TCP byte counters (`ss -ti --cgroup`),
  added up per service; loopback (127.0.0.1) traffic is left out. UDP has no such
  counters, so HTTP/3/QUIC, DNS and other UDP traffic is not counted per project
  (the machine total includes it). Containers with their own network use their
  interface counters, which include everything.

## Notes

- Rates are averaged between two consecutive polls, so they appear from the second
  poll on; the hub takes that second sample 3 s after startup.
- Memory comes from the unit/container cgroup. Kernels booted with
  `cgroup_disable=memory` (the Raspberry Pi OS default) have none; there the
  dashboard shows the summed RSS of the unit's processes, marked `≈`.
- Ports are attributed to a unit through the owning process's cgroup. `ss` only
  reveals the owner of your own processes, so sockets of root-owned services show
  up under *Other listening ports* as "other user".
- Logs: `journalctl --user-unit`/`--unit` and `docker logs` on the remote host.
  System-unit logs need the remote user in the `adm` or `systemd-journal` group.
- Remote timestamps are converted with the remote host's clock, so clock skew
  between machines does not distort "up 3h" / "next in 2h".
