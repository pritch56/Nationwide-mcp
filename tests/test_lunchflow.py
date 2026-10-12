from datetime import date

import httpx
import pytest

from conftest import Recorder, reply
from nationwide_mcp.errors import (
    AuthError,
    ConfigError,
    ConsentExpiredError,
    NotFoundError,
    ProviderUnavailableError,
    RateLimitError,
)
from nationwide_mcp.providers.lunchflow import LunchFlowProvider

ACCOUNTS = {
    "accounts": [
        {
            "id": 11,
            "connection_id": 1,
            "name": "FlexDirect",
            "institution_name": "Nationwide Building Society",
            "provider": "gocardless",
            "currency": "GBP",
            "status": "ACTIVE",
        },
        {
            "id": 12,
            "connection_id": 1,
            "name": "Member Credit Card",
            "institution_name": "Nationwide",
            "provider": "gocardless",
            "currency": "GBP",
            "status": "ACTIVE",
        },
        {
            "id": 99,
            "connection_id": 2,
            "name": "Current",
            "institution_name": "Monzo",
            "provider": "gocardless",
            "currency": "GBP",
            "status": "ACTIVE",
        },
    ],
    "total": 3,
}
TXNS = {
    "transactions": [
        {
            "id": "t-1",
            "accountId": 11,
            "amount": -12.34,
            "currency": "GBP",
            "date": "2026-10-05",
            "merchant": "Tesco",
            "description": "TESCO STORES 1234",
            "isPending": False,
        },
        {
            "id": "t-2",
            "accountId": 11,
            "amount": 1500,
            "currency": "GBP",
            "date": "2026-10-06",
            "merchant": None,
            "description": "SALARY ACME LTD",
            "isPending": False,
        },
        {
            "id": "t-3",
            "accountId": 11,
            "amount": -4.5,
            "currency": "GBP",
            "date": "2026-10-07",
            "merchant": "Pret",
            "description": "",
            "isPending": True,
        },
        {
            "id": "t-0",
            "accountId": 11,
            "amount": -1,
            "currency": "GBP",
            "date": "2026-09-01",
            "merchant": "Old",
            "description": "OUTSIDE WINDOW",
            "isPending": False,
        },
    ],
    "total": 4,
}


def make(handler, **kw):
    rec = Recorder(handler)
    return LunchFlowProvider("test-key", transport=rec.transport, **kw), rec


def happy(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/accounts"):
        return reply(200, ACCOUNTS)
    if path.endswith("/balance"):
        return reply(200, {"balance": {"amount": 1234.56, "currency": "GBP"}})
    if path.endswith("/transactions"):
        return reply(200, TXNS)
    return reply(404, {"error": "Not found", "message": "nope"})


def test_requires_api_key():
    with pytest.raises(ConfigError):
        LunchFlowProvider("")


def test_api_key_header_sent_and_accounts_filtered_to_nationwide():
    provider, rec = make(happy)
    accounts = provider.list_accounts()
    assert [a["id"] for a in accounts] == ["11", "12"]  # Monzo filtered out
    assert accounts[0]["type"] == "current" and accounts[1]["type"] == "credit_card"
    assert accounts[0]["source"] == "nationwide"
    assert rec.requests[0].headers["x-api-key"] == "test-key"
    assert rec.requests[0].url.path == "/api/v1/accounts"


def test_other_banks_accounts_are_not_readable():
    provider, _ = make(happy)
    with pytest.raises(NotFoundError):
        provider.get_balance("99")


def test_balance_in_minor_units():
    provider, _ = make(happy)
    balance = provider.get_balance("11")
    assert balance["current_minor"] == 123456 and balance["currency"] == "GBP"
    assert balance["account_id"] == "11"


def test_transactions_normalised_signs_pending_window_and_query():
    provider, rec = make(happy)
    txns = provider.list_transactions("11", date(2026, 10, 1), date(2026, 10, 8), include_pending=True)
    query = dict(rec.requests[-1].url.params)
    assert query == {"from": "2026-10-01", "to": "2026-10-07", "include_pending": "true"}  # exclusive -> inclusive
    assert [t["amount_minor"] for t in txns] == [-1234, 150000, -450]
    assert [t["pending"] for t in txns] == [False, False, True]
    assert txns[0]["merchant"] == "Tesco" and txns[0]["description"] == "TESCO STORES 1234"
    assert txns[1]["merchant"] is None
    assert txns[2]["description"] == "Pret"  # falls back to merchant
    assert "pending" in txns[2]["notes"]
    assert all(t["account_id"] == "11" and t["source"] == "nationwide" and t["currency"] == "GBP" for t in txns)
    assert all(isinstance(t["amount_minor"], int) for t in txns)
    assert "OUTSIDE WINDOW" not in [t["description"] for t in txns]


def test_ids_stable_across_runs_and_exclude_pending():
    provider, rec = make(happy)
    first = provider.list_transactions("11", date(2026, 10, 1), date(2026, 10, 8), include_pending=True)
    second = provider.list_transactions(
        "11", date(2026, 10, 3), date(2026, 10, 9), include_pending=True
    )  # overlapping window
    ids = {t["description"]: t["id"] for t in first}
    assert all(ids[t["description"]] == t["id"] for t in second)
    booked = provider.list_transactions("11", date(2026, 10, 1), date(2026, 10, 8))  # pending excluded by default
    assert all(not t["pending"] for t in booked) and len(booked) == 2
    # pending is always requested upstream so that the excluded ones can be counted
    assert dict(rec.requests[-1].url.params)["include_pending"] == "true"


def test_invert_sign_option():
    provider, _ = make(happy, invert_sign=True)
    txns = provider.list_transactions("11", date(2026, 10, 1), date(2026, 10, 8), include_pending=True)
    assert [t["amount_minor"] for t in txns] == [1234, -150000, 450]


@pytest.mark.parametrize(
    "status,body,headers,error",
    [
        (401, {"error": "Unauthorized", "message": "Authentication required."}, None, AuthError),
        (403, {"error": "Forbidden", "message": "subscription inactive"}, None, AuthError),
        (429, {"error": "Too many requests", "message": "slow down"}, {"Retry-After": "120"}, RateLimitError),
        (400, {"error": "Bad request", "message": "Connection expired, please reconnect"}, None, ConsentExpiredError),
        (503, {"error": "Unavailable", "message": "upstream"}, None, ProviderUnavailableError),
    ],
)
def test_error_mapping(status, body, headers, error):
    provider, _ = make(lambda request: reply(status, body, headers))
    with pytest.raises(error) as info:
        provider.list_accounts()
    assert "test-key" not in str(info.value)
    if error is RateLimitError:
        assert info.value.retry_after_seconds == 120 and "2 minutes" in info.value.message


def test_network_failure_is_upstream_unavailable():
    def boom(request):
        raise httpx.ConnectTimeout("timed out")

    provider, _ = make(boom)
    with pytest.raises(ProviderUnavailableError):
        provider.list_accounts()


def test_connection_status_active_with_estimated_expiry():
    provider, _ = make(happy, connected_on=date(2026, 9, 1), consent_days=90, today=lambda: date(2026, 10, 10))
    status = provider.connection_status()
    assert status["connected"] is True and status["needs_reauthorisation"] is False
    assert status["consent_expires_at"] == "2026-11-30" and status["days_remaining"] == 51
    assert status["consent_expiry_is_estimate"] is True


def test_connection_status_flags_inactive_and_expired_and_auth():
    def inactive(request):
        return reply(200, {"accounts": [dict(ACCOUNTS["accounts"][0], status="EXPIRED")]})

    provider, _ = make(inactive, connected_on=date(2026, 6, 1), today=lambda: date(2026, 10, 10))
    status = provider.connection_status()
    assert status["connected"] is False and status["needs_reauthorisation"] is True
    assert status["status"] == "NEEDS_ATTENTION" and status["days_remaining"] < 0

    provider, _ = make(lambda r: reply(401, {"error": "Unauthorized", "message": "x"}))
    status = provider.connection_status()  # must report, not raise
    assert status["status"] == "token_expired" and status["needs_reauthorisation"] is True

    provider, _ = make(lambda r: reply(200, {"accounts": []}))
    assert provider.connection_status()["status"] == "NO_ACCOUNTS"
