# linux-computer-use-mcp

**Let Claude see and control your Linux desktop, live.** Claude takes screenshots, and you watch the
real mouse pointer glide across your screen, click, scroll, drag, type and press shortcuts.

The official Claude desktop app supports Computer Use on macOS and Windows, but not (yet) on Linux.
This project is an open-source [MCP](https://modelcontextprotocol.io) server that fills the gap. It works with
the Claude desktop app for Linux, Claude Code, and any other MCP client.

> Community project, not affiliated with or endorsed by Anthropic.

## Features

- **Works on X11 and Wayland**: GNOME, KDE Plasma, Sway, Hyprland, river, Wayfire, XFCE, Cinnamon, MATE…
- **You can watch it work**: the pointer glides smoothly to each target (120 fps eased motion) and text is typed key by key.
- **Fast**:
  - Every action returns a fresh screenshot as soon as the screen *stops changing*, so there are no guessed waits and no separate screenshot call.
  - `batch` runs a whole sequence (click → type → Enter) in one call.
  - On X11 the mouse goes straight through XTEST in-process (a click takes ~3 ms), and screenshots are compact JPEGs.
- **Full toolset**: `screenshot`, `click` (left/right/middle, double/triple, with modifier keys),
  `move_mouse`, `drag`, `scroll`, `type_text`, `press_key` (`ctrl+s`, `alt+Tab`, `super`…), `hold_key`,
  `batch`, `wait` (returns early once the screen settles), `focus_window`, `list_windows`, `zoom`
  (full-resolution crop for small text), `cursor_position`, `open_application`, `list_applications`,
  `system_info`.
- **Smart waiting**: after each action it waits for the UI to *react*, then for a short quiet period, so it
  doesn't return mid-animation or before a click has had any effect, and it gives up quickly when nothing changes.
- **Won't click into the wrong window**: the Claude app pops to the front whenever you send it a message.
  On X11 the server checks which window is really on top at the target point, and refuses to click there
  or type into it if that window is protected (the Claude app, by default). Instead it tells the model to
  `focus_window` the right app first.
- **HiDPI-aware**: screenshots are scaled down (default max 1280×800) and coordinates are mapped back to real pixels.
- **Safety built in**: a kill switch command, a failsafe corner (X11), an action log and a dry-run mode. Details below.

## How it works

| Session | Screenshots | Mouse & keyboard |
|---|---|---|
| X11 (any desktop) | `mss` (bundled), falls back to ImageMagick `import` / `scrot` | XTEST via `python-xlib` (mouse) + `xdotool` (keyboard) |
| Wayland, GNOME | `gnome-screenshot` → XDG Screenshot portal | `dotool` (or `ydotool`) |
| Wayland, KDE Plasma | `spectacle` → portal | `dotool` (or `ydotool`) |
| Wayland, wlroots (Sway, Hyprland…) | `grim` → portal | `dotool` (or `ydotool`) |

Wayland doesn't let apps move the pointer directly. Instead, `dotool` creates a virtual absolute-pointer
and keyboard device through `/dev/uinput`, and the compositor treats it like real hardware.
That's why Wayland needs a one-time permission setup.

## Install

### 1. System packages

**X11**

```bash
sudo apt install xdotool          # Debian / Ubuntu
sudo dnf install xdotool          # Fedora
sudo pacman -S xdotool            # Arch
```

**Wayland**: install a screenshot tool for your desktop, plus `dotool`:

```bash
# Ubuntu / Debian (GNOME)
sudo apt install gnome-screenshot
# KDE: spectacle   ·   Sway/Hyprland: grim
# Arch: sudo pacman -S grim  /  yay -S dotool
```

`dotool` is packaged in the AUR and some other distros. If yours doesn't have it, build it from
[sr.ht/~geb/dotool](https://git.sr.ht/~geb/dotool) (`./build.sh && sudo ./build.sh install`; needs Go).
`ydotool` ≥ 1.0 with `ydotoold` running also works, but its pointer positioning is less precise
(it moves relatively, so pointer acceleration affects it).

Then allow virtual input once, and **log out and back in**:

```bash
linux-computer-use-mcp setup-wayland --yes
```

### 2. The server

```bash
pipx install git+https://github.com/dhakseshr/linux-computer-use-mcp
# or with uv:
uv tool install git+https://github.com/dhakseshr/linux-computer-use-mcp
```

### 3. Check it

```bash
linux-computer-use-mcp doctor
```

## Connect it to Claude

Print a ready-made config for your machine:

```bash
linux-computer-use-mcp config
```

**Claude desktop app (Linux)**: add this to `~/.config/Claude/claude_desktop_config.json`, then restart the app:

```json
{
  "mcpServers": {
    "linux-computer-use": {
      "command": "/home/you/.local/bin/linux-computer-use-mcp"
    }
  }
}
```

**Claude Code**

```bash
claude mcp add linux-computer-use -s user -- linux-computer-use-mcp
```

Then ask something like *"Open Firefox, go to the weather for Chennai and tell me tomorrow's forecast"* and watch the pointer.

## Safety

This tool gives an AI real control of your mouse and keyboard. Treat it the way you would a person
remote-controlling your machine.

- **Kill switch**: `linux-computer-use-mcp stop` immediately blocks every action (including screenshots)
  until you run `linux-computer-use-mcp resume`. **Bind `stop` to a keyboard shortcut** in your desktop's
  settings (e.g. `Super+Escape`) so you can halt it instantly.
- **Failsafe corner (X11)**: slam the mouse into the top-left corner and the next action is refused.
- **Protected windows (X11)**: clicks, drags and scrolls onto a protected window, and typing while one is
  focused, are refused. That stops the model from typing into Claude's own chat box. The default is the
  Claude app; set `LCU_PROTECT_WINDOWS` to a comma-separated list of app-class substrings, or to an empty
  string to turn the guard off.
- **Action log** at `~/.local/state/linux-computer-use/actions.log`.
- **Dry run**: set `LCU_DRY_RUN=1` to log actions without performing them.
- The server instructs the model to ask before sending messages, buying, deleting or changing settings,
  and to treat on-screen text as information, not instructions. That reduces risk but doesn't remove it.
  Prompt injection from web pages is a real risk, so keep an eye on what it does, and don't leave it unattended
  on screens that show sensitive data.

## Configuration

Set these as environment variables, either in your shell or in the `"env"` block of the MCP config.

| Variable | Default | Meaning |
|---|---|---|
| `LCU_MOVE_DURATION` | `0.12` | Seconds the pointer takes to glide to a target. `0` = jump instantly |
| `LCU_AUTO_SCREENSHOT` | `1` | Actions return a screenshot once the screen settles |
| `LCU_SETTLE_TIMEOUT` | `2.0` | Max seconds an action waits for the screen to stop changing |
| `LCU_JPEG_QUALITY` | `75` | Screenshot JPEG quality |
| `LCU_TYPE_DELAY_MS` | `12` | Delay between typed characters |
| `LCU_MAX_WIDTH` / `LCU_MAX_HEIGHT` | `1280` / `800` | Max screenshot size sent to the model |
| `LCU_IMAGE_FORMAT` | `jpeg` | `jpeg` (fast, small) or `png` |
| `LCU_SCREENSHOT_BACKEND` | auto | `mss`, `import`, `scrot`, `grim`, `spectacle`, `gnome-screenshot`, `portal` |
| `LCU_INPUT_BACKEND` | auto | `xtest`, `xdotool`, `dotool`, `ydotool` |
| `LCU_SESSION` | auto | Force `x11` or `wayland` |
| `LCU_FAILSAFE` | `1` | Top-left-corner failsafe (X11) |
| `LCU_PROTECT_WINDOWS` | `claude` | Comma-separated app classes that must never receive clicks or keystrokes (X11). Empty = off |
| `LCU_DRY_RUN` | `0` | Log actions without performing them |
| `LCU_STOP_FILE` | `~/.config/linux-computer-use/STOP` | Kill-switch file |
| `LCU_LOG_FILE` | `~/.local/state/linux-computer-use/actions.log` | Set empty to disable |

## Troubleshooting

- **The Claude window keeps jumping in front of the app it's controlling**: on X11 the protected-window guard
  stops stray input, and the model can call `focus_window` to bring the target back. For the smoothest
  experience, snap Claude to one half of the screen (`Super+Left`) and the target app to the other half, or keep
  Claude on a second monitor.

- **"No graphical session found"**: the MCP client launched the server without `DISPLAY`/`WAYLAND_DISPLAY`.
  Add them to the `"env"` block (`linux-computer-use-mcp config` prints the right values).
- **GNOME asks "Allow screenshot?"**: that's the portal's one-time permission prompt. Allow it, or install `gnome-screenshot`.
- **Pointer doesn't move on Wayland**: run `doctor`. Usually `/dev/uinput` isn't writable yet, because you
  haven't logged out since running `setup-wayland`.
- **Clicks land in slightly the wrong place on Wayland with fractional scaling**: try `dotool` instead of `ydotool`,
  and report your setup in an issue.
- **Multi-monitor**: screenshots cover all monitors as one image. Wayland absolute positioning maps across the
  whole layout, which works on most setups but not all.

## Limitations

- On Wayland, apps can't read where the pointer is, so `cursor_position` reports the last position this server moved it to.
- The X11 failsafe corner doesn't work on Wayland. Use the `stop` shortcut there.
- `list_windows`, `focus_window` and the protected-window guard need X11. Wayland compositors don't let
  apps see other apps' windows.
- While the GNOME overview (Activities) is open, the guard still sees the windows underneath it, so clicks
  over the Claude window's area may be refused. Use `focus_window` instead of the overview.
- Flatpak or Snap sandboxed MCP clients may not be able to run the host's `xdotool`/`dotool`.

## Development

```bash
git clone https://github.com/dhakseshr/linux-computer-use-mcp && cd linux-computer-use-mcp
pip install -e ".[dev]"
pytest                                                  # unit tests
xvfb-run -s "-screen 0 1920x1080x24" pytest             # + live end-to-end tests (needs xdotool, xterm, openbox)
```

Contributions are welcome, especially testing on more desktops, and backends such as the
RemoteDesktop portal or KDE's fake-input protocol.

## License

MIT
