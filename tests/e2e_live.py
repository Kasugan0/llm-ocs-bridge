from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import multiprocessing
import socket
import tempfile
from dataclasses import replace
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit

import httpx
import uvicorn

from llm_ocs_bridge.app import create_app
from llm_ocs_bridge.config import load_settings

_TOKEN = "isolated-live-e2e"
_QUERY = {
    "title": "Python 编程语言的主要创始人是谁？",
    "options": ["A. Guido van Rossum", "B. Linus Torvalds"],
    "type": "single",
}


def serve(connection, settings):
    asyncio.run(serve_async(connection, settings))


async def serve_async(connection, settings):
    app = create_app(settings)
    attempts = []
    original_send = httpx.AsyncClient.send
    llm_host = urlsplit(settings.openai_base_url).hostname

    async def send(client, request, *args, **kwargs):
        if request.url.host == llm_host:
            provider = "llm"
        elif request.url.host == "api.exa.ai":
            provider = "exa"
        else:
            raise RuntimeError("Unexpected live upstream destination")
        attempt = {"provider": provider, "status": None}
        attempts.append(attempt)
        try:
            response = await original_send(client, request, *args, **kwargs)
        except httpx.TransportError as exc:
            attempt["error"] = type(exc).__name__
            attempt["retryable"] = True
            raise
        attempt["status"] = response.status_code
        return response

    httpx.AsyncClient.send = send

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
            status == 200
            or status == 429
            or isinstance(status, int)
            and status >= 500
            or status is None
            and attempt.get("retryable") is True
        )
        if not retryable:
            failures.append("LLM retried a deterministic failure")
    expected = {"search_requests": expected_searches, "llm_calls": len(llm)}
    for counter, count in expected.items():
        delta = cold["counters"].get(counter, 0) - before["counters"].get(counter, 0)
        if delta != count:
            failures.append(f"{counter} disagrees with observed work")
    return failures


async def check(port, mode, query=None, expected_answer="A", max_retries=2):
    query = _QUERY if query is None else query
    snapshots = {"before": await snapshot(port)}
    unauthorized = await curl(port, "/search", query, "incorrect-token")
    snapshots["auth"] = await snapshot(port)
    started = perf_counter()
    cold = await asyncio.gather(*(curl(port, "/search", query) for _ in range(8)))
    cold_ms = (perf_counter() - started) * 1000
    snapshots["cold"] = await snapshot(port)
    warm = await curl(port, "/search", query)
    snapshots["warm"] = await snapshot(port)
    result = {
        "mode": mode,
        "case": "python-creator",
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-paid", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.confirm_paid:
        parser.error("Real upstream calls require --confirm-paid")
    env_path = Path.cwd() / ".env"
    before = hashlib.sha256(env_path.read_bytes()).digest()
    settings = load_settings()
    assert settings.openai_api_key and settings.exa_api_key, (
        "Existing keys are required"
    )
    context = multiprocessing.get_context("spawn")
    results = []
    for mode in ("search", "fallback"):
        with tempfile.TemporaryDirectory(dir=Path.home() / ".cache") as directory:
            live = replace(
                settings,
                cache_path=str(Path(directory) / "live.sqlite3"),
                access_token=_TOKEN,
                exa_api_key=settings.exa_api_key if mode == "search" else "",
                llm_fallback_enabled=mode == "fallback",
            )
            connection, child = context.Pipe()
            process = context.Process(target=serve, args=(child, live))
            process.start()
            try:
                if not connection.poll(30):
                    raise TimeoutError("Live E2E server did not become ready")
                results.append(
                    asyncio.run(
                        check(connection.recv(), mode, max_retries=live.max_llm_retries)
                    )
                )
            finally:
                connection.send("stop")
                process.join(20)
                if process.is_alive():
                    process.terminate()
                    process.join()
                connection.close()
                child.close()
            assert process.exitcode == 0, "Live E2E server did not exit cleanly"
    assert hashlib.sha256(env_path.read_bytes()).digest() == before, ".env changed"
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2))
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
    if not all(row["passed"] for row in results):
        raise SystemExit("Live E2E failed; inspect safe result JSON")


if __name__ == "__main__":
    main()
