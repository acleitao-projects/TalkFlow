"""Add (or refresh) a "TalkFlow" entry in the Start Menu. No admin rights
needed - it goes in the per-user Start Menu folder.

Usage:
    .venv\\Scripts\\python.exe scripts\\create_start_menu_shortcut.py

Re-run it if you move the project folder.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from talkflow.config import shortcuts  # noqa: E402


def main() -> None:
    path = shortcuts.start_menu_dir() / "TalkFlow.lnk"
    shortcuts.create_shortcut(path)
    print(f"Created {path}")
    print("Open TalkFlow from the Start Menu; right-click its taskbar button to pin it.")


if __name__ == "__main__":
    main()
