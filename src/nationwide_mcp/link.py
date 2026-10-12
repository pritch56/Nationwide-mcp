"""One-off helper that YOU run on your own machine to connect Nationwide. The
server never runs it. It never asks for your online-banking credentials: you
approve access on Nationwide's own site/app, as with any Open Banking app.

    nationwide-mcp-link check     # is the chosen route usable? (truelayer: no credentials needed)
    nationwide-mcp-link link      # run the consent flow and store the tokens
    nationwide-mcp-link status    # when does the consent expire?
    nationwide-mcp-link extend --name "..." --email "..."   # truelayer: reconfirm for 90 more days
    nationwide-mcp-link reset     # forget everything in the token store

Behaviour depends on NATIONWIDE_PROVIDER (default truelayer):

* truelayer     - prints a TrueLayer authorisation link (with PKCE), catches the redirect on
                  http://localhost (or you paste the redirected URL), exchanges the one-time
                  code and writes the tokens to the token store, replacing what was there.
* enablebanking - same idea via Enable Banking sessions.
* lunchflow     - the bank is linked in the Lunch Flow dashboard; `check` verifies the key.
* statement     - nothing to link; `check` lists the files it can parse.

Tokens are never printed unless you pass --print-refresh-token, which writes a
SECRET to your terminal (and its scrollback): use it only to seed a secret store.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .errors import AuthError, ConfigError, InvalidRequestError, ProviderError, ProviderUnavailableError
from .providers import build_provider
from .providers._http import USER_AGENT, is_loopback_host, strip_control
from .providers.truelayer import SCOPES, STORE_SECTION, TrueLayerConfig, TrueLayerProvider
from .token_store import build_token_store

DEFAULT_REDIRECT = "http://localhost:3000/callback"
DEFAULT_PROVIDERS = {"live": "uk-ob-all uk-oauth-all", "sandbox": "uk-cs-mock uk-ob-all uk-oauth-all"}
PUBLIC_PROVIDER_LIST = "https://auth.truelayer.com/api/providers"
NATIONWIDE_PROVIDER_ID = "ob-nationwide"  # TrueLayer's id for Nationwide (a constant, not a setting)


def _print(obj: Any) -> None:
    # json.dumps escapes control characters, so third-party text cannot drive the terminal.
    print(json.dumps(obj, indent=2, default=str))


def _say(text: Any) -> str:
    """Third-party text made safe for the terminal."""
    return strip_control(text)


# ------------------------------------------------------------------ truelayer


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge) per RFC 7636."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_auth_url(
    config: TrueLayerConfig,
    redirect_uri: str,
    state: str,
    providers: str | None = None,
    code_challenge: str | None = None,
) -> str:
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "scope": SCOPES,
        "redirect_uri": redirect_uri,
        "providers": providers or DEFAULT_PROVIDERS[config.environment],
        "state": state,
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return f"{config.auth_base}/?{urlencode(params)}"


def extract_code(value: str, expected_state: str | None) -> str:
    """Accept a bare code or the full redirected URL.

    A URL (or query string) must carry the ``state`` of this session: a missing or different
    ``state`` is refused, as is a URL when no state is known (``expected_state`` is None).
    """
    value = value.strip()
    if not value:
        raise ConfigError("Nothing was pasted.")
    if "://" not in value and "=" not in value and "?" not in value:
        return value
    query = parse_qs(urlparse(value).query or value.lstrip("?"))
    if not expected_state:
        raise ConfigError(
            "Paste only the bare code here; a redirect URL can only be checked in the session it came from."
        )
    supplied = query.get("state", [""])[0]
    if not supplied:
        raise ConfigError("The pasted URL has no 'state' parameter, so it cannot be tied to this session. Start again.")
    if not secrets.compare_digest(supplied.encode(), expected_state.encode()):
        raise ConfigError("The 'state' value in the redirect does not match this session. Start again.")
    if "error" in query:
        raise ConfigError(f"Authorisation was not completed: {_say(query['error'][0])}.")
    if not query.get("code"):
        raise ConfigError("No 'code' parameter found in the redirect URL.")
    return query["code"][0]


def wait_for_redirect(redirect_uri: str, expected_state: str, timeout: float = 600.0) -> str:
    """Listen on the loopback redirect URI until a request with the right ``state`` arrives.

    Requests with a wrong or missing ``state`` (a stray browser tab, another local program, an
    attacker's page) get a 400 and are otherwise ignored: the helper keeps listening until the
    genuine redirect arrives or ``timeout`` seconds pass.
    """
    target = urlparse(redirect_uri)
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, text: str) -> None:
            body = text.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != (target.path or "/"):
                self._reply(404, "Not found.")
                return
            query = parse_qs(parsed.query)
            supplied = query.get("state", [""])[0]
            if done.is_set() or not secrets.compare_digest(supplied.encode(), expected_state.encode()):
                self._reply(400, "This request does not belong to the current linking session; it was ignored.")
                return
            if "error" in query:
                result["error"] = strip_control(query["error"][0], 100)
            elif query.get("code"):
                result["code"] = query["code"][0]
            else:
                result["error"] = "no code in redirect"
            ok = "code" in result
            self._reply(
                200 if ok else 400,
                "Nationwide linked. You can close this tab."
                if ok
                else f"Linking failed: {result.get('error')}. Return to the terminal.",
            )
            done.set()

        def log_message(self, *args: Any) -> None:  # the query string holds the code: never log it
            return

    server = HTTPServer(("127.0.0.1", target.port or 80), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        if not done.wait(timeout):
            raise ConfigError("Timed out waiting for the redirect. Run the helper again.")
    finally:
        server.shutdown()
        server.server_close()
    if "code" not in result:
        raise ConfigError(f"Authorisation was not completed: {result.get('error', 'unknown error')}.")
    return result["code"]


def check_public_provider_list(url: str = PUBLIC_PROVIDER_LIST, client: httpx.Client | None = None) -> dict[str, Any]:
    """Look up ob-nationwide in TrueLayer's public provider list (no credentials needed)."""
    own = client is None
    client = client or httpx.Client(timeout=30.0, headers={"User-Agent": USER_AGENT})
    try:
        resp = client.get(url)
        resp.raise_for_status()
        providers = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ConfigError(f"Could not read TrueLayer's provider list ({type(exc).__name__}).") from None
    finally:
        if own:
            client.close()
    for item in providers if isinstance(providers, list) else []:
        if isinstance(item, dict) and item.get("provider_id") == NATIONWIDE_PROVIDER_ID:
            scopes = item.get("scopes") or []
            return {
                "found": True,
                "provider_id": NATIONWIDE_PROVIDER_ID,
                "display_name": item.get("display_name"),
                "scopes": scopes,
                "supports_accounts": "accounts" in scopes,
                "supports_cards": "cards" in scopes,
                "availability": (item.get("availability") or {}).get("recommended_status"),
            }
    return {"found": False, "provider_id": NATIONWIDE_PROVIDER_ID}


def _problem_detail(resp: httpx.Response) -> str:
    """Readable summary of an RFC 7807 ``application/problem+json`` body (or whatever came back)."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return "no details returned"
    parts = [str(body[k]) for k in ("title", "detail") if body.get(k)]
    errors = body.get("errors")
    if isinstance(errors, dict):
        parts += [
            f"{field}: {'; '.join(map(str, msgs)) if isinstance(msgs, list) else msgs}"
            for field, msgs in errors.items()
        ]
    if not parts and body.get("error"):
        parts.append(str(body.get("error_description") or body["error"]))
    if body.get("trace_id"):
        parts.append(f"trace_id {body['trace_id']}")
    return strip_control(". ".join(parts) or "no details returned", 400)


def extend_connection(
    provider: TrueLayerProvider, redirect_uri: str, name: str, email: str, client: httpx.Client | None = None
) -> dict[str, Any]:
    """POST {api}/connections/extend - reconfirm consent for another 90 days.

    Documented at https://docs.truelayer.com/reference/extendconnection: the path has NO
    ``/data/v1`` prefix, and failures are 400 / 401 / 422 with an ``application/problem+json`` body.
    """
    tokens = provider.load_tokens()
    cfg = provider.config
    own = client is None
    client = client or httpx.Client(timeout=60.0, headers={"User-Agent": USER_AGENT})
    try:
        resp = client.post(
            f"{cfg.api_base}/connections/extend",
            json={
                "user_has_reconfirmed_consent": True,
                "client_id": cfg.client_id,
                "client_secret": cfg.client_secret,
                "refresh_token": tokens["refresh_token"],
                "redirect_uri": redirect_uri,
                "user": {"name": name, "email": email},
            },
        )
    except httpx.HTTPError as exc:
        raise ProviderUnavailableError(f"Could not reach TrueLayer ({type(exc).__name__}). Try again later.") from None
    finally:
        if own:
            client.close()
    if resp.status_code != 200:
        detail = _problem_detail(resp)
        status = resp.status_code
        if status == 401:
            raise AuthError(
                f"TrueLayer refused to extend the connection (HTTP 401: {detail}). Check the client id and "
                "secret, and that the stored refresh token is still valid; otherwise re-link."
            )
        if status in (400, 422):
            raise InvalidRequestError(f"TrueLayer rejected the extension request (HTTP {status}: {detail}).")
        if status in (403, 404):
            raise ProviderError(
                f"TrueLayer would not extend the connection (HTTP {status}: {detail}). The endpoint may not be "
                "available to this client; re-link instead (`nationwide-mcp-link link`)."
            )
        if status >= 500:
            raise ProviderUnavailableError(f"TrueLayer is unavailable (HTTP {status}: {detail}). Try again later.")
        raise ProviderError(f"TrueLayer would not extend the connection (HTTP {status}: {detail}).")
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise ProviderError("TrueLayer answered the extension request with something that is not a JSON object.")
    if body.get("action_needed") == "no_action_needed" and body.get("access_token"):
        now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        provider.persist({**tokens, "connected_at": now}, body)
        if provider.store_error is not None:
            raise provider.store_error
    return body


def _truelayer_link(provider: TrueLayerProvider, args: argparse.Namespace) -> int:
    config = provider.config
    redirect_uri = args.redirect_uri or os.environ.get("TRUELAYER_REDIRECT_URI") or DEFAULT_REDIRECT
    if args.command == "extend":
        if not args.name or not args.email:
            raise ConfigError("extend needs --name and --email (TrueLayer requires them for the consent record).")
        answer = input("Do you reconfirm that this app may keep reading your Nationwide data for 90 days? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Not extended.")
            return 1
        body = extend_connection(provider, redirect_uri, args.name, args.email)
        if body.get("action_needed") != "no_action_needed":
            print(
                f"TrueLayer needs a further step ({_say(body.get('action_needed'))}). Open this link to finish:\n"
                f"{_say(body.get('user_input_link'))}"
            )
            return 1
        print("Connection extended. New tokens stored.")
    else:
        state = secrets.token_urlsafe(24)
        verifier, challenge = (None, None) if (args.no_pkce or args.code) else pkce_pair()
        url = build_auth_url(
            config, redirect_uri, state, args.providers or os.environ.get("TRUELAYER_PROVIDERS"), challenge
        )
        target = urlparse(redirect_uri)
        if args.code:
            # A code from an earlier run: no state to check and no PKCE verifier to send.
            code = extract_code(args.code, None)
        else:
            print(
                f"\nEnvironment: {config.environment}\n\nOpen this link in your browser, choose Nationwide and "
                f"approve read access to your accounts (and credit card, if you have one):\n\n{url}\n"
            )
            if args.open:
                webbrowser.open(url)
            if target.scheme == "http" and is_loopback_host(target.hostname) and not args.paste:
                print(f"Waiting for the redirect on {redirect_uri} (Ctrl+C to cancel) ...")
                code = wait_for_redirect(redirect_uri, state)
            else:
                code = extract_code(input("Paste the full URL you were redirected to: "), state)
        provider.exchange_code(code, redirect_uri, verifier)
        print("Nationwide linked. Tokens stored in the token store (replacing any earlier ones).")

    status = provider.connection_status()
    print(
        f"Consent expires: {_say(status.get('consent_expires_at'))} ({_say(status.get('days_remaining'))} days). "
        "Put a reminder in your calendar a week before."
    )
    if args.print_refresh_token:
        print(
            "\nRefresh token - a SECRET, treat it as a password and clear your terminal scrollback afterwards. "
            "It seeds an EMPTY token store through NATIONWIDE_TRUELAYER_REFRESH_TOKEN:"
        )
        print(provider.store.get(STORE_SECTION).get("refresh_token"))
    return 0


# -------------------------------------------------------------- enablebanking


def _enablebanking_check(provider: Any, args: argparse.Namespace) -> int:
    country = os.environ.get("ENABLEBANKING_COUNTRY", "GB")
    wanted = (args.bank or os.environ.get("ENABLEBANKING_ASPSP_NAME") or "Nationwide").lower()
    aspsps = provider.list_aspsps(country)
    matches = [a for a in aspsps if wanted in str(a.get("name", "")).lower()]
    print(f"{len(aspsps)} bank(s) available to this application in {_say(country)}.")
    if not matches:
        print(f"No bank matching {_say(wanted)!r}. This route cannot reach Nationwide with this application.")
        return 1
    for a in matches:
        try:
            days = round(float(a.get("maximum_consent_validity") or 0) / 86400)
        except (TypeError, ValueError):
            days = 0
        print(f"FOUND: {_say(a.get('name'))} ({_say(a.get('country'))}) - max consent {days} days")
    return 0


def _enablebanking_link(provider: Any, args: argparse.Namespace) -> int:
    redirect_url = os.environ.get("ENABLEBANKING_REDIRECT_URL", "").strip()
    if not redirect_url:
        raise ConfigError("Set ENABLEBANKING_REDIRECT_URL to a redirect URL registered on the application (https).")
    country = os.environ.get("ENABLEBANKING_COUNTRY", "GB")
    wanted = (args.bank or os.environ.get("ENABLEBANKING_ASPSP_NAME") or "Nationwide").lower()
    matches = [a for a in provider.list_aspsps(country) if wanted in str(a.get("name", "")).lower()]
    if not matches:
        print(f"No bank matching {_say(wanted)!r} is available to this application in {_say(country)}. Run `check`.")
        return 1
    aspsp = matches[0]
    try:
        validity = int(aspsp.get("maximum_consent_validity") or 90 * 86400)
    except (TypeError, ValueError):
        validity = 90 * 86400
    seconds = max(3600, min(validity, 180 * 86400) - 300)
    valid_until = datetime.now(UTC) + timedelta(seconds=seconds)
    state = secrets.token_urlsafe(16)
    auth = provider.start_authorisation(aspsp["name"], aspsp.get("country", country), redirect_url, valid_until, state)
    link = str(auth.get("url") or "")
    if not link.startswith("https://"):
        raise ProviderError("Enable Banking did not return an https authorisation link.")
    print(f"\nOpen this URL and approve READ-ONLY access in Nationwide's app/website:\n\n   {_say(link)}\n")
    if args.open:
        webbrowser.open(link)
    code = extract_code(input("Paste the full address you were redirected to: "), state)
    record = provider.create_session(code)
    print(f"\nLinked. Consent valid until {_say(record['valid_until'])}; {len(record['accounts'])} account(s).")
    print(f"For a host with no persistent disk set ENABLEBANKING_SESSION_ID={_say(record['session_id'])}")
    return 0


# ------------------------------------------------------------------------ cli


def reset_store() -> None:
    """Forget every stored token/session (file store: delete the file)."""
    store = build_token_store()
    clear = getattr(store, "clear", None)
    if callable(clear):
        clear()
    else:  # a custom store that only implements load/save
        store.save({})


def _run(args: argparse.Namespace) -> int:
    name = (os.environ.get("NATIONWIDE_PROVIDER") or "truelayer").strip().lower()
    if args.reset or args.command == "reset":
        reset_store()
        print("Token store cleared. Run `nationwide-mcp-link link` to connect again.")
        if args.command == "reset":
            return 0
    if name == "truelayer" and args.command == "check":
        result = check_public_provider_list()
        _print(result)
        if not result["found"]:
            print("TrueLayer no longer lists Nationwide; use another provider.")
            return 1
        try:
            TrueLayerConfig.from_env(os.environ)
        except ConfigError as exc:
            print(f"\nNationwide is listed, but your credentials are not configured yet: {exc.message}")
        return 0

    provider = build_provider()
    try:
        if args.command == "status":
            status = provider.connection_status()
            _print(status)
            return 0 if status.get("connected") else 1
        if name == "truelayer":
            return _truelayer_link(provider, args)  # type: ignore[arg-type]
        if args.command == "extend":
            raise ConfigError("extend only applies to the truelayer provider.")
        if name == "enablebanking":
            if args.command == "check":
                return _enablebanking_check(provider, args)
            return _enablebanking_link(provider, args)
        if args.command == "link":
            print(
                "Nothing to link here. Lunch Flow: connect the bank in its dashboard. Statement: drop "
                "CSV/OFX exports into NATIONWIDE_STATEMENT_DIR."
            )
        status = provider.connection_status()
        _print(status)
        if status.get("connected"):
            _print(provider.list_accounts())
        return 0 if status.get("connected") else 1
    finally:
        provider.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="nationwide-mcp-link", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=("check", "link", "status", "extend", "reset"))
    parser.add_argument(
        "--redirect-uri", help=f"truelayer: must be allow-listed in the Console (default {DEFAULT_REDIRECT})"
    )
    parser.add_argument("--providers", help="truelayer 'providers' filter (default: all UK Open Banking providers)")
    parser.add_argument("--open", action="store_true", help="also open the link in your default browser")
    parser.add_argument("--paste", action="store_true", help="paste the redirect URL instead of listening on localhost")
    parser.add_argument(
        "--code", help="exchange a bare authorisation code you already have (from a link made with --no-pkce)"
    )
    parser.add_argument("--no-pkce", action="store_true", help="truelayer: do not add a PKCE challenge to the link")
    parser.add_argument("--reset", action="store_true", help="clear the token store before running the command")
    parser.add_argument("--name", help="your name (only for extend)")
    parser.add_argument("--email", help="your email (only for extend)")
    parser.add_argument("--bank", help="enablebanking: bank name to look for (default Nationwide)")
    parser.add_argument(
        "--print-refresh-token",
        action="store_true",
        help="print the refresh token (a SECRET) so you can copy it into a secret store",
    )
    args = parser.parse_args(argv)
    started = time.monotonic()
    try:
        raise SystemExit(_run(args))
    except ProviderError as exc:
        print(f"\nERROR {strip_control(str(exc), 1000)}", file=sys.stderr)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        print(f"\nCancelled after {int(time.monotonic() - started)}s.", file=sys.stderr)
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
