#!/usr/bin/env python3
"""LAN mouse forwarding tool for Linux evdev/uinput."""

from __future__ import annotations

import argparse
import atexit
import errno
import fcntl
import glob
import hmac
import json
import logging
import os
import select
import signal
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


CONFIG_PATH = "/etc/lanmouse/config.json"
DEFAULT_SETTLE_SECONDS = 0.5
PRESENCE_ARBITER_INTERVAL = 0.1
CONNECT_FAILSAFE_SECONDS = 3.0

EV_SYN = 0
EV_KEY = 1
EV_REL = 2

REL_X = 0
REL_Y = 1
REL_HWHEEL = 6
REL_WHEEL = 8
REL_WHEEL_HI_RES = 11
REL_HWHEEL_HI_RES = 12
ENABLED_REL_CODES = {
    REL_X,
    REL_Y,
    REL_HWHEEL,
    REL_WHEEL,
    REL_WHEEL_HI_RES,
    REL_HWHEEL_HI_RES,
}

BTN_MOUSE_FIRST = 0x110
BTN_MOUSE_LAST = 0x117
ENABLED_BTN_CODES = set(range(BTN_MOUSE_FIRST, BTN_MOUSE_LAST + 1))

WIRE_EVENT_FORMAT = "<HHi"
WIRE_EVENT_SIZE = struct.calcsize(WIRE_EVENT_FORMAT)
INPUT_EVENT_FORMAT = "llHHi"
INPUT_EVENT_SIZE = struct.calcsize(INPUT_EVENT_FORMAT)

UINPUT_NAME = "lanmouse virtual mouse"
UINPUT_MAX_NAME_SIZE = 80
BUS_VIRTUAL = 0x06

UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_DEV_SETUP = 0x405C5503


def _ioc(direction: int, ioctl_type: int, number: int, size: int) -> int:
    nr_bits = 8
    type_bits = 8
    size_bits = 14

    nr_shift = 0
    type_shift = nr_shift + nr_bits
    size_shift = type_shift + type_bits
    dir_shift = size_shift + size_bits
    return (
        (direction << dir_shift)
        | (ioctl_type << type_shift)
        | (number << nr_shift)
        | (size << size_shift)
    )


def _iow(ioctl_type: str, number: int, size: int) -> int:
    return _ioc(1, ord(ioctl_type), number, size)


EVIOCGRAB = _iow("E", 0x90, struct.calcsize("i"))


class ConfigError(Exception):
    pass


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def require_string(config: dict[str, Any], key: str, *, nonempty: bool = True) -> str:
    value = config.get(key)
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string")
    if nonempty and not value:
        raise ConfigError(f"{key} must not be empty")
    return value


def require_port(config: dict[str, Any], key: str, default: int | None = None) -> int:
    value = config.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{key} must be an integer port")
    if value < 1 or value > 65535:
        raise ConfigError(f"{key} must be in 1..65535")
    return value


def require_seconds(config: dict[str, Any], key: str, default: float) -> float:
    value = config.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(f"{key} must be a number of seconds")
    seconds = float(value)
    if seconds < 0:
        raise ConfigError(f"{key} must be >= 0")
    return seconds


def load_config(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("config root must be an object")

    role = require_string(raw, "role")
    if role not in {"sender", "receiver"}:
        raise ConfigError("role must be 'sender' or 'receiver'")
    require_string(raw, "name")
    require_string(raw, "token")
    require_string(raw, "keyboard_match")

    if role == "sender":
        require_string(raw, "mouse_match")
        require_port(raw, "control_port", 47701)
        require_seconds(raw, "settle_seconds", DEFAULT_SETTLE_SECONDS)
        receivers = raw.get("receivers")
        if not isinstance(receivers, list) or not receivers:
            raise ConfigError("receivers must be a non-empty list")
        seen_names: set[str] = set()
        for index, item in enumerate(receivers):
            if not isinstance(item, dict):
                raise ConfigError(f"receivers[{index}] must be an object")
            receiver_name = require_string(item, "name")
            if receiver_name == "local":
                raise ConfigError("receiver name 'local' is reserved")
            if receiver_name in seen_names:
                raise ConfigError(f"duplicate receiver name {receiver_name!r}")
            seen_names.add(receiver_name)
            require_string(item, "host")
            require_port(item, "port")
    else:
        require_port(raw, "listen_port", 47702)
        require_string(raw, "sender_host")
        require_port(raw, "sender_control_port", 47701)

    return raw


def keyboard_present(match: str) -> bool:
    try:
        with open("/proc/bus/input/devices", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("N: Name=") and match in line:
                    return True
    except OSError as exc:
        logging.warning("cannot read /proc/bus/input/devices: %s", exc)
    return False


def find_event_device(name_match: str) -> str | None:
    for name_path in sorted(glob.glob("/sys/class/input/event*/device/name")):
        try:
            with open(name_path, "r", encoding="utf-8", errors="replace") as fh:
                name = fh.read().strip()
        except OSError:
            continue
        if name_match in name:
            event_name = os.path.basename(os.path.dirname(os.path.dirname(name_path)))
            return os.path.join("/dev/input", event_name)
    return None


def safe_close(sock: socket.socket | None) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


class SenderState:
    def __init__(self, receiver_names: set[str]) -> None:
        self._lock = threading.Lock()
        self._target = "local"
        self._mouse_found = False
        self._presence = {"local": False}
        for name in sorted(receiver_names):
            self._presence[name] = False
        self._presence_changed_at = time.monotonic()

    def get_target(self) -> str:
        with self._lock:
            return self._target

    def set_target(self, target: str) -> None:
        with self._lock:
            if self._target == target:
                return
            self._target = target
        logging.info("target -> %s", target)

    def set_mouse_found(self, found: bool) -> None:
        with self._lock:
            if self._mouse_found == found:
                return
            self._mouse_found = found
        logging.info("mouse_found -> %s", found)

    def set_presence(self, name: str, present: bool) -> bool:
        with self._lock:
            old = self._presence[name]
            if old == present:
                return False
            self._presence[name] = present
            self._presence_changed_at = time.monotonic()
        logging.debug("keyboard presence %s -> %s", name, present)
        return True

    def arbitrate_presence(self, settle_seconds: float) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._presence_changed_at < settle_seconds:
                return
            present = [name for name, is_present in self._presence.items() if is_present]
            if len(present) != 1:
                return
            target = present[0]
            if self._target == target:
                return
            self._target = target
        logging.info("target -> %s", target)

    def set_keyboard_local(self, present: bool) -> bool:
        return self.set_presence("local", present)

    def status(self) -> dict[str, Any]:
        with self._lock:
            presence = dict(self._presence)
            return {
                "target": self._target,
                "mouse_found": self._mouse_found,
                "keyboard_local": presence["local"],
                "presence": presence,
            }


class ControlHandler(BaseHTTPRequestHandler):
    server: "ControlServer"

    def log_message(self, fmt: str, *args: Any) -> None:
        logging.debug("control %s - %s", self.client_address[0], fmt % args)

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Lanmouse-Token", "")
        return hmac.compare_digest(supplied, self.server.token)

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def do_GET(self) -> None:
        if not self._authorized():
            self._send_error_json(HTTPStatus.UNAUTHORIZED, "unauthorized")
            return
        if self.path != "/status":
            self._send_error_json(HTTPStatus.NOT_FOUND, "not found")
            return
        self._send_json(HTTPStatus.OK, self.server.state.status())

    def do_POST(self) -> None:
        if not self._authorized():
            self._send_error_json(HTTPStatus.UNAUTHORIZED, "unauthorized")
            return
        if self.path != "/kbd":
            self._send_error_json(HTTPStatus.NOT_FOUND, "not found")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "bad content length")
            return
        if length <= 0 or length > 4096:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "bad content length")
            return

        try:
            body = self.rfile.read(length)
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "bad JSON")
            return

        if not isinstance(payload, dict):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "body must be an object")
            return
        name = payload.get("name")
        present = payload.get("present")
        if not isinstance(name, str) or not isinstance(present, bool):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "name string and present bool required")
            return
        if name not in self.server.receiver_names:
            self._send_error_json(HTTPStatus.NOT_FOUND, "unknown receiver")
            return

        self.server.state.set_presence(name, present)

        self._send_json(HTTPStatus.OK, {"ok": True, "target": self.server.state.get_target()})


class ControlServer(ReusableThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        token: str,
        receiver_names: set[str],
        state: SenderState,
    ) -> None:
        super().__init__(address, ControlHandler)
        self.token = token
        self.receiver_names = receiver_names
        self.state = state


class SenderKeyboardWatcher(threading.Thread):
    def __init__(self, match: str, state: SenderState, stop_event: threading.Event) -> None:
        super().__init__(name="keyboard-watcher", daemon=True)
        self.match = match
        self.state = state
        self.stop_event = stop_event

    def run(self) -> None:
        while not self.stop_event.is_set():
            present = keyboard_present(self.match)
            self.state.set_keyboard_local(present)
            self.stop_event.wait(0.15)


class PresenceArbiter(threading.Thread):
    def __init__(
        self,
        state: SenderState,
        settle_seconds: float,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name="presence-arbiter", daemon=True)
        self.state = state
        self.settle_seconds = settle_seconds
        self.stop_event = stop_event

    def run(self) -> None:
        while not self.stop_event.is_set():
            self.state.arbitrate_presence(self.settle_seconds)
            self.stop_event.wait(PRESENCE_ARBITER_INTERVAL)


class MouseForwarder(threading.Thread):
    def __init__(
        self,
        mouse_match: str,
        receivers: dict[str, dict[str, Any]],
        token: str,
        state: SenderState,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name="mouse-forwarder", daemon=True)
        self.mouse_match = mouse_match
        self.receivers = receivers
        self.token = token
        self.state = state
        self.stop_event = stop_event
        self.fd: int | None = None
        self.grabbed = False
        self.sock: socket.socket | None = None
        self.sock_target: str | None = None
        self.pressed_buttons: set[int] = set()
        self.connect_failure_target: str | None = None
        self.connect_failure_since: float | None = None
        self.connect_failure_error: BaseException | None = None
        self.cleanup_lock = threading.Lock()
        atexit.register(self.cleanup)

    def cleanup(self) -> None:
        with self.cleanup_lock:
            self._close_connection_locked()
            self._ungrab_locked()
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None

    def _release_pressed_buttons_locked(self) -> None:
        if self.sock is None:
            self.pressed_buttons.clear()
            return
        if not self.pressed_buttons:
            return

        payload = bytearray()
        for code in sorted(self.pressed_buttons):
            payload.extend(struct.pack(WIRE_EVENT_FORMAT, EV_KEY, code, 0))
        payload.extend(struct.pack(WIRE_EVENT_FORMAT, EV_SYN, 0, 0))

        try:
            self.sock.sendall(bytes(payload))
        except OSError as exc:
            logging.debug("button release send failed for %s: %s", self.sock_target, exc)
        finally:
            self.pressed_buttons.clear()

    def _close_connection_locked(self) -> None:
        if self.sock is not None:
            self._release_pressed_buttons_locked()
            safe_close(self.sock)
            self.sock = None
            self.sock_target = None
        else:
            self.pressed_buttons.clear()

    def _ungrab_locked(self) -> None:
        if self.fd is None or not self.grabbed:
            self.grabbed = False
            return
        try:
            fcntl.ioctl(self.fd, EVIOCGRAB, 0)
            logging.info("mouse ungrabbed")
        except OSError as exc:
            logging.warning("EVIOCGRAB release failed: %s", exc)
        finally:
            self.grabbed = False

    def _grab(self) -> bool:
        with self.cleanup_lock:
            if self.fd is None:
                return False
            if self.grabbed:
                return True
            try:
                fcntl.ioctl(self.fd, EVIOCGRAB, 1)
            except OSError as exc:
                logging.error("EVIOCGRAB failed: %s", exc)
                return False
            self.grabbed = True
        logging.info("mouse grabbed")
        return True

    def _ungrab(self) -> None:
        with self.cleanup_lock:
            self._ungrab_locked()

    def _close_connection(self) -> None:
        with self.cleanup_lock:
            self._close_connection_locked()

    def _connect_once(self, target: str) -> socket.socket:
        receiver = self.receivers[target]
        sock = socket.create_connection((receiver["host"], receiver["port"]), timeout=3.0)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.sendall(self.token.encode("utf-8") + b"\n")
        sock.settimeout(None)
        return sock

    def _reset_connect_failures(self) -> None:
        self.connect_failure_target = None
        self.connect_failure_since = None
        self.connect_failure_error = None

    def _note_connect_failure(
        self,
        target: str,
        exc: BaseException,
        started_at: float,
    ) -> bool:
        if self.connect_failure_target != target or self.connect_failure_since is None:
            self.connect_failure_target = target
            self.connect_failure_since = started_at
        self.connect_failure_error = exc

        elapsed = time.monotonic() - self.connect_failure_since
        if elapsed < CONNECT_FAILSAFE_SECONDS:
            return False

        logging.error(
            "receiver %s unavailable; reverting to local: %s",
            target,
            exc,
        )
        self.state.set_target("local")
        self._close_connection()
        self._ungrab()
        self._reset_connect_failures()
        return True

    def _ensure_connection_for_tick(self, target: str) -> bool:
        if self.sock is not None and self.sock_target == target:
            self._reset_connect_failures()
            return True

        self._close_connection()
        started_at = time.monotonic()
        try:
            sock = self._connect_once(target)
        except OSError as exc:
            self._note_connect_failure(target, exc, started_at)
            return False

        current_target = self.state.get_target()
        if self.stop_event.is_set() or current_target != target:
            safe_close(sock)
            self._reset_connect_failures()
            if current_target == "local":
                self._ungrab()
            return False

        with self.cleanup_lock:
            self.sock = sock
            self.sock_target = target
        self._reset_connect_failures()
        logging.info("connected to receiver %s", target)
        return True

    def _sync_target(self) -> str:
        target = self.state.get_target()
        if target == "local":
            self._reset_connect_failures()
            self._close_connection()
            self._ungrab()
            return target

        if target not in self.receivers:
            logging.error("unknown target %s; reverting to local", target)
            self.state.set_target("local")
            self._reset_connect_failures()
            self._close_connection()
            self._ungrab()
            return "local"

        if not self._grab():
            self.state.set_target("local")
            self._reset_connect_failures()
            self._close_connection()
            self._ungrab()
            return "local"

        if not self._ensure_connection_for_tick(target):
            if self.state.get_target() == "local":
                self._reset_connect_failures()
                self._close_connection()
                self._ungrab()
            return "local"

        return target

    def _send_payload(self, target: str, payload: bytes) -> None:
        if self.state.get_target() != target:
            return
        with self.cleanup_lock:
            sock = self.sock if self.sock_target == target else None
        if sock is None:
            return

        try:
            sock.sendall(payload)
        except OSError as exc:
            self._apply_button_state_from_payload(payload, release_events=False)
            self._close_connection()
            if self.state.get_target() == target:
                self._note_connect_failure(target, exc, time.monotonic())
            return

        self._apply_button_state_from_payload(payload, release_events=True)
        self._reset_connect_failures()

    def _apply_button_state_from_payload(self, payload: bytes, *, release_events: bool) -> None:
        with self.cleanup_lock:
            limit = len(payload) - (len(payload) % WIRE_EVENT_SIZE)
            for offset in range(0, limit, WIRE_EVENT_SIZE):
                ev_type, code, value = struct.unpack_from(WIRE_EVENT_FORMAT, payload, offset)
                if ev_type != EV_KEY or code not in ENABLED_BTN_CODES:
                    continue
                if value == 1:
                    self.pressed_buttons.add(code)
                elif value == 0 and release_events:
                    self.pressed_buttons.discard(code)

    def _events_to_payload(self, data: bytes) -> bytes:
        out = bytearray()
        limit = len(data) - (len(data) % INPUT_EVENT_SIZE)
        for offset in range(0, limit, INPUT_EVENT_SIZE):
            try:
                _sec, _usec, ev_type, code, value = struct.unpack_from(
                    INPUT_EVENT_FORMAT,
                    data,
                    offset,
                )
            except struct.error:
                break
            if ev_type in {EV_SYN, EV_KEY, EV_REL}:
                out.extend(struct.pack(WIRE_EVENT_FORMAT, ev_type, code, value))
        return bytes(out)

    def _open_mouse(self) -> bool:
        path = find_event_device(self.mouse_match)
        if path is None:
            self.state.set_mouse_found(False)
            return False
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError as exc:
            logging.warning("cannot open %s: %s", path, exc)
            self.state.set_mouse_found(False)
            return False
        with self.cleanup_lock:
            self.fd = fd
        self.state.set_mouse_found(True)
        logging.info("mouse device -> %s", path)
        return True

    def _close_mouse(self) -> None:
        with self.cleanup_lock:
            self._close_connection_locked()
            self._ungrab_locked()
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None
        self.state.set_mouse_found(False)

    def _read_loop(self) -> None:
        partial = b""
        while not self.stop_event.is_set() and self.fd is not None:
            target = self._sync_target()
            try:
                ready, _, _ = select.select([self.fd], [], [], 0.5)
            except (OSError, ValueError) as exc:
                logging.warning("mouse select failed: %s", exc)
                break

            if not ready:
                continue

            try:
                chunk = os.read(self.fd, INPUT_EVENT_SIZE * 64)
            except OSError as exc:
                if exc.errno in {errno.ENODEV, errno.EIO, errno.ENXIO}:
                    logging.warning("mouse device read failed; rescanning: %s", exc)
                else:
                    logging.warning("mouse read failed; rescanning: %s", exc)
                break

            if not chunk:
                logging.warning("mouse device returned EOF; rescanning")
                break

            data = partial + chunk
            full_len = len(data) - (len(data) % INPUT_EVENT_SIZE)
            full_data = data[:full_len]
            partial = data[full_len:]

            if target == "local" or not full_data:
                continue
            payload = self._events_to_payload(full_data)
            if payload:
                self._send_payload(target, payload)

    def run(self) -> None:
        while not self.stop_event.is_set():
            if not self._open_mouse():
                self.stop_event.wait(1.0)
                continue
            try:
                self._read_loop()
            finally:
                self._close_mouse()
            if not self.stop_event.is_set():
                self.stop_event.wait(1.0)
        self.cleanup()


class UInputMouse:
    def __init__(self) -> None:
        self.fd: int | None = None
        self.created = False
        self.lock = threading.Lock()
        atexit.register(self.close)

    def open(self) -> None:
        fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        self.fd = fd

        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_REL)
        for code in sorted(ENABLED_REL_CODES):
            fcntl.ioctl(fd, UI_SET_RELBIT, code)
        for code in sorted(ENABLED_BTN_CODES):
            fcntl.ioctl(fd, UI_SET_KEYBIT, code)

        name = UINPUT_NAME.encode("utf-8")
        if len(name) >= UINPUT_MAX_NAME_SIZE:
            name = name[: UINPUT_MAX_NAME_SIZE - 1]
        name = name + b"\0" * (UINPUT_MAX_NAME_SIZE - len(name))
        setup = struct.pack(
            "HHHH80sI",
            BUS_VIRTUAL,
            0x1D6B,
            0x0104,
            1,
            name,
            0,
        )
        fcntl.ioctl(fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(fd, UI_DEV_CREATE)
        self.created = True
        logging.info("created uinput device %r", UINPUT_NAME)

    def close(self) -> None:
        with self.lock:
            if self.fd is None:
                return
            if self.created:
                try:
                    fcntl.ioctl(self.fd, UI_DEV_DESTROY)
                    logging.info("destroyed uinput device")
                except OSError as exc:
                    logging.warning("UI_DEV_DESTROY failed: %s", exc)
                self.created = False
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def write_event(self, ev_type: int, code: int, value: int) -> bool:
        if ev_type == EV_SYN:
            pass
        elif ev_type == EV_KEY:
            if code not in ENABLED_BTN_CODES:
                return False
        elif ev_type == EV_REL:
            if code not in ENABLED_REL_CODES:
                return False
        else:
            return False

        event = struct.pack(INPUT_EVENT_FORMAT, 0, 0, ev_type, code, value)
        with self.lock:
            if self.fd is None:
                return False
            try:
                os.write(self.fd, event)
                return True
            except BlockingIOError:
                logging.warning("uinput write would block; dropped event")
                return False
            except OSError as exc:
                logging.error("uinput write failed: %s", exc)
                return False


class ReceiverEventServer(threading.Thread):
    def __init__(
        self,
        listen_port: int,
        token: str,
        uinput: UInputMouse,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name="event-server", daemon=True)
        self.listen_port = listen_port
        self.token_bytes = token.encode("utf-8")
        self.uinput = uinput
        self.stop_event = stop_event
        self.listen_sock: socket.socket | None = None
        self.active_sock: socket.socket | None = None
        self.pressed_buttons: set[int] = set()
        self.active_lock = threading.Lock()
        atexit.register(self.close)

    def close(self) -> None:
        if self.listen_sock is not None:
            safe_close(self.listen_sock)
            self.listen_sock = None
        active_sock: socket.socket | None = None
        with self.active_lock:
            if self.active_sock is not None:
                active_sock = self.active_sock
                self.active_sock = None
            self._release_pressed_buttons_locked()
        if active_sock is not None:
            safe_close(active_sock)

    def _is_active(self, conn: socket.socket) -> bool:
        with self.active_lock:
            return self.active_sock is conn

    def _set_active(self, conn: socket.socket) -> None:
        old: socket.socket | None = None
        with self.active_lock:
            old = self.active_sock
            if old is not None and old is not conn:
                self._release_pressed_buttons_locked()
            self.active_sock = conn
        if old is not None and old is not conn:
            safe_close(old)

    def _clear_active(self, conn: socket.socket) -> None:
        with self.active_lock:
            if self.active_sock is conn:
                self._release_pressed_buttons_locked()
                self.active_sock = None

    def _release_pressed_buttons_locked(self) -> None:
        if not self.pressed_buttons:
            return
        codes = sorted(self.pressed_buttons)
        self.pressed_buttons.clear()
        for code in codes:
            self.uinput.write_event(EV_KEY, code, 0)
        self.uinput.write_event(EV_SYN, 0, 0)

    def _apply_active_event(
        self,
        conn: socket.socket,
        ev_type: int,
        code: int,
        value: int,
    ) -> bool:
        with self.active_lock:
            if self.active_sock is not conn:
                return False
            written = self.uinput.write_event(ev_type, code, value)
            if written and ev_type == EV_KEY and code in ENABLED_BTN_CODES:
                if value == 1:
                    self.pressed_buttons.add(code)
                elif value == 0:
                    self.pressed_buttons.discard(code)
            return True

    def _read_token_line(self, conn: socket.socket) -> tuple[bytes | None, bytes]:
        buffer = bytearray()
        while not self.stop_event.is_set():
            if len(buffer) > 256:
                return None, b""
            chunk = conn.recv(256)
            if not chunk:
                return None, b""
            buffer.extend(chunk)
            newline = buffer.find(b"\n")
            if newline >= 0:
                token_line = bytes(buffer[:newline])
                rest = bytes(buffer[newline + 1 :])
                if len(token_line) > 256:
                    return None, b""
                return token_line, rest
        return None, b""

    def _handle_connection(self, conn: socket.socket, address: tuple[str, int]) -> None:
        buffer = b""
        try:
            conn.settimeout(3.0)
            token_line, buffer = self._read_token_line(conn)
            if token_line is None or not hmac.compare_digest(token_line, self.token_bytes):
                logging.warning("rejected connection from %s: bad token", address[0])
                return
            conn.settimeout(None)
            self._set_active(conn)
            logging.info("mouse stream connected from %s", address[0])

            while not self.stop_event.is_set() and self._is_active(conn):
                while len(buffer) >= WIRE_EVENT_SIZE:
                    record = buffer[:WIRE_EVENT_SIZE]
                    buffer = buffer[WIRE_EVENT_SIZE:]
                    ev_type, code, value = struct.unpack(WIRE_EVENT_FORMAT, record)
                    if not self._apply_active_event(conn, ev_type, code, value):
                        return

                chunk = conn.recv(4096)
                if not chunk:
                    return
                buffer += chunk
        except (OSError, struct.error) as exc:
            if not self.stop_event.is_set() and self._is_active(conn):
                logging.warning("mouse stream from %s ended: %s", address[0], exc)
        finally:
            self._clear_active(conn)
            safe_close(conn)
            logging.info("mouse stream disconnected from %s", address[0])

    def run(self) -> None:
        listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listen_sock.bind(("0.0.0.0", self.listen_port))
        listen_sock.listen(5)
        self.listen_sock = listen_sock
        logging.info("listening for mouse stream on 0.0.0.0:%d", self.listen_port)

        while not self.stop_event.is_set():
            try:
                conn, address = listen_sock.accept()
            except OSError:
                if not self.stop_event.is_set():
                    logging.exception("event server accept failed")
                break
            thread = threading.Thread(
                target=self._handle_connection,
                args=(conn, address),
                name=f"event-client-{address[0]}",
                daemon=True,
            )
            thread.start()
        self.close()


class ReceiverKeyboardReporter(threading.Thread):
    def __init__(
        self,
        name: str,
        keyboard_match: str,
        sender_host: str,
        sender_control_port: int,
        token: str,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name="keyboard-reporter", daemon=True)
        self.name = name
        self.keyboard_match = keyboard_match
        self.url = f"http://{sender_host}:{sender_control_port}/kbd"
        self.token = token
        self.stop_event = stop_event

    def _post_state(self, present: bool) -> bool:
        payload = json.dumps({"name": self.name, "present": present}).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
                "X-Lanmouse-Token": self.token,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=3.0) as response:
                response.read()
                if response.status != HTTPStatus.OK:
                    logging.warning("keyboard POST returned HTTP %s", response.status)
                    return False
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logging.warning("keyboard POST failed: %s", exc)
            return False
        logging.info("keyboard present -> %s posted to sender", present)
        return True

    def run(self) -> None:
        last_observed: bool | None = None
        post_required = True
        next_attempt = 0.0

        while not self.stop_event.is_set():
            present = keyboard_present(self.keyboard_match)
            now = time.monotonic()

            if last_observed is None or present != last_observed:
                last_observed = present
                post_required = True
                next_attempt = now
                logging.info("keyboard present -> %s", present)

            if post_required and now >= next_attempt:
                if self._post_state(present):
                    post_required = False
                else:
                    next_attempt = time.monotonic() + 5.0

            self.stop_event.wait(0.15)


def run_sender(config: dict[str, Any], stop_event: threading.Event) -> int:
    receivers = {item["name"]: item for item in config["receivers"]}
    state = SenderState(set(receivers))
    settle_seconds = require_seconds(config, "settle_seconds", DEFAULT_SETTLE_SECONDS)

    control_server = ControlServer(
        ("0.0.0.0", config.get("control_port", 47701)),
        config["token"],
        set(receivers),
        state,
    )
    control_thread = threading.Thread(
        target=control_server.serve_forever,
        kwargs={"poll_interval": 0.5},
        name="control-server",
        daemon=True,
    )

    keyboard_watcher = SenderKeyboardWatcher(config["keyboard_match"], state, stop_event)
    presence_arbiter = PresenceArbiter(state, settle_seconds, stop_event)
    mouse_forwarder = MouseForwarder(
        config["mouse_match"],
        receivers,
        config["token"],
        state,
        stop_event,
    )

    logging.info("control server listening on 0.0.0.0:%d", config.get("control_port", 47701))
    control_thread.start()
    keyboard_watcher.start()
    presence_arbiter.start()
    mouse_forwarder.start()

    try:
        while not stop_event.is_set():
            stop_event.wait(1.0)
    finally:
        stop_event.set()
        control_server.shutdown()
        control_server.server_close()
        mouse_forwarder.cleanup()
    return 0


def run_receiver(config: dict[str, Any], stop_event: threading.Event) -> int:
    uinput = UInputMouse()
    uinput.open()

    event_server = ReceiverEventServer(
        config.get("listen_port", 47702),
        config["token"],
        uinput,
        stop_event,
    )
    keyboard_reporter = ReceiverKeyboardReporter(
        config["name"],
        config["keyboard_match"],
        config["sender_host"],
        config.get("sender_control_port", 47701),
        config["token"],
        stop_event,
    )

    event_server.start()
    keyboard_reporter.start()

    try:
        while not stop_event.is_set():
            stop_event.wait(1.0)
    finally:
        stop_event.set()
        event_server.close()
        uinput.close()
    return 0


def install_signal_handlers(stop_event: threading.Event) -> None:
    def _handle_signal(signum: int, _frame: Any) -> None:
        logging.info("received signal %s; shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LAN mouse forwarding tool")
    parser.add_argument("--config", default=CONFIG_PATH)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.debug)

    if EVIOCGRAB != 0x40044590:
        logging.error("computed EVIOCGRAB unexpectedly as %#x", EVIOCGRAB)
        return 1

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        logging.error("bad config: %s", exc)
        return 2

    stop_event = threading.Event()
    install_signal_handlers(stop_event)

    try:
        if config["role"] == "sender":
            return run_sender(config, stop_event)
        return run_receiver(config, stop_event)
    except ConfigError as exc:
        logging.error("bad config: %s", exc)
        return 2
    except OSError as exc:
        logging.error("runtime setup failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
