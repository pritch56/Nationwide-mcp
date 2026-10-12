"""Error types shared by all providers.

Each error has a stable ``code`` so that an unattended caller can branch on it, and a
human-readable message that says what to do. ``str(error)`` is ``"[code] message"``.
Messages must never contain secrets.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base class for every error a provider can raise. Also used directly for
    any upstream failure that has no more specific class (malformed or non-JSON
    responses, unexpected status codes)."""

    code = "provider_error"

    def __init__(self, message: str, *, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.message = message
        self.retry_after_seconds = retry_after_seconds

    def __str__(self) -> str:  # "[code] message"
        return f"[{self.code}] {self.message}"


class ConfigError(ProviderError):
    """Missing or invalid configuration (env vars, key files, statement dir)."""

    code = "config_error"


class ConsentExpiredError(ProviderError):
    """The bank consent has expired / been revoked. A human must re-authorise."""

    code = "consent_expired"


class AuthError(ProviderError):
    """The API credential (key / JWT / token) was rejected or has expired."""

    code = "token_expired"


class RateLimitError(ProviderError):
    """The aggregator or the bank refused the call because of rate limiting."""

    code = "rate_limited"


class NotFoundError(ProviderError):
    """Unknown account id (or the account is not part of the consent)."""

    code = "account_not_found"


class ProviderUnavailableError(ProviderError):
    """Upstream is down / timing out. Safe to retry later."""

    code = "provider_error"


class NotConnectedError(ProviderError):
    """The bank has never been linked (no session / refresh token stored)."""

    code = "not_connected"


class InvalidRequestError(ProviderError):
    """The caller's arguments were rejected (bad id, bad date range)."""

    code = "invalid_request"


class NotSupportedError(ProviderError):
    """The bank does not offer this endpoint through the aggregator (HTTP 501)."""

    code = "provider_error"


class ScaRequiredError(ProviderError):
    """The bank wants a fresh customer authentication (history older than ~90 days)."""

    code = "sca_required"


class TokenStoreError(ProviderError):
    """The token store could not be read or written."""

    code = "token_store_error"


class MalformedImportError(ProviderError):
    """A statement file is too large, unreadable or not in a recognised layout."""

    code = "malformed_import_file"


#: Every code a tool error can start with (in square brackets).
ERROR_CODES = (
    "consent_expired",
    "token_expired",
    "not_connected",
    "rate_limited",
    "sca_required",
    "invalid_request",
    "account_not_found",
    "provider_error",
    "config_error",
    "token_store_error",
    "malformed_import_file",
    "internal_error",
)
