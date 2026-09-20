import asyncio
import ipaddress
import json
import multiprocessing
import os
import socket
import sys
from contextlib import contextmanager
from dataclasses import replace
from functools import partial

import httpx
import uvicorn
from exa_py import AsyncExa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from llm_ocs_bridge import search as search_module
from llm_ocs_bridge.app import create_app


def restrict_network(event, args):
    if event == "socket.connect":
        address = args[1]
        if (
            isinstance(address, tuple)
            and not ipaddress.ip_address(address[0]).is_loopback
        ):
            raise PermissionError("Local E2E forbids external connections")
    elif event == "socket.getaddrinfo":
        if args[0] not in {"localhost", "127.0.0.1", "::1"}:
            raise PermissionError("Local E2E forbids external DNS")


def upstream_app(script):
    app = FastAPI()
    records = []
    gates = {}
    changed = asyncio.Condition()
    counts = {"exa": 0, "llm": 0, "closed": 0}

    @app.get("/observations")
    async def observations():
        return records

    @app.get("/wait/{provider}/{count}")
    async def wait(provider: str, count: int):
        async with changed:
            await asyncio.wait_for(
                changed.wait_for(lambda: counts[provider] >= count), 10
            )
        return records

    @app.post("/release/{name}")
    async def release(name: str):
        gates.setdefault(name, asyncio.Event()).set()
        return {"released": name}

    async def respond(request, provider):
        payload = await request.json()
        choices = script.get(provider, [{}])
        index = counts[provider]
        action = choices[min(index, len(choices) - 1)]
        record = {
            "provider": provider,
            "payload": payload,
            "status": action.get("status", 200),
        }
        records.append(record)
        async with changed:
            counts[provider] += 1
            changed.notify_all()
        if action.get("gate"):
            await gates.setdefault(action["gate"], asyncio.Event()).wait()
        if record["status"] != 200:
            return JSONResponse(
                {"error": "synthetic failure"}, status_code=record["status"]
            )
        if provider == "exa":
            return {
                "results": []
                if action.get("empty")
                else [
                    {
                        "id": "local-evidence",
                        "title": "water formula authority",
                        "url": "https://example.org/water",
                        "highlights": [
                            "water chemistry reference",
                            "water formula is H2O; LATE-HIGHLIGHT-ANSWER",
                        ],
                    }
                ]
            }
        body = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({"answer": action.get("answer", "B")})
                    }
                }
            ]
        }
        if action.get("stream"):

            async def stream():
                try:
                    yield b'{"choices":'
                    await gates.setdefault("stream", asyncio.Event()).wait()
                    yield json.dumps(body)[len('{"choices":') :].encode()
                finally:
                    record["stream_closed"] = True
                    async with changed:
                        counts["closed"] += 1
                        changed.notify_all()

            return StreamingResponse(stream(), media_type="application/json")
        return body

    @app.post("/search")
    async def search(request: Request):
        return await respond(request, "exa")

    @app.post("/v1/chat/completions")
    async def completion(request: Request):
        return await respond(request, "llm")

    return app


def bridge_app(settings, upstream):
    search_module.AsyncExa = partial(AsyncExa, api_base=upstream)
    app = create_app(replace(settings, openai_base_url=f"{upstream}/v1"))
    requests = {}

    @app.get("/_test/state")
    async def state():
        runtime = app.state.runtime
        return {"waiters": runtime._waiters, "tasks": len(runtime._answers)}

    @app.post("/_test/cancel/{name}")
    async def cancel(name: str):
        requests[name].cancel()
        return {"cancelled": name}

    @app.post("/_test/close")
    async def close():
        runtime = app.state.runtime
        exa = runtime.search_client._client
        llm = runtime.answerer._client if runtime.answerer else None
        await runtime.aclose()
        return {
            "tasks": len(runtime._answers),
            "exa_closed": exa is None or exa.client.is_closed,
            "llm_closed": llm is None or llm.is_closed,
        }

    async def tracked(scope, receive, send):
        if scope["type"] != "http" or scope["path"] != "/search":
            return await app(scope, receive, send)
        name = dict(scope["headers"]).get(b"x-test-request", b"anonymous").decode()
        requests[name] = asyncio.current_task()
        try:
            return await app(scope, receive, send)
        finally:
            requests.pop(name, None)

    return tracked


async def serve(connection, kind, config, upstream):
    app = upstream_app(config) if kind == "upstream" else bridge_app(config, upstream)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(
            uvicorn.Config(
                app, log_level="critical", access_log=False, timeout_graceful_shutdown=2
            )
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        while not server.started:
            if task.done():
                await task
                raise RuntimeError("Local server failed to start")
            await asyncio.sleep(0.01)
        connection.send(listener.getsockname()[1])
        try:
            await asyncio.to_thread(connection.recv)
        finally:
            server.should_exit = True
            await task


def run_server(connection, kind, config, upstream):
    for name in tuple(os.environ):
        if name.lower().endswith("_proxy"):
            os.environ.pop(name)
    sys.addaudithook(restrict_network)
    asyncio.run(serve(connection, kind, config, upstream))


@contextmanager
def local_server(kind, config, upstream=None):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=run_server, args=(child, kind, config, upstream))
    process.start()
    child.close()
    try:
        if not parent.poll(15):
            raise TimeoutError("Local server did not become ready")
        yield f"http://127.0.0.1:{parent.recv()}"
    finally:
        if process.is_alive():
            parent.send("stop")
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
        assert process.exitcode == 0, f"Local server exited with {process.exitcode}"


async def wait_state(client, **expected):
    async with asyncio.timeout(10):
        while True:
            state = (await client.get("/_test/state")).json()
            if all(state[name] == value for name, value in expected.items()):
                return state
            await asyncio.sleep(0.01)


def upstream_client(url):
    return httpx.AsyncClient(base_url=url, timeout=15, trust_env=False)
