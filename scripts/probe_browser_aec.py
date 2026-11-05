"""Phase 1 Step 0 probe (see the Live Assist audio-fix plan): does Chromium's
getUserMedia echoCancellation actually reduce the RodeCaster duplication,
before building the full browser-mic-capture pipeline around it?

Pick the RodeCaster mic device, record a few seconds WITH echo cancellation
and WITHOUT (raw) while ChatGPT/an interviewer's audio is playing, then
compare: each clip is sent to Whisper for a real transcript (via the `Api`
js_api bridge below), so the comparison is "does the WITH transcript still
contain ChatGPT's words" - an objective text diff, not "does this sound
cleaner" from memory of two separate clips (confirmed live that the
by-ear version was a genuinely weak test). The page also prints
track.getSettings() so we can confirm the browser actually applied the
echoCancellation constraint, not just accepted the request silently.

Usage:
    .venv\\Scripts\\python.exe scripts\\probe_browser_aec.py
"""
import base64
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
import webview
from webview.platforms.winforms import BrowserView

from talkflow.history.db import init_db
from talkflow.registry import secrets as secret_store
from talkflow.registry import store as registry_store


class Api:
    """Transcribes each recorded clip with Whisper so the WITH-vs-RAW
    comparison is an objective text diff, not "does this sound cleaner to
    whoever's listening" - the subjective version turned out to be a weak
    test (fair complaint, raised live): two isolated 5s clips, judged from
    memory, with nothing to measure against."""

    def transcribe(self, label: str, base64_webm: str) -> str:
        init_db()
        assignment = registry_store.get_active("live_assist_asr") or registry_store.get_active("asr")
        api_key = secret_store.get_api_key(assignment.api_key_ref) if assignment and assignment.api_key_ref else None
        if not api_key:
            msg = f"[{label}] No OpenAI API key found in the registry - cannot transcribe."
            print(msg)
            return msg
        audio_bytes = base64.b64decode(base64_webm)
        try:
            resp = httpx.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": ("clip.webm", audio_bytes, "audio/webm")},
                data={"model": "whisper-1"},
                timeout=30.0,
            )
            resp.raise_for_status()
            text = resp.json().get("text", "")
        except Exception as e:
            text = f"<transcription failed: {e}>"
        msg = f"[{label}] transcript: {text!r}"
        print(msg)
        return msg

# pywebview's EdgeChromium backend wires several CoreWebView2 events
# (NewWindowRequested, DownloadStarting, ...) but not PermissionRequested -
# confirmed by reading webview/platforms/edgechromium.py directly, nothing
# there handles it at all. Without an explicit handler, WebView2 denies
# getUserMedia silently (no prompt, no error dialog) - confirmed live:
# the probe's device dropdown came back empty with "permission failed" in
# the log. Hook the same underlying CoreWebView2 object (reached via the
# BrowserView.instances registry pywebview itself populates) directly,
# the same way this script's own pythonnet/clr access to the already-loaded
# WebView2 SDK assembly lets it - this is the real engineering gap the
# audio-fix plan flagged for Phase 1 and needs resolving before the full
# browser-mic-capture bridge can work at all.
def _grant_mic_permission(window) -> None:
    import clr  # noqa: F401 - already loaded by importing webview/edgechromium
    from Microsoft.Web.WebView2.Core import CoreWebView2PermissionKind, CoreWebView2PermissionState
    from System import Action

    form = BrowserView.instances.get(window.uid)
    if form is None:
        print("Could not reach the browser form yet - permission grant skipped.")
        return

    def do_hook():
        # CoreWebView2 is a COM/STA object - confirmed live that touching it
        # from the thread pywebview's own `loaded` event fires callbacks on
        # (a plain Python thread, not the WinForms UI thread the control was
        # created on) raises "CoreWebView2 can only be accessed from the UI
        # thread". Form.Invoke marshals this onto that thread instead, same
        # as pywebview's own internals already do for other CoreWebView2
        # calls (see edgechromium.py's own self.webview.Invoke(...) use).
        core = form.browser.webview.CoreWebView2
        if core is None:
            print("CoreWebView2 still not ready - permission grant skipped.")
            return

        def on_permission_requested(sender, args):
            if args.PermissionKind == CoreWebView2PermissionKind.Microphone:
                args.State = CoreWebView2PermissionState.Allow
                print("Auto-granted microphone permission request.")

        core.PermissionRequested += on_permission_requested
        print("Microphone permission auto-grant hook installed.")

    form.Invoke(Action(do_hook))

HTML = """
<!doctype html>
<html>
<head><meta charset="utf-8"><title>AEC probe</title>
<style>
  body { font-family: sans-serif; padding: 16px; background: #111; color: #eee; }
  button { padding: 10px 16px; margin: 4px; font-size: 14px; }
  select { padding: 6px; margin-bottom: 12px; width: 100%; }
  pre { background: #000; color: #0f0; padding: 10px; white-space: pre-wrap; max-height: 200px; overflow: auto; }
  .row { margin-bottom: 16px; }
  audio { width: 100%; margin-top: 6px; }
</style>
</head>
<body>
  <h2>Browser AEC probe</h2>
  <div class="row">
    <label>Microphone device:</label>
    <select id="micSelect"></select>
    <button onclick="refreshDevices()">Refresh device list</button>
  </div>

  <div class="row">
    <button onclick="recordWith(true)">Record 5s WITH echo cancellation</button>
    <audio id="audioWith" controls></audio>
    <div id="settingsWith"></div>
  </div>

  <div class="row">
    <button onclick="recordWith(false)">Record 5s RAW (no processing)</button>
    <audio id="audioRaw" controls></audio>
    <div id="settingsRaw"></div>
  </div>

  <h3>Log</h3>
  <pre id="log"></pre>

  <script>
    const logEl = document.getElementById('log');
    function log(msg) {
      logEl.textContent += msg + "\\n";
      logEl.scrollTop = logEl.scrollHeight;
    }

    async function refreshDevices() {
      try {
        // Need a permission grant first before labels are populated.
        const tmp = await navigator.mediaDevices.getUserMedia({audio: true});
        tmp.getTracks().forEach(t => t.stop());
      } catch (e) {
        log("Permission request failed: " + e.message);
      }
      const devices = await navigator.mediaDevices.enumerateDevices();
      const mics = devices.filter(d => d.kind === 'audioinput');
      const sel = document.getElementById('micSelect');
      sel.innerHTML = mics.map(d => `<option value="${d.deviceId}">${d.label || d.deviceId}</option>`).join('');
      log("Found " + mics.length + " input devices.");
      mics.forEach(d => log("  - " + (d.label || d.deviceId)));
    }

    async function recordWith(echoCancellation) {
      const deviceId = document.getElementById('micSelect').value;
      const tag = echoCancellation ? "WITH" : "RAW";
      log(`--- Recording 5s ${tag} (deviceId=${deviceId}) ---`);
      let stream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            deviceId: deviceId ? {exact: deviceId} : undefined,
            echoCancellation: echoCancellation,
            noiseSuppression: echoCancellation,
            autoGainControl: false,
            channelCount: 1,
          }
        });
      } catch (e) {
        log("getUserMedia failed: " + e.message);
        return;
      }
      const track = stream.getAudioTracks()[0];
      const settings = track.getSettings();
      const constraints = track.getConstraints();
      log(tag + " actual settings: " + JSON.stringify(settings));
      document.getElementById(echoCancellation ? 'settingsWith' : 'settingsRaw').textContent =
        "actual: " + JSON.stringify(settings) + "\\nrequested: " + JSON.stringify(constraints);

      const recorder = new MediaRecorder(stream);
      const chunks = [];
      recorder.ondataavailable = e => chunks.push(e.data);
      recorder.onstop = async () => {
        const blob = new Blob(chunks, {type: 'audio/webm'});
        const url = URL.createObjectURL(blob);
        const audioEl = document.getElementById(echoCancellation ? 'audioWith' : 'audioRaw');
        audioEl.src = url;
        stream.getTracks().forEach(t => t.stop());
        log(tag + " recording ready - press play to listen. Transcribing...");
        const base64 = await blobToBase64(blob);
        const result = await window.pywebview.api.transcribe(tag, base64);
        log(result);
      };
      recorder.start();
      setTimeout(() => recorder.stop(), 5000);
    }

    function blobToBase64(blob) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onloadend = () => resolve(reader.result.split(',')[1]);
        reader.onerror = reject;
        reader.readAsDataURL(blob);
      });
    }

    refreshDevices();
  </script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # keep the terminal clean - pywebview/permission logs matter here, not HTTP access logs


def main() -> None:
    # getUserMedia (and navigator.mediaDevices itself) requires a secure
    # context - confirmed live that webview.create_window(html=...) does
    # NOT count as one under WebView2 (navigator.mediaDevices came back
    # undefined entirely). Loopback HTTP (http://127.0.0.1) is specially
    # treated as secure by browsers/WebView2 even without TLS, so serve the
    # page from a tiny local server instead of inline HTML.
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()

    window = webview.create_window(
        "AEC probe", url=f"http://127.0.0.1:{port}/", width=700, height=700, js_api=Api())
    window.events.loaded += lambda: _grant_mic_permission(window)
    webview.start()


if __name__ == "__main__":
    main()
