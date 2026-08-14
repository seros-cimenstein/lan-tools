#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import signal
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from typing import TypeVar
import urllib.error
import urllib.request


DEFAULT_CONFIG = "~/.config/lanclip-indicator/config.json"
HTTP_TIMEOUT = 2.0
DISPLAY_RETRY_SECONDS = 3.0
DISPLAY_LOG_INTERVAL = 60.0
DISPLAY_WAIT_TIMEOUT = 120.0
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
COPY_GLYPH_COLOR = "#3399ff"

_T = TypeVar("_T")


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
    opacity: float
    dot_opacity: float
    text_opacity: float
    labels: dict[str, str]


@dataclass(frozen=True)
class DotState:
    name: str
    label: str
    reachable: bool
    is_source: bool


@dataclass(frozen=True)
class IndicatorState:
    dots: tuple[DotState, ...]
    ok: bool


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


def require_opacity(config: dict[str, object], key: str, default: float) -> float:
    value = config.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0.0
        or float(value) > 1.0
    ):
        raise ConfigError(f"'{key}' must be a number in (0.0, 1.0]")
    return float(value)


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

    opacity = require_opacity(raw, "opacity", 0.8)
    dot_opacity = require_opacity(raw, "dot_opacity", 1.0)
    text_opacity = require_opacity(raw, "text_opacity", 0.9)

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
        opacity=opacity,
        dot_opacity=dot_opacity,
        text_opacity=text_opacity,
        labels=labels,
    )


def label_for(config: Config, name: str) -> str:
    return config.labels.get(name, name)


def unknown_state() -> IndicatorState:
    return IndicatorState(dots=(), ok=False)


def state_from_payload(config: Config, payload: dict[str, object]) -> IndicatorState:
    peers = payload.get("peers")
    if not isinstance(peers, dict):
        raise ValueError("status response missing peers object")

    last_source = payload.get("last_source")
    dots = [
        DotState(
            name=config.self_name,
            label=label_for(config, config.self_name),
            reachable=True,
            is_source=last_source == config.self_name,
        )
    ]

    for name, peer_status in peers.items():
        if not isinstance(name, str) or not isinstance(peer_status, dict):
            raise ValueError("status response has an invalid peer entry")
        dots.append(
            DotState(
                name=name,
                label=label_for(config, name),
                reachable=peer_status["reachable"] is True,
                is_source=last_source == name,
            )
        )

    return IndicatorState(dots=tuple(dots), ok=True)


def fetch_status(config: Config) -> dict[str, object]:
    request = urllib.request.Request(
        config.status_url,
        headers={"X-Lanclip-Token": config.token},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        payload = json.loads(response.read(65536))

    if not isinstance(payload, dict):
        raise ValueError("status response is not a JSON object")
    return payload


def poll_status(config: Config, shared: SharedState, stop_event: threading.Event) -> None:
    last_logged: IndicatorState | None = None
    while not stop_event.is_set():
        try:
            state = state_from_payload(config, fetch_status(config))
        except Exception:
            state = unknown_state()

        shared.set(state)
        if state != last_logged:
            if state.ok:
                summary = ", ".join(
                    f"{dot.name}={'up' if dot.reachable else 'down'}"
                    f"{'*' if dot.is_source else ''}"
                    for dot in state.dots
                )
            else:
                summary = "?"
            logging.info("status -> %s", summary)
            last_logged = state

        stop_event.wait(config.poll_interval)


def display_groups(state: IndicatorState) -> tuple[tuple[str, str, str, bool], ...]:
    if not state.ok:
        return (("?", UNKNOWN_DOT, ELSEWHERE_FG, False),)
    return tuple(
        (
            dot.label,
            HERE_DOT if dot.reachable else ELSEWHERE_DOT,
            HERE_FG if dot.reachable else ELSEWHERE_FG,
            dot.is_source,
        )
        for dot in state.dots
    )


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
        self.root.title("lanclip-indicator")
        self.root.overrideredirect(True)
        try:
            self.root.attributes("-type", "notification")
        except tk.TclError:
            pass
        self.root.configure(bg=TRANSPARENT_BG)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", config.opacity)
        except tk.TclError:
            pass

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
        dot_gap = 16
        right_pad = 13
        line_space = self.font.metrics("linespace")
        self.height = max(30, line_space + 8)
        groups = display_groups(state)
        group_widths = [
            dot_size + gap + self.font.measure(label)
            for label, _dot_color, _text_color, _is_source in groups
        ]
        width = max(
            39,
            left_pad + sum(group_widths) + (len(groups) - 1) * dot_gap + right_pad,
        )

        self.canvas.configure(width=width, height=self.height)
        self.canvas.delete("all")
        self.draw_pill(width, self.height)

        dot_y = (self.height - dot_size) / 2
        dot_x = float(left_pad)
        for (label, dot_color, text_color, is_source), group_width in zip(
            groups, group_widths
        ):
            self.canvas.create_oval(
                dot_x,
                dot_y,
                dot_x + dot_size,
                dot_y + dot_size,
                fill=dot_color,
                outline="",
            )
            self.canvas.create_text(
                dot_x + dot_size + gap,
                self.height / 2,
                anchor="w",
                text=label,
                fill=text_color,
                font=self.font,
            )
            if is_source:
                self.draw_copy_badge(width, dot_x, dot_y, dot_size)
            dot_x += group_width + dot_gap

        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x_pos = max(0, (screen_width - width) // 2)
        y_pos = max(0, screen_height - self.height - self.config.margin)
        self.root.geometry(f"{width}x{self.height}+{x_pos}+{y_pos}")
        self.root.deiconify()
        self.root.lift()

    def draw_copy_badge(
        self, width: int, dot_x: float, dot_y: float, dot_size: int
    ) -> None:
        badge_size = 10.0
        badge_x = min(width - badge_size, max(0.0, dot_x + dot_size - 4.0))
        badge_y = min(
            self.height - badge_size,
            max(0.0, dot_y - 4.0),
        )
        self.canvas.create_oval(
            badge_x,
            badge_y,
            badge_x + badge_size,
            badge_y + badge_size,
            fill=COPY_GLYPH_COLOR,
            outline="",
        )
        self.canvas.create_line(
            badge_x + 2.4,
            badge_y + 5.2,
            badge_x + 4.4,
            badge_y + 7.1,
            badge_x + 7.9,
            badge_y + 3.1,
            fill=HERE_FG,
            width=1.5,
            capstyle=tk.ROUND,
            joinstyle=tk.ROUND,
        )

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


def color_to_rgb(color: str) -> tuple[float, float, float]:
    return (
        int(color[1:3], 16) / 255.0,
        int(color[3:5], 16) / 255.0,
        int(color[5:7], 16) / 255.0,
    )


def cairo_metric(values: object, name: str, index: int) -> float:
    try:
        return float(getattr(values, name))
    except AttributeError:
        return float(values[index])  # type: ignore[index]


class GtkLayerIndicatorWindow:
    def __init__(
        self,
        gtk: object,
        glib: object,
        gtk_layer_shell: object,
        cairo: object,
        config: Config,
        shared: SharedState,
        stop_event: threading.Event,
    ) -> None:
        self.Gtk = gtk
        self.GLib = glib
        self.GtkLayerShell = gtk_layer_shell
        self.cairo = cairo
        self.config = config
        self.shared = shared
        self.stop_event = stop_event
        self.current_state: IndicatorState | None = None
        self.closed = False
        self.width = 1
        self.height = 30

        self.window = self.Gtk.Window()
        self.window.set_title("lanclip-indicator")
        self.window.set_decorated(False)
        self.window.set_resizable(False)
        self.window.set_app_paintable(True)
        self.window.set_skip_taskbar_hint(True)
        self.window.set_skip_pager_hint(True)

        screen = self.window.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            self.window.set_visual(visual)

        self.GtkLayerShell.init_for_window(self.window)
        self.GtkLayerShell.set_namespace(self.window, "lanclip-indicator")
        self.GtkLayerShell.set_layer(
            self.window,
            self.GtkLayerShell.Layer.OVERLAY,
        )
        self.GtkLayerShell.set_anchor(
            self.window,
            self.GtkLayerShell.Edge.BOTTOM,
            True,
        )
        self.GtkLayerShell.set_margin(
            self.window,
            self.GtkLayerShell.Edge.BOTTOM,
            config.margin,
        )

        self.window.connect("draw", self.on_draw)
        self.window.connect("realize", self.on_realize)
        self.window.connect("destroy", self.on_destroy)

    def start(self) -> None:
        self.apply_latest_state()
        self.window.show_all()
        self.GLib.timeout_add(UI_POLL_MS, self.update_from_shared)
        self.GLib.timeout_add(SIGNAL_TICK_MS, self.signal_tick)

    def update_from_shared(self) -> bool:
        if self.stop_event.is_set():
            self.close()
            self.Gtk.main_quit()
            return False
        self.apply_latest_state()
        return True

    def signal_tick(self) -> bool:
        if self.stop_event.is_set():
            self.close()
            self.Gtk.main_quit()
            return False
        return True

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.window.destroy()

    def on_destroy(self, *_args: object) -> None:
        self.closed = True
        self.stop_event.set()
        self.Gtk.main_quit()

    def on_realize(self, window: object) -> None:
        window.input_shape_combine_region(self.cairo.Region())

    def apply_latest_state(self) -> None:
        state = self.shared.get()
        if state == self.current_state:
            return

        self.current_state = state
        width, height = self.measure(state)
        if width != self.width or height != self.height:
            self.width = width
            self.height = height
            self.window.set_size_request(width, height)
            self.window.resize(width, height)
            self.window.queue_resize()
        self.window.queue_draw()

    def measure(self, state: IndicatorState) -> tuple[int, int]:
        surface = self.cairo.ImageSurface(self.cairo.FORMAT_ARGB32, 1, 1)
        cr = self.cairo.Context(surface)
        self.configure_font(cr)
        groups = display_groups(state)
        text_widths = []
        for label, _dot_color, _text_color, _is_source in groups:
            text_extents = cr.text_extents(label)
            text_widths.append(
                max(
                    cairo_metric(text_extents, "width", 2),
                    cairo_metric(text_extents, "x_advance", 4),
                )
            )
        font_extents = cr.font_extents()
        line_space = cairo_metric(font_extents, "height", 2)

        dot_size = 14
        left_pad = 11
        gap = 8
        dot_gap = 16
        right_pad = 13
        height = max(30, math.ceil(line_space + 8))
        group_widths = [dot_size + gap + text_width for text_width in text_widths]
        width = max(
            39,
            math.ceil(
                left_pad
                + sum(group_widths)
                + (len(groups) - 1) * dot_gap
                + right_pad
            ),
        )
        return width, height

    def configure_font(self, cr: object) -> None:
        cr.select_font_face(
            "Sans",
            self.cairo.FONT_SLANT_NORMAL,
            self.cairo.FONT_WEIGHT_NORMAL,
        )
        cr.set_font_size(self.config.font_size)

    def on_draw(self, window: object, cr: object) -> bool:
        state = self.current_state or self.shared.get()
        width = max(1, window.get_allocated_width())
        height = max(1, window.get_allocated_height())

        cr.save()
        cr.set_operator(self.cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.0)
        cr.paint()
        cr.restore()

        self.draw_pill(cr, width, height)

        dot_size = 14
        left_pad = 11
        gap = 8
        dot_gap = 16
        dot_radius = dot_size / 2.0
        dot_y = (height - dot_size) / 2.0
        dot_x = float(left_pad)
        self.configure_font(cr)
        font_extents = cr.font_extents()
        ascent = cairo_metric(font_extents, "ascent", 0)
        descent = cairo_metric(font_extents, "descent", 1)
        baseline = (height + ascent - descent) / 2.0

        for label, dot_color, text_color, is_source in display_groups(state):
            dot_r, dot_g, dot_b = color_to_rgb(dot_color)
            cr.set_source_rgba(dot_r, dot_g, dot_b, self.config.dot_opacity)
            cr.arc(
                dot_x + dot_radius,
                height / 2.0,
                dot_radius,
                0,
                math.tau,
            )
            cr.fill()

            text_r, text_g, text_b = color_to_rgb(text_color)
            cr.set_source_rgba(text_r, text_g, text_b, self.config.text_opacity)
            cr.move_to(dot_x + dot_size + gap, baseline)
            cr.show_text(label)

            text_extents = cr.text_extents(label)
            text_width = max(
                cairo_metric(text_extents, "width", 2),
                cairo_metric(text_extents, "x_advance", 4),
            )
            if is_source:
                self.draw_copy_badge(cr, width, height, dot_x, dot_y, dot_size)
            dot_x += dot_size + gap + text_width + dot_gap
        return True

    def draw_copy_badge(
        self,
        cr: object,
        width: int,
        height: int,
        dot_x: float,
        dot_y: float,
        dot_size: int,
    ) -> None:
        badge_radius = 5.0
        badge_size = badge_radius * 2.0
        badge_x = min(width - badge_size, max(0.0, dot_x + dot_size - 4.0))
        badge_y = min(height - badge_size, max(0.0, dot_y - 4.0))
        center_x = badge_x + badge_radius
        center_y = badge_y + badge_radius

        badge_r, badge_g, badge_b = color_to_rgb(COPY_GLYPH_COLOR)
        cr.set_source_rgba(badge_r, badge_g, badge_b, self.config.dot_opacity)
        cr.arc(center_x, center_y, badge_radius, 0, math.tau)
        cr.fill()

        cr.set_source_rgba(1.0, 1.0, 1.0, 1.0)
        cr.set_line_width(1.5)
        cr.set_line_cap(self.cairo.LINE_CAP_ROUND)
        cr.set_line_join(self.cairo.LINE_JOIN_ROUND)
        cr.move_to(badge_x + 2.4, badge_y + 5.2)
        cr.line_to(badge_x + 4.4, badge_y + 7.1)
        cr.line_to(badge_x + 7.9, badge_y + 3.1)
        cr.stroke()

    def draw_pill(self, cr: object, width: int, height: int) -> None:
        radius = height / 2.0
        bg_r, bg_g, bg_b = color_to_rgb(PILL_BG)
        cr.set_source_rgba(bg_r, bg_g, bg_b, self.config.opacity)
        cr.new_path()
        cr.move_to(radius, 0)
        cr.line_to(width - radius, 0)
        cr.arc(width - radius, radius, radius, -math.pi / 2.0, math.pi / 2.0)
        cr.line_to(radius, height)
        cr.arc(radius, radius, radius, math.pi / 2.0, 3.0 * math.pi / 2.0)
        cr.close_path()
        cr.fill()


def wait_for_display(attempt: Callable[[], _T | None], label: str) -> _T:
    started = time.monotonic()
    last_logged = float("-inf")
    while True:
        result = attempt()
        if result is not None:
            return result
        now = time.monotonic()
        if now - started >= DISPLAY_WAIT_TIMEOUT:
            logging.info("%s display still unavailable, exiting for a clean restart", label)
            raise SystemExit(1)
        if now - last_logged >= DISPLAY_LOG_INTERVAL:
            logging.info("waiting for %s display", label)
            last_logged = now
        time.sleep(DISPLAY_RETRY_SECONDS)


def gtk_initialized(gtk: object) -> bool:
    try:
        result = gtk.init_check()  # type: ignore[attr-defined]
    except TypeError:
        result = gtk.init_check([])  # type: ignore[attr-defined]
    if isinstance(result, (tuple, list)):
        result = result[0]
    return bool(result)


def create_tk_root() -> tk.Tk:
    def attempt() -> tk.Tk | None:
        try:
            return tk.Tk()
        except tk.TclError:
            return None

    return wait_for_display(attempt, "x11")


def load_gtk_layer_backend() -> tuple[object, object, object, object] | None:
    if not os.environ.get("WAYLAND_DISPLAY"):
        return None

    # gtk-layer-shell only works on the Wayland backend; forcing it keeps
    # init_check from "succeeding" via X11 while the compositor is down.
    # Must be set before the Gtk import or GDK ignores it.
    os.environ.setdefault("GDK_BACKEND", "wayland")

    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("GtkLayerShell", "0.1")
        from gi.repository import GLib, Gtk, GtkLayerShell
        import cairo
    except (ImportError, ValueError):
        return None

    wait_for_display(lambda: gtk_initialized(Gtk) or None, "wayland")
    return Gtk, GLib, GtkLayerShell, cairo


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show lanclip connectivity and the latest clipboard source."
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
    root = create_tk_root()
    stop_event = threading.Event()
    install_signal_handlers(stop_event)
    shared = SharedState(unknown_state())

    app = IndicatorWindow(root, config, shared, stop_event)

    poll_thread = threading.Thread(
        target=poll_status,
        args=(config, shared, stop_event),
        name="lanclip-indicator-poll",
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


def run_gtk_layer(config: Config, backend: tuple[object, object, object, object]) -> int:
    Gtk, GLib, GtkLayerShell, cairo = backend
    stop_event = threading.Event()
    install_signal_handlers(stop_event)
    shared = SharedState(unknown_state())
    app = GtkLayerIndicatorWindow(
        Gtk,
        GLib,
        GtkLayerShell,
        cairo,
        config,
        shared,
        stop_event,
    )

    poll_thread = threading.Thread(
        target=poll_status,
        args=(config, shared, stop_event),
        name="lanclip-indicator-poll",
        daemon=True,
    )
    poll_thread.start()
    app.start()

    try:
        Gtk.main()
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
    gtk_backend = load_gtk_layer_backend()
    if gtk_backend is not None:
        return run_gtk_layer(config, gtk_backend)
    return run(config)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
