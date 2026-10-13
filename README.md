# TalkFlow development brief

This folder is the starting brief for **TalkFlow**, an independently implemented desktop voice-typing product. Wispr Flow is the reference app inspected for product behavior; TalkFlow is the app being planned. These notes do not claim access to Wispr's backend or proprietary model pipeline.

**Implementation status**: there is now a working implementation under `talkflow/` (not just this brief) — see [CLAUDE.md](CLAUDE.md)'s "Project state" section for how to run it, what's been verified, and what's still untested pending the developer's own hands-on check.

## TalkFlow requirements

- Product name: **TalkFlow**.
- Support multilingual speech with automatic source-language detection.
- Let the user assign/configure AI providers and model IDs by pipeline stage.
- Candidate pipeline: Whisper-family speech recognition/translation, followed by an OpenAI-compatible LLM stage to clean up and correct recognized text. The LLM stage processes text; it does not directly repair the audio waveform.
- Make destination language explicit. OpenAI's documented Whisper audio-translation endpoint returns English. For other target languages, use source-language transcription followed by a configurable text translation/cleanup model unless another selected provider supports that audio translation pair.

## Documents

- [System and product brief](SYSTEM_SPEC.md) — supported user journeys, feature inventory, candidate architecture, and MVP boundary.
- [Evidence ledger](EVIDENCE_LEDGER.md) — local package evidence, passive runtime trace, official references, confidence labels, and limits.
- [Open questions and build plan](OPEN_QUESTIONS.md) — the remaining discovery work and a practical sequence for implementation.

## Target inspected

- Windows installation: `%USERPROFILE%\AppData\Local\WisprFlow\app-1.6.1102`
- Main executable: `Wispr Flow.exe`
- Main application bundle: `resources\app.asar` (133,651,466 bytes at inspection)
- Helper executable: `resources\Release\Wispr Flow Helper.exe`
- This is an Electron-style packaged application: Chromium/Electron runtime files, ASAR bundle, unpacked resources, and Squirrel updater files are present.
- Inspection date: 2026-10-11. Treat the version and all observations as a snapshot, not a statement about later releases.

## Evidence labels

- **Observed in package** means a fact read directly from files shipped with this installed build.
- **Observed at runtime** means metadata measured during a user-directed local run. No audio, transcript, clipboard text, or packet payload was collected.
- **Official product claim** means Wispr's current website or help center describes it; it does not prove that this exact build implements the claim in the same way.
- **Inference** means a plausible design conclusion from the evidence, explicitly not source-level proof.
- **Unknown** means not established yet.

## How to use this brief

Start with a small, independent Windows MVP: global push-to-talk, capture microphone audio, submit to a replaceable transcription/formatting service, and insert the completed result into the previously focused text field with a recoverable clipboard path. Keep capture, transcription, formatting, insertion, and UI as separate components so an offline ASR engine or another backend can be substituted later. Add snippets, personal dictionary, styles, history, and command mode as separate features after the basic path is dependable.

The app's ASAR contents were not fully decompiled in this investigation. The local migration scripts and binary inventory are useful evidence, but they are not a complete implementation specification. See the evidence ledger before treating any behavior as certain.
