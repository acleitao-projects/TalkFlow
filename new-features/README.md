# Live Assist — new features overview

Status: proposed · 2026-11-04. This folder specs out a new feature set, working name
**"Live Assist"** (module path `talkflow/features/liveassist/`), being added to this app: a
live, two-source (microphone + system-output) conversation assistant with on-demand suggestions
and screen-question capability, designed for use during a call or an interview. It reuses
TalkFlow's existing capture/ASR/LLM-registry/history/overlay infrastructure rather than being a
new app — see [live-assist-spec.md](live-assist-spec.md) for exactly which existing modules are
reused as-is, which are extended, and which are new.

## Important: this replaces the existing Notetaker/meeting feature

TalkFlow's current **Notetaker** (`talkflow/features/notetaker/`, the tray's "Start/Stop Meeting
Recording" entry, the `notetaker_sessions` table, the main window's Notetaker tab) is a
record-then-summarize meeting feature. **Live Assist's session/history capability is intended to
replace it**, not sit alongside it permanently — a live, two-source, in-call experience with its
own transcript and suggestions supersedes "record the whole meeting, summarize it afterward."

Do not build Live Assist as a second, separate meeting feature that coexists indefinitely with
Notetaker. The roadmap below builds Live Assist first (because it needs real two-source capture
and live transcription Notetaker never had) and retires the old Notetaker feature — tray entry,
`capture.py`/`pipeline.py`, `notetaker_sessions` table and schema, and the main window's
Notetaker tab — once Live Assist's session history covers the same ground (see story **LA-15**
in [roadmap.md](roadmap.md)). Until LA-15 ships, leave Notetaker working as-is; don't break it
mid-migration.

## Read in this order

1. [live-assist-spec.md](live-assist-spec.md) — detailed technical spec, one section per
   feature, each grounded in a specific file/class/line in this repository so the claims about
   "what exists today" can be checked, not just trusted.
2. [roadmap.md](roadmap.md) — ordered backlog (`LA-01`...`LA-15`), milestones, dependencies and
   acceptance criteria, pointing back to the spec section for each story's implementation detail.

## The features, in one line each

1. Live session mode (vs. this app's existing record-then-process pattern).
2. Two-source live transcript (ME/THEY, interim→final) — the one place Notetaker's existing
   capture code (`MeetingRecorder`) can't just be reused as-is, because it mixes both sources
   into one mono stream.
3. Live suggestion engine (manual ask first; auto-trigger is a separate, later feature).
4. Assistant/persona config, independent of the existing per-app formality presets.
5. Screen capture + question (no screenshot/vision code exists in this repo today).
6. A second native overlay surface, built the same way as the existing dictation pill, but with
   its own content (not a retrofit of the pill).
7. **Hide-from-screen-share** — the interview-safety requirement. `SetWindowDisplayAffinity`
   applies directly to this app's native overlay windows, but it's not a universal guarantee
   (OS-version-dependent, varies by capture method, and some proctoring tools specifically defeat
   it) — a manual panic-hide hotkey is the required fallback, not an optional nice-to-have.
8. Session history for Live Assist — the direct replacement for Notetaker's history, not an
   addition next to it (see the replacement note above).
9. Auto-trigger controls (opt-in, coalesced, suspends on failure) — ships after manual ask works.
10. New hotkeys, checked against the four this app already binds.
11. Privacy/retention rules specific to a conversation that includes someone with no settings UI
    of their own.
12. Retiring the legacy Notetaker feature once Live Assist covers its ground.

## Research grounding

Confirmed directly against this repository's source before writing the spec:
`features/notetaker/pipeline.py` transcribes only after a meeting ends, in 60s batches — there is
no live/running transcript today; `overlay/overlay_window.py` is a real native Win32 layered
window, not a browser surface, which is exactly what makes feature 7 (capture exclusion) viable;
`registry/` already does per-stage provider/model/credential config and needs new stages, not a
new config system; no screen-capture, vision, or capture-exclusion code exists anywhere in the
repo today.
