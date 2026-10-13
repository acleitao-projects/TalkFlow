"""Phase 1 risk spike: confirm a pywebview frameless/always-on-top window can be
created and torn down without error on this machine. This only proves the window
*constructs* successfully - it does NOT verify visual appearance (transparency
cleanliness, click-through, focus-stealing), which needs a human to look at the
screen. Not part of the app - delete once verified.
"""
import threading
import webview

HTML = """
<html><body style="margin:0;background:#111;border-radius:999px;
display:flex;align-items:center;justify-content:center;height:100vh;
font-family:sans-serif;color:#fff;">
  <span>Dictate &middot; Caps Lock</span>
</body></html>
"""

def close_after_delay(window):
    import time
    time.sleep(5)
    window.destroy()

def main():
    window = webview.create_window(
        title="TalkFlow overlay spike",
        html=HTML,
        width=260,
        height=56,
        frameless=True,
        on_top=True,
        transparent=True,
        easy_drag=False,
        resizable=False,
    )
    t = threading.Thread(target=close_after_delay, args=(window,), daemon=True)
    t.start()
    webview.start()
    print("Window closed cleanly - construction/teardown OK.")

if __name__ == "__main__":
    main()
