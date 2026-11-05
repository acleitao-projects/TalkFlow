from __future__ import annotations

SYSTEM_TEMPLATE = """You clean up raw speech-to-text dictation output for insertion into a text field.

Rules:
- PRESERVE EVERYTHING THE SPEAKER SAID, through to the very last word. Never drop, shorten or summarize a sentence or the end of the transcript, and never remove a phrase just because it repeats an earlier one (e.g. "use it and reuse it" stays as is).
{cleanup_level_instruction}
- The transcript is in language "{language}". Keep the output in that SAME language - never translate.
- Apply this register/formality: {formality_name} - {formality_instruction}
- The text is being inserted into: {target_app}. Let that inform tone where relevant (e.g. terminals/code editors should stay literal, not be "polished" into prose).
- Output ONLY the cleaned text. No commentary, no quotes, no markdown.
"""

# Auto Cleanup setting (Settings.cleanup_level): how aggressively the cleanup
# stage edits the transcript, independent of the formality/register instruction
# above. "none" never reaches this prompt at all - the adapter short-circuits
# and returns the raw transcript unmodified (see openai_compatible_adapter.py).
CLEANUP_LEVEL_INSTRUCTIONS = {
    "light": (
        '- Only remove: filler sounds (um, uh, er), stutters ("I I want" -> "I want"), and an explicit '
        'spoken self-correction where the speaker clearly retracts the previous words (e.g. "send it '
        'tomorrow- no, Friday" -> "send it Friday"). When unsure, keep the words.\n'
        '- Fix punctuation and capitalization; do not change the meaning or add content that wasn\'t spoken.'
    ),
    "medium": (
        '- Remove filler sounds, stutters, and self-corrections as in "light" cleanup, AND tighten the '
        "wording for clarity and conciseness: cut redundant phrases, merge choppy sentences, prefer the more "
        "direct phrasing - without changing the meaning or dropping any point the speaker made.\n"
        '- Fix punctuation and capitalization.'
    ),
}

USER_TEMPLATE = """Raw transcript:
{raw_transcript}
{dictionary_block}"""


REWRITE_SYSTEM = """You rewrite text according to an instruction. Output ONLY the rewritten text - no commentary, no quotes, no markdown fencing, no preamble."""

REWRITE_USER_TEMPLATE = """Instruction: {instruction}

Text:
{text}"""


def build_rewrite_messages(text: str, instruction: str) -> list[dict]:
    return [
        {"role": "system", "content": REWRITE_SYSTEM},
        {"role": "user", "content": REWRITE_USER_TEMPLATE.format(instruction=instruction, text=text)},
    ]


# LA-09: live suggestion engine. NO_SUGGESTION_MARKER is checked verbatim by
# OpenAiCompatibleAdapter.suggest() against the model's full (stripped)
# output - an explicit, parseable "nothing useful yet" signal, so a thin
# conversation (or one where the user already said everything needed) comes
# back as a real no-suggestion result instead of a fabricated, padded reply.
NO_SUGGESTION_MARKER = "NO_SUGGESTION"

SUGGEST_SYSTEM = """You are a live conversational assistant: "{assistant_name}". Your task: {assistant_task}
Response style: {assistant_style}

You are shown a live two-person conversation transcript - ME (the person you're helping) and THEY (the other \
person). Draft the next thing ME could say or send, grounded ONLY in what was actually said in the transcript \
below - never invent facts, numbers or commitments that weren't mentioned.

Rules:
- Write in language "{language}" - never translate or switch languages.
- Output ONLY the suggested text ME could say/send next. No commentary, no quotes, no markdown, no "Here's a suggestion:" preamble.
- If the transcript is too short, too unclear, or genuinely has nothing useful to suggest right now, output exactly: {no_suggestion_marker}
"""

SUGGEST_USER_TEMPLATE = """Transcript so far:
{transcript_block}"""

# Appended after a custom_system_prompt (a hand-written persona, e.g. a
# ported Perssua ROLE/TASK/STYLE/EXAMPLES prompt) - that kind of prompt
# describes WHO to be, not how this app's transcript is shaped or how to
# signal "nothing to suggest", so those mechanics still need stating
# explicitly regardless of which persona is active.
CUSTOM_PROMPT_FOOTER = """

You are in a live two-person conversation, shown as a transcript below - ME (you) and THEY (the other person). \
Respond as ME, to THEY's most recent point, grounded ONLY in what was actually said in the transcript - never \
invent facts, numbers or claims that weren't established there.

Rules:
- Write in language "{language}" - never translate or switch languages.
- Output ONLY the response ME would say/send next. No commentary, no quotes, no markdown, no preamble.
- If the transcript is too short, too unclear, or genuinely has nothing useful to respond to yet, output exactly: {no_suggestion_marker}
"""


def build_suggestion_messages(
    transcript_lines: list[tuple[str, str]],
    assistant_name: str,
    assistant_task: str,
    assistant_style: str,
    language: str,
    custom_system_prompt: str | None = None,
) -> list[dict]:
    if custom_system_prompt:
        system = custom_system_prompt.strip() + CUSTOM_PROMPT_FOOTER.format(
            language=language, no_suggestion_marker=NO_SUGGESTION_MARKER,
        )
    else:
        system = SUGGEST_SYSTEM.format(
            assistant_name=assistant_name, assistant_task=assistant_task, assistant_style=assistant_style,
            language=language, no_suggestion_marker=NO_SUGGESTION_MARKER,
        )
    transcript_block = "\n".join(
        f"{'ME' if source == 'me' else 'THEY'}: {text}" for source, text in transcript_lines
    ) or "(nothing said yet)"
    user = SUGGEST_USER_TEMPLATE.format(transcript_block=transcript_block)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_messages(
    raw_transcript: str,
    language: str,
    formality_name: str,
    formality_instruction: str,
    target_process_name: str | None,
    dictionary_terms: list[str] | None = None,
    previous_text: str | None = None,
    cleanup_level: str = "light",
) -> list[dict]:
    system = SYSTEM_TEMPLATE.format(
        language=language,
        formality_name=formality_name,
        formality_instruction=formality_instruction,
        target_app=target_process_name or "an unknown application",
        cleanup_level_instruction=CLEANUP_LEVEL_INSTRUCTIONS.get(cleanup_level, CLEANUP_LEVEL_INSTRUCTIONS["light"]),
    )
    if previous_text:
        # Partial paste: this transcript continues a dictation whose earlier
        # part is already in the field.
        system += (
            "- The raw transcript CONTINUES text that was already inserted, shown below as "
            "context only. Do NOT repeat or rewrite it; output only the cleaned continuation, "
            "capitalized and punctuated so it reads correctly right after it.\n"
            f"Already inserted (context only): \"...{previous_text}\"\n"
        )
    dict_block = ""
    if dictionary_terms:
        dict_block = (
            "\n\nKnown vocabulary (use correct spelling if any of these were "
            "spoken, even if the ASR misheard them): " + ", ".join(dictionary_terms)
        )
    user = USER_TEMPLATE.format(raw_transcript=raw_transcript, dictionary_block=dict_block)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
