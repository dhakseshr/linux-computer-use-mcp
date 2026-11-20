"""Glue: screenshots, coordinate scaling, safety checks and input actions."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import keys as K
from .inputs import BUTTONS, DryRunInput, InputBackend, make_input
from .screenshot import Screenshotter, downscale, png_size
from .session import Session, detect_session


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
    image_format: str = "png"  # png | jpeg
    move_duration: float = 0.25  # seconds for the visible pointer glide; 0 = jump
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
        c.move_duration = float(os.environ.get("LCU_MOVE_DURATION", c.move_duration))
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


class Controller:
    def __init__(self, config: Config | None = None, session: Session | None = None):
        self.config = config or Config.from_env()
        self.session = session or detect_session()
        self.shooter = Screenshotter(self.session, self.config.screenshot_backend)
        self.phys: tuple[int, int] | None = None  # physical screen size
        self.shot: tuple[int, int] | None = None  # size of the image we last returned
        self._last_target: tuple[int, int] | None = None
        self._input: InputBackend | None = None

    # ------------------------------------------------------------ plumbing

    @property
    def input(self) -> InputBackend:
        if self._input is None:
            if self.config.dry_run:
                self._input = DryRunInput(lambda m: self.log(f"[dry-run] {m}"))
            else:
                self._input = make_input(self.session, self.physical_size, self.config.input_backend)
        return self._input

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
                self.phys = png_size(self.shooter.capture())
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

    def screenshot(self) -> tuple[bytes, str]:
        if self.config.stop_file.exists():
            raise SafetyStop(f"Computer control is paused (stop file {self.config.stop_file} exists).")
        raw = self.shooter.capture()
        img, shot, phys = downscale(raw, self.config.max_width, self.config.max_height, self.config.image_format)
        self.shot, self.phys = shot, phys
        return img, ("jpeg" if self.config.image_format == "jpeg" else "png")

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
            self.move(x, y)
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
            self.move(x, y)
        self.guard(f"scroll {direction} {amount}")
        self.input.scroll(direction, amount)

    def type_text(self, text: str) -> None:
        if not text:
            return
        self.guard(f"type {len(text)} chars")
        self.input.type_text(text, self.config.type_delay_ms)

    def key(self, combo: str, repeat: int = 1) -> None:
        keys = K.parse_combo(combo)
        if not 1 <= repeat <= 50:
            raise ValueError("repeat must be between 1 and 50")
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
