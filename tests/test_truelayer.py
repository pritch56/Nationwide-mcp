"""TrueLayer provider against mocked HTTP: tokens, accounts + cards, windows, signs, errors."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import httpx
import pytest

from conftest import Recorder, reply
from nationwide_mcp.errors import (
    AuthError,
    ConfigError,
    ConsentExpiredError,
    InvalidRequestError,
    NotConnectedError,
    NotFoundError,
    ProviderUnavailableError,
    RateLimitError,
    ScaRequiredError,
)
from nationwide_mcp.link import build_auth_url, check_public_provider_list, extract_code
from nationwide_mcp.providers import build_provider
from nationwide_mcp.providers.truelayer import STORE_SECTION, TrueLayerConfig, TrueLayerProvider, _windows
from nationwide_mcp.token_store import FileTokenStore, MemoryTokenStore

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC).timestamp()
AUTH = "https://auth.truelayer.com"
API = "https://api.truelayer.com"

ACCOUNTS = [
    {
        "account_id": "acc-current",
        "account_type": "TRANSACTION",
        "display_name": "FlexDirect",
        "currency": "GBP",
        "account_number": {"number": "12345678", "sort_code": "00-00-00"},
        "provider": {"provider_id": "ob-nationwide"},
    },
    {
        "account_id": "acc-savings",
        "account_type": "SAVINGS",
        "display_name": "Loyalty Saver",
        "currency": "GBP",
        "account_number": {"number": "87654321", "sort_code": "00-00-00"},
        "provider": {"provider_id": "ob-nationwide"},
    },
]
CARDS = [
    {
        "account_id": "card-1",
        "card_network": "VISA",
        "card_type": "CREDIT",
        "currency": "GBP",
        "display_name": "Member Credit Card",
        "partial_card_number": "4321",
        "provider": {"provider_id": "ob-nationwide"},
    },
]
ACCOUNT_TXNS = [
    # accounts family: NEGATIVE = money out
    {
        "transaction_id": "volatile-a",
        "normalised_provider_transaction_id": "np-1",
        "timestamp": "2026-10-05T00:00:00Z",
        "description": "TESCO STORES 1234",
        "amount": -12.34,
        "currency": "GBP",
        "transaction_type": "DEBIT",
        "transaction_category": "PURCHASE",
        "transaction_classification": ["Shopping", "Groceries"],
        "merchant_name": "Tesco",
    },
    {
        "transaction_id": "volatile-b",
        "provider_transaction_id": "p-2",
        "timestamp": "2026-10-06T09:30:00+01:00",
        "description": "SALARY ACME LTD",
        "amount": 1500.0,
        "currency": "GBP",
        "transaction_type": "CREDIT",
        "transaction_category": "CREDIT",
        "transaction_classification": [],
    },
    {
        "transaction_id": "volatile-c",
        "timestamp": "2026-10-07T00:00:00Z",
        "description": "CASH",
        "amount": -20,
        "currency": "GBP",
        "transaction_category": "ATM",
    },  # no type, no durable id: sign comes from the amount
]
CARD_TXNS = [
    # cards family: POSITIVE = money out
    {
        "transaction_id": "volatile-d",
        "normalised_provider_transaction_id": "nc-1",
        "timestamp": "2026-10-04T00:00:00Z",
        "description": "AMAZON UK",
        "amount": 20.0,
        "currency": "GBP",
        "transaction_type": "DEBIT",
        "transaction_category": "PURCHASE",
        "merchant_name": "Amazon",
    },
    {
        "transaction_id": "volatile-e",
        "normalised_provider_transaction_id": "nc-2",
        "timestamp": "2026-10-06T00:00:00Z",
        "description": "PAYMENT RECEIVED",
        "amount": -100.0,
        "currency": "GBP",
        "transaction_type": "CREDIT",
        "transaction_category": "CREDIT",
    },
    {
        "transaction_id": "volatile-f",
        "timestamp": "2026-10-07T00:00:00Z",
        "description": "REFUND SHOP",
        "amount": -5.5,
        "currency": "GBP",
    },  # no type: card rule applies, negative = money in
]
PENDING = [
    {
        "transaction_id": "pend-1",
        "timestamp": "2026-10-09T00:00:00Z",
        "description": "PRET A MANGER",
        "amount": -4.5,
        "currency": "GBP",
        "transaction_type": "DEBIT",
        "merchant_name": "Pret",
    },
]
ME = [
    {
        "client_id": "cid",
        "consent_status": "AUTHORISED",
        "consent_created_at": "2026-10-01T10:00:00Z",
        "consent_expires_at": "2026-12-30T10:00:00Z",
        "provider": {"provider_id": "ob-nationwide"},
        "scopes": ["info", "accounts", "balance", "cards", "transactions", "offline_access"],
    }
]


class FakeTrueLayer:
    """Scriptable stand-in for auth.truelayer.com + api.truelayer.com."""

    def __init__(self):
        self.refresh_tokens = ["rt-1"]  # currently valid refresh token(s)
        self.rotate = True
        self.issued = 0
        self.valid_access: set[str] = set()
        self.overrides: dict[str, httpx.Response | list[httpx.Response]] = {}
        self.token_bodies: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/connect/token":
            ctype = request.headers.get("content-type", "")
            body = json.loads(request.content) if "json" in ctype else dict(httpx.QueryParams(request.content.decode()))
            self.token_bodies.append({"content_type": ctype, **body})
            if "/connect/token" in self.overrides:
                return self._override("/connect/token")
            if body.get("client_secret") != "secret":
                return reply(401, {"error": "invalid_client"})
            if body["grant_type"] == "refresh_token" and body.get("refresh_token") not in self.refresh_tokens:
                return reply(400, {"error": "invalid_grant"})
            if body["grant_type"] == "authorization_code" and body.get("code") != "good-code":
                return reply(400, {"error": "invalid_authorization_code"})
            self.issued += 1
            access = f"at-{self.issued}"
            self.valid_access = {access}
            out = {"access_token": access, "expires_in": 3600, "token_type": "Bearer"}
            if self.rotate or body["grant_type"] == "authorization_code":
                new = f"rt-{self.issued + 1}"
                self.refresh_tokens = [new]
                out["refresh_token"] = new
            return reply(200, out)

        if request.headers.get("authorization", "").removeprefix("Bearer ") not in self.valid_access:
            return reply(401, {"error": "invalid_token"})
        if path in self.overrides:
            return self._override(path)
        if path == "/data/v1/accounts":
            return reply(200, {"results": ACCOUNTS})
        if path == "/data/v1/cards":
            return reply(200, {"results": CARDS})
        if path == "/data/v1/me":
            return reply(200, {"results": ME})
        if path == "/data/v1/accounts/acc-current/balance":
            return reply(
                200,
                {
                    "results": [
                        {
                            "currency": "GBP",
                            "available": 1722.22,
                            "current": 1222.22,
                            "overdraft": 500,
                            "update_timestamp": "2026-10-10T11:00:00Z",
                        }
                    ]
                },
            )
        if path == "/data/v1/cards/card-1/balance":
            return reply(
                200,
                {
                    "results": [
                        {
                            "currency": "GBP",
                            "available": 3880.0,
                            "current": 120.0,
                            "credit_limit": 4000,
                            "last_statement_balance": 80.5,
                            "payment_due": 25,
                            "update_timestamp": "2026-10-10T11:00:00Z",
                        }
                    ]
                },
            )
        if path == "/data/v1/accounts/acc-current/transactions":
            return reply(200, {"results": ACCOUNT_TXNS})
        if path == "/data/v1/cards/card-1/transactions":
            return reply(200, {"results": CARD_TXNS})
        if path.endswith("/transactions/pending"):
            return reply(200, {"results": PENDING if "acc-current" in path else []})
        if path.endswith("/transactions"):
            return reply(200, {"results": []})
        return reply(404, {"error": "account_not_found"})

    def _override(self, path: str) -> httpx.Response:
        value = self.overrides[path]
        if isinstance(value, list):
            return value.pop(0) if len(value) > 1 else value[0]
        return value


def make(store=None, fake=None, clock=lambda: NOW, **config):
    fake = fake or FakeTrueLayer()
    rec = Recorder(fake.handler)
    store = store if store is not None else MemoryTokenStore({STORE_SECTION: {"refresh_token": "rt-1"}})
    cfg = TrueLayerConfig("cid", "secret", **config)
    provider = TrueLayerProvider(cfg, store, transport=rec.transport, backoff_seconds=0, clock=clock)
    return provider, rec, store, fake


def data_calls(rec: Recorder, suffix: str = "") -> list[httpx.Request]:
    return [r for r in rec.requests if r.url.path.startswith("/data/") and r.url.path.endswith(suffix)]


# --------------------------------------------------------------- configuration


def test_config_validation_and_env_names():
    with pytest.raises(ConfigError):
        TrueLayerConfig("", "")
    with pytest.raises(ConfigError):
        TrueLayerConfig("cid", "secret", environment="prod")
    cfg = TrueLayerConfig.from_env(
        {
            "TRUELAYER_CLIENT_ID": "cid",
            "TRUELAYER_CLIENT_SECRET": "super-secret",
            "NATIONWIDE_TRUELAYER_REFRESH_TOKEN": "seed",
            "TRUELAYER_TOKEN_ENCODING": "form",
            "TRUELAYER_ENV": "sandbox",
        }
    )
    assert (cfg.seed_refresh_token, cfg.token_encoding) == ("seed", "form")
    assert cfg.api_base == "https://api.truelayer-sandbox.com"
    assert "super-secret" not in repr(cfg)


def test_truelayer_is_the_default_provider():
    env = {"TRUELAYER_CLIENT_ID": "cid", "TRUELAYER_CLIENT_SECRET": "s", "NATIONWIDE_TOKEN_STORE": "memory"}
    assert build_provider(env).name == "truelayer"
    with pytest.raises(ConfigError) as info:
        build_provider({"NATIONWIDE_TOKEN_STORE": "memory"})
    assert "TRUELAYER_CLIENT_ID" in info.value.message


# ---------------------------------------------------------------------- tokens


def test_refresh_then_rotated_token_is_persisted_before_use(tmp_path):
    store = FileTokenStore(tmp_path / "nationwide-tokens.json")
    store.put(STORE_SECTION, {"refresh_token": "rt-1"})
    provider, rec, _, fake = make(store=store)
    provider.list_accounts()

    sent = fake.token_bodies[0]
    assert sent["grant_type"] == "refresh_token" and sent["refresh_token"] == "rt-1"
    assert sent["client_id"] == "cid" and "json" in sent["content_type"]
    assert rec.requests[0].url == f"{AUTH}/connect/token"
    saved = FileTokenStore(tmp_path / "nationwide-tokens.json").get(STORE_SECTION)
    assert saved["refresh_token"] == "rt-2" and saved["access_token"] == "at-1"  # rotation survived a restart
    assert saved["access_token_expires_at"] == NOW + 3600
    assert data_calls(rec)[0].headers["Authorization"] == "Bearer at-1"

    # A new process picks up the rotated token; the superseded rt-1 would be refused.
    clock = {"now": NOW + 7200}
    provider2, _, _, _ = make(
        store=FileTokenStore(tmp_path / "nationwide-tokens.json"), fake=fake, clock=lambda: clock["now"]
    )
    provider2.list_accounts()
    assert fake.token_bodies[-1]["refresh_token"] == "rt-2"
    assert FileTokenStore(tmp_path / "nationwide-tokens.json").get(STORE_SECTION)["refresh_token"] == "rt-3"


def test_access_token_reused_until_near_expiry_and_refresh_token_kept_when_not_rotated():
    clock = {"now": NOW}
    fake = FakeTrueLayer()
    fake.rotate = False
    provider, _, store, _ = make(fake=fake, clock=lambda: clock["now"])
    provider.list_accounts()
    provider.get_balance("acc-current")
    assert len(fake.token_bodies) == 1  # one refresh served every call
    assert store.get(STORE_SECTION)["refresh_token"] == "rt-1"  # nothing returned -> old one kept
    clock["now"] = NOW + 3550  # inside the 60 s safety margin
    provider.get_balance("acc-current")
    assert len(fake.token_bodies) == 2


def test_form_encoding_option():
    provider, _, _, fake = make(token_encoding="form")
    provider.list_accounts()
    assert "x-www-form-urlencoded" in fake.token_bodies[0]["content_type"]
    assert fake.token_bodies[0]["refresh_token"] == "rt-1"


def test_seed_token_only_bootstraps_an_empty_store_and_the_store_always_wins():
    provider, _, store, fake = make(store=MemoryTokenStore(), seed_refresh_token="rt-1")
    provider.list_accounts()
    assert store.get(STORE_SECTION)["refresh_token"] == "rt-2"  # rotated value stored, seed consumed

    # Same seed again (process restart, env unchanged): the stored, newer token wins.
    provider, _, _, _ = make(store=store, fake=fake, seed_refresh_token="rt-1", clock=lambda: NOW + 7200)
    provider.list_accounts()
    assert fake.token_bodies[-1]["refresh_token"] == "rt-2"

    # A DIFFERENT seed in the environment does not override the store either (it may be stale).
    provider, _, _, _ = make(store=store, fake=fake, seed_refresh_token="some-other-seed", clock=lambda: NOW + 14400)
    provider.list_accounts()
    assert fake.token_bodies[-1]["refresh_token"] == "rt-3"
    assert "some-other-seed" not in [b.get("refresh_token") for b in fake.token_bodies]


def test_stale_seed_in_the_environment_does_not_lock_out_a_fresh_link():
    """Reproduced lockout: `link` stored a fresh token, but an old seed was still exported."""
    fake = FakeTrueLayer()
    fake.refresh_tokens = ["fresh-from-link"]
    store = MemoryTokenStore({STORE_SECTION: {"refresh_token": "fresh-from-link", "seed_fingerprint": "whatever"}})
    provider, _, _, _ = make(store=store, fake=fake, seed_refresh_token="stale-old-seed")
    assert len(provider.list_accounts()) == 3
    assert [b["refresh_token"] for b in fake.token_bodies] == ["fresh-from-link"]


def test_rejected_access_token_triggers_one_forced_refresh():
    fake = FakeTrueLayer()
    store = MemoryTokenStore(
        {STORE_SECTION: {"refresh_token": "rt-1", "access_token": "stale", "access_token_expires_at": NOW + 3000}}
    )
    provider, rec, _, _ = make(store=store, fake=fake)
    assert len(provider.list_accounts()) == 3
    assert [r.headers["Authorization"] for r in data_calls(rec, "/accounts")] == ["Bearer stale", "Bearer at-1"]

    fake = FakeTrueLayer()
    fake.overrides["/data/v1/accounts"] = reply(401, {"error": "invalid_token"})
    provider, _, _, _ = make(fake=fake)
    with pytest.raises(AuthError) as info:  # code token_expired
        provider.list_accounts()
    assert info.value.code == "token_expired" and len(fake.token_bodies) == 2


def test_not_connected_and_token_endpoint_errors():
    provider, rec, _, _ = make(store=MemoryTokenStore())
    with pytest.raises(NotConnectedError) as info:
        provider.list_accounts()
    assert info.value.code == "not_connected" and rec.requests == []

    provider, _, _, _ = make(store=MemoryTokenStore({STORE_SECTION: {"refresh_token": "revoked"}}))
    with pytest.raises(ConsentExpiredError) as info:
        provider.list_accounts()
    assert "invalid_grant" in info.value.message and "nationwide-mcp-link" in info.value.message

    fake = FakeTrueLayer()
    provider, _, _, _ = make(fake=fake)
    provider.config.client_secret = "wrong-secret"
    with pytest.raises(ConfigError) as info:
        provider.list_accounts()
    assert "wrong-secret" not in str(info.value)

    fake = FakeTrueLayer()
    fake.overrides["/connect/token"] = reply(429, {"error": "rate_limit"}, {"Retry-After": "30"})
    provider, _, _, _ = make(fake=fake)
    with pytest.raises(RateLimitError) as info:
        provider.list_accounts()
    assert info.value.retry_after_seconds == 30


def test_exchange_code_stores_tokens_and_marks_them_as_the_seed():
    provider, _, store, fake = make(store=MemoryTokenStore())
    provider.exchange_code("good-code", "http://localhost:3000/callback")
    sent = fake.token_bodies[0]
    assert sent["grant_type"] == "authorization_code" and sent["redirect_uri"] == "http://localhost:3000/callback"
    saved = store.get(STORE_SECTION)
    first_refresh = saved["refresh_token"]
    assert first_refresh == "rt-2" and saved["connected_at"].endswith("Z")

    # The user copies that token into the seed variable; after rotation the store must still win.
    provider, _, _, _ = make(store=store, fake=fake, seed_refresh_token=first_refresh, clock=lambda: NOW + 7200)
    provider.list_accounts()
    provider, _, _, _ = make(store=store, fake=fake, seed_refresh_token=first_refresh, clock=lambda: NOW + 14400)
    provider.list_accounts()
    assert fake.token_bodies[-1]["refresh_token"] == "rt-3"

    provider, _, _, _ = make(store=MemoryTokenStore())
    with pytest.raises(InvalidRequestError):
        provider.exchange_code("stale-code", "http://localhost:3000/callback")


# ------------------------------------------------------------ accounts + cards


def test_accounts_and_cards_are_merged():
    provider, rec, _, _ = make()
    accounts = provider.list_accounts()
    assert [(a["id"], a["type"], a["mask"], a["name"]) for a in accounts] == [
        ("acc-current", "current", "5678", "FlexDirect"),
        ("acc-savings", "savings", "4321", "Loyalty Saver"),
        ("card-1", "credit_card", "4321", "Member Credit Card"),
    ]
    assert all(a["source"] == "nationwide" and a["currency"] == "GBP" for a in accounts)
    assert [r.url.path for r in data_calls(rec)] == ["/data/v1/accounts", "/data/v1/cards"]
    assert "12345678" not in json.dumps(accounts)  # full account number never leaves the provider


def test_missing_cards_scope_or_endpoint_does_not_break_accounts():
    for response in (reply(501, {"error": "endpoint_not_supported"}), reply(403, {"error": "access_denied"})):
        fake = FakeTrueLayer()
        fake.overrides["/data/v1/cards"] = response
        provider, _, _, _ = make(fake=fake)
        assert [a["id"] for a in provider.list_accounts()] == ["acc-current", "acc-savings"]

    fake = FakeTrueLayer()  # but a 403 on everything is a dead consent
    fake.overrides["/data/v1/accounts"] = reply(403, {"error": "access_denied"})
    fake.overrides["/data/v1/cards"] = reply(403, {"error": "access_denied"})
    provider, _, _, _ = make(fake=fake)
    with pytest.raises(ConsentExpiredError):
        provider.list_accounts()


def test_balances_use_the_right_endpoint_and_sign():
    provider, rec, _, _ = make()
    current = provider.get_balance("acc-current")
    assert rec.requests[-1].url.path == "/data/v1/accounts/acc-current/balance"
    assert current["current_minor"] == 122222 and current["available_minor"] == 172222  # positive = money held
    assert {b["type"]: b["amount_minor"] for b in current["balances"]}["overdraft_limit"] == 50000

    card = provider.get_balance("card-1")
    assert rec.requests[-1].url.path == "/data/v1/cards/card-1/balance"
    assert card["current_minor"] == -12000  # TrueLayer reports 120.00 spent; ours is negative = owed
    assert card["available_minor"] == 388000
    kinds = {b["type"]: b["amount_minor"] for b in card["balances"]}
    assert kinds["credit_limit"] == 400000 and kinds["last_statement_balance"] == -8050 and kinds["payment_due"] == 2500


# ---------------------------------------------------------------- transactions


def test_account_transactions_sign_dates_ids_and_default_excludes_pending():
    provider, rec, _, _ = make()
    txns = provider.list_transactions("acc-current", date(2026, 10, 1), date(2026, 10, 8))
    call = data_calls(rec, "/transactions")[-1]
    assert call.url.path == "/data/v1/accounts/acc-current/transactions"
    # exclusive `before` -> inclusive `to`; `from` is one day early because the bank may filter in UTC
    assert dict(call.url.params) == {"from": "2026-09-30", "to": "2026-10-07"}
    assert len(data_calls(rec, "/pending")) == 1  # asked only so that excluded pending items can be counted
    assert [(t["date"], t["amount_minor"], t["description"]) for t in txns] == [
        ("2026-10-05", -1234, "TESCO STORES 1234"),
        ("2026-10-06", 150000, "SALARY ACME LTD"),
        ("2026-10-07", -2000, "CASH"),
    ]
    tesco, salary, cash = txns
    assert tesco["merchant"] == "Tesco" and tesco["category"] == "Shopping > Groceries" and tesco["notes"] is None
    assert salary["merchant"] is None and salary["notes"] == "type=CREDIT"
    assert cash["notes"] == "type=ATM"
    assert all(t["pending"] is False and t["source"] == "nationwide" and t["account_id"] == "acc-current" for t in txns)
    assert all(isinstance(t["amount_minor"], int) and len(t["date"]) == 10 for t in txns)
    assert set(tesco) == {
        "id",
        "source",
        "account_id",
        "date",
        "amount_minor",
        "currency",
        "description",
        "merchant",
        "category",
        "pending",
        "notes",
    }


def test_card_transactions_invert_truelayers_sign():
    provider, rec, _, _ = make()
    txns = provider.list_transactions("card-1", date(2026, 10, 1), date(2026, 10, 8))
    assert data_calls(rec, "/transactions")[-1].url.path == "/data/v1/cards/card-1/transactions"
    assert [(t["description"], t["amount_minor"]) for t in txns] == [
        ("AMAZON UK", -2000),  # +20.00 at TrueLayer = purchase = money out
        ("PAYMENT RECEIVED", 10000),  # -100.00 at TrueLayer = money in
        ("REFUND SHOP", 550),  # no transaction_type: card rule, negative = money in
    ]


def test_pending_only_when_asked_and_flagged():
    provider, rec, _, _ = make()
    txns = provider.list_transactions("acc-current", date(2026, 10, 1), date(2026, 10, 11), include_pending=True)
    assert data_calls(rec, "/pending")[0].url.path == "/data/v1/accounts/acc-current/transactions/pending"
    pending = [t for t in txns if t["pending"]]
    assert len(pending) == 1 and pending[0]["amount_minor"] == -450 and pending[0]["date"] == "2026-10-09"
    assert "pending" in pending[0]["notes"] and len(txns) == 4

    fake = FakeTrueLayer()  # banks without a pending endpoint are tolerated
    fake.overrides["/data/v1/accounts/acc-current/transactions/pending"] = reply(
        501, {"error": "endpoint_not_supported"}
    )
    provider, _, _, _ = make(fake=fake)
    result = provider.fetch_transactions("acc-current", date(2026, 10, 1), date(2026, 10, 11), include_pending=True)
    assert len(result["transactions"]) == 3
    assert result["pending_supported"] is False  # said out loud, not silently booked-only


def test_ids_are_stable_despite_volatile_transaction_id():
    provider, _, _, _ = make()
    first = provider.list_transactions("acc-current", date(2026, 10, 1), date(2026, 10, 8))

    fake = FakeTrueLayer()
    renamed = [dict(t, transaction_id=f"changed-{i}") for i, t in enumerate(reversed(ACCOUNT_TXNS))]
    fake.overrides["/data/v1/accounts/acc-current/transactions"] = reply(200, {"results": renamed})
    provider, _, _, _ = make(fake=fake)
    second = provider.list_transactions("acc-current", date(2026, 10, 3), date(2026, 10, 9))
    assert [t["id"] for t in first] == [t["id"] for t in second]
    assert len({t["id"] for t in first}) == 3 and all(t["id"].startswith("nationwide_") for t in first)


def test_long_ranges_are_walked_in_windows_and_never_ask_for_the_future():
    assert _windows(date(2026, 1, 1), date(2026, 1, 10), 4) == [
        (date(2026, 1, 1), date(2026, 1, 4)),
        (date(2026, 1, 4), date(2026, 1, 7)),
        (date(2026, 1, 7), date(2026, 1, 10)),
    ]
    assert _windows(date(2026, 1, 1), date(2026, 1, 3), 1) == [(date(2026, 1, d), date(2026, 1, d)) for d in (1, 2, 3)]
    assert _windows(date(2026, 1, 1), date(2026, 1, 1), 88) == [(date(2026, 1, 1), date(2026, 1, 1))]
    provider, rec, _, _ = make()
    provider.list_transactions("acc-current", date(2026, 4, 2), date(2026, 12, 1))  # `before` is in the future
    spans = [(r.url.params["from"], r.url.params["to"]) for r in data_calls(rec, "/transactions")]
    assert spans == [("2026-04-01", "2026-06-27"), ("2026-06-27", "2026-09-22"), ("2026-09-22", "2026-10-10")]
    # each window shares one day with the next; capped at today (2026-10-10)

    provider, rec, _, _ = make()
    assert provider.list_transactions("acc-current", date(2026, 10, 11), date(2026, 10, 20)) == []
    assert rec.requests == []  # a window that starts after today cannot contain anything


def test_window_is_half_open():
    provider, _, _, _ = make()
    txns = provider.list_transactions("acc-current", date(2026, 10, 5), date(2026, 10, 7))
    assert [t["date"] for t in txns] == ["2026-10-05", "2026-10-06"]  # 7 Oct excluded even though the bank sent it


# ---------------------------------------------------------------------- errors


@pytest.mark.parametrize(
    "response,error,code,needle",
    [
        (reply(403, {"error": "access_denied"}), ConsentExpiredError, "consent_expired", "nationwide-mcp-link"),
        (reply(403, {"error": "sca_exceeded"}), ScaRequiredError, "sca_required", "90 days"),
        (
            reply(429, {"error": "provider_too_many_requests"}, {"Retry-After": "3600"}),
            RateLimitError,
            "rate_limited",
            "do not re-authorise",
        ),
        (reply(404, {"error": "account_not_found"}), NotFoundError, "account_not_found", "list_accounts"),
        (
            reply(400, {"error": "invalid_date_range", "error_description": "from must be before to"}),
            InvalidRequestError,
            "invalid_request",
            "invalid_date_range",
        ),
        (reply(503, {"error": "provider_error"}), ProviderUnavailableError, "provider_error", "retry later"),
    ],
)
def test_data_error_mapping(response, error, code, needle):
    fake = FakeTrueLayer()
    fake.overrides["/data/v1/accounts/acc-current/transactions"] = response
    provider, rec, _, _ = make(fake=fake)
    with pytest.raises(error) as info:
        provider.list_transactions("acc-current", date(2026, 10, 1), date(2026, 10, 8))
    assert info.value.code == code and needle in info.value.message
    assert str(info.value).startswith(f"[{code}] ")
    assert "secret" not in str(info.value) and "rt-1" not in str(info.value)
    if error is RateLimitError:
        assert info.value.retry_after_seconds == 3600
    if error is ProviderUnavailableError:
        assert len(data_calls(rec, "/transactions")) == 3  # retried twice, then reported


def test_transient_5xx_and_network_errors_are_retried():
    fake = FakeTrueLayer()
    fake.overrides["/data/v1/accounts/acc-current/balance"] = [
        reply(502, {"error": "provider_error"}),
        reply(200, {"results": [{"currency": "GBP", "current": 1.0, "available": 1.0}]}),
    ]
    provider, _, _, _ = make(fake=fake)
    assert provider.get_balance("acc-current")["current_minor"] == 100

    def down(request):
        raise httpx.ConnectError("no route")

    provider = TrueLayerProvider(
        TrueLayerConfig("cid", "secret"),
        MemoryTokenStore({STORE_SECTION: {"refresh_token": "x"}}),
        transport=httpx.MockTransport(down),
        backoff_seconds=0,
    )
    with pytest.raises(ProviderUnavailableError):
        provider.list_accounts()


def test_unknown_and_malformed_account_ids():
    provider, _, _, _ = make()
    with pytest.raises(NotFoundError):
        provider.get_balance("someone-elses-account")
    with pytest.raises(InvalidRequestError):
        provider.get_balance("../me")


# ---------------------------------------------------------------------- status


def test_connection_status_from_me_endpoint():
    provider, _, _, _ = make()
    status = provider.connection_status()
    assert status["connected"] is True and status["needs_reauthorisation"] is False
    assert status["consent_expires_at"] == "2026-12-30T10:00:00Z" and status["days_remaining"] == 80
    assert status["aggregator_provider_id"] == "ob-nationwide" and status["consent_expiry_is_estimate"] is False


def test_connection_status_estimates_expiry_and_reports_problems_without_raising():
    fake = FakeTrueLayer()
    fake.overrides["/data/v1/me"] = reply(200, {"results": [{"provider": {"provider_id": "ob-nationwide"}}]})
    store = MemoryTokenStore({STORE_SECTION: {"refresh_token": "rt-1", "connected_at": "2026-07-20T12:00:00Z"}})
    provider, _, _, _ = make(store=store, fake=fake)
    status = provider.connection_status()
    assert status["consent_expires_at"] == "2026-10-18T12:00:00Z" and status["consent_expiry_is_estimate"] is True
    assert status["days_remaining"] == 8 and "8 day(s)" in status["message"]

    provider, _, _, _ = make(store=MemoryTokenStore())
    status = provider.connection_status()
    assert status["connected"] is False and status["status"] == "not_connected" and status["needs_reauthorisation"]

    provider, _, _, _ = make(store=MemoryTokenStore({STORE_SECTION: {"refresh_token": "revoked"}}))
    status = provider.connection_status()
    assert status["connected"] is False and status["status"] == "consent_expired"

    fake = FakeTrueLayer()
    fake.overrides["/data/v1/me"] = reply(429, {"error": "rate_limit"})
    provider, _, _, _ = make(fake=fake)
    status = provider.connection_status()
    assert status["status"] == "rate_limited" and status["needs_reauthorisation"] is False


# ----------------------------------------------------------------- link helper


def test_auth_url_requests_read_only_scopes_for_accounts_and_cards():
    url = build_auth_url(TrueLayerConfig("cid", "secret"), "http://localhost:3000/callback", "state-1")
    query = dict(httpx.URL(url).params)
    assert url.startswith("https://auth.truelayer.com/?")
    assert query["scope"].split() == ["info", "accounts", "balance", "cards", "transactions", "offline_access"]
    assert query["response_type"] == "code" and query["state"] == "state-1" and query["client_id"] == "cid"
    assert "secret" not in url and "payments" not in url


def test_extract_code_checks_state():
    assert extract_code("abc", "s") == "abc"
    assert extract_code("http://localhost:3000/callback?code=abc&state=s", "s") == "abc"
    with pytest.raises(ConfigError):
        extract_code("http://localhost:3000/callback?code=abc&state=other", "s")
    with pytest.raises(ConfigError):
        extract_code("http://localhost:3000/callback?error=access_denied&state=s", "s")


def test_public_provider_list_check():
    listing = [
        {"provider_id": "ob-amex", "scopes": ["cards"]},
        {
            "provider_id": "ob-nationwide",
            "display_name": "Nationwide",
            "scopes": ["info", "accounts", "balance", "transactions", "cards", "offline_access"],
            "availability": {"recommended_status": "healthy"},
        },
    ]
    client = httpx.Client(transport=httpx.MockTransport(lambda r: reply(200, listing)))
    result = check_public_provider_list(client=client)
    assert result["found"] and result["supports_accounts"] and result["supports_cards"]
    client = httpx.Client(transport=httpx.MockTransport(lambda r: reply(200, listing[:1])))
    assert check_public_provider_list(client=client)["found"] is False


def test_refresh_token_env_names_and_wrong_bank_guard():
    base = {"TRUELAYER_CLIENT_ID": "cid", "TRUELAYER_CLIENT_SECRET": "s"}
    # The generic name is NOT read: a token exported for another bank's connection must not be picked up.
    assert TrueLayerConfig.from_env({**base, "TRUELAYER_REFRESH_TOKEN": "generic"}).seed_refresh_token is None
    both = {**base, "TRUELAYER_REFRESH_TOKEN": "generic", "NATIONWIDE_TRUELAYER_REFRESH_TOKEN": "mine"}
    assert TrueLayerConfig.from_env(both).seed_refresh_token == "mine"

    # A token for another bank's connection must never be served as Nationwide data.
    fake = FakeTrueLayer()
    fake.overrides["/data/v1/accounts"] = reply(501, {"error": "endpoint_not_supported"})
    fake.overrides["/data/v1/cards"] = reply(
        200,
        {"results": [{"account_id": "amex-card", "display_name": "Amex Gold", "provider": {"provider_id": "ob-amex"}}]},
    )
    provider, _, _, _ = make(fake=fake)
    with pytest.raises(ConfigError) as info:
        provider.list_accounts()
    assert "ob-amex" in info.value.message and "NATIONWIDE_TRUELAYER_REFRESH_TOKEN" in info.value.message
    with pytest.raises(ConfigError):
        provider.get_balance("amex-card")
