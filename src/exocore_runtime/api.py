"""FastAPI loopback surface for the versioned runtime protocol."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import asynccontextmanager
import hmac
import re
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse

from exocore_runtime.config import RuntimeConfig
from exocore_runtime.contracts import (
    ATTACHMENT_ID_PATTERN,
    MAX_ATTACHMENT_BYTES,
    PROTOCOL_VERSION,
    RUNTIME_CAPABILITIES,
    GenerationSpec,
    JournalReplayHeader,
    RetireRequest,
    TurnRequest,
)
from exocore_runtime.errors import (
    AttachmentSizeExceededError,
    InvalidRequestError,
    NotFoundError,
    RuntimeGatewayError,
)
from exocore_runtime.providers.antigravity.adapter import AntigravityAdapter
from exocore_runtime.providers.antigravity.process import (
    AgyProcessConfig,
    AgyProcessSupervisor,
)
from exocore_runtime.providers.base import RuntimeProviderAdapter
from exocore_runtime.providers.fake import DeterministicFakeAdapter
from exocore_runtime.service import RuntimeService
from exocore_runtime.state_store import RuntimeStateStore


def create_app(
    config: RuntimeConfig,
    provider: RuntimeProviderAdapter | Mapping[str, RuntimeProviderAdapter] | None = None,
    store: RuntimeStateStore | None = None,
) -> FastAPI:
    """Create one isolated app lifecycle; configuration is validated before this call."""

    runtime_store = store or RuntimeStateStore(config.state_path)
    if provider is None:
        process_config = AgyProcessConfig.official(
            config.agy_executable,
            init_timeout_seconds=config.agy_init_timeout,
            idle_timeout_seconds=config.agy_idle_timeout,
            hard_timeout_seconds=config.agy_hard_timeout,
            close_timeout_seconds=config.agy_close_timeout,
        )
        runtime_provider: RuntimeProviderAdapter | Mapping[str, RuntimeProviderAdapter] = {
            "fake": DeterministicFakeAdapter(),
            "antigravity": AntigravityAdapter(
                config.effective_provider_data_root,
                AgyProcessSupervisor(process_config),
                memory_mcp_root=config.effective_memory_mcp_root,
                mailbox_ttl_seconds=config.agy_mailbox_ttl,
            ),
        }
    else:
        runtime_provider = provider
    service = RuntimeService(runtime_store, runtime_provider, (config.token,))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await service.shutdown()

    app = FastAPI(
        title="ExoCore Runtime Gateway",
        version="v2",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.runtime_service = service
    app.state.runtime_store = runtime_store
    app.state.runtime_provider = runtime_provider

    @app.middleware("http")
    async def authenticate_non_health(request: Request, call_next):
        if request.url.path == "/v2/health":
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

    @app.get("/v2/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "schema_version": PROTOCOL_VERSION,
            "protocol": "subscription-runtime-v2",
            "capabilities": list(RUNTIME_CAPABILITIES),
        }

    @app.put("/v2/generations/{binding_id}")
    async def ensure_generation(binding_id: UUID, spec: GenerationSpec):
        reject_secret_echo(spec)
        return await service.ensure_generation(binding_id, spec)

    @app.post("/v2/generations/{binding_id}/turns")
    async def stream_turn(binding_id: UUID, turn: TurnRequest) -> StreamingResponse:
        reject_secret_echo(turn)
        service.preflight_turn(binding_id, turn)

        async def lines():
            async for event in service.stream_turn(binding_id, turn):
                yield event.model_dump_json() + "\n"

        return StreamingResponse(lines(), media_type="application/x-ndjson")

    @app.put(
        "/v2/generations/{binding_id}/turns/{request_id}/attachments/{artifact_id}"
    )
    async def stage_attachment(
        binding_id: UUID,
        request_id: UUID,
        artifact_id: str,
        request: Request,
    ) -> Response:
        if re.fullmatch(ATTACHMENT_ID_PATTERN, artifact_id) is None:
            raise InvalidRequestError("invalid artifact id")
        if request.headers.get("transfer-encoding") is not None:
            raise InvalidRequestError("chunked attachment uploads are not accepted")
        content_type = request.headers.get("content-type", "").split(";", 1)[0]
        if content_type.strip().lower() != "application/octet-stream":
            raise InvalidRequestError("attachment content type is invalid")
        declared_text = request.headers.get("content-length")
        if declared_text is None or not declared_text.isdigit():
            raise InvalidRequestError("attachment content length is required")
        declared_size = int(declared_text)
        if declared_size <= 0:
            raise InvalidRequestError("attachment body cannot be empty")
        if declared_size > MAX_ATTACHMENT_BYTES:
            raise AttachmentSizeExceededError()
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_ATTACHMENT_BYTES:
                raise AttachmentSizeExceededError()
        if len(body) != declared_size:
            raise InvalidRequestError("attachment content length does not match body")
        await service.stage_attachment(
            binding_id,
            request_id,
            artifact_id,
            bytes(body),
        )
        return Response(status_code=200)

    @app.delete("/v2/generations/{binding_id}/turns/{request_id}/attachments")
    async def discard_attachments(
        binding_id: UUID,
        request_id: UUID,
    ) -> Response:
        await service.discard_attachments(binding_id, request_id)
        return Response(status_code=200)

    @app.post("/v2/generations/{binding_id}/turns/{request_id}/cancel")
    async def cancel(binding_id: UUID, request_id: UUID):
        return await service.cancel(binding_id, request_id)

    class JournalNotTerminalError(RuntimeGatewayError):
        code = "journal_not_terminal"
        status_code = 409

    @app.get("/v2/generations/{binding_id}/turns/{request_id}/journal")
    async def request_journal_replay(
        binding_id: UUID,
        request_id: UUID,
    ) -> StreamingResponse:
        """Authenticated read-only replay of one terminal request journal.

        Resolves nothing: no provider lookup, no generation ensure, no process
        supervisor, no prepare/send/cancel/resume/retire and no SQLite write.
        Only the durable request row and its ordered event snapshot are read.
        """

        binding = str(binding_id)
        request_key = str(request_id)
        request_record = service.store.get_request(binding, request_key)
        if request_record is None:
            raise NotFoundError()
        if not request_record.terminal:
            raise JournalNotTerminalError()
        events = service.journal.replay(binding, request_key)
        if not events:
            raise RuntimeGatewayError()
        last_sequence = int(request_record.last_sequence)
        if (
            last_sequence < 1
            or len(events) != last_sequence
            or events[-1].sequence != last_sequence
            or not events[-1].terminal
            or events[-1].terminal_status != request_record.status
        ):
            raise RuntimeGatewayError()
        for index, event in enumerate(events, start=1):
            if event.sequence != index:
                raise RuntimeGatewayError()
        header = JournalReplayHeader(
            schema_version="v2",
            frame_type="journal_header",
            binding_id=binding_id,
            request_id=request_id,
            request_payload_sha256=request_record.payload_hash,
            request_status=request_record.status,
            event_count=len(events),
            last_sequence=last_sequence,
        )

        async def lines():
            yield header.model_dump_json() + "\n"
            for event in events:
                yield event.model_dump_json() + "\n"

        return StreamingResponse(lines(), media_type="application/x-ndjson")

    @app.post("/v2/generations/{binding_id}/retire")
    async def retire(binding_id: UUID, body: RetireRequest | None = None):
        if body is not None:
            reject_secret_echo(body)
        reason = body.reason if body is not None else "retired"
        return await service.retire(binding_id, reason)

    return app
