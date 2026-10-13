"""Lifecycle state machine: idle -> arming -> recording -> stopping ->
transcribing -> formatting -> inserting -> complete (or error/cancelled at
any point). Wires capture -> ASR -> LLM cleanup -> insertion -> history,
resolving each stage's provider from the model registry (never hardcoded).
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field

from talkflow.asr.base import AsrAdapter
from talkflow.capture.recorder import AudioRecorder
from talkflow.capture.vad import FRAME_MS, ChunkBoundaryDetector
from talkflow.features.dictionary.bias import build_initial_prompt
from talkflow.features.dictionary.store import all_terms
from talkflow.features.snippets.matcher import expand as expand_snippets
from talkflow.features.snippets.store import list_snippets
from talkflow.formality.app_assignment import get_preset_for_app
from talkflow.formality.presets import get_preset
from talkflow.history.repository import list_recent, save_session
from talkflow.insertion.insertion_service import InsertionService
from talkflow.llm.base import LlmAdapter
from talkflow.pipeline import timing
from talkflow.pipeline.context_provider import get_focused_target
from talkflow.pipeline.session import DictationSession, SessionState

logger = logging.getLogger(__name__)


TAIL_GRACE_S = 0.35
# The grace wait is skipped when the VAD has already seen this much silence.
TRAILING_SILENCE_SKIPS_GRACE_MS = 300

# Partial paste: a pause this long closes a segment, which is transcribed,
# cleaned and pasted while recording continues.
SEGMENT_PAUSE_MS = 600
# Shorter segments are left open: very short clips make Whisper hallucinate
# and give the cleanup stage too little context.
MIN_SEGMENT_S = 3.0
# After the first pasted piece, a tail with less detected speech than this
# is treated as noise rather than sent to ASR.
MIN_TAIL_SPEECH_MS = 150
PREVIOUS_TEXT_CONTEXT_CHARS = 400


def _trim_silence(pcm: bytes, sample_rate: int = 16000, pad_s: float = 0.25) -> bytes:
    """Drop leading/trailing silence (keeping `pad_s` of margin so no word onset
    or tail is clipped). Shorter audio = less to upload and process. Returns
    the input unchanged if no clear speech is found."""
    import numpy as np
    x = np.frombuffer(pcm, dtype=np.int16)
    frame = int(sample_rate * 0.02)
    n = len(x) // frame
    if n < 4:
        return pcm
    env = np.abs(x[: n * frame].astype(np.int32)).reshape(n, frame).mean(axis=1)
    thresh = max(120.0, float(env.max()) * 0.06)
    voiced = np.nonzero(env > thresh)[0]
    if len(voiced) == 0:
        return pcm
    pad = int(pad_s / 0.02)
    start = max(0, int(voiced[0]) - pad) * frame
    end = min(n, int(voiced[-1]) + 1 + pad) * frame
    return x[start:end].tobytes()


def _is_silent(pcm: bytes, peak_threshold: int = 600) -> bool:
    """True if a 16-bit PCM clip never rises above background noise."""
    if len(pcm) < 3200:  # < 0.1s
        return True
    import numpy as np
    return int(np.abs(np.frombuffer(pcm, dtype=np.int16)).max()) < peak_threshold


@dataclass
class _PartialRun:
    """State of one partial-paste dictation, shared between the recorder's
    frame thread (enqueues closed segments) and the segment worker."""
    queue: "queue.Queue[bytes | None]" = field(default_factory=queue.Queue)
    thread: threading.Thread | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    # True: each segment is cleaned and pasted on its own while recording.
    # False: segments are only transcribed while recording; cleanup and paste
    # happen once, on the whole transcript, at stop.
    paste_pieces: bool = False
    consumed_bytes: int = 0      # audio already handed to the worker
    segments_enqueued: int = 0
    raw_parts: list[str] = field(default_factory=list)
    clean_parts: list[str] = field(default_factory=list)
    language: str | None = None
    target: object | None = None
    preset: object | None = None
    preset_id: str | None = None
    clipboard_session: bool = False
    insert_error: str | None = None
    insert_method: str | None = None
    error: tuple[str, str] | None = None   # (error_code, message)
    # Timing record (see talkflow/pipeline/timing.py).
    t_stop: float | None = None                          # when the stop key was handled
    segments: list[dict] = field(default_factory=list)   # one entry per ASR call
    after_stop: dict = field(default_factory=dict)       # phase name -> ms


class Orchestrator:
    def __init__(
        self,
        asr: AsrAdapter,
        llm: LlmAdapter,
        insertion: InsertionService,
        allowed_languages: list[str],
        input_device_id: int | None = None,
        on_state_change=None,  # Callable[[DictationSession], None] - drives the overlay UI
        partial_paste: bool = False,
        incremental_asr: bool = False,
    ):
        self.asr = asr
        self.llm = llm
        self.insertion = insertion
        self.allowed_languages = allowed_languages
        self.input_device_id = input_device_id
        self.on_state_change = on_state_change
        # Paste cleaned text piece by piece at each pause while still
        # recording. Needs an ASR adapter with a stateless transcribe_pcm().
        self.partial_paste = partial_paste
        # Transcribe each segment at its pause while recording continues, so
        # only the tail is left for ASR at stop - but clean up and paste ONCE,
        # on the whole transcript. Cleaning piece by piece (partial_paste) gave
        # the LLM too little context to do its job.
        self.incremental_asr = incremental_asr

        self._recorder: AudioRecorder | None = None
        self._vad: ChunkBoundaryDetector | None = None
        self._pending_frames: list[bytes] = []
        self._frame_level = False
        self._session: DictationSession | None = None
        self._run: _PartialRun | None = None
        self._seg_frames: list[bytes] = []
        self._seg_speech_frames = 0
        self._last_language: str | None = None  # language of the last good dictation

    def _allowed_bases(self) -> list[str]:
        return [a.split("-")[0].lower() for a in self.allowed_languages]

    def _preferred_language(self) -> str | None:
        """The allowlisted language the user dictated in most recently - what
        the ASR adapter falls back to when detection lands outside the allowlist."""
        if self._last_language:
            return self._last_language
        try:
            for row in list_recent(20):
                lang = (row["detected_language"] or "").split("-")[0].lower()
                if row["state"] == "complete" and lang in self._allowed_bases():
                    return lang
        except Exception:
            logger.exception("could not read the last dictation language from history")
        return None

    def _remember_language(self, lang: str | None) -> None:
        if lang and lang.split("-")[0].lower() in self._allowed_bases():
            self._last_language = lang.split("-")[0].lower()

    def _emit(self) -> None:
        if self.on_state_change and self._session:
            self.on_state_change(self._session)

    def start(self, trigger_mode: str) -> None:
        self._session = DictationSession(
            state=SessionState.ARMING,
            input_device_id=str(self.input_device_id) if self.input_device_id is not None else None,
            trigger_mode=trigger_mode,
            allowed_languages=self.allowed_languages,
        )
        self._emit()

        try:
            initial_prompt = build_initial_prompt()
        except Exception:
            initial_prompt = None
        self.asr.start_session(self.allowed_languages, initial_prompt=initial_prompt)
        if hasattr(self.asr, "preferred_language"):
            self.asr.preferred_language = self._preferred_language()
        self._pending_frames = []
        self._warm_connections()

        self._run = None
        self._frame_level = bool(getattr(getattr(self.asr, "capabilities", None), "frame_level", False))
        if self._frame_level:
            # The ASR stream itself is the "incremental" layer here - no VAD
            # batching, no segment worker, no stateless transcribe_pcm() to
            # fall back on. _on_frame() feeds every captured frame straight
            # to the adapter; finalize() at stop() reconciles the transcript.
            self._vad = None
        elif (self.partial_paste or self.incremental_asr) and hasattr(self.asr, "transcribe_pcm"):
            self._vad = ChunkBoundaryDetector(silence_ms_for_boundary=SEGMENT_PAUSE_MS)
            self._seg_frames = []
            self._seg_speech_frames = 0
            run = _PartialRun(paste_pieces=self.partial_paste)
            run.thread = threading.Thread(target=self._segment_worker, args=(run,), daemon=True)
            run.thread.start()
            self._run = run
        else:
            self._vad = ChunkBoundaryDetector()

        self._recorder = AudioRecorder(device_id=self.input_device_id)
        self._session.state = SessionState.RECORDING
        self._emit()
        self._recorder.start(on_frame=self._on_frame)

    def _warm_connections(self) -> None:
        # Open the HTTP connections while the user is still speaking.
        for adapter in (self.asr, self.llm):
            warm = getattr(adapter, "warm", None)
            if warm:
                threading.Thread(target=warm, daemon=True).start()

    def _on_frame(self, frame: bytes) -> None:
        if self._frame_level:
            try:
                partial = self.asr.feed_chunk(frame, sample_rate=16000)
            except Exception:
                logger.exception("streaming ASR feed_chunk failed")
                return
            if partial is not None and self._session:
                self._session.raw_transcript = partial.text
                self._emit()
            return
        if self._run is not None:
            self._on_frame_partial(frame, self._run)
            return
        self._pending_frames.append(frame)
        if self._vad and self._vad.feed_frame(frame):
            pcm = b"".join(self._pending_frames)
            self._pending_frames = []
            try:
                self.asr.feed_chunk(pcm, sample_rate=16000)
            except Exception:
                logger.exception("rolling ASR feed_chunk failed")

    # ---- partial paste ---------------------------------------------------

    def _on_frame_partial(self, frame: bytes, run: _PartialRun) -> None:
        """Runs on the recorder's frame thread: only bookkeeping and a queue
        put, never a network call, so capture is never held up."""
        self._seg_frames.append(frame)
        boundary = self._vad.feed_frame(frame) if self._vad else False
        if getattr(self._vad, "last_frame_was_speech", False):
            self._seg_speech_frames += 1
        if not boundary or not getattr(self._vad, "last_boundary_was_silence", False):
            return
        if len(self._seg_frames) * FRAME_MS < MIN_SEGMENT_S * 1000:
            return
        pcm = b"".join(self._seg_frames)
        self._seg_frames = []
        self._seg_speech_frames = 0
        run.consumed_bytes += len(pcm)
        run.segments_enqueued += 1
        run.queue.put(pcm)

    def _segment_worker(self, run: _PartialRun) -> None:
        while True:
            pcm = run.queue.get()
            if pcm is None:
                return
            if run.cancelled.is_set() or run.error:
                continue
            try:
                self._process_segment(run, pcm)
            except Exception as e:
                logger.exception("segment processing failed")
                run.error = ("pipeline_error", str(e))

    def _process_segment(self, run: _PartialRun, pcm: bytes) -> None:
        """ASR -> snippets -> cleanup -> paste for one segment."""
        pcm = _trim_silence(pcm)
        if _is_silent(pcm):
            return

        t0 = time.monotonic()
        try:
            final = self.asr.transcribe_pcm(pcm, 16000, language=run.language)
        except Exception as e:
            run.error = ("asr_error", str(e))
            return
        text = final.text.strip()
        seg = {"audio_s": round(len(pcm) / 32000, 2), "asr_ms": round((time.monotonic() - t0) * 1000),
               "chars": len(text), "after_stop": run.t_stop is not None and t0 >= run.t_stop}
        run.segments.append(seg)
        if not text:
            return
        detected = (final.detected_language or "").split("-")[0].lower()
        if run.language is None and detected in self._allowed_bases():
            # Later segments of the same dictation are pinned to this language
            # - but only ever to one the user configured.
            run.language = detected

        if not run.paste_pieces:
            # Cleanup and paste happen once, on the whole transcript, at stop.
            run.raw_parts.append(text)
            return

        try:
            expanded_text = expand_snippets(text, list_snippets())
        except Exception:
            logger.exception("snippet expansion failed; using raw transcript")
            expanded_text = text

        if run.target is None:
            run.target = get_focused_target()
            run.preset_id = get_preset_for_app(run.target.process_name)
            run.preset = get_preset(run.preset_id)
        preset = run.preset

        t1 = time.monotonic()
        kwargs = {}
        if run.clean_parts:
            kwargs["previous_text"] = " ".join(run.clean_parts)[-PREVIOUS_TEXT_CONTEXT_CHARS:]
        try:
            result = self.llm.cleanup(
                raw_transcript=expanded_text,
                language=run.language or self.allowed_languages[0],
                formality_name=preset.name if preset else "Normal",
                formality_instruction=preset.instruction if preset else "Clear, neutral, professional.",
                target_process_name=run.target.process_name,
                dictionary_terms=all_terms() or None,
                **kwargs,
            )
        except Exception as e:
            run.error = ("llm_error", str(e))
            return
        seg["cleanup_ms"] = round((time.monotonic() - t1) * 1000)
        cleaned = result.cleaned_text.strip()
        if not cleaned or run.cancelled.is_set():
            return

        if not run.clipboard_session:
            begin = getattr(self.insertion, "begin_session", None)
            if begin:
                begin()
            run.clipboard_session = True
        t2 = time.monotonic()
        outcome = self.insertion.insert((" " + cleaned) if run.clean_parts else cleaned)
        seg["paste_ms"] = round((time.monotonic() - t2) * 1000)
        run.raw_parts.append(text)
        if not outcome.success:
            run.error = ("insertion_failed", outcome.error or "insertion failed")
            return
        run.insert_method = outcome.method
        run.clean_parts.append(cleaned)

    def _clean_and_paste_whole(self, run: _PartialRun) -> None:
        """Incremental-ASR mode, at stop: one cleanup call over the complete
        transcript, one paste."""
        raw = " ".join(run.raw_parts)
        try:
            expanded_text = expand_snippets(raw, list_snippets())
        except Exception:
            logger.exception("snippet expansion failed; using raw transcript")
            expanded_text = raw

        run.target = get_focused_target()
        run.preset_id = get_preset_for_app(run.target.process_name)
        run.preset = preset = get_preset(run.preset_id)

        self._session.state = SessionState.FORMATTING
        self._emit()
        t1 = time.monotonic()
        try:
            result = self.llm.cleanup(
                raw_transcript=expanded_text,
                language=run.language or self.allowed_languages[0],
                formality_name=preset.name if preset else "Normal",
                formality_instruction=preset.instruction if preset else "Clear, neutral, professional.",
                target_process_name=run.target.process_name,
                dictionary_terms=all_terms() or None,
            )
        except Exception as e:
            run.error = ("llm_error", str(e))
            return
        run.after_stop["cleanup_ms"] = round((time.monotonic() - t1) * 1000)
        cleaned = result.cleaned_text.strip()

        self._session.state = SessionState.INSERTING
        self._emit()
        t2 = time.monotonic()
        outcome = self.insertion.insert(cleaned)
        run.after_stop["paste_ms"] = round((time.monotonic() - t2) * 1000)
        if not outcome.success:
            run.error = ("insertion_failed", outcome.error or "insertion failed")
            return
        run.insert_method = outcome.method
        run.clean_parts.append(cleaned)

    def _end_clipboard_session(self, run: _PartialRun) -> None:
        if run.clipboard_session:
            end = getattr(self.insertion, "end_session", None)
            if end:
                end()
            run.clipboard_session = False

    def _record_partial(self, run: _PartialRun) -> None:
        s = self._session
        s.raw_transcript = " ".join(run.raw_parts)
        s.cleaned_transcript = " ".join(run.clean_parts) or None
        if run.raw_parts:
            s.detected_language = run.language or self.allowed_languages[0]
        if run.target is not None:
            s.target_process_name = run.target.process_name
            s.target_window_title = run.target.window_title
            s.applied_preset_id = run.preset_id
        if run.insert_method:
            s.insertion_outcome = f"{run.insert_method}:partial" if run.paste_pieces else run.insert_method

    def _finish_partial(self, run: _PartialRun, full_pcm: bytes, t_stop: float) -> None:
        tail = full_pcm[run.consumed_bytes:]
        # Once something has been pasted, don't let a noise-only tail turn
        # into a hallucinated extra sentence.
        noise_tail = (run.segments_enqueued > 0
                      and self._seg_speech_frames * FRAME_MS < MIN_TAIL_SPEECH_MS)
        if tail and not noise_tail:
            run.queue.put(tail)
        run.queue.put(None)

        self._session.state = SessionState.TRANSCRIBING
        self._emit()
        t_wait = time.monotonic()
        run.thread.join()
        # Time spent after the stop key waiting for transcription still in
        # flight or queued (in partial-paste mode this includes the pieces'
        # own cleanup and paste).
        run.after_stop["asr_wait_ms"] = round((time.monotonic() - t_wait) * 1000)
        self._run = None
        if not run.paste_pieces and run.raw_parts and not run.error:
            self._clean_and_paste_whole(run)
        stop_to_paste_ms = round((time.monotonic() - t_stop) * 1000)
        self._end_clipboard_session(run)
        self._record_partial(run)

        timing.report({
            "mode": "partial paste" if run.paste_pieces else "incremental asr, single cleanup",
            "asr_model": getattr(self.asr, "model_id", "?"),
            "asr_host": getattr(self.asr, "base_url", "?"),
            "llm_model": getattr(self.llm, "model_id", "?"),
            "language": run.language,
            "audio_s": round(len(full_pcm) / 32000, 2),
            "raw_chars": len(self._session.raw_transcript or ""),
            "clean_chars": len(self._session.cleaned_transcript or ""),
            "segments": run.segments,
            "after_stop": run.after_stop,
            "stop_to_paste_ms": stop_to_paste_ms,
            "error": run.error[0] if run.error else None,
        })

        if run.error:
            code, message = run.error
            if code == "insertion_failed":
                self._session.insertion_outcome = f"failed:{message}"
            self._fail(code, message)
            return
        if not run.raw_parts:
            self._session.finish(SessionState.CANCELLED)
            self._session.error_code = "no_speech"
        else:
            self._remember_language(run.language)
            self._session.finish(SessionState.COMPLETE)
        save_session(self._session)
        self._emit()

    # ---- stop / cancel ---------------------------------------------------

    def _tail_grace_s(self) -> float:
        # People tap/release a beat before they finish the last word, and the
        # mic pipeline has a little latency: keep capturing briefly - unless
        # the VAD already shows they stopped talking a moment ago.
        if getattr(self._vad, "trailing_silence_ms", 0) >= TRAILING_SILENCE_SKIPS_GRACE_MS:
            return 0.0
        return TAIL_GRACE_S

    def stop(self) -> None:
        if not self._recorder or not self._session:
            return
        t_stop = time.monotonic()
        self._session.state = SessionState.STOPPING
        self._emit()

        if self._run is not None:
            self._run.t_stop = t_stop
        grace = self._tail_grace_s()
        if grace:
            time.sleep(grace)
        t_rec = time.monotonic()
        full_pcm = self._recorder.stop()
        self._recorder = None
        if self._run is not None:
            self._run.after_stop["grace_ms"] = round(grace * 1000)
            self._run.after_stop["recorder_stop_ms"] = round((time.monotonic() - t_rec) * 1000)
            self._finish_partial(self._run, full_pcm, t_stop)
            return
        if _is_silent(full_pcm):
            # Whisper hallucinates text ("You", "Thanks for watching") on
            # silence, so don't even send it.
            self._end_frame_level_stream()
            self._session.raw_transcript = ""
            self._session.finish(SessionState.CANCELLED)
            self._session.error_code = "no_speech"
            save_session(self._session)
            self._emit()
            return
        if hasattr(self.asr, "set_full_audio"):
            self.asr.set_full_audio(_trim_silence(full_pcm), 16000)

        self._session.state = SessionState.TRANSCRIBING
        self._emit()
        t_asr = time.monotonic()
        try:
            final = self.asr.finalize()
        except Exception as e:
            self._fail("asr_error", str(e))
            return
        asr_ms = (time.monotonic() - t_asr) * 1000

        self._session.raw_transcript = final.text
        self._session.detected_language = final.detected_language or self.allowed_languages[0]
        self._session.detected_language_confidence = final.detected_language_confidence

        if not final.text.strip():
            self._session.finish(SessionState.CANCELLED)
            self._session.error_code = "no_speech"
            save_session(self._session)
            self._emit()
            return

        try:
            expanded_text = expand_snippets(final.text, list_snippets())
        except Exception:
            logger.exception("snippet expansion failed; using raw transcript")
            expanded_text = final.text

        target = get_focused_target()
        self._session.target_process_name = target.process_name
        self._session.target_window_title = target.window_title

        preset_id = get_preset_for_app(target.process_name)
        preset = get_preset(preset_id)
        self._session.applied_preset_id = preset_id

        self._session.state = SessionState.FORMATTING
        self._emit()
        t_llm = time.monotonic()
        try:
            result = self.llm.cleanup(
                raw_transcript=expanded_text,
                language=self._session.detected_language,
                formality_name=preset.name if preset else "Normal",
                formality_instruction=preset.instruction if preset else "Clear, neutral, professional.",
                target_process_name=target.process_name,
                dictionary_terms=all_terms() or None,
            )
        except Exception as e:
            self._fail("llm_error", str(e))
            return

        llm_ms = (time.monotonic() - t_llm) * 1000
        self._session.cleaned_transcript = result.cleaned_text

        self._session.state = SessionState.INSERTING
        self._emit()
        t_insert = time.monotonic()
        outcome = self.insertion.insert(result.cleaned_text)
        logger.info(
            "dictation timing: audio_s=%.1f grace_ms=%.0f asr_ms=%.0f llm_ms=%.0f insert_ms=%.0f stop_to_done_ms=%.0f",
            len(full_pcm) / 32000, grace * 1000, asr_ms, llm_ms,
            (time.monotonic() - t_insert) * 1000, (time.monotonic() - t_stop) * 1000,
        )
        self._session.insertion_outcome = outcome.method if outcome.success else f"failed:{outcome.error}"

        if outcome.success:
            self._remember_language(self._session.detected_language)
        self._session.finish(SessionState.COMPLETE if outcome.success else SessionState.ERROR)
        if not outcome.success:
            self._session.error_code = "insertion_failed"
        save_session(self._session)
        self._emit()

    def _end_frame_level_stream(self) -> None:
        """Signal end-of-stream to a frame_level adapter without blocking the
        caller on finalize()'s network round-trip; the result is discarded
        (used when the dictation never reaches the normal finalize() call,
        e.g. silence or cancel), but the sentinel still has to be sent so the
        adapter's background stream thread isn't left waiting forever."""
        if self._frame_level:
            threading.Thread(target=self.asr.finalize, daemon=True).start()

    def cancel(self) -> None:
        if self._recorder:
            self._recorder.cancel()
            self._recorder = None
        self._end_frame_level_stream()
        run, self._run = self._run, None
        if run is not None:
            # Pieces already pasted stay in the field; nothing more is pasted.
            run.cancelled.set()
            run.queue.put(None)
            self._end_clipboard_session(run)
            if self._session:
                self._record_partial(run)
        if self._session:
            self._session.finish(SessionState.CANCELLED)
            save_session(self._session)
            self._emit()

    def _fail(self, error_code: str, message: str) -> None:
        logger.error("%s: %s", error_code, message)
        if self._session:
            self._session.error_code = error_code
            self._session.finish(SessionState.ERROR)
            save_session(self._session)
            self._emit()
