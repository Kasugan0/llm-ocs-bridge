from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import multiprocessing
import os
import socket
import struct
import subprocess
import tempfile
import zlib
from dataclasses import replace
from pathlib import Path
from time import perf_counter

import httpx
import uvicorn

from llm_ocs_bridge.answers import normalize_answer_payload, validate_answer
from llm_ocs_bridge.app import create_app
from llm_ocs_bridge.config import Settings, load_settings

_TOKEN = "isolated-live-e2e"
_QUERY = {
    "title": "Python 编程语言的主要创始人是谁？",
    "options": ["A. Guido van Rossum", "B. Linus Torvalds"],
    "type": "single",
}


def _solid_png(red: int, green: int, blue: int, size: int = 64) -> str:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    rows = b"".join(b"\x00" + bytes((red, green, blue)) * size for _ in range(size))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _fixture(name: str) -> str:
    path = Path(__file__).parent / "fixtures" / name
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


_RED_SQUARE = _solid_png(220, 20, 20)
_CALCULUS_IMAGE = _fixture("calculus-integral.png")


_MAX_ATTEMPTS = 24
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
        '["H", "O"]',
    ),
    (
        "image-logo",
        {
            "title": "图中展示的是哪家公司的标志？"
            '<img src="https://www.baidu.com/img/bd_logo1.png">',
            "options": [
                "A. 谷歌 Google",
                "B. 百度 Baidu",
                "C. 腾讯 Tencent",
                "D. 阿里巴巴 Alibaba",
            ],
            "type": "single",
        },
        "B",
    ),
    (
        "image-ocs5-color",
        {
            "title": "图中的正方形是什么颜色？",
            "options": ["A. 红色", "B. 绿色", "C. 蓝色"],
            "type": "single",
            "images": [_RED_SQUARE],
            "suggestion_title": "图中的正方形是什么颜色？ [图片1]",
            "suggestion_options": "A. 红色\nB. 绿色\nC. 蓝色",
        },
        "A",
    ),
    (
        "image-ocs5-calculus",
        {
            "title": "计算下面的题目：",
            "options": ["A. 1/4", "B. 1/3", "C. 1/2", "D. 1"],
            "type": "single",
            "images": [_CALCULUS_IMAGE],
            "suggestion_title": "计算下面的题目： [图片1]",
            "suggestion_options": "A. 1/4\nB. 1/3\nC. 1/2\nD. 1",
        },
        "B",
    ),
)

# 每个拒绝用例只发一次请求，必须零出站调用：不消耗预算，也不依赖 search/fallback 模式。
_IMAGE_QUERY = {
    "title": "图中是什么公式",
    "options": ["A.甲", "B.乙"],
    "type": "single",
    "images": [_RED_SQUARE],
    "suggestion_title": "图中是什么公式 [图片1]",
    "suggestion_options": "A.甲\nB.乙",
}
_OVERSIZED_BODY = b'{"title":"' + b"a" * 4096 + b'"}'
_OVERSIZED_IMAGE = "data:image/png;base64," + "A" * 4096
_IMAGE_MESSAGES = {
    "image-not-data-url": "图片必须是 png、jpeg、gif 或 webp 的 base64 data URL",
    "image-bad-base64": "图片 base64 编码不合法",
    "image-too-many": "图片数量超过限制",
    "image-too-large": "图片超过大小限制",
}


def _rejections(settings: Settings) -> tuple[tuple[str, bytes, str], ...]:
    def body(payload: dict[str, object]) -> bytes:
        return json.dumps(payload).encode()

    return (
        ("body-invalid-json", b"{not json", "请求体必须是合法 JSON"),
        ("body-not-object", b"[]", "请求体必须是 JSON 对象"),
        ("body-not-utf8", b"\xff", "请求体必须是 UTF-8 编码"),
        (
            "image-not-data-url",
            body(_IMAGE_QUERY | {"images": ["https://example.org/a.png"]}),
            _IMAGE_MESSAGES["image-not-data-url"],
        ),
        (
            "image-bad-base64",
            body(_IMAGE_QUERY | {"images": ["data:image/png;base64,iVBORw0KGgo=="]}),
            _IMAGE_MESSAGES["image-bad-base64"],
        ),
        (
            "image-too-many",
            body(_IMAGE_QUERY | {"images": [_RED_SQUARE] * (settings.max_images + 1)}),
            _IMAGE_MESSAGES["image-too-many"],
        ),
        (
            "placeholder-beyond-images",
            body(_IMAGE_QUERY | {"suggestion_title": "图中是什么公式 [图片2]"}),
            "图片占位符超出图片数量",
        ),
        (
            "unknown-type",
            body(_QUERY | {"type": "matrix"}),
            "不支持的题型",
        ),
        (
            "single-without-options",
            body({"title": "题目", "type": "single"}),
            "单选题和多选题必须提供有效选项",
        ),
        (
            "multiple-without-options",
            body({"title": "题目", "type": "multiple"}),
            "单选题和多选题必须提供有效选项",
        ),
        ("empty-title", body({"title": "   "}), "题目不能为空"),
    )


def _auth_case():
    return (("auth", json.dumps(_IMAGE_QUERY).encode(), "无效的访问令牌"),)


_AUTH_CASES = {"auth"}


def _image_rejections(settings: Settings) -> tuple[tuple[str, bytes, str], ...]:
    return tuple(
        (name, raw, message)
        for name, raw, message in _rejections(settings)
        if name in _IMAGE_MESSAGES
    )


def _without_vision(settings: Settings) -> Settings:
    return replace(settings, vision_model="")


def _with_tight_body_limit(settings: Settings) -> Settings:
    return replace(settings, max_request_bytes=len(_OVERSIZED_BODY) - 1)


def _with_tight_image_limit(settings: Settings) -> Settings:
    return replace(settings, max_image_bytes=1024)


# 缺少视觉模型时，图片题在解析之后才失败：改用无文字题干，确保拒绝发生在任何检索之前。
_VISION_QUERY = {
    "title": "",
    "options": [],
    "type": "",
    "images": [_RED_SQUARE],
    "suggestion_title": "[图片1]",
}


def drop_proxy_env() -> None:
    for name in tuple(os.environ):
        if name.lower().endswith("_proxy"):
            os.environ.pop(name)


def reserve_attempt(budget):
    with budget.get_lock():
        if budget.value >= _MAX_ATTEMPTS:
            raise RuntimeError("Live E2E attempt budget exhausted")
        budget.value += 1


def instrument_transport(settings, budget, attempts, current):
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
            attempt["invalid_answer"] = not _valid_success(body, current["query"])
        return response

    httpx.AsyncHTTPTransport.handle_async_request = send
    return original


def _valid_success(body, query):
    try:
        content = json.loads(body)["choices"][0]["message"]["content"]
        from llm_ocs_bridge.answers import parse_payload

        payload = parse_payload(content)
        answer = normalize_answer_payload(
            payload, query["type"], tuple(query.get("options", ()))
        )
        return validate_answer(answer, query["type"], tuple(query.get("options", ())))
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError):
        return False


def failure(mode, case, reason):
    return {
        "mode": mode,
        "case": case,
        "passed": False,
        "failures": [reason],
        "attempts": [],
    }


def serve(connection, settings, budget, token=_TOKEN):
    drop_proxy_env()
    asyncio.run(serve_async(connection, settings, budget, token))


async def serve_async(connection, settings, budget, token=_TOKEN):
    app = create_app(replace(settings, access_token=token))
    attempts = []
    current = {"query": _QUERY}
    original = instrument_transport(settings, budget, attempts, current)

    @app.get("/live-attempts")
    async def observed_attempts():
        return attempts

    @app.post("/live-query")
    async def declare_query(query: dict):
        current["query"] = query
        return query

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


async def curl(port, path, payload=None, token=_TOKEN, data=None):
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
    if payload is not None or data is not None:
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
    if data is not None:
        sent = data
    else:
        sent = json.dumps(payload).encode() if payload else b""
    output, _ = await process.communicate(sent)
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


def validate_rejection(result):
    failures = []
    response = result["response"]
    if response["status"] != 200:
        failures.append("rejection response is not HTTP 200")
    if response["body"] != {"code": 0, "msg": result["expected"]}:
        failures.append("rejection response violates OCS contract")
    if response["provider"] is not None:
        failures.append("rejection response carries a search provider")
    before, after = result["snapshots"]["before"], result["snapshots"]["after"]
    if after["attempts"] != before["attempts"]:
        failures.append("rejection triggered upstream attempts")
    for counter in ("search_requests", "llm_calls"):
        if before["counters"].get(counter, 0) != after["counters"].get(counter, 0):
            failures.append(f"rejection changed {counter}")
    return failures


async def check_rejection(port, case, data, expected):
    snapshots = {"before": await snapshot(port)}
    token = "" if case in _AUTH_CASES else _TOKEN
    response = await curl(port, "/search", data=data, token=token)
    snapshots["after"] = await snapshot(port)
    result = {
        "mode": "rejection",
        "case": case,
        "expected": expected,
        "response": response,
        "snapshots": snapshots,
        "attempts": snapshots["after"]["attempts"],
        "counters": snapshots["after"]["counters"],
    }
    result["failures"] = validate_rejection(result)
    result["passed"] = not result["failures"]
    return result


def check_rejections(port, cases):
    results = []
    for case, data, expected in cases:
        try:
            results.append(asyncio.run(check_rejection(port, case, data, expected)))
        except Exception as exc:
            results.append(failure("rejection", case, type(exc).__name__))
            break
    return results


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
    declared = await curl(port, "/live-query", query)
    if declared["status"] != 200:
        raise RuntimeError("Cannot declare live query")
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


def check_cases(port, mode, settings, budget, only=""):
    results = []
    for case, query, expected in _CASES:
        if only and case != only:
            continue
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


def check_mode(context, settings, mode, budget, only=""):
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
            results = check_cases(connection.recv(), mode, live, budget, only)
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


def zero_cost_cases(settings, only=""):
    live = _without_vision(settings)
    if only:
        return _image_rejections(live)
    return _auth_case() + _rejections(live) + (
        (
            "image-without-vision",
            json.dumps(_VISION_QUERY).encode(),
            "图片题未配置视觉模型",
        ),
    )


def tight_body_cases():
    return (("body-too-large", _OVERSIZED_BODY, "请求体超过大小限制"),)


def tight_image_cases():
    return (
        (
            "image-too-large",
            json.dumps(_IMAGE_QUERY | {"images": [_OVERSIZED_IMAGE]}).encode(),
            _IMAGE_MESSAGES["image-too-large"],
        ),
    )


def check_zero_cost(context, settings, budget, only=""):
    results = _run_rejections(
        context, _without_vision(settings), budget, zero_cost_cases(settings, only)
    )
    if not only:
        results += _run_rejections(
            context, _with_tight_body_limit(settings), budget, tight_body_cases()
        )
        results += _run_rejections(
            context, _with_tight_image_limit(settings), budget, tight_image_cases()
        )
    return results


def _run_rejections(context, live, budget, cases, mode="rejection"):
    results = []
    with tempfile.TemporaryDirectory(dir=Path.home() / ".cache") as directory:
        connection, child = context.Pipe()
        process = context.Process(
            target=serve,
            args=(
                child,
                replace(live, cache_path=str(Path(directory) / "live.sqlite3")),
                budget,
            ),
        )
        process.start()
        child.close()
        try:
            if not connection.poll(30):
                raise TimeoutError("Zero-cost E2E server did not become ready")
            results = check_rejections(connection.recv(), cases)
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


def expected_results(settings, case=""):
    paid = 2 * (1 if case else len(_CASES))
    zero_cost = len(zero_cost_cases(settings, case))
    if not case:
        zero_cost += len(tight_body_cases()) + len(tight_image_cases())
    return paid + zero_cost


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-paid", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", default="")
    args = parser.parse_args()
    if not args.confirm_paid:
        parser.error("Real upstream calls require --confirm-paid")
    drop_proxy_env()
    root = Path(__file__).resolve().parents[1]
    env_path = root / ".env"
    before = hashlib.sha256(env_path.read_bytes()).digest()
    settings = load_settings(env_path)
    assert settings.openai_api_key and settings.exa_api_key, (
        "Existing keys are required"
    )
    context = multiprocessing.get_context("spawn")
    budget = context.Value("i", 0)
    results = check_zero_cost(context, settings, budget, args.case)
    for mode in ("search", "fallback"):
        results.extend(check_mode(context, settings, mode, budget, args.case))
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
        and len(results) == expected_results(settings, args.case)
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
