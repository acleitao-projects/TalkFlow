"""Phase 1 risk spike: confirm pynput's global keyboard hook works without
admin rights on this machine (corporate group policy can occasionally block
SetWindowsHookEx even in user mode). Not part of the app - delete once verified.

Run it, then press F9 a few times, then Esc to quit.
"""
from pynput import keyboard

print("Listening for F9 (prints a message) and Esc (quits). Try pressing F9 a few times.")

def on_press(key):
    if key == keyboard.Key.f9:
        print("F9 detected - global hook works without admin.")
    elif key == keyboard.Key.esc:
        print("Esc detected - stopping listener.")
        return False

with keyboard.Listener(on_press=on_press) as listener:
    listener.join()
