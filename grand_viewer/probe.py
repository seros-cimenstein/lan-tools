#!/usr/bin/env python3
"""One-shot host snapshot for grand_viewer.

Runs on the monitored machine (piped over ssh as `python3 -`, or directly for
the local host) and prints one JSON object to stdout. Standard library only;
must stay compatible with the oldest python3 on any monitored host.

Counters (CPU jiffies, unit CPU nanoseconds, cgroup usage, process ticks) are
reported raw together with a CLOCK_MONOTONIC stamp; the hub turns consecutive
snapshots into rates.
"""
import glob
import json
import os
import pwd
import re
import subprocess
import sys
import threading
import time

HOME = os.path.expanduser("~")
SYSTEM_UNIT_DIRS = ["/etc/systemd/system"]
USER_UNIT_DIRS = [HOME + "/.config/systemd/user", HOME + "/.local/share/systemd/user"]
# Unit files that resolve into these trees belong to the distro, not to a project.
DISTRO_PREFIXES = ("/usr/", "/lib/", "/dev/")
CGROUP_ROOT = "/sys/fs/cgroup"
UNIT_PROPS = [
    "Id", "Description", "LoadState", "ActiveState", "SubState", "UnitFileState",
    "Type", "MainPID", "MemoryCurrent", "CPUUsageNSec", "NRestarts", "Result",
    "ActiveEnterTimestamp", "InactiveEnterTimestamp", "ExecMainExitTimestamp",
    "ExecMainStatus", "Triggers", "TriggeredBy", "NextElapseUSecRealtime",
    "LastTriggerUSec", "ControlGroup",
]
REAL_FS = {
    "ext2", "ext3", "ext4", "xfs", "btrfs", "vfat", "exfat", "ntfs", "ntfs3",
    "fuseblk", "f2fs", "zfs", "jfs", "reiserfs", "nfs", "nfs4", "cifs", "smb3",
    "bcachefs",
}
NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3"}


def run(cmd, timeout=10):
    """Return (returncode, stdout, stderr); returncode None if cmd is unavailable."""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "", str(e)


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


# ---------------------------------------------------------------- host basics

def host_info():
    os_name = ""
    for line in read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            os_name = line.split("=", 1)[1].strip().strip('"')
    u = os.uname()
    return {
        "hostname": u.nodename,
        "kernel": u.release,
        "arch": u.machine,
        "os": os_name,
        "user": pwd.getpwuid(os.getuid()).pw_name,
        "cpus": os.cpu_count() or 1,
        "uptime": float(read("/proc/uptime", "0").split()[0]),
        "load": list(os.getloadavg()),
        "clk_tck": os.sysconf("SC_CLK_TCK"),
        "page_kb": os.sysconf("SC_PAGE_SIZE") // 1024,
    }


def cpu_counters():
    fields = [int(x) for x in read("/proc/stat").splitlines()[0].split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)  # idle + iowait
    # guest/guest_nice (fields 8, 9) are already included in user/nice.
    return {"total": sum(fields[:8]), "idle": idle}


def memory():
    info = {}
    for line in read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            info[key] = int(parts[0]) * 1024
    return {
        "total": info.get("MemTotal", 0),
        "available": info.get("MemAvailable", info.get("MemFree", 0)),
        "swap_total": info.get("SwapTotal", 0),
        "swap_free": info.get("SwapFree", 0),
    }


def temperature():
    """Hottest CPU sensor in degrees C (else hottest sensor of any kind), or None."""
    cpu_kinds = ("x86_pkg", "cpu", "soc", "k10temp", "coretemp", "zenpower")
    readings = []  # (kind, path of a millidegree file)
    for zone in glob.glob("/sys/class/thermal/thermal_zone*"):
        readings.append((read(zone + "/type"), zone + "/temp"))
    # AMD/Intel laptops often expose CPU temperature only through hwmon.
    for mon in glob.glob("/sys/class/hwmon/hwmon*"):
        kind = read(mon + "/name")
        if any(k in kind.lower() for k in cpu_kinds):
            readings.extend((kind, f) for f in glob.glob(mon + "/temp*_input"))
    best = None
    preferred = None
    for kind, path in readings:
        raw = read(path).strip()
        if not raw.lstrip("-").isdigit():
            continue
        c = int(raw) / 1000.0
        if c <= 0 or c > 150:
            continue
        if any(k in kind.strip().lower() for k in cpu_kinds):
            preferred = c if preferred is None else max(preferred, c)
        best = c if best is None else max(best, c)
    return preferred if preferred is not None else best


def disks():
    seen = {}
    for line in read("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[2] not in REAL_FS:
            continue
        dev, mnt, fstype = parts[0], parts[1].replace("\\040", " "), parts[2]
        # Bind mounts and btrfs subvolumes repeat the device: keep the shortest path.
        if dev in seen and len(seen[dev][0]) <= len(mnt):
            continue
        seen[dev] = (mnt, fstype)

    out = []
    for dev, (mnt, fstype) in sorted(seen.items(), key=lambda kv: kv[1][0]):
        result = {}

        def stat(mnt=mnt, result=result):
            try:
                result["st"] = os.statvfs(mnt)
            except OSError:
                pass

        # A dead network mount blocks statvfs forever; never let it stall the probe.
        t = threading.Thread(target=stat, daemon=True)
        t.start()
        t.join(2 if fstype in NETWORK_FS else 5)
        st = result.get("st")
        if st is None or st.f_blocks == 0:
            out.append({"mount": mnt, "device": dev, "dev_real": os.path.realpath(dev),
                        "fs": fstype, "error": "unresponsive"})
            continue
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        avail = st.f_bavail * st.f_frsize
        out.append({
            "mount": mnt, "device": dev, "dev_real": os.path.realpath(dev), "fs": fstype,
            "size": st.f_blocks * st.f_frsize, "used": used, "avail": avail,
            "inodes_pct": (100.0 * (st.f_files - st.f_ffree) / st.f_files) if st.f_files else None,
        })
    return out


def storage(mounted):
    """Physical drives with their partitions; each partition that is mounted
    carries the `mount` key of its entry in disks()."""
    cols = "NAME,PATH,SIZE,TYPE,FSTYPE,MOUNTPOINT,TRAN,MODEL,RM,ROTA,LABEL"
    rc, stdout, _ = run(["lsblk", "-J", "-b", "-o", cols])
    if rc != 0:  # util-linux < 2.33 has no PATH column
        rc, stdout, _ = run(["lsblk", "-J", "-b", "-o", cols.replace("PATH,", "")])
    if rc != 0:
        return None
    try:
        tree = json.loads(stdout).get("blockdevices", [])
    except ValueError:
        return None
    by_dev = {d["dev_real"]: d["mount"] for d in mounted}

    def flag(v):  # lsblk prints booleans as true/false or "0"/"1" depending on version
        return v in (True, 1, "1")

    def leaf(n):
        path = n.get("path") or "/dev/" + n["name"]
        mp = n.get("mountpoint")
        return {
            "name": n["name"], "size": int(n.get("size") or 0),
            "type": n.get("type"), "fstype": n.get("fstype"), "label": n.get("label"),
            "mount": by_dev.get(os.path.realpath(path)),
            "swap": mp == "[SWAP]",
        }

    def leaves(n):
        kids = n.get("children") or []
        if not kids:
            return [leaf(n)]
        out = []
        for k in kids:
            out.extend(leaves(k))
        return out

    out = []
    for n in tree:
        if n.get("type") not in ("disk", "rom") or n["name"].startswith(("zram", "ram", "loop")):
            continue
        size = int(n.get("size") or 0)
        parts = leaves(n) if (n.get("children") or n.get("fstype")) else []
        out.append({
            "name": n["name"], "model": (n.get("model") or "").strip(), "size": size,
            "tran": n.get("tran"), "removable": flag(n.get("rm")),
            "rotational": flag(n.get("rota")), "type": n.get("type"), "parts": parts,
        })
    return out


# ------------------------------------------------------------- systemd units

def project_unit_names(dirs):
    """Service/timer unit files installed by the user rather than the distro."""
    names = []
    for d in dirs:
        for path in sorted(glob.glob(os.path.join(d, "*"))):
            name = os.path.basename(path)
            if not name.endswith((".service", ".timer")) or not os.path.isfile(path):
                continue
            if os.path.islink(path) and os.path.realpath(path).startswith(DISTRO_PREFIXES):
                continue  # alias of a distro unit
            if name not in names:
                names.append(name)
    return names


def expand_templates(scope_args, names):
    """foo@.service -> its loaded instances (foo@a.service, ...)."""
    out = []
    for name in names:
        if "@." not in name:
            out.append(name)
            continue
        pattern = name.replace("@.", "@*.")
        rc, stdout, _ = run(["systemctl"] + scope_args + [
            "list-units", "--all", "--plain", "--no-legend", pattern])
        if rc == 0:
            out.extend(line.split()[0] for line in stdout.splitlines() if line.strip())
    return out


def parse_show(text):
    units, cur = [], {}
    for line in text.splitlines():
        if not line.strip():
            if cur:
                units.append(cur)
                cur = {}
            continue
        k, _, v = line.partition("=")
        cur[k] = v
    if cur:
        units.append(cur)
    return units


def parse_ts(value):
    """systemctl timestamp -> unix seconds (target-local wall clock), or None."""
    if not value or value in ("n/a", "0"):
        return None
    if value.startswith("@"):
        try:
            return int(value[1:])
        except ValueError:
            return None
    # "Wed 2026-10-14 19:02:08 +03": properties --timestamp=unix leaves untouched.
    m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", value)
    if not m:
        return None
    return int(time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")))


def to_int(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    # systemd reports "unset" counters as UINT64_MAX.
    return None if n >= 2 ** 63 else n


def unit_of(pid):
    """(scope, unit) owning pid, from its systemd cgroup path; None if unmanaged."""
    path = ""
    for line in read("/proc/%d/cgroup" % pid).splitlines():
        # cgroup v2 is "0::/path"; on v1 the systemd hierarchy is "N:name=systemd:/path".
        if line.startswith("0::") or ":name=systemd:" in line:
            path = line.split(":", 2)[2]
            break
    if "/user@" in path:
        scope = "user"
    elif path.startswith("/system.slice/"):
        scope = "system"
    else:
        return None
    for part in reversed(path.split("/")):
        if part.endswith((".service", ".scope")):
            return scope, part
    return None


def units(scope):
    scope_args = ["--user"] if scope == "user" else []
    names = project_unit_names(USER_UNIT_DIRS if scope == "user" else SYSTEM_UNIT_DIRS)
    names = expand_templates(scope_args, names)
    if not names:
        return []
    rc, stdout, stderr = run(["systemctl"] + scope_args + [
        "show", "--timestamp=unix", "-p", ",".join(UNIT_PROPS)] + names, timeout=15)
    if rc != 0:
        # systemd < 248 has no --timestamp=unix; parse_ts handles both formats.
        rc, stdout, stderr = run(["systemctl"] + scope_args + [
            "show", "-p", ",".join(UNIT_PROPS)] + names, timeout=15)
    out = []
    for u in parse_show(stdout):
        if u.get("LoadState") == "not-found":
            continue
        out.append({
            "id": u.get("Id", ""),
            "scope": scope,
            "description": u.get("Description", ""),
            "load": u.get("LoadState", ""),
            "active": u.get("ActiveState", ""),
            "sub": u.get("SubState", ""),
            "enabled": u.get("UnitFileState", ""),
            "type": u.get("Type", ""),
            "pid": to_int(u.get("MainPID")) or None,
            "memory": to_int(u.get("MemoryCurrent")),
            "cpu_ns": to_int(u.get("CPUUsageNSec")),
            "restarts": to_int(u.get("NRestarts")),
            "result": u.get("Result", ""),
            "exit_status": to_int(u.get("ExecMainStatus")),
            "active_since": parse_ts(u.get("ActiveEnterTimestamp")),
            "inactive_since": parse_ts(u.get("InactiveEnterTimestamp")),
            "last_exit": parse_ts(u.get("ExecMainExitTimestamp")),
            "triggers": u.get("Triggers", "").split(),
            "triggered_by": u.get("TriggeredBy", "").split(),
            "next_run": parse_ts(u.get("NextElapseUSecRealtime")),
            "last_trigger": parse_ts(u.get("LastTriggerUSec")),
            "cgroup": u.get("ControlGroup", ""),
        })
    return out


def failed_units(scope):
    scope_args = ["--user"] if scope == "user" else []
    rc, stdout, _ = run(["systemctl"] + scope_args + [
        "list-units", "--failed", "--plain", "--no-legend"])
    out = []
    for line in stdout.splitlines():
        parts = line.split(None, 4)
        if parts:
            out.append({"id": parts[0], "scope": scope,
                        "description": parts[4] if len(parts) > 4 else ""})
    return out


# -------------------------------------------------------------- docker, ports

def containers():
    rc, stdout, stderr = run(["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"])
    if rc is None:
        return None, None  # docker not installed
    if rc != 0:
        return [], (stderr.strip().splitlines() or ["docker ps failed"])[-1]
    out = []
    for line in stdout.splitlines():
        try:
            c = json.loads(line)
        except ValueError:
            continue
        labels = dict(kv.split("=", 1) for kv in c.get("Labels", "").split(",") if "=" in kv)
        cid = c.get("ID", "")
        cg = ""
        # systemd cgroup driver, then cgroupfs driver.
        for path in ("/system.slice/docker-%s.scope" % cid, "/docker/%s" % cid):
            if os.path.isdir(CGROUP_ROOT + path):
                cg = path
                break
        mem = read(CGROUP_ROOT + cg + "/memory.current").strip() if cg else ""
        usage = None
        for line2 in read(CGROUP_ROOT + cg + "/cpu.stat").splitlines() if cg else []:
            if line2.startswith("usage_usec "):
                usage = int(line2.split()[1]) * 1000
        out.append({
            "id": cid[:12],
            "name": c.get("Names", ""),
            "image": c.get("Image", ""),
            "state": c.get("State", ""),
            "status": c.get("Status", ""),
            "ports": c.get("Ports", ""),
            "project": labels.get("com.docker.compose.project", ""),
            "memory": int(mem) if mem.isdigit() else None,
            "cpu_ns": usage,
            "cgroup": cg,
            "networks": c.get("Networks", ""),
        })
    return out, None


def listening():
    """Listening TCP sockets; pid is only visible for our own processes."""
    rc, stdout, _ = run(["ss", "-tlnpH"])
    out = []
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        addr, _, port = local.rpartition(":")
        m = re.search(r'\("([^"]*)",pid=(\d+)', line)
        out.append({
            "addr": addr, "port": int(port) if port.isdigit() else port,
            "process": m.group(1) if m else None,
            "pid": int(m.group(2)) if m else None,
        })
    return out


def processes(page_kb):
    out = []
    for d in glob.glob("/proc/[0-9]*"):
        stat = read(d + "/stat")
        if not stat:
            continue
        # comm may contain spaces/parens: split around the last ')'.
        head, _, tail = stat.rpartition(")")
        comm = head.partition("(")[2]
        f = tail.split()
        if len(f) < 22:
            continue
        try:
            uid = os.stat(d).st_uid
        except OSError:
            continue
        # f[0] is field 3 (state): utime=14, stime=15, starttime=22, rss=24.
        # /proc/<pid>/io is only readable for our own processes (or as root).
        io = {}
        for line in read(d + "/io").splitlines():
            k, _, v = line.partition(": ")
            io[k] = v
        rd, wr = io.get("read_bytes"), io.get("write_bytes")
        out.append([int(d[6:]), int(f[19]), comm, uid,
                    int(f[11]) + int(f[12]), int(f[21]) * page_kb,
                    int(rd) if rd else None, int(wr) if wr else None])
    return out


def is_loopback(addr):
    a = addr.rpartition(":")[0].strip("[]").partition("%")[0]
    return a.startswith("127.") or a == "::1" or a.startswith("::ffff:127.")


def tcp_sockets():
    """[local peer, cgroup, received, sent] per non-loopback TCP connection, from
    the kernel's per-socket byte counters; `--cgroup` names the owning cgroup for
    every socket without needing root. None when ss is too old for --cgroup."""
    rc, stdout, _ = run(["ss", "-tinH", "--cgroup"])
    if rc != 0:
        return None
    out, cur = [], None
    for line in stdout.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():  # header line: State Recv-Q Send-Q Local Peer [cgroup:...]
            parts = line.split()
            cg = next((p[7:] for p in parts if p.startswith("cgroup:")), None)
            cur = None
            if len(parts) >= 5 and cg and not is_loopback(parts[3]):
                cur = [parts[3] + " " + parts[4], cg, 0, 0]
                out.append(cur)
        elif cur is not None:
            m = re.search(r"\bbytes_received:(\d+)", line)
            cur[2] = int(m.group(1)) if m else 0
            m = re.search(r"\bbytes_acked:(\d+)", line)
            cur[3] = int(m.group(1)) if m else 0
    return out


def net_counters(path, physical_only):
    """[received, sent] bytes summed over the interfaces in a /proc/.../net/dev."""
    rx = tx = 0
    text = read(path)
    if not text:
        return None
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        f = rest.split()
        if name == "lo" or len(f) < 9:
            continue
        # Physical NICs only: tunnels, bridges and veths carry the same bytes again.
        if physical_only and not os.path.exists("/sys/class/net/%s/device" % name):
            continue
        rx += int(f[0])
        tx += int(f[8])
    return [rx, tx]


def disk_counters():
    """[read, written] bytes summed over physical block devices."""
    rd = wr = 0
    for line in read("/proc/diskstats").splitlines():
        f = line.split()
        # Whole physical disks only (partitions, dm, md, zram and loop would double count).
        if len(f) >= 10 and os.path.exists("/sys/block/%s/device" % f[2]):
            rd += int(f[5]) * 512
            wr += int(f[9]) * 512
    return [rd, wr]


def cgroup_io(path):
    """[read, written] bytes from a v2 cgroup's io.stat; None without io accounting."""
    text = read(CGROUP_ROOT + path + "/io.stat", None)
    if text is None:
        return None
    rd = wr = 0
    for line in text.splitlines():
        for field in line.split()[1:]:
            k, _, v = field.partition("=")
            if k == "rbytes":
                rd += int(v)
            elif k == "wbytes":
                wr += int(v)
    return [rd, wr]


def cgroup_pids(path):
    """Every pid in a cgroup subtree (v2 path, or under v1's systemd hierarchy)."""
    for root in (CGROUP_ROOT, CGROUP_ROOT + "/systemd"):
        if os.path.isdir(root + path):
            pids = set()
            for d, _, files in os.walk(root + path):
                if "cgroup.procs" in files:
                    pids.update(int(x) for x in read(d + "/cgroup.procs").split())
            return pids
    return set()


def cgroup_details(items, procs, clk_tck):
    """Member pids and io counters for every unit/container with a cgroup.

    Kernels booted with cgroup_disable=memory (Raspberry Pi OS default) report
    no cgroup memory: approximate it as the summed RSS of the member processes.
    CPU is filled the same way where cgroup accounting is missing."""
    by_pid = {p[0]: p for p in procs}
    for item in items:
        cg = item["cgroup"]
        item["pids"] = sorted(cgroup_pids(cg)) if cg else []
        item["io"] = cgroup_io(cg) if cg else None
        members = [by_pid[pid] for pid in item["pids"] if pid in by_pid]
        if not members:
            continue
        if item["memory"] is None:
            item["memory"] = sum(p[5] for p in members) * 1024
            item["memory_rss"] = True
        if item["cpu_ns"] is None:
            item["cpu_ns"] = sum(p[4] for p in members) * 10 ** 9 // clk_tck


def main():
    os.environ.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
    snap = {"version": 1, "mono": time.monotonic(), "time": time.time()}
    snap.update(host_info())
    snap["cpu"] = cpu_counters()
    snap["disk_io"] = disk_counters()
    snap["net_io"] = net_counters("/proc/net/dev", physical_only=True)
    snap["memory"] = memory()
    snap["temp"] = temperature()
    snap["disks"] = disks()
    snap["storage"] = storage(snap["disks"])
    snap["units"] = units("user") + units("system")
    snap["failed"] = failed_units("user") + failed_units("system")
    snap["containers"], snap["docker_error"] = containers()
    snap["listening"] = listening()
    snap["processes"] = processes(snap["page_kb"])
    snap["uids"] = {}
    for p in snap["processes"]:
        uid = p[3]
        if uid not in snap["uids"]:
            try:
                snap["uids"][uid] = pwd.getpwuid(uid).pw_name
            except KeyError:
                snap["uids"][uid] = str(uid)
    cgroup_details(snap["units"] + (snap["containers"] or []),
                   snap["processes"], snap["clk_tck"])
    for c in snap["containers"] or []:
        # Own network namespace: its interfaces count exactly this container's traffic.
        # Host-networked containers are covered by their sockets instead.
        c["net"] = None
        if c["pids"] and c["networks"] != "host":
            c["net"] = net_counters("/proc/%d/net/dev" % c["pids"][0], physical_only=False)
    snap["sockets"] = tcp_sockets()
    for s in snap["listening"]:
        owner = unit_of(s["pid"]) if s["pid"] else None
        if owner:
            s["scope"], s["unit"] = owner
    json.dump(snap, sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main()
