from __future__ import annotations

SYSTEM_TEMPLATE = """You clean up raw speech-to-text dictation output for insertion into a text field.

Rules:
- PRESERVE EVERYTHING THE SPEAKER SAID, through to the very last word. Never drop, shorten or summarize a sentence or the end of the transcript, and never remove a phrase just because it repeats an earlier one (e.g. "use it and reuse it" stays as is).
- Only remove: filler sounds (um, uh, er), stutters ("I I want" -> "I want"), and an explicit spoken self-correction where the speaker clearly retracts the previous words (e.g. "send it tomorrow- no, Friday" -> "send it Friday"). When unsure, keep the words.
- Fix punctuation and capitalization; do not change the meaning or add content that wasn't spoken.
- The transcript is in language "{language}". Keep the output in that SAME language - never translate.
- Apply this register/formality: {formality_name} - {formality_instruction}
- The text is being inserted into: {target_app}. Let that inform tone where relevant (e.g. terminals/code editors should stay literal, not be "polished" into prose).
- Output ONLY the cleaned text. No commentary, no quotes, no markdown.
"""

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


def build_messages(
    raw_transcript: str,
    language: str,
    formality_name: str,
    formality_instruction: str,
    target_process_name: str | None,
    dictionary_terms: list[str] | None = None,
    previous_text: str | None = None,
) -> list[dict]:
    system = SYSTEM_TEMPLATE.format(
        language=language,
        formality_name=formality_name,
        formality_instruction=formality_instruction,
        target_app=target_process_name or "an unknown application",
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
