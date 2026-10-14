"""Usage insights computed live from dictation_sessions + dictionary_entries
- no separate tracking table, no fabricated/benchmarked numbers (e.g. no
"top X% of users" claim - that needs a cross-user population TalkFlow, a
solo-user tool, doesn't have). Everything here is derived from what's
actually in the local database.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from talkflow.features.dictionary.store import list_entries as list_dictionary_entries
from talkflow.history.db import connect

MIN_SESSION_SECONDS_FOR_WPM = 2.0  # filters out near-instant/aborted sessions that would skew WPM


def _word_count(text: str | None) -> int:
    return len((text or "").split())


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


@dataclass
class AppUsage:
    process_name: str
    session_count: int
    word_count: int
    pct: float


@dataclass
class LanguageUsage:
    language: str
    session_count: int
    word_count: int
    pct: float


@dataclass
class InsightsSummary:
    total_words: int
    total_sessions: int
    avg_wpm: float
    terms_learned: int
    terms_manual: int
    app_usage: list[AppUsage] = field(default_factory=list)
    language_usage: list[LanguageUsage] = field(default_factory=list)
    current_streak: int = 0
    longest_streak: int = 0
    daily_counts: dict = field(default_factory=dict)  # "YYYY-MM-DD" -> session count


def get_summary(days: int = 180) -> InsightsSummary:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT started_at, ended_at, cleaned_transcript, target_process_name, detected_language "
            "FROM dictation_sessions WHERE state='complete'"
        ).fetchall()
    finally:
        conn.close()

    total_words = 0
    wpm_samples: list[float] = []
    app_sessions: Counter = Counter()
    app_words: Counter = Counter()
    lang_sessions: Counter = Counter()
    lang_words: Counter = Counter()
    daily_counts: defaultdict = defaultdict(int)

    for row in rows:
        words = _word_count(row["cleaned_transcript"])
        total_words += words

        started = _parse(row["started_at"])
        ended = _parse(row["ended_at"])
        if started and ended:
            duration_s = (ended - started).total_seconds()
            if duration_s >= MIN_SESSION_SECONDS_FOR_WPM and words > 0:
                wpm_samples.append(words / (duration_s / 60))
            daily_counts[started.astimezone(timezone.utc).date().isoformat()] += 1

        process = row["target_process_name"]
        if process:
            app_sessions[process] += 1
            app_words[process] += words

        language = row["detected_language"]
        if language:
            lang_sessions[language] += 1
            lang_words[language] += words

    avg_wpm = sum(wpm_samples) / len(wpm_samples) if wpm_samples else 0.0

    total_app_sessions = sum(app_sessions.values()) or 1
    app_usage = [
        AppUsage(process_name=name, session_count=count, word_count=app_words[name],
                  pct=round(100 * count / total_app_sessions, 1))
        for name, count in app_sessions.most_common(8)
    ]

    total_lang_sessions = sum(lang_sessions.values()) or 1
    language_usage = [
        LanguageUsage(language=lang, session_count=count, word_count=lang_words[lang],
                       pct=round(100 * count / total_lang_sessions, 1))
        for lang, count in lang_sessions.most_common(8)
    ]

    entries = list_dictionary_entries()
    terms_learned = sum(1 for e in entries if e.source == "learned")
    terms_manual = sum(1 for e in entries if e.source != "learned")

    current_streak, longest_streak = _compute_streaks(set(daily_counts.keys()))

    return InsightsSummary(
        total_words=total_words,
        total_sessions=len(rows),
        avg_wpm=round(avg_wpm, 1),
        terms_learned=terms_learned,
        terms_manual=terms_manual,
        app_usage=app_usage,
        language_usage=language_usage,
        current_streak=current_streak,
        longest_streak=longest_streak,
        daily_counts=dict(daily_counts),
    )


def _compute_streaks(active_dates: set) -> tuple[int, int]:
    """active_dates: set of 'YYYY-MM-DD' strings. Returns (current_streak,
    longest_streak) in consecutive calendar days. Current streak counts back
    from today (or yesterday, so a day still in progress doesn't look broken)."""
    if not active_dates:
        return 0, 0

    parsed = sorted(datetime.fromisoformat(d).date() for d in active_dates)
    longest = 1
    run = 1
    for prev, cur in zip(parsed, parsed[1:]):
        if (cur - prev).days == 1:
            run += 1
            longest = max(longest, run)
        elif (cur - prev).days > 1:
            run = 1

    today = datetime.now(timezone.utc).date()
    anchor = today if today.isoformat() in active_dates else today - timedelta(days=1)
    current = 0
    cursor = anchor
    date_set = set(parsed)
    while cursor in date_set:
        current += 1
        cursor -= timedelta(days=1)

    return current, longest
