# TalkFlow system and product brief

Product being designed: **TalkFlow**. Reference snapshot: Wispr Flow for Windows 1.6.1102, inspected 2026-10-11. Scope: desktop dictation and related product features; mobile behavior is included only where the reference product documentation says it is shared.

## Product goal

Turn speech into polished text in the text field the user is working in. TalkFlow must detect spoken language automatically when the user selects Auto, support multilingual dictation, and allow an explicit target language for translation. Desktop dictation is described by Wispr as a capture-then-process flow: start recording, speak, stop, transcribe/format, then insert the completed result. Reference help says desktop does not show a live word-by-word transcript while the person is speaking.

## Model and language requirements

- **Automatic language detection:** default to `Auto`; detect the input language from the audio for each dictation and record detected language/confidence when the provider returns it. Let users pin an input language when auto-detection is undesirable.
- **Separate transcription from translation:** normal dictation preserves the spoken language. Translation is an explicit mode or setting with a visible target language; never silently translate during ordinary dictation.
- **Model assignment:** configure providers and model IDs independently for speech recognition, speech translation, text translation, transcript cleanup, and optional command interpretation. Allow a saved pipeline profile to assign a model to multiple stages.
- **Whisper candidate:** use a Whisper-compatible speech endpoint as the initial ASR candidate. OpenAI currently documents `/v1/audio/transcriptions` for source-language transcription and `/v1/audio/translations` for translation to English; its documented translation endpoint currently lists only `whisper-1`. Do not assume every OpenAI-compatible server implements either audio endpoint.
- **LLM cleanup:** send recognized text plus language, target language (if translating), dictionary/snippet/style rules, and explicit instructions to a configurable OpenAI-compatible text-model endpoint. The LLM corrects recognition/formatting errors in text; it does not repair the audio waveform. Preserve raw ASR output alongside cleaned output.
- **OpenAI-compatible configuration:** support base URL, model ID, endpoint mode, secret API key storage, timeout, streaming/retry settings, and optional provider-specific fields. Treat chat-completions-style text generation as a compatibility adapter; audio transcription/translation needs a separate provider adapter because compatibility does not guarantee those routes.
- **Selection and fallback:** allow a model per stage, a default profile, and optional fallback models. Show which profile ran. Keep model inventories refreshable/user-editable rather than embedding a permanent model list.
- **Privacy and cost:** show which stages send audio versus text, provider/model, and usage where available. Do not send audio to the cleanup LLM by default. Allow local-only providers if configured.

## Primary user journey

1. User focuses an editable field in another application.
2. User starts dictation using the configured push-to-talk shortcut, hands-free shortcut, or supported Flow UI control.
3. App captures microphone audio and shows a recording state.
4. User releases/stops. App submits audio for transcription and formatting; progress/error/recovery UI may appear.
5. App inserts the final formatted text into the text field focused at insertion time. If insertion fails, expose a copy/paste recovery path and preserve a recoverable transcript/history entry when storage settings allow.
6. User reviews the output. The app should not silently submit or send the user's message to the destination app.

### Candidate lifecycle model (inference for an independent implementation)

`idle → arming → recording → stopping → transcribing → formatting → inserting → complete`

Failure branches should distinguish permission/device failure, no audio, network/ASR failure, formatting failure, no eligible target field, paste failure, cancellation, and saved/retryable session. The precise Wispr internal state machine is unknown.

## Feature inventory

The “official” column refers to current Wispr site/help-center descriptions. Availability, account plan, OS, organization policy, and app version may change access.

| Capability | Product behavior described publicly | Evidence/implementation status |
|---|---|---|
| Push-to-talk dictation | Hold shortcut, speak, release; Windows default is Ctrl+Win. | Official help; live trace saw mic-open interval after user activation. Exact hotkey handling is not recovered from code. |
| Hands-free dictation | Toggle recording with a shortcut; Windows default Ctrl+Win+Space. | Official help. Exact debounce/cancel semantics should be confirmed from UI/runtime before matching. |
| Global insertion | Works across apps/text fields; target is the focused field at insertion time. | Official help. Live trace showed clipboard sequence changes only after mic stop; this is consistent with clipboard insertion but does not prove exact text or causation. |
| Transcription and language detection | Speech is converted after stopping; TalkFlow must auto-detect source language and allow an explicit language override. | TalkFlow requirement. Wispr official help says its transcription needs internet; reference ASR provider/protocol/model remain hidden. |
| Translation | Optional, explicit source-to-target translation; ordinary dictation preserves spoken language. | TalkFlow requirement. OpenAI Whisper audio translation currently targets English; other targets need a text LLM translation stage or a provider supporting that pair. |
| Assignable AI models | Select provider/model independently for ASR, translation, cleanup, and command interpretation; support OpenAI-compatible text API plus provider-specific audio adapters. | TalkFlow requirement and proposed architecture; not a claim about Wispr. |
| Formatting/refinement | Filler removal, punctuation, lists, spoken corrections/backtracking, paragraphs. | Official site/help. Exact prompts/models and edge cases are unknown. |
| Languages | Official site/help claims more than 100 languages. | Product claim, not validated exhaustively on this build. |
| Context and names | Site says app/context helps uncommon names and developer jargon. | Product claim. Which context is captured, when, and how it reaches processing are unknown. |
| Personal dictionary | Custom/corrected terms; local package models Dictionary and `isSnippet`. | Schema supports dictionary/snippet concepts. UI validation, sync protocol, and ranking are not inspected. |
| Snippets | Spoken trigger expands to saved formatted text; personal and shared/team libraries described. | Official help/site; migration adds `Dictionary.isSnippet`. Trigger matching, conflict rules, rich-text transfer and sync need deeper inspection. |
| Styles | Output tone varies by app/category; official docs describe Formal, Casual, very casual, and Excited styles, with limits by language/platform/category. | Official help. Exact app classification and prompts are not known. |
| Command Mode | Voice instructions can edit text, draft a reply, or search; desktop uses separate shortcut and requires paid access per official help. | Official help; shipped History enum includes `command`. Exact intent model, command set, and safety checks are unknown. |
| Flow Bar / hub | Desktop UI provides hub/pages, settings and task state; current public docs mention History, Dictionary, Snippets, Style and Settings. | Official help. Screens and interactions for this exact build are not comprehensively recorded. |
| History/recovery | History can hold transcript, audio, status and context; retry/recovery depend on settings/retention. | Local migration schema proves fields existed in shipped migration history; current data retention/configuration was not read. |
| Polish / transform | App has a separate polish history schema with initial/polished text, instruction, status, timings and undo marker. | Package migration proves a polish workflow is modeled. UI invocation and algorithm not fully inspected. |
| Developer workflows | Site advertises syntax awareness, file tagging in Cursor/Windsurf, CLI/code formatting and jargon. | Official product claims only; no feature-level runtime confirmation in this trace. |
| Shared team tools | Site advertises shared dictionary/snippets and usage dashboards. | Official product claims. Server, admin, entitlement, and sync behavior unknown. |
| Scratchpad | Official docs describe a desktop tabbed rich-text note editor with dictation, notes, images and history. | Current documentation evidence, not verified against this specific installed build. |
| Integrations / other features | Public help/site pages also describe clipboard recovery, keyboard shortcuts, notifications, app-aware paste paths, and organization controls. | Documentation-backed requirements; implementation details vary by platform and release. |

## Candidate architecture for a new app (design recommendation, not a Wispr source claim)

- **Desktop shell**: tray/menu, updater, settings window, global shortcuts, notification/status surface.
- **Capture service**: device selection, permission state, audio stream, stop/cancel, silence/length limits, temporary recording storage.
- **Model registry and pipeline config**: per-stage provider, model ID, endpoint capability, credentials reference, timeout and fallback. Do not assume text API compatibility implies audio API compatibility.
- **Transcription adapter**: `transcribe(audio, language: auto|code, context) → {text, detectedLanguage, confidence?}`; keep provider secrets and network retries outside UI.
- **Translation adapter**: `translateAudio` or `translateText` with explicit source and target language; check provider capability before offering it for a stage.
- **Formatting pipeline**: send ASR text, detected language, target language, cleanup rules, style, dictionary and snippet candidates to the assigned text LLM. Return cleaned text plus language metadata; retain raw ASR separately.
- **Target context provider**: active app/site and editable-field context if permission permits. Collect the minimum context required; make this opt-in and visible.
- **Insertion adapter**: preferred platform accessibility/text API when reliable, clipboard paste fallback, restore clipboard, and explicit failure recovery.
- **Persistence/sync**: local history and settings, retention controls, encrypted/authenticated server sync only if needed. Keep audio retention separate from transcript retention.
- **Feature services**: dictionary, snippets, styles, command mode, polish/transform, notes; they should depend on stable capture/transcription/output interfaces rather than implement their own microphone path.

## MVP acceptance outcomes

1. A global shortcut starts/stops capture without stealing the destination field's focus.
2. The chosen microphone is used; missing/denied device yields an actionable state.
3. Stop leads to a completed transcript or a distinct, recoverable error.
4. Completed text is inserted into a common Windows text field; a visible manual-copy fallback exists.
5. Clipboard contents are not needlessly lost; restoration timing and paste fallback are designed and documented.
6. Cancel prevents insertion and removes/discards the session according to the new app's retention policy.
7. User can configure shortcuts, microphone, language, formatting preferences and whether history/audio are retained.
8. The user can tell when the mic is active, when processing is happening, and where the result went.
9. Auto language detection works for supported languages; detected language is visible and can be overridden.
10. Translation is a distinct user-selected target-language behavior; source-language dictation remains the default.
11. Users can assign and test different provider/model configurations by stage; show clear errors when a provider lacks a required audio or text capability.

These are proposed outcomes for a new application; they are not assertions that Wispr uses this architecture internally.
