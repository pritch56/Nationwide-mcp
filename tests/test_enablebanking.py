from datetime import UTC, date, datetime

import httpx
import jwt
import pytest

from conftest import Recorder, reply
from nationwide_mcp.errors import (
    AuthError,
    ConfigError,
    ConsentExpiredError,
    NotConnectedError,
    NotFoundError,
    ProviderUnavailableError,
    RateLimitError,
)
from nationwide_mcp.providers.enablebanking import STORE_SECTION, EnableBankingProvider
from nationwide_mcp.token_store import MemoryTokenStore

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC).timestamp()
SESSION = {
    "status": "AUTHORIZED",
    "access": {"valid_until": "2027-01-08T12:00:00+00:00"},
    "accounts": ["uid-1", "uid-2"],
    "accounts_data": [
        {"uid": "uid-1", "identification_hash": "hash-current"},
        {"uid": "uid-2", "identification_hash": "hash-card"},
    ],
    "aspsp": {"name": "Nationwide", "country": "GB"},
}
STORED = {
    STORE_SECTION: {
        "session_id": "sess-1",
        "valid_until": "2027-01-08T12:00:00+00:00",
        "accounts": [
            {
                "uid": "uid-1",
                "identification_hash": "hash-current",
                "name": "FlexDirect",
                "currency": "GBP",
                "cash_account_type": "CACC",
                "mask": "5678",
            },
            {
                "uid": "uid-2",
                "identification_hash": "hash-card",
                "name": "Member Credit Card",
                "currency": "GBP",
                "cash_account_type": "CARD",
                "mask": "4321",
            },
        ],
    }
}
PAGE_1 = {
    "transactions": [
        {
            "entry_reference": "E1",
            "transaction_amount": {"currency": "GBP", "amount": "12.34"},
            "credit_debit_indicator": "DBIT",
            "status": "BOOK",
            "booking_date": "2026-10-05",
            "creditor": {"name": "TESCO STORES"},
            "remittance_information": ["TESCO STORES 1234", "LONDON"],
            "bank_transaction_code": {"description": "Visa purchase"},
            "merchant_category_code": "5411",
        },
        {
            "entry_reference": "E2",
            "transaction_amount": {"currency": "GBP", "amount": "1500.00"},
            "credit_debit_indicator": "CRDT",
            "status": "BOOK",
            "booking_date": "2026-10-06",
            "debtor": {"name": "ACME LTD"},
            "remittance_information": ["SALARY"],
        },
    ],
    "continuation_key": "page-2",
}
PAGE_2 = {
    "transactions": [
        {
            "transaction_id": "volatile-1",
            "transaction_amount": {"currency": "GBP", "amount": "4.50"},
            "credit_debit_indicator": "DBIT",
            "status": "PDNG",
            "value_date": "2026-10-07",
            "creditor": {"name": "PRET A MANGER"},
        },
        {
            "entry_reference": "E3",
            "transaction_amount": {"currency": "GBP", "amount": "9.99"},
            "credit_debit_indicator": "DBIT",
            "status": "RJCT",
            "booking_date": "2026-10-07",
        },
        {
            "entry_reference": "E4",
            "transaction_amount": {"currency": "GBP", "amount": "1.00"},
            "credit_debit_indicator": "DBIT",
            "status": "BOOK",
            "booking_date": "2026-09-30",
        },
    ],
    "continuation_key": "page-3",
}
PAGE_3 = {
    "transactions": [
        {
            "entry_reference": "E5",
            "transaction_amount": {"currency": "GBP", "amount": "20.00"},
            "credit_debit_indicator": "CRDT",
            "status": "BOOK",
            "booking_date": "2026-10-07",
            "debtor": {"name": "J SMITH"},
            "remittance_information": ["REFUND"],
        },
    ]
}


def happy(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/sessions/sess-1":
        return reply(200, SESSION)
    if path.endswith("/balances"):
        return reply(
            200,
            {
                "balances": [
                    {
                        "name": "Booked",
                        "balance_type": "CLBD",
                        "balance_amount": {"currency": "GBP", "amount": "1234.56"},
                        "reference_date": "2026-10-09",
                    },
                    {
                        "name": "Available",
                        "balance_type": "ITAV",
                        "balance_amount": {"currency": "GBP", "amount": "1200.00"},
                    },
                ]
            },
        )
    if path.endswith("/transactions"):
        key = request.url.params.get("continuation_key")
        return reply(200, {None: PAGE_1, "page-2": PAGE_2, "page-3": PAGE_3}[key])
    return reply(404, {"error": "NOT_FOUND", "message": "nope"})


def make(rsa_keys, handler=happy, store=None, **kw):
    rec = Recorder(handler)
    store = store if store is not None else MemoryTokenStore(STORED)
    provider = EnableBankingProvider(
        "app-123", rsa_keys[0], store, transport=rec.transport, clock=kw.pop("clock", lambda: NOW), **kw
    )
    return provider, rec, store


# -- auth / token handling ---------------------------------------------------


def test_config_errors(rsa_keys):
    with pytest.raises(ConfigError):
        EnableBankingProvider("", rsa_keys[0], MemoryTokenStore())
    with pytest.raises(ConfigError):
        EnableBankingProvider("app", "", MemoryTokenStore())
    bad = EnableBankingProvider(
        "app",
        "-----BEGIN PRIVATE KEY-----\nxxxx\n-----END PRIVATE KEY-----\n",
        MemoryTokenStore(STORED),
        transport=Recorder(happy).transport,
    )
    with pytest.raises(ConfigError) as info:
        bad.list_accounts()
    assert "xxxx" not in str(info.value)


def test_jwt_is_rs256_signed_with_kid_and_claims(rsa_keys):
    provider, rec, _ = make(rsa_keys)
    provider.list_accounts()
    token = rec.requests[0].headers["Authorization"].removeprefix("Bearer ")
    assert jwt.get_unverified_header(token) == {"alg": "RS256", "kid": "app-123", "typ": "JWT"}
    claims = jwt.decode(
        token,
        rsa_keys[1],
        algorithms=["RS256"],
        audience="api.enablebanking.com",
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims["iss"] == "enablebanking.com"
    assert claims["iat"] == int(NOW) and 0 < claims["exp"] - claims["iat"] <= 86400


def test_jwt_reused_until_near_expiry_then_reissued(rsa_keys):
    clock = {"now": NOW}
    provider, rec, _ = make(rsa_keys, clock=lambda: clock["now"])
    provider.get_balance("hash-current")
    provider.get_balance("hash-current")
    first = {r.headers["Authorization"] for r in rec.requests}
    assert len(first) == 1
    clock["now"] = NOW + 3590  # inside the 60 s safety margin
    provider.get_balance("hash-current")
    assert rec.requests[-1].headers["Authorization"] not in first


def test_rejected_jwt_is_reissued_once_then_reported(rsa_keys):
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return reply(401, {"error": "UNAUTHORIZED_ACCESS", "message": "token expired"})
        return happy(request)

    provider, rec, _ = make(rsa_keys, flaky)
    assert len(provider.list_accounts()) == 2
    assert rec.paths()[:2] == ["/sessions/sess-1", "/sessions/sess-1"]  # retried exactly once

    provider, rec, _ = make(rsa_keys, lambda r: reply(401, {"error": "UNAUTHORIZED_ACCESS", "message": "bad"}))
    with pytest.raises(AuthError) as info:
        provider.list_accounts()
    assert len(rec.requests) == 2 and "ENABLEBANKING_APP_ID" in info.value.message


# -- accounts / balances -----------------------------------------------------


def test_accounts_use_stable_identification_hash(rsa_keys):
    provider, _, _ = make(rsa_keys)
    accounts = provider.list_accounts()
    assert [(a["id"], a["type"], a["mask"]) for a in accounts] == [
        ("hash-current", "current", "5678"),
        ("hash-card", "credit_card", "4321"),
    ]
    assert all(a["source"] == "nationwide" and a["currency"] == "GBP" for a in accounts)


def test_session_id_from_env_fetches_details_when_store_is_empty(rsa_keys):
    def handler(request):
        if request.url.path == "/accounts/uid-1/details":
            return reply(
                200,
                {
                    "name": "FlexDirect",
                    "currency": "GBP",
                    "cash_account_type": "CACC",
                    "account_id": {"other": {"identification": "00000012345678"}},
                },
            )
        if request.url.path.endswith("/details"):
            return reply(500, {"error": "ASPSP_ERROR", "message": "x"})  # cosmetic failure tolerated
        return happy(request)

    provider, _, _ = make(rsa_keys, handler, store=MemoryTokenStore(), session_id="sess-1")
    accounts = provider.list_accounts()
    assert accounts[0]["name"] == "FlexDirect" and accounts[0]["mask"] == "5678"
    assert accounts[1]["id"] == "hash-card" and accounts[1]["name"] == "Nationwide account"  # never empty


def test_balance_picks_current_and_available(rsa_keys):
    provider, rec, _ = make(rsa_keys)
    balance = provider.get_balance("hash-current")
    assert rec.paths()[-1] == "/accounts/uid-1/balances"  # stable id translated to session uid
    assert balance["current_minor"] == 123456 and balance["available_minor"] == 120000
    assert balance["account_id"] == "hash-current" and balance["as_of"] == "2026-10-09"
    assert len(balance["balances"]) == 2


def test_unknown_account(rsa_keys):
    provider, _, _ = make(rsa_keys)
    with pytest.raises(NotFoundError):
        provider.get_balance("nope")


# -- transactions: pagination + normalisation --------------------------------


def test_pagination_follows_continuation_keys(rsa_keys):
    provider, rec, _ = make(rsa_keys)
    txns = provider.list_transactions("hash-current", date(2026, 10, 1), date(2026, 10, 8), include_pending=True)
    pages = [r for r in rec.requests if r.url.path == "/accounts/uid-1/transactions"]
    assert [p.url.params.get("continuation_key") for p in pages] == [None, "page-2", "page-3"]
    assert all(p.url.params["date_from"] == "2026-10-01" and p.url.params["date_to"] == "2026-10-07" for p in pages)
    assert len(txns) == 4  # E1, E2, pending, E5; rejected and out-of-window dropped


def test_normalisation_signs_pending_merchant_notes(rsa_keys):
    provider, _, _ = make(rsa_keys)
    txns = {
        t["description"]: t
        for t in provider.list_transactions("hash-current", date(2026, 10, 1), date(2026, 10, 8), include_pending=True)
    }
    tesco = txns["TESCO STORES 1234 LONDON"]
    assert tesco["amount_minor"] == -1234 and tesco["merchant"] == "TESCO STORES" and tesco["pending"] is False
    assert tesco["date"] == "2026-10-05" and tesco["currency"] == "GBP" and tesco["category"] is None
    assert "type: Visa purchase" in tesco["notes"] and "mcc: 5411" in tesco["notes"]
    salary = txns["SALARY"]
    assert salary["amount_minor"] == 150000 and salary["merchant"] == "ACME LTD"  # credit: counterparty is debtor
    pret = txns["PRET A MANGER"]
    assert pret["pending"] is True and pret["amount_minor"] == -450 and pret["date"] == "2026-10-07"
    assert "pending" in pret["notes"]
    assert txns["REFUND"]["amount_minor"] == 2000
    assert all(t["account_id"] == "hash-current" and t["source"] == "nationwide" for t in txns.values())


def test_ids_stable_across_runs_and_sessions_and_pending_filter(rsa_keys):
    provider, _, _ = make(rsa_keys)
    first = provider.list_transactions("hash-current", date(2026, 10, 1), date(2026, 10, 8), include_pending=True)

    # A re-authorisation issues new uids; ids must not change.
    renewed = {
        **SESSION,
        "accounts": ["uid-9"],
        "accounts_data": [{"uid": "uid-9", "identification_hash": "hash-current"}],
    }

    def after_reauth(request):
        if request.url.path == "/sessions/sess-2":
            return reply(200, renewed)
        if request.url.path == "/accounts/uid-9/transactions":
            key = request.url.params.get("continuation_key")
            return reply(200, {None: PAGE_1, "page-2": PAGE_2, "page-3": PAGE_3}[key])
        return reply(404, {"error": "ACCOUNT_DOES_NOT_EXIST", "message": "old uid"})

    provider2, _, _ = make(rsa_keys, after_reauth, store=MemoryTokenStore(), session_id="sess-2")
    second = provider2.list_transactions("hash-current", date(2026, 10, 1), date(2026, 10, 8), include_pending=True)
    assert [t["id"] for t in first] == [t["id"] for t in second]
    assert len({t["id"] for t in first}) == len(first)

    booked = provider.list_transactions(
        "hash-current", date(2026, 10, 1), date(2026, 10, 8)
    )  # default excludes pending
    assert len(booked) == 3 and not any(t["pending"] for t in booked)


# -- error cases -------------------------------------------------------------


@pytest.mark.parametrize("code", ["EXPIRED_SESSION", "REVOKED_SESSION", "CLOSED_SESSION"])
def test_expired_consent_on_fetch(rsa_keys, code):
    def handler(request):
        if request.url.path.endswith("/transactions"):
            return reply(401, {"error": code, "message": "Session is not valid", "code": 401})
        return happy(request)

    provider, rec, _ = make(rsa_keys, handler)
    with pytest.raises(ConsentExpiredError) as info:
        provider.list_transactions("hash-current", date(2026, 10, 1), date(2026, 10, 8), include_pending=True)
    assert "nationwide-mcp-link link" in info.value.message
    assert rec.paths().count("/accounts/uid-1/transactions") == 1  # no pointless JWT retry


def test_expired_consent_detected_from_dates_and_status_and_missing_session(rsa_keys):
    late = datetime(2027, 2, 1, tzinfo=UTC).timestamp()
    provider, _, _ = make(rsa_keys, clock=lambda: late)
    with pytest.raises(ConsentExpiredError) as info:
        provider.list_accounts()
    assert "2027-01-08" in info.value.message

    provider, _, _ = make(rsa_keys, lambda r: reply(200, {**SESSION, "status": "EXPIRED"}))
    with pytest.raises(ConsentExpiredError):
        provider.list_accounts()

    provider, rec, _ = make(rsa_keys, store=MemoryTokenStore())
    with pytest.raises(NotConnectedError) as info:
        provider.list_accounts()
    assert "No Nationwide session" in info.value.message and rec.requests == []


def test_rate_limit(rsa_keys):
    def handler(request):
        if request.url.path.endswith("/balances"):
            return reply(429, {"error": "ASPSP_RATE_LIMIT_EXCEEDED", "message": "Too many requests", "code": 429})
        return happy(request)

    provider, _, _ = make(rsa_keys, handler)
    with pytest.raises(RateLimitError) as info:
        provider.get_balance("hash-current")
    assert info.value.retry_after_seconds == 6 * 3600
    assert "do not re-authorise" in info.value.message


def test_upstream_and_forbidden_errors(rsa_keys):
    def aspsp_down(request):
        if request.url.path.endswith("/balances"):
            return reply(500, {"error": "ASPSP_ERROR", "message": "bank down"})
        return happy(request)

    provider, _, _ = make(rsa_keys, aspsp_down)
    with pytest.raises(ProviderUnavailableError):
        provider.get_balance("hash-current")

    provider, _, _ = make(rsa_keys, lambda r: reply(403, {"error": "ACCESS_DENIED", "message": "no"}))
    with pytest.raises(AuthError):
        provider.list_accounts()


# -- status + link flow ------------------------------------------------------


def test_connection_status_reports_expiry(rsa_keys):
    provider, _, _ = make(rsa_keys)
    status = provider.connection_status()
    assert status["connected"] is True and status["needs_reauthorisation"] is False
    assert status["consent_expires_at"] == "2027-01-08T12:00:00+00:00" and status["days_remaining"] == 90
    assert status["consent_expiry_is_estimate"] is False

    provider, _, _ = make(rsa_keys, lambda r: reply(401, {"error": "EXPIRED_SESSION", "message": "gone"}))
    status = provider.connection_status()  # reports instead of raising
    assert status["connected"] is False and status["needs_reauthorisation"] is True
    assert status["status"] == "consent_expired"
    assert status["consent_expires_at"] == "2027-01-08T12:00:00+00:00"  # last known value from the store


def test_link_flow_persists_new_session(rsa_keys):
    import json

    def handler(request):
        if request.url.path == "/aspsps":
            assert request.url.params["country"] == "GB"
            return reply(
                200, {"aspsps": [{"name": "Nationwide", "country": "GB", "maximum_consent_validity": 7776000}]}
            )
        if request.url.path == "/auth":
            body = json.loads(request.content)
            assert body["aspsp"] == {"name": "Nationwide", "country": "GB"} and body["psu_type"] == "personal"
            assert body["access"]["balances"] is True and "payments" not in json.dumps(body)
            return reply(200, {"url": "https://bank.example/authorise", "authorization_id": "a1"})
        if request.url.path == "/sessions":
            assert json.loads(request.content) == {"code": "the-code"}
            return reply(
                200,
                {
                    "session_id": "sess-new",
                    "access": {"valid_until": "2027-04-01T00:00:00+00:00"},
                    "aspsp": {"name": "Nationwide", "country": "GB"},
                    "accounts": [
                        {
                            "uid": "u1",
                            "identification_hash": "h1",
                            "name": "FlexDirect",
                            "currency": "GBP",
                            "cash_account_type": "CACC",
                            "account_id": {"other": {"identification": "00000012345678"}},
                        }
                    ],
                },
            )
        return reply(404, {})

    provider, _, store = make(rsa_keys, handler, store=MemoryTokenStore(STORED))
    assert provider.list_aspsps("GB")[0]["name"] == "Nationwide"
    auth = provider.start_authorisation(
        "Nationwide", "GB", "https://localhost/cb", datetime(2027, 4, 1, tzinfo=UTC), "state-1"
    )
    assert auth["url"].startswith("https://")
    record = provider.create_session("the-code")
    saved = store.get(STORE_SECTION)
    assert saved["session_id"] == "sess-new" == record["session_id"]  # old session replaced
    assert saved["accounts"][0]["mask"] == "5678"
    assert "00000012345678" not in json.dumps(saved)  # full account number is not persisted
