# TalkFlow

TalkFlow is a voice-typing app for Windows. Hold a hotkey, speak, and it types cleaned-up text into whatever app you were using. Speech recognition runs locally on your machine. The text cleanup step uses an LLM, which can be local (Ollama) or any OpenAI-compatible cloud API.

It is a personal, independently written project. Its behavior is modeled on [Wispr Flow](https://wisprflow.ai), but it shares no code with it and is not affiliated with it.

## Features

- **Dictation**: speak into any focused text field. Filler words, stutters and self-corrections are removed before the text is pasted.
- **Local, streaming speech recognition**: NVIDIA's Nemotron 3.5 streaming ASR model runs in-process through [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx). No audio leaves your computer.
- **Multiple languages**: choose the languages you speak (for example English and Brazilian Portuguese), and each dictation is detected and cleaned in its own language. It is never translated.
- **Per-app style**: Friendly, Normal or Formal register (or your own presets), chosen by which app has focus.
- **Dictionary**: names and terms the cleanup step should get right. It can learn from your edits in History.
- **Snippets**: spoken shortcuts that expand into longer text ("btw" becomes "by the way"; add your own).
- **Transforms**: rewrite the selected text with one hotkey (Polish, Prompt Engineer, or up to 8 of your own).
- **Command Mode**: speak an instruction to edit the selected text or search the web.
- **Notetaker**: record a meeting (microphone and system audio) and get a transcript with a summary and action items.
- **History**, a floating **Scratchpad**, and a **Model Registry** where each pipeline stage (speech recognition, cleanup) gets its own provider, model and API key.

## Requirements

- Windows 10 or 11. Admin rights are not needed: everything installs per user.
- Python 3.12.
- About 1 GB of disk space for the speech model.
- For local cleanup: [Ollama](https://ollama.com), either the Windows app or the Docker container in this repo. An NVIDIA GPU helps but is not required.
- Or for cloud cleanup: an API key for any OpenAI-compatible chat API (Ollama Cloud, OpenAI, OpenRouter, ...).

## Installation

### 1. Get the code and install the Python packages

```powershell
git clone https://github.com/acleitao-projects/TalkFlow.git
cd TalkFlow
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`requirements-lock.txt` holds the exact versions this was tested with, if you want a reproducible install.

### 2. Download the speech recognition model

TalkFlow expects the Nemotron 3.5 streaming model (int8, 160 ms chunks) from the sherpa-onnx releases, extracted under `models\sherpa-nemotron\`:

```powershell
mkdir models\sherpa-nemotron
curl.exe -L -o models\sherpa-nemotron\model.tar.bz2 https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-160ms-int8-2026-06-11.tar.bz2
tar -xjf models\sherpa-nemotron\model.tar.bz2 -C models\sherpa-nemotron
del models\sherpa-nemotron\model.tar.bz2
```

You should end up with `models\sherpa-nemotron\sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-160ms-int8-2026-06-11\encoder.int8.onnx` (plus `decoder`, `joiner` and `tokens.txt`). If you use a different export, point the speech recognition stage's model path at it in the Model Registry tab.

### 3. Set up the cleanup LLM

Choose one of the options below. You can switch at any time in the **Model Registry** tab.

**Local (default): Ollama on `http://localhost:11434` with `gemma3:1b`.**

- With the Ollama Windows app: install it, then run `ollama pull gemma3:1b`.
- Or with Docker (this repo's compose file passes your GPU through):

  ```powershell
  docker compose -f docker-compose.ollama.yml up -d
  docker exec talkflow-ollama-local ollama pull gemma3:1b
  ```

Small local models vary a lot. `gemma3:1b` is safe but light-touch. `llama3.2:3b` cleans up noticeably better if you have the memory for it. Some small models translate or follow instructions embedded in what you dictate, so try a model before relying on it.

**Cloud: any OpenAI-compatible API.** Open the Model Registry tab and add a cleanup entry with the provider's base URL (for example `https://ollama.com` for Ollama Cloud), the model ID and your API key. The key is stored in Windows Credential Manager, never in a file or in the database.

### 4. Run it

```powershell
.venv\Scripts\python.exe -m talkflow.app
```

TalkFlow starts in the system tray, and a small pill sits at the bottom of the screen. Open the main window from the tray icon (or by launching TalkFlow again). Only one copy runs at a time.

To add a Start Menu entry (which you can then pin to the taskbar), run:

```powershell
.venv\Scripts\python.exe scripts\create_start_menu_shortcut.py
```

"Launch at login" is a switch in the Settings tab.

## Using it

| Action | Default hotkey |
|---|---|
| Dictate (hold to talk, release to stop) | `Caps Lock` |
| Cancel the current dictation | `Esc` |
| Transform: Polish the selected text | `Ctrl+Shift+P` |
| Transform: Prompt Engineer | `Ctrl+Shift+E` |
| Command Mode (hold, speak an instruction) | `Ctrl+Shift+M` |

Every hotkey can be changed in the app by clicking the hotkey control and pressing the keys you want. Dictation can also be hands-free (tap to start, tap to stop) instead of hold-to-talk. You can also start a dictation or a meeting recording by hovering over the pill and clicking its mic or record button.

Before your first dictation, set your languages in Settings. The default is English only.

## Data and privacy

- Settings, history and the model registry are stored in `%USERPROFILE%\.talkflow`, with logs in its `logs` subfolder.
- Audio is transcribed locally and is not kept (unless you turn on audio retention). Only text is sent to the cleanup LLM, which is your local Ollama unless you configure a cloud provider.
- Text is inserted through the clipboard, and your previous clipboard contents are restored afterwards.

## Development

```powershell
.venv\Scripts\python.exe -m pytest tests
```

- `talkflow/`: the app. Capture, ASR adapters, LLM adapters, the orchestrator pipeline, insertion, overlay, main window (pywebview), and features.
- `scripts/`: helper and diagnostic scripts (model benchmarking, ASR switching, the API key helper).
- `docker-compose.asr.yml`: an alternative Whisper (speaches) speech recognition server, selectable in the Model Registry.
- [SYSTEM_SPEC.md](SYSTEM_SPEC.md) and [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md): the original product brief and design questions.
