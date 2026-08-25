"""FastAPI loopback surface for the versioned runtime protocol."""

from __future__ import annotations

import hmac
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from exocore_runtime.config import RuntimeConfig
from exocore_runtime.contracts import GenerationSpec, RetireRequest, TurnRequest
from exocore_runtime.errors import InvalidRequestError, RuntimeGatewayError
from exocore_runtime.providers.base import RuntimeProviderAdapter
from exocore_runtime.providers.fake import DeterministicFakeAdapter
from exocore_runtime.service import RuntimeService
from exocore_runtime.state_store import RuntimeStateStore


def create_app(
    config: RuntimeConfig,
    provider: RuntimeProviderAdapter | None = None,
    store: RuntimeStateStore | None = None,
) -> FastAPI:
    """Create one isolated app lifecycle; configuration is validated before this call."""

    runtime_store = store or RuntimeStateStore(config.state_path)
    runtime_provider = provider or DeterministicFakeAdapter()
    service = RuntimeService(runtime_store, runtime_provider, (config.token,))
    app = FastAPI(
        title="ExoCore Runtime Gateway",
        version="v1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.runtime_service = service
    app.state.runtime_store = runtime_store
    app.state.runtime_provider = runtime_provider

    @app.middleware("http")
    async def authenticate_non_health(request: Request, call_next):
        if request.url.path == "/v1/health":
            return await call_next(request)
        authorization = request.headers.get("authorization")
        expected = f"Bearer {config.token}"
        if (
            config.token in request.url.path
            or authorization is None
            or not hmac.compare_digest(authorization, expected)
        ):
            return JSONResponse(
                status_code=401,
                content={"error": "unauthorized"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    def reject_secret_echo(contract) -> None:
        serialized = contract.model_dump_json()
        if config.token in serialized:
            raise InvalidRequestError("request body contains reserved credential material")

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_request"},
        )

    @app.exception_handler(RuntimeGatewayError)
    async def runtime_error_handler(
        request: Request,
        exc: RuntimeGatewayError,
    ) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.code},
            headers=headers,
        )

    @app.get("/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "schema_version": "v1"}

    @app.put("/v1/generations/{binding_id}")
    async def ensure_generation(binding_id: UUID, spec: GenerationSpec):
        reject_secret_echo(spec)
        return await service.ensure_generation(binding_id, spec)

    @app.post("/v1/generations/{binding_id}/turns")
    async def stream_turn(binding_id: UUID, turn: TurnRequest) -> StreamingResponse:
        reject_secret_echo(turn)
        service.preflight_turn(binding_id, turn)

        async def lines():
            async for event in service.stream_turn(binding_id, turn):
                yield event.model_dump_json() + "\n"

        return StreamingResponse(lines(), media_type="application/x-ndjson")

    @app.post("/v1/generations/{binding_id}/turns/{request_id}/cancel")
    async def cancel(binding_id: UUID, request_id: UUID):
        return await service.cancel(binding_id, request_id)

    @app.post("/v1/generations/{binding_id}/retire")
    async def retire(binding_id: UUID, body: RetireRequest | None = None):
        if body is not None:
            reject_secret_echo(body)
        reason = body.reason if body is not None else "retired"
        return await service.retire(binding_id, reason)

    return app
