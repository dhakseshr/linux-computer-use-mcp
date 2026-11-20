"""Find and launch desktop applications via their .desktop entries (works on X11 and Wayland)."""

from __future__ import annotations

import configparser
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class DesktopApp:
    id: str
    name: str
    path: Path
    generic: str = ""


def _data_dirs() -> list[Path]:
    home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    dirs = [home] + [Path(p) for p in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if p]
    dirs += [Path("/var/lib/flatpak/exports/share"), Path.home() / ".local/share/flatpak/exports/share",
             Path("/var/lib/snapd/desktop")]
    return dirs


def list_apps() -> list[DesktopApp]:
    seen: dict[str, DesktopApp] = {}
    for d in _data_dirs():
        appdir = d / "applications"
        if not appdir.is_dir():
            continue
        for f in sorted(appdir.rglob("*.desktop")):
            app_id = str(f.relative_to(appdir)).replace("/", "-")
            if app_id in seen:
                continue
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(f, encoding="utf-8")
                e = cp["Desktop Entry"]
            except Exception:
                continue
            if e.get("Type", "Application") != "Application" or e.getboolean("NoDisplay", fallback=False) \
                    or e.getboolean("Hidden", fallback=False):
                continue
            seen[app_id] = DesktopApp(app_id, e.get("Name", app_id), f, e.get("GenericName", ""))
    return sorted(seen.values(), key=lambda a: a.name.lower())


def find_app(query: str) -> DesktopApp | None:
    q = query.strip().lower().removesuffix(".desktop")
    apps = list_apps()
    for test in (
        lambda a: a.name.lower() == q or a.id.lower().removesuffix(".desktop") == q,
        lambda a: a.id.lower().removesuffix(".desktop").split(".")[-1] == q,
        lambda a: a.name.lower().startswith(q),
        lambda a: q in a.name.lower() or q in a.generic.lower() or q in a.id.lower(),
    ):
        hits = [a for a in apps if test(a)]
        if hits:
            return hits[0]
    return None


def launch(app: DesktopApp) -> None:
    app_id = app.id.removesuffix(".desktop")
    if shutil.which("gtk-launch"):
        cmd = ["gtk-launch", app_id]
    elif shutil.which("gio"):
        cmd = ["gio", "launch", str(app.path)]
    elif shutil.which("kioclient"):
        cmd = ["kioclient", "exec", str(app.path)]
    else:
        raise RuntimeError("need gtk-launch, gio or kioclient to start applications")
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
