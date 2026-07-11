#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import logging
import math
from pathlib import Path
import signal
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.request


DEFAULT_CONFIG = "~/.config/lanmouse-indicator/config.json"
HTTP_TIMEOUT = 2.0
UI_POLL_MS = 200
SIGNAL_TICK_MS = 100
TOPMOST_REASSERT_MS = 3000

PILL_BG = "#222222"
TRANSPARENT_BG = "#010101"
HERE_DOT = "#33cc33"
ELSEWHERE_DOT = "#555555"
UNKNOWN_DOT = "#ff9933"
HERE_FG = "#ffffff"
ELSEWHERE_FG = "#aaaaaa"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    self_name: str
    status_url: str
    token: str
    poll_interval: float
    margin: int
    font_size: int
    labels: dict[str, str]


@dataclass(frozen=True)
class IndicatorState:
    mode: str
    target: str | None
    text: str
    dot_color: str
    text_color: str


class SharedState:
    def __init__(self, state: IndicatorState) -> None:
        self._state = state
        self._lock = threading.Lock()

    def get(self) -> IndicatorState:
        with self._lock:
            return self._state

    def set(self, state: IndicatorState) -> None:
        with self._lock:
            self._state = state


def require_string(config: dict[str, object], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key!r} must be a non-empty string")
    return value


def read_config(path: Path) -> Config:
    try:
        with path.open("r", encoding="utf-8") as config_file:
            raw = json.load(config_file)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("config root must be a JSON object")

    self_name = require_string(raw, "self")
    status_url = require_string(raw, "status_url")
    token = require_string(raw, "token")

    poll_interval_raw = raw.get("poll_interval", 1.0)
    if (
        isinstance(poll_interval_raw, bool)
        or not isinstance(poll_interval_raw, (int, float))
        or not math.isfinite(float(poll_interval_raw))
        or float(poll_interval_raw) <= 0
    ):
        raise ConfigError("'poll_interval' must be a positive number")
    poll_interval = float(poll_interval_raw)

    margin_raw = raw.get("margin", 8)
    if (
        isinstance(margin_raw, bool)
        or not isinstance(margin_raw, (int, float))
        or not math.isfinite(float(margin_raw))
        or float(margin_raw) < 0
    ):
        raise ConfigError("'margin' must be a non-negative integer")
    if isinstance(margin_raw, float) and not margin_raw.is_integer():
        raise ConfigError("'margin' must be a non-negative integer")
    margin = int(margin_raw)

    font_size_raw = raw.get("font_size", 14)
    if (
        isinstance(font_size_raw, bool)
        or not isinstance(font_size_raw, int)
        or font_size_raw <= 0
    ):
        raise ConfigError("'font_size' must be a positive integer")
    font_size = font_size_raw

    labels_raw = raw.get("labels", {})
    if not isinstance(labels_raw, dict):
        raise ConfigError("'labels' must be an object mapping strings to strings")
    labels: dict[str, str] = {}
    for key, value in labels_raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ConfigError("'labels' must be an object mapping strings to strings")
        labels[key] = value

    return Config(
        self_name=self_name,
        status_url=status_url,
        token=token,
        poll_interval=poll_interval,
        margin=margin,
        font_size=font_size,
        labels=labels,
    )


def label_for(config: Config, target: str) -> str:
    return config.labels.get(target, target)


def unknown_state() -> IndicatorState:
    return IndicatorState("unknown", None, "?", UNKNOWN_DOT, ELSEWHERE_FG)


def state_for_target(config: Config, target: str) -> IndicatorState:
    if target == config.self_name:
        return IndicatorState(
            "here",
            target,
            label_for(config, config.self_name),
            HERE_DOT,
            HERE_FG,
        )
    return IndicatorState(
        "elsewhere",
        target,
        label_for(config, target),
        ELSEWHERE_DOT,
        ELSEWHERE_FG,
    )


def fetch_target(config: Config) -> str:
    request = urllib.request.Request(
        config.status_url,
        headers={"X-Lanmouse-Token": config.token},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        payload = json.loads(response.read(65536))

    if not isinstance(payload, dict):
        raise ValueError("status response is not a JSON object")

    target = payload.get("target")
    if not isinstance(target, str) or not target:
        raise ValueError("status response missing non-empty string target")
    return target


def poll_status(config: Config, shared: SharedState, stop_event: threading.Event) -> None:
    last_logged: IndicatorState | None = None
    while not stop_event.is_set():
        try:
            state = state_for_target(config, fetch_target(config))
        except Exception:
            state = unknown_state()

        shared.set(state)
        if state != last_logged:
            logging.info("target -> %s", state.target if state.target is not None else "?")
            last_logged = state

        stop_event.wait(config.poll_interval)


class IndicatorWindow:
    def __init__(
        self,
        root: tk.Tk,
        config: Config,
        shared: SharedState,
        stop_event: threading.Event,
    ) -> None:
        self.root = root
        self.config = config
        self.shared = shared
        self.stop_event = stop_event
        self.current_state: IndicatorState | None = None
        self.height = 30

        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.configure(bg=TRANSPARENT_BG)
        self.root.attributes("-topmost", True)

        self.transparent = True
        try:
            self.root.attributes("-transparentcolor", TRANSPARENT_BG)
        except tk.TclError:
            self.transparent = False
            self.root.configure(bg=PILL_BG)

        self.canvas = tk.Canvas(
            self.root,
            highlightthickness=0,
            bd=0,
            bg=TRANSPARENT_BG if self.transparent else PILL_BG,
        )
        self.canvas.pack(fill="both", expand=True)

        self.font = tkfont.nametofont("TkDefaultFont").copy()
        self.font.configure(size=config.font_size)

    def start(self) -> None:
        self.apply_latest_state()
        self.root.after(UI_POLL_MS, self.update_from_shared)
        self.root.after(SIGNAL_TICK_MS, self.signal_tick)
        self.root.after(TOPMOST_REASSERT_MS, self.reassert_topmost)

    def update_from_shared(self) -> None:
        if self.stop_event.is_set():
            self.close()
            return
        self.apply_latest_state()
        self.root.after(UI_POLL_MS, self.update_from_shared)

    def signal_tick(self) -> None:
        if self.stop_event.is_set():
            self.close()
            return
        self.root.after(SIGNAL_TICK_MS, self.signal_tick)

    def reassert_topmost(self) -> None:
        if self.stop_event.is_set():
            return
        try:
            self.root.attributes("-topmost", True)
            self.root.lift()
            self.root.after(TOPMOST_REASSERT_MS, self.reassert_topmost)
        except tk.TclError:
            return

    def close(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def apply_latest_state(self) -> None:
        state = self.shared.get()
        if state == self.current_state:
            return
        self.current_state = state
        self.redraw(state)

    def redraw(self, state: IndicatorState) -> None:
        dot_size = 14
        left_pad = 11
        gap = 8
        right_pad = 13
        line_space = self.font.metrics("linespace")
        self.height = max(30, line_space + 8)
        text_width = self.font.measure(state.text)
        width = max(39, left_pad + dot_size + gap + text_width + right_pad)

        self.canvas.configure(width=width, height=self.height)
        self.canvas.delete("all")
        self.draw_pill(width, self.height)

        dot_y = (self.height - dot_size) / 2
        dot_x = left_pad
        self.canvas.create_oval(
            dot_x,
            dot_y,
            dot_x + dot_size,
            dot_y + dot_size,
            fill=state.dot_color,
            outline="",
        )
        self.canvas.create_text(
            dot_x + dot_size + gap,
            self.height / 2,
            anchor="w",
            text=state.text,
            fill=state.text_color,
            font=self.font,
        )

        screen_height = self.root.winfo_screenheight()
        x_pos = self.config.margin
        y_pos = max(0, screen_height - self.height - self.config.margin)
        self.root.geometry(f"{width}x{self.height}+{x_pos}+{y_pos}")
        self.root.deiconify()
        self.root.lift()

    def draw_pill(self, width: int, height: int) -> None:
        radius = height // 2
        self.canvas.create_rectangle(
            radius,
            0,
            width - radius,
            height,
            fill=PILL_BG,
            outline="",
        )
        self.canvas.create_oval(0, 0, height, height, fill=PILL_BG, outline="")
        self.canvas.create_oval(
            width - height,
            0,
            width,
            height,
            fill=PILL_BG,
            outline="",
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show the current lanmouse keyboard/mouse target."
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help=f"config path, default: {DEFAULT_CONFIG}",
    )
    return parser.parse_args(argv)


def install_signal_handlers(stop_event: threading.Event) -> None:
    def handle_signal(signum: int, frame: object) -> None:
        del signum, frame
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)


def run(config: Config) -> int:
    stop_event = threading.Event()
    install_signal_handlers(stop_event)
    shared = SharedState(unknown_state())

    root = tk.Tk()
    app = IndicatorWindow(root, config, shared, stop_event)

    poll_thread = threading.Thread(
        target=poll_status,
        args=(config, shared, stop_event),
        name="lanmouse-indicator-poll",
        daemon=True,
    )
    poll_thread.start()
    app.start()

    try:
        root.mainloop()
    except KeyboardInterrupt:
        stop_event.set()
        app.close()
    finally:
        stop_event.set()
        poll_thread.join(timeout=HTTP_TIMEOUT + 0.5)

    return 0


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="indicator: %(message)s")
    args = parse_args(argv)
    config_path = Path(args.config).expanduser()
    try:
        config = read_config(config_path)
    except ConfigError as exc:
        print(f"indicator: config error: {exc}", file=sys.stderr)
        return 2
    return run(config)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
