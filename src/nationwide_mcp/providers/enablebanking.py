"""Enable Banking provider (https://enablebanking.com/docs/api/reference/).

Auth: every request carries a short-lived RS256 JWT that we sign ourselves with
the application's private key (header ``kid`` = application id). Nothing is
exchanged with a token endpoint and nothing rotates; if the API answers 401 to
a GET we mint a fresh JWT once and retry before reporting an auth failure
(POST /auth and POST /sessions are never replayed).

Consent: the link helper creates a *session* that is valid until
``access.valid_until``. The session id (not secret on its own, useless without
the private key) lives in the token store or ENABLEBANKING_SESSION_ID.

Account ids: Enable Banking issues a new account ``uid`` for every session, so
we expose ``identification_hash`` (stable across sessions) as the account id
and translate to the current ``uid`` internally.

UNVERIFIED (written from the public API reference, never run against the
service): that Nationwide is reachable at all; the sign of ``balance_amount``
(passed through unchanged: no credit/debit logic is applied to balances, so a
credit-card or overdrawn balance may come out with the wrong sign); the
default transaction status filter (no ``transaction_status`` is sent, so
whether pending items are included is up to the API); pagination beyond
``continuation_key``; and the PSU headers (``Psu-Ip-Address`` etc. are not
sent, i.e. every call is presented as unattended).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import jwt

from ..errors import (
    AuthError,
    ConfigError,
    ConsentExpiredError,
    NotConnectedError,
    NotFoundError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
)
from ..models import (
    SOURCE,
    clean,
    guess_account_type,
    last4,
    london_date,
    make_transaction,
    to_minor,
    today_london,
    window_result,
)
from ..token_store import TokenStore
from ._http import (
    describe_wait,
    dict_items,
    guard_malformed,
    is_path_safe,
    json_body,
    json_object,
    make_client,
    retry_after_seconds,
    send,
    strip_control,
)
from .base import Provider

DEFAULT_BASE_URL = "https://api.enablebanking.com"
STORE_SECTION = "enablebanking"
JWT_TTL_SECONDS = 3600  # API maximum is 86400
MAX_PAGES = 500

_SESSION_GONE = {
    "EXPIRED_SESSION",
    "REVOKED_SESSION",
    "CLOSED_SESSION",
    "SESSION_DOES_NOT_EXIST",
    "WRONG_SESSION_STATUS",
}
_PENDING_STATUSES = {"PDNG", "HOLD"}
_SKIP_STATUSES = {"CNCL", "RJCT", "SCHD"}
_CURRENT_TYPES = ("ITBD", "CLBD", "XPCD", "OPBD", "PRCD", "OTHR")
_AVAILABLE_TYPES = ("ITAV", "CLAV", "OPAV")

WHO = "Enable Banking"
REAUTH_HELP = (
    "Re-run `nationwide-mcp-link link` (you will approve access in the Nationwide app) to create a new session."
)


class EnableBankingProvider(Provider):
    name = "enablebanking"

    def __init__(
        self,
        app_id: str,
        private_key: str,
        store: TokenStore,
        *,
        session_id: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ):
        if not app_id:
            raise ConfigError("ENABLEBANKING_APP_ID is not set.")
        if not private_key or "PRIVATE KEY" not in private_key:
            raise ConfigError(
                "Enable Banking private key missing: set ENABLEBANKING_PRIVATE_KEY (PEM text) or "
                "ENABLEBANKING_PRIVATE_KEY_FILE (path to the .pem downloaded when the application was registered)."
            )
        self._app_id = app_id
        self._private_key = private_key
        self._store = store
        self._session_override = session_id
        self._client = make_client(base_url, transport)
        self._clock = clock
        self._jwt: str | None = None
        self._jwt_exp = 0.0
        self._session_cache: dict[str, Any] | None = None

    def close(self) -> None:
        self._client.close()

    # -- auth ---------------------------------------------------------------
    def _token(self, force: bool = False) -> str:
        now = self._clock()
        if force or not self._jwt or now >= self._jwt_exp - 60:
            iat = int(now)
            try:
                self._jwt = jwt.encode(
                    {
                        "iss": "enablebanking.com",
                        "aud": "api.enablebanking.com",
                        "iat": iat,
                        "exp": iat + JWT_TTL_SECONDS,
                    },
                    self._private_key,
                    algorithm="RS256",
                    headers={"kid": self._app_id},
                )
            except Exception as exc:  # malformed key; never echo key material
                raise ConfigError(
                    f"The Enable Banking private key could not be used to sign a token ({type(exc).__name__})."
                ) from None
            self._jwt_exp = iat + JWT_TTL_SECONDS
        return self._jwt

    def _request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> dict:
        response = None
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {self._token(force=attempt == 2)}"}
            response = send(self._client, method, path, who="Enable Banking", params=params, json=body, headers=headers)
            error = json_body(response).get("error") if response.status_code >= 400 else None
            # A 401 that is not about the bank session means our JWT was refused
            # (expired / clock skew): sign a new one and retry exactly once. Only a
            # GET is replayed: POST /auth and POST /sessions are not idempotent.
            if method == "GET" and response.status_code == 401 and error not in _SESSION_GONE and attempt == 1:
                continue
            break
        assert response is not None
        if response.status_code < 300:
            return json_object(response, WHO)
        self._raise_for(response)
        raise AssertionError("unreachable")

    def _raise_for(self, response: httpx.Response) -> None:
        body = json_body(response)
        error = str(body.get("error") or "")
        detail = strip_control(clean(body.get("message")) or f"HTTP {response.status_code}", 200)
        status = response.status_code
        if error in _SESSION_GONE:
            raise ConsentExpiredError(f"The Nationwide consent is no longer valid ({error}: {detail}). {REAUTH_HELP}")
        if status == 429 or error == "ASPSP_RATE_LIMIT_EXCEEDED":
            wait = retry_after_seconds(response)
            raise RateLimitError(
                "Rate limit reached (Nationwide allows only a few unattended fetches per account per day)."
                + (describe_wait(wait) or " Retry in about 6 hours.")
                + " The consent is still valid; do not re-authorise.",
                retry_after_seconds=wait or 6 * 3600,
            )
        if status == 401:
            raise AuthError(
                f"Enable Banking rejected the signed token ({error or 'UNAUTHORIZED'}: {detail}). Check that "
                "ENABLEBANKING_APP_ID matches the private key and that the system clock is correct."
            )
        if error == "ASPSP_PSU_ACTION_REQUIRED":
            raise ConsentExpiredError(
                f"Nationwide needs you to take action before data can be shared ({detail}). {REAUTH_HELP}"
            )
        if status == 403 or error in {"ACCESS_DENIED", "UNAUTHORIZED_IP", "NO_ACCOUNTS_ADDED"}:
            raise AuthError(
                f"Enable Banking denied access ({error or 'FORBIDDEN'}: {detail}). In restricted (free) mode only "
                "accounts linked to the application in the Enable Banking control panel are readable."
            )
        if error in {"ACCOUNT_DOES_NOT_EXIST", "ASPSP_ACCOUNT_NOT_ACCESSIBLE"} or status == 404:
            raise NotFoundError(f"Account not available ({error or 'NOT_FOUND'}: {detail}). Call list_accounts.")
        if error in {"ASPSP_ERROR", "ASPSP_TIMEOUT"} or status in (408,) or status >= 500:
            raise ProviderUnavailableError(f"Upstream problem ({error or status}: {detail}). Try again later.")
        raise ProviderError(f"Enable Banking error ({error or status}: {detail}).")

    # -- session ------------------------------------------------------------
    def _stored(self) -> dict[str, Any]:
        return self._store.get(STORE_SECTION)

    def _session_id(self) -> str:
        sid = self._session_override or self._stored().get("session_id")
        if not sid:
            raise NotConnectedError(
                "No Nationwide session has been created yet. Run `nationwide-mcp-link link` once, then either "
                "keep the token file or set ENABLEBANKING_SESSION_ID."
            )
        if not is_path_safe(str(sid)):
            raise ConfigError("The Enable Banking session id contains unexpected characters.")
        return str(sid)

    def _session(self, refresh: bool = False) -> dict[str, Any]:
        """Session metadata: {session_id, valid_until, status, accounts:[...]}."""
        if self._session_cache is not None and not refresh:
            return self._session_cache
        sid = self._session_id()
        stored = self._stored()
        known = {a.get("uid"): a for a in stored.get("accounts", []) if stored.get("session_id") == sid}
        data = self._request("GET", f"/sessions/{sid}")
        accounts = []
        listed = data.get("accounts_data") or [{"uid": uid} for uid in data.get("accounts") or []]
        for item in dict_items(listed, WHO, "accounts"):
            uid = item.get("uid")
            if not is_path_safe(uid):  # it is interpolated into URL paths
                raise ProviderError("Enable Banking returned an account uid in an unexpected format.")
            merged = dict(known.get(uid) or {})
            merged.update({k: v for k, v in item.items() if v is not None})
            accounts.append(merged)
        session = {
            "session_id": sid,
            "status": data.get("status"),
            "valid_until": (data.get("access") or {}).get("valid_until") or stored.get("valid_until"),
            "aspsp": data.get("aspsp") or stored.get("aspsp"),
            "accounts": accounts,
        }
        status = str(session["status"] or "").upper()
        if status and status != "AUTHORIZED":
            raise ConsentExpiredError(f"The Nationwide session is {status}. {REAUTH_HELP}")
        expires = _parse_dt(session["valid_until"])
        if expires and expires <= datetime.fromtimestamp(self._clock(), UTC):
            raise ConsentExpiredError(f"The Nationwide consent expired on {expires.date().isoformat()}. {REAUTH_HELP}")
        self._session_cache = session
        return session

    def _resolve_uid(self, account_id: str) -> tuple[str, dict[str, Any]]:
        for account in self._session()["accounts"]:
            if account_id in (account.get("identification_hash"), account.get("uid")):
                return str(account["uid"]), account
        raise NotFoundError("That account is not covered by the current consent. Call list_accounts.")

    @staticmethod
    def _public_id(account: dict[str, Any]) -> str:
        return str(account.get("identification_hash") or account.get("uid"))

    # -- accounts -----------------------------------------------------------
    @guard_malformed(WHO)
    def list_accounts(self) -> list[dict[str, Any]]:
        out = []
        for account in self._session()["accounts"]:
            if not account.get("name") and not account.get("currency"):
                account.update(self._details(account["uid"]))
            ident = account.get("account_id") or {}
            other = ident.get("other") or {}
            number = ident.get("iban") or other.get("identification") or account.get("mask")
            out.append(
                {
                    "id": self._public_id(account),
                    "source": SOURCE,
                    "name": clean(account.get("name"))
                    or clean(account.get("product"))
                    or clean(account.get("details"))
                    or "Nationwide account",
                    "type": guess_account_type(
                        account.get("cash_account_type"), account.get("product"), account.get("name")
                    ),
                    "currency": clean(account.get("currency")) or "GBP",
                    "institution": "Nationwide Building Society",
                    "mask": last4(number),
                }
            )
        return out

    def _details(self, uid: str) -> dict[str, Any]:
        try:
            return self._request("GET", f"/accounts/{uid}/details")
        except (ConsentExpiredError, AuthError):
            raise
        except ProviderError:
            return {}  # details are cosmetic; balances/transactions still work

    # -- balance ------------------------------------------------------------
    @guard_malformed(WHO)
    def get_balance(self, account_id: str) -> dict[str, Any]:
        """UNVERIFIED sign: ``balance_amount`` is passed through as reported. No credit/debit
        indicator is applied, so check a credit-card or overdrawn balance against the bank."""
        uid, account = self._resolve_uid(account_id)
        raw = dict_items(self._request("GET", f"/accounts/{uid}/balances").get("balances"), WHO, "balances")
        balances = []
        for item in raw:
            amount = item.get("balance_amount") or {}
            if amount.get("amount") is None:
                raise ProviderError("Enable Banking returned a balance without an amount.")
            currency = clean(amount.get("currency")) or clean(account.get("currency")) or "GBP"
            balances.append(
                {
                    "type": clean(item.get("balance_type")) or "OTHR",
                    "name": clean(item.get("name")),
                    "amount_minor": to_minor(amount["amount"], currency),
                    "currency": currency,
                    "as_of": clean(item.get("last_change_date_time")) or clean(item.get("reference_date")),
                }
            )

        def pick(order: tuple[str, ...]) -> dict[str, Any] | None:
            for wanted in order:
                for bal in balances:
                    if bal["type"] == wanted:
                        return bal
            return None

        current = pick(_CURRENT_TYPES) or (balances[0] if balances else None)
        available = pick(_AVAILABLE_TYPES)
        return {
            "account_id": self._public_id(account),
            "source": SOURCE,
            "currency": (current or available or {}).get("currency") or clean(account.get("currency")) or "GBP",
            "current_minor": current["amount_minor"] if current else None,
            "available_minor": available["amount_minor"] if available else None,
            "as_of": (current or available or {}).get("as_of"),
            "balances": balances,
            "notes": "sign passed through from Enable Banking unverified; compare with the bank before relying on it",
        }

    # -- transactions -------------------------------------------------------
    @guard_malformed(WHO)
    def fetch_transactions(
        self, account_id: str, since: date, before: date, include_pending: bool = False
    ) -> dict[str, Any]:
        uid, account = self._resolve_uid(account_id)
        public_id = self._public_id(account)
        params: dict[str, Any] = {
            "date_from": since.isoformat(),
            "date_to": (before - timedelta(days=1)).isoformat(),  # API range is inclusive
        }
        today = today_london(self._clock)
        out = []
        for _ in range(MAX_PAGES):
            page = self._request("GET", f"/accounts/{uid}/transactions", params=params)
            for item in dict_items(page.get("transactions"), WHO, "transactions"):
                txn = self._normalise(item, public_id, account, today)
                if txn is not None:
                    out.append(txn)
            key = page.get("continuation_key")
            if not key:
                break
            params = {**params, "continuation_key": key}
        else:
            raise ProviderError("Enable Banking kept returning continuation keys; aborted after too many pages.")
        return window_result(out, since, before, include_pending)

    @staticmethod
    def _normalise(item: dict[str, Any], account_id: str, account: dict[str, Any], today: date) -> dict | None:
        status = str(item.get("status") or "BOOK").upper()
        if status in _SKIP_STATUSES:
            return None
        pending = status in _PENDING_STATUSES
        amount = item.get("transaction_amount") or {}
        currency = clean(amount.get("currency")) or clean(account.get("currency")) or "GBP"
        minor = abs(to_minor(amount.get("amount"), currency))
        indicator = str(item.get("credit_debit_indicator") or "").upper()
        if indicator == "DBIT":
            minor = -minor
        elif indicator == "CRDT":
            pass
        elif str(amount.get("amount")).strip().startswith("-"):
            minor = -minor
        else:
            # No indicator and no sign: the direction is unknown. Refuse rather than guess.
            raise ProviderError(
                "Enable Banking returned a transaction with neither a credit_debit_indicator nor a signed "
                "amount, so its direction (money in or out) cannot be determined."
            )
        day = (
            london_date(item.get("booking_date"))
            or london_date(item.get("transaction_date"))
            or london_date(item.get("value_date"))
            or (today if pending else None)
        )
        if day is None:
            raise ProviderError("Enable Banking returned a booked transaction without a date.")
        counterparty = (item.get("creditor") if minor < 0 else item.get("debtor")) or {}
        merchant = clean(counterparty.get("name"))
        remittance = item.get("remittance_information") or []
        if isinstance(remittance, str):
            remittance = [remittance]
        code = item.get("bank_transaction_code") or {}
        description = clean(" ".join(str(r) for r in remittance if r)) or merchant or clean(code.get("description"))
        notes = []
        if clean(item.get("note")):
            notes.append(clean(item.get("note")))
        if clean(code.get("description")) and clean(code.get("description")) != description:
            notes.append(f"type: {clean(code.get('description'))}")
        if item.get("merchant_category_code"):
            notes.append(f"mcc: {item['merchant_category_code']}")
        if pending:
            notes.append("pending: id and amount may change when this settles")
        # entry_reference is the identifier Enable Banking documents as stable
        # for booked items; pending items only carry it if the bank keeps it.
        ref = item.get("entry_reference") or (None if pending else item.get("transaction_id"))
        return make_transaction(
            account_id=account_id,
            day=day,
            amount_minor=minor,
            currency=currency,
            description=description,
            merchant=merchant,
            pending=pending,
            notes="; ".join(notes) or None,
            provider_ref=ref,
        )

    # -- status -------------------------------------------------------------
    @guard_malformed(WHO)
    def connection_status(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "provider": self.name,
            "connected": False,
            "status": "UNKNOWN",
            "consent_expires_at": None,
            "consent_expiry_is_estimate": False,
            "days_remaining": None,
            "needs_reauthorisation": False,
            "accounts": [],
            "message": "",
        }
        stored = self._stored()
        if stored.get("valid_until"):
            result["consent_expires_at"] = stored["valid_until"]
        try:
            session = self._session(refresh=True)
        except ProviderError as exc:
            result["status"] = result["error_code"] = exc.code
            result["needs_reauthorisation"] = isinstance(exc, (ConsentExpiredError, NotConnectedError))
            result["message"] = exc.message
            return result
        result["connected"] = True
        result["status"] = session.get("status") or "AUTHORIZED"
        result["consent_expires_at"] = session.get("valid_until")
        result["accounts"] = [{"id": self._public_id(a)} for a in session["accounts"]]
        expires = _parse_dt(session.get("valid_until"))
        if expires:
            remaining = (expires - datetime.fromtimestamp(self._clock(), UTC)).days
            result["days_remaining"] = remaining
            result["message"] = f"Consent valid until {expires.date().isoformat()} ({remaining} day(s) left)."
            if remaining <= 14:
                result["message"] += f" Renew soon: {REAUTH_HELP}"
        else:
            result["message"] = "Session is authorised; the bank did not report an expiry date."
        return result

    # -- used only by the link helper ---------------------------------------
    def list_aspsps(self, country: str = "GB") -> list[dict[str, Any]]:
        return dict_items(self._request("GET", "/aspsps", params={"country": country}).get("aspsps"), WHO, "aspsps")

    def start_authorisation(
        self, aspsp_name: str, country: str, redirect_url: str, valid_until: datetime, state: str
    ) -> dict[str, Any]:
        body = {
            "access": {"valid_until": valid_until.astimezone(UTC).isoformat(), "balances": True, "transactions": True},
            "aspsp": {"name": aspsp_name, "country": country},
            "state": state,
            "redirect_url": redirect_url,
            "psu_type": "personal",
        }
        return self._request("POST", "/auth", body=body)

    def create_session(self, code: str) -> dict[str, Any]:
        data = self._request("POST", "/sessions", body={"code": code})
        record = {
            "session_id": data.get("session_id"),
            "valid_until": (data.get("access") or {}).get("valid_until"),
            "aspsp": data.get("aspsp"),
            "created": datetime.fromtimestamp(self._clock(), UTC).replace(microsecond=0).isoformat(),
            "accounts": [
                {
                    "uid": a.get("uid"),
                    "identification_hash": a.get("identification_hash"),
                    "name": a.get("name"),
                    "product": a.get("product"),
                    "details": a.get("details"),
                    "currency": a.get("currency"),
                    "cash_account_type": a.get("cash_account_type"),
                    "mask": last4(
                        ((a.get("account_id") or {}).get("iban"))
                        or (((a.get("account_id") or {}).get("other") or {}).get("identification"))
                    ),
                }
                for a in data.get("accounts") or []
            ],
        }
        self._store.put(STORE_SECTION, record)
        self._session_cache = None
        return record


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
