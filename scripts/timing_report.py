"""Summarize the per-dictation timing records TalkFlow writes to
<data dir>/logs/dictation_timing.jsonl: one row per dictation, then averages
grouped by ASR model + cleanup model + mode, so two setups can be compared.

Usage:
    .venv\\Scripts\\python.exe scripts\\timing_report.py          # last 30 dictations
    .venv\\Scripts\\python.exe scripts\\timing_report.py 100      # last 100
    .venv\\Scripts\\python.exe scripts\\timing_report.py clear    # start a fresh benchmark
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from talkflow.config.paths import log_dir  # noqa: E402
from talkflow.pipeline.timing import TIMING_FILE  # noqa: E402

COLUMNS = ("audio_s", "pieces", "asr_total", "asr_wait", "cleanup", "paste", "stop>paste")


def _row(r: dict) -> dict:
    after = r["after_stop"]
    return {
        "audio_s": r["audio_s"],
        "pieces": len(r["segments"]),
        "asr_total": sum(s["asr_ms"] for s in r["segments"]),   # all ASR work, incl. while speaking
        "asr_wait": after.get("asr_wait_ms", 0),                  # the part of it you waited for
        "cleanup": after.get("cleanup_ms", sum(s.get("cleanup_ms", 0) for s in r["segments"])),
        "paste": after.get("paste_ms", sum(s.get("paste_ms", 0) for s in r["segments"])),
        "stop>paste": r["stop_to_paste_ms"],
    }


def _fmt(values: dict) -> str:
    return "".join(f"{values[c]:>11.1f}" if c == "audio_s" else f"{values[c]:>11.0f}" for c in COLUMNS)


def main() -> None:
    path = log_dir() / TIMING_FILE
    if len(sys.argv) > 1 and sys.argv[1] == "clear":
        path.unlink(missing_ok=True)
        print(f"Cleared {path}")
        return
    if not path.exists():
        print(f"No timing records yet ({path}). Dictate something first.")
        return
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    records = [r for r in records if r["segments"]][-limit:]

    header = f"{'time':<10}{'asr model':<32}" + "".join(f"{c:>11}" for c in COLUMNS)
    print("All times in ms, except audio_s.\n" + header)
    groups = defaultdict(list)
    for r in records:
        row = _row(r)
        print(f"{r['at'][11:]:<10}{r['asr_model'][-30:]:<32}{_fmt(row)}" + ("  ERROR " + r["error"] if r.get("error") else ""))
        if not r.get("error"):
            groups[(r["asr_model"], r["llm_model"], r["mode"])].append(row)

    print("\nAverages per setup:")
    for (asr_model, llm_model, mode), rows in groups.items():
        avg = {c: sum(row[c] for row in rows) / len(rows) for c in COLUMNS}
        print(f"\n  asr={asr_model}  llm={llm_model}  [{mode}]  n={len(rows)}")
        print(f"  {'':<40}" + "".join(f"{c:>11}" for c in COLUMNS))
        print(f"  {'':<40}{_fmt(avg)}")
        audio = sum(row["audio_s"] for row in rows)
        if audio:
            print(f"  ASR speed: {sum(row['asr_total'] for row in rows) / audio:.0f} ms per second of audio")


if __name__ == "__main__":
    main()
