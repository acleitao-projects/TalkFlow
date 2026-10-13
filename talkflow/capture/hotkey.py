"""Global hotkey listener supporting two trigger modes:
- hold: press-and-hold starts, release stops (Wispr's default).
- toggle: tap twice quickly to start hands-free, single tap to stop.

Hotkey specs are modifier-combo strings like "<ctrl>+<alt>+d" - plain F-keys
are deliberately NOT the default (F-keys often get intercepted by laptop
media-key/Fn-lock behavior before reaching any app, and F5 collides with
browser refresh). A bare single-key spec like "<f9>" still works if you want
it, but Ctrl+Alt+<letter> combos are the recommended/default pattern.

Built on pynput (user-mode SetWindowsHookEx, no admin needed - verified via
scripts/spike_hotkey.py).
"""
from __future__ import annotations

import ctypes
import threading
import time
from typing import Callable

from pynput import keyboard

DOUBLE_TAP_WINDOW_S = 0.5

_MODIFIER_ALIASES = {
    "<ctrl>": {keyboard.Key.ctrl_l, keyboard.Key.ctrl_r, keyboard.Key.ctrl},
    # alt_gr: on ABNT2/international layouts the right Alt key is AltGr, and
    # Windows reports Ctrl+Alt as ctrl_l + alt_gr.
    "<alt>": {keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt, keyboard.Key.alt_gr},
    "<shift>": {keyboard.Key.shift_l, keyboard.Key.shift_r, keyboard.Key.shift},
    "<win>": {keyboard.Key.cmd_l, keyboard.Key.cmd_r, keyboard.Key.cmd},
}

_SPECIAL_KEY_ALIASES = {
    "<f1>": keyboard.Key.f1, "<f2>": keyboard.Key.f2, "<f3>": keyboard.Key.f3,
    "<f4>": keyboard.Key.f4, "<f5>": keyboard.Key.f5, "<f6>": keyboard.Key.f6,
    "<f7>": keyboard.Key.f7, "<f8>": keyboard.Key.f8, "<f9>": keyboard.Key.f9,
    "<f10>": keyboard.Key.f10, "<f11>": keyboard.Key.f11, "<f12>": keyboard.Key.f12,
    "<caps_lock>": keyboard.Key.caps_lock, "<space>": keyboard.Key.space,
    "<tab>": keyboard.Key.tab,
}


def _parse_spec(spec: str) -> tuple[frozenset, object]:
    """Parses "<ctrl>+<alt>+d" into ({modifier-key-sets}, main_key). A bare
    "<f9>" or single-char spec with no modifiers returns an empty modifier set."""
    parts = [p.strip() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"Empty hotkey spec: {spec!r}")

    modifiers = []
    main_part = None
    for part in parts:
        if part in _MODIFIER_ALIASES:
            modifiers.append(_MODIFIER_ALIASES[part])
        else:
            main_part = part

    if main_part is None:
        raise ValueError(f"Hotkey spec has no non-modifier key: {spec!r}")

    if main_part in _SPECIAL_KEY_ALIASES:
        main_key = _SPECIAL_KEY_ALIASES[main_part]
    elif main_part.startswith("<") and main_part.endswith(">"):
        raise ValueError(f"Unsupported key alias: {main_part!r}")
    elif len(main_part) == 1:
        main_key = keyboard.KeyCode.from_char(main_part.lower())
    else:
        raise ValueError(f"Unsupported hotkey main key: {main_part!r}")

    return frozenset(frozenset(m) for m in modifiers), main_key


def _keys_match(pressed: object, target: object) -> bool:
    if pressed == target:
        return True
    # KeyCode equality can fail char-vs-vk mismatches depending on keyboard
    # layout; fall back to comparing .char when both are KeyCode instances.
    if isinstance(pressed, keyboard.KeyCode) and isinstance(target, keyboard.KeyCode):
        # With Ctrl/Alt held, .char can be a control char or None; the virtual
        # key code of a letter/digit equals its uppercase ASCII code.
        if pressed.vk is not None and target.char is not None and len(target.char) == 1                 and pressed.vk == ord(target.char.upper()):
            return True
        return pressed.char is not None and target.char is not None and pressed.char.lower() == target.char.lower()
    return False


_VK_FOR_MODIFIER = {
    "<ctrl>": 0x11, "<shift>": 0x10, "<alt>": 0x12,  # generic VKs: either side, and AltGr (= Ctrl+Alt)
}


def _vk_down(vk: int) -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


class HotkeyController:
    """Trigger modes:
    - hold:   press starts, release of the main key stops (and processes).
    - toggle: tap once to start, tap once more to stop (and process).
    In both modes Esc cancels an active recording via `on_cancel`.

    Modifier state is read live from GetAsyncKeyState rather than tracked from
    events: AltGr (Ctrl+Alt on international layouts) injects fake Ctrl events
    and event tracking drifted, causing phantom/missed triggers."""

    def __init__(self, hotkey_spec: str, mode: str, on_start: Callable[[], None],
                 on_stop: Callable[[], None], on_cancel: Callable[[], None] | None = None):
        self.required_modifier_groups, self.main_key = _parse_spec(hotkey_spec)
        self._required_vks = [
            _VK_FOR_MODIFIER[p.strip()] for p in hotkey_spec.split("+") if p.strip() in _VK_FOR_MODIFIER
        ]
        self._needs_win = "<win>" in hotkey_spec
        self.mode = mode  # "hold" | "toggle"
        self.on_start = on_start
        self.on_stop = on_stop
        self.on_cancel = on_cancel
        self._active = False
        self._main_down = False
        self._last_tap_time = 0.0
        self._listener: keyboard.Listener | None = None
        # Caps Lock as a hold-to-talk key: the press that starts a recording
        # also flips the Caps Lock state, so it is flipped back on release.
        self._caps_toggled_by_hold = False
        self._ignore_until = 0.0  # swallow our own synthesized Caps Lock tap

    @property
    def active(self) -> bool:
        return self._active

    def reset(self) -> None:
        """Forget in-progress state (call when the recording was ended some
        other way, e.g. an overlay button)."""
        self._active = False
        self._last_tap_time = 0.0

    def _modifiers_satisfied(self) -> bool:
        if not all(_vk_down(vk) for vk in self._required_vks):
            return False
        if self._needs_win and not (_vk_down(0x5B) or _vk_down(0x5C)):
            return False
        return True

    def start(self) -> None:
        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.start()

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()
            self._listener = None

    def _undo_caps_toggle(self) -> None:
        def _tap() -> None:
            self._ignore_until = time.monotonic() + 0.3
            kb = keyboard.Controller()
            kb.press(keyboard.Key.caps_lock)
            kb.release(keyboard.Key.caps_lock)
        # Off the hook thread: input sent from inside the callback is delayed.
        threading.Thread(target=_tap, daemon=True).start()

    def _on_press(self, key) -> None:
        if time.monotonic() < self._ignore_until and key == keyboard.Key.caps_lock:
            return
        if key == keyboard.Key.esc:
            if self._active and self.on_cancel:
                self.reset()
                self.on_cancel()
            return

        if not _keys_match(key, self.main_key):
            return
        if self._main_down:  # OS auto-repeat while the key is held
            return
        if not self._modifiers_satisfied():
            return
        self._main_down = True

        if self.mode == "hold":
            if self.main_key == keyboard.Key.caps_lock:
                self._caps_toggled_by_hold = True
            if not self._active:
                self._active = True
                self.on_start()
        elif self.mode == "toggle":
            # Single tap starts, next single tap stops and processes.
            if not self._active:
                self._active = True
                self.on_start()
            else:
                self._active = False
                self.on_stop()

    def _on_release(self, key) -> None:
        if not _keys_match(key, self.main_key):
            return
        if time.monotonic() < self._ignore_until and key == keyboard.Key.caps_lock:
            return
        self._main_down = False
        if self.mode == "hold" and self._active:
            # AltGr/fake-modifier events can emit spurious releases; confirm
            # the key is physically up before ending the recording.
            time.sleep(0.03)
            if self._main_vk_down():
                self._main_down = True
                return
            self._active = False
            self.on_stop()
        if self._caps_toggled_by_hold:
            # Also reached after an Esc-cancel, when the key comes up later.
            self._caps_toggled_by_hold = False
            self._undo_caps_toggle()

    def _main_vk_down(self) -> bool:
        mk = self.main_key
        vk = getattr(mk, "vk", None)
        if vk is None and isinstance(mk, keyboard.KeyCode) and mk.char and mk.char.isalnum():
            vk = ord(mk.char.upper())
        if vk is None and hasattr(mk, "value"):
            vk = getattr(mk.value, "vk", None)
        return _vk_down(vk) if vk else False
