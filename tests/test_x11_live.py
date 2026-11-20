"""End-to-end test over a real MCP stdio connection on an X server.

Runs only when DISPLAY is set and xdotool + xterm are installed, e.g.:
    xvfb-run -s "-screen 0 1920x1080x24" pytest tests/test_x11_live.py
"""

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DISPLAY") and shutil.which("xdotool") and shutil.which("xterm")),
    reason="needs an X display with xdotool and xterm",
)


def _err(res):
    return getattr(res, "is_error", None) if hasattr(res, "is_error") else res.isError


def _client():
    try:
        from mcp.client.stdio import StdioServerParameters, stdio_client
        from mcp import ClientSession
    except ImportError:  # pragma: no cover
        pytest.skip("mcp client not available")
    return StdioServerParameters, stdio_client, ClientSession


def test_end_to_end(tmp_path: Path):
    out = tmp_path / "typed.txt"
    term = subprocess.Popen(["xterm", "-geometry", "100x30+100+100", "-e", "bash", "--norc"])
    time.sleep(1.5)
    Params, stdio_client, ClientSession = _client()
    env = dict(os.environ, LCU_STOP_FILE=str(tmp_path / "STOP"), LCU_LOG_FILE=str(tmp_path / "log"))

    async def run():
        params = Params(command=sys.executable, args=["-m", "linux_computer_use"], env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                assert {"screenshot", "click", "type_text", "press_key", "drag", "scroll"} <= names

                shot = await s.call_tool("screenshot", {})
                kinds = [c.type for c in shot.content]
                assert "image" in kinds, shot
                text = next(c.text for c in shot.content if c.type == "text")
                assert "1280x720" in text and "1920x1080" in text, text

                # xterm sits at physical (100,100)+; screenshot scale is 1280/1920
                r1 = await s.call_tool("click", {"x": 200, "y": 150})
                assert not _err(r1), r1
                pos = await s.call_tool("cursor_position", {})
                assert pos.content[0].text == "200,150"

                info = await s.call_tool("system_info", {})
                assert "input: xtest" in info.content[0].text, info

                # one round trip: type + Enter, returns a screenshot after the screen settles
                res = await s.call_tool("batch", {"actions": [
                    {"action": "type", "text": f"echo 'hello from claude' > {out}"},
                    {"action": "key", "keys": "enter"},
                ]})
                assert not _err(res), res
                assert [c.type for c in res.content] == ["text", "image"], res
                assert "Ran 2 actions" in res.content[0].text

                (tmp_path / "STOP").write_text("x")
                blocked = await s.call_tool("click", {"x": 10, "y": 10})
                assert _err(blocked) and "paused" in str(blocked.content).lower()

    try:
        asyncio.run(run())
        assert out.read_text().strip() == "hello from claude"
        assert "click left" in (tmp_path / "log").read_text()
    finally:
        term.kill()
