# Live Assist — roadmap and stories

Status: proposed · 2026-11-04. Prefix **LA** (Live Assist) identifies this track's stories.
Full technical detail for every story below lives in
[live-assist-spec.md](live-assist-spec.md); this document is ordering, dependencies and
acceptance, not the how.

Points are relative effort (1/2/3/5/8), not days. All items start **Ready for refinement**.
"Small-model-buildable" means the spec section it links to names concrete files, classes and
method signatures — if a story's linked section doesn't yet give you that, stop and fill in the
spec before writing code against it.

**This replaces the existing Notetaker/meeting feature** (see [README.md](README.md)). Notetaker
keeps working unbroken until LA-15, the explicit retirement story, lands — it is not removed as
a side effect of any earlier story.

## Milestones

| Milestone | Stories | Exit gate |
| --- | --- | --- |
| **LA-M0 — stealth spike + foundation** | LA-01, LA-02 | Capture-exclusion behavior is measured (not assumed) on the real target machine/OS build, across the real capture-method matrix; new DB tables exist and `init_db()` runs clean against a fresh and an existing `talkflow.db`. |
| **LA-M1 — two-source live capture** | LA-03, LA-04, LA-05 | A live-assist session starts/pauses/resumes/stops correctly; known mic and known system-output speech each produce correctly-labelled, separately-finalized transcript text. |
| **LA-M2 — assist surface** | LA-06, LA-07, LA-08 | The live-assist overlay is visible locally, excluded from the tested capture methods, draggable, and driven by real session/transcript state; all four new hotkeys work without colliding with the existing four. |
| **LA-M3 — suggestions and screen** | LA-09, LA-10, LA-11 | A manual ask against a real two-source session produces a grounded suggestion in the configured language; a screen capture + question produces a grounded answer; neither leaks into the overlay's own excluded capture. |
| **LA-M4 — harden and migrate** | LA-12, LA-13, LA-14, LA-15 | History is browsable/searchable/deletable; auto-suggest is opt-in, coalesced, and suspends correctly on failure; a full session leaves no raw audio/screenshot on disk by default and all disclosure copy is in place; the legacy Notetaker feature is explicitly retired (or its data preserved) per a recorded decision, not silently dropped. |

Milestone order follows risk first: LA-01 (stealth) is first regardless of what's "easiest,"
because it's the one story whose outcome could force a rethink of Feature 6/7's design — better
to find that out before building the overlay around an assumption. LA-15 (retire Notetaker) is
deliberately last, after Live Assist's own history has been proven with real sessions, not a
simultaneous swap.

## Ordered inventory

| Rank | ID | Item | Milestone | Points | Depends on | Spec section |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | [LA-01](#la-01) | Hide-from-capture spike | LA-M0 | 5 | — | Feature 7 |
| 2 | [LA-02](#la-02) | Live-assist data schema | LA-M0 | 3 | — | Features 1,2,3,4,11 (schema parts) |
| 3 | [LA-03](#la-03) | Session lifecycle + orchestrator skeleton | LA-M1 | 5 | LA-02 | Feature 1 |
| 4 | [LA-04](#la-04) | Two-source capture (no mixing) | LA-M1 | 5 | LA-03 | Feature 2 |
| 5 | [LA-05](#la-05) | Multi-stream live ASR | LA-M1 | 8 | LA-04 | Feature 2 |
| 6 | [LA-06](#la-06) | Live-assist overlay window | LA-M2 | 8 | LA-01, LA-03 | Features 6,7 |
| 7 | [LA-07](#la-07) | Hotkeys for live-assist | LA-M2 | 2 | LA-06 | Feature 10 |
| 8 | [LA-08](#la-08) | Assistant/persona config | LA-M2 | 3 | LA-02 | Feature 4 |
| 9 | [LA-09](#la-09) | Live suggestion engine | LA-M3 | 8 | LA-05, LA-08 | Feature 3 |
| 10 | [LA-10](#la-10) | Screen capture + question | LA-M3 | 5 | LA-06 | Feature 5 |
| 11 | [LA-11](#la-11) | Vision answer pipeline | LA-M3 | 5 | LA-10 | Feature 5 |
| 12 | [LA-12](#la-12) | Live-assist history | LA-M4 | 5 | LA-05, LA-09, LA-11 | Feature 8 |
| 13 | [LA-13](#la-13) | Auto-trigger controls | LA-M4 | 5 | LA-09 | Feature 9 |
| 14 | [LA-14](#la-14) | Privacy/retention pass | LA-M4 | 3 | LA-06, LA-09, LA-11 | Feature 11 |
| 15 | [LA-15](#la-15) | Retire legacy Notetaker feature | LA-M4 | 5 | LA-12 | Feature 12 |

## Story cards

Each card's acceptance is the authoritative one from the matching spec section; this card only
restates it compactly plus dependency/definition-of-ready notes specific to sequencing.

### LA-01

**Hide-from-capture spike** · LA-M0 · 5 points

As the user, I need to know — not assume — whether the overlay can stay invisible on a real
screen-share before any UI is built around that assumption.

Dependencies: none. Do this first regardless of what else looks easier.

Acceptance: `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` tested against a throwaway test
window (not yet the real overlay) across Teams screen-share, Zoom screen-share, Google Meet
screen-share, OBS capture, and `Win+Shift+S`, each result recorded individually with the
Windows build number of the test machine; a manual panic-hide (`ShowWindow`/`SW_HIDE`) fallback
is proven working independent of the affinity API's result.

### LA-02

**Live-assist data schema** · LA-M0 · 3 points

As an implementer, I have the tables this whole feature set writes to, migrated cleanly into
the existing single-sqlite-file design.

Dependencies: none.

Acceptance: `live_assist_sessions`, `live_assist_utterances`, `live_assist_suggestions`,
`live_assist_assistants`, `live_assist_captures` created via `talkflow/history/db.py`'s existing
`SCHEMA`/`init_db()` pattern; one builtin "General Assistant" row seeded on first run, following
the exact seeding guard already used for `_BUILTIN_PRESETS`/`_BUILTIN_SNIPPETS` (only seed when
the table is empty, not on every init); running `init_db()` against both a brand-new and an
already-existing `talkflow.db` leaves the database in the same end state; `notetaker_sessions`
and every other existing table are untouched.

### LA-03

**Session lifecycle + orchestrator skeleton** · LA-M1 · 5 points

As the user, my live-assist session's start/pause/resume/stop is correct even before any ASR or
suggestion logic exists behind it — proven first with fake/no-op capture.

Dependencies: LA-02.

Acceptance: a second, temporary tray entry ("Start/Stop Live Assist") starts/stops a session
alongside the still-working "Start/Stop Meeting Recording" entry; `LiveAssistOrchestrator`
reaches `ACTIVE` only once required capture is armed, `stop()` is idempotent,
`live_assist_sessions` rows persist correctly through all exit paths.

### LA-04

**Two-source capture (no mixing)** · LA-M1 · 5 points

As the ASR/transcript layer, I receive two genuinely separate PCM streams, not one mixed
mono signal — the one point where copying Notetaker's existing capture code verbatim would be
wrong (see spec Feature 2's note on `MeetingRecorder`'s summing).

Dependencies: LA-03.

Acceptance: `TwoSourceRecorder` delivers labelled frames from mic and loopback independently;
a test recording with known distinct content on each source shows no cross-source bleed in the
raw captured PCM.

### LA-05

**Multi-stream live ASR** · LA-M1 · 8 points

As the user, both sources are transcribed concurrently with interim text that gets replaced by
one committed final line per utterance per source.

Dependencies: LA-04.

Acceptance: `MultiStreamAsrAdapter` serves two named sessions off one loaded recognizer (memory/
startup-time measured, not assumed — see spec's open question); known mic and known system-
output speech each produce correct, separately-labelled, separately-finalized transcript rows in
`live_assist_utterances`.

### LA-06

**Live-assist overlay window** · LA-M2 · 8 points

As the user, I can see session state, both transcript feeds, and the current suggestion without
that surface being capturable on a shared screen.

Dependencies: LA-01 (apply its proven mechanism, don't re-derive it), LA-03 (something real to
display).

Acceptance: native layered window parity with the existing pill's rendering technique;
`exclude_from_capture()` applied at creation and verified against the same matrix LA-01
established; draggable and persists position; reflects live session/transcript/suggestion state.

### LA-07

**Hotkeys for live-assist** · LA-M2 · 2 points

As the user, I can control a session without touching the mouse, without breaking any of the
four hotkeys this app already has.

Dependencies: LA-06.

Acceptance: four new bindings (toggle, ask, capture, panic-hide) all fire correctly with the
existing four bound simultaneously; rebinding to a colliding combo surfaces an explicit conflict,
not silent failure.

### LA-08

**Assistant/persona config** · LA-M2 · 3 points

As the user, I can define who this assistant is and what it's helping with, independent of any
focused application.

Dependencies: LA-02.

Acceptance: CRUD works from a new main-window "Assistants" section; a session's selected
assistant's task/style reaches the suggestion engine without a restart.

### LA-09

**Live suggestion engine** · LA-M3 · 8 points

As the user, I can ask for a drafted response grounded in the actual two-source conversation so
far.

Dependencies: LA-05 (needs real transcript text), LA-08 (needs a persona to apply).

Acceptance: `LlmAdapter.suggest()` implemented and registered under a new `"live_suggestion"`
registry stage; manual ask returns a grounded, language-correct suggestion or an explicit
no-suggestion result; a second manual ask supersedes a still-streaming first one.

### LA-10

**Screen capture + question** · LA-M3 · 5 points

As the user, I can show the assistant what's on a chosen monitor, with a preview before anything
is sent anywhere.

Dependencies: LA-06 (needs the overlay to drive it from, and needs Feature 7 already proven so
the overlay itself never leaks into the capture).

Acceptance: monitor picker + `PIL.ImageGrab`-based preview; cancel discards in-memory bytes with
no disk write; the live-assist overlay never appears inside its own captured image; a failed/
unplugged monitor produces an explicit retry state.

### LA-11

**Vision answer pipeline** · LA-M3 · 5 points

As the user, a captured screenshot plus an optional question gets a grounded answer from a
vision-capable model, configured independently from every other stage.

Dependencies: LA-10.

Acceptance: new `"screen_answer"` registry stage; an answer correctly reflects the captured
image's actual content on a known test screenshot; a non-vision-capable configured model is
rejected with an explicit "choose a capable model" message, never silently sent anyway.

### LA-12

**Live-assist history** · LA-M4 · 5 points

As a returning user, I can find and review a past session's transcript, suggestions, and screen
Q&A locally — this becomes the one meeting-history surface, not a third one next to Dictation
History and Notetaker.

Dependencies: LA-05, LA-09, LA-11 (there has to be real content worth browsing first).

Acceptance: new "Live Assist" history tab; search works over utterance text; delete cascades all
child rows in one transaction; suggestions are never rendered as if they were spoken content.

### LA-13

**Auto-trigger controls** · LA-M4 · 5 points

As the user, I can opt a specific session into automatic completed-turn suggestions without
risking runaway requests or cost.

Dependencies: LA-09.

Acceptance: per-session opt-in, off by default; one coalesced pending automatic job at a time;
minimum interval and minimum previous-suggestion visibility enforced; suspends on pause, source
failure, or a provider auth/quota error for the `live_suggestion` route, with that suspended
state visible in the overlay.

### LA-14

**Privacy/retention pass** · LA-M4 · 3 points

As the user — and as the other person in the conversation, who has no settings UI of their own —
I need this feature's defaults to actually hold up, not just be documented as intentions.

Dependencies: LA-06, LA-09, LA-11 (everything that could leave a trace must exist first to audit
it).

Acceptance: a full session including one screen capture leaves no raw audio and no screenshot
image under `~/.talkflow` afterward, confirmed by direct inspection; disclosure copy for
`live_suggestion`/`screen_answer` shown before first use of each; panic-hide is documented as
part of this feature's own onboarding, not buried in general settings.

### LA-15

**Retire legacy Notetaker feature** · LA-M4 · 5 points

As the maintainer, I don't want two permanent meeting-recording features with two mental models
and two history surfaces once Live Assist genuinely covers Notetaker's ground.

Dependencies: LA-12 (Live Assist history has to exist and be proven with real sessions first —
this is a deliberate last step, not a simultaneous swap with any earlier story).

Acceptance: tray entry, hotkey wiring, `capture.py`/`pipeline.py`, main-window tab and `Api`
methods for the old Notetaker feature are removed; existing `notetaker_sessions` data is either
migrated into `live_assist_*` tables (verified against a real pre-existing database) or
explicitly preserved read-only — whichever is chosen is recorded as its own decision, not an
implicit side effect; no regression to Live Assist's own history from this cleanup.

## Refinement rules

Before a story moves into implementation, confirm its dependency stories' acceptance actually
passed (not just "merged") — a dependency satisfied by a fake/stub does not count for stories
that need real audio/model behavior (LA-05, LA-09, LA-11 specifically; LA-03 is the one story
explicitly allowed to prove its lifecycle with a fake capture first). If a story turns out to
need splitting, keep its ID stable and suffix (`LA-05a`, `LA-05b`) rather than renumbering.
