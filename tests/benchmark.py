import argparse
import asyncio
import json
import multiprocessing
import platform
import socket
import sqlite3
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from importlib.metadata import version
from multiprocessing.connection import Connection
from pathlib import Path
from time import perf_counter

import httpx
import uvicorn
from dotenv import dotenv_values
from fastapi import FastAPI

from llm_ocs_bridge.app import create_app
from llm_ocs_bridge.cache import answer_key
from llm_ocs_bridge.config import load_settings
from llm_ocs_bridge.llm import LLMAnswerer
from llm_ocs_bridge.models import OCSQuery, SearchResult

_QUERY = OCSQuery("water", ("A.H2O", "B.CO2"), "single")
_FALLBACK_QUERY = OCSQuery("fallback water", _QUERY.options, "single")
_TOKEN = "benchmark-token"


def serve(connection: Connection, directory: str) -> None:
    asyncio.run(serve_async(connection, directory))


async def serve_async(connection: Connection, directory: str) -> None:
    values = dotenv_values(Path(__file__).resolve().parents[1] / ".env.example")
    values.update(
        CACHE_PATH=str(Path(directory) / "benchmark.sqlite3"),
        ACCESS_TOKEN=_TOKEN,
        OPENAI_API_KEY="benchmark-key",
        EXA_API_KEY="benchmark-key",
        MAX_CACHE_BYTES=str(1024**3),
        MAX_ACTIVE_ANSWERS="128",
        MAX_ANSWER_WAITERS="256",
        LLM_FALLBACK_ENABLED="true",
    )
    settings = load_settings(environ=values)
    app = create_app(settings)
    original_lifespan = app.router.lifespan_context

    async def search(query: OCSQuery, limit: int | None):
        if query == _FALLBACK_QUERY:
            return [], 0
        await asyncio.sleep(0.1)
        return [SearchResult(query.title, "https://example.com", "water H2O", "exa")], 1

    async def model(
        messages: list[dict[str, str]], on_attempt: Callable[[], Awaitable[None]]
    ) -> str:
        await on_attempt()
        await asyncio.sleep(0.2)
        return '{"answer":"A"}'

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        async with original_lifespan(application):
            runtime = application.state.runtime
            runtime.search_client.search = search
            runtime.answerer = LLMAnswerer(settings)
            runtime.answerer._call_model = model
            await runtime.answer_cache.set(
                answer_key(_QUERY, "search", settings),
                _QUERY,
                settings.openai_model,
                settings.prompt_version,
                "search",
                "A",
            )
            await runtime.answer_cache.set(
                answer_key(_FALLBACK_QUERY, "fallback", settings),
                _FALLBACK_QUERY,
                settings.openai_model,
                settings.prompt_version,
                "fallback",
                "A",
            )
            original_read = runtime.db.read
            async with original_read() as database:
                original_execute = database._execute

            @asynccontextmanager
            async def read():
                started = perf_counter()
                async with original_read() as database:
                    waits.append((perf_counter() - started) * 1000)
                    yield database

            async def execute(function, *args, **kwargs):
                operations["worker"] += 1
                operations["commits"] += function.__name__ == "commit"
                return await original_execute(function, *args, **kwargs)

            runtime.db.read = read
            database._execute = execute
            monitor = asyncio.create_task(heartbeat())
            try:
                yield
            finally:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)

    waits, lags = [], []
    operations = {"worker": 0, "commits": 0}

    async def heartbeat():
        while True:
            started = perf_counter()
            await asyncio.sleep(0.005)
            lags.append(max(0, (perf_counter() - started - 0.005) * 1000))

    @app.get("/benchmark-observations")
    async def observations():
        waits.sort()
        snapshot = {
            **operations,
            "lock_wait_p95_ms": waits[int((len(waits) - 1) * 0.95)] if waits else 0,
            "lock_wait_p99_ms": waits[int((len(waits) - 1) * 0.99)] if waits else 0,
            "loop_lag_max_ms": max(lags, default=0),
        }
        waits.clear()
        lags.clear()
        operations.update(worker=0, commits=0)
        return snapshot

    app.router.lifespan_context = lifespan
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
                raise RuntimeError("Benchmark server did not start")
            await asyncio.sleep(0.01)
        connection.send(listener.getsockname()[1])
        try:
            await asyncio.to_thread(connection.recv)
        finally:
            server.should_exit = True
            await task


async def measure(
    client: httpx.AsyncClient, case: str, count: int, concurrency: int, trial: int
) -> dict:
    gate = asyncio.Semaphore(concurrency)
    durations: list[float] = []
    successes = 0
    before = (await client.get("/stats")).json()["counters"]
    await client.get("/benchmark-observations")

    async def request(ordinal: int) -> None:
        nonlocal successes
        title = _QUERY.title
        if case == "fallback":
            title = _FALLBACK_QUERY.title
        elif case == "unique":
            title = f"water {trial} {ordinal}"
        elif case == "identical":
            title = f"water repeated {trial}"
        async with gate:
            started = perf_counter()
            response = await client.post(
                "/search",
                json={
                    "title": title,
                    "options": list(_QUERY.options),
                    "type": "single",
                },
            )
            response.raise_for_status()
            durations.append((perf_counter() - started) * 1000)
            successes += response.json().get("answer") == "A"

    started = perf_counter()
    await asyncio.gather(*(request(ordinal) for ordinal in range(count)))
    elapsed = perf_counter() - started
    observations = (await client.get("/benchmark-observations")).json()
    after = (await client.get("/stats")).json()["counters"]
    durations.sort()
    return {
        "case": case,
        "trial": trial,
        "requests": count,
        "concurrency": concurrency,
        "successes": successes,
        "seconds": elapsed,
        "rps": count / elapsed,
        "p50_ms": durations[int((count - 1) * 0.5)],
        "p95_ms": durations[int((count - 1) * 0.95)],
        "search_calls": after["search_requests"] - before["search_requests"],
        "llm_calls": after["llm_calls"] - before["llm_calls"],
        "observations": observations,
    }


async def benchmark(port: int, rounds: int, requests: int) -> list[dict]:
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}",
        headers={"X-Access-Token": _TOKEN},
        limits=httpx.Limits(max_connections=256, max_keepalive_connections=256),
        timeout=120,
        trust_env=False,
    ) as client:
        await measure(client, "cached", 20, 1, 0)
        results = []
        for trial in range(rounds):
            for case in ("cached", "fallback"):
                for concurrency in (1, 8, 32, 128):
                    results.append(
                        await measure(client, case, requests, concurrency, trial)
                    )
            for case in ("unique", "identical"):
                results.append(await measure(client, case, 32, 32, trial))
        return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--requests", type=int, default=600)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.rounds < 1 or args.requests < 1:
        parser.error("rounds and requests must be positive")
    context = multiprocessing.get_context("spawn")
    connection, child_connection = context.Pipe()
    with tempfile.TemporaryDirectory(dir=Path.home() / ".cache") as directory:
        process = context.Process(target=serve, args=(child_connection, directory))
        process.start()
        try:
            if not connection.poll(30):
                raise TimeoutError("Benchmark server did not become ready")
            port = connection.recv()
            results = asyncio.run(benchmark(port, args.rounds, args.requests))
        finally:
            connection.send("stop")
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join()
            connection.close()
            child_connection.close()
    if process.exitcode:
        raise RuntimeError(f"Benchmark server exited with {process.exitcode}")
    args.output.write_text(
        json.dumps(
            {
                "python": platform.python_version(),
                "sqlite": sqlite3.sqlite_version,
                "packages": {
                    package: version(package)
                    for package in ("aiosqlite", "httpx", "uvicorn", "fastapi")
                },
                "upstream": "synthetic async search=100ms, model=200ms",
                "separate_client_process": True,
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
