"""Small shared HTTP helpers. Nothing here logs headers or bodies."""

from __future__ import annotations

import functools
import ipaddress
import logging
import re
from collections.abc import Callable
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx

from .. import __version__
from ..errors import ConfigError, InvalidRequestError, ProviderError, ProviderUnavailableError

log = logging.getLogger("nationwide_mcp.http")

DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
USER_AGENT = f"nationwide-mcp/{__version__}"

_PATH_ID = re.compile(r"[A-Za-z0-9_-]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")

F = TypeVar("F", bound=Callable[..., Any])


def is_path_safe(value: Any) -> bool:
    """True if ``value`` may be interpolated into a URL path (ASCII letters,
    digits, ``_`` and ``-`` only; ``str.isalnum`` would pass non-ASCII digits)."""
    return isinstance(value, str) and _PATH_ID.fullmatch(value) is not None


def require_path_id(value: Any, what: str = "account_id") -> str:
    """Validate a caller-supplied identifier before any request is sent."""
    if not is_path_safe(value):
        raise InvalidRequestError(f"{what} is missing or malformed. Use a value returned by list_accounts.")
    return value


def strip_control(text: Any, limit: int = 300) -> str:
    """Make third-party text safe to echo to a terminal or an error message."""
    return _CONTROL.sub("", str(text if text is not None else ""))[:limit]


def is_loopback_host(host: str | None) -> bool:
    name = (host or "").strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def validate_base_url(url: str, variable: str) -> str:
    """Base-URL overrides are test hooks: https only, unless the host is loopback."""
    parts = urlsplit(url.strip())
    ok = parts.scheme == "https" and bool(parts.hostname)
    if parts.scheme == "http" and is_loopback_host(parts.hostname):
        ok = True
    if not ok or parts.username or parts.password:
        raise ConfigError(f"{variable} must be an https:// URL (plain http is accepted for a loopback host only).")
    return url.strip().rstrip("/")


def make_client(base_url: str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        timeout=DEFAULT_TIMEOUT,
        transport=transport,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )


def send(client: httpx.Client, method: str, path: str, *, who: str, **kwargs: Any) -> httpx.Response:
    """Send a request, turning network failures into ProviderUnavailableError."""
    try:
        return client.request(method, path, **kwargs)
    except httpx.TimeoutException as exc:
        raise ProviderUnavailableError(f"{who} timed out ({type(exc).__name__}). Try again later.") from None
    except httpx.HTTPError as exc:
        raise ProviderUnavailableError(f"Could not reach {who} ({type(exc).__name__}). Try again later.") from None


def retry_after_seconds(response: httpx.Response) -> int | None:
    raw = (response.headers.get("Retry-After") or "").strip()
    if raw.isascii() and raw.isdigit():
        return int(raw)
    return None


def json_body(response: httpx.Response) -> dict[str, Any]:
    """Lenient: for reading error bodies. Never raises."""
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {"data": data}


def json_object(response: httpx.Response, who: str) -> dict[str, Any]:
    """Strict: a successful response must be a JSON object."""
    try:
        data = response.json()
    except ValueError:
        raise ProviderError(f"{who} returned a response that is not JSON (HTTP {response.status_code}).") from None
    if not isinstance(data, dict):
        raise ProviderError(f"{who} returned JSON of an unexpected shape (HTTP {response.status_code}).")
    return data


def dict_items(value: Any, who: str, what: str) -> list[dict[str, Any]]:
    """``value`` must be a list of objects (or absent)."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ProviderError(f"{who} returned a malformed '{what}' list.")
    return value


def guard_malformed(who: str) -> Callable[[F], F]:
    """Turn the exceptions that malformed upstream data provokes while it is
    being normalised into ``[provider_error]`` instead of an internal error."""

    def decorate(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except ProviderError:
                raise
            except (ValueError, TypeError, KeyError, IndexError, AttributeError, ArithmeticError) as exc:
                log.warning(
                    "malformed data from %s: %s", who, type(exc).__name__, exc_info=log.isEnabledFor(logging.DEBUG)
                )
                raise ProviderError(
                    f"{who} returned data in an unexpected format ({type(exc).__name__}). Try again later."
                ) from None

        return wrapper  # type: ignore[return-value]

    return decorate


def describe_wait(seconds: int | None) -> str:
    if not seconds:
        return ""
    if seconds >= 3600:
        return f" Retry in about {round(seconds / 3600, 1)} hours."
    if seconds >= 60:
        return f" Retry in about {round(seconds / 60)} minutes."
    return f" Retry in {seconds} seconds."
