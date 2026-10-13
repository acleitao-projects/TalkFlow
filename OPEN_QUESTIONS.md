# TalkFlow open questions and build plan

## Readiness

There is enough information to start **TalkFlow**, an independent Windows dictation MVP with multilingual auto-detection and configurable AI stages. There is not enough evidence to reproduce Wispr Flow faithfully across its full feature set. Core UX is publicly described; several database concepts are visible in shipped migrations; the service protocols, source logic, exact insertion code, prompts, context policy, and many UI details remain hidden.

## Highest-value questions to resolve before polishing an implementation

### 1. Capture and lifecycle

- Which Windows audio capture API and format should the new app use? What sample rate/channel count/codec does the ASR adapter require?
- What are the exact transitions for hold-to-talk, hands-free, cancel, too-short recordings, focus loss, sleep/resume, no device, and a second press while processing?
- Which audio-retention choices are required, and how are temporary recordings securely deleted?
- What should the UI surface show in recording, transcribing, formatting and error states?

### 2. ASR, language detection, translation, and formatting

- Define supported languages, auto-detection behavior, confidence threshold, language override, mixed-language utterances, and how detected language appears in UI/history.
- Choose the initial Whisper-compatible endpoint. OpenAI documents audio transcription and a Whisper translation endpoint that returns English; decide how TalkFlow handles non-English translation targets (text LLM translation or a provider with the needed audio-translation capability).
- Define provider-neutral interfaces and capability metadata for audio transcription, audio translation, text translation, cleanup, and command interpretation. A text endpoint that is OpenAI-compatible may not implement OpenAI's audio routes.
- Decide how users assign a different provider/model to each stage, configure a default pipeline, set language-specific overrides, test endpoints, and order fallbacks.
- Decide whether transcription is offline, cloud, or hybrid; Wispr's official help says its voice transcription needs an internet connection, but its provider/protocol is unknown.
- Specify partial vs final transcript, punctuation, filler cleanup, backtracking, list formatting, custom vocabulary, and retry. Preserve raw ASR output separately from LLM-cleaned text.
- Clarify whether “fixing the audio” means correcting recognition errors in the transcript or denoising/enhancing the waveform. The proposed LLM stage operates on transcript text; waveform enhancement needs a separate audio-processing stage.
- Define exactly when formatting runs and how to preserve literal/code input.

### 3. Insertion and target context

- Decide on Windows UI Automation/accessibility insertion vs clipboard paste vs hybrid; test in ordinary edit fields, elevated apps, terminals, browsers, Office apps and remote desktop.
- Preserve the previously focused target and avoid sending text if focus moves to an unsafe/unexpected field.
- Define clipboard snapshot, supported data formats, restore timing, paste acknowledgement, and recovery when the target app reads late or insertion fails.
- Specify whether the new app reads active app/title/URL/text around the cursor and how users grant/revoke that access.

### 4. Feature-level policy

- **Dictionary:** what counts as correction/learning; matching precedence; per-user vs shared terms; import/export; merge conflicts.
- **Snippets:** trigger boundaries/case/punctuation; expansion precedence vs dictionary; rich text; matching inside longer dictation; duplicates; permissions and sync.
- **Styles:** app categorization, style definitions, supported languages, user custom styles, and whether styling occurs locally or through a model.
- **Command Mode:** supported commands and actions, whether commands can change text or invoke web search, confirmation rules, undo, shortcuts and access gating.
- **History:** local schema, audio vs text retention, search/recovery/retry, editing/undo, privacy controls and sync.
- **Polish/Transform:** selection capture, instruction UI, handling non-editable fields, undo, and insertion recovery.
- **Scratchpad/notes:** decide whether in scope; if included, specify editor format, images, versions, autosave and sync.
- **Developer integrations:** whether file tagging/context in Cursor/VS Code/Windsurf is a real requirement for v1 or a later adapter.
- **Team features:** shared dictionary/snippets and dashboards require account, admin, entitlement, sync and privacy designs; treat them as separate later work.

## Suggested implementation sequence

1. **Define platform and privacy contract.** Windows first; state whether audio leaves device, what metadata is read, what is retained, and how the user can delete it.
2. **Build a thin capture shell.** Tray app, global shortcut, mic selection/permission states, visible recording indicator, start/stop/cancel.
3. **Add a model registry and replaceable adapters.** Assign provider/model per stage. Start with an OpenAI-compatible text adapter and a separate Whisper/audio adapter; a fake adapter can support UI work.
4. **Implement multilingual ASR and explicit translation.** Auto-detect source language, allow override, preserve source-language output for ordinary dictation, and require a target language for translation. Handle endpoint capability differences (OpenAI Whisper translation currently targets English).
5. **Add LLM transcript cleanup behind its own stage.** Configure its model independently. Keep raw ASR, cleaned text, and user-edited text as distinct values.
6. **Implement insertion and recovery.** Make text insertion reliable before adding advanced language features. Treat clipboard as sensitive transient state.
7. **Add local history and user controls.** Explicit retention settings; separate transcript storage from audio storage; include retry and copy/paste recovery.
8. **Add dictionary and snippets.** Keep both backed by a clear schema and explicit precedence rules; test rich formatting and ambiguous triggers.
9. **Add styles, polish and command mode.** These rely on a solid context/formatting pipeline; command mode needs clear scope and undo behavior.
10. **Add Scratchpad, integrations, shared/team sync only if selected.** These widen product and security scope substantially.

## Proposed starter data model (design choice)

```text
DictationSession
  id, startedAt, endedAt, state, inputDeviceId, language
  audioRef? (separately governed retention)
  rawTranscript?, formattedTranscript?, insertedText?
  targetAppId?, targetFieldKind? (only if captured with user consent)
  errorCode?, processingDuration?, insertionOutcome?

DictionaryEntry
  id, trigger, replacement, scope(personal/team), createdAt, updatedAt

Snippet
  id, spokenTrigger, expansion, richText?, scope(personal/team), createdAt, updatedAt

StyleProfile
  id, category, instructionsOrPreset, language, enabled

ModelAssignment
  id, stage(asr|audio_translation|text_translation|cleanup|command)
  provider, baseUrl, modelId, endpointMode, credentialRef
  languageAllowlist?, targetLanguage?, fallbackAssignmentId?

CommandRun
  id, instruction, targetContextRef?, actionType, result, status, undoRef?
```

This is an independent starting model, not a claim about Wispr's production schema. Avoid copying any actual user's local database records into a development dataset.

For auto-detection, persist the selected mode (`auto` or fixed language), detected source language, confidence when provided, and explicit target language for translations. Never conflate detection with translation.

## Next reverse-engineering pass (if closer parity is needed)

1. Preserve the exact installed build and hashes; extract/decompile `resources/app.asar` into a separate analysis directory and inventory native modules.
2. Trace focused code paths from shortcut registration → microphone open/close → transcript request → formatting → clipboard/target insertion → history persistence.
3. Inspect migration chain and model definitions together; document actual tables/columns without reading private history rows.
4. Capture screen states for the installed build and record shortcut/settings/feature availability.
5. Run a user-directed, privacy-bounded trace with a known harmless phrase and an instrumented blank text field, recording only test phrase/output as explicitly authorized, process/file/network metadata, and timestamps needed to correlate stages.
6. Use endpoint hostnames only when collected through a lawful, privacy-preserving method; never infer payload semantics from TLS/IP timing alone.
7. Compare each runtime observation with source-level evidence and public docs. Keep evidence, inference and unknown categories separate.

## Decision points for the developer

- Windows only initially, or cross-platform from the first release?
- Which ASR, translation, and cleanup providers/models should be the initial defaults, and should any run locally?
- Which source/target language pairs are required in v1?
- Is the goal a simple push-to-talk utility or product-level breadth (history, snippets, styles, command mode, notes, teams)?
- Is any app/window context required, and what user-visible privacy controls will govern it?
- Is account sync required or can v1 remain local?

Answering these product decisions is more valuable before implementation than attempting to guess Wispr's hidden provider endpoints or copy implementation internals.
