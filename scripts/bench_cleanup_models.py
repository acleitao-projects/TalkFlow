"""Time the dictation cleanup prompt against several models on the active
cleanup provider, to pick a faster `model_id` for the Model Registry.

Usage:
    .venv\\Scripts\\python.exe scripts\\bench_cleanup_models.py            # list the provider's models
    .venv\\Scripts\\python.exe scripts\\bench_cleanup_models.py m1 m2 ...  # benchmark these (plus the active one)

Uses the real cleanup prompt (talkflow/llm/prompts.py) and the active
cleanup registry entry's base_url + stored API key. Nothing is changed in the
registry - set the winner yourself in the Model Registry tab.
"""
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from talkflow.history.db import init_db  # noqa: E402
from talkflow.llm.openai_compatible_adapter import OpenAiCompatibleAdapter  # noqa: E402
from talkflow.registry import secrets as secret_store  # noqa: E402
from talkflow.registry import store as registry_store  # noqa: E402

SAMPLES = [
    ("en", "um so I think uh we should we should ship it on on Friday, no wait, Monday, "
           "and uh let the the team know before lunch"),
    ("pt", "é então hum eu acho que a gente a gente devia mandar o relatório ah amanhã cedo "
           "e tipo avisar o o pessoal do financeiro antes da reunião"),
]
RUNS_PER_SAMPLE = 2  # first run includes connection setup / model load


def main() -> None:
    init_db()
    registry_store.ensure_defaults()
    assignment = registry_store.get_active("cleanup")
    if assignment is None:
        print("No active cleanup-stage registry entry.")
        return
    api_key = secret_store.get_api_key(assignment.api_key_ref) if assignment.api_key_ref else None
    base_url = assignment.base_url.rstrip("/")
    print(f"Provider: {assignment.provider}  base_url: {base_url}  active model: {assignment.model_id}\n")

    candidates = sys.argv[1:]
    if not candidates:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        resp = httpx.get(f"{base_url}/v1/models", headers=headers, timeout=30.0)
        resp.raise_for_status()
        print("Available models:")
        for m in sorted(item["id"] for item in resp.json().get("data", [])):
            print(f"  {m}")
        print("\nRe-run with the model ids to compare, e.g.:")
        print("  .venv\\Scripts\\python.exe scripts\\bench_cleanup_models.py <model-a> <model-b>")
        return

    models = list(dict.fromkeys([assignment.model_id, *candidates]))
    summary = []
    for model_id in models:
        adapter = OpenAiCompatibleAdapter(base_url=base_url, model_id=model_id, api_key=api_key)
        print(f"=== {model_id}")
        latencies = []
        try:
            for language, raw in SAMPLES:
                for run in range(RUNS_PER_SAMPLE):
                    start = time.monotonic()
                    result = adapter.cleanup(raw, language, "Normal",
                                             "Clear, neutral, professional.", "notepad.exe")
                    ms = (time.monotonic() - start) * 1000
                    latencies.append(ms)
                    if run == RUNS_PER_SAMPLE - 1:
                        print(f"  [{language}] {ms:6.0f} ms  {result.cleaned_text}")
        except Exception as e:
            print(f"  FAILED: {e}")
            continue
        # Skip the very first call (cold connection) when averaging.
        warm = latencies[1:] or latencies
        summary.append((sum(warm) / len(warm), model_id))

    print("\nAverage warm latency:")
    for avg, model_id in sorted(summary):
        print(f"  {avg:6.0f} ms  {model_id}")


if __name__ == "__main__":
    main()
