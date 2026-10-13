"""Per-dictation timing records, for comparing ASR/LLM providers and pipeline
changes. Each finished dictation is logged as a readable INFO block and
appended as one JSON line to <data dir>/logs/dictation_timing.jsonl
(summarized by scripts/timing_report.py). Durations only - plus character
counts, never the dictated text.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from talkflow.config.paths import log_dir

logger = logging.getLogger(__name__)

TIMING_FILE = "dictation_timing.jsonl"

# The phases that make up the wait between the stop key and the paste, in order.
AFTER_STOP_PHASES = (
    ("grace_ms", "tail grace"),
    ("recorder_stop_ms", "stop mic"),
    ("asr_wait_ms", "wait for ASR"),
    ("cleanup_ms", "cleanup"),
    ("paste_ms", "paste"),
)


def format_record(r: dict) -> str:
    lines = [
        f"dictation timing [{r['mode']}]  asr={r['asr_model']} @ {r['asr_host']}  llm={r['llm_model']}",
        f"  audio {r['audio_s']:.1f}s in {len(r['segments'])} piece(s), "
        f"{r['raw_chars']} chars raw -> {r['clean_chars']} clean"
        + (f"  ERROR: {r['error']}" if r.get("error") else ""),
    ]
    for i, seg in enumerate(r["segments"], 1):
        when = "after stop" if seg["after_stop"] else "while speaking"
        extra = "".join(f" | {label} {seg[key]:.0f}ms"
                        for key, label in (("cleanup_ms", "cleanup"), ("paste_ms", "paste")) if key in seg)
        lines.append(f"  asr piece {i} ({when}): {seg['audio_s']:.1f}s audio -> {seg['asr_ms']:.0f}ms, "
                     f"{seg['chars']} chars{extra}")
    after = r["after_stop"]
    lines.append("  after stop key: " + " | ".join(
        f"{label} {after[key]:.0f}ms" for key, label in AFTER_STOP_PHASES if key in after))
    lines.append(f"  STOP KEY -> PASTED: {r['stop_to_paste_ms']:.0f}ms")
    return "\n".join(lines)


def report(record: dict) -> None:
    record = {"at": datetime.now().isoformat(timespec="seconds"), **record}
    logger.info("%s", format_record(record))
    try:
        with open(log_dir() / TIMING_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("could not append the timing record")
