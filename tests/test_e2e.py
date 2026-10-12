"""End-to-end: launch the real server process on each transport and talk MCP to it, with
TrueLayer (token endpoint + Data API) replaced by a local fake HTTP server.

The server is started with ``sys.executable -m nationwide_mcp``, so these tests depend neither on
the folder the project lives in nor on any launcher being on PATH."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import anyio
import httpx
import pytest
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

PROJECT = Path(__file__).resolve().parents[1]
CLIENT_ID, CLIENT_SECRET, SEED_REFRESH = "e2e-client", "e2e-secret", "e2e-refresh-1"
AUTH_TOKEN = "e2e-bearer-token"


class FakeTrueLayer(BaseHTTPRequestHandler):
    refresh_count = 0
    valid_refresh = {SEED_REFRESH}
    lock = threading.Lock()

    def log_message(self, *args):  # keep test output quiet
        pass

    def _send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if urlparse(self.path).path != "/connect/token":
            return self._send(404, {"error": "not_found"})
        if body.get("client_id") != CLIENT_ID or body.get("client_secret") != CLIENT_SECRET:
            return self._send(401, {"error": "invalid_client"})
        with FakeTrueLayer.lock:
            if body.get("refresh_token") not in FakeTrueLayer.valid_refresh:
                return self._send(400, {"error": "invalid_grant"})
            FakeTrueLayer.refresh_count += 1
            rotated = f"e2e-refresh-rotated-{FakeTrueLayer.refresh_count}"
            # The seed stays valid so each test process can bootstrap; rotated ones accumulate.
            FakeTrueLayer.valid_refresh.add(rotated)
        return self._send(200, {"access_token": "e2e-access", "expires_in": 3600, "refresh_token": rotated})

    def do_GET(self):
        path = urlparse(self.path).path
        if self.headers.get("Authorization") != "Bearer e2e-access":
            return self._send(401, {"error": "invalid_token"})
        results = {
            "/data/v1/accounts": [
                {
                    "account_id": "acc1",
                    "account_type": "TRANSACTION",
                    "display_name": "FlexDirect",
                    "currency": "GBP",
                    "account_number": {"number": "12345678"},
                }
            ],
            "/data/v1/cards": [
                {
                    "account_id": "card1",
                    "card_type": "CREDIT",
                    "display_name": "Member Credit Card",
                    "currency": "GBP",
                    "partial_card_number": "4321",
                }
            ],
            "/data/v1/me": [
                {
                    "consent_status": "AUTHORISED",
                    "consent_expires_at": "2099-01-01T00:00:00Z",
                    "provider": {"provider_id": "ob-nationwide"},
                }
            ],
            "/data/v1/accounts/acc1/balance": [{"currency": "GBP", "current": 1234.56, "available": 1734.56}],
            "/data/v1/cards/card1/balance": [{"currency": "GBP", "current": 120.0, "available": 3880.0}],
            "/data/v1/accounts/acc1/transactions": [
                {
                    "transaction_id": "x",
                    "normalised_provider_transaction_id": "n1",
                    "timestamp": "2026-10-05T00:00:00Z",
                    "description": "TESCO STORES 1234",
                    "amount": -12.34,
                    "currency": "GBP",
                    "transaction_type": "DEBIT",
                    "merchant_name": "Tesco",
                }
            ],
            "/data/v1/accounts/acc1/transactions/pending": [
                {
                    "transaction_id": "y",
                    "timestamp": "2026-10-07T00:00:00Z",
                    "description": "PRET",
                    "amount": -4.5,
                    "currency": "GBP",
                    "transaction_type": "DEBIT",
                }
            ],
            "/data/v1/cards/card1/transactions": [
                {
                    "transaction_id": "z",
                    "normalised_provider_transaction_id": "c1",
                    "timestamp": "2026-10-04T00:00:00Z",
                    "description": "AMAZON UK",
                    "amount": 20.0,
                    "currency": "GBP",
                    "transaction_type": "DEBIT",
                }
            ],
            "/data/v1/cards/card1/transactions/pending": [],
        }
        if path in results:
            return self._send(200, {"results": results[path]})
        return self._send(404, {"error": "account_not_found"})


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def backend():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeTrueLayer)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def server_env(backend: str, token_file: Path, **extra: str) -> dict[str, str]:
    prefixes = ("LUNCHFLOW_", "ENABLEBANKING_", "NATIONWIDE_", "TRUELAYER_", "MCP_")
    env = {k: v for k, v in os.environ.items() if not k.startswith(prefixes) and k not in ("PORT", "LOG_LEVEL")}
    env.update(
        {
            # NATIONWIDE_PROVIDER deliberately unset: truelayer must be the default.
            "TRUELAYER_CLIENT_ID": CLIENT_ID,
            "TRUELAYER_CLIENT_SECRET": CLIENT_SECRET,
            "NATIONWIDE_TRUELAYER_REFRESH_TOKEN": SEED_REFRESH,
            "TRUELAYER_AUTH_BASE": backend,
            "TRUELAYER_API_BASE": backend,
            "NATIONWIDE_TOKEN_FILE": str(token_file),
            **extra,
        }
    )
    return env


async def exercise(client: Client) -> dict:
    tools = sorted(t.name for t in (await client.list_tools()).tools)
    accounts = await client.call_tool("list_accounts", {})
    window = {"since": "2026-10-01", "before": "2026-10-08"}
    return {
        "tools": tools,
        "accounts": accounts,
        "balance": await client.call_tool("get_balance", {"account_id": "acc1"}),
        "card_balance": await client.call_tool("get_balance", {"account_id": "card1"}),
        "txns": await client.call_tool("list_transactions", {"account_id": "acc1", **window}),
        "with_pending": await client.call_tool(
            "list_transactions", {"account_id": "acc1", "include_pending": True, **window}
        ),
        "card_txns": await client.call_tool("list_transactions", {"account_id": "card1", **window}),
        "status": await client.call_tool("get_connection_status", {}),
        "missing": await client.call_tool("get_balance", {"account_id": "nope"}),
    }


def check(result: dict, token_file: Path) -> None:
    assert result["tools"] == ["get_balance", "get_connection_status", "list_accounts", "list_transactions"]
    accounts = result["accounts"].structured_content["accounts"]
    assert [(a["id"], a["type"]) for a in accounts] == [("acc1", "current"), ("card1", "credit_card")]
    assert all(a["id"] and a["name"] for a in accounts)
    assert result["balance"].structured_content["balance_minor"] == 123456
    assert result["balance"].structured_content["current_minor"] == 123456  # duplicate kept for compatibility
    assert result["card_balance"].structured_content["balance_minor"] == -12000  # negative = money owed

    txns = result["txns"].structured_content["transactions"]
    assert [(t["amount_minor"], t["pending"], t["date"]) for t in txns] == [(-1234, False, "2026-10-05")]
    body = result["txns"].structured_content
    assert (body["since"], body["before"]) == ("2026-10-01", "2026-10-08")
    assert body["pending_excluded"] == 1 and body["earliest_pending_date"] == "2026-10-07"
    assert body["pending_supported"] is True
    with_pending = result["with_pending"].structured_content["transactions"]
    assert [(t["amount_minor"], t["pending"]) for t in with_pending] == [(-1234, False), (-450, True)]
    card = result["card_txns"].structured_content["transactions"]
    assert [(t["amount_minor"], t["description"]) for t in card] == [(-2000, "AMAZON UK")]
    assert all(t["source"] == "nationwide" and t["id"].startswith("nationwide_") for t in txns + card)

    status = result["status"].structured_content
    assert status["connected"] is True and status["warning"] is None
    assert status["consent_expires_at"] == "2099-01-01T00:00:00Z" and isinstance(status["days_remaining"], int)
    assert result["missing"].is_error and "[account_not_found]" in result["missing"].content[0].text

    # The real process persisted the ROTATED refresh token to the Nationwide token file.
    saved = json.loads(token_file.read_text(encoding="utf-8"))["truelayer"]
    assert saved["refresh_token"].startswith("e2e-refresh-rotated-") and saved["access_token"] == "e2e-access"


def test_stdio_transport(backend, tmp_path):
    """Launch the way an MCP client does, from an unrelated working directory."""
    token_file = tmp_path / "tokens.json"
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "nationwide_mcp"], env=server_env(backend, token_file), cwd=str(tmp_path)
    )

    async def go():
        async with Client(params) as client:
            return await exercise(client)

    check(anyio.run(go), token_file)


def test_stdio_transport_reports_dead_consent(backend, tmp_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "nationwide_mcp"],
        env=server_env(backend, tmp_path / "tokens.json", NATIONWIDE_TRUELAYER_REFRESH_TOKEN="revoked-token"),
    )

    async def go():
        async with Client(params) as client:
            return (await client.call_tool("list_accounts", {}), await client.call_tool("get_connection_status", {}))

    failed, status = anyio.run(go)
    text = failed.content[0].text
    assert failed.is_error and "[consent_expired]" in text and "revoked-token" not in text
    body = status.structured_content  # reported, not raised
    assert body["connected"] is False and body["days_remaining"] is None
    assert body["warning"].startswith("[consent_expired] ") and "revoked-token" not in body["warning"]


def test_stdio_transport_reports_not_connected(backend, tmp_path):
    env = server_env(backend, tmp_path / "tokens.json")
    del env["NATIONWIDE_TRUELAYER_REFRESH_TOKEN"]
    env["TRUELAYER_REFRESH_TOKEN"] = SEED_REFRESH  # the old generic name is no longer read
    params = StdioServerParameters(command=sys.executable, args=["-m", "nationwide_mcp"], env=env)

    async def go():
        async with Client(params) as client:
            return await client.call_tool("list_accounts", {}), await client.call_tool("get_connection_status", {})

    result, status = anyio.run(go)
    assert result.is_error and "[not_connected]" in result.content[0].text
    assert status.structured_content["connected"] is False
    assert status.structured_content["warning"].startswith("[not_connected] ")


def test_stdio_transport_unconfigured_status_is_not_an_error(backend, tmp_path):
    env = server_env(backend, tmp_path / "tokens.json")
    del env["TRUELAYER_CLIENT_ID"]
    params = StdioServerParameters(command=sys.executable, args=["-m", "nationwide_mcp"], env=env)

    async def go():
        async with Client(params) as client:
            return await client.call_tool("get_connection_status", {}), await client.call_tool("list_accounts", {})

    status, accounts = anyio.run(go)
    assert not status.is_error and status.structured_content["connected"] is False
    assert status.structured_content["warning"].startswith("[config_error] ")
    assert accounts.is_error and "[config_error]" in accounts.content[0].text


def test_range_cap_is_enforced_before_any_upstream_request(backend, tmp_path):
    token_file = tmp_path / "tokens.json"
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "nationwide_mcp"], env=server_env(backend, token_file)
    )

    async def go():
        async with Client(params) as client:
            return await client.call_tool(
                "list_transactions", {"account_id": "acc1", "since": "1970-01-01", "before": "2026-10-08"}
            )

    result = anyio.run(go)
    assert result.is_error and "[invalid_request]" in result.content[0].text
    assert not token_file.exists()  # not even a token refresh was attempted


def test_streamable_http_transport(backend, tmp_path):
    token_file = tmp_path / "tokens.json"
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "nationwide_mcp", "--transport", "http", "--port", str(port)],
        env=server_env(backend, token_file, NATIONWIDE_MCP_AUTH_TOKEN=AUTH_TOKEN),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while True:
            try:
                health = httpx.get(f"{base}/healthz", timeout=1)
                if health.status_code == 200:
                    assert health.json() == {"ok": True}  # liveness only
                    break
            except httpx.HTTPError:
                pass
            if proc.poll() is not None or time.time() > deadline:
                raise AssertionError(f"server did not start: {proc.stderr.read().decode(errors='replace')[-2000:]}")
            time.sleep(0.2)

        assert httpx.post(f"{base}/mcp", json={}, timeout=5).status_code == 401  # no bearer token
        wrong = {"Authorization": "Bearer not-the-token"}
        assert httpx.post(f"{base}/mcp", json={}, headers=wrong, timeout=5).status_code == 401

        async def go():
            async with create_mcp_http_client(headers={"Authorization": f"Bearer {AUTH_TOKEN}"}) as http_client:
                transport = streamable_http_client(f"{base}/mcp", http_client=http_client)
                async with Client(transport) as client:
                    return await exercise(client)

        check(anyio.run(go), token_file)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
