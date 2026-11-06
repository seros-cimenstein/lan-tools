#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = "~/.config/lanclip/config.json"
DEFAULT_LISTEN_PORT = 47700
DEFAULT_POLL_INTERVAL = 0.7
DEFAULT_MAX_BYTES = 10_000_000
READ_TIMEOUT = 5
WRITE_TIMEOUT = 5
POST_TIMEOUT = 3
# Liveness probes run on a 2s loop and may try several candidate addresses,
# so they use a tighter timeout than clip delivery to stay responsive.
PING_TIMEOUT = 1.0
PROBE_INTERVAL = 2.0
# How often to re-test a peer from the top of its address list, so that a peer
# which failed over to a later address (e.g. wifi) returns to the preferred
# earlier one (e.g. ethernet) once that recovers.
PREFERRED_RECHECK_INTERVAL = 60.0
FAILURE_LOG_INTERVAL = 60
TEXT_MIME = "text/plain"
IMAGE_MIMES = ("image/png", "image/jpeg")
SUPPORTED_MIMES = {TEXT_MIME, *IMAGE_MIMES}
PLAIN_TEXT_TYPES = {"text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING", "TEXT"}


LOGGER = logging.getLogger("lanclip")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Peer:
    name: str
    hosts: tuple[str, ...]
    port: int

    @property
    def host(self) -> str:
        return self.hosts[0]


@dataclass(frozen=True)
class Config:
    name: str
    listen_port: int
    token: str
    peers: list[Peer]
    poll_interval: float
    max_bytes: int


class ClipboardBackend:
    name = "clipboard"

    def read(self) -> tuple[str, bytes] | None:
        raise NotImplementedError

    def write(self, mime: str, data: bytes) -> bool:
        raise NotImplementedError


def run_clipboard_bytes(args: list[str]) -> bytes | None:
    try:
        completed = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=READ_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if completed.returncode != 0:
        return None

    return completed.stdout


def run_clipboard_text(args: list[str], encoding: str = "utf-8") -> str | None:
    data = run_clipboard_bytes(args)
    if data is None:
        return None
    try:
        return data.decode(encoding)
    except UnicodeDecodeError:
        return None


def parse_clipboard_types(data: bytes) -> set[str] | None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return {line.strip() for line in text.splitlines() if line.strip()}


def write_with_run(args: list[str], data: bytes) -> bool:
    try:
        completed = subprocess.run(
            args,
            input=data,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=WRITE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def write_with_xclip(args: list[str], data: bytes) -> bool:
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False

    try:
        process.communicate(input=data, timeout=WRITE_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=1)
        except Exception:
            pass
        return False

    return process.returncode == 0


class WaylandClipboard(ClipboardBackend):
    name = "wayland"

    def read(self) -> tuple[str, bytes] | None:
        listed = run_clipboard_bytes(["wl-paste", "--list-types"])
        if listed is None:
            return None
        offered = parse_clipboard_types(listed)
        if offered is None:
            return None

        if offered & PLAIN_TEXT_TYPES:
            text = run_clipboard_text(["wl-paste", "--no-newline", "--type", "text"])
            if text:
                return TEXT_MIME, text.encode("utf-8")

        for mime in IMAGE_MIMES:
            if mime in offered:
                data = run_clipboard_bytes(["wl-paste", "--type", mime])
                return (mime, data) if data is not None else None

        return None

    def write(self, mime: str, data: bytes) -> bool:
        if mime == TEXT_MIME:
            return write_with_run(["wl-copy"], data)
        if mime in IMAGE_MIMES:
            return write_with_run(["wl-copy", "--type", mime], data)
        return False


class X11Clipboard(ClipboardBackend):
    name = "x11"

    def read(self) -> tuple[str, bytes] | None:
        targets = run_clipboard_bytes(["xclip", "-selection", "clipboard", "-o", "-t", "TARGETS"])
        if targets is None:
            return None
        offered = parse_clipboard_types(targets)
        if offered is None:
            return None

        if offered & PLAIN_TEXT_TYPES:
            text = self.read_text(offered)
            if text:
                return TEXT_MIME, text.encode("utf-8")

        for mime in IMAGE_MIMES:
            if mime in offered:
                data = run_clipboard_bytes(["xclip", "-selection", "clipboard", "-o", "-t", mime])
                return (mime, data) if data is not None else None

        return None

    def read_text(self, offered: set[str]) -> str | None:
        for target, encoding in (
            ("UTF8_STRING", "utf-8"),
            ("STRING", "latin-1"),
            ("TEXT", "utf-8"),
            ("text/plain;charset=utf-8", "utf-8"),
            ("text/plain", "utf-8"),
        ):
            if target not in offered:
                continue
            text = run_clipboard_text(
                ["xclip", "-selection", "clipboard", "-o", "-t", target],
                encoding,
            )
            if text is not None:
                return text
        return None

    def write(self, mime: str, data: bytes) -> bool:
        if mime == TEXT_MIME:
            return write_with_xclip(["xclip", "-selection", "clipboard", "-i"], data)
        if mime in IMAGE_MIMES:
            return write_with_xclip(
                ["xclip", "-selection", "clipboard", "-i", "-t", mime],
                data,
            )
        return False


_WAYLAND_BACKEND = WaylandClipboard()
_X11_BACKEND = X11Clipboard()


class SharedState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_seen_hash: str | None = None
        self.last_remote_hash: str | None = None
        self.last_failure_log: dict[str, float] = {}
        self.peer_reachable: dict[str, bool] = {}
        self.peer_last_seen: dict[str, float] = {}
        self.last_source: str | None = None
        self.last_source_at: float | None = None
        self.peer_active_host: dict[str, str] = {}

    def get_active_host(self, peer_name: str) -> str | None:
        with self.lock:
            return self.peer_active_host.get(peer_name)

    def set_active_host(self, peer_name: str, host: str) -> bool:
        """Remember the address that answered. Returns True if it changed."""
        with self.lock:
            previous = self.peer_active_host.get(peer_name)
            if previous == host:
                return False
            self.peer_active_host[peer_name] = host
            return previous is not None

    def mark_remote(self, content_hash: str) -> None:
        with self.lock:
            self.last_remote_hash = content_hash
            self.last_seen_hash = content_hash

    def mark_seen_if_changed(self, content_hash: str) -> tuple[bool, str | None]:
        with self.lock:
            if content_hash == self.last_seen_hash:
                return False, self.last_remote_hash
            self.last_seen_hash = content_hash
            return True, self.last_remote_hash

    def should_log_peer_failure(self, peer_name: str) -> bool:
        now = time.monotonic()
        with self.lock:
            last = self.last_failure_log.get(peer_name, 0)
            if now - last < FAILURE_LOG_INTERVAL:
                return False
            self.last_failure_log[peer_name] = now
            return True

    def set_peer_reachable(self, peer_name: str, reachable: bool) -> None:
        with self.lock:
            self.peer_reachable[peer_name] = reachable
            if reachable:
                self.peer_last_seen[peer_name] = time.time()

    def mark_source(self, name: str) -> None:
        with self.lock:
            self.last_source = name
            self.last_source_at = time.time()

    def status_snapshot(self, peer_names: list[str]) -> dict:
        with self.lock:
            return {
                "last_source": self.last_source,
                "last_source_at": self.last_source_at,
                "peers": {
                    peer_name: {
                        "reachable": self.peer_reachable.get(peer_name, False),
                        "last_seen": self.peer_last_seen.get(peer_name),
                        "host": self.peer_active_host.get(peer_name),
                    }
                    for peer_name in peer_names
                },
            }


class LanclipApp:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.state = SharedState()
        self.clipboard_lock = threading.Lock()

    def read_clipboard(self) -> tuple[str, bytes] | None:
        with self.clipboard_lock:
            backend = current_backend()
            return backend.read()

    def set_clipboard_from_remote(self, mime: str, data: bytes) -> bool:
        content_hash = bytes_hash(data)
        with self.clipboard_lock:
            backend = current_backend()
            if not backend.write(mime, data):
                return False
            self.state.mark_remote(content_hash)
            return True


class LanclipHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], app: LanclipApp) -> None:
        self.app = app
        super().__init__(server_address, LanclipHandler)


class LanclipHandler(BaseHTTPRequestHandler):
    server: LanclipHTTPServer

    def log_message(self, _format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path not in {"/ping", "/status"}:
            self.send_empty(HTTPStatus.NOT_FOUND)
            return

        if not self.authorized():
            self.send_empty(HTTPStatus.UNAUTHORIZED)
            return

        if path == "/ping":
            body = self.server.app.config.name.encode("utf-8")
            content_type = "text/plain; charset=utf-8"
        else:
            body = json.dumps(build_status_payload(self.server.app)).encode("utf-8")
            content_type = "application/json; charset=utf-8"

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path != "/clip":
            self.send_empty(HTTPStatus.NOT_FOUND)
            return

        if not self.authorized():
            self.send_empty(HTTPStatus.UNAUTHORIZED)
            return

        content_length = self.headers.get("Content-Length")
        if content_length is None:
            self.send_empty(HTTPStatus.LENGTH_REQUIRED)
            return

        try:
            length = int(content_length)
        except ValueError:
            self.send_empty(HTTPStatus.BAD_REQUEST)
            return

        if length < 0:
            self.send_empty(HTTPStatus.BAD_REQUEST)
            return
        if length > encoded_body_limit(self.server.app.config.max_bytes):
            self.send_empty(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return

        try:
            self.connection.settimeout(READ_TIMEOUT)
            body = self.rfile.read(length)
            payload = json.loads(body.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            self.send_empty(HTTPStatus.BAD_REQUEST)
            return

        if not isinstance(payload, dict):
            self.send_empty(HTTPStatus.BAD_REQUEST)
            return

        peer_name = payload.get("name")
        mime = payload.get("mime")
        encoded_data = payload.get("data")
        if (
            not isinstance(peer_name, str)
            or not isinstance(mime, str)
            or not isinstance(encoded_data, str)
        ):
            self.send_empty(HTTPStatus.BAD_REQUEST)
            return

        if mime not in SUPPORTED_MIMES:
            self.send_empty(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return

        try:
            data = base64.b64decode(encoded_data, validate=True)
        except (binascii.Error, ValueError):
            self.send_empty(HTTPStatus.BAD_REQUEST)
            return

        if len(data) > self.server.app.config.max_bytes:
            self.send_empty(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return

        if not self.server.app.set_clipboard_from_remote(mime, data):
            LOGGER.warning("clip <- %s failed: could not set local clipboard", peer_name)
            self.send_empty(HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self.server.app.state.mark_source(peer_name)
        self.send_empty(HTTPStatus.NO_CONTENT)
        LOGGER.info("clip <- %s (%s, %d bytes)", peer_name, mime, len(data))

    def authorized(self) -> bool:
        supplied = self.headers.get("X-Lanclip-Token", "")
        expected = self.server.app.config.token
        supplied_bytes = supplied.encode("utf-8", "surrogatepass")
        expected_bytes = expected.encode("utf-8", "surrogatepass")
        return hmac.compare_digest(supplied_bytes, expected_bytes)

    def send_empty(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


def bytes_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded_body_limit(max_bytes: int) -> int:
    return ((max_bytes + 2) // 3) * 4 + 4096


def current_backend() -> ClipboardBackend:
    if os.environ.get("WAYLAND_DISPLAY"):
        return _WAYLAND_BACKEND
    return _X11_BACKEND


def build_status_payload(app: LanclipApp) -> dict[str, Any]:
    peer_names = [peer.name for peer in app.config.peers]
    snapshot = app.state.status_snapshot(peer_names)
    return {"name": app.config.name, **snapshot}


def load_config(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"config missing: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"could not read config {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in config {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")

    name = raw.get("name")
    token = raw.get("token")
    peers_raw = raw.get("peers")
    listen_port = raw.get("listen_port", DEFAULT_LISTEN_PORT)
    poll_interval = raw.get("poll_interval", DEFAULT_POLL_INTERVAL)
    max_bytes = raw.get("max_bytes", DEFAULT_MAX_BYTES)

    if not isinstance(name, str) or not name.strip():
        raise ConfigError("config key 'name' must be a non-empty string")
    if not isinstance(token, str) or not token.strip():
        raise ConfigError("config key 'token' must be a non-empty string")
    if not isinstance(peers_raw, list):
        raise ConfigError("config key 'peers' must be a list")

    peers = [parse_peer(item, index) for index, item in enumerate(peers_raw)]
    return Config(
        name=name,
        listen_port=parse_port(listen_port, "listen_port"),
        token=token,
        peers=peers,
        poll_interval=parse_positive_float(poll_interval, "poll_interval"),
        max_bytes=parse_positive_int(max_bytes, "max_bytes"),
    )


def parse_peer(value: Any, index: int) -> Peer:
    if not isinstance(value, dict):
        raise ConfigError(f"peer #{index + 1} must be a JSON object")

    name = value.get("name")
    port = value.get("port")

    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"peer #{index + 1} key 'name' must be a non-empty string")

    # A peer may be given as a single "host" or as a "hosts" list of candidate
    # addresses (eth IP, wifi IP, mDNS name, ...). Every candidate is tried
    # until one answers, so a DHCP lease change no longer breaks the peer.
    hosts: list[str] = []
    for key in ("host", "hosts"):
        raw = value.get(key)
        if raw is None:
            continue
        items = raw if isinstance(raw, list) else [raw]
        if not isinstance(raw, (str, list)):
            raise ConfigError(
                f"peer #{index + 1} key '{key}' must be a string or a list of strings"
            )
        for item in items:
            if not isinstance(item, str) or not item.strip():
                raise ConfigError(
                    f"peer #{index + 1} key '{key}' must contain non-empty strings"
                )
            candidate = item.strip()
            if candidate not in hosts:
                hosts.append(candidate)

    if not hosts:
        raise ConfigError(
            f"peer #{index + 1} needs a 'host' string or a non-empty 'hosts' list"
        )

    return Peer(
        name=name,
        hosts=tuple(hosts),
        port=parse_port(port, f"peer #{index + 1} port"),
    )


def parse_port(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not (1 <= value <= 65535):
        raise ConfigError(f"config key '{label}' must be an integer from 1 to 65535")
    return value


def parse_positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"config key '{label}' must be a positive integer")
    return value


def parse_positive_float(value: Any, label: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"config key '{label}' must be a positive number")
    return float(value)


def poll_clipboard(app: LanclipApp, stop_event: threading.Event) -> None:
    # Establish the startup baseline immediately. Waiting for the first poll
    # would discard a copy made during that interval as pre-existing content.
    initial_item = app.read_clipboard()
    if initial_item is not None:
        initial_mime, initial_data = initial_item
        if initial_mime in SUPPORTED_MIMES:
            app.state.mark_seen_if_changed(bytes_hash(initial_data))

    while not stop_event.wait(app.config.poll_interval):
        item = app.read_clipboard()
        if item is None:
            continue

        mime, data = item
        if mime not in SUPPORTED_MIMES:
            continue
        content_hash = bytes_hash(data)

        changed, remote_hash = app.state.mark_seen_if_changed(content_hash)

        if not changed:
            continue

        if not data:
            continue
        if content_hash == remote_hash:
            continue
        if len(data) > app.config.max_bytes:
            LOGGER.info(
                "clip too large; skipping (%s, %d bytes > %d max)",
                mime,
                len(data),
                app.config.max_bytes,
            )
            continue

        app.state.mark_source(app.config.name)
        broadcast(app, mime, data)


def broadcast(app: LanclipApp, mime: str, data: bytes) -> None:
    for peer in app.config.peers:
        thread = threading.Thread(
            target=broadcast_to_peer,
            args=(app, peer, mime, data),
            name=f"lanclip-send-{peer.name}",
            daemon=True,
        )
        thread.start()


def candidate_hosts(
    app: LanclipApp, peer: Peer, from_top: bool = False
) -> list[str]:
    """Peer addresses to try, most-recently-working one first.

    With from_top, config order is kept instead, so the preferred address
    (listed first) wins again as soon as it comes back.
    """
    hosts = list(peer.hosts)
    if from_top:
        return hosts
    active = app.state.get_active_host(peer.name)
    if active in hosts:
        hosts.remove(active)
        hosts.insert(0, active)
    return hosts


def note_active_host(app: LanclipApp, peer: Peer, host: str) -> None:
    if app.state.set_active_host(peer.name, host):
        LOGGER.info("peer %s now reachable at %s", peer.name, host)


def broadcast_to_peer(app: LanclipApp, peer: Peer, mime: str, data: bytes) -> None:
    body = json.dumps(
        {
            "name": app.config.name,
            "mime": mime,
            "data": base64.b64encode(data).decode("ascii"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    last_error: Exception | None = None
    for host in candidate_hosts(app, peer):
        request = urllib.request.Request(
            f"http://{host}:{peer.port}/clip",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-Lanclip-Token": app.config.token,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=POST_TIMEOUT) as response:
                if not (200 <= response.status < 300):
                    raise urllib.error.URLError(f"HTTP {response.status}")
        except Exception as exc:
            last_error = exc
            continue

        note_active_host(app, peer, host)
        LOGGER.info("clip -> %s (%s, %d bytes)", peer.name, mime, len(data))
        return

    if app.state.should_log_peer_failure(peer.name):
        LOGGER.warning(
            "clip -> %s failed on all of %s: %s",
            peer.name,
            ", ".join(peer.hosts),
            last_error,
        )


def ping_peer(app: LanclipApp, peer: Peer, from_top: bool = False) -> bool:
    for host in candidate_hosts(app, peer, from_top=from_top):
        request = urllib.request.Request(
            f"http://{host}:{peer.port}/ping",
            headers={"X-Lanclip-Token": app.config.token},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=PING_TIMEOUT) as response:
                if not (200 <= response.status < 300):
                    continue
        except Exception:
            continue

        note_active_host(app, peer, host)
        return True

    return False


def probe_peers(app: LanclipApp, stop_event: threading.Event) -> None:
    next_recheck = time.monotonic() + PREFERRED_RECHECK_INTERVAL
    while not stop_event.wait(PROBE_INTERVAL):
        now = time.monotonic()
        # Periodically retry from the top of each address list so a peer that
        # fell back to a later address returns to the preferred one.
        from_top = now >= next_recheck
        if from_top:
            next_recheck = now + PREFERRED_RECHECK_INTERVAL
        for peer in app.config.peers:
            app.state.set_peer_reachable(
                peer.name, ping_peer(app, peer, from_top=from_top)
            )


def serve_http(server: LanclipHTTPServer) -> None:
    try:
        server.serve_forever(poll_interval=0.5)
    except Exception:
        LOGGER.exception("HTTP server stopped unexpectedly")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LAN universal clipboard daemon")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"config path (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument("--debug", action="store_true", help="enable debug logging")
    return parser.parse_args(argv)


def configure_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.debug)

    config_path = Path(args.config).expanduser()
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"lanclip: {exc}", file=sys.stderr)
        return 2

    app = LanclipApp(config)
    stop_event = threading.Event()

    def stop(signum: int, _frame: Any) -> None:
        LOGGER.info("received signal %d, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    server = LanclipHTTPServer(("0.0.0.0", config.listen_port), app)
    server_thread = threading.Thread(target=serve_http, args=(server,), name="lanclip-http")
    poll_thread = threading.Thread(
        target=poll_clipboard,
        args=(app, stop_event),
        name="lanclip-poll",
    )
    probe_thread = threading.Thread(
        target=probe_peers,
        args=(app, stop_event),
        name="lanclip-probe",
        daemon=True,
    )

    LOGGER.info(
        "starting lanclip as %s on port %d "
        "(backend re-evaluated each cycle; initial guess: %s)",
        config.name,
        config.listen_port,
        current_backend().name,
    )

    server_thread.start()
    poll_thread.start()
    probe_thread.start()

    try:
        while not stop_event.wait(1):
            pass
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()
        poll_thread.join(timeout=READ_TIMEOUT + 1)
        server_thread.join(timeout=1)
        probe_thread.join(timeout=1)
        LOGGER.info("stopped")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
