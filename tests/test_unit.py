from pathlib import Path

import pytest

from linux_computer_use import keys as K
from linux_computer_use.controller import Config, Controller, SafetyStop
from linux_computer_use.inputs import path_points
from linux_computer_use.screenshot import candidate_order
from linux_computer_use.session import Session, detect_session


# ---------------------------------------------------------------- session

@pytest.mark.parametrize("env,kind,desktop", [
    ({"WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}, "wayland", "gnome"),
    ({"WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "KDE"}, "wayland", "kde"),
    ({"WAYLAND_DISPLAY": "wayland-1", "XDG_CURRENT_DESKTOP": "Hyprland"}, "wayland", "wlroots"),
    ({"WAYLAND_DISPLAY": "wayland-1", "SWAYSOCK": "/run/sway"}, "wayland", "wlroots"),
    ({"DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "XFCE"}, "x11", "other"),
    ({"DISPLAY": ":0", "WAYLAND_DISPLAY": "w", "LCU_SESSION": "x11"}, "x11", "other"),
])
def test_detect(env, kind, desktop):
    s = detect_session(env)
    assert (s.kind, s.desktop) == (kind, desktop)


def test_detect_none():
    with pytest.raises(RuntimeError):
        detect_session({})


def test_screenshot_order():
    assert candidate_order(Session("x11", "gnome", ""))[0] == "mss"
    assert candidate_order(Session("wayland", "gnome", ""))[0] == "gnome-screenshot"
    assert candidate_order(Session("wayland", "wlroots", ""))[0] == "grim"
    assert candidate_order(Session("wayland", "kde", ""))[0] == "spectacle"


# ---------------------------------------------------------------- keys

@pytest.mark.parametrize("combo,expected", [
    ("ctrl+s", ["ctrl", "s"]),
    ("Ctrl+Shift+T", ["ctrl", "shift", "t"]),
    ("cmd+space", ["super", "space"]),
    ("enter", ["Return"]),
    ("esc", ["Escape"]),
    ("ctrl++", ["ctrl", "plus"]),
    ("+", ["plus"]),
    ("alt+F4", ["alt", "F4"]),
    ("pgdn", ["Page_Down"]),
])
def test_parse_combo(combo, expected):
    assert K.parse_combo(combo) == expected


def test_backend_key_translation():
    assert K.to_xdotool(["ctrl", "shift", "t"]) == "ctrl+shift+t"
    assert K.to_dotool(["ctrl", "Return"]) == "ctrl+enter"
    assert K.to_dotool(["XF86Calculator"]) == "x:XF86Calculator"
    assert K.to_ydotool(["ctrl", "s"]) == ["29:1", "31:1", "31:0", "29:0"]
    with pytest.raises(K.KeyComboError):
        K.to_ydotool(["XF86Calculator"])


# ---------------------------------------------------------------- motion

def test_path_points_smooth_and_exact():
    pts = path_points((0, 0), (300, 200), 0.25)
    assert len(pts) > 5
    assert pts[-1][:2] == (300, 200)
    xs = [p[0] for p in pts]
    assert xs == sorted(xs)  # monotonic glide
    assert path_points((0, 0), (500, 500), 0) == [(500, 500, 0.0)]


# ---------------------------------------------------------------- controller

class FakeShooter:
    order = ["fake"]
    working = "fake"
    fast = True

    def __init__(self, size):
        from PIL import Image
        self.im = Image.new("RGB", size, "white")

    def capture_image(self):
        return self.im

    capture = capture_image


class RecInput:
    name = "rec"

    def __init__(self):
        self.calls = []
        self.pos = (500, 500)

    def position(self):
        return self.pos

    def move(self, x, y, d):
        self.calls.append(("move", x, y))
        self.pos = (x, y)

    def button(self, b, action="click", count=1):
        self.calls.append(("button", b, action, count))

    def scroll(self, d, a):
        self.calls.append(("scroll", d, a))

    def type_text(self, t, delay):
        self.calls.append(("type", t))

    def key(self, combo):
        self.calls.append(("key", tuple(combo)))

    def hold_keys(self, combo, down):
        self.calls.append(("hold", tuple(combo), down))

    def close(self):
        pass


def make_ctl(tmp_path: Path, phys=(2560, 1600)):
    cfg = Config(stop_file=tmp_path / "STOP", log_file=None, move_duration=0)
    c = Controller(cfg, Session("x11", "other", ""))
    c.shooter = FakeShooter(phys)
    c._input = RecInput()
    return c


def test_scaling_roundtrip(tmp_path):
    c = make_ctl(tmp_path)
    img, fmt = c.screenshot()
    assert c.shot == (1280, 800) and c.phys == (2560, 1600)
    c.click(640, 400)
    assert ("move", 1280, 800) in c._input.calls
    assert c.cursor_position() == (640, 400)
    with pytest.raises(ValueError):
        c.click(5000, 10)


def test_modifier_click_and_drag(tmp_path):
    c = make_ctl(tmp_path)
    c.screenshot()
    c.click(10, 10, modifiers=["ctrl"])
    calls = c._input.calls
    assert calls.index(("hold", ("ctrl",), True)) < calls.index(("button", "left", "click", 1)) \
        < calls.index(("hold", ("ctrl",), False))
    c._input.calls.clear()
    c.drag(10, 10, 100, 100)
    kinds = [x[0] + (x[2] if x[0] == "button" else "") for x in c._input.calls]
    assert kinds == ["move", "buttondown", "move", "buttonup"]


def test_stop_file_blocks(tmp_path):
    c = make_ctl(tmp_path)
    c.screenshot()
    (tmp_path / "STOP").write_text("x")
    with pytest.raises(SafetyStop):
        c.click(10, 10)
    with pytest.raises(SafetyStop):
        c.screenshot()


def test_failsafe_corner(tmp_path):
    c = make_ctl(tmp_path)
    c.screenshot()
    c._input.pos = (0, 0)  # user slammed the mouse into the corner
    with pytest.raises(SafetyStop):
        c.type_text("hello")


def test_settle_returns_quickly_on_static_screen(tmp_path):
    import time
    c = make_ctl(tmp_path)
    t = time.perf_counter()
    frame = c.settle(timeout=2.0)
    assert frame is not None and time.perf_counter() - t < 0.5


def test_settle_waits_for_change_with_baseline(tmp_path):
    import threading, time
    from PIL import Image
    c = make_ctl(tmp_path)
    base = c.fingerprint()
    threading.Timer(0.3, lambda: setattr(c.shooter, "im", Image.new("RGB", (2560, 1600), "black"))).start()
    t = time.perf_counter()
    frame = c.settle(timeout=3.0, baseline=base, react=3.0)
    took = time.perf_counter() - t
    assert 0.28 < took < 1.5 and frame.getpixel((0, 0)) == (0, 0, 0)


def test_jpeg_default_and_size(tmp_path):
    c = make_ctl(tmp_path)
    data, fmt = c.screenshot()
    assert fmt == "jpeg" and data[:2] == b"\xff\xd8"


def test_settle_gives_up_quickly_when_action_has_no_effect(tmp_path):
    import time
    c = make_ctl(tmp_path)
    base = c.fingerprint()
    t = time.perf_counter()
    c.settle(timeout=3.0, baseline=base, react=0.3)
    assert time.perf_counter() - t < 0.6  # react window, not the full timeout


def test_settle_waits_for_quiet_period_during_animation(tmp_path):
    """A progress bar that keeps moving must not count as 'settled'."""
    import threading, time
    from PIL import Image, ImageDraw
    c = make_ctl(tmp_path)
    stop = time.perf_counter() + 0.8

    def animate():
        x = 0
        while time.perf_counter() < stop:
            im = Image.new("RGB", (2560, 1600), "white")
            ImageDraw.Draw(im).rectangle([0, 800, x, 830], fill="black")  # thin growing bar
            c.shooter.im = im
            x += 120
            time.sleep(0.04)

    th = threading.Thread(target=animate)
    th.start()
    t = time.perf_counter()
    c.settle(timeout=3.0, quiet=0.3)
    took = time.perf_counter() - t
    th.join()
    assert took >= 0.75, took  # kept waiting while the bar moved


def test_frame_diff_ignores_caret_blink():
    from PIL import Image, ImageDraw
    from linux_computer_use.screenshot import CHANGED, frame_diff, thumbnail
    a = Image.new("RGB", (1920, 1080), "white")
    b = a.copy()
    ImageDraw.Draw(b).rectangle([500, 500, 501, 520], fill="black")  # 2px-wide text caret
    assert frame_diff(thumbnail(a), thumbnail(b)) <= CHANGED
    ImageDraw.Draw(b).rectangle([0, 900, 600, 906], fill="black")  # thin progress bar
    assert frame_diff(thumbnail(a), thumbnail(b)) > CHANGED


def test_newlines_become_return_presses(tmp_path):
    c = make_ctl(tmp_path)
    c.screenshot()
    c.type_text("first\nsecond\n")
    assert c._input.calls == [("type", "first"), ("key", ("Return",)), ("type", "second"), ("key", ("Return",))]
