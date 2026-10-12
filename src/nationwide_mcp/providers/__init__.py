"""Provider registry / factory. Configuration comes only from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from ..errors import ConfigError
from ..token_store import TokenStore, build_token_store
from ._http import validate_base_url
from .base import Provider

PROVIDERS = ("truelayer", "lunchflow", "enablebanking", "statement")


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ConfigError(f"{name} must be a positive integer.")
    return value


def read_private_key(env: Mapping[str, str]) -> str:
    """PEM text from ENABLEBANKING_PRIVATE_KEY (literal \\n allowed) or *_FILE."""
    inline = env.get("ENABLEBANKING_PRIVATE_KEY", "")
    if inline.strip():
        return inline.replace("\\n", "\n").strip() + "\n"
    path = env.get("ENABLEBANKING_PRIVATE_KEY_FILE", "")
    if path.strip():
        try:
            return Path(path.strip()).expanduser().read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            raise ConfigError(f"Cannot read ENABLEBANKING_PRIVATE_KEY_FILE ({type(exc).__name__}).") from None
    return ""


def build_provider(env: Mapping[str, str] | None = None, store: TokenStore | None = None) -> Provider:
    env = env if env is not None else os.environ
    name = (env.get("NATIONWIDE_PROVIDER") or "truelayer").strip().lower()

    if name == "truelayer":
        from .truelayer import TrueLayerConfig, TrueLayerProvider

        return TrueLayerProvider(TrueLayerConfig.from_env(env), store or build_token_store(env))

    if name == "lunchflow":
        from .lunchflow import DEFAULT_BASE_URL, LunchFlowProvider

        connected_on = None
        if (env.get("LUNCHFLOW_CONNECTED_ON") or "").strip():
            try:
                connected_on = date.fromisoformat(env["LUNCHFLOW_CONNECTED_ON"].strip())
            except ValueError:
                raise ConfigError("LUNCHFLOW_CONNECTED_ON must be YYYY-MM-DD.") from None
        override = (env.get("LUNCHFLOW_BASE_URL") or "").strip()
        return LunchFlowProvider(
            env.get("LUNCHFLOW_API_KEY", "").strip(),
            base_url=validate_base_url(override, "LUNCHFLOW_BASE_URL") if override else DEFAULT_BASE_URL,
            institution_filter=env.get("LUNCHFLOW_INSTITUTION_FILTER", "nationwide"),
            connected_on=connected_on,
            consent_days=_positive_int(env, "LUNCHFLOW_CONSENT_DAYS", 90),
            invert_sign=_truthy(env.get("LUNCHFLOW_INVERT_SIGN")),
        )

    if name == "enablebanking":
        from .enablebanking import DEFAULT_BASE_URL, EnableBankingProvider

        override = (env.get("ENABLEBANKING_BASE_URL") or "").strip()
        return EnableBankingProvider(
            env.get("ENABLEBANKING_APP_ID", "").strip(),
            read_private_key(env),
            store or build_token_store(env),
            session_id=(env.get("ENABLEBANKING_SESSION_ID") or "").strip() or None,
            base_url=validate_base_url(override, "ENABLEBANKING_BASE_URL") if override else DEFAULT_BASE_URL,
        )

    if name == "statement":
        from .statement import DEFAULT_MAX_BYTES, StatementProvider

        return StatementProvider(
            (env.get("NATIONWIDE_STATEMENT_DIR") or "").strip(),
            max_bytes=_positive_int(env, "NATIONWIDE_STATEMENT_MAX_BYTES", DEFAULT_MAX_BYTES),
        )

    raise ConfigError(f"Unknown NATIONWIDE_PROVIDER; choose one of: {', '.join(PROVIDERS)}.")


__all__ = ["PROVIDERS", "Provider", "build_provider", "read_private_key"]
