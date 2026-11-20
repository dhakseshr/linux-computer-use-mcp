# Changelog

## 0.3.0
- **Protected windows (X11):** refuses clicks, drags and scrolls whose target is covered by the Claude app,
  and typing while it's focused, so stray input can't land in Claude's own chat box.
  Configure with `LCU_PROTECT_WINDOWS`.
- **New tools:** `focus_window` (bring an app forward by title/class), `list_windows`, and `zoom`
  (full-resolution crop for reading small text).
- **Smarter settling:** waits for the UI to react to an action, then for a quiet period. It counts changed
  pixels, so thin progress bars and spinners count as activity and a blinking caret doesn't.
  `wait` requires up to 1 s of stillness, so slow splash screens aren't mistaken for "done".
- **Fix:** newlines in `type_text` now press a real Return key. Previously GTK dialogs and browser
  address bars ignored them.

## 0.2.0
- Every action returns a screenshot as soon as the screen settles; new `batch` tool.
- X11 mouse via in-process XTEST (a click takes ~3 ms) with a 120 fps glide; JPEG screenshots.

## 0.1.0
- First release: screenshots, mouse and keyboard for X11 and Wayland (GNOME, KDE, wlroots), with a kill
  switch, a failsafe corner, an action log and a dry-run mode.
