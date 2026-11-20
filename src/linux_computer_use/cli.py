"""Command line: run the server, diagnose the setup, pause/resume control."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__

UDEV_RULE = 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"\n'


def _ok(flag: bool) -> str:
    return "\033[32m✔\033[0m" if flag else "\033[31m✘\033[0m"


def cmd_doctor(_args) -> int:
    from .controller import Config
    from .screenshot import Screenshotter
    from .session import detect_session

    print(f"linux-computer-use-mcp {__version__}\n")
    for var in ("XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "WAYLAND_DISPLAY", "DISPLAY"):
        print(f"  {var:20} {os.environ.get(var, '-')}")
    try:
        s = detect_session()
    except RuntimeError as e:
        print(f"\n{_ok(False)} {e}")
        return 1
    print(f"\nSession: {s.label}\n")

    problems = 0
    if s.kind == "x11":
        has = bool(shutil.which("xdotool"))
        print(f"{_ok(has)} xdotool (mouse/keyboard)")
        problems += not has
    else:
        dotool, ydotool = shutil.which("dotool"), shutil.which("ydotool")
        print(f"{_ok(bool(dotool or ydotool))} input tool: dotool={'yes' if dotool else 'no'} (recommended), "
              f"ydotool={'yes' if ydotool else 'no'}")
        problems += not (dotool or ydotool)
        uinput = Path("/dev/uinput")
        writable = uinput.exists() and os.access(uinput, os.W_OK)
        print(f"{_ok(writable)} /dev/uinput writable by {getpass.getuser()}"
              + ("" if writable else "  -> run `linux-computer-use-mcp setup-wayland`"))
        problems += not writable
        if ydotool and not dotool:
            sock = os.environ.get("YDOTOOL_SOCKET") or f"/run/user/{os.getuid()}/.ydotool_socket"
            running = Path(sock).exists() or Path("/tmp/.ydotool_socket").exists()
            print(f"{_ok(running)} ydotoold running")
            problems += not running

    shooter = Screenshotter(s, Config.from_env().screenshot_backend)
    try:
        data = shooter.capture()
        from .screenshot import png_size
        w, h = png_size(data)
        print(f"{_ok(True)} screenshot via {shooter.working}: {w}x{h}")
    except Exception as e:
        print(f"{_ok(False)} screenshot: {e}")
        problems += 1

    stop = Config.from_env().stop_file
    if stop.exists():
        print(f"{_ok(False)} control is PAUSED (stop file {stop}); run `linux-computer-use-mcp resume`")
        problems += 1

    print("\nAll good. Add the server to your MCP client (see `linux-computer-use-mcp config`)." if not problems
          else f"\n{problems} problem(s) found. See README → Troubleshooting.")
    return 1 if problems else 0


def cmd_stop(_args) -> int:
    from .controller import Config

    p = Config.from_env().stop_file
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("paused\n")
    print("Paused. Claude can no longer control the computer until you run `linux-computer-use-mcp resume`.")
    return 0


def cmd_resume(_args) -> int:
    from .controller import Config

    p = Config.from_env().stop_file
    p.unlink(missing_ok=True)
    print("Resumed. Claude can control the computer again.")
    return 0


def cmd_setup_wayland(args) -> int:
    user = getpass.getuser()
    steps = [
        ["sudo", "modprobe", "uinput"],
        ["sudo", "sh", "-c", "echo uinput > /etc/modules-load.d/uinput.conf"],
        ["sudo", "sh", "-c", f"printf '%s' '{UDEV_RULE.strip()}' > /etc/udev/rules.d/80-uinput.rules"],
        ["sudo", "udevadm", "control", "--reload-rules"],
        ["sudo", "udevadm", "trigger", "--name-match=uinput"],
        ["sudo", "usermod", "-aG", "input", user],
    ]
    print("Wayland needs a virtual input device (/dev/uinput) to move your pointer and type.\n"
          f"This allows members of the 'input' group (you: {user}) to create one:\n")
    for s in steps:
        print("   " + " ".join(s))
    if not args.yes:
        print("\nRe-run with --yes to apply, or run the commands yourself.")
        return 0
    for s in steps:
        r = subprocess.run(s)
        if r.returncode != 0:
            print(f"failed: {' '.join(s)}", file=sys.stderr)
            return r.returncode
    print("\nDone. Log out and back in (or reboot) so the new group membership applies, "
          "then run `linux-computer-use-mcp doctor`.")
    return 0


def cmd_config(_args) -> int:
    exe = shutil.which("linux-computer-use-mcp")
    entry = {"command": exe, "args": []} if exe else {"command": "uvx", "args": ["linux-computer-use-mcp"]}
    session_env = {k: os.environ[k] for k in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE",
                                              "XDG_CURRENT_DESKTOP", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
                   if k in os.environ}
    if session_env:
        entry["env"] = session_env
    print("Claude desktop app → add to ~/.config/Claude/claude_desktop_config.json:\n")
    print(json.dumps({"mcpServers": {"linux-computer-use": entry}}, indent=2))
    cmd = entry["command"] + (" " + " ".join(entry["args"]) if entry["args"] else "")
    print(f"\nClaude Code:\n\n  claude mcp add linux-computer-use -s user -- {cmd}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="linux-computer-use-mcp",
                                description="Let Claude see and control your Linux desktop over MCP.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the MCP server on stdio (default)")
    sub.add_parser("doctor", help="check that screenshots and input work on this desktop")
    sub.add_parser("stop", help="pause all computer control immediately (kill switch)")
    sub.add_parser("resume", help="allow computer control again")
    w = sub.add_parser("setup-wayland", help="one-time permission setup for virtual input on Wayland")
    w.add_argument("--yes", action="store_true", help="apply the changes (uses sudo)")
    sub.add_parser("config", help="print config snippets for Claude Desktop / Claude Code")
    args = p.parse_args(argv)

    if args.cmd in (None, "serve"):
        from .server import run

        run()
        return 0
    return {"doctor": cmd_doctor, "stop": cmd_stop, "resume": cmd_resume,
            "setup-wayland": cmd_setup_wayland, "config": cmd_config}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
