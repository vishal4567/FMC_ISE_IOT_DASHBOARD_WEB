"""Shared exception types for the Cisco integration clients."""


class IntegrationError(Exception):
    """Base error for any ISE/FMC integration problem.

    Carries a human-readable message plus optional context (HTTP status,
    source system) so views can render a useful, non-fatal error card
    instead of a 500.
    """

    def __init__(self, message, *, source=None, status=None, detail=None):
        super().__init__(message)
        self.message = message
        self.source = source
        self.status = status
        self.detail = detail

    def __str__(self):
        parts = [self.message]
        if self.status is not None:
            parts.append(f"(HTTP {self.status})")
        return " ".join(parts)


class ConfigError(IntegrationError):
    """Raised when required configuration (host/credentials) is missing."""


class AuthError(IntegrationError):
    """Raised when authentication with ISE/FMC fails."""
