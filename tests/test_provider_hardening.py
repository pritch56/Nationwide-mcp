"""Statement import, Lunch Flow and Enable Banking: the release-review findings."""

from __future__ import annotations

import os
import time
from datetime import date

import httpx
import pytest

from conftest import Recorder, reply
from nationwide_mcp.errors import (
    AuthError,
    ConfigError,
    InvalidRequestError,
    MalformedImportError,
    ProviderError,
    RateLimitError,
)
from nationwide_mcp.providers import build_provider
from nationwide_mcp.providers.statement import StatementProvider, _money
from test_enablebanking import PAGE_1, SESSION
from test_enablebanking import happy as eb_happy
from test_enablebanking import make as eb_make
from test_lunchflow import ACCOUNTS, TXNS
from test_lunchflow import happy as lf_happy
from test_lunchflow import make as lf_make
from test_statement import CSV_WEEK_1, OFX

POUND = "£"
WINDOW = (date(2026, 10, 1), date(2026, 10, 11))

# ------------------------------------------------------------------ statement


@pytest.mark.parametrize(
    "text,expected",
    [
        (f"{POUND}12.34", 1234),
        (f"{POUND}-12.34", -1234),  # the sign after the currency symbol used to be lost
        (f"-{POUND}12.34", -1234),
        (f"- {POUND} 1,234.50", -123450),
        ("(12.34)", -1234),
        (f"({POUND}12.34)", -1234),
        ("12.34 DR", -1234),
        ("12.34DR", -1234),
        (f"{POUND}12.34 CR", 1234),
        ("12.34-", -1234),
        ("+12.34", 1234),
        ("0.00", 0),
        ("", None),
        ("n/a", None),
        ("DR", None),
    ],
)
def test_statement_amount_signs(text, expected):
    assert _money(text) == expected


def test_overdrawn_balance_and_signed_amount_column_keep_their_sign(tmp_path):
    (tmp_path / "overdrawn.csv").write_text(
        f'"Account Name:","FlexDirect ****5678"\n"Account Balance:","{POUND}-12.34"\n'
        f'"Available Balance:","-{POUND}2.00"\n\n'
        '"Date","Description","Amount","Balance"\n'
        f'"05 Oct 2026","CARD PAYMENT","{POUND}-5.00","{POUND}-7.34"\n'
        f'"06 Oct 2026","REFUND","{POUND}5.00 CR","{POUND}2.34 DR"\n'
        f'"07 Oct 2026","FEE","(1.50)","{POUND}-3.84"\n',
        encoding="utf-8",
    )
    provider = StatementProvider(tmp_path)
    account_id = provider.list_accounts()[0]["id"]
    balance = provider.get_balance(account_id)
    assert balance["current_minor"] == -1234 and balance["available_minor"] == -200
    txns = provider.list_transactions(account_id, *WINDOW)
    assert [(t["description"], t["amount_minor"]) for t in txns] == [
        ("CARD PAYMENT", -500),
        ("REFUND", 500),
        ("FEE", -150),
    ]
    result = provider.fetch_transactions(account_id, *WINDOW)
    assert result["pending_supported"] is False and result["pending_excluded"] == 0  # exports hold booked items only


def test_oversized_statement_file_is_refused(tmp_path):
    (tmp_path / "big.csv").write_bytes(CSV_WEEK_1.encode("cp1252") + b"x" * 5000)
    provider = StatementProvider(tmp_path, max_bytes=4096)
    with pytest.raises(MalformedImportError) as info:
        provider.list_accounts()
    assert "big.csv" in info.value.message and "NATIONWIDE_STATEMENT_MAX_BYTES" in info.value.message
    assert provider.parse_count == 0  # refused on size alone, never read or parsed
    assert provider.connection_status()["status"] == "malformed_import_file"

    configured = build_provider(
        {
            "NATIONWIDE_PROVIDER": "statement",
            "NATIONWIDE_STATEMENT_DIR": str(tmp_path),
            "NATIONWIDE_STATEMENT_MAX_BYTES": "1000000",
        }
    )
    assert len(configured.list_accounts()) == 1
    with pytest.raises(ConfigError):
        build_provider(
            {
                "NATIONWIDE_PROVIDER": "statement",
                "NATIONWIDE_STATEMENT_DIR": str(tmp_path),
                "NATIONWIDE_STATEMENT_MAX_BYTES": "0",
            }
        )


def test_statement_files_are_parsed_once_and_reparsed_only_when_they_change(tmp_path):
    first = tmp_path / "flex.csv"
    first.write_bytes(CSV_WEEK_1.encode("cp1252"))
    (tmp_path / "savings.ofx").write_text(OFX, encoding="utf-8")
    provider = StatementProvider(tmp_path)
    accounts = provider.list_accounts()
    for account in accounts:
        provider.get_balance(account["id"])
        provider.list_transactions(account["id"], *WINDOW)
    provider.connection_status()
    assert provider.parse_count == 2  # one parse per file, however many calls

    changed = CSV_WEEK_1 + f'"09 Oct 2026","Visa purchase","NEW ROW","{POUND}1.00","","{POUND}1,443.00"\n'
    first.write_bytes(changed.encode("cp1252"))
    os.utime(first, (time.time() + 5, time.time() + 5))
    flex = next(a["id"] for a in accounts if a["type"] == "current")
    assert "NEW ROW" in [t["description"] for t in provider.list_transactions(flex, *WINDOW)]
    assert provider.parse_count == 3  # only the changed file

    (tmp_path / "savings.ofx").unlink()
    assert len(provider.list_accounts()) == 1 and provider.parse_count == 3  # deleted files drop out of the cache


def test_symlink_pointing_outside_the_statement_directory_is_refused(tmp_path):
    inside = tmp_path / "statements"
    inside.mkdir()
    (inside / "flex.csv").write_bytes(CSV_WEEK_1.encode("cp1252"))
    secret = tmp_path / "elsewhere.csv"
    secret.write_text(
        '"Account Name:","Somebody Else ****0000"\n\n"Date","Description","Amount"\n"05 Oct 2026","PRIVATE","-1.00"\n',
        encoding="utf-8",
    )
    try:
        (inside / "link.csv").symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("this platform/user cannot create symlinks")
    provider = StatementProvider(inside)
    assert [a["name"] for a in provider.list_accounts()] == ["FlexDirect ****5678"]
    status = provider.connection_status()
    assert status["skipped_files"] == ["link.csv"] and len(status["accounts"]) == 1


@pytest.mark.parametrize("amount", ["1e999999999", "9" * 30, "NaN", "Infinity", "12.3.4"])
def test_absurd_amounts_are_a_malformed_file_not_a_crash(tmp_path, amount):
    (tmp_path / "bad.csv").write_text(
        f'"Account Name:","X ****1111"\n\n"Date","Description","Amount"\n"05 Oct 2026","ROW","{amount}"\n',
        encoding="utf-8",
    )
    (tmp_path / "bad.ofx").write_text(
        f"<OFX><ACCTID>12345678<STMTTRN><DTPOSTED>20261005<TRNAMT>{amount}<FITID>1<NAME>ROW</STMTTRN></OFX>",
        encoding="utf-8",
    )
    for name in ("bad.csv", "bad.ofx"):
        only = tmp_path / f"only-{name.replace('.', '-')}"
        only.mkdir()
        (only / name).write_bytes((tmp_path / name).read_bytes())
        provider = StatementProvider(only)
        try:
            txns = [t for a in provider.list_accounts() for t in provider.list_transactions(a["id"], *WINDOW)]
        except MalformedImportError as exc:
            assert str(exc).startswith("[malformed_import_file] ") and name in exc.message
        else:  # a cell that is not a number at all is skipped rather than guessed at
            assert txns == [] and name == "bad.csv" and amount in ("NaN", "Infinity")


HOSTILE_IDS = ["ledgerbal", "stmttrn", "angle-brackets", "nested-balances", "newlines"]


@pytest.mark.parametrize(
    "hostile",
    [
        "<LEDGERBAL>" * 150_000,
        "<STMTTRN>" * 150_000,
        "<STMTTRN><TRNAMT>" + "<" * 1_500_000,
        "<A>" + "<LEDGERBAL><BALAMT>" * 80_000 + "x",
        "<AVAILBAL>" + "\n" * 1_500_000,
    ],
    ids=HOSTILE_IDS,
)
def test_hostile_ofx_is_parsed_in_linear_time(tmp_path, hostile):
    path = tmp_path / "hostile.ofx"
    path.write_text("<OFX><ACCTID>12345678" + hostile, encoding="utf-8")
    assert path.stat().st_size > 1_000_000
    started = time.perf_counter()
    try:
        StatementProvider(tmp_path).list_accounts()
    except MalformedImportError:
        pass
    assert time.perf_counter() - started < 5  # a quadratic pattern needs minutes on input this size


def test_large_csv_is_fast_enough_and_second_call_is_free(tmp_path):
    row = f'"05 Oct 2026","Visa purchase","SHOP {{}}","{POUND}1.00","","{POUND}100.00"\n'
    body = "".join(row.format(i) for i in range(60_000))  # roughly 4 MiB
    (tmp_path / "big.csv").write_text(
        '"Account Name:","Flex ****1111"\n\n'
        '"Date","Transaction type","Description","Paid out","Paid in","Balance"\n' + body,
        encoding="utf-8",
    )
    provider = StatementProvider(tmp_path)
    started = time.perf_counter()
    account_id = provider.list_accounts()[0]["id"]
    first = time.perf_counter() - started
    started = time.perf_counter()
    provider.get_balance(account_id)
    second = time.perf_counter() - started
    assert first < 20 and second < first / 3 and provider.parse_count == 1


# ------------------------------------------------------------------ lunch flow


def test_lunchflow_truncated_responses_are_refused_not_returned_short():
    def truncated(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/transactions"):
            return reply(200, {"transactions": TXNS["transactions"][:2], "total": 250})
        return lf_happy(request)

    provider, _ = lf_make(truncated)
    with pytest.raises(ProviderError) as info:
        provider.list_transactions("11", date(2026, 10, 1), date(2026, 10, 8))
    assert info.value.code == "provider_error" and "2 of 250" in info.value.message

    provider, _ = lf_make(lambda r: reply(200, {"accounts": ACCOUNTS["accounts"][:1], "total": 40}))
    with pytest.raises(ProviderError):
        provider.list_accounts()
    provider, _ = lf_make(lf_happy)  # total == number returned: fine
    assert len(provider.list_transactions("11", date(2026, 10, 1), date(2026, 10, 8), include_pending=True)) == 3


def test_lunchflow_reports_excluded_pending_and_validates_ids():
    provider, rec = lf_make(lf_happy)
    result = provider.fetch_transactions("11", date(2026, 10, 1), date(2026, 10, 8))
    assert len(result["transactions"]) == 2 and result["pending_excluded"] == 1
    assert result["earliest_pending_date"] == "2026-10-07" and result["pending_supported"] is True
    before = len(rec.requests)
    for bad in ("../11", "11/balance", "١١", ""):
        with pytest.raises(InvalidRequestError):
            provider.get_balance(bad)
    assert len(rec.requests) == before  # rejected before any request


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>login</html>"),
        httpx.Response(200, content=b"[1, 2]"),
        reply(200, {"accounts": "none"}),
        reply(200, {"accounts": ["x"]}),
    ],
)
def test_lunchflow_malformed_success_bodies_are_provider_errors(response):
    provider, _ = lf_make(lambda r: response)
    with pytest.raises(ProviderError) as info:
        provider.list_accounts()
    assert info.value.code == "provider_error"
    assert provider.connection_status()["connected"] is False


def test_lunchflow_429_is_not_retried_and_bad_amounts_are_provider_errors():
    provider, rec = lf_make(lambda r: reply(429, {"message": "slow down"}))
    with pytest.raises(RateLimitError):
        provider.list_accounts()
    assert len(rec.requests) == 1

    def bad_amount(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/transactions"):
            return reply(200, {"transactions": [{"id": "t", "amount": "lots", "date": "2026-10-05"}]})
        return lf_happy(request)

    provider, _ = lf_make(bad_amount)
    with pytest.raises(ProviderError) as info:
        provider.list_transactions("11", date(2026, 10, 1), date(2026, 10, 8))
    assert info.value.code == "provider_error"


# -------------------------------------------------------------- enable banking


def _eb_with_transactions(items):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/transactions"):
            return reply(200, {"transactions": items})
        return eb_happy(request)

    return handler


def test_enablebanking_refuses_to_guess_the_direction_of_an_unsigned_amount(rsa_keys):
    unsigned = {
        "entry_reference": "U1",
        "transaction_amount": {"currency": "GBP", "amount": "12.34"},
        "status": "BOOK",
        "booking_date": "2026-10-05",
    }  # no credit_debit_indicator: used to become +12.34
    provider, _, _ = eb_make(rsa_keys, _eb_with_transactions([unsigned]))
    with pytest.raises(ProviderError) as info:
        provider.list_transactions("hash-current", date(2026, 10, 1), date(2026, 10, 8))
    assert info.value.code == "provider_error" and "credit_debit_indicator" in info.value.message

    signed = dict(unsigned, transaction_amount={"currency": "GBP", "amount": "-12.34"})
    provider, _, _ = eb_make(rsa_keys, _eb_with_transactions([signed, PAGE_1["transactions"][1]]))
    txns = provider.list_transactions("hash-current", date(2026, 10, 1), date(2026, 10, 8))
    assert [t["amount_minor"] for t in txns] == [-1234, 150000]  # explicit minus sign or indicator: accepted


def test_enablebanking_replays_a_401_for_get_only(rsa_keys):
    def refuse(request: httpx.Request) -> httpx.Response:
        return reply(401, {"error": "UNAUTHORIZED_ACCESS", "message": "token expired"})

    provider, rec, _ = eb_make(rsa_keys, refuse)
    with pytest.raises(AuthError):
        provider.create_session("one-time-code")
    with pytest.raises(AuthError):
        provider.start_authorisation(
            "Nationwide",
            "GB",
            "https://localhost/cb",
            __import__("datetime").datetime(2027, 1, 1, tzinfo=__import__("datetime").UTC),
            "state",
        )
    assert [(r.method, r.url.path) for r in rec.requests] == [("POST", "/sessions"), ("POST", "/auth")]  # once each

    with pytest.raises(AuthError):
        provider.list_aspsps("GB")
    assert [r.url.path for r in rec.requests].count("/aspsps") == 2  # an idempotent GET is still retried once


def test_enablebanking_pending_report_ids_and_malformed_data(rsa_keys):
    provider, _, _ = eb_make(rsa_keys)
    result = provider.fetch_transactions("hash-current", date(2026, 10, 1), date(2026, 10, 8))
    assert len(result["transactions"]) == 3 and result["pending_excluded"] == 1
    assert result["earliest_pending_date"] == "2026-10-07"

    hostile = {**SESSION, "accounts_data": [{"uid": "../../sessions", "identification_hash": "hash-current"}]}
    provider, rec, _ = eb_make(rsa_keys, lambda r: reply(200, hostile))
    with pytest.raises(ProviderError):
        provider.get_balance("hash-current")
    assert all("/accounts/" not in r.url.path for r in rec.requests)  # never interpolated into a path

    provider, _, _ = eb_make(rsa_keys, lambda r: httpx.Response(200, content=b"not json"))
    with pytest.raises(ProviderError) as info:
        provider.list_accounts()
    assert info.value.code == "provider_error"

    provider, _, _ = eb_make(rsa_keys, session_id="../etc")
    with pytest.raises(ConfigError):
        provider.list_accounts()

    def no_amount(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/balances"):
            return reply(200, {"balances": [{"balance_type": "CLBD", "balance_amount": {"currency": "GBP"}}]})
        return eb_happy(request)

    provider, _, _ = eb_make(rsa_keys, no_amount)
    with pytest.raises(ProviderError):  # a missing amount is not silently reported as 0.00
        provider.get_balance("hash-current")


def test_recorder_helper_is_used():
    assert Recorder(lambda r: reply(200)).paths() == []
