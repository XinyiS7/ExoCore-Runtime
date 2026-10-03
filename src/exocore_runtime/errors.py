"""Stable service errors that contain no request payload or credentials."""


class RuntimeGatewayError(Exception):
    """Base class for safe, stable runtime errors."""

    code = "runtime_error"
    status_code = 500


class AuthenticationError(RuntimeGatewayError):
    code = "unauthorized"
    status_code = 401


class InvalidRequestError(RuntimeGatewayError):
    code = "invalid_request"
    status_code = 400


class NotFoundError(RuntimeGatewayError):
    code = "not_found"
    status_code = 404


class ConflictError(RuntimeGatewayError):
    code = "identity_conflict"
    status_code = 409


class CancelUnregisteredError(RuntimeGatewayError):
    """Cancel arrived before the request crossed durable registration.

    Cancel-operation-specific metadata-only code: ExoCore classifies this
    conservatively as indeterminate instead of a definitive request failure,
    because a send attempt may already have crossed the wire (CP4 R14).
    """

    code = "cancel_request_unregistered"
    status_code = 409


class RetiredError(RuntimeGatewayError):
    code = "generation_retired"
    status_code = 409


class RequestRegisteredError(RuntimeGatewayError):
    code = "request_registered"
    status_code = 409


class AttachmentSizeExceededError(RuntimeGatewayError):
    code = "attachment_size_exceeded"
    status_code = 400


class AttachmentCapacityExceededError(RuntimeGatewayError):
    code = "attachment_capacity_exceeded"
    status_code = 400


class AttachmentStagingError(RuntimeGatewayError):
    code = "attachment_staging_failed"
    status_code = 500


class GeneratedArtifactUnavailableError(RuntimeGatewayError):
    """No exportable snapshot exists for this binding and reference."""

    code = "generated_artifact_unavailable"
    status_code = 404


class StateResetRequiredError(RuntimeGatewayError):
    code = "v2_state_reset_required"
    status_code = 409


class ProviderProtocolError(RuntimeGatewayError):
    code = "provider_protocol_error"
    status_code = 502


class ProviderAdapterError(RuntimeGatewayError):
    """Safe typed provider failure with no raw process or request projection."""

    status_code = 502

    def __init__(
        self,
        code: str,
        *,
        terminal_status: str = "failed",
        fatal_generation: bool = False,
        status_code: int = 502,
    ) -> None:
        if terminal_status not in {"failed", "indeterminate"}:
            raise ValueError("invalid provider terminal status")
        super().__init__(code)
        self.code = code
        self.terminal_status = terminal_status
        self.fatal_generation = fatal_generation
        self.status_code = status_code

    def __str__(self) -> str:
        return self.code
