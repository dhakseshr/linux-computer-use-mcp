"""Detect what kind of Linux desktop session we are running in."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class Session:
    kind: str  # "x11" | "wayland"
    desktop: str  # "gnome" | "kde" | "wlroots" | "other"
    raw_desktop: str

    @property
    def label(self) -> str:
        return f"{self.kind}/{self.desktop} ({self.raw_desktop or 'unknown desktop'})"


WLROOTS_DESKTOPS = ("sway", "hyprland", "river", "wayfire", "labwc", "niri", "dwl", "cosmic", "wlroots")


def detect_session(env: dict[str, str] | None = None) -> Session:
    env = dict(os.environ if env is None else env)

    override = env.get("LCU_SESSION", "").strip().lower()
    if override in ("x11", "wayland"):
        kind = override
    elif env.get("WAYLAND_DISPLAY") or env.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        kind = "wayland"
    elif env.get("DISPLAY") or env.get("XDG_SESSION_TYPE", "").lower() == "x11":
        kind = "x11"
    else:
        raise RuntimeError(
            "No graphical session found: neither WAYLAND_DISPLAY nor DISPLAY is set. "
            "Run the server from inside your desktop session, or set DISPLAY / WAYLAND_DISPLAY "
            "in your MCP client's env config."
        )

    raw = (env.get("XDG_CURRENT_DESKTOP") or env.get("XDG_SESSION_DESKTOP") or env.get("DESKTOP_SESSION") or "")
    low = raw.lower()
    if "gnome" in low or "unity" in low or "ubuntu" in low or "pop" in low or "budgie" in low:
        desktop = "gnome"
    elif "kde" in low or "plasma" in low:
        desktop = "kde"
    elif any(w in low for w in WLROOTS_DESKTOPS) or env.get("SWAYSOCK") or env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        desktop = "wlroots"
    else:
        desktop = "other"
    return Session(kind=kind, desktop=desktop, raw_desktop=raw)


def which(name: str) -> str | None:
    return shutil.which(name)
