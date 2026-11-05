import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from talkflow.history.db import init_db
from talkflow.registry import store as rs, secrets as ss
from talkflow.llm.openai_compatible_adapter import OpenAiCompatibleAdapter

init_db()
a = rs.get_active('live_suggestion') or rs.get_active('cleanup')
key = ss.get_api_key(a.api_key_ref) if a.api_key_ref else None
adapter = OpenAiCompatibleAdapter(base_url=a.base_url, model_id=a.model_id, api_key=key, timeout=30.0)

# Clean, non-garbled transcript (no acoustic-bleed duplication) with a clear question.
transcript = [
    ("they", "Como voce priorizaria duas funcionalidades concorrentes para o mesmo sprint?"),
    ("me", "Isso depende de qual gera mais valor para o usuario."),
    ("they", "Como voce faria essa priorizacao?"),
]
result = adapter.suggest(
    transcript_lines=transcript,
    assistant_name="Interview Assistant",
    assistant_task=(
        "You are listening live to a job interview. THEY is the interviewer; ME is the user being "
        "interviewed. When asked for a suggestion, answer THEY's most recent question as ME would say it "
        "out loud, grounded ONLY in what ME has actually said earlier in this same conversation."
    ),
    assistant_style="Confident, concise, first-person, spoken out loud naturally - 2 to 4 sentences.",
    language="pt",
)
print("has_suggestion:", result.has_suggestion)
print("text:", repr(result.suggestion_text))
