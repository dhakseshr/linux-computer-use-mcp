"""Mouse and keyboard backends.

All coordinates here are *physical* screen pixels (what a full-resolution screenshot shows).

* XdotoolInput - X11. Moves the real pointer through the X server.
* DotoolInput  - Wayland (any compositor). Creates a virtual absolute pointer + keyboard via
                 /dev/uinput, so the compositor moves the real cursor. Needs uinput access.
* YdotoolInput - Wayland fallback (ydotool >= 1.0 with ydotoold running).
"""

from __future__ import annotations

import math
import subprocess
import threading
import time
from typing import Protocol

from . import keys as K
from .session import Session, which

BUTTONS = ("left", "right", "middle")


class InputError(RuntimeError):
    pass


def ease_in_out(t: float) -> float:
    return 0.5 - 0.5 * math.cos(math.pi * t)


def path_points(start: tuple[int, int], end: tuple[int, int], duration: float, fps: int = 60) -> list[tuple[int, int, float]]:
    """Points (x, y, delay_after) for a smooth, eased glide from start to end."""
    dist = math.dist(start, end)
    if duration <= 0 or dist < 3:
        return [(end[0], end[1], 0.0)]
    steps = max(2, min(int(duration * fps), int(dist / 2) + 1))
    dt = duration / steps
    pts = []
    for i in range(1, steps + 1):
        t = ease_in_out(i / steps)
        pts.append((round(start[0] + (end[0] - start[0]) * t), round(start[1] + (end[1] - start[1]) * t), dt))
    return pts


class InputBackend(Protocol):
    name: str

    def position(self) -> tuple[int, int] | None: ...
    def move(self, x: int, y: int, duration: float) -> None: ...
    def button(self, button: str, action: str = "click", count: int = 1) -> None: ...
    def scroll(self, direction: str, amount: int) -> None: ...
    def type_text(self, text: str, delay_ms: int) -> None: ...
    def key(self, combo: list[str]) -> None: ...
    def hold_keys(self, combo: list[str], down: bool) -> None: ...
    def close(self) -> None: ...


def _run(cmd: list[str], timeout: float = 30, stdin: str | None = None) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin)
    except FileNotFoundError as e:
        raise InputError(f"{cmd[0]} is not installed") from e
    if proc.returncode != 0:
        raise InputError(f"{' '.join(cmd[:2])} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


# ===================================================================== X11

class XdotoolInput:
    name = "xdotool"
    BTN = {"left": "1", "middle": "2", "right": "3"}
    WHEEL = {"up": "4", "down": "5", "left": "6", "right": "7"}

    def position(self) -> tuple[int, int] | None:
        out = _run(["xdotool", "getmouselocation", "--shell"])
        vals = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        return int(vals["X"]), int(vals["Y"])

    def screen_size(self) -> tuple[int, int]:
        w, h = _run(["xdotool", "getdisplaygeometry"]).split()
        return int(w), int(h)

    def move(self, x: int, y: int, duration: float) -> None:
        start = self.position() or (x, y)
        args = ["xdotool"]
        for px, py, dt in path_points(start, (x, y), duration):
            args += ["mousemove", str(px), str(py)]
            if dt:
                args += ["sleep", f"{dt:.4f}"]
        _run(args, timeout=30 + duration)

    def button(self, button: str, action: str = "click", count: int = 1) -> None:
        b = self.BTN[button]
        if action == "click":
            _run(["xdotool", "click", "--repeat", str(count), "--delay", "90", b])
        elif action == "down":
            _run(["xdotool", "mousedown", b])
        else:
            _run(["xdotool", "mouseup", b])

    def scroll(self, direction: str, amount: int) -> None:
        _run(["xdotool", "click", "--repeat", str(amount), "--delay", "30", self.WHEEL[direction]])

    def type_text(self, text: str, delay_ms: int) -> None:
        # chunk to keep each call short and responsive
        for i in range(0, len(text), 200):
            _run(["xdotool", "type", "--clearmodifiers", "--delay", str(delay_ms), "--", text[i:i + 200]],
                 timeout=60 + len(text) * delay_ms / 1000)

    def key(self, combo: list[str]) -> None:
        _run(["xdotool", "key", "--clearmodifiers", K.to_xdotool(combo)])

    def hold_keys(self, combo: list[str], down: bool) -> None:
        _run(["xdotool", "keydown" if down else "keyup", K.to_xdotool(combo)])

    def close(self) -> None:
        pass


# ===================================================================== Wayland: dotool

class DotoolInput:
    """Keeps one long-lived `dotool` process so its virtual device stays registered
    with the compositor (a fresh uinput device drops its first events)."""

    name = "dotool"
    BTN = {"left": "left", "right": "right", "middle": "middle"}

    def __init__(self, screen_size_fn):
        self._screen_size = screen_size_fn
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._pos: tuple[int, int] | None = None

    def _send(self, *lines: str) -> None:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                try:
                    self._proc = subprocess.Popen(["dotool"], stdin=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                except FileNotFoundError as e:
                    raise InputError("dotool is not installed") from e
                time.sleep(0.6)  # let the compositor pick up the new virtual device
                if self._proc.poll() is not None:
                    err = self._proc.stderr.read() if self._proc.stderr else ""
                    raise InputError(f"dotool exited: {err.strip()} (is your user allowed to write /dev/uinput? "
                                     "run `linux-computer-use-mcp setup-wayland`)")
            assert self._proc.stdin
            for line in lines:
                self._proc.stdin.write(line + "\n")
            self._proc.stdin.flush()

    def position(self) -> tuple[int, int] | None:
        return self._pos  # Wayland gives no global cursor query; we track our own moves

    def move(self, x: int, y: int, duration: float) -> None:
        w, h = self._screen_size()
        start = self._pos or (x, y)
        for px, py, dt in path_points(start, (x, y), duration):
            self._send(f"mouseto {min(max(px / w, 0), 1):.5f} {min(max(py / h, 0), 1):.5f}")
            if dt:
                time.sleep(dt)
        self._pos = (x, y)

    def button(self, button: str, action: str = "click", count: int = 1) -> None:
        b = self.BTN[button]
        cmd = {"click": "click", "down": "buttondown", "up": "buttonup"}[action]
        for i in range(count if action == "click" else 1):
            self._send(f"{cmd} {b}")
            if i + 1 < count:
                time.sleep(0.09)

    def scroll(self, direction: str, amount: int) -> None:
        if direction in ("up", "down"):
            self._send(f"wheel {amount if direction == 'up' else -amount}")
        else:
            self._send(f"hwheel {amount if direction == 'right' else -amount}")

    def type_text(self, text: str, delay_ms: int) -> None:
        self._send(f"typedelay {delay_ms}")
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line:
                self._send(f"type {line}")
            if i + 1 < len(lines):
                self._send("key enter")
        time.sleep(len(text) * delay_ms / 1000)

    def key(self, combo: list[str]) -> None:
        self._send(f"key {K.to_dotool(combo)}")

    def hold_keys(self, combo: list[str], down: bool) -> None:
        self._send(f"{'keydown' if down else 'keyup'} {K.to_dotool(combo)}")

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.close()  # type: ignore[union-attr]
                self._proc.wait(timeout=2)
            except Exception:
                self._proc.kill()


# ===================================================================== Wayland: ydotool

class YdotoolInput:
    name = "ydotool"
    BTN = {"left": 0x00, "right": 0x01, "middle": 0x02}

    def __init__(self):
        self._pos: tuple[int, int] | None = None

    def position(self) -> tuple[int, int] | None:
        return self._pos

    def move(self, x: int, y: int, duration: float) -> None:
        start = self._pos or (x, y)
        pts = path_points(start, (x, y), duration, fps=30)  # each step is a process spawn
        for px, py, dt in pts:
            _run(["ydotool", "mousemove", "--absolute", "-x", str(px), "-y", str(py)])
            if dt:
                time.sleep(dt)
        self._pos = (x, y)

    def button(self, button: str, action: str = "click", count: int = 1) -> None:
        code = self.BTN[button] | {"click": 0xC0, "down": 0x40, "up": 0x80}[action]
        args = ["ydotool", "click"]
        if action == "click" and count > 1:
            args += ["--repeat", str(count), "--next-delay", "90"]
        _run(args + [hex(code)])

    def scroll(self, direction: str, amount: int) -> None:
        dx = {"left": -amount, "right": amount}.get(direction, 0)
        dy = {"up": amount, "down": -amount}.get(direction, 0)
        _run(["ydotool", "mousemove", "--wheel", "-x", str(dx), "-y", str(dy)])

    def type_text(self, text: str, delay_ms: int) -> None:
        _run(["ydotool", "type", "--key-delay", str(delay_ms), "--file", "-"], stdin=text,
             timeout=60 + len(text) * delay_ms / 1000)

    def key(self, combo: list[str]) -> None:
        _run(["ydotool", "key", *K.to_ydotool(combo)])

    def hold_keys(self, combo: list[str], down: bool) -> None:
        seq = K.to_ydotool(combo)
        half = len(seq) // 2
        _run(["ydotool", "key", *(seq[:half] if down else seq[half:])])

    def close(self) -> None:
        pass


# ===================================================================== selection

class DryRunInput:
    """Logs instead of acting. Enabled with LCU_DRY_RUN=1."""

    name = "dry-run"

    def __init__(self, log):
        self.log = log
        self._pos: tuple[int, int] | None = None

    def position(self):
        return self._pos

    def move(self, x, y, duration):
        self._pos = (x, y)
        self.log(f"move {x},{y} ({duration:.2f}s)")

    def button(self, button, action="click", count=1):
        self.log(f"{action} {button} x{count}")

    def scroll(self, direction, amount):
        self.log(f"scroll {direction} {amount}")

    def type_text(self, text, delay_ms):
        self.log(f"type {len(text)} chars")

    def key(self, combo):
        self.log(f"key {'+'.join(combo)}")

    def hold_keys(self, combo, down):
        self.log(f"{'keydown' if down else 'keyup'} {'+'.join(combo)}")

    def close(self):
        pass


def make_input(session: Session, screen_size_fn, forced: str | None = None) -> InputBackend:
    choice = forced
    if not choice:
        if session.kind == "x11":
            choice = "xdotool"
        elif which("dotool"):
            choice = "dotool"
        elif which("ydotool"):
            choice = "ydotool"
        else:
            raise InputError(
                "No Wayland input backend found. Install dotool (recommended) or ydotool, then run "
                "`linux-computer-use-mcp setup-wayland` once to allow virtual input devices."
            )
    if choice == "xdotool":
        if not which("xdotool"):
            raise InputError("xdotool is not installed (sudo apt install xdotool / dnf install xdotool / pacman -S xdotool)")
        return XdotoolInput()
    if choice == "dotool":
        return DotoolInput(screen_size_fn)
    if choice == "ydotool":
        return YdotoolInput()
    raise InputError(f"unknown input backend {choice!r}")
