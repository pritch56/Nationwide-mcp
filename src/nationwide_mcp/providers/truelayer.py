"""TrueLayer Data API v1 provider for Nationwide (accounts AND cards), read-only.

Docs used (read October 2026; nothing here has been run against the live API):
  https://docs.truelayer.com/docs/account-data-requests
  https://docs.truelayer.com/docs/card-data-requests
  https://docs.truelayer.com/reference/generateaccesstoken
  https://docs.truelayer.com/docs/data-api-errors
  https://auth.truelayer.com/api/providers   (lists provider_id "ob-nationwide" with scopes
      info, accounts, balance, transactions, direct_debits, standing_orders, cards, offline_access)

Sign conventions differ per endpoint family, so they are handled separately:

* /data/v1/accounts/...  amount NEGATIVE = money out (already our convention);
                         balance `current` positive = money in the account.
* /data/v1/cards/...     amount POSITIVE = money out (inverted here);
                         balance `current` positive = amount owed (inverted here).

In both families `transaction_type` (DEBIT / CREDIT) is treated as authoritative when present.

Dates: `timestamp` is converted to a Europe/London calendar date. Because the bank may filter
`from`/`to` in UTC, each request asks for one day earlier than the window and the result is
filtered locally. Long ranges are walked in windows of at most 88 days that overlap by one day;
repeats are removed by id (or, for items without a durable id, by counting).

Tokens: the access token lasts about an hour; the refresh token is long-lived and MAY be
rotated on any refresh. After a refresh the new token set is held in memory and used even if
writing it to the token store fails; the write is retried on the next call and the superseded
refresh token is never sent again. A token in the store always wins over the environment seed.
Each bank is a separate TrueLayer connection with its own refresh token.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from ..errors import AuthError as TokenExpiredError
from ..errors import (
    ConfigError,
    ConsentExpiredError,
    InvalidRequestError,
    NotConnectedError,
    NotFoundError,
    NotSupportedError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    ScaRequiredError,
    TokenStoreError,
)
from ..models import (
    SOURCE,
    clean,
    last4,
    london_date,
    make_transaction,
    to_minor,
    today_london,
    today_utc,
    window_result,
)
from ..token_store import TokenStore
from ._http import DEFAULT_TIMEOUT, USER_AGENT, dict_items, guard_malformed, require_path_id, validate_base_url
from .base import Provider

log = logging.getLogger("nationwide_mcp.truelayer")

ENVIRONMENTS = {
    "live": ("https://auth.truelayer.com", "https://api.truelayer.com"),
    "sandbox": ("https://auth.truelayer-sandbox.com", "https://api.truelayer-sandbox.com"),
}
SCOPES = "info accounts balance cards transactions offline_access"
STORE_SECTION = "truelayer"
EXPECTED_PROVIDER_ID = "ob-nationwide"
RELINK_HINT = "Re-authorise Nationwide by running `nationwide-mcp-link link` on your own machine."
MAX_WINDOW_DAYS = 88  # TrueLayer's own default span; also a safe single-request span
WINDOW_OVERLAP_DAYS = 1
#: Hard ceiling on upstream requests (token + data, retries included) made by ONE tool call.
MAX_REQUESTS_PER_CALL = 30
CONSENT_DAYS = 90
REAUTH_WARNING_DAYS = 14
WHO = "TrueLayer"


class TrueLayerConfig:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        environment: str = "live",
        auth_base: str | None = None,
        api_base: str | None = None,
        seed_refresh_token: str | None = None,
        token_encoding: str = "json",
        window_days: int = MAX_WINDOW_DAYS,
    ):
        if environment not in ENVIRONMENTS:
            raise ConfigError("TRUELAYER_ENV must be 'live' or 'sandbox'.")
        if not client_id or not client_secret:
            raise ConfigError(
                "TRUELAYER_CLIENT_ID and TRUELAYER_CLIENT_SECRET must be set (from the TrueLayer Console). "
                "See README.md."
            )
        default_auth, default_api = ENVIRONMENTS[environment]
        self.client_id = client_id
        self.client_secret = client_secret
        self.environment = environment
        self.auth_base = validate_base_url(auth_base, "TRUELAYER_AUTH_BASE") if auth_base else default_auth
        self.api_base = validate_base_url(api_base, "TRUELAYER_API_BASE") if api_base else default_api
        self.seed_refresh_token = seed_refresh_token or None
        self.token_encoding = token_encoding if token_encoding in ("json", "form") else "json"
        self.window_days = max(1, min(int(window_days), MAX_WINDOW_DAYS))

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> TrueLayerConfig:
        try:
            window_days = int(env.get("TRUELAYER_WINDOW_DAYS") or MAX_WINDOW_DAYS)
        except ValueError:
            raise ConfigError("TRUELAYER_WINDOW_DAYS must be an integer.") from None
        return cls(
            client_id=(env.get("TRUELAYER_CLIENT_ID") or "").strip(),
            client_secret=(env.get("TRUELAYER_CLIENT_SECRET") or "").strip(),
            environment=(env.get("TRUELAYER_ENV") or "live").strip().lower(),
            auth_base=(env.get("TRUELAYER_AUTH_BASE") or "").strip() or None,
            api_base=(env.get("TRUELAYER_API_BASE") or "").strip() or None,
            # The refresh token belongs to ONE bank connection, hence the bank-specific name.
            # It only seeds an EMPTY token store; see load_tokens().
            seed_refresh_token=(env.get("NATIONWIDE_TRUELAYER_REFRESH_TOKEN") or "").strip() or None,
            token_encoding=(env.get("TRUELAYER_TOKEN_ENCODING") or "json").strip().lower(),
            window_days=window_days,
        )

    def __repr__(self) -> str:  # never leak the secret through logs / tracebacks
        return f"TrueLayerConfig(client_id={self.client_id!r}, environment={self.environment!r})"


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _public(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Entry point of one tool call: fresh request budget + malformed-data guard."""
    guarded = guard_malformed(WHO)(fn)

    def wrapper(self: TrueLayerProvider, *args: Any, **kwargs: Any) -> Any:
        self._call.left = MAX_REQUESTS_PER_CALL
        try:
            return guarded(self, *args, **kwargs)
        finally:
            self._call.left = None

    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper


class TrueLayerProvider(Provider):
    name = "truelayer"

    def __init__(
        self,
        config: TrueLayerConfig,
        store: TokenStore,
        *,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 2,
        backoff_seconds: float = 1.0,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self.store = store
        self._client = httpx.Client(
            timeout=DEFAULT_TIMEOUT,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        self._lock = threading.RLock()  # serialises token reads, refreshes and saves
        self._call = threading.local()  # per-tool-call request budget
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._clock = clock
        self._kinds: dict[str, str] = {}  # account_id -> "accounts" | "cards"
        self._memory: dict[str, Any] | None = None  # newest token set this process has seen
        self._unsaved = False  # True while _memory is newer than the store
        self.store_error: TokenStoreError | None = None

    def close(self) -> None:
        self._client.close()

    def _iso_now(self) -> str:
        return datetime.fromtimestamp(self._clock(), UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _spend(self) -> None:
        left = getattr(self._call, "left", None)
        if left is None:  # not inside a tool call (link helper)
            return
        if left <= 0:
            raise ProviderError(
                f"Stopped after {MAX_REQUESTS_PER_CALL} upstream requests in one call (safety limit). "
                "Ask for a shorter date range."
            )
        self._call.left = left - 1

    # ------------------------------------------------------------------ tokens

    def _store_put(self, tokens: dict[str, Any]) -> bool:
        try:
            self.store.put(STORE_SECTION, tokens)
        except Exception as exc:  # the token must survive in memory whatever the store does
            self._unsaved = True
            self.store_error = (
                exc
                if isinstance(exc, TokenStoreError)
                else TokenStoreError(f"The token store could not be written ({type(exc).__name__}).")
            )
            log.warning(
                "token store write failed (%s); keeping the tokens in memory and retrying on the next call",
                type(exc).__name__,
            )
            return False
        self._unsaved = False
        self.store_error = None
        return True

    def _store_get(self) -> dict[str, Any]:
        try:
            return self.store.get(STORE_SECTION)
        except ProviderError:
            raise
        except Exception as exc:
            raise TokenStoreError(f"The token store could not be read ({type(exc).__name__}).") from None

    def load_tokens(self) -> dict[str, Any]:
        """Current token set. Precedence: tokens this process could not save yet, then the token
        store, then (only if the store is empty) the NATIONWIDE_TRUELAYER_REFRESH_TOKEN seed."""
        with self._lock:
            if self._unsaved and self._memory:
                self._store_put(self._memory)  # retry the failed save
                return dict(self._memory)
            try:
                tokens = self._store_get()
            except TokenStoreError as exc:
                if self._memory:
                    self.store_error = exc
                    return dict(self._memory)
                raise
            if tokens.get("refresh_token"):
                self._memory = dict(tokens)
                return tokens
            seed = self.config.seed_refresh_token
            if seed:
                return {"refresh_token": seed, "seed_fingerprint": _fingerprint(seed)}
            raise NotConnectedError(
                "Nationwide has not been linked yet (no refresh token in the token store and "
                "NATIONWIDE_TRUELAYER_REFRESH_TOKEN is not set). " + RELINK_HINT
            )

    def _token_request(self, payload: dict[str, str]) -> dict[str, Any]:
        url = f"{self.config.auth_base}/connect/token"
        kwargs: dict[str, Any] = {"json": payload} if self.config.token_encoding == "json" else {"data": payload}
        self._spend()
        try:
            resp = self._client.post(url, **kwargs)
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"Could not reach TrueLayer's token endpoint ({type(exc).__name__}). Safe to retry later."
            ) from None
        if resp.status_code == 200:
            return _json(resp)
        error = _error_code(resp)
        if error == "invalid_grant":
            raise ConsentExpiredError(
                "TrueLayer rejected the refresh token (invalid_grant): the Nationwide consent has expired, "
                "been revoked, or the token was superseded by a newer one. " + RELINK_HINT
            )
        if error in ("invalid_client", "unauthorized_client"):
            raise ConfigError(
                f"TrueLayer rejected the client credentials ({error}). Check TRUELAYER_CLIENT_ID / "
                "TRUELAYER_CLIENT_SECRET and that TRUELAYER_ENV matches them (live vs sandbox)."
            )
        if error == "invalid_authorization_code":
            raise InvalidRequestError(
                "The authorisation code is wrong, already used, or older than 5 minutes. Start the link flow again."
            )
        if resp.status_code == 429:  # never retried
            raise RateLimitError(
                "TrueLayer's token endpoint is rate limiting requests. Try again later.",
                retry_after_seconds=_retry_after(resp),
            )
        raise ProviderUnavailableError(
            f"TrueLayer token endpoint returned HTTP {resp.status_code} ({error or 'no error code'})."
        )

    def persist(self, previous: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        """Adopt a token response. The new set is kept in memory first, then written to the
        store; a failed write is remembered (``store_error``) and retried on the next call."""
        tokens = {k: v for k, v in previous.items() if v is not None}
        tokens["access_token"] = body.get("access_token")
        tokens["access_token_expires_at"] = self._clock() + float(body.get("expires_in") or 3600)
        # Refresh tokens may rotate: always keep the newest; keep the old one if none is returned.
        if body.get("refresh_token"):
            tokens["refresh_token"] = body["refresh_token"]
        tokens["updated_at"] = self._iso_now()
        with self._lock:
            self._memory = dict(tokens)
            self._store_put(tokens)
        return tokens

    def _refresh(self, tokens: dict[str, Any]) -> dict[str, Any]:
        def request(current: dict[str, Any]) -> dict[str, Any]:
            return self._token_request(
                {
                    "grant_type": "refresh_token",
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "refresh_token": current["refresh_token"],
                }
            )

        try:
            body = request(tokens)
        except ConsentExpiredError:
            # Another process (the link helper) may have stored a newer token since we read ours.
            newer = {} if self._unsaved else self._store_get()
            if not newer.get("refresh_token") or newer["refresh_token"] == tokens["refresh_token"]:
                raise
            tokens = newer
            body = request(tokens)
        if not body.get("access_token"):
            raise ProviderUnavailableError("TrueLayer's token response did not contain an access token.")
        return self.persist(tokens, body)

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> dict[str, Any]:
        """Used only by the local link helper. Overwrites whatever the store held."""
        payload = {
            "grant_type": "authorization_code",
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }
        if code_verifier:
            payload["code_verifier"] = code_verifier  # PKCE
        body = self._token_request(payload)
        if not body.get("refresh_token") or not body.get("access_token"):
            raise ProviderError(
                "TrueLayer did not return a refresh token. The 'offline_access' scope must be enabled for the "
                "application and requested in the auth link."
            )
        tokens = self.persist(
            {"connected_at": self._iso_now(), "seed_fingerprint": _fingerprint(body["refresh_token"])}, body
        )
        if self.store_error is not None:
            raise self.store_error  # linking is pointless if the token cannot be kept
        return tokens

    def _access_token(self, rejected: str | None = None) -> str:
        """A usable access token. ``rejected`` is a token the API just answered 401 to: refresh
        only if that is still the current one, otherwise reuse the newer token another thread got."""
        with self._lock:  # one refresh at a time
            tokens = self.load_tokens()
            current = tokens.get("access_token")
            try:
                expires_at = float(tokens.get("access_token_expires_at") or 0)
            except (TypeError, ValueError):
                expires_at = 0.0
            usable = bool(current) and expires_at - 60 > self._clock()
            if usable and current != rejected:
                return str(current)
            return str(self._refresh(tokens)["access_token"])

    # ---------------------------------------------------------------- requests

    def _get(self, path: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        url = f"{self.config.api_base}{path}"
        refreshed = False
        attempt = 0
        token = self._access_token()
        while True:
            self._spend()
            try:
                resp = self._client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as exc:
                if attempt < self._max_retries:
                    attempt += 1
                    time.sleep(self._backoff * attempt)
                    continue
                raise ProviderUnavailableError(
                    f"Could not reach TrueLayer ({type(exc).__name__}). Safe to retry later."
                ) from None

            status, error = resp.status_code, _error_code(resp)
            if status == 200:
                try:
                    body = resp.json()
                except ValueError:
                    raise ProviderError("TrueLayer returned a response that is not JSON.") from None
                results = body.get("results") if isinstance(body, dict) else None
                if not isinstance(results, list):
                    raise ProviderError("Unexpected response shape from TrueLayer (no 'results' list).")
                return dict_items(results, WHO, "results")
            if status == 401:
                if not refreshed:
                    refreshed = True
                    token = self._access_token(rejected=token)
                    continue
                raise TokenExpiredError(
                    f"TrueLayer rejected the access token even after refreshing it ({error or 'unauthorized'}). "
                    + RELINK_HINT
                )
            if status == 403:
                if error == "sca_exceeded":
                    raise ScaRequiredError(
                        "Nationwide requires a fresh authentication for this request (sca_exceeded). This happens "
                        "when asking for history older than about 90 days long after linking. Request a more "
                        "recent range, or re-link and fetch the older history within 5 minutes."
                    )
                raise ConsentExpiredError(
                    f"Access to the Nationwide data was denied ({error or 'access_denied'}): the consent has "
                    "expired or been revoked. " + RELINK_HINT
                )
            if status == 404:
                raise NotFoundError(
                    "TrueLayer does not know this account (account_not_found). Call list_accounts for current "
                    "account_id values."
                )
            if status == 429:  # never retried
                retry_after = _retry_after(resp)
                raise RateLimitError(
                    f"Rate limited ({error or 'too_many_requests'}). Banks typically allow only a handful of "
                    "unattended data fetches per day per endpoint. Do not retry immediately"
                    + (f"; retry after {retry_after}s." if retry_after else "; retry in 6 hours or more.")
                    + " The consent is still valid; do not re-authorise.",
                    retry_after_seconds=retry_after,
                )
            if status == 400:
                detail = (clean(_json(resp).get("error_description")) or "")[:200]
                raise InvalidRequestError(
                    f"TrueLayer rejected the request ({error or 'bad_request'}). {detail}".strip()
                )
            if status == 501:
                raise NotSupportedError(
                    f"Nationwide does not support this endpoint via TrueLayer ({error or 'endpoint_not_supported'})."
                )
            if status >= 500:
                if attempt < self._max_retries:
                    attempt += 1
                    time.sleep(self._backoff * attempt)
                    continue
                raise ProviderUnavailableError(
                    f"TrueLayer / Nationwide is temporarily unavailable (HTTP {status}, {error or 'no error code'}). "
                    "Safe to retry later."
                )
            raise ProviderError(f"Unexpected HTTP {status} from TrueLayer ({error or 'no error code'}).")

    # ---------------------------------------------------------------- accounts

    def _fetch_all(self) -> list[tuple[str, dict[str, Any]]]:
        """[(kind, raw)] for current/savings accounts and credit cards."""
        out: list[tuple[str, dict[str, Any]]] = []
        accounts_ok = False
        try:
            out += [("accounts", a) for a in self._get("/data/v1/accounts")]
            accounts_ok = True
        except NotSupportedError:
            pass
        try:
            out += [("cards", c) for c in self._get("/data/v1/cards")]
        except NotSupportedError:
            pass
        except ConsentExpiredError:
            # The accounts call just succeeded, so the consent is alive: the user simply did not
            # share any card (or has none). Only re-raise if nothing at all is readable.
            if not accounts_ok:
                raise
            log.info("cards endpoint not permitted for this consent; continuing with accounts only")
        if self.config.environment == "live":
            foreign = {
                str((raw.get("provider") or {}).get("provider_id"))
                for _, raw in out
                if (raw.get("provider") or {}).get("provider_id") not in (None, "", EXPECTED_PROVIDER_ID)
            }
            if foreign:
                raise ConfigError(
                    f"The stored refresh token belongs to another bank connection ({', '.join(sorted(foreign))}), "
                    "not Nationwide. Each bank connection has its own refresh token: run `nationwide-mcp-link reset` "
                    "then `nationwide-mcp-link link` (or correct NATIONWIDE_TRUELAYER_REFRESH_TOKEN), and never "
                    "reuse a token issued for a different bank."
                )
        self._kinds = {str(raw.get("account_id")): kind for kind, raw in out}
        return out

    def _kind(self, account_id: str) -> str:
        require_path_id(account_id)
        if account_id not in self._kinds:
            self._fetch_all()
        if account_id not in self._kinds:
            raise NotFoundError("That account is not part of the Nationwide consent. Call list_accounts for valid ids.")
        return self._kinds[account_id]

    @_public
    def list_accounts(self) -> list[dict[str, Any]]:
        out = []
        for kind, raw in self._fetch_all():
            if kind == "cards":
                account_type = "credit_card"
                mask = last4(raw.get("partial_card_number"))
                default_name = "Nationwide credit card"
            else:
                tl_type = str(raw.get("account_type") or "").upper()
                account_type = "savings" if "SAVINGS" in tl_type else "current" if "TRANSACTION" in tl_type else "other"
                mask = last4((raw.get("account_number") or {}).get("number"))
                default_name = "Nationwide account"
            out.append(
                {
                    "id": str(raw.get("account_id")),
                    "source": SOURCE,
                    "name": clean(raw.get("display_name")) or default_name,
                    "type": account_type,
                    "currency": str(raw.get("currency") or "GBP").upper(),
                    "institution": "Nationwide Building Society",
                    "mask": mask,
                }
            )
        return out

    # ----------------------------------------------------------------- balance

    @_public
    def get_balance(self, account_id: str) -> dict[str, Any]:
        kind = self._kind(account_id)
        results = self._get(f"/data/v1/{kind}/{account_id}/balance")
        if not results:
            raise ProviderUnavailableError("TrueLayer returned no balance for this account.")
        raw = results[0]
        currency = str(raw.get("currency") or "GBP").upper()
        as_of = raw.get("update_timestamp")

        def minor(key: str, negate: bool = False) -> int | None:
            if raw.get(key) is None:
                return None
            value = to_minor(raw[key], currency)
            return -value if negate else value

        balances: list[dict[str, Any]] = []

        def add(kind_name: str, value: int | None) -> None:
            if value is not None:
                balances.append({"type": kind_name, "amount_minor": value, "currency": currency, "as_of": as_of})

        if kind == "cards":
            # TrueLayer: card `current` = amount spent (positive = owed). Ours: negative = owed.
            current = minor("current", negate=True)
            available = minor("available")
            add("current", current)
            add("available_credit", available)
            add("credit_limit", minor("credit_limit"))
            add("last_statement_balance", minor("last_statement_balance", negate=True))
            add("payment_due", minor("payment_due"))
            notes = "credit card: balance_minor is negative when money is owed; available_minor is unused credit"
        else:
            current = minor("current")
            available = minor("available")
            add("current", current)
            add("available", available)
            add("overdraft_limit", minor("overdraft"))
            notes = "balance_minor excludes pending transactions; available_minor includes them and any overdraft"
        return {
            "account_id": account_id,
            "source": SOURCE,
            "currency": currency,
            "current_minor": current,
            "available_minor": available,
            "as_of": as_of,
            "balances": balances,
            "notes": notes,
        }

    # ------------------------------------------------------------ transactions

    @_public
    def fetch_transactions(
        self, account_id: str, since: date, before: date, include_pending: bool = False
    ) -> dict[str, Any]:
        require_path_id(account_id)
        if since >= before:
            raise InvalidRequestError(f"since ({since}) must be earlier than before ({before}).")
        if since > today_london(self._clock):
            return window_result([], since, before, include_pending)  # nothing can exist yet
        # The bank may filter `from`/`to` in UTC while we report London dates, so ask one day early;
        # `to` is inclusive and must not be in the future.
        req_to = min(before - timedelta(days=1), today_utc(self._clock))
        req_from = min(since - timedelta(days=1), req_to)
        windows = _windows(req_from, req_to, self.config.window_days)
        if len(windows) + 5 > MAX_REQUESTS_PER_CALL:
            raise InvalidRequestError(
                f"This range needs {len(windows)} upstream requests, more than the per-call safety limit. "
                "Ask for a shorter range (or raise TRUELAYER_WINDOW_DAYS towards its default of 88)."
            )
        kind = self._kind(account_id)
        collected: list[dict[str, Any]] = []
        kept_unreferenced: Counter[tuple] = Counter()
        for win_from, win_to in windows:
            raw = self._get(
                f"/data/v1/{kind}/{account_id}/transactions",
                {"from": win_from.isoformat(), "to": win_to.isoformat()},
            )
            in_this_window: Counter[tuple] = Counter()
            for item in raw:
                txn = self._normalise(item, account_id, kind, pending=False)
                if txn["_ref"]:
                    collected.append(txn)  # repeats from the overlap day are dropped by id later
                    continue
                # No durable id: an item seen n times in one window and m times in the next
                # (the overlap day) exists max(n, m) times, not n + m.
                key = (txn["date"], txn["amount_minor"], txn["description"])
                in_this_window[key] += 1
                if in_this_window[key] > kept_unreferenced[key]:
                    kept_unreferenced[key] += 1
                    collected.append(txn)
        pending_supported = True
        try:
            pending_raw = self._get(f"/data/v1/{kind}/{account_id}/transactions/pending")
        except NotSupportedError:
            pending_supported, pending_raw = False, []
        collected += [self._normalise(item, account_id, kind, pending=True) for item in pending_raw]
        return window_result(collected, since, before, include_pending, pending_supported=pending_supported)

    @staticmethod
    def _normalise(raw: dict[str, Any], account_id: str, kind: str, pending: bool) -> dict[str, Any]:
        currency = str(raw.get("currency") or "GBP").upper()
        signed = to_minor(raw.get("amount"), currency)
        direction = str(raw.get("transaction_type") or "").upper()
        if direction == "DEBIT":
            amount = -abs(signed)
        elif direction == "CREDIT":
            amount = abs(signed)
        elif kind == "cards":
            amount = -signed  # card rule: positive = money out
        else:
            amount = signed  # account rule: negative = money out
        day = london_date(raw.get("timestamp"))
        if day is None:
            raise ProviderError("TrueLayer returned a transaction without a valid timestamp.")
        meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
        classification = [clean(x) for x in (raw.get("transaction_classification") or []) if x]
        category = " > ".join(c for c in classification if c) or clean(meta.get("provider_transaction_category"))
        notes = []
        tl_category = str(raw.get("transaction_category") or "").upper()
        if tl_category and tl_category not in ("PURCHASE", "UNKNOWN"):
            notes.append(f"type={tl_category}")
        if pending:
            notes.append("pending: amount and id may change when this settles")
        # `transaction_id` is documented as unstable between requests; the normalised provider id is
        # the one TrueLayer says will not change. Pending items rarely have a durable id, so they
        # (and any booked item without one) get a content-derived id instead.
        ref = None if pending else (raw.get("normalised_provider_transaction_id") or raw.get("provider_transaction_id"))
        return make_transaction(
            account_id=account_id,
            day=day,
            amount_minor=amount,
            currency=currency,
            description=raw.get("description"),
            merchant=raw.get("merchant_name"),
            category=category,
            pending=pending,
            notes="; ".join(notes) or None,
            provider_ref=ref,
        )

    # ------------------------------------------------------------------ status

    @_public
    def connection_status(self) -> dict[str, Any]:
        status: dict[str, Any] = {
            "provider": self.name,
            "environment": self.config.environment,
            "connected": False,
            "status": "unknown",
            "consent_expires_at": None,
            "consent_expiry_is_estimate": False,
            "days_remaining": None,
            "needs_reauthorisation": True,
            "accounts": [],
            "message": "",
        }
        try:
            results = self._get("/data/v1/me")
        except ProviderError as exc:
            status["status"] = status["error_code"] = exc.code
            status["needs_reauthorisation"] = isinstance(
                exc, (NotConnectedError, ConsentExpiredError, TokenExpiredError)
            )
            status["message"] = exc.message
            return status
        finally:
            if self.store_error is not None:
                # Served from memory, but the rotated token is not on disk: say so loudly.
                status["token_store_error"] = str(self.store_error)

        me = results[0] if results else {}
        expires = _parse_dt(me.get("consent_expires_at"))
        estimated = False
        if expires is None:
            connected = _parse_dt((self._memory or {}).get("connected_at"))
            if connected:
                expires, estimated = connected + timedelta(days=CONSENT_DAYS), True
        status.update(
            {
                "connected": True,
                "status": str(me.get("consent_status") or "authorised").lower(),
                "needs_reauthorisation": False,
                "consent_created_at": me.get("consent_created_at"),
                "consent_expiry_is_estimate": estimated,
                "aggregator_provider_id": (me.get("provider") or {}).get("provider_id"),
                "scopes": me.get("scopes"),
            }
        )
        if expires is None:
            status["message"] = (
                "Connected, but TrueLayer did not report a consent expiry. UK consents must be reconfirmed "
                "at least every 90 days."
            )
            return status
        status["consent_expires_at"] = expires.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        remaining = expires - datetime.fromtimestamp(self._clock(), UTC)
        status["days_remaining"] = remaining.days
        day = expires.date().isoformat()
        if remaining.total_seconds() <= 0:
            status["connected"] = False
            status["needs_reauthorisation"] = True
            status["error_code"] = "consent_expired"
            status["message"] = f"Consent expired on {day}. " + RELINK_HINT
        elif remaining.days < REAUTH_WARNING_DAYS:
            status["message"] = f"Consent expires in {remaining.days} day(s) on {day}. " + RELINK_HINT
        else:
            status["message"] = f"Connected. Consent is valid until {day} ({remaining.days} days)."
        return status


def _windows(start: date, end: date, days: int, overlap: int = WINDOW_OVERLAP_DAYS) -> list[tuple[date, date]]:
    """Inclusive (from, to) windows of at most ``days`` days covering start..end. Consecutive
    windows share ``overlap`` day(s) so that nothing can fall between two requests."""
    step = days - overlap if days > overlap else days
    out, cur = [], start
    while cur <= end:
        win_end = min(cur + timedelta(days=days - 1), end)
        out.append((cur, win_end))
        if win_end >= end:
            break
        cur = cur + timedelta(days=step)
    return out


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_code(resp: httpx.Response) -> str:
    return str(_json(resp).get("error") or "")[:80]


def _retry_after(resp: httpx.Response) -> int | None:
    value = (resp.headers.get("Retry-After") or "").strip()
    return int(value) if value.isascii() and value.isdigit() else None
