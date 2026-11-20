"""Full-screen screenshot backends for X11 and Wayland.

Every backend returns PNG bytes of the whole desktop in physical pixels.
"""

from __future__ import annotations

import io
import os
import secrets
import subprocess
import tempfile
import threading
import urllib.parse
from typing import Callable

from .session import Session, which


class ScreenshotError(RuntimeError):
    pass


def _run_to_file(cmd: list[str], suffix: str = ".png", timeout: float = 15) -> bytes:
    fd, path = tempfile.mkstemp(prefix="lcu-", suffix=suffix)
    os.close(fd)
    try:
        full = [path if c == "{out}" else c for c in cmd]
        proc = subprocess.run(full, capture_output=True, timeout=timeout)
        if proc.returncode != 0:
            raise ScreenshotError(f"{cmd[0]} failed: {proc.stderr.decode(errors='replace').strip()}")
        with open(path, "rb") as f:
            data = f.read()
        if not data:
            raise ScreenshotError(f"{cmd[0]} produced an empty file")
        return data
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _run_stdout(cmd: list[str], timeout: float = 15) -> bytes:
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if proc.returncode != 0 or not proc.stdout:
        raise ScreenshotError(f"{cmd[0]} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


# ---------------------------------------------------------------- X11

_tls = threading.local()


def shot_mss():
    """Returns a PIL image directly (no PNG round trip). ~10-20 ms for 1080p."""
    import mss
    from PIL import Image

    sct = getattr(_tls, "sct", None)
    if sct is None:
        sct = _tls.sct = mss.mss()
    img = sct.grab(sct.monitors[0])  # all monitors combined
    return Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")


def shot_import() -> bytes:
    return _run_stdout(["import", "-silent", "-window", "root", "png:-"])


def shot_scrot() -> bytes:
    return _run_to_file(["scrot", "-o", "{out}"])


# ---------------------------------------------------------------- Wayland

def shot_grim() -> bytes:
    return _run_stdout(["grim", "-t", "png", "-"])


def shot_spectacle() -> bytes:
    return _run_to_file(["spectacle", "--background", "--nonotify", "--fullscreen", "--output", "{out}"])


def shot_gnome_screenshot() -> bytes:
    return _run_to_file(["gnome-screenshot", "-f", "{out}"])


def shot_portal(timeout: float = 30) -> bytes:
    """org.freedesktop.portal.Screenshot (works on GNOME, KDE and most wlroots setups with a portal).

    GNOME may show a one-time "Allow screenshots?" dialog the first time.
    """
    from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
    from jeepney.io.blocking import Proxy, open_dbus_connection

    portal = DBusAddress(
        "/org/freedesktop/portal/desktop",
        bus_name="org.freedesktop.portal.Desktop",
        interface="org.freedesktop.portal.Screenshot",
    )
    conn = open_dbus_connection(bus="SESSION")
    try:
        token = "lcu" + secrets.token_hex(6)
        sender = conn.unique_name.lstrip(":").replace(".", "_")
        handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        rule = MatchRule(
            type="signal",
            interface="org.freedesktop.portal.Request",
            member="Response",
            path=handle,
        )
        Proxy(message_bus, conn).AddMatch(rule)
        with conn.filter(rule) as queue:
            msg = new_method_call(
                portal, "Screenshot", "sa{sv}",
                ("", {"handle_token": ("s", token), "interactive": ("b", False)}),
            )
            conn.send_and_get_reply(msg, timeout=timeout)
            resp = conn.recv_until_filtered(queue, timeout=timeout)
        code, results = resp.body
        if code != 0:
            raise ScreenshotError("screenshot portal request was denied or cancelled "
                                  "(GNOME: allow screenshots for this app in Settings > Privacy)")
        uri = results["uri"][1]
        path = urllib.parse.unquote(urllib.parse.urlparse(uri).path)
        with open(path, "rb") as f:
            data = f.read()
        try:
            os.unlink(path)  # the portal saves into ~/Pictures; don't litter
        except OSError:
            pass
        return data
    finally:
        conn.close()


# ---------------------------------------------------------------- selection

BACKENDS: dict[str, tuple[Callable[[], bytes], str | None]] = {
    "mss": (shot_mss, None),
    "import": (shot_import, "import"),
    "scrot": (shot_scrot, "scrot"),
    "grim": (shot_grim, "grim"),
    "spectacle": (shot_spectacle, "spectacle"),
    "gnome-screenshot": (shot_gnome_screenshot, "gnome-screenshot"),
    "portal": (shot_portal, None),
}


def candidate_order(session: Session) -> list[str]:
    if session.kind == "x11":
        return ["mss", "import", "scrot"]
    if session.desktop == "wlroots":
        return ["grim", "portal"]
    if session.desktop == "kde":
        return ["spectacle", "portal", "grim"]
    if session.desktop == "gnome":
        return ["gnome-screenshot", "portal"]
    return ["grim", "portal", "spectacle", "gnome-screenshot"]


def available(name: str) -> bool:
    fn, binary = BACKENDS[name]
    return binary is None or which(binary) is not None


class Screenshotter:
    def __init__(self, session: Session, forced: str | None = None):
        self.session = session
        if forced:
            if forced not in BACKENDS:
                raise ValueError(f"unknown screenshot backend {forced!r}; choose from {sorted(BACKENDS)}")
            self.order = [forced]
        else:
            self.order = [n for n in candidate_order(session) if available(n)]
        self.working: str | None = None
        self.errors: dict[str, str] = {}

    @property
    def fast(self) -> bool:
        """True when a capture is cheap enough to poll for 'screen settled' detection."""
        return (self.working or (self.order[0] if self.order else "")) in ("mss", "grim")

    def capture_image(self):
        from PIL import Image

        out = self.capture()
        if isinstance(out, (bytes, bytearray)):
            im = Image.open(io.BytesIO(out))
            im.load()
            return im.convert("RGB")
        return out

    def capture(self):
        order = [self.working] if self.working else self.order
        if not order:
            raise ScreenshotError(self._help())
        for name in order:
            try:
                data = BACKENDS[name][0]()
                self.working = name
                return data
            except Exception as e:  # try next backend
                self.errors[name] = f"{type(e).__name__}: {e}"
        if self.working:  # previously-working backend broke; retry full chain once
            self.working = None
            return self.capture()
        raise ScreenshotError(self._help())

    def _help(self) -> str:
        tried = "; ".join(f"{k}: {v}" for k, v in self.errors.items()) or "none available"
        hint = {
            "x11": "pip install mss (bundled) or install imagemagick/scrot",
            "wlroots": "install grim",
            "kde": "install spectacle",
            "gnome": "install gnome-screenshot, or make sure xdg-desktop-portal-gnome is running",
        }.get(self.session.kind if self.session.kind == "x11" else self.session.desktop,
              "install grim or make sure xdg-desktop-portal is running")
        return f"No screenshot backend worked on {self.session.label}. Tried: {tried}. Hint: {hint}."


def encode(im, max_w: int, max_h: int, fmt: str = "jpeg", quality: int = 75) -> tuple[bytes, tuple[int, int], tuple[int, int]]:
    """Downscale a PIL image and encode it. Return (bytes, (shot_w, shot_h), (phys_w, phys_h))."""
    from PIL import Image

    phys = im.size
    scale = min(1.0, max_w / phys[0], max_h / phys[1])
    if scale < 1.0:
        im = im.resize((max(1, round(phys[0] * scale)), max(1, round(phys[1] * scale))),
                       Image.BILINEAR, reducing_gap=2.0)
    out = io.BytesIO()
    if fmt == "png":
        im.save(out, "PNG", compress_level=1)
    else:
        im.save(out, "JPEG", quality=quality)
    return out.getvalue(), im.size, phys


def thumbnail(im):
    """Tiny grayscale fingerprint used to detect when the screen stops changing."""
    from PIL import Image

    return im.convert("L").resize((160, 90), Image.BILINEAR, reducing_gap=None)


def frame_diff(a, b) -> float:
    """Mean absolute pixel difference (0-255) between two thumbnails."""
    from PIL import ImageChops, ImageStat

    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]

