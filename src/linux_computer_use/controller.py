"""Glue: screenshots, coordinate scaling, safety checks and input actions."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import keys as K
from .inputs import BUTTONS, DryRunInput, InputBackend, make_input
from .screenshot import CHANGED, Screenshotter, encode, frame_diff, thumbnail
from .session import Session, detect_session
from . import windows as W


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _xdg(var: str, fallback: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / fallback)


@dataclass
class Config:
    max_width: int = 1280
    max_height: int = 800
    image_format: str = "jpeg"  # jpeg (fast, small) | png
    jpeg_quality: int = 75
    move_duration: float = 0.12  # seconds for the visible pointer glide; 0 = jump
    auto_screenshot: bool = True  # actions return a screenshot once the screen settles
    settle_timeout: float = 2.0  # max seconds to wait for the screen to stop changing
    type_delay_ms: int = 12
    dry_run: bool = False
    failsafe: bool = True
    screenshot_backend: str | None = None
    input_backend: str | None = None
    stop_file: Path = field(default_factory=lambda: _xdg("XDG_CONFIG_HOME", ".config") / "linux-computer-use" / "STOP")
    log_file: Path | None = field(default_factory=lambda: _xdg("XDG_STATE_HOME", ".local/state") / "linux-computer-use" / "actions.log")

    @classmethod
    def from_env(cls) -> "Config":
        c = cls()
        c.max_width = int(os.environ.get("LCU_MAX_WIDTH", c.max_width))
        c.max_height = int(os.environ.get("LCU_MAX_HEIGHT", c.max_height))
        c.image_format = os.environ.get("LCU_IMAGE_FORMAT", c.image_format).lower()
        c.jpeg_quality = int(os.environ.get("LCU_JPEG_QUALITY", c.jpeg_quality))
        c.move_duration = float(os.environ.get("LCU_MOVE_DURATION", c.move_duration))
        c.auto_screenshot = _env_bool("LCU_AUTO_SCREENSHOT", c.auto_screenshot)
        c.settle_timeout = float(os.environ.get("LCU_SETTLE_TIMEOUT", c.settle_timeout))
        c.type_delay_ms = int(os.environ.get("LCU_TYPE_DELAY_MS", c.type_delay_ms))
        c.dry_run = _env_bool("LCU_DRY_RUN", c.dry_run)
        c.failsafe = _env_bool("LCU_FAILSAFE", c.failsafe)
        c.screenshot_backend = os.environ.get("LCU_SCREENSHOT_BACKEND") or None
        c.input_backend = os.environ.get("LCU_INPUT_BACKEND") or None
        if os.environ.get("LCU_STOP_FILE"):
            c.stop_file = Path(os.environ["LCU_STOP_FILE"]).expanduser()
        if os.environ.get("LCU_LOG_FILE") is not None:
            c.log_file = Path(os.environ["LCU_LOG_FILE"]).expanduser() if os.environ["LCU_LOG_FILE"] else None
        return c


class SafetyStop(RuntimeError):
    pass


class ProtectedWindow(RuntimeError):
    pass


class Controller:
    def __init__(self, config: Config | None = None, session: Session | None = None):
        self.config = config or Config.from_env()
        self.session = session or detect_session()
        self.shooter = Screenshotter(self.session, self.config.screenshot_backend)
        self.phys: tuple[int, int] | None = None  # physical screen size
        self.shot: tuple[int, int] | None = None  # size of the image we last returned
        self._last_target: tuple[int, int] | None = None
        self._input: InputBackend | None = None
        self._windows = None  # X11Windows | False (unavailable) | None (not tried yet)

    # ------------------------------------------------------------ plumbing

    @property
    def input(self) -> InputBackend:
        if self._input is None:
            if self.config.dry_run:
                self._input = DryRunInput(lambda m: self.log(f"[dry-run] {m}"))
            else:
                self._input = make_input(self.session, self.physical_size, self.config.input_backend)
        return self._input

    @property
    def windows(self):
        """X11 window helper, or None where unavailable (Wayland, no EWMH, dry-run)."""
        if self._windows is None:
            self._windows = False
            if self.session.kind == "x11" and not self.config.dry_run:
                try:
                    self._windows = W.X11Windows()
                except Exception:
                    self._windows = False
        return self._windows or None

    def check_point(self, px: int, py: int, what: str) -> None:
        """Refuse to click/drag/scroll onto a protected window (default: the Claude app)."""
        wm = self.windows
        if not wm:
            return
        try:
            win = wm.at(px, py)
        except Exception:
            return
        if W.is_protected(win):
            sx, sy = self.to_shot(px, py)
            raise ProtectedWindow(
                f"Not performing {what}: at {sx},{sy} the topmost window is '{win.title or win.wm_class}' "
                f"({win.wm_class}), which is protected, so the input would land there instead of the "
                "intended target. Bring the target app forward with focus_window first, then retry.")

    def check_focus(self, what: str) -> None:
        """Refuse keyboard input while a protected window (e.g. Claude's own chat box) has focus."""
        wm = self.windows
        if not wm:
            return
        try:
            active = next((w for w in wm.windows() if w.active), None)
        except Exception:
            return
        if W.is_protected(active):
            raise ProtectedWindow(
                f"Not performing {what}: the focused window is '{active.title or active.wm_class}', which "
                "is protected (keystrokes would go into it). Use focus_window to focus the target app first.")

    def list_windows(self):
        wm = self.windows
        if not wm:
            raise W.WindowsUnavailable("window listing needs an X11 session with an EWMH window manager")
        return wm.windows()

    def focus_window(self, query: str):
        wins = self.list_windows()
        hits = [w for w in wins if w.matches(query)]
        if not hits:
            raise ValueError(f"no window matches {query!r}; open windows: "
                             + "; ".join(f"{w.title} [{w.wm_class}]" for w in wins[:20]))
        target = next((w for w in hits if not W.is_protected(w)), hits[0])
        self.guard(f"focus_window {target.title!r}")
        self.windows.activate(target)
        return target

    def zoom(self, x: float, y: float, w: float, h: float) -> tuple[bytes, str, tuple[int, int]]:
        """Full-resolution crop of a region given in screenshot coordinates (for small text)."""
        if w <= 0 or h <= 0:
            raise ValueError("width and height must be positive")
        x0, y0 = self.to_phys(x, y)
        shot, phys = self._scale_dims()
        x1 = min(phys[0], round((x + w) * phys[0] / shot[0]))
        y1 = min(phys[1], round((y + h) * phys[1] / shot[1]))
        if self.config.stop_file.exists():
            raise SafetyStop(f"Computer control is paused (stop file {self.config.stop_file} exists).")
        crop = self.shooter.capture_image().crop((x0, y0, max(x0 + 1, x1), max(y0 + 1, y1)))
        fmt = "png" if self.config.image_format == "png" else "jpeg"
        data, size, _ = encode(crop, self.config.max_width, self.config.max_height, fmt, self.config.jpeg_quality)
        return data, fmt, size

    def log(self, msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, file=sys.stderr, flush=True)
        if self.config.log_file:
            try:
                self.config.log_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self.config.log_file, "a") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def physical_size(self) -> tuple[int, int]:
        if self.phys is None:
            if self.session.kind == "x11" and hasattr(self._input, "screen_size"):
                self.phys = self._input.screen_size()  # type: ignore[union-attr]
            else:
                self.phys = self.shooter.capture_image().size
        return self.phys

    def _scale_dims(self) -> tuple[tuple[int, int], tuple[int, int]]:
        phys = self.physical_size()
        if self.shot is None:
            s = min(1.0, self.config.max_width / phys[0], self.config.max_height / phys[1])
            self.shot = (max(1, round(phys[0] * s)), max(1, round(phys[1] * s)))
        return self.shot, phys

    def to_phys(self, x: float, y: float) -> tuple[int, int]:
        shot, phys = self._scale_dims()
        if not (0 <= x < shot[0] + 1 and 0 <= y < shot[1] + 1):
            raise ValueError(f"({x}, {y}) is outside the screenshot area {shot[0]}x{shot[1]}")
        return (min(phys[0] - 1, round(x * phys[0] / shot[0])), min(phys[1] - 1, round(y * phys[1] / shot[1])))

    def to_shot(self, x: int, y: int) -> tuple[int, int]:
        shot, phys = self._scale_dims()
        return round(x * shot[0] / phys[0]), round(y * shot[1] / phys[1])

    def guard(self, action: str) -> None:
        if self.config.stop_file.exists():
            raise SafetyStop(
                f"Computer control is paused (stop file {self.config.stop_file} exists). "
                "The user must run `linux-computer-use-mcp resume` before more actions."
            )
        if self.config.failsafe and not self.config.dry_run:
            try:
                pos = self.input.position()
            except Exception:
                pos = None
            if pos is not None and pos[0] <= 1 and pos[1] <= 1 and self._last_target not in ((0, 0), (0, 1), (1, 0), (1, 1)):
                raise SafetyStop(
                    "Failsafe triggered: the user moved the mouse into the top-left corner of the screen. "
                    "Stop and ask the user before continuing."
                )
        self.log(action)

    # ------------------------------------------------------------ actions

    def screenshot(self, image=None) -> tuple[bytes, str]:
        """Encode `image` (a frame we already grabbed) or capture a fresh one."""
        if self.config.stop_file.exists():
            raise SafetyStop(f"Computer control is paused (stop file {self.config.stop_file} exists).")
        im = image if image is not None else self.shooter.capture_image()
        fmt = "png" if self.config.image_format == "png" else "jpeg"
        data, shot, phys = encode(im, self.config.max_width, self.config.max_height, fmt, self.config.jpeg_quality)
        self.shot, self.phys = shot, phys
        return data, fmt

    def fingerprint(self):
        return thumbnail(self.shooter.capture_image()) if self.shooter.fast else None

    def settle(self, timeout: float | None = None, baseline=None, react: float = 0.0,
               quiet: float = 0.2):
        """Wait for the UI instead of sleeping a fixed time. Returns the last full frame (reused as
        the screenshot) or None when captures are too slow to poll.

        1. Reaction: if `baseline` (a fingerprint from *before* the action) is given, first wait up
           to `react` seconds for the screen to differ from it. No change in that window means the
           action had no visible effect, so return right away.
        2. Quiet period: then wait until nothing has changed for `quiet` seconds, so we don't
           return mid-animation or while a page is still painting.
        `timeout` caps the total time.
        """
        timeout = self.config.settle_timeout if timeout is None else timeout
        if not self.shooter.fast:  # e.g. gnome-screenshot/portal: polling would be slow/flashy
            time.sleep(min(max(0.4, react), timeout))
            return None
        t0 = time.perf_counter()
        deadline = t0 + timeout
        frame = self.shooter.capture_image()
        th = thumbnail(frame)

        if baseline is not None and frame_diff(th, baseline) <= CHANGED:
            react_end = min(deadline, t0 + react)
            while time.perf_counter() < react_end:
                time.sleep(0.035)
                frame = self.shooter.capture_image()
                th = thumbnail(frame)
                if frame_diff(th, baseline) > CHANGED:
                    break
            else:
                return frame  # nothing visibly happened

        prev, last_change = th, time.perf_counter()
        while time.perf_counter() < deadline:
            time.sleep(0.035)
            frame = self.shooter.capture_image()
            th = thumbnail(frame)
            now = time.perf_counter()
            if frame_diff(th, prev) > CHANGED:
                last_change = now
            elif now - last_change >= quiet:
                break
            prev = th
        return frame

    def move(self, x: float, y: float, duration: float | None = None) -> None:
        px, py = self.to_phys(x, y)
        self.guard(f"move -> ({x}, {y}) [phys {px},{py}]")
        self.input.move(px, py, self.config.move_duration if duration is None else duration)
        self._last_target = (px, py)

    def click(self, x: float | None, y: float | None, button: str = "left", count: int = 1,
              modifiers: list[str] | None = None) -> None:
        if button not in BUTTONS:
            raise ValueError(f"button must be one of {BUTTONS}")
        if not 1 <= count <= 3:
            raise ValueError("count must be 1, 2 or 3")
        if x is not None and y is not None:
            self.check_point(*self.to_phys(x, y), "click")
            self.move(x, y)
        else:
            pos = self.input.position()
            if pos:
                self.check_point(*pos, "click")
        self.guard(f"click {button} x{count}" + (f" with {'+'.join(modifiers)}" if modifiers else ""))
        mods = [K.normalize_key(m) for m in (modifiers or [])]
        if mods:
            self.input.hold_keys(mods, True)
        try:
            self.input.button(button, "click", count)
        finally:
            if mods:
                self.input.hold_keys(mods, False)

    def drag(self, x1: float, y1: float, x2: float, y2: float, button: str = "left") -> None:
        self.check_point(*self.to_phys(x1, y1), "drag")
        self.move(x1, y1)
        self.guard(f"drag {button} ({x1},{y1}) -> ({x2},{y2})")
        self.input.button(button, "down")
        try:
            time.sleep(0.05)
            px, py = self.to_phys(x2, y2)
            self.input.move(px, py, max(0.4, self.config.move_duration * 2))
            self._last_target = (px, py)
            time.sleep(0.05)
        finally:
            self.input.button(button, "up")

    def scroll(self, x: float | None, y: float | None, direction: str, amount: int = 3) -> None:
        if direction not in ("up", "down", "left", "right"):
            raise ValueError("direction must be up, down, left or right")
        if not 1 <= amount <= 50:
            raise ValueError("amount must be between 1 and 50")
        if x is not None and y is not None:
            self.check_point(*self.to_phys(x, y), "scroll")
            self.move(x, y)
        self.guard(f"scroll {direction} {amount}")
        self.input.scroll(direction, amount)

    def type_text(self, text: str) -> None:
        if not text:
            return
        self.check_focus("typing")
        self.guard(f"type {len(text)} chars")
        # A literal "\n" sent through xdotool/ydotool is not a real Return key press in many apps
        # (GTK file dialogs, browser address bars), so newlines become explicit Return presses.
        lines = text.replace("\r\n", "\n").split("\n")
        for i, line in enumerate(lines):
            if line:
                self.input.type_text(line, self.config.type_delay_ms)
            if i + 1 < len(lines):
                time.sleep(0.03)
                self.input.key(["Return"])

    def key(self, combo: str, repeat: int = 1) -> None:
        keys = K.parse_combo(combo)
        if not 1 <= repeat <= 50:
            raise ValueError("repeat must be between 1 and 50")
        # Desktop-level shortcuts (Super..., Alt+Tab) are handled by the window manager, not the
        # focused app, so they stay allowed even when a protected window has focus.
        if not ("super" in keys or keys in (["alt", "Tab"], ["alt", "shift", "Tab"])):
            self.check_focus(f"key {'+'.join(keys)}")
        for i in range(repeat):
            self.guard(f"key {'+'.join(keys)}" + (f" ({i + 1}/{repeat})" if repeat > 1 else ""))
            self.input.key(keys)
            if repeat > 1:
                time.sleep(0.04)

    def hold_key(self, combo: str, seconds: float) -> None:
        keys = K.parse_combo(combo)
        if not 0 < seconds <= 10:
            raise ValueError("seconds must be in (0, 10]")
        self.guard(f"hold {'+'.join(keys)} {seconds}s")
        self.input.hold_keys(keys, True)
        try:
            time.sleep(seconds)
        finally:
            self.input.hold_keys(keys, False)

    def cursor_position(self) -> tuple[int, int] | None:
        pos = self.input.position()
        return None if pos is None else self.to_shot(*pos)

    def close(self) -> None:
        if self._input:
            self._input.close()
        if self._windows:
            self._windows.close()
