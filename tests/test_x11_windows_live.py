"""Live test of window awareness: protected-window guard, list_windows, focus_window.

Needs an X display with an EWMH window manager (e.g. openbox), xdotool and xterm:
    xvfb-run -s "-screen 0 1920x1080x24" pytest tests/test_x11_windows_live.py
"""

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

WM = next((w for w in ("openbox", "fluxbox", "xfwm4") if shutil.which(w)), None)
pytestmark = pytest.mark.skipif(
    not (os.environ.get("DISPLAY") and WM and shutil.which("xterm") and shutil.which("xdotool")),
    reason="needs an X display, an EWMH window manager, xterm and xdotool",
)


def _err(res):
    return res.is_error if hasattr(res, "is_error") else res.isError


def _text(res):
    return " ".join(getattr(c, "text", "") for c in res.content)


def test_guard_and_focus(tmp_path: Path):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    out = tmp_path / "typed.txt"
    procs = [subprocess.Popen([WM], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]
    time.sleep(1.0)
    # the target terminal, and a fake "Claude" window placed on top of part of it
    procs.append(subprocess.Popen(["xterm", "-T", "Target Terminal", "-geometry", "120x40+50+50",
                                   "-e", "bash", "--norc"]))
    time.sleep(1.0)
    procs.append(subprocess.Popen(["xterm", "-class", "Claude", "-name", "claude", "-T", "Claude",
                                   "-geometry", "60x40+300+80"]))
    time.sleep(1.5)
    env = dict(os.environ, LCU_STOP_FILE=str(tmp_path / "STOP"), LCU_LOG_FILE="")

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "linux_computer_use"], env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                await s.call_tool("screenshot", {})

                lw = _text(await s.call_tool("list_windows", {}))
                assert "Target Terminal" in lw and "Claude" in lw, lw
                assert lw.index("Claude") < lw.index("Target Terminal"), "Claude should be topmost"

                # (1) click on a point covered by the protected Claude window is refused
                blocked = await s.call_tool("click", {"x": 300, "y": 150})
                assert _err(blocked) and "protected" in _text(blocked), _text(blocked)

                # (2) typing while Claude has focus is refused, but a Super shortcut is allowed
                t = await s.call_tool("type_text", {"text": "should not be typed"})
                assert _err(t) and "focused window" in _text(t), _text(t)

                # (3) focus_window brings the target forward; then clicking + typing works
                f = await s.call_tool("focus_window", {"query": "Target"})
                assert not _err(f), _text(f)
                lw2 = _text(await s.call_tool("list_windows", {}))
                assert lw2.splitlines()[0].startswith("Target Terminal"), lw2
                ok = await s.call_tool("click", {"x": 300, "y": 150})
                assert not _err(ok), _text(ok)
                res = await s.call_tool("type_text", {"text": f"echo window-ok > {out}\n"})
                assert not _err(res), _text(res)

                # (4) zoom returns a crop image
                z = await s.call_tool("zoom", {"x": 30, "y": 30, "width": 200, "height": 100})
                assert [c.type for c in z.content] == ["text", "image"]

    try:
        asyncio.run(run())
        deadline = time.time() + 3
        while time.time() < deadline and not out.exists():
            time.sleep(0.1)
        assert out.read_text().strip() == "window-ok"
    finally:
        for p in reversed(procs):
            p.kill()
