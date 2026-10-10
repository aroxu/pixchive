from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import protected, public
from app.core.config import Settings
from app.core.data_state import data_access
from app.core.db import db, initialize
from app.core.errors import DomainError
from app.core.redaction import install as install_redaction
from app.migrations.runner import migrate


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        install_redaction()
        with data_access(settings.data_dir):
            initialize(settings.data_dir)
            try:
                migrate()
                yield
            finally:
                if not db.is_closed():
                    db.close()

    app = FastAPI(
        title="Pixchive", version="0.1.0.dev0", lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.state.settings = settings

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not request.app.state.settings.allows_origin(request.headers.get("origin")):
                return JSONResponse(
                    {"code": "origin_rejected", "message": "요청 출처가 다릅니다."}, status_code=403
                )
            body = b""
            async for chunk in request.stream():
                body += chunk
                if len(body) > 32768:
                    return JSONResponse(
                        {"code": "request_too_large", "message": "요청이 너무 큽니다."},
                        status_code=413,
                    )
            request._body = body
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' blob:; font-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default errors include the original input, including secrets.
        return JSONResponse(
            {"code": "invalid_request", "message": "입력값을 확인해 주세요."}, status_code=422
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return JSONResponse(
            {"code": "internal_error", "message": "요청을 처리하지 못했습니다."}, status_code=500
        )

    app.include_router(public)
    app.include_router(protected)

    @app.get("/health", include_in_schema=False)
    async def health():
        return {"status": "ok"}

    if (settings.frontend_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=settings.frontend_dir / "assets"))

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith(("api/", "assets/")) or path in ("api", "assets"):
            return JSONResponse({"code": "not_found", "message": "찾을 수 없습니다."}, 404)
        index = settings.frontend_dir / "index.html"
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return JSONResponse(
            {"code": "frontend_not_built", "message": "화면 빌드가 필요합니다."}, 503
        )

    return app


app = create_app()
