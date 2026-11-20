"""Window awareness on X11 via EWMH (_NET_CLIENT_LIST_STACKING), which GNOME, KDE, XFCE, Cinnamon,
MATE, i3 and most other X11 window managers maintain.

Used to (1) list and focus windows directly instead of clicking through overviews, and
(2) refuse clicks that would land on a *protected* window (by default the Claude app itself,
which pops to the front whenever the user sends a message).
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass


class WindowsUnavailable(RuntimeError):
    pass


@dataclass
class Win:
    id: int
    wm_class: str  # "instance.Class"
    title: str
    x: int
    y: int
    w: int
    h: int
    minimized: bool
    active: bool

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h

    def matches(self, query: str) -> bool:
        q = query.lower()
        return q in self.title.lower() or q in self.wm_class.lower() or q == hex(self.id) or q == str(self.id)


def protected_patterns() -> list[str]:
    raw = os.environ.get("LCU_PROTECT_WINDOWS", "claude")
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


class X11Windows:
    def __init__(self):
        try:
            from Xlib import X, Xatom, display, protocol
        except ImportError as e:  # pragma: no cover
            raise WindowsUnavailable("python-xlib is not installed") from e
        self.X, self.Xatom, self.protocol = X, Xatom, protocol
        self.d = display.Display()
        self.root = self.d.screen().root
        self._lock = threading.Lock()
        self._a = {n: self.d.intern_atom(n) for n in (
            "_NET_CLIENT_LIST_STACKING", "_NET_CLIENT_LIST", "_NET_ACTIVE_WINDOW", "_NET_WM_NAME",
            "UTF8_STRING", "_NET_WM_STATE", "_NET_WM_STATE_HIDDEN", "_NET_WM_DESKTOP",
            "_NET_CURRENT_DESKTOP", "_NET_WM_WINDOW_TYPE", "_NET_WM_WINDOW_TYPE_DESKTOP",
            "_NET_WM_WINDOW_TYPE_DOCK")}

    # -------------------------------------------------------------- helpers
    def _prop(self, win, name, typ=None):
        p = win.get_full_property(self._a[name], typ if typ is not None else self.X.AnyPropertyType)
        return p.value if p is not None else None

    def _title(self, win) -> str:
        v = self._prop(win, "_NET_WM_NAME", self._a["UTF8_STRING"])
        if v is not None:
            return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
        try:
            n = win.get_wm_name()
            return n.decode("latin-1") if isinstance(n, bytes) else (n or "")
        except Exception:
            return ""

    def _stacking(self) -> list[int]:
        """Client window ids, bottom -> top."""
        for name in ("_NET_CLIENT_LIST_STACKING", "_NET_CLIENT_LIST"):
            v = self._prop(self.root, name)
            if v is not None:
                return list(v)
        raise WindowsUnavailable("the window manager does not publish an EWMH client list")

    def windows(self) -> list[Win]:
        """Visible-workspace windows, topmost first."""
        with self._lock:
            ids = self._stacking()
            active = self._prop(self.root, "_NET_ACTIVE_WINDOW")
            active_id = int(active[0]) if active is not None and len(active) else 0
            cur = self._prop(self.root, "_NET_CURRENT_DESKTOP")
            cur_desk = int(cur[0]) if cur is not None and len(cur) else None
            out: list[Win] = []
            for wid in reversed(ids):
                try:
                    w = self.d.create_resource_object("window", wid)
                    types = self._prop(w, "_NET_WM_WINDOW_TYPE")
                    if types is not None and (self._a["_NET_WM_WINDOW_TYPE_DESKTOP"] in types
                                              or self._a["_NET_WM_WINDOW_TYPE_DOCK"] in types):
                        continue
                    desk = self._prop(w, "_NET_WM_DESKTOP")
                    if cur_desk is not None and desk is not None and len(desk):
                        dv = int(desk[0])
                        if dv != cur_desk and dv != 0xFFFFFFFF:
                            continue
                    state = self._prop(w, "_NET_WM_STATE")
                    hidden = state is not None and self._a["_NET_WM_STATE_HIDDEN"] in state
                    geo = w.get_geometry()
                    pos = self.root.translate_coords(w, 0, 0)
                    cls = w.get_wm_class() or ("", "")
                    out.append(Win(wid, ".".join(c for c in cls if c), self._title(w), pos.x, pos.y,
                                   geo.width, geo.height, hidden, wid == active_id))
                except Exception:
                    continue  # window vanished while we looked
            return out

    def at(self, px: int, py: int) -> Win | None:
        for w in self.windows():
            if not w.minimized and w.contains(px, py):
                return w
        return None

    def activate(self, win: Win) -> None:
        """EWMH activation request (source = pager), honoured by GNOME/KDE focus-stealing rules."""
        X, protocol = self.X, self.protocol
        with self._lock:
            w = self.d.create_resource_object("window", win.id)
            ev = protocol.event.ClientMessage(
                window=w, client_type=self._a["_NET_ACTIVE_WINDOW"],
                data=(32, [2, X.CurrentTime, 0, 0, 0]))
            self.root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
            self.d.flush()

    def close(self) -> None:
        try:
            self.d.close()
        except Exception:
            pass


def is_protected(win: Win | None, patterns: list[str] | None = None) -> bool:
    if win is None:
        return False
    pats = protected_patterns() if patterns is None else patterns
    cls = win.wm_class.lower()
    return any(p and p in cls for p in pats)
