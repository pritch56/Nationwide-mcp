"""MCP-level tests using the SDK's in-process client and a stub provider."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from importlib.metadata import version

import anyio
import httpx
import pytest
from mcp.client.client import Client

from nationwide_mcp import server
from nationwide_mcp.errors import (
    AuthError,
    ConfigError,
    ConsentExpiredError,
    NotConnectedError,
    RateLimitError,
    TokenStoreError,
)
from nationwide_mcp.models import LONDON, make_transaction, window_result
from nationwide_mcp.providers._http import USER_AGENT


class StubProvider:
    name = "stub"

    def __init__(self, fail: Exception | None = None, status_fail: Exception | None = None):
        self.fail = fail
        self.status_fail = status_fail
        self.calls: list[tuple] = []

    def _maybe_fail(self):
        if self.fail:
            raise self.fail

    def list_accounts(self):
        self._maybe_fail()
        return [
            {
                "id": "acc-1",
                "source": "nationwide",
                "name": "FlexDirect",
                "type": "current",
                "currency": "GBP",
                "institution": "Nationwide Building Society",
                "mask": "5678",
            }
        ]

    def get_balance(self, account_id):
        self._maybe_fail()
        return {
            "account_id": account_id,
            "source": "nationwide",
            "currency": "GBP",
            "current_minor": 123456,
            "available_minor": None,
            "as_of": None,
            "balances": [],
        }

    def fetch_transactions(self, account_id, since, before, include_pending=False):
        self._maybe_fail()
        self.calls.append((account_id, since, before, include_pending))
        txns = [
            make_transaction(
                account_id=account_id,
                day=since,
                amount_minor=-1234,
                currency="GBP",
                description="TESCO",
                provider_ref="r1",
            ),
            make_transaction(
                account_id=account_id,
                day=since + timedelta(days=1),
                amount_minor=-450,
                currency="GBP",
                description="PRET",
                pending=True,
            ),
        ]
        return window_result(txns, since, before, include_pending)

    def connection_status(self):
        if self.status_fail:
            raise self.status_fail
        return {
            "provider": "stub",
            "connected": True,
            "needs_reauthorisation": False,
            "consent_expires_at": "2027-01-08",
            "days_remaining": 90,
        }


@pytest.fixture
def stub():
    provider = StubProvider()
    server.set_provider(provider)
    yield provider
    server.set_provider(None)


def run(coro_fn):
    return anyio.run(coro_fn)


def call(tool: str, args: dict | None = None):
    async def go():
        async with Client(server.create_server()) as client:
            return await client.call_tool(tool, args or {})

    return run(go)


def test_only_read_only_tools_are_exposed_and_server_info_has_the_package_version(stub):
    async def go():
        async with Client(server.create_server()) as client:
            return (await client.list_tools()).tools, client.server_info

    tools, info = run(go)
    assert sorted(t.name for t in tools) == [
        "get_balance",
        "get_connection_status",
        "list_accounts",
        "list_transactions",
    ]
    assert all(t.annotations.read_only_hint is True and t.annotations.destructive_hint is False for t in tools)
    forbidden = ("pay", "transfer", "send", "initiate", "create", "delete", "update")
    assert not any(word in t.name for t in tools for word in forbidden)
    assert info.version == version("nationwide-mcp")
    assert USER_AGENT == f"nationwide-mcp/{version('nationwide-mcp')}"  # one User-Agent everywhere


def test_tool_calls_return_structured_data(stub):
    async def go():
        async with Client(server.create_server()) as client:
            accounts = await client.call_tool("list_accounts", {})
            balance = await client.call_tool("get_balance", {"account_id": "acc-1"})
            txns = await client.call_tool(
                "list_transactions", {"account_id": "acc-1", "since": "2026-10-03", "before": "2026-10-10"}
            )
            status = await client.call_tool("get_connection_status", {})
            return accounts, balance, txns, status

    accounts, balance, txns, status = run(go)
    account = accounts.structured_content["accounts"][0]
    assert not accounts.is_error and account["id"] == "acc-1" and account["name"] == "FlexDirect"
    # balance_minor is the primary field; current_minor is kept as a duplicate
    assert balance.structured_content["balance_minor"] == 123456 == balance.structured_content["current_minor"]
    body = txns.structured_content
    assert body["since"] == "2026-10-03" and body["before"] == "2026-10-10" and body["count"] == 1
    # the pending item was left out, and the result says so
    assert body["pending_excluded"] == 1 and body["earliest_pending_date"] == "2026-10-04"
    assert body["pending_supported"] is True
    txn = body["transactions"][0]
    assert set(txn) == {
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
    assert txn["amount_minor"] == -1234 and txn["source"] == "nationwide" and txn["notes"] is None
    assert stub.calls == [("acc-1", date(2026, 10, 3), date(2026, 10, 10), False)]  # pending excluded by default
    body = status.structured_content
    assert body["connected"] is True and body["consent_expires_at"] == "2027-01-08T00:00:00Z"
    assert body["days_remaining"] == 90 and body["warning"] is None


def test_default_window_is_thirty_days_ending_tomorrow_in_london(stub):
    result = call("list_transactions", {"account_id": "acc-1", "include_pending": True})
    tomorrow = datetime.now(LONDON).date() + timedelta(days=1)
    _, since, before, pending = stub.calls[0]
    assert before in (tomorrow, tomorrow + timedelta(days=1)) and before - since == timedelta(days=30)
    assert pending is True and not result.is_error
    body = result.structured_content
    assert body["since"] == since.isoformat() and body["before"] == before.isoformat()  # effective window echoed
    assert body["count"] == 2 and body["pending_excluded"] == 0


def test_default_before_uses_the_london_date_not_utc():
    # 23:30 UTC on 30 June is already 1 July in London (BST), so "tomorrow" is 2 July.
    late = datetime(2026, 6, 30, 23, 30, tzinfo=UTC).astimezone(LONDON)
    assert late.date() == date(2026, 7, 1)
    assert server.resolve_window(None, None, today=late.date()) == (date(2026, 6, 2), date(2026, 7, 2))


def test_range_longer_than_the_maximum_is_refused_before_any_request(stub, monkeypatch):
    result = call("list_transactions", {"account_id": "acc-1", "since": "1970-01-01", "before": "2026-10-10"})
    assert result.is_error and "[invalid_request]" in result.content[0].text and "400" in result.content[0].text
    ok = call("list_transactions", {"account_id": "acc-1", "since": "2025-09-05", "before": "2026-10-10"})  # 400 days
    assert not ok.is_error
    monkeypatch.setenv("NATIONWIDE_MAX_RANGE_DAYS", "7")
    result = call("list_transactions", {"account_id": "acc-1", "since": "2026-10-01", "before": "2026-10-10"})
    assert result.is_error and "[invalid_request]" in result.content[0].text
    monkeypatch.setenv("NATIONWIDE_MAX_RANGE_DAYS", "lots")
    result = call("list_transactions", {"account_id": "acc-1", "since": "2026-10-01", "before": "2026-10-10"})
    assert result.is_error and "[config_error]" in result.content[0].text
    assert len(stub.calls) == 1  # only the permitted call reached the provider


@pytest.mark.parametrize(
    "error,expected",
    [
        (ConsentExpiredError("Consent expired; re-run link."), "[consent_expired] Consent expired"),
        (AuthError("API key rejected."), "[token_expired] API key rejected."),
        (
            RateLimitError("Slow down.", retry_after_seconds=21600),
            "[rate_limited] Slow down. (retry_after_seconds=21600)",
        ),
        (NotConnectedError("Not linked yet."), "[not_connected] Not linked yet."),
        (TokenStoreError("Disk full."), "[token_store_error] Disk full."),
        (RuntimeError("secret-internal-detail"), "[internal_error] Unexpected RuntimeError"),
        (ValueError("secret-internal-detail"), "[internal_error] Unexpected ValueError"),
    ],
)
@pytest.mark.parametrize("tool", ["get_balance", "list_accounts", "list_transactions"])
def test_errors_reach_the_client_with_a_code(error, expected, tool):
    server.set_provider(StubProvider(fail=error))
    try:
        result = call(tool, {} if tool == "list_accounts" else {"account_id": "acc-1"})
    finally:
        server.set_provider(None)
    text = result.content[0].text
    assert result.is_error and expected in text
    assert "secret-internal-detail" not in text


def test_bad_arguments_are_rejected(stub):
    bad_date = call("list_transactions", {"account_id": "a", "since": "last week"})
    inverted = call("list_transactions", {"account_id": "a", "since": "2026-10-10", "before": "2026-10-01"})
    empty = call("list_transactions", {"account_id": "a", "since": "2026-10-10", "before": "2026-10-10"})
    assert bad_date.is_error and "[invalid_request]" in bad_date.content[0].text
    assert inverted.is_error and "must be earlier" in inverted.content[0].text
    assert empty.is_error and "[invalid_request]" in empty.content[0].text
    assert stub.calls == []


def test_missing_configuration_is_reported_per_call(monkeypatch):
    server.set_provider(None)
    monkeypatch.setenv("NATIONWIDE_PROVIDER", "lunchflow")
    monkeypatch.delenv("LUNCHFLOW_API_KEY", raising=False)
    result = call("list_accounts")
    assert (
        result.is_error and "[config_error]" in result.content[0].text and "LUNCHFLOW_API_KEY" in result.content[0].text
    )


def test_connection_status_never_raises_when_unconfigured(monkeypatch):
    server.set_provider(None)
    monkeypatch.setenv("NATIONWIDE_PROVIDER", "truelayer")
    monkeypatch.delenv("TRUELAYER_CLIENT_ID", raising=False)
    monkeypatch.delenv("TRUELAYER_CLIENT_SECRET", raising=False)
    result = call("get_connection_status")
    body = result.structured_content
    assert not result.is_error and body["connected"] is False
    assert body["warning"].startswith("[config_error] ") and "TRUELAYER_CLIENT_ID" in body["warning"]
    assert body["consent_expires_at"] is None and body["days_remaining"] is None


@pytest.mark.parametrize(
    "error,prefix",
    [
        (RateLimitError("Slow down."), "[rate_limited] Slow down."),
        (ConsentExpiredError("Gone."), "[consent_expired] Gone."),
        (TokenStoreError("Disk full."), "[token_store_error] Disk full."),
        (RuntimeError("secret-internal-detail"), "[internal_error] Unexpected RuntimeError"),
    ],
)
def test_connection_status_reports_instead_of_raising(error, prefix):
    server.set_provider(StubProvider(status_fail=error))
    try:
        result = call("get_connection_status")
    finally:
        server.set_provider(None)
    body = result.structured_content
    assert not result.is_error and body["connected"] is False and body["warning"].startswith(prefix)
    assert "secret-internal-detail" not in body["warning"]


# ------------------------------------------------------------------ HTTP transport


async def _inner(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _statuses(app, requests):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return [
                (await client.request(method, path, headers=headers)).status_code for method, path, headers in requests
            ]

    return run(go)


def test_bearer_auth_middleware():
    app = server.BearerAuthMiddleware(_inner, token="  s3cret\n")  # configured token is stripped
    assert _statuses(
        app,
        [
            ("POST", "/mcp", {}),
            ("POST", "/mcp", {"Authorization": "Bearer wrong"}),
            ("POST", "/mcp", {"Authorization": "Bearer s3cret"}),
            ("POST", "/mcp", {"Authorization": "bearer s3cret"}),  # scheme is case-insensitive
            ("POST", "/mcp", {"Authorization": "BEARER   s3cret  "}),  # surrounding whitespace ignored
            ("POST", "/mcp", {"Authorization": "Basic s3cret"}),
            ("POST", "/mcp", {"Authorization": "s3cret"}),
            ("POST", "/mcp", {"Authorization": "Bearer s3cre"}),
            ("GET", "/healthz", {}),
        ],
    ) == [401, 401, 200, 200, 200, 401, 401, 401, 200]
    with pytest.raises(ConfigError):
        server.BearerAuthMiddleware(_inner, token="   ")


def test_app_builder_itself_refuses_a_public_bind_without_a_token():
    mcp = server.create_server()
    for host in ("0.0.0.0", "192.168.1.20", "::", "example.org"):
        with pytest.raises(ConfigError) as info:
            server.build_http_app(mcp, host, None)
        assert (
            "NATIONWIDE_MCP_AUTH_TOKEN" in info.value.message and "NATIONWIDE_MCP_ALLOW_NO_AUTH" in info.value.message
        )
        with pytest.raises(ConfigError):
            server.build_http_app(mcp, host, "   ")  # a blank token is no token
    # the documented override, a token, or a loopback bind all build
    assert server.build_http_app(server.create_server(), "0.0.0.0", None, allow_no_auth=True) is not None
    assert server.build_http_app(server.create_server(), "0.0.0.0", "tok") is not None
    for host in ("127.0.0.1", "localhost", "::1", "127.0.0.5"):
        assert server.build_http_app(server.create_server(), host, None) is not None
    with pytest.raises(ConfigError):
        server.build_http_app(server.create_server(), "127.0.0.1", None, path="/healthz")
    with pytest.raises(ConfigError):
        server.build_http_app(server.create_server(), "127.0.0.1", None, path="mcp")


def test_http_app_health_probe_reveals_nothing_and_custom_path_is_served():
    app = server.build_http_app(server.create_server(), "127.0.0.1", "tok", path="/bank/mcp")

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as c:
            health = await c.get("/healthz")
            return (
                health.status_code,
                health.json(),
                (await c.post("/bank/mcp", json={})).status_code,
                (await c.post("/healthz")).status_code,
            )

    status, body, unauthenticated, wrong_method = run(go)
    assert (status, body) == (200, {"ok": True})  # liveness only: no version, provider or account data
    assert unauthenticated == 401 and wrong_method in (401, 405)


def test_main_refuses_public_bind_without_token_and_uses_prefixed_env_names(monkeypatch, capsys):
    for name in ("NATIONWIDE_MCP_AUTH_TOKEN", "NATIONWIDE_MCP_ALLOW_NO_AUTH"):
        monkeypatch.delenv(name, raising=False)
    # The old generic names must be ignored: a host tool that exports them must not reconfigure this server.
    monkeypatch.setenv("MCP_AUTH_TOKEN", "generic-token")
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "1")
    assert server.main(["--transport", "http", "--host", "0.0.0.0", "--port", "1"]) == 2
    assert "NATIONWIDE_MCP_AUTH_TOKEN" in capsys.readouterr().err

    seen = {}

    def fake_run(app, host, port, log_level):
        seen.update(host=host, port=port)

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setenv("NATIONWIDE_MCP_TRANSPORT", "http")
    monkeypatch.setenv("NATIONWIDE_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("NATIONWIDE_MCP_PORT", "8123")
    monkeypatch.setenv("NATIONWIDE_MCP_PATH", "/x")
    monkeypatch.setenv("NATIONWIDE_MCP_ALLOWED_HOSTS", "bank.example.org")
    monkeypatch.setenv("NATIONWIDE_MCP_LOG_LEVEL", "nonsense")
    assert server.main([]) == 0 and seen == {"host": "127.0.0.1", "port": 8123}  # PORT=1 and MCP_HOST ignored
    monkeypatch.setenv("NATIONWIDE_MCP_PORT", "eighty")
    assert server.main([]) == 2


@pytest.mark.parametrize(
    "raw,connected,days,warns",
    [
        ({"connected": True, "consent_expires_at": "2027-01-08", "days_remaining": 90}, True, 90, False),
        ({"connected": True, "consent_expires_at": "2026-10-23", "days_remaining": 13}, True, 13, True),
        ({"connected": True, "consent_expires_at": "2026-10-24", "days_remaining": 14}, True, 14, False),
        ({"connected": True}, True, None, False),
        ({"connected": False, "needs_reauthorisation": True, "message": "Consent expired."}, False, None, True),
    ],
)
def test_connection_status_contract(raw, connected, days, warns):
    out = server.normalise_status(raw)
    assert {"connected", "consent_expires_at", "days_remaining", "warning"} <= set(out)
    assert out["connected"] is connected and out["days_remaining"] == days
    assert (out["warning"] is not None) is warns
    if warns and connected:
        assert "13 day(s)" in out["warning"] and "2026-10-23" in out["warning"]
    if not connected:
        assert out["warning"] == "[consent_expired] Consent expired."


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2027-01-08T12:00:00+00:00", "2027-01-08T12:00:00Z"),
        ("2027-01-08T13:00:00+01:00", "2027-01-08T12:00:00Z"),
        ("2027-01-08T12:00:00Z", "2027-01-08T12:00:00Z"),
        ("2027-01-08", "2027-01-08T00:00:00Z"),
        ("soon", None),
        (None, None),
    ],
)
def test_consent_expiry_is_always_a_utc_timestamp_ending_in_z(value, expected):
    assert server.normalise_status({"connected": True, "consent_expires_at": value})["consent_expires_at"] == expected


def test_token_store_problem_is_surfaced_in_the_status_warning():
    out = server.normalise_status(
        {
            "connected": True,
            "days_remaining": 80,
            "token_store_error": "[token_store_error] Token file could not be written.",
        }
    )
    assert out["connected"] is True and out["warning"].startswith("[token_store_error] ")
    assert "token_store_error" not in out
