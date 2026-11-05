"""TwoSourceRecorder._resolve_pid - matching a configured process/executable
name against flexaudio.processes() candidates, case-insensitively and with
or without the ".exe" suffix on either side. Doesn't construct
TwoSourceRecorder itself (its __init__ touches real audio devices) - this
static method needs no device at all."""
import sys
import types

import pytest

from talkflow.features.liveassist.capture import TwoSourceRecorder


class _FakeProcess:
    def __init__(self, pid, name, executable):
        self.pid, self.name, self.executable = pid, name, executable


def _install_fake_flexaudio(monkeypatch, processes):
    fake = types.ModuleType("flexaudio")
    fake.processes = lambda: processes
    monkeypatch.setitem(sys.modules, "flexaudio", fake)


def test_resolve_pid_matches_executable_case_insensitively(monkeypatch):
    _install_fake_flexaudio(monkeypatch, [_FakeProcess(123, "Zoom", "Zoom.exe")])
    assert TwoSourceRecorder._resolve_pid("zoom.exe") == 123
    assert TwoSourceRecorder._resolve_pid("ZOOM.EXE") == 123


def test_resolve_pid_matches_without_exe_suffix_on_either_side(monkeypatch):
    _install_fake_flexaudio(monkeypatch, [_FakeProcess(456, "Teams", "ms-teams.exe")])
    assert TwoSourceRecorder._resolve_pid("ms-teams") == 456


def test_resolve_pid_falls_back_to_display_name(monkeypatch):
    _install_fake_flexaudio(monkeypatch, [_FakeProcess(789, "Google Chrome", None)])
    assert TwoSourceRecorder._resolve_pid("Google Chrome") == 789


def test_resolve_pid_returns_none_when_not_found(monkeypatch):
    _install_fake_flexaudio(monkeypatch, [_FakeProcess(123, "Zoom", "Zoom.exe")])
    assert TwoSourceRecorder._resolve_pid("discord.exe") is None


class _FakeMic:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"_FakeMic({self.name!r})"


class _FakeSoundcard:
    def __init__(self, mics):
        self._mics = mics

    def all_microphones(self, include_loopback=False):
        return self._mics

    def default_microphone(self):
        return self._mics[0]


def test_find_microphone_exact_match():
    sc = _FakeSoundcard([_FakeMic("Microfone (RODECaster Pro II Main Stereo)"), _FakeMic("Realtek")])
    found = TwoSourceRecorder._find_microphone(sc, "Microfone (RODECaster Pro II Main Stereo)")
    assert found.name == "Microfone (RODECaster Pro II Main Stereo)"


def test_find_microphone_substring_match_is_case_insensitive():
    sc = _FakeSoundcard([_FakeMic("Microfone (RODECaster Pro II Main Stereo)"), _FakeMic("Realtek")])
    found = TwoSourceRecorder._find_microphone(sc, "rodecaster pro ii main")
    assert found.name == "Microfone (RODECaster Pro II Main Stereo)"


def test_find_microphone_falls_back_to_default_when_not_found():
    sc = _FakeSoundcard([_FakeMic("Realtek")])
    found = TwoSourceRecorder._find_microphone(sc, "nonexistent device")
    assert found.name == "Realtek"  # default_microphone() fallback


class _FakeSpeaker:
    def __init__(self, name):
        self.name = name


class _FakeLoopbackMic:
    def __init__(self, name, isloopback):
        self.name = name
        self.isloopback = isloopback


class _FakeSoundcardForOutput:
    def __init__(self, speakers, loopback_by_name):
        self._speakers = speakers
        self._loopback_by_name = loopback_by_name

    def all_speakers(self):
        return self._speakers

    def get_microphone(self, name, include_loopback=True):
        return self._loopback_by_name[name]


def test_resolve_output_loopback_exact_match_succeeds():
    sc = _FakeSoundcardForOutput(
        speakers=[_FakeSpeaker("Alto-falantes (RODECaster Pro II Chat)")],
        loopback_by_name={"Alto-falantes (RODECaster Pro II Chat)": _FakeLoopbackMic("loop", isloopback=True)},
    )
    found = TwoSourceRecorder._resolve_output_loopback(sc, "Alto-falantes (RODECaster Pro II Chat)")
    assert found.isloopback is True


def test_resolve_output_loopback_fuzzy_match_to_non_loopback_device_fails_loud():
    """A name that doesn't exactly match any speaker must raise, not fall
    back to a different device - this is the fail-loud fix: an explicit,
    invalid override must never silently substitute another device."""
    sc = _FakeSoundcardForOutput(speakers=[_FakeSpeaker("Alto-falantes (Main)")], loopback_by_name={})
    with pytest.raises(ValueError):
        TwoSourceRecorder._resolve_output_loopback(sc, "Alto-falantes (Mai")  # partial/fuzzy, not exact


def test_resolve_output_loopback_non_loopback_result_fails_loud():
    sc = _FakeSoundcardForOutput(
        speakers=[_FakeSpeaker("Alto-falantes (Main)")],
        loopback_by_name={"Alto-falantes (Main)": _FakeLoopbackMic("not actually loopback", isloopback=False)},
    )
    with pytest.raises(ValueError):
        TwoSourceRecorder._resolve_output_loopback(sc, "Alto-falantes (Main)")


def test_resolve_output_loopback_unresolvable_name_fails_loud():
    sc = _FakeSoundcardForOutput(speakers=[], loopback_by_name={})
    with pytest.raises(ValueError):
        TwoSourceRecorder._resolve_output_loopback(sc, "nonexistent output")
