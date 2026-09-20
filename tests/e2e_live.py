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
        provider = "llm" if request.url.host == llm_host else "exa"
        attempt = {"provider": provider, "status": None}
        attempts.append(attempt)
        response = await original_send(client, request, *args, **kwargs)
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


async def check(port, mode):
    unauthorized = await curl(port, "/search", _QUERY, "incorrect-token")
    assert unauthorized["status"] == 200 and unauthorized["body"]["code"] == 0
    assert unauthorized["provider"] is None
    started = perf_counter()
    cold = await asyncio.gather(*(curl(port, "/search", _QUERY) for _ in range(8)))
    cold_ms = (perf_counter() - started) * 1000
    warm = await curl(port, "/search", _QUERY)
    attempts = (await curl(port, "/live-attempts"))["body"]
    counters = (await curl(port, "/stats"))["body"]["counters"]
    result = {
        "mode": mode,
        "cold_ms": cold_ms,
        "cold": cold,
        "warm": warm,
        "attempts": attempts,
        "counters": counters,
    }
    result["passed"] = (
        all(row["status"] == 200 and row["body"].get("answer") == "A" for row in cold)
        and warm["body"].get("answer") == "A"
        and warm["provider"] is None
        and any(
            row["provider"] == ("exa" if mode == "search" else "llm-fallback")
            for row in cold
        )
        and counters["search_requests"] == (1 if mode == "search" else 0)
        and counters["llm_calls"] == 1
    )
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
                results.append(asyncio.run(check(connection.recv(), mode)))
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
    assert all(row["passed"] for row in results), (
        "Live E2E failed; inspect safe result JSON"
    )


if __name__ == "__main__":
    main()
