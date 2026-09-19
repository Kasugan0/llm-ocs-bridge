from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from time import time

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .config import Settings, load_settings, validate_runtime_settings
from .questions import QuestionError, parse_query
from .service import Runtime, answer, build_runtime


class RequestError(ValueError):
    pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime = build_runtime(app.state.settings)
    app.state.runtime = runtime
    try:
        yield
    finally:
        await runtime.aclose()


router = APIRouter()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else load_settings()
    validate_runtime_settings(settings)
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.state.settings = settings
    app.include_router(router)
    return app


def _runtime(request: Request) -> Runtime:
    return request.app.state.runtime


def _auth_ok(request: Request) -> bool:
    settings = request.app.state.settings
    token = request.headers.get("X-Access-Token", "")
    if not settings.access_token:
        return False
    return secrets.compare_digest(token.encode(), settings.access_token.encode())


def _ocs_response(
    question: str,
    answer: str | None,
    msg: str | None = None,
) -> JSONResponse:
    if msg:
        return JSONResponse({"code": 0, "msg": msg})
    return JSONResponse({"code": 1, "question": question, "answer": answer})


async def _read_body(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    if request.method == "GET":
        body = dict(request.query_params)
        body_size = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        if body_size > settings.max_request_bytes:
            raise RequestError("请求参数超过大小限制")
        return body

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            parsed_content_length = int(content_length)
            if parsed_content_length < 0:
                raise RequestError("Content-Length 无效")
            if parsed_content_length > settings.max_request_bytes:
                raise RequestError("请求体超过大小限制")
        except RequestError:
            raise
        except ValueError as exc:
            raise RequestError("Content-Length 无效") from exc

    chunks: list[bytes] = []
    body_size = 0
    async for chunk in request.stream():
        body_size += len(chunk)
        if body_size > settings.max_request_bytes:
            raise RequestError("请求体超过大小限制")
        chunks.append(chunk)

    try:
        data = json.loads(b"".join(chunks).decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise RequestError("请求体必须是 UTF-8 编码") from exc
    except json.JSONDecodeError as exc:
        raise RequestError("请求体必须是合法 JSON") from exc
    if not isinstance(data, dict):
        raise RequestError("请求体必须是 JSON 对象")
    return data


def _providers(runtime: Runtime) -> list[str]:
    return [runtime.search_client.name] if runtime.search_client.enabled else []


@router.get("/")
@router.head("/")
async def health_root() -> PlainTextResponse:
    return PlainTextResponse("ok")


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    runtime = _runtime(request)
    return JSONResponse(
        {
            "status": "ok",
            "uptime": round(time() - runtime.start_time, 3),
            "search_providers": _providers(runtime),
        }
    )


@router.get("/stats")
async def stats(request: Request) -> JSONResponse:
    if not _auth_ok(request):
        return JSONResponse({"code": 0, "msg": "invalid token"}, status_code=401)

    runtime = _runtime(request)
    counters = await runtime.metrics_store.get_counters()
    total_hits = counters["search_cache_hits"] + counters["answer_cache_hits"]
    total_misses = counters["search_cache_misses"] + counters["answer_cache_misses"]
    total_requests = total_hits + total_misses
    hit_rate = round(total_hits / total_requests, 2) if total_requests > 0 else 0.0

    return JSONResponse(
        {
            "status": "ok",
            "uptime": round(time() - runtime.start_time, 3),
            "providers": _providers(runtime),
            "cache_stats": {
                "search_hits": counters["search_cache_hits"],
                "search_misses": counters["search_cache_misses"],
                "answer_hits": counters["answer_cache_hits"],
                "answer_misses": counters["answer_cache_misses"],
                "hit_rate": hit_rate,
            },
            "counters": counters,
            "search_observability": await runtime.search_cache.recent(
                runtime.settings.stats_recent_limit, runtime.settings.max_title_length
            ),
            "recent_errors": await runtime.metrics_store.get_recent_errors(
                runtime.settings.stats_recent_limit
            ),
        }
    )


@router.api_route("/search", methods=["GET", "POST"])
async def search(request: Request) -> JSONResponse:
    if not _auth_ok(request):
        return _ocs_response("", None, "无效的访问令牌")
    try:
        query = parse_query(await _read_body(request), request.app.state.settings)
    except (RequestError, QuestionError) as exc:
        return _ocs_response("", None, str(exc))
    result = await answer(_runtime(request), query)
    response = _ocs_response(query.title, result.answer, result.error)
    if result.provider:
        response.headers["X-Search-Provider"] = result.provider
    return response
