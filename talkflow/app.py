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

from talkflow.asr.base import AsrAdapter
from talkflow.asr.nemotron_sherpa_adapter import NemotronSherpaAdapter
from talkflow.asr.whisper_http_adapter import WhisperHttpAdapter, ensure_model_downloaded
from talkflow.capture.hotkey import HotkeyController
from talkflow.config.paths import log_dir
from talkflow.config.settings import Settings
from talkflow.features.command_mode.engine import CommandModeEngine
from talkflow.features.notetaker.capture import MeetingRecorder
from talkflow.features.notetaker.pipeline import process_meeting
from talkflow.features.transforms.engine import TransformsEngine
from talkflow.features.transforms.store import list_custom_transforms
from talkflow.floatingnotes.window import FloatingNotesWindow
from talkflow.history.db import init_db
from talkflow.insertion.insertion_service import InsertionService
from talkflow.llm.openai_compatible_adapter import OpenAiCompatibleAdapter
from talkflow.mainwindow.main_window import MainWindow
from talkflow.overlay.overlay_window import OverlayWindow
from talkflow.pipeline.orchestrator import Orchestrator
from talkflow.registry import secrets as secret_store
from talkflow.registry import store as registry_store
from talkflow.tray.tray_icon import TrayIcon

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

    def open_main_window() -> None:
        main_window.show()

    def quit_app() -> None:
        hotkey.stop()
        transform_hotkey.stop()
        prompt_engineer_hotkey.stop()
        for ctrl in custom_transform_hotkeys:
            ctrl.stop()
        command_hotkey.stop()
        webview.windows[:] and [w.destroy() for w in list(webview.windows)]

    # Notetaker: toggled from the tray menu (continuous background recording,
    # not a quick hotkey tap). Least-proven feature per CLAUDE.md Risk 5 -
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

    tray = TrayIcon(
        on_open_main=open_main_window, on_quit=quit_app,
        on_toggle_meeting=toggle_meeting,
        meeting_active=lambda: meeting_state["recorder"] is not None,
    )
    tray.run_in_background()

    logger.info("TalkFlow running. Hotkey=%s mode=%s", settings.hotkey, settings.trigger_mode)
    webview.start(icon=str(ICON_PATH) if ICON_PATH.exists() else None)


if __name__ == "__main__":
    main()
