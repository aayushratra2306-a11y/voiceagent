# Task 2.8 — regression guard. Baseline established live 2026-08-31:
# 28/32 (88%). The 4 known misses at baseline are real, understood
# limitations (two land on a page adjacent to the right one — a chunking-
# granularity effect, not a wrong answer's worth of drift; two paraphrased
# queries scored under the rerank threshold) — see run_eval.py's own output
# for the live breakdown. This asserts against a MARGIN below that (24/32,
# 75%), not the exact baseline: RAG retrieval has real day-to-day variance
# (embedding/rerank model updates, index state) that shouldn't fail CI on
# its own — the point is catching an actual regression (a change that
# breaks retrieval), not chasing every point of natural fluctuation.
import pytest

from tests.rag_eval.run_eval import run

pytestmark = pytest.mark.asyncio(loop_scope="session")

MINIMUM_ACCEPTABLE_SCORE = 24  # out of len(RAG_TEST_CASES) == 32


async def test_rag_quality_meets_baseline():
    passed, total, results = await run()
    misses = [r["question"] for r in results if not r["passed"]]
    assert passed >= MINIMUM_ACCEPTABLE_SCORE, (
        f"RAG quality regressed: {passed}/{total} passed (baseline: 28/32). "
        f"Newly failing or still-failing questions: {misses}"
    )
