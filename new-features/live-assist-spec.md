# Live Assist — technical specification

Status: proposed · 2026-11-04. This is the detailed spec behind [README.md](README.md).
"Live Assist" is this new feature set's working name (module path
`talkflow/features/liveassist/`) — not a product/marketing name. Every claim about today's code
below is cited to a real file in this repository so a future contributor (or a small model) can
open that file and verify it before building on it. Everything under "Build" is new.

Read `CLAUDE.md` (repo root) first — it states this project's existing constraints (pipeline
stages independently configurable, raw/cleaned text always kept separate, no admin rights on the
dev machine, secrets only in `keyring`, privacy by default) and this spec follows them rather
than repeating them per feature.

**This feature set replaces the existing Notetaker/meeting feature** — see the note at the top
of [README.md](README.md) and Feature 8/12 below. Don't read what follows as "a new feature
alongside Notetaker"; read it as "Notetaker's eventual replacement, built the right way from the
start because Notetaker's one-shot batch design can't become live."

## Shared building blocks used across features below

- **Registry** (`talkflow/registry/`): `ModelAssignment(id, stage, name, provider, base_url,
  model_id, api_key_ref, is_active, extra_json)`, one active row per `stage` string (free-form —
  `"asr"`, `"cleanup"`, `"command"`, `"transform"`, `"notetaker_summary"` today). `store.py`'s
  `get_active(stage)` / `upsert()` / `activate()` need no change — we only add new `stage` values:
  `"live_suggestion"` and `"screen_answer"`.
- **Secrets** (`talkflow/registry/secrets.py`): `keyring`-backed, referenced by `api_key_ref`.
  Reused as-is.
- **DB** (`talkflow/history/db.py`): one sqlite file, `SCHEMA` string executed with
  `CREATE TABLE IF NOT EXISTS`, plus `_COLUMN_MIGRATIONS` for retrofitting a column onto an
  existing table. New tables for this feature set append to `SCHEMA`; no existing table is
  altered by the features below (Notetaker's `notetaker_sessions` table is only touched later,
  by the explicit retirement story, Feature 12/LA-15).
- **Settings** (`talkflow/config/settings.py`): one `Settings` dataclass, JSON file, loaded
  whole. New fields append to the dataclass with defaults, same pattern as existing hotkey
  fields.
- **Hotkeys** (`talkflow/capture/hotkey.py`): `HotkeyController(spec, mode, on_start, on_stop,
  on_cancel)`, one instance per bound action, each started independently in `app.py::main()`.
  Existing bindings: dictate `<caps_lock>`, Polish `<ctrl>+<shift>+p`, Prompt Engineer
  `<ctrl>+<shift>+e`, Command Mode `<ctrl>+<shift>+m`. Avoid `c`/`i`/`j` (Chrome DevTools) and
  bare F-keys (laptop Fn-lock) per the existing convention in `Settings`'s docstring.
- **Main window** (`talkflow/mainwindow/`): `MainWindow` wraps one `pywebview.Window` showing
  `mainwindow/web/index.html` (Tailwind via CDN, no build step), backed by `Api`
  (`mainwindow/api.py`) as the `js_api` bridge. A new sidebar section = a new HTML section in
  that single-page file + new `Api` methods, following the existing CRUD-method pattern (e.g.
  `list_registry`/`upsert_registry`).
- **Overlay technique** (`talkflow/overlay/overlay_window.py`): a native Win32 layered window
  (`CreateWindowEx` + `UpdateLayeredWindow`, `WS_EX_NOACTIVATE|WS_EX_TOOLWINDOW|TOPMOST`, PIL
  rendering at 4x supersampling, own message-loop thread). This is a real HWND — important
  because `SetWindowDisplayAffinity` (feature 7) only works on a real window handle, not inside
  a WebView2/Chromium surface.

---

## Feature 1 — Live session mode

**Why.** Everything this app does today is either one bounded action (a dictation: arm → record
→ stop → done) or a single record-then-batch-process job (Notetaker). A live-assist session
needs to stay open indefinitely, with the user explicitly controlling pause/resume/stop, while
multiple things happen concurrently inside it (capture, live transcription, on-demand
suggestions, occasional screen captures). None of the two existing lifecycle shapes fit:
`DictationSession`/`SessionState` (`talkflow/pipeline/session.py`) is single-shot and ends at
`COMPLETE`; Notetaker (`talkflow/features/notetaker/pipeline.py`) only processes audio *after*
`MeetingRecorder.stop()` returns the whole recording — there is no "live" state during capture
at all, `talkflow/features/notetaker/capture.py` just accumulates frames in a list.

**Build.** New module `talkflow/features/liveassist/session.py`:

```python
class LiveAssistState(str, Enum):
    IDLE = "idle"; STARTING = "starting"; ACTIVE = "active"
    PAUSED = "paused"; DEGRADED = "degraded"; STOPPING = "stopping"; ENDED = "ended"

@dataclass
class LiveAssistSession:
    id: str
    started_at: datetime
    ended_at: datetime | None = None
    state: LiveAssistState = LiveAssistState.IDLE
    assistant_id: str | None = None
    language: str = "en"
    goal: str | None = None
```

`DEGRADED` covers "mic or loopback source failed but the other is still usable" (see Feature 9).
A new `talkflow/features/liveassist/orchestrator.py::LiveAssistOrchestrator` owns this state and
is the single place that starts/stops the two capture streams (Feature 2), feeds ASR (Feature 2),
and dispatches suggestion/capture requests (Features 3, 5) — mirroring how
`pipeline/orchestrator.py::Orchestrator` is the one place dictation's capture/ASR/LLM/insertion
are wired, but built fresh rather than subclassed from it: the dictation orchestrator's state
machine, segment-pause logic and insertion step are all specific to "one bounded utterance,
paste once," which doesn't apply here.

**Entry point, transitional.** Until Feature 12/LA-15 retires it, add a *second* tray menu item
next to the existing "Start/Stop Meeting Recording" (`talkflow/tray/tray_icon.py:26-39`,
`on_toggle_meeting`/`meeting_active` callbacks passed from `app.py`): add
`on_toggle_live_assist`/`live_assist_active` the same way, rendered as "Start/Stop Live Assist."
Running both menu items side by side is intentional and temporary — it's how Notetaker keeps
working unbroken while Live Assist is built out, per the migration plan in the README. The
start/stop hotkey (Feature 10) is a second, faster entry point once the feature is trusted enough
to use mid-call.

**New DB table** (append to `talkflow/history/db.py`'s `SCHEMA`):

```sql
CREATE TABLE IF NOT EXISTS live_assist_sessions (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    state TEXT NOT NULL,
    assistant_id TEXT,
    language TEXT,
    goal TEXT
);
```

**Acceptance.** Starting a session from the tray (or hotkey) with no AI calls made yet reaches
`ACTIVE` with both sources armed; pause immediately stops new capture; resume continues the same
session row; stop is idempotent (a second `stop()` call while already `STOPPING`/`ENDED` is a
no-op, same guard style as `Orchestrator.stop()`'s state check at
`pipeline/orchestrator.py:500`); the session row's `ended_at`/`state` persist correctly in all
four exits (stop, pause-then-stop, source failure escalating to stop, process killed — the last
one is only a "last known state" DB row, not a guarantee, same honesty this app already applies
elsewhere).

---

## Feature 2 — Two-source live transcript

**Why — and the one real gap found in Notetaker's existing capture code.** This app already
proves mic+loopback capture works without admin rights
(`talkflow/features/notetaker/capture.py::MeetingRecorder`, built on `soundcard`). But look
closely at lines 41-42: `mixed = np.clip(mic_chunk[:n, 0] + loop_chunk[:n, 0], -1.0, 1.0)` — the
two sources are **summed into one mono stream before any transcription happens**. That is fine
for a one-shot meeting summary (Notetaker doesn't care who said what), but it is fatal for
Live Assist's "ME microphone / THEY output" source labelling, which requires the two streams to
stay separate all the way through ASR. This is the one piece of "reuse Notetaker's capture" that
cannot just be reused as-is — it has to be forked into two independent streams, not summed.

**Build.** New `talkflow/features/liveassist/capture.py::TwoSourceRecorder`: start two
*independent* `soundcard` recorder contexts (`sc.default_microphone()` and
`sc.get_microphone(sc.default_speaker().name, include_loopback=True)`, same device-lookup calls
as `MeetingRecorder.__init__`), each on its own thread, each calling a per-source `on_frame`
callback with labelled 16kHz mono PCM16 chunks — do not add the two signals together anywhere.
Keep `talkflow/capture/recorder.py::AudioRecorder` (the dictation mic path, `sounddevice`-based)
untouched; this is a new, separate capture class for this feature, not a shared one, because its
warm-stream-reuse design (`recorder.py:1-13`) is tuned for the hold-to-talk dictation lifecycle
and has no notion of "two sources for an indefinite session."

**The ASR side needs its own change, not just reuse.** `NemotronSherpaAdapter`
(`talkflow/asr/nemotron_sherpa_adapter.py`) is the default `frame_level=True` streaming adapter.
Its constructor (lines 84-106) loads the ONNX model **once** into a `sherpa_onnx.OnlineRecognizer`
(`self._recognizer`), and `start_session()` creates **one** lightweight `self._stream` via
`self._recognizer.create_stream()` (line 114) — note that `create_stream()` is cheap and the
*recognizer* is what's expensive to load (the three ONNX models, ~2-3s). sherpa-onnx's
`OnlineRecognizer` supports multiple concurrent streams from one recognizer by design. So: do
**not** construct two full `NemotronSherpaAdapter` instances (that reloads the model twice,
doubling memory and startup time) — instead add a thin wrapper,
`talkflow/features/liveassist/multi_stream_asr.py::MultiStreamAsrAdapter`, that holds one
`NemotronSherpaAdapter`-style `OnlineRecognizer` and exposes per-source sessions:

```python
class MultiStreamAsrAdapter:
    def __init__(self, model_dir: str): ...               # loads the recognizer ONCE
    def start_source(self, source_id: str, allowed_languages: list[str]) -> None: ...
    def feed_source_chunk(self, source_id: str, pcm: bytes) -> PartialResult | None: ...
    def finalize_source(self, source_id: str) -> FinalResult: ...
```

internally keyed by `source_id` ("me" / "they") to its own `create_stream()` result, reusing the
exact `accept_waveform` / `is_ready` / `decode_stream` / `get_result` sequence already proven in
`feed_chunk`/`finalize` (lines 128-167) — copy that sequence per source, don't share one
`_stream`. If a non-Nemotron ASR stage is ever configured for this feature, fall back to one
adapter instance per source (accepting the double-load cost) rather than block the feature on
every adapter supporting multi-stream.

**Interim vs. final, and ordering.** Each source's `PartialResult` (rolling re-transcription,
same shape dictation already uses) replaces that source's current interim line in the UI; a
`FinalResult` (only produced when the orchestrator — not the model, `enable_endpoint_detection=False`
at line 98 — decides a turn ended, e.g. on pause/stop or a silence-based boundary reusing
`talkflow/capture/vad.py::ChunkBoundaryDetector`) commits one durable row. Cross-source ordering
for display uses capture timestamp, each source keeping its own monotonic sequence number — do
not try to merge the two sources into one interleaved transcript inside the ASR layer; let the UI
(Feature 6) merge two label streams by timestamp.

**New DB table:**

```sql
CREATE TABLE IF NOT EXISTS live_assist_utterances (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES live_assist_sessions(id),
    source TEXT NOT NULL CHECK (source IN ('me','they')),
    sequence INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    text TEXT NOT NULL,
    is_final INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_live_assist_utterances_session
    ON live_assist_utterances(session_id, captured_at, sequence);
```

Only final rows are durable UPDATE-OR-INSERT by `(session_id, source, sequence)`; interim text is
an in-memory projection, never written to this table (same "raw/cleaned never collapse, interim
never counted as history" discipline this app already applies to dictation).

**Acceptance.** Known mic speech and known system-output speech (e.g. play a recorded test
clip through the default output device) produce two separately labelled final transcript lines
each, in the correct source, with no cross-talk between labels; stopping while both sources are
mid-utterance finalizes each source independently (one source's finalize failing must not drop
the other's final text).

---

## Feature 3 — Live suggestion engine

**Why.** `talkflow/llm/base.py::LlmAdapter` has exactly two methods: `cleanup()` (dictation-
flavored: preserve language, remove disfluencies, apply a formality preset) and `rewrite()`
(generic instruction-following rewrite of *existing* text — used by Transforms and Command
Mode's edit intent). Neither fits "given an ongoing two-source conversation, draft what the user
could say next": that is a different input shape (two labelled transcript streams plus an
assistant persona plus a trigger reason) and a different output framing (draft new speech, not
rewrite existing text).

**Build.** Add a third method to `LlmAdapter` (interface change, not a new unrelated class —
keep one adapter type per stage's *kind of work*, consistent with how `cleanup`/`rewrite` already
live together):

```python
@dataclass
class SuggestionResult:
    text: str
    kind: Literal["speech", "answer", "code", "none"]
    model_used: str
    latency_ms: float

class LlmAdapter(ABC):
    ...
    @abstractmethod
    def suggest(
        self, transcript_me: str, transcript_they: str, assistant_task: str,
        response_style: str, response_language: str, trigger: str,
        previous_suggestion: str | None = None,
    ) -> SuggestionResult: ...
```

Implement it in `talkflow/llm/openai_compatible_adapter.py::OpenAiCompatibleAdapter` (same class
that already implements `cleanup`/`rewrite`, reusing its existing `_post_chat()` helper at line
37 — no new HTTP client). Write an original prompt for this project (don't borrow prompt text
from any other product): task, response language, trigger reason, both labelled transcripts, the
previous suggestion (so the model doesn't repeat itself), and an explicit instruction to return a
structured no-suggestion result when nothing useful follows — map that to `kind="none"`
deterministically in the adapter (parse a small JSON envelope in the response, same "ask the
model for structure, validate before trusting it" posture `cleanup()`/`rewrite()` already need to
take with a non-function-calling local model).

**Registry.** New stage `"live_suggestion"`. Seed a default the same way
`registry/store.py::ensure_defaults()` seeds `"asr"`/`"cleanup"` today (lines 92-114) — but do
**not** assume the cleanup-stage model is good enough for this; a tiny local model (e.g.
`gemma3:1b`, the existing cleanup default per `CLAUDE.md`'s "Cleanup model comparison") is
documented there as "light-touch" at best — live suggestion quality should be validated with its
own fixture set before picking a default, per Feature 11 below.

**Triggering.** This feature ships **manual only** at first: a button/hotkey (Feature 10) calls
`orchestrator.request_suggestion(trigger="manual")`, which reads the current finalized text per
source from `live_assist_utterances`, calls `suggest()`, and emits the result to the overlay. One
request in flight at a time; a new manual request cancels/supersedes a pending one rather than
queuing silently — a stale result must never win over a newer one. Automatic completed-turn
triggering is Feature 9, intentionally shipped after manual works.

**New DB table:**

```sql
CREATE TABLE IF NOT EXISTS live_assist_suggestions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES live_assist_sessions(id),
    trigger TEXT NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    disposition TEXT
);
```

`disposition` is set only when the user explicitly copies/saves a suggestion (NULL = transient,
never surfaced as "what was said" — never conflate a suggestion row with an utterance row).

**Acceptance.** A manual ask against a known two-source fixture transcript returns a suggestion
grounded in both sides of the conversation, in the session's configured language; an empty or
irrelevant transcript returns `kind="none"` rather than a fabricated suggestion; a second manual
ask while the first is still streaming supersedes it and only the second result ever reaches the
overlay.

---

## Feature 4 — Assistant/persona config

**Why.** This app's closest existing concept is a **formality preset**
(`talkflow/formality/presets.py`, `formality_presets` table): a named instruction string applied
to dictation's register, resolved per-app. That's the wrong shape for "who is this assistant and
what is it helping with" — a live-assist persona needs a task description and response style
*independent* of any focused application (there is no "focused app" during a call), and it feeds
the suggestion engine (Feature 3), not the dictation cleanup stage.

**Build.** New table + a small CRUD module, same pattern as `talkflow/formality/presets.py` /
`style/store.py`-style modules already in the repo:

```sql
CREATE TABLE IF NOT EXISTS live_assist_assistants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    task TEXT NOT NULL,
    response_style TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    is_builtin INTEGER NOT NULL DEFAULT 0
);
```

Seed one builtin "General Assistant" row on first `init_db()` — no "assistant builder AI" needed
yet, that's out of scope for now. `Api` gains `list_live_assist_assistants` /
`create_live_assist_assistant` / `update_live_assist_assistant` /
`delete_live_assist_assistant`, mirroring the existing `list_presets`/`create_preset`/
`delete_preset` methods in `mainwindow/api.py` almost one-for-one. A new "Assistants" section in
`mainwindow/web/index.html`'s sidebar, next to the existing Style section.

**Acceptance.** Creating/editing/deleting an assistant persists immediately and the active
session picks up the selected assistant's `task`/`response_style` on its next suggestion request
(no restart required) — same "settings apply live" expectation the Model Registry tab already
sets.

---

## Feature 5 — Screen capture + question

**Why.** No screenshot/vision code exists anywhere in this repo today (confirmed — nothing
imports `PIL.ImageGrab`, `mss`, or any Win32 `BitBlt`/`PrintWindow` call). `Pillow` is already a
dependency (used by the overlay's PIL rendering, `overlay_window.py:31`), so
`PIL.ImageGrab.grab(bbox=..., all_screens=True)` needs no new package — this is the simplest
correct option on Windows and should be preferred over adding `mss` purely for this feature.

**Build.** New `talkflow/features/liveassist/screen_capture.py`:

```python
def list_monitors() -> list[MonitorInfo]: ...          # win32api.EnumDisplayMonitors
def capture_monitor(monitor_id: int) -> bytes:          # PNG bytes, via PIL.ImageGrab
    ...
```

Flow, driven from the live-assist overlay (Feature 6): user hits the Capture hotkey/button →
`list_monitors()` backs a small picker (or capture the monitor the overlay's own window is
currently on, as the one-click default, with "choose a different monitor" as a secondary
action) → `capture_monitor()` → show the PNG in a preview (reuse the overlay's own PIL rendering
path, or a small second layered window) with an optional question field → Send or Cancel. Cancel
discards the in-memory bytes immediately; nothing is written to disk by default (this matters —
see Feature 11). A failed capture (e.g. `ImageGrab.grab()` raising on a disconnected monitor) must
surface as a distinct error state, never silently retried against the wrong monitor.

**Must-exclude-self.** The live-assist overlay window must not appear inside its own captured
screenshot. Apply `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` (Feature 7) to the overlay
HWND *before* wiring this feature up — `PIL.ImageGrab.grab()` uses the same desktop-capture path
that affinity flag is designed to exclude a window from, on the same Windows versions, so this
should "just work" once Feature 7 lands, but confirm it explicitly for `ImageGrab` specifically
(it's a different capture API family than video-call screen-share, and Feature 7's spike should
test both).

**Acceptance.** Capturing a named, currently-available monitor shows an accurate preview before
anything is sent anywhere; cancelling leaves no trace; the overlay itself never appears in its
own captured image; an unplugged/invalid monitor selection produces an explicit retry/choose-
again state instead of defaulting to the primary monitor silently.

---

## Feature 6 — Live-assist overlay surface

**Why.** `talkflow/overlay/overlay_window.py::OverlayWindow` is purpose-built for the dictation
pill: five fixed layouts (idle/hover/recording/busy/message, `_LAYOUT` dict at lines 45-51), a
single `set_state(str)` entry point, no transcript/suggestion content model at all. Retrofitting
live-assist's much richer surface (two transcript feeds, current suggestion, Ask box, Capture
button, pause/stop, session duration) onto that class would either break the pill's proven
behavior or bloat one class into two unrelated UIs. Keep what's already verified working stable
and add this new surface alongside it, not inside it.

**Build.** New `talkflow/features/liveassist/overlay.py::LiveAssistOverlayWindow`, built with
the **same native Win32 layered-window technique** as `OverlayWindow` (own `CreateWindowEx` +
`UpdateLayeredWindow` + private message-loop thread — copy the mechanism, not the pill's specific
layouts), because that native-HWND property is exactly what Feature 7's capture-exclusion needs
and a WebView2/pywebview surface can't reliably give it (per `CLAUDE.md`'s own note that true
alpha/click-through was already unreliable enough on pywebview to justify the main window being
opaque). Content: a vertically laid out panel — session state + duration, two scrolling
labelled transcript lines (interim text visually distinct, e.g. italic/dimmer, from finalized
text), the current suggestion with Copy, Ask input, Capture button, Pause/Resume, Stop. Driven by
the same `on_state_change`-callback pattern `Orchestrator`/`OverlayWindow` already use
(`app.py:118-132`'s `on_state_change` closure is the template): `LiveAssistOrchestrator` pushes a
small immutable view-model object on every change; the window thread repaints from it.

**Acceptance.** The overlay reflects session/source/suggestion state with no perceptible lag for
a live conversation (seconds, not minutes — measure, don't guess an exact number yet); dragging/
repositioning persists like the pill's geometry does; closing/hiding the overlay does not stop an
active session (parity with the existing "overlay is a view, not the owner of state" rule already
true of `OverlayWindow`).

---

## Feature 7 — Hide-from-screen-share (interview-safety requirement)

**Why this is first regardless of build order.** This is the one feature whose feasibility is
genuinely uncertain on this platform, and it's the reason the whole feature set is usable in an
interview at all — everything else is "build it," this one is "verify it, then build around
whatever's actually true."

**Primary mechanism.** `ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)`
(`WDA_EXCLUDEFROMCAPTURE = 0x00000011`), available from **Windows 10 version 2004 (build 19041)
onward**. Unlike the older `WDA_MONITOR` (which hides a window from *all* display output,
including the user's own screen — unusable here), `WDA_EXCLUDEFROMCAPTURE` keeps the window
visible locally while excluding it from capture APIs (desktop duplication/DXGI, GDI `BitBlt`,
and — per Microsoft's own documentation for this flag — modern screen-share paths). Build:
`talkflow/platform/capture_exclusion.py`:

```python
import ctypes
WDA_EXCLUDEFROMCAPTURE = 0x00000011

def exclude_from_capture(hwnd: int) -> bool:
    """Returns True if the call succeeded. False (or an unavailable API on
    pre-2004 Windows) means this window WILL show up in captures - callers
    must not assume success."""
    try:
        return bool(ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))
    except Exception:
        return False
```

Apply to the live-assist overlay's HWND right after `CreateWindowEx` in
`LiveAssistOverlayWindow.create()` (Feature 6) — the same place `OverlayWindow.create()`
(`overlay_window.py:84`) finishes its own window setup.

**This is not a guarantee — document and test it as such.** Three real risk classes, each
needing its own verification before this feature is trusted for an actual interview:

1. **OS version.** Dev/target machine's Windows build must be confirmed ≥ 19041. On an older
   build the API call is a silent no-op from this codebase's point of view (`ctypes` won't raise;
   the flag just won't take effect) — `exclude_from_capture()`'s return value must be checked and
   surfaced to the user (e.g. a one-time warning in the UI, not just a log line), not assumed.
2. **Capture-method variance.** Test against each path separately, not just "Zoom works so we're
   done": Zoom, Microsoft Teams, Google Meet (all three likely use different internal capture
   APIs — some DXGI desktop duplication, some legacy GDI), OBS (a common target for "record my
   screen" rather than live-share, and a different code path again), and Windows' own
   `Snipping Tool`/`Win+Shift+S` (to confirm the exclusion also holds against a manual
   screenshot, which an interviewer could take independently of any screen-share tool).
3. **Deliberate defeat.** Some interview-proctoring/anti-cheat tools are specifically built to
   detect or defeat capture-exclusion tricks (this is an active cat-and-mouse space, not a
   solved one) — this spec makes **no claim** this feature defeats such tools, and the product's
   honest framing to the user must say so.

**Required fallback: a manual panic-hide hotkey**, independent of the API above — a global
hotkey (Feature 10's hotkey set) that calls `win32gui.ShowWindow(hwnd, win32con.SW_HIDE)` on
every Live Assist window instantly, so the user has a guaranteed manual escape hatch the moment
they *see* their own share going wrong, rather than trusting the API silently. Un-hide is a
second explicit action (same hotkey again, or from the tray) — never auto-reappear on a timer,
since that could re-expose the overlay mid-share without the user noticing.

**Explicitly out of scope for now, recorded as an open question, not silently solved:** hiding
this app from a process list a proctoring tool might enumerate (task name/window title spoofing).
That is a materially different and more invasive mechanism than window capture exclusion and
should get its own explicit decision later, not be bundled into this feature's scope by default.

**Acceptance.** On a confirmed-≥19041 machine, the live-assist overlay does not appear in: a
Teams screen-share, a Zoom screen-share, a Google Meet screen-share, an OBS capture, and a
manual `Win+Shift+S` snip — each tested and recorded individually, not inferred from one passing.
The panic-hide hotkey works regardless of whether the API above succeeded, and un-hides only on
a second explicit action.

---

## Feature 8 — Session history for Live Assist (the Notetaker replacement)

**Why.** `talkflow/history/repository.py` (`list_recent`, `save_session`) and the main window's
History tab are built around `dictation_sessions`' one-row-per-dictation shape. Notetaker
correctly keeps `notetaker_sessions` as its own separate table/tab rather than overloading
dictation history. This feature's history is built the same separate way — but, per the
README's replacement note, it is meant to **become the one meeting/call history surface**,
retiring Notetaker's tab and table once it covers the same ground (Feature 12 below), not stay
as a third permanent history surface next to both Dictation and Notetaker.

**Build.** A `talkflow/features/liveassist/history.py` module (`list_sessions`, `get_session`
joining `live_assist_sessions` + its `live_assist_utterances`/`live_assist_suggestions`,
`delete_session` cascading all three child tables in one transaction, `search` over utterance
text) plus `Api` methods (`list_live_assist_sessions`, `get_live_assist_session`,
`delete_live_assist_session`, `search_live_assist_history`) and a new "Live Assist" tab in the
main window sidebar. Add it next to Notetaker for now (so nothing already working breaks); it
replaces Notetaker's tab in Feature 12/LA-15, not before.

**Acceptance.** A session's transcript and suggestions are browsable and searchable after the
app restarts; deleting a session removes its utterances and suggestions in the same transaction
(no orphaned rows); a suggestion is never rendered as if it were something a participant said
(keep rendering the two tables as visually distinct lists, matching the "never collapse
generated and recorded content" rule already stated for Feature 3).

---

## Feature 9 — Auto-trigger controls

**Why.** Feature 3 ships manual-only on purpose. Before any automatic triggering exists, it needs
explicit per-session opt-in, a minimum interval, and coalescing — otherwise a chatty "THEY" side
could fire suggestion requests (and paid API calls, for a cloud `live_suggestion` model) far
faster than useful.

**Build.** A per-session boolean on `LiveAssistSession` (`auto_suggest_enabled`, default `False`,
set from the overlay, scoped to that session only — never a global default) plus scheduling logic
in `LiveAssistOrchestrator`: trigger only on a finalized "they" utterance, enforce a minimum delay
after that utterance finalizes and a minimum visibility time since the previous suggestion was
shown, coalesce to at most one pending automatic job (a second eligible trigger while one is
in-flight replaces the pending one, it does not queue a second). Suspend entirely while paused,
on source failure, or while the configured `live_suggestion` route is in an auth/quota-error
state — suspend new paid work for that one affected route until the user resolves it, not the
whole feature.

**Acceptance.** With auto-suggest on, a single completed "they" turn produces exactly one
suggestion request, respecting the minimum interval; toggling auto-suggest off mid-session stops
new automatic requests immediately without affecting a request already in flight; a provider
auth/quota failure suspends further automatic requests for that stage until the user fixes it,
with a visible state in the overlay.

---

## Feature 10 — Hotkeys and shortcut integration

**Why.** Four `HotkeyController` instances already run concurrently in `app.py::main()` (dictate,
Polish, Prompt Engineer, Command Mode — see the grep of `HotkeyController(` calls in `app.py`).
Adding four more needs the same no-admin, user-mode `pynput`-hook approach already proven, and
must not collide with what's already bound.

**Build.** New `Settings` fields (append, don't rename existing ones):

```python
live_assist_toggle_hotkey: str = "<ctrl>+<shift>+l"   # start/stop a live-assist session
live_assist_ask_hotkey: str = "<ctrl>+<shift>+a"      # manual suggestion request
live_assist_capture_hotkey: str = "<ctrl>+<shift>+k"  # screen capture + question
live_assist_panic_hide_hotkey: str = "<ctrl>+<shift>+h"  # instant hide, independent of session state
```

Chosen to avoid the existing bindings (`<caps_lock>`, `ctrl+shift+p/e/m`) and the Chrome DevTools
letters (`c`/`i`/`j`) `Settings`'s own docstring already warns off. Each gets its own
`HotkeyController(mode="toggle", ...)` instance wired in `app.py::main()` next to the existing
four, following the exact same construction pattern (see `app.py`'s `hotkey =
HotkeyController(...)` block). The panic-hide hotkey is intentionally **not** routed through the
orchestrator at all — it calls the overlay's hide method directly, so it keeps working even if
the orchestrator/session state is in some broken state. The main window's existing
"press a key to set a hotkey" capture UI (every hotkey field uses a "Change"/"Set hotkey" button
that arms a `keydown` listener) is extended to these four new fields for consistency, rather than
inventing a different configuration UX for this feature.

**Acceptance.** All four new hotkeys fire reliably with no interference between them or with the
four existing ones (test all eight bound simultaneously); a user who rebinds one to collide with
an existing binding gets an explicit conflict message, not silent partial failure.

---

## Feature 11 — Privacy/retention rules specific to this feature

**Why.** This app's existing privacy posture (`CLAUDE.md`: "don't send audio to the cleanup LLM
by default", "surface which stages send audio vs. text", `Settings.retain_audio: bool = False`
default) was designed for solo dictation, where the only person's data at stake is the user's
own. A live-assist session routinely includes a second person's speech (an interviewer, in the
stated use case) who has no settings UI of their own and may not know the tool is running — the
stakes and the disclosure requirement are higher here, not just "reuse the same toggle."

**Build.**

- No raw audio for either source is retained beyond what live processing needs — same
  `Settings.retain_audio` default (`False`) extended to cover both live-assist capture streams,
  not just dictation's.
- Screenshots (Feature 5) are **never** written to disk or to `live_assist_captures` by default;
  only the question text and the answer text are optionally saved, with no image bytes. A new
  table, deliberately imageless by default:

  ```sql
  CREATE TABLE IF NOT EXISTS live_assist_captures (
      id TEXT PRIMARY KEY,
      session_id TEXT NOT NULL REFERENCES live_assist_sessions(id),
      created_at TEXT NOT NULL,
      question TEXT,
      answer TEXT
  );
  ```

- `live_suggestion` and `screen_answer` are routes that can reach a cloud provider during a call
  someone else is part of. The onboarding/settings copy for these two stages must say so plainly
  (which stage sends what, to which configured provider) before a session with auto-suggest or
  screen capture can be enabled — reuse the existing per-stage "which stages send audio vs. text"
  disclosure principle this app already states for dictation, written out concretely for these
  two new stages rather than assumed to carry over silently.
- The panic-hide hotkey (Feature 7/10) must be reachable and documented as part of this feature's
  own onboarding, not buried in general shortcuts settings — it is a safety control for this
  specific use case, not a generic convenience shortcut.

**Acceptance.** A fresh install with default settings, run through a full live-assist session
including one screen capture, leaves no raw audio and no screenshot image on disk afterward,
confirmed by inspecting `app_data_dir()` (`~/.talkflow`) directly; the disclosure copy for
`live_suggestion`/`screen_answer` is shown before first use of each; the panic-hide hotkey is
discoverable without reading source code.

---

## Feature 12 — Retire the legacy Notetaker feature

**Why.** Per the README's replacement note: Notetaker's record-then-summarize design is strictly
less capable than Live Assist's live, two-source, session-based history once that history exists
(Feature 8). Keeping both permanently means two meeting-recording entry points, two mental
models, and two places history can live — confusing, and unnecessary maintenance. This feature
is the explicit cleanup step, done once Live Assist has proven itself, not a simultaneous
rip-and-replace.

**Build.** Once Feature 8 is in place and has been used for real sessions successfully:

- Remove the tray's "Start/Stop Meeting Recording" entry and its `on_toggle_meeting`/
  `meeting_active` wiring in `app.py`, replaced entirely by the Live Assist tray entry/hotkey.
- Remove `talkflow/features/notetaker/capture.py` and `pipeline.py`, and the Notetaker tab from
  `mainwindow/web/index.html` and its backing `Api` methods (`list_meetings`, `delete_meeting`).
- Decide, explicitly and separately (do not silently drop user data): migrate existing
  `notetaker_sessions` rows into the new `live_assist_*` tables where a reasonable mapping exists
  (likely: one `live_assist_sessions` row with the old summary as a single `live_assist_suggestions`
  row of trigger `"notetaker_migration"`, and the old transcript split into `live_assist_utterances`
  rows tagged to a single synthetic source since Notetaker never separated ME/THEY), or leave the
  old table in place read-only and only remove the UI that writes to it. Record whichever choice
  is made as its own decision, not an implicit side effect of this story.

**Acceptance.** No tray entry, hotkey, UI tab, or `Api` method references the old Notetaker
capture/pipeline code after this lands; existing `notetaker_sessions` data is either migrated (and
the migration verified against a real pre-existing database) or explicitly preserved read-only,
per whichever decision was recorded; no regression to Live Assist's own session history from this
cleanup.

---

## Open questions carried over from the overview (still unresolved — do not guess answers here)

- Whether `MultiStreamAsrAdapter` (Feature 2) should be the production path immediately or start
  as two full adapter instances first (simpler, more memory) with the shared-recognizer
  optimization as a fast-follow — decide after a quick memory/startup-time measurement, not by
  assumption.
- Whether `live_suggestion`'s default model should be local (consistent with this app's
  "local-first" default elsewhere) or cloud (likely better suggestion quality) — needs the same
  kind of live comparison `CLAUDE.md`'s "Cleanup model comparison" section already did for the
  cleanup stage, not a guess.
- The exact Windows build number present on the actual interview-use machine, confirmed before
  Feature 7 is relied upon for anything real.
- The exact migrate-vs-preserve decision for existing `notetaker_sessions` data (Feature 12).
