"""Link helper: extend endpoint, redirect listener, pasted URLs, PKCE, reset."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading
import time

import httpx
import pytest

from conftest import Recorder, reply
from nationwide_mcp import link
from nationwide_mcp.errors import AuthError, ConfigError, InvalidRequestError, ProviderError
from nationwide_mcp.providers.truelayer import STORE_SECTION, TrueLayerConfig
from nationwide_mcp.token_store import FileTokenStore, MemoryTokenStore
from test_truelayer import make

REDIRECT = "http://localhost:3000/callback"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --------------------------------------------------------------------- extend


def test_extend_posts_to_the_documented_path_and_stores_the_new_tokens():
    provider, _, store, _ = make()
    rec = Recorder(
        lambda r: reply(
            200,
            {
                "action_needed": "no_action_needed",
                "access_token": "new-at",
                "expires_in": 3600,
                "refresh_token": "new-rt",
                "token_type": "Bearer",
            },
        )
    )
    body = link.extend_connection(
        provider, REDIRECT, "Ada", "ada@example.org", client=httpx.Client(transport=rec.transport)
    )
    request = rec.requests[0]
    # https://docs.truelayer.com/reference/extendconnection : no /data/v1 prefix
    assert str(request.url) == "https://api.truelayer.com/connections/extend" and request.method == "POST"
    sent = json.loads(request.content)
    assert sent["user_has_reconfirmed_consent"] is True and sent["refresh_token"] == "rt-1"
    assert sent["user"] == {"name": "Ada", "email": "ada@example.org"} and sent["redirect_uri"] == REDIRECT
    assert body["action_needed"] == "no_action_needed"
    saved = store.get(STORE_SECTION)
    assert saved["refresh_token"] == "new-rt" and saved["access_token"] == "new-at" and saved["connected_at"]


@pytest.mark.parametrize(
    "status,body,error,needles",
    [
        (
            401,
            {
                "type": "https://docs.truelayer.com/errors#unauthenticated",
                "title": "Unauthenticated",
                "status": 401,
                "detail": "Invalid client credentials.",
                "trace_id": "abc123",
            },
            AuthError,
            ["Invalid client credentials", "abc123", "401"],
        ),
        (
            400,
            {
                "title": "Invalid Parameters",
                "status": 400,
                "trace_id": "t-9",
                "errors": {"user.email": ["must be a valid email"]},
            },
            InvalidRequestError,
            ["Invalid Parameters", "user.email", "must be a valid email"],
        ),
        (
            422,
            {
                "title": "Unprocessable Entity",
                "status": 422,
                "detail": "Connection cannot be extended yet.",
                "trace_id": "t-1",
            },
            InvalidRequestError,
            ["cannot be extended yet"],
        ),
        (404, {}, ProviderError, ["re-link"]),
    ],
)
def test_extend_reports_the_problem_json_instead_of_a_blanket_message(status, body, error, needles):
    provider, _, store, _ = make()
    transport = httpx.MockTransport(
        lambda r: httpx.Response(status, content=json.dumps(body), headers={"content-type": "application/problem+json"})
    )
    with pytest.raises(error) as info:
        link.extend_connection(provider, REDIRECT, "Ada", "ada@example.org", client=httpx.Client(transport=transport))
    for needle in needles:
        assert needle in info.value.message
    if status != 404:
        assert "has to be enabled for your client" not in info.value.message
    assert "rt-1" not in info.value.message  # the refresh token is never echoed
    assert store.get(STORE_SECTION) == {"refresh_token": "rt-1"}  # untouched


def test_extend_strips_control_characters_from_upstream_text():
    provider, _, _, _ = make()
    transport = httpx.MockTransport(lambda r: reply(400, {"title": "Bad\x1b[2J\x07 thing", "status": 400}))
    with pytest.raises(InvalidRequestError) as info:
        link.extend_connection(provider, REDIRECT, "Ada", "a@example.org", client=httpx.Client(transport=transport))
    assert (
        "\x1b" not in info.value.message and "\x07" not in info.value.message and "Bad[2J thing" in info.value.message
    )


# ------------------------------------------------------------------- redirect


def test_listener_ignores_wrong_state_and_keeps_waiting_for_the_real_redirect():
    port = free_port()
    uri = f"http://localhost:{port}/callback"
    outcome: dict = {}

    def waiter():
        try:
            outcome["code"] = link.wait_for_redirect(uri, "good-state", timeout=20)
        except Exception as exc:  # pragma: no cover - reported by the assertions below
            outcome["error"] = exc

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while True:
        try:
            first = httpx.get(f"{base}/callback?code=evil&state=wrong-state", timeout=2)
            break
        except httpx.HTTPError:
            if time.time() > deadline:
                raise
            time.sleep(0.05)
    assert first.status_code == 400  # refused ...
    assert httpx.get(f"{base}/callback?code=evil", timeout=2).status_code == 400  # ... as is a missing state
    assert httpx.get(f"{base}/callback?error=access_denied", timeout=2).status_code == 400
    assert httpx.get(f"{base}/other", timeout=2).status_code == 404
    assert thread.is_alive() and not outcome  # ... and the flow was NOT aborted
    good = httpx.get(f"{base}/callback?code=real-code&state=good-state", timeout=2)
    thread.join(timeout=10)
    assert good.status_code == 200 and outcome == {"code": "real-code"}


def test_listener_times_out_if_only_forged_requests_arrive():
    port = free_port()
    with pytest.raises(ConfigError) as info:
        link.wait_for_redirect(f"http://localhost:{port}/callback", "good-state", timeout=0.3)
    assert "Timed out" in info.value.message


def test_pasted_url_must_carry_this_sessions_state():
    assert link.extract_code("abc", "s") == "abc"
    assert link.extract_code("  abc\n", None) == "abc"  # a bare code needs no state
    assert link.extract_code("http://localhost:3000/callback?code=abc&state=s", "s") == "abc"
    assert link.extract_code("code=abc&state=s", "s") == "abc"
    for bad in (
        "http://localhost:3000/callback?code=abc",  # no state at all: previously accepted
        "http://localhost:3000/callback?code=abc&state=",
        "http://localhost:3000/callback?code=abc&state=other",
        "code=abc",
        "?code=abc",
        "",
    ):
        with pytest.raises(ConfigError):
            link.extract_code(bad, "s")
    with pytest.raises(ConfigError):  # a URL cannot be verified when no session state is known
        link.extract_code("http://localhost:3000/callback?code=abc&state=s", None)
    with pytest.raises(ConfigError) as info:
        link.extract_code("http://localhost:3000/callback?error=denied\x1b[31m&state=s", "s")
    assert "\x1b" not in info.value.message


# ----------------------------------------------------------------------- PKCE


def test_auth_link_carries_a_pkce_challenge_and_the_exchange_sends_the_verifier():
    verifier, challenge = link.pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected and 43 <= len(verifier) <= 128
    url = link.build_auth_url(TrueLayerConfig("cid", "secret"), REDIRECT, "state-1", code_challenge=challenge)
    query = dict(httpx.URL(url).params)
    assert query["code_challenge"] == challenge and query["code_challenge_method"] == "S256"
    assert verifier not in url and "secret" not in url

    provider, _, _, fake = make(store=MemoryTokenStore())
    provider.exchange_code("good-code", REDIRECT, verifier)
    assert fake.token_bodies[0]["code_verifier"] == verifier
    provider, _, _, fake = make(store=MemoryTokenStore())
    provider.exchange_code("good-code", REDIRECT)
    assert "code_verifier" not in fake.token_bodies[0]


def test_linking_fails_loudly_if_the_token_cannot_be_stored():
    class Broken(MemoryTokenStore):
        def save(self, data):
            raise OSError("read-only file system")

    provider, _, _, _ = make(store=Broken())
    with pytest.raises(ProviderError) as info:
        provider.exchange_code("good-code", REDIRECT)
    assert info.value.code == "token_store_error"


# ---------------------------------------------------------------------- reset


def test_reset_clears_the_store_and_relinking_overwrites_it(tmp_path, monkeypatch, capsys):
    path = tmp_path / "tokens.json"
    FileTokenStore(path).put(STORE_SECTION, {"refresh_token": "old"})
    monkeypatch.setenv("NATIONWIDE_TOKEN_STORE", "file")
    monkeypatch.setenv("NATIONWIDE_TOKEN_FILE", str(path))
    with pytest.raises(SystemExit) as info:
        link.main(["reset"])
    assert info.value.code == 0 and not path.exists()
    assert "cleared" in capsys.readouterr().out
    link.reset_store()  # clearing an already empty store is fine

    store = FileTokenStore(path)
    store.put(STORE_SECTION, {"refresh_token": "stale-from-last-time", "access_token": "x"})
    provider, _, _, _ = make(store=store)
    provider.exchange_code("good-code", REDIRECT)  # `link` replaces whatever was stored
    assert FileTokenStore(path).get(STORE_SECTION)["refresh_token"] == "rt-2"
