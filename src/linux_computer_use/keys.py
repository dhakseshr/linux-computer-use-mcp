"""Key-name normalization.

Clients send combos like "ctrl+s", "cmd+shift+t", "Enter", "esc", "Page_Down".
We normalize each key to a canonical X keysym-style name, then translate per backend:
  * xdotool  -> X keysym names (canonical form)
  * dotool   -> Linux input key names (lowercase, without KEY_)
  * ydotool  -> Linux input keycodes ("29:1 31:1 31:0 29:0")
"""

from __future__ import annotations

# alias (lowercase) -> canonical keysym
ALIASES: dict[str, str] = {
    "ctrl": "ctrl", "control": "ctrl", "ctl": "ctrl", "lctrl": "ctrl", "control_l": "ctrl",
    "shift": "shift", "lshift": "shift", "shift_l": "shift",
    "alt": "alt", "option": "alt", "opt": "alt", "lalt": "alt", "alt_l": "alt",
    "altgr": "altgr", "ralt": "altgr", "alt_r": "altgr", "iso_level3_shift": "altgr",
    "super": "super", "cmd": "super", "command": "super", "win": "super", "windows": "super",
    "meta": "super", "logo": "super", "super_l": "super",
    "enter": "Return", "return": "Return", "ret": "Return",
    "esc": "Escape", "escape": "Escape",
    "tab": "Tab",
    "backspace": "BackSpace", "bksp": "BackSpace",
    "delete": "Delete", "del": "Delete",
    "insert": "Insert", "ins": "Insert",
    "home": "Home", "end": "End",
    "pageup": "Page_Up", "page_up": "Page_Up", "pgup": "Page_Up", "prior": "Page_Up",
    "pagedown": "Page_Down", "page_down": "Page_Down", "pgdn": "Page_Down", "next": "Page_Down",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "arrowup": "Up", "arrowdown": "Down", "arrowleft": "Left", "arrowright": "Right",
    "space": "space", "spacebar": "space", " ": "space",
    "print": "Print", "printscreen": "Print", "prtsc": "Print",
    "menu": "Menu", "contextmenu": "Menu",
    "capslock": "Caps_Lock", "caps_lock": "Caps_Lock",
    "numlock": "Num_Lock", "num_lock": "Num_Lock",
    "scrolllock": "Scroll_Lock", "scroll_lock": "Scroll_Lock",
    "pause": "Pause",
    "minus": "minus", "-": "minus",
    "equal": "equal", "=": "equal", "plus": "plus",
    "bracketleft": "bracketleft", "[": "bracketleft",
    "bracketright": "bracketright", "]": "bracketright",
    "semicolon": "semicolon", ";": "semicolon",
    "apostrophe": "apostrophe", "'": "apostrophe",
    "grave": "grave", "`": "grave",
    "backslash": "backslash", "\\": "backslash",
    "comma": "comma", ",": "comma",
    "period": "period", ".": "period",
    "slash": "slash", "/": "slash",
    "volumeup": "XF86AudioRaiseVolume", "volumedown": "XF86AudioLowerVolume",
    "volumemute": "XF86AudioMute", "mute": "XF86AudioMute",
}

MODIFIERS = {"ctrl", "shift", "alt", "altgr", "super"}

# canonical -> (linux name, linux keycode)
LINUX: dict[str, tuple[str, int]] = {
    "ctrl": ("leftctrl", 29), "shift": ("leftshift", 42), "alt": ("leftalt", 56),
    "altgr": ("rightalt", 100), "super": ("leftmeta", 125),
    "Return": ("enter", 28), "Escape": ("esc", 1), "Tab": ("tab", 15), "BackSpace": ("backspace", 14),
    "Delete": ("delete", 111), "Insert": ("insert", 110), "Home": ("home", 102), "End": ("end", 107),
    "Page_Up": ("pageup", 104), "Page_Down": ("pagedown", 109),
    "Up": ("up", 103), "Down": ("down", 108), "Left": ("left", 105), "Right": ("right", 106),
    "space": ("space", 57), "Print": ("sysrq", 99), "Menu": ("compose", 127),
    "Caps_Lock": ("capslock", 58), "Num_Lock": ("numlock", 69), "Scroll_Lock": ("scrolllock", 70),
    "Pause": ("pause", 119),
    "minus": ("minus", 12), "equal": ("equal", 13), "bracketleft": ("leftbrace", 26),
    "bracketright": ("rightbrace", 27), "semicolon": ("semicolon", 39), "apostrophe": ("apostrophe", 40),
    "grave": ("grave", 41), "backslash": ("backslash", 43), "comma": ("comma", 51), "period": ("dot", 52),
    "slash": ("slash", 53),
    "XF86AudioRaiseVolume": ("volumeup", 115), "XF86AudioLowerVolume": ("volumedown", 114),
    "XF86AudioMute": ("mute", 113),
}
for _i, _c in enumerate([59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 87, 88], start=1):
    LINUX[f"F{_i}"] = (f"f{_i}", _c)
for _ch, _c in zip("1234567890", [2, 3, 4, 5, 6, 7, 8, 9, 10, 11]):
    LINUX[_ch] = (_ch, _c)
_LETTER_CODES = dict(zip("qwertyuiop", range(16, 26)))
_LETTER_CODES.update(zip("asdfghjkl", range(30, 39)))
_LETTER_CODES.update(zip("zxcvbnm", range(44, 51)))
for _ch, _c in _LETTER_CODES.items():
    LINUX[_ch] = (_ch, _c)

# canonical -> xdotool keysym (modifiers need explicit names for xdotool)
XDOTOOL_MOD = {"ctrl": "ctrl", "shift": "shift", "alt": "alt", "altgr": "ISO_Level3_Shift", "super": "super"}


class KeyComboError(ValueError):
    pass


def normalize_key(name: str) -> str:
    if name == "+":
        return "plus"
    raw = name.strip() if name.strip() else name
    low = raw.lower()
    if low in ALIASES:
        return ALIASES[low]
    if len(raw) == 1:
        return raw.lower() if raw.isalpha() else raw
    if low.startswith("f") and low[1:].isdigit() and 1 <= int(low[1:]) <= 24:
        return f"F{int(low[1:])}"
    # pass through anything else (e.g. XF86 keysyms, exact keysym names)
    return raw


def parse_combo(combo: str) -> list[str]:
    """'ctrl+shift+t' -> ['ctrl', 'shift', 't'];  'ctrl++' -> ['ctrl', 'plus']."""
    combo = combo.strip()
    if not combo:
        raise KeyComboError("empty key combo")
    parts: list[str] = []
    buf = ""
    i = 0
    while i < len(combo):
        ch = combo[i]
        if ch == "+" and buf:
            parts.append(buf)
            buf = ""
        elif ch == "+" and not buf:
            parts.append("+")
            if i + 1 < len(combo) and combo[i + 1] == "+":
                i += 1  # skip separator after a literal plus
        else:
            buf += ch
        i += 1
    if buf:
        parts.append(buf)
    keys = [normalize_key(p) for p in parts]
    return keys


def to_xdotool(keys: list[str]) -> str:
    return "+".join(XDOTOOL_MOD.get(k, k) for k in keys)


def to_dotool(keys: list[str]) -> str:
    out = []
    for k in keys:
        if k in ("ctrl", "shift", "alt", "super"):
            out.append(k)
        elif k in LINUX:
            out.append(LINUX[k][0])
        else:
            out.append("x:" + k)  # dotool accepts XKB keysym names with an x: prefix
    return "+".join(out)


def to_ydotool(keys: list[str]) -> list[str]:
    codes = []
    for k in keys:
        if k not in LINUX:
            raise KeyComboError(f"key {k!r} is not supported by the ydotool backend; use dotool for exotic keys")
        codes.append(LINUX[k][1])
    return [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
