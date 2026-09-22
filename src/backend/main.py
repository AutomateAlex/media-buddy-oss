"""FastAPI application entrypoint for Media Buddy backend."""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# Source-available build: `.env` at the repo root is one place to put provider
# keys; the Settings page (OS keychain via secret_store) is the other. Both end
# up in os.environ, which is what the rest of the code reads.
_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"  # <repo>/.env
load_dotenv(_ENV_FILE)

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.database import Base, engine, migration_engine
import backend.models  # noqa: F401 — registers all ORM tables before create_all
from backend.api import projects, pipeline, system, settings as settings_api, series as series_api, batch as batch_api, studio as studio_api, style_presets as style_presets_api, auth as auth_api, logs as logs_api, tts as tts_api, script_intelligence as script_intelligence_api, publish as publish_api
from backend.lib import secret_store
from backend.lib.app_token import HEADER_NAME, PUBLIC_PATHS, get_token, verify
from backend.lib.diagnostics import configure_file_logging
from backend.lib.ffmpeg_locator import ensure_ffmpeg_on_path

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_file_logging()
    ensure_ffmpeg_on_path()
    # Keys saved from the Settings page live in the OS keychain; load them into
    # os.environ (a value already present from .env wins, nothing is migrated).
    from backend.api.settings import KNOWN_KEYS
    populated = secret_store.populate_environ(list(KNOWN_KEYS))
    if populated:
        logger.info("loaded %d API key(s) from the keychain into env", populated)

    # Phase 2.11g — generate / load the per-install app token now so the
    # token middleware below can validate from the very first request.
    _ = get_token()

    # Use the migration engine (session/direct pooler) for DDL — create_all +
    # the lightweight ALTERs run multi-statement transactions + reflection that
    # the transaction-mode pooler handles poorly.
    Base.metadata.create_all(bind=migration_engine)
    backend.models.apply_lightweight_migrations(migration_engine)


    # Phase 2.8 — recover orphans + start in-process BackgroundWorker
    # (replaces Celery + Redis). Single daemon thread drains pending
    # Projects sequentially; chunk-level parallelism inside Orchestrator
    # is unchanged.
    # the management backend only enqueues video_jobs, so we DON'T start the
    # in-process worker here. Desktop (single-user) keeps the local worker.
    from backend.lib.user_context import multi_tenant_enabled
    worker = None
    if not multi_tenant_enabled():
        from backend.workers.background_worker import get_background_worker
        from backend.workers.startup_recovery import recover_orphaned_running_projects
        recover_orphaned_running_projects()
        worker = get_background_worker()
        worker.start()
    try:
        yield
    finally:
        if worker is not None:
            worker.stop(timeout=5.0)


# 🚨 **应用日志必须有出口。**
#
# 后果:出稿落库的 NameError 连报三天没人看见 —— 数据没存、日志没有、
# 表面一切正常。这里兜底:root logger 没有 handler 就配一个。
# ⚠️ 只在没有 handler 时才配 —— worker 进程自己配过,别给它叠一份重复输出。
import logging as _logging

if not _logging.getLogger().handlers:
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

app = FastAPI(title="Media Buddy API", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def reject_stale_user_token(request: Request, call_next):
    """令牌**过期/无效**时明确回 401，而不是把请求当成匿名。


    当天上线开启了 JWT 签名 + `exp` 校验（安全上完全正确）。但验不过时
    请求被当成**没有用户** → `owner_filter` 空过滤 → 客户点自己的频道
    拿到 **404「频道不存在」**，然后被踢回登录页。刷新一次重演一次。

    前端本来就有自救逻辑：**遇 401 悄悄续期再重试一次**
    （`lib/webAuth.ts` 的 `refreshNow`）。可 404 不是 401 —— 那套逻辑
    从来没被触发过。

      `/api/series/{id}` 的 404：**0 → 8**
      被迫重新登录：5 → 12

    ⚠️ 访问令牌每小时过期一次是**正常事件**，不是错误。它必须回 401 让
       前端续期；回 404 等于把「你该续期了」说成「你的东西没了」。

    ## 三条边界（漏一条就会造出新的故障）

    ⚠️ **没带令牌的请求不管** —— 那是匿名访问，公开端点照常放行。
       这一道只针对「带了令牌但令牌坏了」。
    ⚠️ **`/api/auth/*` 全部放行** —— 续期请求自己往往还带着那张过期的
       访问令牌。在这里挡住它，前端就永远续不了期，故障从「偶尔被踢」
       变成「谁都登不上」。
    ⚠️ **单机/桌面模式（多租户关闭）不生效** —— 那边不校验签名，
       `token_present_but_invalid` 恒为 False。
    """

    path = request.url.path
    if request.method == "OPTIONS" or path.startswith("/api/auth/"):
        return await call_next(request)
    try:
        from backend.lib.user_context import token_present_but_invalid

        stale = token_present_but_invalid(request)
    except Exception:
        stale = False
    if stale:
        return JSONResponse(
            status_code=401,
            content={"detail": {"error": "token_expired"}},
        )
    return await call_next(request)


@app.middleware("http")
async def verify_app_token(request: Request, call_next):
    """Phase 2.11g — gate the API behind a per-install token.

    Even though we bind to 127.0.0.1, raw localhost reachability is a known
    DNS-rebinding / cross-tenant attack surface. We require every request
    to carry an ``X-MediaBuddy-Token`` header matching the value written
    to ``~/.media-buddy-oss/.app_token`` at startup. The Electron preload
    reads that file and injects the header for every renderer request.
    A handful of paths (/health, /docs, /openapi.json) are public so the
    server-manager can readiness-probe before it knows the token.
    """
    # Test escape hatch — pytest sets this so unit tests don't need to
    # juggle headers. NEVER set this in production.
    import os as _os
    if _os.environ.get("MEDIA_BUDDY_DISABLE_TOKEN_CHECK") == "1":
        return await call_next(request)
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    # Everything outside /api is the built web UI (index.html, /assets/*) —
    # public files, no token needed. A direct navigation to http://127.0.0.1:8000/
    # carries neither Origin nor Referer, so it must be allowed here.
    if not path.startswith("/api/"):
        return await call_next(request)
    # CORS preflight requests from the browser don't carry custom headers
    # — let them through; the actual subsequent request still needs the
    # token.
    if request.method == "OPTIONS":
        return await call_next(request)

    # Phase 2.11g — dev-mode trusted-origin bypass.
    # The browser stamps both Origin (on XHR/fetch) and Referer (on
    # resource loads like <video src=...>) to the page URL — neither
    # can be forged by JS. Trusting either when it points at our local
    # Vite dev server (http://localhost:5173) is safe:
    #   - axios JSON requests carry Origin
    #   - <video>/<audio>/<img> tag loads carry Referer (no Origin)
    #   - DNS-rebinding attacker's headers point at THEIR hostname,
    #     not localhost:5173 → still rejected.
    def _is_local_dev_url(s: str) -> bool:
        # 5173 = default Vite dev server; 5174 = the parallel source-only dev
        # stack (source backend on 8001) used for fast local iteration without
        # touching the installed app. Both are trusted local origins — a
        # DNS-rebinding attacker's headers would point at their own hostname.
        return s.startswith((
            "http://localhost:5173", "http://127.0.0.1:5173",
            "http://localhost:5174", "http://127.0.0.1:5174",
        ))

    origin = request.headers.get("origin", "")
    referer = request.headers.get("referer", "")
    if _is_local_dev_url(origin) or _is_local_dev_url(referer):
        return await call_next(request)
    # Source-available build: the backend serves the built frontend from its own
    # origin (http://127.0.0.1:8000). A browser stamps Origin/Referer on every
    # request from that page; trust our own host the same way as the dev server.
    _self = f"{request.url.scheme}://{request.url.netloc}"
    if origin.startswith(_self) or referer.startswith(_self):
        return await call_next(request)

    # Phase 2.11i — query-param token for HTML resource fetches.
    # <video src=...>, <img src=...>, <audio src=...> can't add custom
    # headers, but they CAN carry a query string. Frontend appends
    # ``?token=<...>`` to media URLs. Treat that as equivalent to the
    # header for GET requests on these resource paths only — POST/PUT/
    # DELETE still require the header (defends against CSRF-style abuse
    # via crafted <img> tags).
    provided = request.headers.get(HEADER_NAME)
    if (provided is None and request.method == "GET"
            and "token" in request.query_params):
        provided = request.query_params.get("token")

    if not verify(provided):
        return JSONResponse(
            status_code=401,
            content={"error": "missing or invalid app token",
                     "header": HEADER_NAME},
        )
    return await call_next(request)


# --- 操作错误监控 ---
# 把失败请求(4xx/5xx)旁路记进 api_errors 表,喂给 BUG 看板的"操作错误"区:
# 可疑片看板只检"已产出的成片",一个在出片前就被拒的请求(如重复选题 409)它看不到。
# 这几个处理器只负责记录 + 委托给默认处理器返回原本的响应,best-effort、绝不改变行为。
from starlette.exceptions import HTTPException as _StarletteHTTPException
from fastapi.exceptions import RequestValidationError as _RequestValidationError
from fastapi.exception_handlers import (
    http_exception_handler as _default_http_exc_handler,
    request_validation_exception_handler as _default_validation_handler,
)
from backend.lib.api_error_log import record_api_error as _record_api_error

_errlog = logging.getLogger("backend.api_errors")


@app.exception_handler(_StarletteHTTPException)
async def _record_http_exception(request: Request, exc: _StarletteHTTPException):
    code = exc.detail.get("error") if isinstance(exc.detail, dict) else None
    _record_api_error(request, exc.status_code, code)
    return await _default_http_exc_handler(request, exc)


@app.exception_handler(_RequestValidationError)
async def _record_validation_error(request: Request, exc: _RequestValidationError):
    _record_api_error(request, 422, "validation_error")
    return await _default_validation_handler(request, exc)


@app.exception_handler(Exception)
async def _record_unhandled_error(request: Request, exc: Exception):
    _record_api_error(request, 500, type(exc).__name__)
    _errlog.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


# 上云 P2 — CORS origins are env-extensible so the hosted web frontend's
# domain (e.g. your own domain) can be allowed without a code
# change. Desktop is unaffected: with MEDIA_BUDDY_CORS_ORIGINS unset the
# allow-list is exactly the original local-dev origins.
import os as _os_cors
_default_cors_origins = ["http://localhost:5173", "http://127.0.0.1:5173", "file://"]
_extra_cors_origins = [
    o.strip()
    for o in _os_cors.environ.get("MEDIA_BUDDY_CORS_ORIGINS", "").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_default_cors_origins + _extra_cors_origins,
    allow_methods=["*"],
    allow_headers=["*", HEADER_NAME],
)

app.include_router(projects.router, prefix="/api/projects", tags=["projects"])
app.include_router(pipeline.router, prefix="/api/pipeline", tags=["pipeline"])
app.include_router(system.router, prefix="/api/system", tags=["system"])
app.include_router(settings_api.router, prefix="/api/settings", tags=["settings"])
app.include_router(series_api.router, prefix="/api/series", tags=["series"])
app.include_router(batch_api.series_batch_router, prefix="/api/series", tags=["batch"])
app.include_router(batch_api.batch_router, prefix="/api/batch", tags=["batch"])
app.include_router(studio_api.router, prefix="/api/studio", tags=["studio"])
app.include_router(style_presets_api.router, prefix="/api/style-presets", tags=["style_presets"])
app.include_router(logs_api.router, prefix="/api/logs", tags=["logs"])
app.include_router(auth_api.router)
app.include_router(tts_api.router)
app.include_router(script_intelligence_api.router, prefix="/api/script-intelligence", tags=["script_intelligence"])
app.include_router(publish_api.router, prefix="/api/publish", tags=["publish"])


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Serve the built frontend (src/frontend/dist/renderer) from the same origin ──
from pathlib import Path as _Path  # noqa: E402
from fastapi.responses import FileResponse as _FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

_DIST = _Path(__file__).resolve().parents[1] / "frontend" / "dist" / "renderer"
if (_DIST / "index.html").is_file():
    app.mount("/assets", StaticFiles(directory=str(_DIST / "assets")), name="assets")

    @app.get("/", include_in_schema=False)
    def _index():
        return _FileResponse(str(_DIST / "index.html"))

    @app.get("/{spa_path:path}", include_in_schema=False)
    def _spa(spa_path: str):
        candidate = (_DIST / spa_path)
        if spa_path and candidate.is_file() and _DIST in candidate.resolve().parents:
            return _FileResponse(str(candidate))
        return _FileResponse(str(_DIST / "index.html"))
