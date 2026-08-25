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


class RetiredError(RuntimeGatewayError):
    code = "generation_retired"
    status_code = 409


class ProviderProtocolError(RuntimeGatewayError):
    code = "provider_protocol_error"
    status_code = 502
