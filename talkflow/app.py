"""TalkFlow entrypoint. Run with: python -m talkflow.app

Wires: sqlite init -> model registry (seeding local-Docker ASR + Ollama Cloud
cleanup defaults) -> ASR/LLM adapters resolved from the registry -> insertion
service -> orchestrator -> global hotkey -> floating overlay -> tray icon ->
main settings window. pywebview's event loop owns the main thread; the
hotkey listener and tray icon each run on their own background thread.
"""
from __future__ import annotations

import logging
import logging.handlers
import threading
from pathlib import Path

import webview

ICON_PATH = Path(__file__).resolve().parent / "assets" / "icon.ico"

# Without an explicit AppUserModelID, Windows groups every window of this
# process under python.exe/pythonw.exe and the taskbar button shows that
# executable's generic icon - no matter what icon the window itself sets
# (webview.start(icon=...) and WM_SETICON alone don't change it). Must run
# before any window is created.
try:
    import ctypes
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TalkFlow.App")
except Exception:
    pass

from talkflow.asr.base import AsrAdapter
from talkflow.asr.nemotron_sherpa_adapter import NemotronSherpaAdapter
from talkflow.asr.whisper_http_adapter import WhisperHttpAdapter, ensure_model_downloaded
from talkflow.capture.hotkey import HotkeyController
from talkflow.config.paths import log_dir
from talkflow.config.settings import Settings
from talkflow.features.command_mode.engine import CommandModeEngine
from talkflow.features.liveassist import assistants as liveassist_assistants
from talkflow.features.liveassist.batch_asr import TwoSourceBatchAsrAdapter
from talkflow.features.liveassist.deepgram_asr import TwoSourceDeepgramAsrAdapter
from talkflow.features.liveassist.multi_stream_asr import MultiStreamAsrAdapter
from talkflow.features.liveassist.realtime_asr import TwoSourceRealtimeAsrAdapter
from talkflow.features.liveassist.whisperlivekit_asr import TwoSourceWhisperLiveKitAdapter
from talkflow.features.liveassist.orchestrator import LiveAssistOrchestrator
from talkflow.features.liveassist.session import LiveAssistState
from talkflow.features.liveassist.suggestion_engine import SuggestionEngine
from talkflow.features.notetaker.capture import MeetingRecorder
from talkflow.features.notetaker.pipeline import process_meeting
from talkflow.features.transforms.engine import TransformsEngine
from talkflow.features.transforms.store import list_custom_transforms
from talkflow.floatingnotes.window import FloatingNotesWindow
from talkflow.history.db import init_db
from talkflow.insertion.insertion_service import InsertionService
from talkflow.llm.openai_compatible_adapter import OpenAiCompatibleAdapter
from talkflow.mainwindow.main_window import MainWindow
from talkflow.overlay.live_assist_overlay_window import LiveAssistOverlayWindow
from talkflow.overlay.overlay_window import OverlayWindow
from talkflow.pipeline.orchestrator import Orchestrator
from talkflow.registry import secrets as secret_store
from talkflow.registry import store as registry_store
from talkflow.tray.tray_icon import TrayIcon
from talkflow import single_instance

# Launched via pythonw.exe (startup shortcut, no console - see
# scripts/launch_talkflow.vbs), the console-only logging below goes nowhere:
# a log file is the only way to diagnose anything after the fact. Rotates at
# 5MB x3 so it can't grow unbounded on a background app nobody watches.
_log_file = logging.handlers.RotatingFileHandler(
    log_dir() / "talkflow.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), _log_file],
)
logger = logging.getLogger("talkflow")


def format_hotkey_spec(spec: str) -> str:
    """"<ctrl>+<shift>+d" -> "Ctrl+Shift+D", matching the JS formatter in
    mainwindow/web/index.html's formatHotkeySpec()."""
    parts = []
    for part in spec.split("+"):
        part = part.strip("<>")
        parts.append(part.upper() if len(part) == 1 else part.replace("_", " ").title())
    return "+".join(parts)


def build_asr_adapter() -> AsrAdapter:
    asr_assignment = registry_store.get_active("asr")
    if asr_assignment is None:
        raise RuntimeError("Model registry missing active asr assignment")
    if asr_assignment.provider == "nemotron-local":
        # In-process (sherpa-onnx, no container/network) - base_url holds the
        # extracted model directory, not an address. A missing/corrupt model
        # directory surfaces as a normal exception from the constructor.
        return NemotronSherpaAdapter(model_dir=asr_assignment.base_url)
    if asr_assignment.provider == "local-docker":
        try:
            ensure_model_downloaded(asr_assignment.base_url, asr_assignment.model_id)
        except Exception:
            logger.exception("Could not confirm local ASR model is downloaded - "
                              "is the Docker container running? (docker compose -f docker-compose.asr.yml up -d)")
    asr_api_key = secret_store.get_api_key(asr_assignment.api_key_ref) if asr_assignment.api_key_ref else None
    return WhisperHttpAdapter(
        base_url=asr_assignment.base_url, model_id=asr_assignment.model_id, api_key=asr_api_key,
    )


def build_llm_adapter() -> OpenAiCompatibleAdapter:
    llm_assignment = registry_store.get_active("cleanup")
    if llm_assignment is None:
        raise RuntimeError("Model registry missing active cleanup assignment")
    llm_api_key = secret_store.get_api_key(llm_assignment.api_key_ref) if llm_assignment.api_key_ref else None
    if not llm_api_key:
        logger.warning(
            "No API key stored for the cleanup stage (key_ref=%r). "
            "Run scripts/set_llm_api_key.py to store it before dictating.",
            llm_assignment.api_key_ref,
        )
    return OpenAiCompatibleAdapter(
        base_url=llm_assignment.base_url, model_id=llm_assignment.model_id, api_key=llm_api_key,
    )


def build_live_suggestion_llm() -> OpenAiCompatibleAdapter:
    # "live_suggestion" is its own registry stage (LA-09), independently
    # swappable from plain dictation's "cleanup" stage - falls back to
    # "cleanup"'s assignment when none is configured, so this works out of
    # the box without requiring a separate registry entry first.
    assignment = registry_store.get_active("live_suggestion") or registry_store.get_active("cleanup")
    if assignment is None:
        raise RuntimeError("Model registry missing active live_suggestion/cleanup assignment")
    api_key = secret_store.get_api_key(assignment.api_key_ref) if assignment.api_key_ref else None
    return OpenAiCompatibleAdapter(base_url=assignment.base_url, model_id=assignment.model_id, api_key=api_key)


def build_orchestrator(overlay: OverlayWindow, llm: OpenAiCompatibleAdapter, insertion: InsertionService) -> Orchestrator:
    settings = Settings.load()
    asr = build_asr_adapter()

    def on_state_change(session) -> None:
        # The pill stays visible at all times (see overlay_window.py) - it
        # never hides itself, it just reverts to "idle" a moment after a
        # dictation finishes so the state readout doesn't get stuck on
        # "Done"/"Error" forever.
        state = session.state.value if hasattr(session.state, "value") else str(session.state)
        if state == "cancelled" and getattr(session, "error_code", None) == "no_speech":
            state = "nospeech"
        overlay.set_state(state)
        if state in ("complete", "error", "cancelled", "nospeech"):
            def _revert_to_idle_later():
                import time
                time.sleep(1.2)
                overlay.set_state("idle")
            threading.Thread(target=_revert_to_idle_later, daemon=True).start()

    return Orchestrator(
        asr=asr, llm=llm, insertion=insertion,
        allowed_languages=settings.language_allowlist,
        input_device_id=int(settings.input_device_id) if settings.input_device_id else None,
        on_state_change=on_state_change,
        partial_paste=settings.partial_paste_allowed(),
        incremental_asr=True,
        cleanup_level=settings.cleanup_level,
    )


def main() -> None:
    if not single_instance.acquire():
        logger.info("TalkFlow is already running; asked it to show its window, exiting")
        return

    init_db()
    registry_store.ensure_defaults()
    settings = Settings.load()

    overlay = OverlayWindow()
    overlay.create()
    overlay.set_hotkey_hint(format_hotkey_spec(settings.hotkey))

    main_window = MainWindow()
    main_window.create()

    floating_notes = FloatingNotesWindow(main_window.api)
    floating_notes.create()
    main_window.api.overlay = overlay
    main_window.api.floating_notes = floating_notes
    overlay.on_notes_click = floating_notes.toggle
    overlay.set_flow_bar_enabled(settings.scratchpad_in_pill)

    def _active_assistant_name() -> str:
        active_id = Settings.load().live_assist_active_assistant_id
        a = liveassist_assistants.get_assistant(active_id) if active_id else None
        if a:
            return a.name
        all_assistants = liveassist_assistants.list_assistants()
        return all_assistants[0].name if all_assistants else "Assistant"

    def cycle_assistant() -> None:
        all_assistants = liveassist_assistants.list_assistants()
        if not all_assistants:
            return
        current_id = Settings.load().live_assist_active_assistant_id
        ids = [a.id for a in all_assistants]
        try:
            next_index = (ids.index(current_id) + 1) % len(ids)
        except ValueError:
            next_index = 0
        s = Settings.load()
        s.live_assist_active_assistant_id = ids[next_index]
        s.save()
        overlay.set_assistant_name(all_assistants[next_index].name)

    overlay.on_assistant_cycle_click = cycle_assistant
    overlay.on_open_assistants_click = lambda: main_window.show_tab("assistants")
    overlay.set_assistant_name(_active_assistant_name())

    shared_llm = build_llm_adapter()
    shared_insertion = InsertionService(prefer_ui_automation=False)

    orchestrator_holder: dict = {}

    def get_orchestrator() -> Orchestrator:
        if "o" not in orchestrator_holder:
            orchestrator_holder["o"] = build_orchestrator(overlay, shared_llm, shared_insertion)
        return orchestrator_holder["o"]

    # Build the orchestrator (ASR adapter, model check) now so the first
    # hotkey press doesn't stall for seconds before the pill reacts.
    def _prewarm() -> None:
        try:
            orch = get_orchestrator()
            # Load the ASR model into GPU memory now (first request after a
            # cold container can take ~10s) with half a second of silence.
            orch.asr.start_session(["en"])
            if hasattr(orch.asr, "set_full_audio"):
                orch.asr.set_full_audio(bytes(16000), 16000)
            else:
                orch.asr.feed_chunk(bytes(16000), 16000)
            orch.asr.finalize()
        except Exception:
            logger.exception("Orchestrator pre-warm failed")
    threading.Thread(target=_prewarm, daemon=True).start()

    def _prewarm_llm() -> None:
        # A local model's FIRST completion call pays for mapping the weights
        # into VRAM on top of normal inference - measured ~99s for Gemma 3 1B
        # on this GPU (vs. ~2.5s once warm). warm()'s HEAD request alone
        # doesn't trigger that load; only a real completion does.
        try:
            shared_llm.cleanup(
                raw_transcript="warm up", language="en", formality_name="Normal",
                formality_instruction="Clear, neutral, professional.", target_process_name=None,
            )
        except Exception:
            logger.exception("LLM pre-warm failed")
    threading.Thread(target=_prewarm_llm, daemon=True).start()

    busy = {"processing": False}

    def on_hotkey_start() -> None:
        if busy["processing"]:
            # Still transcribing/inserting the previous dictation.
            hotkey.reset()
            return
        overlay.clear_panic_hide()
        try:
            get_orchestrator().start(trigger_mode=settings.trigger_mode)
        except Exception:
            logger.exception("Failed to start dictation session")
            hotkey.reset()

    def on_hotkey_stop() -> None:
        def _run():
            busy["processing"] = True
            try:
                get_orchestrator().stop()
            except Exception:
                logger.exception("Failed to stop/process dictation session")
            finally:
                busy["processing"] = False
        threading.Thread(target=_run, daemon=True).start()

    def on_dictation_cancel() -> None:
        try:
            get_orchestrator().cancel()
        except Exception:
            logger.exception("Dictation cancel failed")

    # Overlay buttons (usable in tap-to-toggle mode): X discards, check
    # stops and processes. Keep the hotkey's own state in sync.
    def on_overlay_cancel() -> None:
        hotkey.reset()
        on_dictation_cancel()

    def on_overlay_confirm() -> None:
        hotkey.reset()
        on_hotkey_stop()

    overlay.on_cancel = on_overlay_cancel
    overlay.on_confirm = on_overlay_confirm

    # Mic button on the hover pill: start a hands-free dictation (stopped by
    # the pill's check/X buttons, or the dictate hotkey).
    def on_overlay_mic() -> None:
        if hotkey.active or busy["processing"]:
            return
        hotkey.mark_active()
        try:
            get_orchestrator().start(trigger_mode="toggle")
        except Exception:
            logger.exception("Failed to start dictation from the overlay")
            hotkey.reset()

    overlay.on_mic_click = on_overlay_mic

    hotkey = HotkeyController(
        hotkey_spec=settings.hotkey, mode=settings.trigger_mode,
        on_start=on_hotkey_start, on_stop=on_hotkey_stop, on_cancel=on_dictation_cancel,
    )
    hotkey.start()

    # Transforms: tap once to run "Polish" on the current selection.
    transforms_engine = TransformsEngine(shared_llm, shared_insertion)

    def on_transform_hotkey() -> None:
        def _run():
            result = transforms_engine.run("polish")
            logger.info("Transform result: success=%s error=%s", result.success, result.error)
        threading.Thread(target=_run, daemon=True).start()

    transform_hotkey = HotkeyController(
        hotkey_spec=settings.transform_polish_hotkey, mode="hold",
        on_start=on_transform_hotkey, on_stop=lambda: None,
    )
    transform_hotkey.start()

    def on_prompt_engineer_hotkey() -> None:
        def _run():
            result = transforms_engine.run("prompt_engineer")
            logger.info("Prompt Engineer transform result: success=%s error=%s", result.success, result.error)
        threading.Thread(target=_run, daemon=True).start()

    prompt_engineer_hotkey = HotkeyController(
        hotkey_spec=settings.transform_prompt_engineer_hotkey, mode="hold",
        on_start=on_prompt_engineer_hotkey, on_stop=lambda: None,
    )
    prompt_engineer_hotkey.start()

    # Custom transforms: each gets its own hotkey controller if a hotkey is set.
    custom_transform_hotkeys: list[HotkeyController] = []
    for ct in list_custom_transforms():
        if not ct.hotkey:
            continue
        def _make_handler(transform_id: str, transform_name: str):
            def _handler() -> None:
                def _run():
                    result = transforms_engine.run(transform_id)
                    logger.info("Custom transform '%s' result: success=%s error=%s",
                                transform_name, result.success, result.error)
                threading.Thread(target=_run, daemon=True).start()
            return _handler
        try:
            ctrl = HotkeyController(
                hotkey_spec=ct.hotkey, mode="hold",
                on_start=_make_handler(ct.id, ct.name), on_stop=lambda: None,
            )
            ctrl.start()
            custom_transform_hotkeys.append(ctrl)
        except ValueError:
            logger.warning("Custom transform %r has an unsupported hotkey spec %r, skipping", ct.name, ct.hotkey)

    # Command Mode: hold to speak an instruction, release to run it.
    command_asr = build_asr_adapter()
    command_engine = CommandModeEngine(command_asr, shared_llm, shared_insertion)

    def on_command_start() -> None:
        try:
            command_engine.start()
        except Exception:
            logger.exception("Failed to start Command Mode recording")

    def on_command_stop() -> None:
        def _run():
            result = command_engine.stop()
            logger.info("Command Mode result: success=%s intent=%s error=%s",
                        result.success, result.intent, result.error)
        threading.Thread(target=_run, daemon=True).start()

    command_hotkey = HotkeyController(
        hotkey_spec=settings.command_mode_hotkey, mode="hold",
        on_start=on_command_start, on_stop=on_command_stop,
    )
    command_hotkey.start()

    # Panic-hide: an always-reliable fallback to exclude_from_capture(),
    # since SetWindowDisplayAffinity isn't guaranteed on every OS build/
    # capture method. Toggle mode: one tap hides, another tap shows again.
    # toggle_panic_hide() already decides hide-vs-show itself (flips its own
    # _panic_hidden flag), so - same bug/fix as live_assist_hotkey above -
    # it doesn't need HotkeyController's own toggle bookkeeping on top of
    # that; without the reset, only every OTHER press actually hid/showed
    # the pill, with the no-op on_stop silently swallowing the others.
    panic_hide_hotkey = HotkeyController(
        hotkey_spec=settings.panic_hide_hotkey, mode="toggle",
        on_start=lambda: (overlay.toggle_panic_hide(), panic_hide_hotkey.reset()),
        on_stop=lambda: None,
    )
    panic_hide_hotkey.start()

    def open_main_window() -> None:
        main_window.show()

    def quit_app() -> None:
        hotkey.stop()
        transform_hotkey.stop()
        prompt_engineer_hotkey.stop()
        for ctrl in custom_transform_hotkeys:
            ctrl.stop()
        command_hotkey.stop()
        panic_hide_hotkey.stop()
        live_assist_hotkey.stop()
        live_assist_ask_hotkey.stop()
        live_assist_capture_hotkey.stop()
        webview.windows[:] and [w.destroy() for w in list(webview.windows)]

    # Notetaker: toggled from the tray menu (continuous background recording,
    # not a quick hotkey tap). Least-proven feature per the design notes' Risk 5 -
    # loopback capture was spiked and confirmed working, but this is the
    # newest/least-tested capture path end-to-end.
    meeting_state: dict = {"recorder": None}

    def toggle_meeting() -> None:
        if meeting_state["recorder"] is None:
            logger.info("Starting meeting recording (mic + system audio loopback)")
            rec = MeetingRecorder()
            rec.start()
            meeting_state["recorder"] = rec
        else:
            logger.info("Stopping meeting recording, processing transcript/summary...")
            rec = meeting_state["recorder"]
            meeting_state["recorder"] = None
            pcm = rec.stop()

            def _process():
                try:
                    notetaker_asr = build_asr_adapter()
                    result = process_meeting(pcm, notetaker_asr, shared_llm)
                    logger.info("Meeting processed: session_id=%s transcript_len=%d",
                                result.session_id, len(result.transcript))
                except Exception:
                    logger.exception("Meeting processing failed")
            threading.Thread(target=_process, daemon=True).start()
        overlay.set_meeting_active(meeting_state["recorder"] is not None)

    overlay.on_record_click = toggle_meeting

    # Live Assist: LA-04/LA-05 - real two-source capture (mic "me" / system-
    # output loopback "they") and multi-stream ASR now sit behind the LA-03
    # session skeleton. Controlled from the pill - the only overlay surface
    # this app has - not a tray entry, mirroring how the meeting toggle
    # above is wired.
    live_assist_state: dict = {"orchestrator": None}
    live_assist_overlay = LiveAssistOverlayWindow()
    live_assist_overlay.create()
    main_window.api.live_assist_overlay = live_assist_overlay

    def get_live_assist_asr():
        """Factory, not a cache - called fresh by LiveAssistOrchestrator.start()
        on every session (see orchestrator.py's asr_factory=). Confirmed as a
        real bug: caching the built adapter in live_assist_state used to mean
        the orchestrator (itself cached for the process's lifetime) held one
        `self.asr` forever - clearing a module-level cache alone never
        reached it, so a changed live_assist_asr registry row, or the
        realtime adapter's stale WebSocket state from a previous session,
        never actually went away until the whole app restarted.
        The one exception: nemotron-local's sherpa-onnx model load is
        genuinely expensive (~2-3s) and has no per-connection state that can
        go stale between sessions (every start_source() call already gets a
        brand new stream object), so that one case still caches the loaded
        recognizer across sessions - keyed on the registry assignment, so a
        changed assignment still rebuilds it.
        """
        # "live_assist_asr" is its own registry stage so Live Assist's ASR
        # can be swapped independently of plain dictation's "asr" stage
        # (e.g. keep dictation on local Nemotron but point Live Assist at
        # a cloud API because the local model is too slow for two
        # concurrent streams on this GPU). Falls back to the "asr" stage
        # when no live_assist_asr row has been configured, so existing
        # setups keep working unchanged.
        asr_assignment = registry_store.get_active("live_assist_asr") or registry_store.get_active("asr")
        if asr_assignment is None:
            raise RuntimeError("Model registry missing active asr/live_assist_asr assignment")
        if asr_assignment.provider == "nemotron-local":
            cache_key = (asr_assignment.provider, asr_assignment.base_url)
            if live_assist_state.get("nemotron_asr_key") != cache_key:
                # num_threads=2 (not the single-stream adapter's default of 4):
                # two sources decode concurrently here, each requesting its own
                # intra-op thread pool - at 4 each that's up to 8 compute
                # threads fighting for cores, which measured live as CPU
                # pegged at ~83% and made the whole machine feel unusable.
                # 2+2=4 matches the single-session dictation adapter's original
                # total footprint.
                live_assist_state["nemotron_asr"] = MultiStreamAsrAdapter(
                    model_dir=asr_assignment.base_url, num_threads=2)
                live_assist_state["nemotron_asr_key"] = cache_key
            return live_assist_state["nemotron_asr"]
        if asr_assignment.provider == "openai-realtime":
            # True streaming via OpenAI's Realtime transcription API - real
            # interim text as you speak, not batch_asr.py's "buffer until a
            # chunk closes, then one HTTP POST". Confirmed live in the
            # reference app Persua's own decompiled code (see
            # realtime_asr.py's docstring) that this is also how to keep it
            # cheap: the adapter's noise gate means silence is never
            # appended to the socket, so it's never billed either - without
            # that, a persistent connection streams (and bills for) dead air
            # for the whole session.
            asr_api_key = (secret_store.get_api_key(asr_assignment.api_key_ref)
                           if asr_assignment.api_key_ref else None)
            if not asr_api_key:
                raise RuntimeError("live_assist_asr provider is openai-realtime but no API key is stored")
            return TwoSourceRealtimeAsrAdapter(
                api_key=asr_api_key, model=asr_assignment.model_id or "gpt-live-transcribe",
            )
        if asr_assignment.provider == "deepgram":
            # True streaming via Deepgram's Nova-3 WebSocket API - see
            # deepgram_asr.py's docstring for why this was added: roughly
            # half OpenAI Realtime's per-minute cost, confirmed sub-300ms
            # latency, and no silence billing (same noise-gate benefit).
            asr_api_key = (secret_store.get_api_key(asr_assignment.api_key_ref)
                           if asr_assignment.api_key_ref else None)
            if not asr_api_key:
                raise RuntimeError("live_assist_asr provider is deepgram but no API key is stored")
            return TwoSourceDeepgramAsrAdapter(
                api_key=asr_api_key, model=asr_assignment.model_id or "nova-3",
            )
        if asr_assignment.provider == "whisperlivekit":
            # Self-hosted faster-whisper streaming server on the LAN (LXC
            # vmid 141, "whisper-live", 192.168.1.36) - see
            # whisperlivekit_asr.py's docstring for why: Deepgram's real-world
            # latency turned out far worse than documented (1.4-3s, confirmed
            # via live timing logs), likely network/routing related. A LAN
            # round trip removes that variable entirely, and the R730 this
            # runs on has far more CPU headroom than this laptop. base_url is
            # the server's http(s) origin, e.g. "http://192.168.1.36:8765".
            return TwoSourceWhisperLiveKitAdapter(base_url=asr_assignment.base_url)
        # Any OpenAI-compatible batch transcription host (OpenAI's own API
        # included - base_url="https://api.openai.com", model_id="whisper-1"
        # or "gpt-4o-transcribe") works here via the same WhisperHttpAdapter
        # plain dictation already uses, just wrapped per-source since this
        # needs two independent streams at once. No live interim text with
        # this path - see batch_asr.py's docstring - but far faster than
        # local Nemotron on underpowered hardware.
        asr_api_key = (secret_store.get_api_key(asr_assignment.api_key_ref)
                       if asr_assignment.api_key_ref else None)
        return TwoSourceBatchAsrAdapter(
            base_url=asr_assignment.base_url, model_id=asr_assignment.model_id, api_key=asr_api_key,
        )

    def on_live_assist_state_change(session) -> None:
        overlay.set_live_assist_active(session.state in (
            LiveAssistState.ARMING, LiveAssistState.ACTIVE, LiveAssistState.PAUSED))
        live_assist_overlay.set_session_state(session.state)
        if session.state == LiveAssistState.ARMING:
            live_assist_overlay.show()
        elif session.state in (LiveAssistState.ENDED, LiveAssistState.ERROR):
            live_assist_overlay.hide()

    def on_live_assist_utterance(source_id: str, text: str, is_final: bool) -> None:
        logger.info("live assist utterance: source=%s final=%s text=%r", source_id, is_final, text)
        live_assist_overlay.append_utterance(source_id, text, is_final)

    def toggle_live_assist() -> None:
        orch = live_assist_state["orchestrator"]
        if orch is None:
            try:
                orch = LiveAssistOrchestrator(
                    asr_factory=get_live_assist_asr, allowed_languages=settings.language_allowlist,
                    on_state_change=on_live_assist_state_change, on_utterance=on_live_assist_utterance,
                )
            except Exception:
                logger.exception("Could not start Live Assist")
                return
            live_assist_state["orchestrator"] = orch
        if orch.state in (None, LiveAssistState.ENDED, LiveAssistState.ERROR):
            orch.start()
        else:
            orch.stop()

    overlay.on_live_assist_click = toggle_live_assist

    def live_assist_pause_resume() -> None:
        orch = live_assist_state["orchestrator"]
        if orch is None:
            return
        if orch.state == LiveAssistState.PAUSED:
            orch.resume()
        elif orch.state == LiveAssistState.ACTIVE:
            orch.pause()

    def live_assist_stop() -> None:
        orch = live_assist_state["orchestrator"]
        if orch is not None:
            orch.stop()

    live_assist_overlay.on_pause_resume_click = live_assist_pause_resume
    live_assist_overlay.on_stop_click = live_assist_stop

    live_assist_hotkey = HotkeyController(
        hotkey_spec=settings.live_assist_hotkey, mode="toggle",
        # toggle_live_assist() already decides start-vs-stop itself (from
        # live_assist_state), so it doesn't need HotkeyController's own
        # toggle bookkeeping on top - on_stop was a no-op, which meant
        # _active stayed stuck "on" after every press, so only every OTHER
        # press actually reached toggle_live_assist() (the no-op on_stop
        # fired on the others). Resetting right after every press makes
        # every single press call toggle_live_assist() fresh, which is
        # exactly what the pill's own click handler already does (same
        # function, no such bug there - it's not filtered through
        # HotkeyController at all).
        on_start=lambda: (toggle_live_assist(), live_assist_hotkey.reset()),
        on_stop=lambda: None,
    )
    live_assist_hotkey.start()

    # LA-09: live suggestion engine - lazily built on first ask, same
    # cached-singleton pattern as live_assist_state["asr"].
    live_assist_suggestion_state: dict = {"engine": None}

    def get_suggestion_engine() -> SuggestionEngine:
        if live_assist_suggestion_state["engine"] is None:
            llm = build_live_suggestion_llm()
            live_assist_suggestion_state["engine"] = SuggestionEngine(
                llm, on_result=lambda text, has_suggestion: live_assist_overlay.set_suggestion(text, has_suggestion),
            )
        return live_assist_suggestion_state["engine"]

    def trigger_live_assist_ask() -> None:
        # Ask is a one-shot action, not a real start/stop toggle, but
        # HotkeyController only has "hold"/"toggle" modes - "toggle" with a
        # no-op on_stop meant every press alternated between actually
        # firing (on_start) and silently doing nothing (on_stop), i.e. it
        # took two presses to get one suggestion request. Resetting right
        # after every press, on every exit path (including "no active
        # session"), makes HotkeyController treat every press as a fresh
        # "first" press, so every tap fires.
        try:
            orch = live_assist_state["orchestrator"]
            if orch is None or orch.state not in (LiveAssistState.ACTIVE, LiveAssistState.PAUSED):
                logger.info("Live Assist: ask hotkey pressed with no active session - ignored")
                return
            try:
                engine = get_suggestion_engine()
            except Exception:
                logger.exception("Could not build the live suggestion engine")
                return
            live_assist_overlay.set_suggestion_pending()
            settings_now = Settings.load()
            language = settings_now.language_allowlist[0] if settings_now.language_allowlist else "en"
            engine.request(
                session_id=orch._session.id, assistant_id=settings_now.live_assist_active_assistant_id,
                language=language,
            )
        finally:
            live_assist_ask_hotkey.reset()

    live_assist_ask_hotkey = HotkeyController(
        hotkey_spec=settings.live_assist_ask_hotkey, mode="toggle",
        on_start=trigger_live_assist_ask, on_stop=lambda: None,
    )
    live_assist_ask_hotkey.start()

    def trigger_live_assist_capture() -> None:
        # LA-10/LA-11 (screen capture + vision answer) aren't built yet -
        # this hotkey is wired per LA-07's acceptance ("four new bindings...
        # fire correctly") but intentionally a no-op stub until then, not a
        # silent failure disguised as success.
        logger.info("Live Assist: capture hotkey pressed - screen capture (LA-10) isn't implemented yet")
        # Same one-shot-action-on-"toggle"-mode fix as trigger_live_assist_ask
        # above - without this, only every other press would even reach here.
        live_assist_capture_hotkey.reset()

    live_assist_capture_hotkey = HotkeyController(
        hotkey_spec=settings.live_assist_capture_hotkey, mode="toggle",
        on_start=trigger_live_assist_capture, on_stop=lambda: None,
    )
    live_assist_capture_hotkey.start()

    tray = TrayIcon(
        on_open_main=open_main_window, on_quit=quit_app,
        on_toggle_meeting=toggle_meeting,
        meeting_active=lambda: meeting_state["recorder"] is not None,
    )
    tray.run_in_background()
    single_instance.listen_for_show_requests(main_window.bring_to_front)

    logger.info("TalkFlow running. Hotkey=%s mode=%s", settings.hotkey, settings.trigger_mode)
    webview.start(icon=str(ICON_PATH) if ICON_PATH.exists() else None)


if __name__ == "__main__":
    main()
