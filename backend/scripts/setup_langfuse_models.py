"""Task 2.7 — register model prices in Langfuse so traces show real cost.

    python -m scripts.setup_langfuse_models

Langfuse computes cost as (tokens x price), and it only knows the prices of
models in its own table. Groq's gpt-oss-* models are not in it, so without
this every trace arrives with the token counts correct and the cost blank —
which is the one number Task 2.7 actually exists to produce.

Run this once per Langfuse project. It is needed again on any NEW project,
including a self-hosted instance later (see the Phase 4 note in
deploy/docker-compose.langfuse.yml) — model definitions live in the project,
not in this repo, so this script is the only record of them.

Prices are per token, from Groq's published per-million rates (verified
2026-09-02). Re-check them before trusting a cost figure months from now;
inference prices fall regularly and a stale table reads as confident and
wrong rather than as missing.
"""

import base64
import json
import re
import sys
import urllib.error
import urllib.request

from app.core.config import settings

# model name -> (USD per 1M input tokens, USD per 1M output tokens)
GROQ_PRICES_PER_MILLION = {
    "openai/gpt-oss-120b": (0.15, 0.60),  # main conversational LLM
    "openai/gpt-oss-20b": (0.075, 0.30),  # Task 1.6 query rewriting
}


def main() -> int:
    if not (settings.langfuse_host and settings.langfuse_public_key and settings.langfuse_secret_key):
        print("Langfuse settings are not configured — nothing to do.")
        return 1

    auth = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
    ).decode()

    failures = 0
    for name, (in_per_m, out_per_m) in GROQ_PRICES_PER_MILLION.items():
        body = json.dumps({
            "modelName": name,
            # Anchored and escaped: an unescaped name would let '.' and '/'
            # match models these prices do not apply to.
            "matchPattern": "(?i)^" + re.escape(name) + "$",
            "unit": "TOKENS",
            "inputPrice": in_per_m / 1_000_000,
            "outputPrice": out_per_m / 1_000_000,
        }).encode()

        req = urllib.request.Request(
            f"{settings.langfuse_host}/api/public/models",
            data=body,
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                json.load(r)
            print(f"  registered {name}: ${in_per_m}/1M in, ${out_per_m}/1M out")
        except urllib.error.HTTPError as e:
            print(f"  FAILED {name}: HTTP {e.code} {e.read().decode()[:200]}")
            failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
