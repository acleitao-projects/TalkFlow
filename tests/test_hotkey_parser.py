import pytest
from pynput import keyboard

from talkflow.capture.hotkey import HotkeyController, _parse_spec


def test_parse_simple_combo():
    modifiers, main_key = _parse_spec("<ctrl>+<alt>+d")
    assert main_key == keyboard.KeyCode.from_char("d")
    assert len(modifiers) == 2


def test_parse_bare_fkey_no_modifiers():
    modifiers, main_key = _parse_spec("<f9>")
    assert main_key == keyboard.Key.f9
    assert len(modifiers) == 0


def test_parse_rejects_modifier_only_spec():
    with pytest.raises(ValueError):
        _parse_spec("<ctrl>+<alt>")


def test_controller_fires_only_when_modifiers_held(monkeypatch):
    from talkflow.capture import hotkey as hk
    held = set()
    monkeypatch.setattr(hk, "_vk_down", lambda vk: vk in held)
    events = []
    ctrl = HotkeyController(
        hotkey_spec="<ctrl>+<alt>+d", mode="hold",
        on_start=lambda: events.append("start"), on_stop=lambda: events.append("stop"),
    )

    # 'd' alone, no modifiers held - must NOT fire.
    ctrl._on_press(keyboard.KeyCode.from_char("d"))
    assert events == []

    # Hold ctrl+alt, then press 'd' - must fire start.
    held.update({0x11, 0x12})  # ctrl, alt physically down
    ctrl._on_press(keyboard.KeyCode.from_char("d"))
    assert events == ["start"]

    # Release 'd' while modifiers still held - must fire stop.
    ctrl._on_release(keyboard.KeyCode.from_char("d"))
    assert events == ["start", "stop"]


def test_controller_ignores_wrong_key():
    events = []
    ctrl = HotkeyController(
        hotkey_spec="<ctrl>+<alt>+d", mode="hold",
        on_start=lambda: events.append("start"), on_stop=lambda: events.append("stop"),
    )
    ctrl._on_press(keyboard.Key.ctrl_l)
    ctrl._on_press(keyboard.Key.alt_l)
    ctrl._on_press(keyboard.KeyCode.from_char("x"))  # wrong key
    assert events == []
