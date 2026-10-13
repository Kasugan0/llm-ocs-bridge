from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import multiprocessing
import socket
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from time import perf_counter

import httpx
import uvicorn

from llm_ocs_bridge.app import create_app
from llm_ocs_bridge.config import load_settings
from llm_ocs_bridge.llm import normalize_answer_payload, validate_answer

_TOKEN = "isolated-live-e2e"
_QUERY = {
    "title": "Python 编程语言的主要创始人是谁？",
    "options": ["A. Guido van Rossum", "B. Linus Torvalds"],
    "type": "single",
}
_MAX_ATTEMPTS = 16
_CASES = (
    (
        "non-first-single",
        {
            "title": "Python 编程语言的主要创始人是谁？",
            "options": ["A. Linus Torvalds", "B. Guido van Rossum"],
            "type": "single",
        },
        "B",
    ),
    (
        "multiple",
        {
            "title": "以下哪些元素属于稀有气体（noble gases）？",
            "options": ["A. 氦 He", "B. 氧 O", "C. 氖 Ne", "D. 氮 N"],
            "type": "multiple",
        },
        "A#C",
    ),
    (
        "judgement",
        {
            "title": "水的化学式是 H2O。",
            "type": "judgement",
            "options": ["正确", "错误"],
        },
        "正确",
    ),
    (
        "completion",
        {
            "title": "元素周期表中，氢元素的化学符号是____，氧元素的化学符号是____。",
            "options": [],
            "type": "completion",
        },
        "H#O",
    ),
)


def reserve_attempt(budget):
    with budget.get_lock():
        if budget.value >= _MAX_ATTEMPTS:
            raise RuntimeError("Live E2E attempt budget exhausted")
        budget.value += 1


def instrument_transport(settings, budget, attempts):
    original = httpx.AsyncHTTPTransport.handle_async_request
    llm_url = httpx.URL(settings.openai_base_url)

    async def send(transport, request):
        if (request.url.host, request.url.port) == (llm_url.host, llm_url.port):
            provider = "llm"
        elif request.url.host == "api.exa.ai" and request.url.scheme == "https":
            provider = "exa"
        else:
            raise RuntimeError("Unexpected live upstream destination")
        reserve_attempt(budget)
        attempt = {"provider": provider, "status": None}
        attempts.append(attempt)
        try:
            response = await original(transport, request)
        except httpx.TransportError as exc:
            attempt["error"] = type(exc).__name__
            attempt["retryable"] = True
            raise
        attempt["status"] = response.status_code
        if provider == "llm" and response.status_code == 200:
            body = await response.aread()
            attempt["invalid_answer"] = not _valid_success(body, settings)
        return response

    httpx.AsyncHTTPTransport.handle_async_request = send
    return original


def _valid_success(body, settings):
    try:
        content = json.loads(body)["choices"][0]["message"]["content"]
        from llm_ocs_bridge.llm import LLMAnswerer

        payload = LLMAnswerer._parse_payload(content)
        answer = normalize_answer_payload(payload, _QUERY["type"])
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError):
        return False
    return validate_answer(answer, _QUERY["type"], tuple(_QUERY["options"]))


def failure(mode, case, reason):
    return {
        "mode": mode,
        "case": case,
        "passed": False,
        "failures": [reason],
        "attempts": [],
    }


def serve(connection, settings, budget):
    asyncio.run(serve_async(connection, settings, budget))


async def serve_async(connection, settings, budget):
    app = create_app(settings)
    attempts = []
    original = instrument_transport(settings, budget, attempts)

    @app.get("/live-attempts")
    async def observed_attempts():
        return attempts

    with socket.socket() as listener:
        listener.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="error", access_log=False)
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        while not server.started:
            if task.done():
                await task
                raise RuntimeError("Live E2E server failed to start")
            await asyncio.sleep(0.01)
        connection.send(listener.getsockname()[1])
        try:
            await asyncio.to_thread(connection.recv)
        finally:
            server.should_exit = True
            await task
            httpx.AsyncHTTPTransport.handle_async_request = original


async def curl(port, path, payload=None, token=_TOKEN):
    command = [
        "curl",
        "--silent",
        "--show-error",
        "--noproxy",
        "*",
        "--max-time",
        "150",
        "--dump-header",
        "-",
        "--header",
        f"X-Access-Token: {token}",
    ]
    if payload is not None:
        command.extend(
            ["--header", "Content-Type: application/json", "--data-binary", "@-"]
        )
    command.append(f"http://127.0.0.1:{port}{path}")
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, _ = await process.communicate(
        json.dumps(payload).encode() if payload else b""
    )
    if process.returncode:
        raise RuntimeError(f"curl exited with {process.returncode}")
    header_bytes, body = output.split(b"\r\n\r\n", 1)
    lines = header_bytes.decode().split("\r\n")
    headers = dict(line.lower().split(": ", 1) for line in lines[1:] if ": " in line)
    return {
        "status": int(lines[0].split()[1]),
        "body": json.loads(body),
        "provider": headers.get("x-search-provider"),
    }


async def snapshot(port):
    attempts = await curl(port, "/live-attempts")
    stats = await curl(port, "/stats")
    if attempts["status"] != 200 or stats["status"] != 200:
        raise RuntimeError("Cannot collect live observations")
    return {"attempts": attempts["body"], "counters": stats["body"]["counters"]}


def validate_case(result, expected_answer="A", max_retries=2):
    failures = []
    provider = "exa" if result["mode"] == "search" else "llm-fallback"
    unauthorized = result["unauthorized"]
    if not (
        unauthorized["status"] == 200
        and unauthorized["body"].get("code") == 0
        and unauthorized["provider"] is None
    ):
        failures.append("unauthorized response violates OCS contract")
    if not result["cold"]:
        failures.append("missing cold responses")
    for phase, rows in (("cold", result["cold"]), ("warm", [result["warm"]])):
        allowed = {None, provider} if phase == "cold" else {None}
        for index, row in enumerate(rows):
            if not (
                row["status"] == 200
                and row["body"].get("code") == 1
                and row["body"].get("answer") == expected_answer
                and row["provider"] in allowed
            ):
                failures.append(f"{phase}[{index}] violates answer contract")
    if not any(row["provider"] == provider for row in result["cold"]):
        failures.append("cold responses lack the expected provider")

    stages = result["snapshots"]
    names = ("before", "auth", "cold", "warm")
    for previous, current in zip(names, names[1:], strict=False):
        before, after = stages[previous], stages[current]
        if after["attempts"][: len(before["attempts"])] != before["attempts"]:
            failures.append(f"{current} lost prior attempt records")
        if current in {"auth", "warm"}:
            if before["attempts"] != after["attempts"]:
                failures.append(f"{current} triggered upstream attempts")
            for counter in ("search_requests", "llm_calls"):
                if before["counters"].get(counter, 0) != after["counters"].get(
                    counter, 0
                ):
                    failures.append(f"{current} changed {counter}")

    before, cold = stages["auth"], stages["cold"]
    cold_hits = cold["counters"].get("answer_cache_hits", 0) - before["counters"].get(
        "answer_cache_hits", 0
    )
    if any(row["provider"] is None for row in result["cold"]) and cold_hits < 1:
        failures.append("cold cache response lacks a persistent cache hit")
    warm_hits = stages["warm"]["counters"].get("answer_cache_hits", 0) - cold[
        "counters"
    ].get("answer_cache_hits", 0)
    if warm_hits != 1:
        failures.append("warm response lacks one persistent cache hit")
    attempts = cold["attempts"][len(before["attempts"]) :]
    if any(attempt["provider"] not in {"exa", "llm"} for attempt in attempts):
        failures.append("unknown upstream in attempt records")
    expected_searches = int(result["mode"] == "search")
    exa = [attempt for attempt in attempts if attempt["provider"] == "exa"]
    llm = [attempt for attempt in attempts if attempt["provider"] == "llm"]
    if len(exa) != expected_searches or any(row["status"] != 200 for row in exa):
        failures.append("missing or unsuccessful Exa attempt")
    if not 1 <= len(llm) <= max_retries + 1 or llm[-1]["status"] != 200:
        failures.append("missing successful LLM attempt or retry limit exceeded")
    for attempt in llm[:-1]:
        status = attempt["status"]
        retryable = (
            status == 429
            or isinstance(status, int)
            and status >= 500
            or status is None
            and attempt.get("retryable") is True
            or status == 200
            and attempt.get("invalid_answer") is True
        )
        if not retryable:
            failures.append("LLM retried a deterministic failure")
    expected = {"search_requests": expected_searches, "llm_calls": len(llm)}
    for counter, count in expected.items():
        delta = cold["counters"].get(counter, 0) - before["counters"].get(counter, 0)
        if delta != count:
            failures.append(f"{counter} disagrees with observed work")
    return failures


async def check(
    port,
    mode,
    query=None,
    expected_answer="A",
    max_retries=2,
    case="python-creator",
    cold_count=1,
):
    query = _QUERY if query is None else query
    snapshots = {"before": await snapshot(port)}
    unauthorized = await curl(port, "/search", query, "incorrect-token")
    snapshots["auth"] = await snapshot(port)
    started = perf_counter()
    cold = await asyncio.gather(
        *(curl(port, "/search", query) for _ in range(cold_count))
    )
    cold_ms = (perf_counter() - started) * 1000
    snapshots["cold"] = await snapshot(port)
    warm = await curl(port, "/search", query)
    snapshots["warm"] = await snapshot(port)
    result = {
        "mode": mode,
        "case": case,
        "unauthorized": unauthorized,
        "cold_ms": cold_ms,
        "cold": cold,
        "warm": warm,
        "snapshots": snapshots,
        "attempts": snapshots["warm"]["attempts"],
        "counters": snapshots["warm"]["counters"],
    }
    result["failures"] = validate_case(result, expected_answer, max_retries)
    result["passed"] = not result["failures"]
    return result


def check_cases(port, mode, settings, budget):
    results = []
    for case, query, expected in _CASES:
        if budget.value >= _MAX_ATTEMPTS:
            results.append(failure(mode, case, "attempt budget exhausted"))
            break
        try:
            results.append(
                asyncio.run(
                    check(port, mode, query, expected, settings.max_llm_retries, case)
                )
            )
        except Exception as exc:
            results.append(failure(mode, case, type(exc).__name__))
            break
    return results


def check_mode(context, settings, mode, budget):
    with tempfile.TemporaryDirectory(dir=Path.home() / ".cache") as directory:
        live = replace(
            settings,
            cache_path=str(Path(directory) / "live.sqlite3"),
            access_token=_TOKEN,
            exa_api_key=settings.exa_api_key if mode == "search" else "",
            llm_fallback_enabled=mode == "fallback",
        )
        connection, child = context.Pipe()
        process = context.Process(target=serve, args=(child, live, budget))
        process.start()
        child.close()
        results = []
        try:
            if not connection.poll(30):
                raise TimeoutError("Live E2E server did not become ready")
            results = check_cases(connection.recv(), mode, live, budget)
        except Exception as exc:
            results.append(failure(mode, "startup", type(exc).__name__))
        finally:
            if process.is_alive():
                connection.send("stop")
            process.join(20)
            if process.is_alive():
                process.terminate()
                process.join()
            connection.close()
        if process.exitcode != 0:
            results.append(failure(mode, "shutdown", "unclean server exit"))
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-paid", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.confirm_paid:
        parser.error("Real upstream calls require --confirm-paid")
    root = Path(__file__).resolve().parents[1]
    env_path = root / ".env"
    before = hashlib.sha256(env_path.read_bytes()).digest()
    settings = load_settings(env_path)
    assert settings.openai_api_key and settings.exa_api_key, (
        "Existing keys are required"
    )
    context = multiprocessing.get_context("spawn")
    budget = context.Value("i", 0)
    results = []
    for mode in ("search", "fallback"):
        results.extend(check_mode(context, settings, mode, budget))
        if budget.value >= _MAX_ATTEMPTS:
            break
    unchanged = hashlib.sha256(env_path.read_bytes()).digest() == before
    report = {
        "revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "attempt_budget": _MAX_ATTEMPTS,
        "attempts_used": budget.value,
        "env_unchanged": unchanged,
        "results": results,
        "passed": unchanged
        and len(results) == 8
        and all(row["passed"] for row in results),
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            [
                {
                    "mode": row["mode"],
                    "passed": row["passed"],
                    "attempts": row["attempts"],
                }
                for row in results
            ]
        )
    )
    if not report["passed"]:
        raise SystemExit("Live E2E failed; inspect safe result JSON")


if __name__ == "__main__":
    main()
