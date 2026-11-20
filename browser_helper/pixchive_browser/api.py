import ipaddress
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from starlette.responses import JSONResponse

from .config import Settings
from .identity import COOKIE_VALUE
from .relay import connect_vnc, relay
from .service import BrowserService, ProfileOccupied, Unavailable


class Status(BaseModel):
    state: Literal["starting", "connected", "reauthenticating", "needs_login", "error"]
    last_checked: int | None
    cookie_available: bool
    viewing: bool
    retry_at: int
    login_pending: bool = False
    login_generation: int = 0


class Resume(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generation: int = Field(strict=True, ge=0)


class Seed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cookie: SecretStr

    @field_validator("cookie", mode="before")
    @classmethod
    def validate_cookie(cls, value):
        if not isinstance(value, str) or COOKIE_VALUE.fullmatch(value) is None:
            raise ValueError("Invalid cookie")
        return value


def private_peer(connection) -> bool:
    try:
        address = ipaddress.ip_address(connection.client.host)
        return address.is_private and not address.is_unspecified and not address.is_multicast
    except (ValueError, AttributeError):
        return False


def create_app(settings: Settings | None = None, *, service=None, connector=connect_vnc) -> FastAPI:
    service = service or BrowserService(settings or Settings())

    @asynccontextmanager
    async def lifespan(_app):
        await service.start()
        try:
            yield
        finally:
            await service.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.browser = service

    @app.middleware("http")
    async def protect(request: Request, call_next):
        response = None
        try:
            if request.url.scheme != "http" or not private_peer(request):
                response = JSONResponse(
                    {"detail": "Private HTTP connection required"}, status_code=403
                )
            elif not service.secrets.authorized(request.headers):
                response = JSONResponse(
                    {"detail": "Unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            else:
                response = await call_next(request)
        except Exception:
            # Do not let ASGI error logging print Playwright/Pydantic exceptions containing secrets.
            response = JSONResponse({"detail": "Browser unavailable"}, status_code=503)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, _error):
        return JSONResponse({"detail": "Invalid request"}, status_code=422)

    @app.exception_handler(Unavailable)
    async def unavailable(_request, _error):
        return JSONResponse({"detail": "Browser unavailable"}, status_code=503)

    @app.exception_handler(ProfileOccupied)
    async def occupied(_request, _error):
        return JSONResponse({"detail": "Browser profile is not empty"}, status_code=409)

    @app.get("/status", response_model=Status)
    async def status():
        return service.status()

    @app.post("/open", response_model=Status)
    async def open_browser():
        return await service.open()

    @app.post("/reauth", response_model=Status)
    async def reauth():
        return await service.reauth()

    @app.post("/login", response_model=Status)
    async def login():
        return await service.login()

    @app.post("/complete", response_model=Status)
    async def complete():
        return await service.complete()

    @app.post("/resume", response_model=Status)
    async def resume(payload: Resume):
        return await service.resume(payload.generation)

    @app.post("/disconnect", response_model=Status)
    async def disconnect():
        return await service.disconnect()

    @app.post("/seed", response_model=Status)
    async def seed(request: Request):
        payload = bytearray()
        async for chunk in request.stream():
            payload.extend(chunk)
            if len(payload) > 8192:
                return JSONResponse({"detail": "Request too large"}, status_code=413)
        try:
            seed = Seed.model_validate_json(payload)
        except (ValidationError, ValueError):
            return JSONResponse({"detail": "Invalid request"}, status_code=422)
        return await service.seed(seed.cookie.get_secret_value())

    @app.get("/credential")
    async def credential():
        return {"cookie": service.credential()}

    @app.websocket("/view")
    async def view(websocket: WebSocket):
        try:
            if not private_peer(websocket) or not service.secrets.authorized(websocket.headers):
                await websocket.close(code=1008)
                return
        except Exception:
            await websocket.close(code=1011)
            return
        async with service.view():
            await relay(websocket, connect=connector)

    return app


app = create_app()
