class ServiceUnavailableError(RuntimeError):
    """Raised when a required local service or configuration is unavailable."""


class UpstreamServiceError(RuntimeError):
    """Raised when an external model or other upstream service fails."""


class UpstreamTimeoutError(RuntimeError):
    """Raised when an external model or other upstream service times out."""
