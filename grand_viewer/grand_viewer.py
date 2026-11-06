#!/usr/bin/env python3
"""grand_viewer — one web page showing every machine on the LAN.

The hub polls each configured host over ssh (agentless: probe.py is piped to
the remote `python3 -`), keeps the latest snapshot per host, turns counters
into rates, and serves a dashboard plus a small JSON API. Standard library only.
"""
import argparse
import ipaddress
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE_PATH = os.path.join(HERE, "probe.py")
INDEX_PATH = os.path.join(HERE, "index.html")
CONFIG_PATH = os.path.expanduser("~/.config/grand_viewer/config.json")
DEFAULTS = {"listen": "127.0.0.1", "port": 47800, "interval": 30, "allowed_hosts": []}
# A host that fell back to a secondary route goes back to its first route this often.
PREFERRED_RECHECK_INTERVAL = 300
PROBE_TIMEOUT = 40
TOP_PROCESSES = 12
LOG_LINES_MAX = 2000
DU_TIMEOUT = 900
DU_CACHE = 40           # finished scans kept per host
DU_PARALLEL = 2         # scans allowed to run at once per host
# Runs on the host as `python3 - PATH`: one level of `du`, plus which entries
# are directories (only those can be drilled into). Python 3.5-compatible.
DU_SCRIPT = r'''
import json, os, stat, subprocess, sys
root = sys.argv[1]
p = subprocess.Popen(["du", "-x", "-a", "-d1", "-B1", "-0", "--", root],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
out, err = p.communicate()
total, entries = None, []
for rec in out.split(b"\0"):
    size, _, path = rec.partition(b"\t")
    if not path:
        continue
    path = os.fsdecode(path)
    if path == root:
        total = int(size)
        continue
    try:
        is_dir = stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        is_dir = False
    name = os.path.basename(path)
    try:
        name.encode("utf-8")
        ok = True
    except UnicodeEncodeError:  # not valid UTF-8 on disk: show it, but can't address it
        name = name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
        ok = False
    entries.append({"name": name, "size": int(size), "dir": is_dir, "addressable": ok})
errors = [l for l in err.decode("utf-8", "replace").splitlines() if l.strip()]
json.dump({"total": total, "entries": entries, "errors": len(errors),
           "error_sample": errors[:5], "rc": p.returncode}, sys.stdout)
'''


def ssh_options():
    control_dir = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "grand_viewer")
    os.makedirs(control_dir, mode=0o700, exist_ok=True)
    return [
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=4",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
        # One multiplexed connection per route: polls and log fetches skip the handshake.
        "-o", "ControlMaster=auto",
        "-o", "ControlPath=" + os.path.join(control_dir, "%C"),
        "-o", "ControlPersist=300",
    ]


class Unreachable(Exception):
    pass


class Host:
    def __init__(self, cfg, ssh_opts):
        self.name = cfg["name"]
        self.local = bool(cfg.get("local"))
        routes = cfg.get("ssh", [])
        self.routes = [routes] if isinstance(routes, str) else list(routes)
        if not self.local and not self.routes:
            raise ValueError("host %r needs \"ssh\" (alias or list) or \"local\": true" % self.name)
        self.ssh_opts = ssh_opts
        self.route_idx = 0
        self.last_recheck = 0.0
        self.wake = threading.Event()
        self.lock = threading.Lock()
        # Guarded by lock:
        self.raw = None          # last raw probe output (for rate deltas)
        self.view = None         # processed snapshot served to the UI
        self.fetched_at = None   # hub clock when view was taken
        self.route = None
        self.error = None
        self.last_try = None
        self.took = None
        self.polling = False
        self.scans = {}          # path -> scan record (see disk_usage)

    # ------------------------------------------------------------- transport

    def execute(self, args, stdin=None, timeout=30, merge_stderr=False):
        """Run argv on the host. Returns (CompletedProcess, route); output is str
        with undecodable bytes replaced (filenames and logs need not be UTF-8)."""
        stderr = subprocess.STDOUT if merge_stderr else subprocess.PIPE
        data = stdin.encode() if isinstance(stdin, str) else stdin

        def finish(p):
            p.stdout = p.stdout.decode(errors="replace")
            if p.stderr is not None:
                p.stderr = p.stderr.decode(errors="replace")
            return p

        if self.local:
            p = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=stderr,
                               timeout=timeout)
            return finish(p), "local"

        now = time.monotonic()
        start = 0 if now - self.last_recheck > PREFERRED_RECHECK_INTERVAL else self.route_idx
        order = list(range(start, len(self.routes))) + list(range(start))
        errors = []
        for i in order:
            route = self.routes[i]
            cmd = ["ssh"] + self.ssh_opts + [route, shlex.join(args)]
            try:
                p = finish(subprocess.run(cmd, input=data, stdout=subprocess.PIPE,
                                          stderr=stderr, timeout=timeout))
            except subprocess.TimeoutExpired:
                errors.append("%s: timed out" % route)
                continue
            # 255 is ssh's own failure (connect/auth); anything else came from the remote.
            if p.returncode == 255 and not p.stdout.strip():
                msg = ((p.stderr or "").strip().splitlines() or ["ssh failed"])[-1]
                errors.append("%s: %s" % (route, msg))
                continue
            if start == 0:
                self.last_recheck = now
            self.route_idx = i
            return p, route
        raise Unreachable("; ".join(errors))

    # ----------------------------------------------------------------- polls

    def poll(self):
        with self.lock:
            self.polling = True
            self.last_try = time.time()
        t0 = time.monotonic()
        try:
            with open(PROBE_PATH) as f:
                probe = f.read()
            args = [sys.executable, PROBE_PATH] if self.local else ["python3", "-"]
            p, route = self.execute(args, stdin=None if self.local else probe,
                                    timeout=PROBE_TIMEOUT)
            if p.returncode != 0:
                tail = (p.stderr or "").strip().splitlines()[-3:]
                raise RuntimeError("probe exited %d: %s" % (p.returncode, " | ".join(tail)))
            raw = json.loads(p.stdout)
        except (Unreachable, RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as e:
            with self.lock:
                self.error = str(e) or e.__class__.__name__
                self.took = time.monotonic() - t0
                self.polling = False
            return False
        with self.lock:
            view = process(raw, self.raw)
            self.raw, self.view = raw, view
            self.fetched_at = time.time()
            self.route = route
            self.error = None
            self.took = time.monotonic() - t0
            self.polling = False
        return True

    def run_forever(self, interval):
        while True:
            ok = self.poll()
            # Rates need two samples: take the second one soon after the first.
            with self.lock:
                first = ok and self.view and self.view["cpu_pct"] is None
            self.wake.wait(3 if first else interval)
            self.wake.clear()

    def state(self):
        with self.lock:
            return {
                "name": self.name,
                "local": self.local,
                "routes": ["local"] if self.local else self.routes,
                "route": self.route,
                "error": self.error,
                "polling": self.polling,
                "last_try": self.last_try,
                "fetched_at": self.fetched_at,
                "took": self.took,
                "data": self.view,
            }

    # ------------------------------------------------------------------ logs

    def logs(self, kind, scope, name, lines):
        with self.lock:
            view = self.view
        if view is None:
            raise LookupError("no snapshot yet")
        if kind == "unit":
            known = {(u["scope"], u["id"]) for u in view["projects"]}
            known |= {(u["scope"], u["id"]) for u in view["failed"]}
            if (scope, name) not in known:
                raise LookupError("unknown unit")
            # --user-unit (not --user -u) also finds user-unit lines in the system
            # journal, which is the only place they land when journald is volatile.
            unit_flag = "--user-unit" if scope == "user" else "--unit"
            args = ["journalctl", unit_flag, name, "-n", str(lines), "--no-pager", "-o", "short-iso"]
        elif kind == "container":
            if name not in {c["name"] for c in view["containers"] or []}:
                raise LookupError("unknown container")
            args = ["docker", "logs", "--tail", str(lines), "--timestamps", name]
        else:
            raise LookupError("unknown log kind")
        p, _ = self.execute(args, timeout=20, merge_stderr=True)
        return p.stdout

    # ------------------------------------------------------------ disk usage

    def disk_usage(self, path, start=False):
        """Scan record for path; start=True launches a (re)scan unless one is running."""
        if not path.startswith("/") or "\0" in path or len(path) > 4096:
            raise ValueError("path must be absolute")
        path = os.path.normpath(path)
        if path.startswith("//"):
            path = "/" + path.lstrip("/")
        with self.lock:
            rec = self.scans.get(path)
            if start and (rec is None or rec["state"] != "running"):
                if sum(r["state"] == "running" for r in self.scans.values()) >= DU_PARALLEL:
                    raise LookupError("%d scans already running on %s" % (DU_PARALLEL, self.name))
                rec = {"path": path, "state": "running", "started": time.time()}
                self.scans[path] = rec
                done = [p for p, r in self.scans.items() if r["state"] != "running"]
                for p in sorted(done, key=lambda p: self.scans[p]["started"])[:-DU_CACHE]:
                    del self.scans[p]
                threading.Thread(target=self._scan, args=(rec,), daemon=True).start()
            return dict(rec) if rec else {"path": path, "state": "none"}

    def _scan(self, rec):
        exe = sys.executable if self.local else "python3"
        try:
            p, _ = self.execute([exe, "-", rec["path"]], stdin=DU_SCRIPT, timeout=DU_TIMEOUT)
            if p.returncode != 0:
                raise RuntimeError("scan failed: " + (p.stderr.strip().splitlines() or ["?"])[-1])
            result = json.loads(p.stdout)
            if result["total"] is None:
                raise RuntimeError("; ".join(result["error_sample"]) or "du printed nothing")
            result["entries"].sort(key=lambda e: e["size"], reverse=True)
            update = {"state": "done", "result": result}
        except (Unreachable, RuntimeError, ValueError, KeyError, OSError,
                subprocess.TimeoutExpired) as e:
            update = {"state": "error", "error": str(e) or e.__class__.__name__}
        with self.lock:
            rec.update(update, finished=time.time())


# --------------------------------------------------------------- processing

def per_sec(cur, old, dt):
    """Per-second rates of a [read, write]-style counter pair; None if unknown or reset."""
    if cur is None or old is None or dt <= 0 or any(c < o for c, o in zip(cur, old)):
        return None
    return [(c - o) / dt for c, o in zip(cur, old)]


def add(a, b):
    """Sum of two optional [x, y] pairs."""
    if a is None:
        return b
    return a if b is None else [a[0] + b[0], a[1] + b[1]]


def classify(unit, timer):
    active, sub = unit["active"], unit["sub"]
    if active == "failed":
        return "failed"
    if active in ("activating", "deactivating", "reloading"):
        return "running" if timer and active == "activating" else "busy"
    if active == "active":
        if unit["id"].endswith(".timer"):
            return "scheduled"
        return "running" if sub == "running" else "done"
    if unit["result"] not in ("", "success"):
        return "failed"
    if timer and timer["active"] == "active":
        return "scheduled"
    return "stopped"


def process(raw, prev):
    """Raw probe snapshot (+ the previous one) -> the view served to the UI.

    Every CPU figure is a percentage of the whole machine (all cores), so a
    project's share adds up to the host total; disk and network are bytes/s."""
    dt = 0.0
    if prev and raw["uptime"] > prev["uptime"]:  # same boot
        dt = raw["mono"] - prev["mono"]
    else:
        prev = None
    cpus = raw["cpus"]

    def cpu_share(cur_ns, old_ns):
        if cur_ns is None or old_ns is None or dt <= 0 or cur_ns < old_ns:
            return None
        return 100.0 * (cur_ns - old_ns) / (dt * 1e9) / cpus

    cpu_pct = None
    if prev:
        dtotal = raw["cpu"]["total"] - prev["cpu"]["total"]
        didle = raw["cpu"]["idle"] - prev["cpu"]["idle"]
        if dtotal > 0:
            cpu_pct = max(0.0, 100.0 * (1 - didle / dtotal))

    # Processes, keyed by (pid, start time) so a recycled pid never pairs up with
    # a stranger. One born since the previous sample counts in full.
    tck = raw["clk_tck"]
    prev_procs = {(p[0], p[1]): p for p in prev["processes"]} if prev else {}
    procs = []
    proc_io = {}
    for pid, start, comm, uid, ticks, rss_kb, rd, wr in raw["processes"]:
        cpu = io = None
        if prev:
            old = prev_procs.get((pid, start))
            if old is None and start / tck > prev["uptime"]:
                old = [pid, start, comm, uid, 0, 0, 0, 0]
            if old is not None and dt > 0:
                cpu = 100.0 * (ticks - old[4]) / tck / dt / cpus
                if rd is not None and old[6] is not None:
                    io = per_sec([rd, wr], [old[6], old[7]], dt)
        proc_io[pid] = io
        procs.append({"pid": pid, "name": comm, "user": raw["uids"].get(str(uid), str(uid)),
                      "cpu_pct": cpu, "rss": rss_kb * 1024, "disk": io})

    # TCP traffic per cgroup, from per-connection byte counters. A connection
    # first seen now was opened since the previous sample: all of it is new.
    net_by_cg = None
    if prev and raw["sockets"] is not None and prev["sockets"] is not None and dt > 0:
        old_socks = {(s[0], s[1]): s for s in prev["sockets"]}
        net_by_cg = {}
        for key, cg, rx, tx in raw["sockets"]:
            old = old_socks.get((key, cg), (key, cg, 0, 0))
            r = per_sec([rx, tx], [old[2], old[3]], dt)
            if r:
                net_by_cg[cg] = add(net_by_cg.get(cg), r)

    def cgroup_net(cg):
        if net_by_cg is None:
            return None
        total = [0.0, 0.0]
        for path, r in net_by_cg.items():
            if cg and (path == cg or path.startswith(cg + "/")):
                total = add(total, r)
        return total

    def cgroup_disk(item, old):
        """cgroup io.stat when the io controller covers it, else the summed
        /proc/<pid>/io of its processes (readable for our own processes)."""
        if item["io"] is not None:
            return per_sec(item["io"], old and old["io"], dt)
        total = None
        for pid in item["pids"]:
            total = add(total, proc_io.get(pid))
        return total

    def running(item):
        return bool(item["pids"]) and prev is not None

    # Ports owned by a unit's cgroup.
    ports = {}
    other_ports = []
    for s in raw["listening"]:
        key = (s.get("scope"), s.get("unit"))
        ports.setdefault(key, [])
        if s.get("unit"):
            ports[key].append({"addr": s["addr"], "port": s["port"]})
        else:
            other_ports.append(s)

    prev_units = {(u["scope"], u["id"]): u for u in prev["units"]} if prev else {}
    by_id = {(u["scope"], u["id"]): u for u in raw["units"]}
    projects = []
    for u in raw["units"]:
        key = (u["scope"], u["id"])
        timer = None
        for t in u["triggered_by"]:
            timer = by_id.get((u["scope"], t)) or timer
        if u["id"].endswith(".timer") and any((u["scope"], t) in by_id for t in u["triggers"]):
            continue  # shown on the service it triggers
        p = {k: v for k, v in u.items() if k not in ("pids", "io", "cgroup")}
        p["timer"] = None if timer is None else {
            "id": timer["id"], "active": timer["active"],
            "next_run": timer["next_run"], "last_trigger": timer["last_trigger"]}
        if u["id"].endswith(".timer"):
            p["timer"] = {"id": u["id"], "active": u["active"],
                          "next_run": u["next_run"], "last_trigger": u["last_trigger"]}
        p["status"] = classify(u, p["timer"])
        old = prev_units.get(key)
        p["cpu_pct"] = cpu_share(u["cpu_ns"], old and old["cpu_ns"])
        p["disk"] = cgroup_disk(u, old) if running(u) else None
        p["net"] = cgroup_net(u["cgroup"]) if running(u) else None
        seen = set()
        p["ports"] = []
        for port in ports.get(key, []):
            if port["port"] not in seen:
                seen.add(port["port"])
                p["ports"].append(port)
        projects.append(p)

    containers = None
    if raw["containers"] is not None:
        prev_c = {c["id"]: c for c in prev["containers"] or []} if prev else {}
        containers = []
        for c in raw["containers"]:
            old = prev_c.get(c["id"])
            v = {k: val for k, val in c.items() if k not in ("pids", "io", "cgroup", "net")}
            v["cpu_pct"] = cpu_share(c["cpu_ns"], old and old["cpu_ns"])
            v["disk"] = cgroup_disk(c, old) if running(c) else None
            v["net"] = None
            if running(c):
                # Own network namespace: its interface counters; host network: its sockets.
                v["net"] = (per_sec(c["net"], old and old["net"], dt) if c["net"] is not None
                            else cgroup_net(c["cgroup"]))
            containers.append(v)

    def top(key):
        return sorted(procs, key=key, reverse=True)[:TOP_PROCESSES]

    picked = {}
    for p in (top(lambda p: p["cpu_pct"] or 0) + top(lambda p: p["rss"])
              + top(lambda p: sum(p["disk"]) if p["disk"] else 0)):
        picked[p["pid"]] = p

    project_ids = {(p["scope"], p["id"]) for p in projects}
    return {
        "hostname": raw["hostname"], "os": raw["os"], "kernel": raw["kernel"],
        "arch": raw["arch"], "user": raw["user"], "cpus": cpus,
        "uptime": raw["uptime"], "load": raw["load"], "time": raw["time"],
        "cpu_pct": cpu_pct, "memory": raw["memory"], "temp": raw["temp"],
        "disk_io": per_sec(raw["disk_io"], prev and prev["disk_io"], dt),
        "net_io": per_sec(raw["net_io"], prev and prev["net_io"], dt),
        "tcp_accounting": raw["sockets"] is not None,
        "disks": raw["disks"],
        "storage": raw["storage"],
        "projects": projects,
        "failed": [f for f in raw["failed"] if (f["scope"], f["id"]) not in project_ids],
        "containers": containers,
        "docker_error": raw["docker_error"],
        "other_ports": other_ports,
        "processes": list(picked.values()),
        "process_count": len(procs),
    }


# --------------------------------------------------------------------- http

class App:
    def __init__(self, config):
        self.config = config
        opts = ssh_options()
        self.hosts = [Host(h, opts) for h in config["hosts"]]
        self.by_name = {h.name: h for h in self.hosts}
        if len(self.by_name) != len(self.hosts):
            raise ValueError("host names must be unique")
        names = {"localhost", socket.gethostname().lower(), socket.gethostname().lower() + ".local"}
        self.allowed_hosts = names | {h.lower() for h in config["allowed_hosts"]}

    def start(self):
        for h in self.hosts:
            threading.Thread(target=h.run_forever, args=(self.config["interval"],),
                             name="poll-" + h.name, daemon=True).start()

    def host_header_ok(self, value):
        """Block DNS rebinding: only IP literals and known names may address us."""
        if not value:
            return False
        host = value.rsplit("]", 1)[0].lstrip("[") if value.startswith("[") else value.rsplit(":", 1)[0]
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return host.lower() in self.allowed_hosts


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "grand_viewer"

        def log_message(self, fmt, *args):
            pass

        def send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def guard(self):
            if not app.host_header_ok(self.headers.get("Host")):
                self.send(403, json.dumps({"error": "unexpected Host header (add it to allowed_hosts)"}))
                return False
            # Other sites open in the browser may POST here blind (CSRF): browsers
            # always send Origin on cross-origin POSTs, so it must name this page.
            origin = self.headers.get("Origin")
            if self.command == "POST" and origin and urlparse(origin).netloc != self.headers["Host"]:
                self.send(403, json.dumps({"error": "cross-origin request refused"}))
                return False
            return True

        def do_GET(self):
            if not self.guard():
                return
            url = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            if url.path == "/":
                with open(INDEX_PATH, "rb") as f:
                    self.send(200, f.read(), "text/html")
            elif url.path == "/api/state":
                self.send(200, json.dumps({
                    "now": time.time(),
                    "interval": app.config["interval"],
                    "hosts": [h.state() for h in app.hosts],
                }))
            elif url.path == "/api/logs":
                host = app.by_name.get(q.get("host", ""))
                if host is None:
                    return self.send(404, json.dumps({"error": "unknown host"}))
                try:
                    lines = max(1, min(int(q.get("lines", 300)), LOG_LINES_MAX))
                    text = host.logs(q.get("kind", ""), q.get("scope", ""), q.get("name", ""), lines)
                except (LookupError, ValueError) as e:
                    return self.send(400, json.dumps({"error": str(e)}))
                except (Unreachable, OSError, subprocess.TimeoutExpired) as e:
                    return self.send(502, json.dumps({"error": str(e)}))
                self.send(200, json.dumps({"text": text}))
            elif url.path == "/api/du":
                self.disk_usage(q, start=False)
            else:
                self.send(404, json.dumps({"error": "not found"}))

        def disk_usage(self, q, start):
            host = app.by_name.get(q.get("host", ""))
            if host is None:
                return self.send(404, json.dumps({"error": "unknown host"}))
            try:
                self.send(200, json.dumps(host.disk_usage(q.get("path", ""), start=start)))
            except ValueError as e:
                self.send(400, json.dumps({"error": str(e)}))
            except LookupError as e:
                self.send(429, json.dumps({"error": str(e)}))

        def do_POST(self):
            if not self.guard():
                return
            url = urlparse(self.path)
            if url.path == "/api/refresh":
                for h in app.hosts:
                    h.wake.set()
                self.send(200, json.dumps({"ok": True}))
            elif url.path == "/api/du":
                self.disk_usage({k: v[-1] for k, v in parse_qs(url.query).items()}, start=True)
            else:
                self.send(404, json.dumps({"error": "not found"}))

    return Handler


def load_config(path):
    if os.path.exists(path):
        with open(path) as f:
            cfg = json.load(f)
    else:
        print("grand_viewer: %s missing, watching this machine only" % path, file=sys.stderr)
        cfg = {"hosts": [{"name": socket.gethostname(), "local": True}]}
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    if not cfg.get("hosts"):
        raise ValueError("config has no hosts")
    return cfg


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--once", action="store_true",
                    help="poll every host once, print the result as JSON, and exit")
    args = ap.parse_args()
    config = load_config(args.config)
    app = App(config)

    if args.once:
        threads = [threading.Thread(target=h.poll) for h in app.hosts]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        json.dump([h.state() for h in app.hosts], sys.stdout, indent=1)
        print()
        return

    app.start()
    server = ThreadingHTTPServer((config["listen"], config["port"]), make_handler(app))
    server.daemon_threads = True
    print("grand_viewer: http://%s:%d/" % (config["listen"], config["port"]), file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
