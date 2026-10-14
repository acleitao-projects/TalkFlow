"""Notetaker pipeline: chunk the full meeting recording into fixed-length
segments (so one very long meeting doesn't balloon a single ASR adapter's
growing buffer or HTTP request), transcribe each segment independently,
concatenate, then ask the cleanup-stage LLM for a summary + action items via
its generic rewrite(). Stored separately from ordinary dictation history
(notetaker_sessions table) per CLAUDE.md's "raw vs cleaned kept separate"
principle extended to this feature.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from talkflow.asr.base import AsrAdapter
from talkflow.history.db import connect
from talkflow.llm.base import LlmAdapter

SEGMENT_SECONDS = 60
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2

SUMMARY_INSTRUCTION = (
    "This is a raw meeting transcript, possibly with recognition errors. "
    "Produce: a short Summary section, a Key Points bullet list, and an "
    "Action Items bullet list (empty if none). Markdown, concise."
)


@dataclass
class NotetakerResult:
    session_id: str
    transcript: str
    summary: str


def process_meeting(pcm: bytes, asr: AsrAdapter, llm: LlmAdapter) -> NotetakerResult:
    session_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()

    segment_bytes = SEGMENT_SECONDS * SAMPLE_RATE * BYTES_PER_SAMPLE
    transcript_parts = []
    for offset in range(0, len(pcm), segment_bytes):
        segment = pcm[offset:offset + segment_bytes]
        if not segment:
            continue
        asr.start_session(["en"])
        asr.feed_chunk(segment, sample_rate=SAMPLE_RATE)
        final = asr.finalize()
        if final.text.strip():
            transcript_parts.append(final.text.strip())

    transcript = "\n".join(transcript_parts)
    summary = ""
    if transcript.strip():
        try:
            result = llm.rewrite(transcript, SUMMARY_INSTRUCTION)
            summary = result.cleaned_text
        except Exception:
            summary = "(summary generation failed - see transcript)"

    _save(session_id, started_at, transcript, summary)
    return NotetakerResult(session_id=session_id, transcript=transcript, summary=summary)


def _save(session_id: str, started_at: str, transcript: str, summary: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO notetaker_sessions (id, started_at, ended_at, state, transcript, summary) "
            "VALUES (?, ?, ?, 'complete', ?, ?)",
            (session_id, started_at, datetime.now(timezone.utc).isoformat(), transcript, summary),
        )
        conn.commit()
    finally:
        conn.close()


def delete_meeting(session_id: str) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM notetaker_sessions WHERE id=?", (session_id,))
        conn.commit()
    finally:
        conn.close()


def list_meetings(limit: int = 20) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM notetaker_sessions ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
