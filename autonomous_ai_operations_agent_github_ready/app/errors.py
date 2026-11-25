from __future__ import annotations


class OpsError(Exception):
    """Base class for errors that are safe to show to API clients."""

    status_code = 400


class InvalidInput(OpsError):
    status_code = 422


class Unauthenticated(OpsError):
    status_code = 401


class PermissionDenied(OpsError):
    status_code = 403


class NotFound(OpsError):
    status_code = 404


class InvalidState(OpsError):
    status_code = 409


class ApprovalRequired(OpsError):
    status_code = 409


class ToolError(OpsError):
    status_code = 500


class InvalidToolArguments(ToolError):
    status_code = 422


class TransientToolError(ToolError):
    """A failure that is safe to retry because the external action did not happen."""

    status_code = 503


class ProviderTransientError(Exception):
    """Raised by providers for retryable failures (timeouts, rate limits)."""


class UploadRejected(OpsError):
    status_code = 422


class FileTooLarge(OpsError):
    status_code = 413


class ExtractionError(UploadRejected):
    """A file was accepted by type but its contents could not be read as text."""
