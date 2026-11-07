from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import statistics
import time
from pathlib import Path

from llm_ocs_bridge.answers import format_search_context
from llm_ocs_bridge.cache import encode_search_results
from llm_ocs_bridge.models import OCSQuery, SearchResult
from llm_ocs_bridge.search import ExaSearch, question_terms


def measure(results, terms):
    started = time.perf_counter()
    scores = [
        ExaSearch._semantic_score(terms, f"{item.title}\n{item.snippet}")
        for item in results
    ]
    scored = time.perf_counter()
    payload = encode_search_results(results)
    encoded = time.perf_counter()
    context = format_search_context(results, 131072)
    finished = time.perf_counter()
    return {
        "score_ms": (scored - started) * 1000,
        "encode_ms": (encoded - scored) * 1000,
        "context_ms": (finished - encoded) * 1000,
        "total_ms": (finished - started) * 1000,
        "scores": scores,
        "payload_bytes": len(payload.encode()),
        "context_bytes": len(context.encode()),
        "payload_sha256": hashlib.sha256(payload.encode()).hexdigest(),
        "context_sha256": hashlib.sha256(context.encode()).hexdigest(),
    }


async def concurrent(results, terms):
    started = time.perf_counter()
    latencies = []
    loop_lags = []

    async def heartbeat():
        before = time.perf_counter()
        await asyncio.sleep(0.005)
        loop_lags.append(max(0, (time.perf_counter() - before - 0.005) * 1000))

    async def request():
        measure(results, terms)
        latencies.append((time.perf_counter() - started) * 1000)

    await asyncio.gather(heartbeat(), *(request() for _ in range(8)))
    return {
        "requests": 8,
        "p50_ms": statistics.median(latencies),
        "p95_ms": max(latencies),
        "loop_lag_ms": max(loop_lags),
    }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    samples = []
    for size, long_question in ((1000, False), (100000, False), (100000, True)):
        title = (
            "".join(chr(0x4E00 + index) for index in range(8192))
            if long_question
            else "光合作用释放什么气体？"
        )
        terms = question_terms(OCSQuery(title, ("A.氧气", "B.氮气")))
        snippet = ("完全无关的长篇材料" * size)[:size] + "光合作用释放氧气"
        results = [
            SearchResult(f"证据{index}", f"https://example.org/{index}", snippet)
            for index in range(10)
        ]
        for round_number in range(args.rounds):
            samples.append(
                {
                    "round": round_number + 1,
                    "characters": len(snippet),
                    "query_terms": len(terms),
                    **measure(results, terms),
                    "concurrent": await concurrent(results, terms),
                }
            )
    args.output.write_text(
        json.dumps({"python": platform.python_version(), "samples": samples}, indent=2)
    )
    print(args.output)


if __name__ == "__main__":
    asyncio.run(main())
