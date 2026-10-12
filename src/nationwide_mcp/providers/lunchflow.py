"""Lunch Flow personal API provider (https://www.lunchflow.app/docs/api/personal-api-overview).

Lunch Flow is a consumer subscription that holds the Open Banking consent for
you (UK banks via its upstream aggregators) and exposes a read-only REST API
authenticated with a static API key:

    GET /accounts
    GET /accounts/{id}/balance
    GET /accounts/{id}/transactions?from=YYYY-MM-DD&to=YYYY-MM-DD&include_pending=true

The bank is linked in the Lunch Flow dashboard, not here, so the API does not
expose a consent expiry date. ``connection_status`` therefore reports the
per-account ``status`` field and, if LUNCHFLOW_CONNECTED_ON is set, an
*estimated* expiry (UK consents need re-confirming roughly every 90 days).

UNVERIFIED (written from the public docs, never run against the service): the
sign of ``amount`` (assumed negative = money out; LUNCHFLOW_INVERT_SIGN flips
it), the sign of a credit-card balance, whether ``id`` is stable for pending
items, and pagination: the docs show a ``total`` field but no paging
parameters, so a response holding fewer items than ``total`` is treated as an
error rather than silently returned truncated.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from ..errors import (
    AuthError,
    ConfigError,
    ConsentExpiredError,
    NotFoundError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
)
from ..models import (
    SOURCE,
    clean,
    guess_account_type,
    london_date,
    make_transaction,
    to_minor,
    today_london,
    window_result,
)
from ._http import (
    describe_wait,
    dict_items,
    guard_malformed,
    is_path_safe,
    json_body,
    json_object,
    make_client,
    require_path_id,
    retry_after_seconds,
    send,
    strip_control,
)
from .base import Provider

DEFAULT_BASE_URL = "https://lunchflow.app/api/v1"
WHO = "Lunch Flow"
_RECONNECT_HINTS = ("reconnect", "re-connect", "reauth", "re-auth", "expired", "consent", "disconnected")


class LunchFlowProvider(Provider):
    name = "lunchflow"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        institution_filter: str | None = "nationwide",
        connected_on: date | None = None,
        consent_days: int = 90,
        invert_sign: bool = False,
        transport: httpx.BaseTransport | None = None,
        today: Callable[[], date] | None = None,
    ):
        if not api_key:
            raise ConfigError("LUNCHFLOW_API_KEY is not set. Create an API destination in the Lunch Flow dashboard.")
        self._api_key = api_key
        self._filter = (institution_filter or "").strip().lower()
        self._connected_on = connected_on
        self._consent_days = consent_days
        self._invert = invert_sign
        self._client = make_client(base_url, transport)
        self._today = today or today_london

    def close(self) -> None:
        self._client.close()

    # -- HTTP ---------------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = send(self._client, "GET", path, who=WHO, params=params, headers={"x-api-key": self._api_key})
        if response.status_code < 300:
            return json_object(response, WHO)
        body = json_body(response)
        detail = strip_control(
            clean(body.get("message")) or clean(body.get("error")) or f"HTTP {response.status_code}", 200
        )
        status = response.status_code
        if status in (401, 403):
            raise AuthError(
                f"Lunch Flow rejected the API key ({detail}). It may have been revoked or the subscription "
                "has lapsed. Create a new API destination key in the Lunch Flow dashboard and update LUNCHFLOW_API_KEY."
            )
        if status == 429:  # never retried
            wait = retry_after_seconds(response)
            raise RateLimitError(f"Lunch Flow rate limit reached.{describe_wait(wait)}", retry_after_seconds=wait)
        if any(hint in detail.lower() for hint in _RECONNECT_HINTS):
            raise ConsentExpiredError(
                f"Lunch Flow reports the bank connection needs attention ({detail}). "
                "Reconnect Nationwide in the Lunch Flow dashboard (this needs you and your banking app)."
            )
        if status == 404:
            raise NotFoundError(f"Lunch Flow does not know this account ({detail}). Call list_accounts for valid ids.")
        if status >= 500:
            raise ProviderUnavailableError(f"Lunch Flow is unavailable ({detail}). Try again later.")
        raise ProviderError(f"Lunch Flow returned an error ({detail}).")

    def _list(self, path: str, key: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """One list endpoint. Refuses a response that admits to being incomplete."""
        body = self._get(path, params)
        items = dict_items(body.get(key), WHO, key)
        total = body.get("total")
        if isinstance(total, int) and not isinstance(total, bool) and total > len(items):
            raise ProviderError(
                f"Lunch Flow returned {len(items)} of {total} {key}: the response is truncated and paging is "
                "not documented, so nothing is returned rather than a partial list. Ask for a shorter date range."
            )
        return items

    # -- accounts -----------------------------------------------------------
    def _raw_accounts(self) -> list[dict[str, Any]]:
        accounts = self._list("/accounts", "accounts")
        if not self._filter:
            return accounts
        return [a for a in accounts if self._filter in str(a.get("institution_name") or "").lower()]

    def _require_account(self, account_id: str) -> dict[str, Any]:
        require_path_id(account_id)
        for account in self._raw_accounts():
            if str(account.get("id")) == account_id:
                return account
        raise NotFoundError(
            "That account is not one of the Nationwide accounts visible to this API key. "
            "Call list_accounts for valid ids."
        )

    @guard_malformed(WHO)
    def list_accounts(self) -> list[dict[str, Any]]:
        return [
            {
                "id": str(a.get("id")),
                "source": SOURCE,
                "name": clean(a.get("name")) or "Nationwide account",
                "type": guess_account_type(a.get("name")),
                "currency": clean(a.get("currency")) or "GBP",
                "institution": clean(a.get("institution_name")),
                "mask": None,
                "status": clean(a.get("status")),
            }
            for a in self._raw_accounts()
            if is_path_safe(str(a.get("id")))
        ]

    # -- balance ------------------------------------------------------------
    @guard_malformed(WHO)
    def get_balance(self, account_id: str) -> dict[str, Any]:
        """The sign is passed through as Lunch Flow reports it (UNVERIFIED for credit cards)."""
        account = self._require_account(account_id)
        balance = self._get(f"/accounts/{account_id}/balance").get("balance")
        if not isinstance(balance, dict) or balance.get("amount") is None:
            raise ProviderError("Lunch Flow returned no balance for this account.")
        currency = clean(balance.get("currency")) or clean(account.get("currency")) or "GBP"
        amount = to_minor(balance["amount"], currency)
        as_of = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        return {
            "account_id": account_id,
            "source": SOURCE,
            "currency": currency,
            "current_minor": amount,
            "available_minor": None,
            "as_of": as_of,
            "balances": [{"type": "current", "amount_minor": amount, "currency": currency, "as_of": as_of}],
        }

    # -- transactions -------------------------------------------------------
    @guard_malformed(WHO)
    def fetch_transactions(
        self, account_id: str, since: date, before: date, include_pending: bool = False
    ) -> dict[str, Any]:
        account = self._require_account(account_id)
        params = {
            "from": since.isoformat(),
            "to": (before - timedelta(days=1)).isoformat(),  # their range is inclusive
            # Always asked for, so that pending items left out of the result can be counted.
            "include_pending": "true",
        }
        raw = self._list(f"/accounts/{account_id}/transactions", "transactions", params)
        fallback_currency = clean(account.get("currency")) or "GBP"
        out = []
        for item in raw:
            pending = bool(item.get("isPending"))
            day = london_date(item.get("date"))
            if day is None:
                raise ProviderError("Lunch Flow returned a transaction without a valid date.")
            currency = clean(item.get("currency")) or fallback_currency
            amount = to_minor(item.get("amount"), currency)
            if self._invert:
                amount = -amount
            notes = "pending: id and amount may change when this settles" if pending else None
            out.append(
                make_transaction(
                    account_id=account_id,
                    day=day,
                    amount_minor=amount,
                    currency=currency,
                    description=item.get("description") or item.get("merchant"),
                    merchant=item.get("merchant"),
                    pending=pending,
                    notes=notes,
                    provider_ref=item.get("id"),
                )
            )
        return window_result(out, since, before, include_pending)

    # -- status -------------------------------------------------------------
    @guard_malformed(WHO)
    def connection_status(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "provider": self.name,
            "connected": False,
            "status": "UNKNOWN",
            "consent_expires_at": None,
            "consent_expiry_is_estimate": True,
            "days_remaining": None,
            "needs_reauthorisation": False,
            "accounts": [],
            "message": "",
        }
        try:
            accounts = self._raw_accounts()
        except ProviderError as exc:
            result["status"] = result["error_code"] = exc.code
            result["needs_reauthorisation"] = isinstance(exc, (ConsentExpiredError, AuthError))
            result["message"] = exc.message
            return result

        result["accounts"] = [{"id": str(a.get("id")), "status": clean(a.get("status"))} for a in accounts]
        inactive = [a for a in accounts if str(a.get("status") or "").upper() != "ACTIVE"]
        if not accounts:
            result["status"] = "NO_ACCOUNTS"
            result["error_code"] = "not_connected"
            result["needs_reauthorisation"] = True
            result["message"] = (
                "No Nationwide accounts are visible to this API key. Connect Nationwide in the Lunch Flow "
                "dashboard, or adjust LUNCHFLOW_INSTITUTION_FILTER."
            )
            return result
        if inactive:
            result["status"] = "NEEDS_ATTENTION"
            result["error_code"] = "consent_expired"
            result["needs_reauthorisation"] = True
            result["message"] = (
                f"{len(inactive)} of {len(accounts)} account(s) are not ACTIVE. Reconnect Nationwide in the "
                "Lunch Flow dashboard."
            )
        else:
            result["connected"] = True
            result["status"] = "ACTIVE"
            result["message"] = "All Nationwide accounts are ACTIVE."

        if self._connected_on:
            expires = self._connected_on + timedelta(days=self._consent_days)
            remaining = (expires - self._today()).days
            result["consent_expires_at"] = expires.isoformat()
            result["days_remaining"] = remaining
            if remaining <= 0:
                result["needs_reauthorisation"] = True
                result["message"] += (
                    f" The estimated {self._consent_days}-day consent window has passed; expect to reconnect."
                )
            elif remaining <= 14:
                result["message"] += f" Estimated consent expiry in {remaining} day(s): reconnect soon."
        else:
            result["message"] += (
                " Lunch Flow does not expose the consent expiry; set LUNCHFLOW_CONNECTED_ON=YYYY-MM-DD "
                "to get an estimated date."
            )
        return result
