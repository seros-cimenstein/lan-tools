#!/usr/bin/env python3
"""Merge several i3bar-protocol status commands into one swaybar status line.

swaybar runs a single status_command. This runs each given command through
`sh -c`, concatenates their latest block arrays in argument order, and routes
swaybar click events back to the child that produced the clicked block.

Children must print one block array per line, as lanmouse-indicator and
lanclip-indicator do.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading


class Child:
    def __init__(self, command: str) -> None:
        self.command = command
        self.proc = subprocess.Popen(
            ["sh", "-c", command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        header = json.loads(self.proc.stdout.readline())
        self.click_events = bool(header.get("click_events"))
        self.blocks: list[dict[str, object]] = []
        self.clicks_started = False

    def send_click(self, event: str) -> None:
        if not self.click_events:
            return
        try:
            if not self.clicks_started:
                self.proc.stdin.write("[\n")
                self.clicks_started = True
                self.proc.stdin.write(event + "\n")
            else:
                self.proc.stdin.write("," + event + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            self.click_events = False


def parse_line(line: str) -> object | None:
    # Both directions are endless JSON arrays: "[" alone, then one element per
    # line, each after the first prefixed by ",". Some writers trail "," instead.
    line = line.strip().removeprefix(",").removesuffix(",").strip()
    if not line or line == "[":
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def stop_children(children: list[Child]) -> None:
    # One SIGTERM, then SIGKILL stragglers so a sway reload never leaks producers.
    for child in children:
        try:
            child.proc.stdin.close()
        except (OSError, ValueError):
            pass
        if child.proc.poll() is None:
            child.proc.terminate()
    for child in children:
        try:
            child.proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            child.proc.kill()
            child.proc.wait()


def main(commands: list[str]) -> int:
    if not commands:
        print("usage: swaybar-merge.py COMMAND...", file=sys.stderr)
        return 2

    children: list[Child] = []
    lock = threading.Lock()
    changed = threading.Condition(lock)
    dirty = True

    def shutdown(signum: int, frame: object) -> None:
        del frame
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    def read_child(child: Child) -> None:
        nonlocal dirty
        for line in child.proc.stdout:
            blocks = parse_line(line)
            if isinstance(blocks, list):
                with changed:
                    child.blocks = blocks
                    dirty = True
                    changed.notify()
        with changed:
            child.blocks = []
            dirty = True
            changed.notify()
        print(f"swaybar-merge: exited: {child.command}", file=sys.stderr)

    def read_clicks() -> None:
        for line in sys.stdin:
            event = parse_line(line)
            if not isinstance(event, dict):
                continue
            raw = json.dumps(event)
            with lock:
                owners = [
                    child
                    for child in children
                    if any(
                        block.get("name") == event.get("name")
                        and block.get("instance") == event.get("instance")
                        for block in child.blocks
                    )
                ]
            for child in owners:
                child.send_click(raw)

    try:
        children.extend(Child(command) for command in commands)
        readers = [
            threading.Thread(target=read_child, args=(child,), daemon=True)
            for child in children
        ]
        for reader in readers:
            reader.start()
        threading.Thread(target=read_clicks, daemon=True).start()

        click_events = any(child.click_events for child in children)
        header = {"version": 1, "click_events": click_events}
        sys.stdout.write(json.dumps(header) + "\n[\n")
        separator = ""
        last_line: str | None = None
        while any(reader.is_alive() for reader in readers):
            with changed:
                changed.wait_for(lambda: dirty, timeout=1.0)
                dirty = False
                merged = [block for child in children for block in child.blocks]
            line = json.dumps(merged, separators=(",", ":"))
            if line != last_line:
                sys.stdout.write(f"{separator}{line}\n")
                sys.stdout.flush()
                separator = ","
                last_line = line
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    finally:
        stop_children(children)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
