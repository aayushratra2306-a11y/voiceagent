"""Task 2.8 — one command prints a score.

    python -m tests.rag_eval.run_eval

Runs every question in qa_set.py through the REAL retrieval path (query
rewrite -> hybrid search -> rerank, exactly what a live call does), checks
whether the expected page's chunk came back, and prints a score plus a
per-question breakdown of every miss. Run this before and after any
search-related change (chunking, thresholds, prompts, models) to see
immediately whether it helped, hurt, or did nothing — instead of guessing
from a handful of manual tests, which is how this project tuned the RAG
threshold by feel before this suite existed.
"""

import asyncio
import sys

from app.services.rag import query_context, rewrite_query
from tests.rag_eval.qa_set import BOT_ID, RAG_TEST_CASES


async def run() -> tuple[int, int, list[dict]]:
    results = []
    for case in RAG_TEST_CASES:
        question, expected_page = case["question"], case["expected_page"]
        search_query = await rewrite_query(question)
        retrieved, _sources = await query_context(BOT_ID, search_query)

        if expected_page is None:
            passed = retrieved == ""
        else:
            passed = f"[Page {expected_page}]" in retrieved

        results.append({
            "question": question,
            "expected_page": expected_page,
            "rewritten": search_query,
            "passed": passed,
            "retrieved_preview": retrieved[:120],
        })

    passed_count = sum(1 for r in results if r["passed"])
    return passed_count, len(results), results


def main():
    # This suite makes ~30 real retrieval calls; app.services.rag logs a
    # DEBUG line per candidate per call, which would otherwise bury the
    # score under thousands of lines. Only silenced for CLI use — importing
    # `run` (e.g. from test_rag_quality.py) leaves logging untouched for
    # whatever else is running in the same process.
    from loguru import logger
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    passed, total, results = asyncio.run(run())

    print(f"\n{'='*70}\nRAG TEST SUITE: {passed}/{total} passed ({100*passed/total:.0f}%)\n{'='*70}\n")

    misses = [r for r in results if not r["passed"]]
    if misses:
        print(f"MISSES ({len(misses)}):\n")
        for r in misses:
            expected = r["expected_page"] if r["expected_page"] is not None else "(should find nothing)"
            print(f"  Q: {r['question']!r}")
            print(f"     rewritten: {r['rewritten']!r}")
            print(f"     expected page: {expected}")
            print(f"     got: {r['retrieved_preview']!r}\n")
    else:
        print("No misses.\n")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
