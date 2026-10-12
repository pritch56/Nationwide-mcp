"""TrueLayer provider: token lifecycle, request limits, dates, de-duplication, malformed data."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, date, datetime

import httpx
import pytest

from conftest import Recorder, reply
from nationwide_mcp.errors import (
    ConfigError,
    ConsentExpiredError,
    InvalidRequestError,
    ProviderError,
    RateLimitError,
    TokenStoreError,
)
from nationwide_mcp.providers import build_provider
from nationwide_mcp.providers._http import USER_AGENT
from nationwide_mcp.providers.truelayer import (
    MAX_REQUESTS_PER_CALL,
    STORE_SECTION,
    TrueLayerConfig,
    TrueLayerProvider,
)
from nationwide_mcp.token_store import MemoryTokenStore
from test_truelayer import ACCOUNT_TXNS, NOW, FakeTrueLayer, data_calls, make

TXN_PATH = "/data/v1/accounts/acc-current/transactions"


class FlakyStore(MemoryTokenStore):
    """A store whose writes can be switched off (disk full, read-only volume, ...)."""

    def __init__(self, data=None):
        super().__init__(data)
        self.broken = False
        self.saves = 0

    def save(self, data):
        if self.broken:
            raise OSError("disk full")
        self.saves += 1
        super().save(data)


# ---------------------------------------------------------------------- tokens


def test_rotated_token_survives_a_failed_save_and_the_old_one_is_never_sent_again():
    clock = {"now": NOW}
    store = FlakyStore({STORE_SECTION: {"refresh_token": "rt-1"}})
    store.broken = True
    provider, _, _, fake = make(store=store, clock=lambda: clock["now"])

    # The refresh succeeded upstream (rt-1 is now dead); the save failed; the data call is still served.
    assert len(provider.list_accounts()) == 3
    assert store.get(STORE_SECTION) == {"refresh_token": "rt-1"}  # nothing reached the store
    assert isinstance(provider.store_error, TokenStoreError)
    status = provider.connection_status()
    assert status["connected"] is True and status["token_store_error"].startswith("[token_store_error] ")

    # Next refresh: the in-memory rt-2 is used. Re-reading rt-1 from the store would be a lockout.
    clock["now"] = NOW + 7200
    provider.list_accounts()
    assert [b["refresh_token"] for b in fake.token_bodies] == ["rt-1", "rt-2"]

    # The store comes back: the save is retried on the next call without another refresh.
    store.broken = False
    provider.get_balance("acc-current")
    assert len(fake.token_bodies) == 2
    assert store.get(STORE_SECTION)["refresh_token"] == "rt-3" and provider.store_error is None
    assert "token_store_error" not in provider.connection_status()


def test_unreadable_store_without_tokens_in_memory_is_a_token_store_error():
    class Unreadable(MemoryTokenStore):
        def load(self):
            raise OSError("permission denied")

    provider, rec, _, _ = make(store=Unreadable())
    with pytest.raises(TokenStoreError) as info:
        provider.list_accounts()
    assert str(info.value).startswith("[token_store_error] ") and rec.requests == []
    status = provider.connection_status()  # reported, not raised
    assert status["connected"] is False and status["error_code"] == "token_store_error"


def test_concurrent_401s_cause_a_single_forced_refresh():
    fake = FakeTrueLayer()
    store = MemoryTokenStore(
        {STORE_SECTION: {"refresh_token": "rt-1", "access_token": "stale", "access_token_expires_at": NOW + 3000}}
    )
    provider, _, _, _ = make(store=store, fake=fake)
    # Two callers were both answered 401 for "stale". The first refreshes ...
    assert provider._access_token(rejected="stale") == "at-1"
    # ... the second must reuse at-1 instead of refreshing again (which would also burn rt-2).
    assert provider._access_token(rejected="stale") == "at-1"
    assert len(fake.token_bodies) == 1
    # Only when the CURRENT token is the one that failed is another refresh justified.
    assert provider._access_token(rejected="at-1") == "at-2"
    assert len(fake.token_bodies) == 2


def test_parallel_calls_with_a_stale_token_refresh_once():
    fake = FakeTrueLayer()
    gate = threading.Barrier(4)
    original = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/data/v1/me" and request.headers["authorization"] == "Bearer stale":
            try:
                gate.wait(timeout=5)  # make sure all four hold the stale token at once
            except threading.BrokenBarrierError:
                pass
        return original(request)

    store = MemoryTokenStore(
        {STORE_SECTION: {"refresh_token": "rt-1", "access_token": "stale", "access_token_expires_at": NOW + 3000}}
    )
    provider = TrueLayerProvider(
        TrueLayerConfig("cid", "secret"),
        store,
        transport=httpx.MockTransport(handler),
        backoff_seconds=0,
        clock=lambda: NOW,
    )
    results: list[bool] = []
    threads = [
        threading.Thread(target=lambda: results.append(provider.connection_status()["connected"])) for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert results == [True] * 4
    assert len(fake.token_bodies) == 1  # one refresh served all four


def test_token_replaced_by_the_link_helper_is_picked_up_without_a_restart():
    fake = FakeTrueLayer()
    store = MemoryTokenStore({STORE_SECTION: {"refresh_token": "old-and-revoked"}})
    provider, _, _, _ = make(store=store, fake=fake)

    original = fake.handler

    def relink_during_refresh(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.url.path == "/connect/token" and response.status_code == 400:
            store.put(STORE_SECTION, {"refresh_token": "rt-1"})  # another process just re-linked
        return response

    provider._client = httpx.Client(transport=httpx.MockTransport(relink_during_refresh))
    assert len(provider.list_accounts()) == 3
    assert [b["refresh_token"] for b in fake.token_bodies] == ["old-and-revoked", "rt-1"]

    provider, _, _, fake = make(store=MemoryTokenStore({STORE_SECTION: {"refresh_token": "revoked"}}))
    with pytest.raises(ConsentExpiredError):
        provider.list_accounts()
    assert len(fake.token_bodies) == 1  # nothing newer in the store: no second attempt


# ------------------------------------------------------------- request limits


def test_malformed_account_ids_are_rejected_before_any_request():
    provider, rec, _, _ = make()
    for bad in ("../me", "acc/../../me", "acc current", "acc?x=1", "", "١٢٣", "accé", "acc%2e"):
        for call in (
            lambda b=bad: provider.get_balance(b),
            lambda b=bad: provider.fetch_transactions(b, date(2026, 10, 1), date(2026, 10, 8)),
        ):
            with pytest.raises(InvalidRequestError) as info:
                call()
            assert info.value.code == "invalid_request"
    assert rec.requests == []  # "١٢٣" (Arabic-Indic digits) passes str.isalnum() but not this check


def test_request_budget_stops_fan_out():
    # Day-sized windows over a year would be hundreds of requests: refused before the first one.
    provider, rec, _, _ = make(window_days=1)
    with pytest.raises(InvalidRequestError) as info:
        provider.fetch_transactions("acc-current", date(2025, 10, 1), date(2026, 10, 1))
    assert "safety limit" in info.value.message and rec.requests == []

    # The hard per-call budget also counts retries and cannot be exceeded whatever happens upstream.
    fake = FakeTrueLayer()
    fake.overrides[TXN_PATH] = reply(503, {"error": "provider_error"})
    provider, rec, _, _ = make(fake=fake, window_days=20)
    provider._max_retries = 10_000
    with pytest.raises(ProviderError) as info:
        provider.fetch_transactions("acc-current", date(2026, 9, 1), date(2026, 10, 1))
    assert "safety limit" in info.value.message
    assert len(rec.requests) == MAX_REQUESTS_PER_CALL

    # A full default-maximum range (400 days) stays well inside the budget.
    provider, rec, _, _ = make()
    provider.fetch_transactions("acc-current", date(2025, 9, 6), date(2026, 10, 11))
    assert len(rec.requests) <= 10


def test_429_is_never_retried():
    fake = FakeTrueLayer()
    fake.overrides[TXN_PATH] = reply(429, {"error": "provider_too_many_requests"})
    provider, rec, _, _ = make(fake=fake)
    with pytest.raises(RateLimitError):
        provider.list_transactions("acc-current", date(2026, 10, 1), date(2026, 10, 8))
    assert len(data_calls(rec, "/transactions")) == 1


def test_base_url_overrides_must_be_https_unless_loopback():
    for bad in ("http://evil.example.org", "ftp://127.0.0.1", "https://user:pw@api.example.org", "api.example.org"):
        with pytest.raises(ConfigError) as info:
            TrueLayerConfig("cid", "secret", api_base=bad)
        assert "TRUELAYER_API_BASE" in info.value.message
        with pytest.raises(ConfigError):
            TrueLayerConfig("cid", "secret", auth_base=bad)
    cfg = TrueLayerConfig("cid", "secret", api_base="http://127.0.0.1:9000/", auth_base="https://auth.example.org")
    assert cfg.api_base == "http://127.0.0.1:9000" and cfg.auth_base == "https://auth.example.org"
    assert TrueLayerConfig("cid", "secret", api_base="http://localhost:1").api_base == "http://localhost:1"

    base = {"NATIONWIDE_TOKEN_STORE": "memory"}
    with pytest.raises(ConfigError) as info:
        build_provider(
            {
                **base,
                "NATIONWIDE_PROVIDER": "lunchflow",
                "LUNCHFLOW_API_KEY": "k",
                "LUNCHFLOW_BASE_URL": "http://lunch.example.org",
            }
        )
    assert "LUNCHFLOW_BASE_URL" in info.value.message
    with pytest.raises(ConfigError) as info:
        build_provider(
            {
                **base,
                "NATIONWIDE_PROVIDER": "enablebanking",
                "ENABLEBANKING_APP_ID": "a",
                "ENABLEBANKING_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\\nx\\n-----END PRIVATE KEY-----",
                "ENABLEBANKING_BASE_URL": "http://eb.example.org",
            }
        )
    assert "ENABLEBANKING_BASE_URL" in info.value.message


def test_one_user_agent_on_token_and_data_requests():
    provider, rec, _, _ = make()
    provider.list_accounts()
    assert {r.headers["User-Agent"] for r in rec.requests} == {USER_AGENT}
    assert USER_AGENT.startswith("nationwide-mcp/") and USER_AGENT != "nationwide-mcp/"


# ------------------------------------------------------ dates and de-duplication


def _txn(ts: str, amount: float, description: str, **extra) -> dict:
    return {
        "transaction_id": "volatile",
        "timestamp": ts,
        "description": description,
        "amount": amount,
        "currency": "GBP",
        **extra,
    }


def test_dates_are_london_dates_across_the_bst_boundary():
    fake = FakeTrueLayer()
    fake.overrides[TXN_PATH] = reply(
        200,
        {
            "results": [
                _txn("2026-06-30T22:59:59Z", -1, "STILL JUNE", normalised_provider_transaction_id="a"),  # 23:59:59 BST
                _txn(
                    "2026-06-30T23:00:00Z", -2, "ALREADY JULY", normalised_provider_transaction_id="b"
                ),  # 00:00 BST 1 Jul
                _txn("2026-07-01T00:30:00+01:00", -3, "JULY WITH OFFSET", normalised_provider_transaction_id="c"),
                _txn(
                    "2026-07-01T23:30:00-05:00", -4, "NEW YORK EVENING", normalised_provider_transaction_id="d"
                ),  # 05:30 BST 2 Jul
                _txn("2026-07-01", -5, "DATE ONLY", normalised_provider_transaction_id="e"),
                _txn("2026-01-15T23:30:00Z", -6, "WINTER IS UTC", normalised_provider_transaction_id="f"),
            ]
        },
    )
    provider, rec, _, _ = make(fake=fake)
    dates = {
        t["description"]: t["date"]
        for t in provider.list_transactions("acc-current", date(2026, 1, 1), date(2026, 8, 1))
    }
    assert dates == {
        "STILL JUNE": "2026-06-30",
        "ALREADY JULY": "2026-07-01",
        "JULY WITH OFFSET": "2026-07-01",
        "NEW YORK EVENING": "2026-07-02",
        "DATE ONLY": "2026-07-01",
        "WINTER IS UTC": "2026-01-15",
    }
    # The window is applied to the LONDON date: "ALREADY JULY" belongs to a July window, not a June one,
    # and the request starts a day early so the bank's UTC filter cannot cut it off.
    july = provider.list_transactions("acc-current", date(2026, 7, 1), date(2026, 7, 2))
    assert sorted(t["description"] for t in july) == ["ALREADY JULY", "DATE ONLY", "JULY WITH OFFSET"]
    assert data_calls(rec, "/transactions")[-1].url.params["from"] == "2026-06-30"


def test_today_is_the_london_date_just_after_midnight_bst():
    just_after_midnight = datetime(2026, 6, 30, 23, 30, tzinfo=UTC).timestamp()  # 00:30 on 1 July in London
    provider, rec, _, _ = make(clock=lambda: just_after_midnight)
    provider.fetch_transactions("acc-current", date(2026, 7, 1), date(2026, 7, 2))
    call = data_calls(rec, "/transactions")[-1]
    # 1 July has begun in London, so the window is live; `to` is still not in the (UTC) future.
    assert dict(call.url.params) == {"from": "2026-06-30", "to": "2026-06-30"}


def test_two_transactions_sharing_a_provider_id_are_both_kept(caplog):
    """Reproduced data loss: -5.00 COFFEE and -7.50 LUNCH with the same normalised id."""
    fake = FakeTrueLayer()
    fake.overrides[TXN_PATH] = reply(
        200,
        {
            "results": [
                _txn("2026-10-05T10:00:00Z", -5.00, "COFFEE", normalised_provider_transaction_id="dup-1"),
                _txn("2026-10-05T13:00:00Z", -7.50, "LUNCH", normalised_provider_transaction_id="dup-1"),
                _txn(
                    "2026-10-05T13:00:00Z", -7.50, "LUNCH", normalised_provider_transaction_id="dup-1"
                ),  # exact repeat
            ]
        },
    )
    provider, _, _, _ = make(fake=fake)
    with caplog.at_level(logging.WARNING, logger="nationwide_mcp.models"):
        txns = provider.list_transactions("acc-current", date(2026, 10, 1), date(2026, 10, 8))
    assert sorted((t["description"], t["amount_minor"]) for t in txns) == [("COFFEE", -500), ("LUNCH", -750)]
    assert len({t["id"] for t in txns}) == 2
    assert any("provider reference shared" in r.message for r in caplog.records)
    fake.refresh_tokens = ["rt-1"]  # a fresh provider below starts from the seed store again
    again = make(fake=fake)[0].list_transactions("acc-current", date(2026, 10, 1), date(2026, 10, 8))
    assert [t["id"] for t in again] == [t["id"] for t in txns]  # the derived id is stable


def test_overlapping_windows_do_not_duplicate_or_drop_anything():
    boundary = "2026-07-10"  # with 20-day windows from 20 June, 9 July closes one window and opens the next

    def handler(request: httpx.Request) -> httpx.Response | None:
        lo, hi = request.url.params["from"], request.url.params["to"]
        rows = [
            _txn("2026-07-09T12:00:00Z", -1.00, "WITH ID ON SHARED DAY", normalised_provider_transaction_id="n-1"),
            _txn("2026-07-09T12:00:00Z", -3.00, "COFFEE NO ID"),
            _txn("2026-07-09T15:00:00Z", -3.00, "COFFEE NO ID"),  # a genuine second identical coffee
            _txn(f"{boundary}T12:00:00Z", -9.00, "DAY AFTER", normalised_provider_transaction_id="n-2"),
        ]
        return reply(200, {"results": [r for r in rows if lo <= r["timestamp"][:10] <= hi]})

    fake = FakeTrueLayer()
    original = fake.handler

    def routed(request: httpx.Request) -> httpx.Response:
        if request.url.path == TXN_PATH and request.headers.get("authorization") == "Bearer at-1":
            return handler(request)
        return original(request)

    rec = Recorder(routed)
    provider = TrueLayerProvider(
        TrueLayerConfig("cid", "secret", window_days=20),
        MemoryTokenStore({STORE_SECTION: {"refresh_token": "rt-1"}}),
        transport=rec.transport,
        backoff_seconds=0,
        clock=lambda: NOW,
    )
    txns = provider.list_transactions("acc-current", date(2026, 6, 21), date(2026, 8, 1))
    spans = [(r.url.params["from"], r.url.params["to"]) for r in data_calls(rec, "/transactions")]
    assert spans[:2] == [("2026-06-20", "2026-07-09"), ("2026-07-09", "2026-07-28")]  # 9 July is asked for twice
    assert sorted((t["description"], t["amount_minor"]) for t in txns) == [
        ("COFFEE NO ID", -300),
        ("COFFEE NO ID", -300),
        ("DAY AFTER", -900),
        ("WITH ID ON SHARED DAY", -100),
    ]
    assert len({t["id"] for t in txns}) == 4


# ------------------------------------------------------------------- pending


def test_pending_left_out_is_counted_and_dated():
    provider, _, _, _ = make()
    result = provider.fetch_transactions("acc-current", date(2026, 10, 1), date(2026, 10, 11))
    assert [t["pending"] for t in result["transactions"]] == [False, False, False]
    assert result["pending_excluded"] == 1 and result["earliest_pending_date"] == "2026-10-09"
    assert result["pending_supported"] is True

    with_pending = provider.fetch_transactions("acc-current", date(2026, 10, 1), date(2026, 10, 11), True)
    assert with_pending["pending_excluded"] == 0 and with_pending["earliest_pending_date"] == "2026-10-09"
    booked_ids = [t["id"] for t in result["transactions"]]
    assert [t["id"] for t in with_pending["transactions"] if not t["pending"]] == booked_ids  # ids do not shift

    fake = FakeTrueLayer()  # the bank has no pending endpoint: say so even when pending was not asked for
    fake.overrides[TXN_PATH + "/pending"] = reply(501, {"error": "endpoint_not_supported"})
    result = make(fake=fake)[0].fetch_transactions("acc-current", date(2026, 10, 1), date(2026, 10, 11))
    assert result["pending_supported"] is False and result["pending_excluded"] == 0
    assert result["earliest_pending_date"] is None and len(result["transactions"]) == 3


# ----------------------------------------------------------------- malformed


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>gateway</html>"),
        httpx.Response(200, content=b'["not", "an", "object"]'),
        reply(200, {"results": "nope"}),
        reply(200, {"results": ["not-an-object"]}),
        reply(200, {"results": [{"timestamp": "2026-10-05T00:00:00Z", "amount": "abc", "currency": "GBP"}]}),
        reply(200, {"results": [{"timestamp": "2026-10-05T00:00:00Z", "currency": "GBP"}]}),  # no amount
        reply(200, {"results": [{"timestamp": "2026-10-05T00:00:00Z", "amount": "1e999999", "currency": "GBP"}]}),
        reply(200, {"results": [{"timestamp": "2026-10-05T00:00:00Z", "amount": "NaN", "currency": "GBP"}]}),
        reply(200, {"results": [{"timestamp": "yesterday", "amount": 1, "currency": "GBP"}]}),
        reply(
            200,
            {
                "results": [
                    {
                        "timestamp": "2026-10-05T00:00:00Z",
                        "amount": 1,
                        "currency": "GBP",
                        "transaction_classification": 7,
                    }
                ]
            },
        ),
        reply(418, {"error": "teapot"}),
    ],
)
def test_malformed_upstream_data_is_a_provider_error(response):
    fake = FakeTrueLayer()
    fake.overrides[TXN_PATH] = response
    provider, _, _, _ = make(fake=fake)
    with pytest.raises(ProviderError) as info:
        provider.list_transactions("acc-current", date(2026, 10, 1), date(2026, 10, 8))
    assert info.value.code == "provider_error" and str(info.value).startswith("[provider_error] ")


def test_notes_are_null_never_empty():
    provider, _, _, _ = make()
    txns = provider.list_transactions("acc-current", date(2026, 10, 1), date(2026, 10, 8))
    assert all(t["notes"] is None or t["notes"].strip() for t in txns)
    assert [t["notes"] for t in txns if t["description"] == ACCOUNT_TXNS[0]["description"]] == [None]
