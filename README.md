# nationwide-mcp

[![CI](https://github.com/pritch56/Nationwide-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/pritch56/Nationwide-mcp/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange)](#unverified-assumptions)

**Ask Claude (or any MCP client) about your own Nationwide Building Society accounts, balances and
transactions, through a server that can only read.**

An **unofficial**, **read-only** [Model Context Protocol](https://modelcontextprotocol.io) server for
Nationwide Building Society (UK) account data: accounts, balances and transactions in one normalised
shape. It has no payment tool of any kind and never logs in to online banking with your credentials
(no screen scraping). Data comes from an Open Banking aggregator that you sign up to yourself, or from
statement files that you download yourself.

> **Status: alpha, never run against a live API.** Everything here was written from public
> documentation and is tested only against mocked HTTP, a local fake TrueLayer server and synthetic
> statement files. No real Nationwide data has been fetched with it. Expect to find differences on
> first contact with the real services; the specific assumptions that have not been checked are
> listed under [Unverified assumptions](#unverified-assumptions). Verify amounts and signs against
> your banking app before relying on anything.

**Before you use it:** unofficial and not affiliated with Nationwide or any aggregator; read-only;
for **your own accounts only** (giving other people access to bank data is a regulated activity in
the UK); you must follow your provider's terms; no warranty and not financial advice. The full text
is under [Disclaimer](#disclaimer) and [Security](#security).

## Contents

[What you can ask](#what-you-can-ask) · [60-second quick start](#60-second-quick-start) ·
[MCP client configuration](#mcp-client-configuration) · [Tools](#tools) ·
[Do not lose transactions](#do-not-lose-transactions-overlapping-windows) · [Providers](#providers) ·
[Connecting your real accounts](#connecting-your-real-accounts) · [Running](#running) ·
[Configuration](#configuration) · [Disclaimer](#disclaimer) · [Security](#security) ·
[Limitations](#limitations) · [Unverified assumptions](#unverified-assumptions) · [Errors](#errors) ·
[Tokens](#tokens-truelayer) · [Troubleshooting](#troubleshooting) · [Development](#development)

## What you can ask

The server gives the model four read-only tools (accounts, balance, transactions, connection
status). The model does the arithmetic and the summarising. Prompts that these tools support:

- "Which Nationwide accounts can you see, and what is the balance on each?"
- "List everything that left my current account between 1 and 31 October and total it by
  description."
- "What were my five largest payments last month, and did any direct debit change amount compared
  with the month before?"
- "Is my bank connection still working, and when does the consent expire?"
- "Are there any pending card payments that have not settled yet?" (aggregator providers only:
  statement files contain booked transactions only)

It cannot make payments, move money, change anything at the bank, or see accounts you have not
linked or exported. Categories and merchant names are only present when the data source supplies
them (statement files supply neither).

## 60-second quick start

The lowest-friction way to see it work needs **no bank account, no sign-up and no credentials**: the
`statement` provider reads CSV / OFX files from a folder, and the repository ships a small
**synthetic** sample file (made-up data, see [examples/](examples/README.md)).

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

PowerShell:

```powershell
git clone https://github.com/pritch56/Nationwide-mcp.git
cd Nationwide-mcp
uv sync
$env:NATIONWIDE_PROVIDER = "statement"
$env:NATIONWIDE_STATEMENT_DIR = "examples/sample-statements"
uv run nationwide-mcp-link check
```

bash:

```bash
git clone https://github.com/pritch56/Nationwide-mcp.git
cd Nationwide-mcp
uv sync
export NATIONWIDE_PROVIDER=statement NATIONWIDE_STATEMENT_DIR=examples/sample-statements
uv run nationwide-mcp-link check
```

`check` prints `"connected": true`, `"status": "FILES_LOADED"` and one account. Then add the server
to your MCP client with the [configuration below](#mcp-client-configuration) and ask, for example,
"list my Nationwide transactions between 2026-10-01 and 2026-10-31" (the sample rows are dated
October 2026, so name the dates: the default window is the last 30 days).

To use **your own data** the same way, download transactions from the Nationwide Internet Bank
(CSV or OFX) into a folder outside this repository and point `NATIONWIDE_STATEMENT_DIR` at it. Read
[Statement files](#statement-files) first: the CSV layout the parser expects has not been checked
against a real export. For live data through an aggregator, see
[Connecting your real accounts](#connecting-your-real-accounts).

## MCP client configuration

### Statement files (no credentials)

The same block works in Claude Code (`.mcp.json` in your project) and Claude Desktop
(`claude_desktop_config.json`). Replace both paths with absolute paths on your machine; on Windows
write them with forward slashes, as in `C:/path/to/Nationwide-mcp`.

```json
{
  "mcpServers": {
    "nationwide": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/Nationwide-mcp", "nationwide-mcp"],
      "env": {
        "NATIONWIDE_PROVIDER": "statement",
        "NATIONWIDE_STATEMENT_DIR": "/absolute/path/to/Nationwide-mcp/examples/sample-statements"
      }
    }
  }
}
```

Restart the client; the server appears as `nationwide` with four tools.

### An aggregator (TrueLayer shown)

Keep secrets out of client configuration files: reference environment variables.

Claude Code, `.mcp.json` (it expands `${VAR}` from the environment Claude Code was started in):

```json
{
  "mcpServers": {
    "nationwide": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/Nationwide-mcp", "nationwide-mcp"],
      "env": {
        "TRUELAYER_CLIENT_ID": "${TRUELAYER_CLIENT_ID}",
        "TRUELAYER_CLIENT_SECRET": "${TRUELAYER_CLIENT_SECRET}"
      }
    }
  }
}
```

Claude Desktop, `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "nationwide": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/Nationwide-mcp", "--env-file", ".env", "nationwide-mcp"],
      "env": {
        "TRUELAYER_CLIENT_ID": "${TRUELAYER_CLIENT_ID}",
        "TRUELAYER_CLIENT_SECRET": "${TRUELAYER_CLIENT_SECRET}"
      }
    }
  }
}
```

If your client does not expand `${VAR}` placeholders, delete the `env` block and rely on the
`--env-file .env` argument shown above (the `.env` file stays on your disk, outside the client
configuration). On Windows write the path as `C:/path/to/Nationwide-mcp`.

To set the variables for the session — PowerShell:

```powershell
$env:TRUELAYER_CLIENT_ID = "<client id>"
$env:TRUELAYER_CLIENT_SECRET = Read-Host "TrueLayer client secret"
```

bash:

```bash
export TRUELAYER_CLIENT_ID="<client id>"
read -rs TRUELAYER_CLIENT_SECRET && export TRUELAYER_CLIENT_SECRET
```

## Tools

All four tools are read-only.

| Tool | Returns |
|---|---|
| `get_connection_status()` | `connected` (bool), `consent_expires_at` (ISO 8601 UTC timestamp ending `Z`, or null), `days_remaining` (int or null), `warning` (string or null; set when fewer than 14 days remain or action is needed), plus `provider`, `status`, `needs_reauthorisation`, `message`. **Never raises** for provider, authentication, configuration or rate-limit problems: it returns `connected: false` with the bracketed error in `warning` |
| `list_accounts()` | `{source, count, accounts:[{id, source, name, type, currency, institution, mask}]}`; `id` and `name` are always present; `type` is `current`, `savings`, `credit_card` or `other` |
| `get_balance(account_id)` | `{account_id, source, currency, balance_minor, current_minor, available_minor, as_of, balances:[…]}`. `balance_minor` is the primary field; `current_minor` is the same value, kept for compatibility |
| `list_transactions(account_id, since?, before?, include_pending=false)` | `{account_id, source, since, before, count, pending_supported, pending_excluded, earliest_pending_date, transactions:[…]}` |

`list_transactions` details:

- The window is half-open: `since <= date < before`. Defaults: `before` = tomorrow (Europe/London
  date), `since` = `before` minus 30 days. The effective `since` and `before` are echoed back.
- `since >= before`, an unparseable date, or a range longer than `NATIONWIDE_MAX_RANGE_DAYS`
  (default 400) gives `[invalid_request]` before any request is sent upstream.
- `pending_excluded` is the number of pending items in the window that were left out because
  `include_pending` is false; `earliest_pending_date` is the date of the earliest pending item in the
  window (or null); `pending_supported` is `false` when the data source cannot report pending items
  at all (statement files; a bank without a pending endpoint).

A transaction:

```json
{"id": "nationwide_3f2a9c…", "source": "nationwide", "account_id": "acc1", "date": "2026-10-05",
 "amount_minor": -1234, "currency": "GBP", "description": "TESCO STORES 1234",
 "merchant": "Tesco", "category": "Shopping > Groceries", "pending": false, "notes": null}
```

- `date` is a `YYYY-MM-DD` calendar date in **Europe/London** local time. Timestamps that carry a
  UTC offset are converted, not truncated (`2026-06-30T23:30:00Z` is 1 July).
- `amount_minor` is a signed integer in minor units (pence); **negative is money out**. Minor units
  follow ISO 4217 (0 decimals for JPY, KRW and similar; 3 for BHD, KWD and similar).
- Balances: positive is money you have; negative is overdrawn or, on a credit card, money owed.
- `notes` is `null` when empty, never `""`.
- `id` is deterministic. With TrueLayer it is a hash of the account id and
  `normalised_provider_transaction_id` (or `provider_transaction_id`); TrueLayer's own
  `transaction_id` is documented as unstable and is not used. Items without a durable id, and all
  pending items, get a hash of account + date + amount + description + occurrence number.
- If two *different* transactions arrive with the same provider id, both are kept: the first keeps
  the normal id, each later one gets a content-derived id, and a warning is logged.
- TrueLayer sign handling: account transactions arrive negative for money out (kept); card
  transactions arrive positive for money out (inverted); a card's `current` balance arrives positive
  when money is owed (inverted). `transaction_type` (DEBIT/CREDIT) wins when present.

## Do not lose transactions: overlapping windows

With pending items excluded (the default), a transaction that is **pending when you fetch and
settles later** keeps its original date. A later fetch whose window starts *after* that date will
never return it. Fetching back-to-back, non-overlapping windows (for example "since the last run")
therefore **silently drops transactions**.

Callers must:

1. **Re-fetch an overlapping window every time.** Start at or before the `earliest_pending_date`
   reported by the previous call. If you do not have one, or `pending_supported` is `false`, go back
   at least **14 days**.
2. **De-duplicate on `id`** (insert-or-update keyed on `id`).

`pending_excluded` tells you how many items are waiting; when it is greater than zero the next fetch
must reach back to `earliest_pending_date`. Pending items themselves (`include_pending=true`) can
change `id` and amount when they settle, so do not store them as final.

## Providers

Nationwide has no API for individual customers; its Open Banking API can only be called by regulated
firms. An individual therefore needs an intermediary. Four data sources are implemented behind one
interface (`src/nationwide_mcp/providers/base.py`); choose one with `NATIONWIDE_PROVIDER`.

| `NATIONWIDE_PROVIDER` | What it is | You need |
|---|---|---|
| `truelayer` (default) | TrueLayer Data API v1: current/savings accounts and credit cards | A TrueLayer Console application with Live access |
| `lunchflow` | Lunch Flow personal API (a paid consumer service that holds the bank consent) | A Lunch Flow subscription and API key |
| `enablebanking` | Enable Banking API | An Enable Banking application and private key |
| `statement` | CSV / OFX files you export from the Nationwide Internet Bank | Nothing but the files |

Which of these actually reaches Nationwide for a private individual, and at what price, changes over
time and has not been verified by running this code. A dated comparison with sources is in
[docs/research.md](docs/research.md). Transaction ids differ between providers, so use one provider
for any given period.

### Comparison

What each route costs and whether it reaches Nationwide is taken from public pages read in October
2026 ([docs/research.md](docs/research.md)); none of it was confirmed by opening an account. "Tested
against" describes this project's own test suite.

| Provider | Sign-up needed | Cost (as read, Oct 2026) | Reaches Nationwide? (as read, Oct 2026) | Pending items | Tested against |
|---|---|---|---|---|---|
| `statement` | None | Free | Yes, to the extent Nationwide offers a download for the account type | No (booked only) | Synthetic files only; CSV layout written from memory |
| `truelayer` (default) | TrueLayer Console application in Live mode; availability to private individuals unconfirmed | No public price list found | `ob-nationwide` appeared in TrueLayer's public provider list | Yes, if the bank offers the endpoint | Mocked HTTP and a local fake server |
| `lunchflow` | Paid consumer subscription and API key | Roughly US$2.92 per month billed yearly was advertised; indicative only | Not confirmed | As reported by Lunch Flow; not verified | Mocked HTTP |
| `enablebanking` | Application and private key; free "restricted" mode for your own accounts | Free in restricted mode | Doubtful: no Nationwide entry was found | Depends on the bank; not verified | Mocked HTTP |

## Connecting your real accounts

Live data needs one of the aggregator providers and a `.env` file holding its settings
(`.env.example` lists every variable). Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

PowerShell:

```powershell
git clone https://github.com/pritch56/Nationwide-mcp.git
cd Nationwide-mcp
uv sync
Copy-Item .env.example .env      # then edit .env
uv run --env-file .env nationwide-mcp-link check
```

bash:

```bash
git clone https://github.com/pritch56/Nationwide-mcp.git
cd Nationwide-mcp
uv sync
cp .env.example .env             # then edit .env
uv run --env-file .env nationwide-mcp-link check
```

`check` needs no credentials for the default provider: it looks Nationwide up in TrueLayer's public
provider list.

### Linking the bank

#### TrueLayer (default)

1. Create an account and an application at <https://console.truelayer.com>.
2. The application must be in **Live** mode to see real banks (Sandbox returns mock data only).
   Whether, and on what terms, TrueLayer grants Live access to an individual is unconfirmed.
3. Enable the Data API scopes `info`, `accounts`, `balance`, `cards`, `transactions`,
   `offline_access`, and allow-list the redirect URI `http://localhost:3000/callback`.
4. Put `TRUELAYER_CLIENT_ID` and `TRUELAYER_CLIENT_SECRET` in `.env`.
5. Link:

   PowerShell / bash (same command):

   ```
   uv run --env-file .env nationwide-mcp-link link
   ```

   It prints a TrueLayer link. Open it, choose **Nationwide**, approve in Nationwide's own app and
   pick the accounts (and credit card) to share. The helper catches the redirect on
   `http://localhost:3000/callback` and writes the tokens to the token store
   (`~/.nationwide-mcp/tokens.json` by default), replacing any earlier ones. Use `--paste` to paste
   the redirected URL instead of listening.
6. Check: `uv run --env-file .env nationwide-mcp-link status`, then call `list_transactions` once
   and compare a few rows with your banking app (spending must be negative).

The link uses **PKCE** (`code_challenge` with method `S256`; the token exchange sends
`code_verifier`). TrueLayer's token reference documents `code_verifier`; the authorisation-link
parameters were not found in the pages read, so if TrueLayer rejects the link, pass `--no-pkce`.
`state` is always checked: a redirect with a wrong or missing `state` is answered with 400 and
ignored, and the helper keeps waiting for the genuine one.

Other helper commands:

| Command | Purpose |
|---|---|
| `nationwide-mcp-link check` | Is the chosen provider usable? |
| `nationwide-mcp-link status` | Connection state and consent expiry |
| `nationwide-mcp-link extend --name "…" --email "…"` | TrueLayer only: reconfirm the consent for another 90 days through `POST /connections/extend`, without visiting the bank. May not be available to every client |
| `nationwide-mcp-link reset` | Delete everything in the token store (also `--reset` on any command) |
| `nationwide-mcp-link link --print-refresh-token` | Also print the refresh token. **This prints a secret** |

#### Lunch Flow

Sign up at <https://www.lunchflow.app>, connect Nationwide in its dashboard (stop if it is not
offered), add an API destination, then set `NATIONWIDE_PROVIDER=lunchflow`, `LUNCHFLOW_API_KEY` and
`LUNCHFLOW_CONNECTED_ON=<the date you connected>`. Run `nationwide-mcp-link check`. If spending
shows as positive, set `LUNCHFLOW_INVERT_SIGN=1`.

#### Enable Banking

Register an application at <https://enablebanking.com>. A newly registered production application
starts in **restricted mode**: free, and limited to accounts that you link to the application
yourself in the Enable Banking control panel, i.e. your own accounts. An application cannot be
offered to other people or the public without a contract with Enable Banking. Download the private
key (`.pem`), register an `https` redirect URL, then set `NATIONWIDE_PROVIDER=enablebanking`,
`ENABLEBANKING_APP_ID`, `ENABLEBANKING_PRIVATE_KEY_FILE` and `ENABLEBANKING_REDIRECT_URL`.
`nationwide-mcp-link check` says whether Nationwide is offered to your application; if it is, run
`nationwide-mcp-link link`.

#### Statement files

In the Nationwide Internet Bank choose **Download transactions** (CSV or OFX), save the files into
one folder and set `NATIONWIDE_PROVIDER=statement` and `NATIONWIDE_STATEMENT_DIR`. Overlapping
exports are merged. The CSV layout this parser expects was written **from memory of the format, not
from a real export**: check the first import carefully.

## Running

stdio (what MCP clients launch) — PowerShell / bash:

```
uv run --env-file .env nationwide-mcp
```

Streamable HTTP at `http://HOST:PORT/mcp` — PowerShell:

```powershell
$env:NATIONWIDE_MCP_AUTH_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
uv run --env-file .env nationwide-mcp --transport http --host 127.0.0.1 --port 8000
```

bash:

```bash
export NATIONWIDE_MCP_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run --env-file .env nationwide-mcp --transport http --host 127.0.0.1 --port 8000
```

`python -m nationwide_mcp` is equivalent to the `nationwide-mcp` script.

HTTP notes:

- Clients send `Authorization: Bearer <token>`. On a non-loopback host the server **refuses to
  start without `NATIONWIDE_MCP_AUTH_TOKEN`**, unless you set `NATIONWIDE_MCP_ALLOW_NO_AUTH=1`
  because an authenticating gateway sits in front of it.
- `GET /healthz` is unauthenticated on purpose (for load-balancer probes). It answers
  `{"ok": true}` and reveals nothing else.
- The transport is plain HTTP, stateless, with JSON responses. Put TLS in front of it.
- Behind a reverse proxy or tunnel, list the public host names in `NATIONWIDE_MCP_ALLOWED_HOSTS`
  so that the `Host` header check accepts them.

## Configuration

Every environment variable the code reads. Nothing is read from generic names such as
`MCP_TRANSPORT`, `PORT` or `LOG_LEVEL`.

### General

| Variable | Default | Purpose |
|---|---|---|
| `NATIONWIDE_PROVIDER` | `truelayer` | `truelayer`, `lunchflow`, `enablebanking` or `statement` |
| `NATIONWIDE_MAX_RANGE_DAYS` | `400` | Longest `since`..`before` range `list_transactions` accepts |
| `NATIONWIDE_TOKEN_STORE` | `file` | `file`, `env`, `memory`, or `package.module:ClassName` (see [Security](#security)) |
| `NATIONWIDE_TOKEN_FILE` | `~/.nationwide-mcp/tokens.json` | Path of the token file (`file` store) |
| `NATIONWIDE_TOKEN_JSON` | empty | The token document as JSON (`env` store; read-only, changes last until exit) |

### Transport

| Variable | Default | Purpose |
|---|---|---|
| `NATIONWIDE_MCP_TRANSPORT` | `stdio` | `stdio` or `http` (same as `--transport`) |
| `NATIONWIDE_MCP_HOST` | `127.0.0.1` | HTTP bind address (`--host`) |
| `NATIONWIDE_MCP_PORT` | `8000` | HTTP port (`--port`) |
| `NATIONWIDE_MCP_PATH` | `/mcp` | URL path of the MCP endpoint (`--path`) |
| `NATIONWIDE_MCP_AUTH_TOKEN` | empty | Static bearer token required on every HTTP request. Required for a non-loopback bind |
| `NATIONWIDE_MCP_ALLOW_NO_AUTH` | empty | `1` permits a non-loopback bind without a token (only behind an authenticating gateway) |
| `NATIONWIDE_MCP_ALLOWED_HOSTS` | empty | Comma-separated public host names accepted in the `Host` header (reverse proxy / tunnel) |
| `NATIONWIDE_MCP_LOG_LEVEL` | `WARNING` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`; logs go to stderr |

### TrueLayer

| Variable | Default | Purpose |
|---|---|---|
| `TRUELAYER_CLIENT_ID` | none (required) | Application client id from the Console |
| `TRUELAYER_CLIENT_SECRET` | none (required) | Application client secret |
| `TRUELAYER_ENV` | `live` | `live` or `sandbox` |
| `TRUELAYER_REDIRECT_URI` | `http://localhost:3000/callback` | Redirect URI used by the link helper; must be allow-listed in the Console |
| `TRUELAYER_PROVIDERS` | all UK Open Banking providers | The `providers` filter put in the authorisation link (also `--providers`) |
| `TRUELAYER_TOKEN_ENCODING` | `json` | `json` or `form` body for the token endpoint (the reference documents JSON) |
| `TRUELAYER_WINDOW_DAYS` | `88` | Days per transactions request (1 to 88). Long ranges are split into windows of this size |
| `NATIONWIDE_TRUELAYER_REFRESH_TOKEN` | empty | Seed refresh token, used **only when the token store is empty** (see [Tokens](#tokens-truelayer)) |
| `TRUELAYER_AUTH_BASE`, `TRUELAYER_API_BASE` | TrueLayer's hosts | **Test hooks.** Must be `https://`, or `http://` on a loopback host |

### Lunch Flow

| Variable | Default | Purpose |
|---|---|---|
| `LUNCHFLOW_API_KEY` | none (required) | API destination key |
| `LUNCHFLOW_INSTITUTION_FILTER` | `nationwide` | Only accounts whose institution name contains this text are exposed; empty = all |
| `LUNCHFLOW_CONNECTED_ON` | empty | `YYYY-MM-DD` you connected the bank; enables an estimated consent expiry |
| `LUNCHFLOW_CONSENT_DAYS` | `90` | Consent length used for that estimate |
| `LUNCHFLOW_INVERT_SIGN` | empty | `1` flips the sign of every transaction amount |
| `LUNCHFLOW_BASE_URL` | `https://lunchflow.app/api/v1` | **Test hook.** Same `https` rule as above |

### Enable Banking

| Variable | Default | Purpose |
|---|---|---|
| `ENABLEBANKING_APP_ID` | none (required) | Application id (the JWT `kid`) |
| `ENABLEBANKING_PRIVATE_KEY_FILE` | empty | Path to the application's private key (`.pem`) |
| `ENABLEBANKING_PRIVATE_KEY` | empty | The PEM text itself (literal `\n` allowed); takes precedence over the file |
| `ENABLEBANKING_REDIRECT_URL` | none | Redirect URL registered on the application (link helper) |
| `ENABLEBANKING_COUNTRY` | `GB` | Country used when listing banks (link helper) |
| `ENABLEBANKING_ASPSP_NAME` | `Nationwide` | Bank name to look for (link helper; also `--bank`) |
| `ENABLEBANKING_SESSION_ID` | empty | Session id to use instead of the one in the token store |
| `ENABLEBANKING_BASE_URL` | `https://api.enablebanking.com` | **Test hook.** Same `https` rule as above |

### Statement files

| Variable | Default | Purpose |
|---|---|---|
| `NATIONWIDE_STATEMENT_DIR` | none (required) | Folder holding the `.csv` / `.ofx` / `.qfx` exports |
| `NATIONWIDE_STATEMENT_MAX_BYTES` | `10485760` | Largest file accepted (bytes) |

Standard proxy variables (`HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`, `NO_PROXY`) and `SSL_CERT_FILE`
are honoured by the HTTP library (httpx). On Windows the token-file ACL uses `USERNAME` /
`USERDOMAIN`.

## Disclaimer

- This project is **not affiliated with, endorsed by or supported by** Nationwide Building Society,
  TrueLayer, Lunch Flow or Enable Banking. All trademarks belong to their owners.
- It is provided **as is, without warranty of any kind**. It is not financial advice.
- **You must register your own API client** with whichever provider you use and comply with that
  provider's terms. Whether TrueLayer offers Live (real bank) access to private individuals is
  **unconfirmed**: read the TrueLayer Console terms yourself. Enable Banking's free "restricted" mode
  is for your own accounts only. Lunch Flow's terms have not been reviewed.
- It is for **your own accounts only**. Do not host it for other people: giving third parties access
  to bank data is a regulated activity in the UK.

## Security

- **Treat every text field as untrusted third-party input.** Descriptions, merchant names, notes,
  account names and statement contents are written by merchants, payers and banks. They can contain
  **prompt-injection text** aimed at the model reading them and **spreadsheet formulas**. Do not pair
  this server with tools that run shell commands or send e-mail without human confirmation. Anything
  that writes these values into a spreadsheet must neutralise cells beginning with `=`, `+`, `-` or
  `@`.
- **Token files are plaintext.** The file is written atomically with owner-only access: mode `0600`
  on POSIX; on Windows inheritance is removed and the ACL is reduced to the current user with
  `icacls` (if that fails a warning is logged and the file keeps its inherited permissions). Anyone
  who can read the file, or your backups of it, can read your bank data until the consent ends.
- **`--print-refresh-token` prints a secret** to your terminal and its scrollback.
- **`NATIONWIDE_TOKEN_STORE=package.module:ClassName` imports and runs code named in the
  environment.** Only set it to code you trust, and protect whatever sets your environment.
- **HTTP transport** is plain HTTP with a single static bearer token. Put it behind TLS, always set
  `NATIONWIDE_MCP_AUTH_TOKEN` (even on loopback: other local users and browser pages can reach
  loopback ports), and do not expose it to the internet directly. `/healthz` is unauthenticated.
- **Proxy variables are honoured** (`HTTPS_PROXY` and friends): a proxy set in the environment sees
  the traffic's metadata, and a TLS-intercepting one sees the tokens.
- Base-URL overrides are test hooks and must be `https://` (or loopback), so a typo cannot send
  credentials over plain HTTP.
- The statement importer reads only regular files that resolve inside `NATIONWIDE_STATEMENT_DIR`
  (symlinks leading elsewhere are skipped) and caps file size.
- Client secrets, tokens, API keys and private keys are never logged or put in error messages.
  `.env`, `*.pem` and token files are git-ignored.

Report vulnerabilities privately: see [SECURITY.md](SECURITY.md).

## Limitations

- **Consent lasts about 90 days** and a human must renew it (`nationwide-mcp-link link`, or
  `extend` where available). `get_connection_status` sets `warning` when fewer than 14 days remain.
- **Rate limits.** UK banks typically allow only a handful of unattended requests per account per
  day (commonly quoted as four per endpoint per 24 hours; not verified for Nationwide). Each
  `list_transactions` call makes one request per 88-day window plus one for pending items. Never
  retry immediately on `[rate_limited]`.
- **Request ceiling.** One tool call makes at most 30 upstream requests (retries included), and a
  range that would need more is refused with `[invalid_request]`.
- **History.** Requests for data older than about 90 days usually fail with `[sca_required]` unless
  made within minutes of linking. Back-fill then, or from statement files.
- **Rotating tokens need writable, persistent storage.** On a host with an ephemeral disk a rotated
  refresh token is lost on restart; mount a volume for `NATIONWIDE_TOKEN_FILE` or plug in your own
  store.
- **One process per token store.** Two servers sharing one store can each rotate the token and lock
  the other out.
- Statement files contain booked transactions only, and their balance is only as fresh as the file.

## Unverified assumptions

Nothing below has been confirmed against a live service.

**TrueLayer**

- Whether TrueLayer rotates refresh tokens for this kind of connection (the code handles both).
- Whether an individual can obtain Live access to the Data API, and what it costs.
- That Nationwide actually returns credit cards on `/data/v1/cards` and pending items on
  `/transactions/pending` through TrueLayer (the public provider list advertises the scopes).
- The sign of a card's balance (`current` assumed positive when money is owed) and of card
  transactions (assumed positive for spending).
- Whether TrueLayer account ids change when you re-link. Transaction ids here are derived from the
  account id, so **if account ids change on re-link, every transaction id changes too** and a caller
  de-duplicating on `id` will see duplicates of older items.
- Whether pending items keep a stable id (assumed not; they get content-derived ids).
- Whether `timestamp` is the posting date or the purchase date (it is used as given).
- `X-PSU-IP` is never sent, so every request is presented as unattended and counts against the
  bank's unattended limits.
- Whether `/data/v1/me` reports `consent_expires_at` (otherwise the expiry is estimated as link date
  + 90 days and flagged `consent_expiry_is_estimate`).
- Whether the bank filters `from`/`to` in UTC or local time (requests start one day early to cover
  both), and the token endpoint's body encoding (JSON per the reference).
- The PKCE parameters on the authorisation link, and that `/connections/extend` is available to a
  self-registered client.

**Lunch Flow**

- That Nationwide can be connected at all, and Lunch Flow's price and terms.
- The sign of `amount` (assumed negative for money out; `LUNCHFLOW_INVERT_SIGN` flips it) and of a
  credit-card balance (passed through unchanged).
- Pagination: the documentation shows a `total` field but no paging parameters. A response with
  fewer items than `total` is rejected with `[provider_error]` rather than returned truncated.
- That `include_pending=true` and `isPending` behave as documented, and that ids are stable.
- No PSU headers are sent; whether Lunch Flow refreshes data on request or on its own schedule.

**Enable Banking**

- That Nationwide is reachable in restricted mode at all.
- Transaction direction relies on `credit_debit_indicator`. An item with neither an indicator nor a
  signed amount is rejected with `[provider_error]` rather than guessed.
- **Balance signs are not interpreted**: `balance_amount` is passed through, so a credit-card or
  overdrawn balance may have the wrong sign.
- The default transaction status filter (none is sent) and therefore whether pending items appear;
  pagination beyond `continuation_key`; that `identification_hash` is stable across sessions.
- PSU headers (`Psu-Ip-Address` and similar) are not sent.

**Statement files**

- The Nationwide CSV layout (metadata rows, column names, date format, encoding) was written from
  memory of the format, not from a real export. OFX handling follows the generic OFX 1.x layout.
- The sign conventions of a credit-card export and of an overdrawn balance.

## Errors

A failed tool call is an MCP tool error whose text contains a code in square brackets. The SDK
prefixes the text with `Error executing tool <name>: `, so **match the bracketed code as a
substring**, not at the start of the string. Rate-limit errors may end with
`(retry_after_seconds=N)`.

| Code | Meaning | What a caller should do |
|---|---|---|
| `[consent_expired]` | The bank consent expired or was revoked | Stop; a human must re-link |
| `[not_connected]` | The bank was never linked (no token / session / files) | Stop; a human must link |
| `[token_expired]` | A credential (access token, API key, signed JWT) was rejected | Stop; check the credential |
| `[rate_limited]` | The aggregator or the bank refused the call | Wait (hours); never retried automatically; do not re-link |
| `[sca_required]` | The bank wants fresh customer authentication (typically history older than about 90 days) | Ask for a more recent range |
| `[invalid_request]` | Bad arguments: malformed id or date, inverted or over-long range | Fix the call |
| `[account_not_found]` | Unknown account id | Call `list_accounts` |
| `[provider_error]` | Any other upstream failure: timeout, 5xx, unsupported endpoint, malformed or non-JSON response | Retry later |
| `[config_error]` | Missing or invalid configuration | Fix the environment |
| `[token_store_error]` | The token store could not be read or written | Fix the store (permissions, disk) |
| `[malformed_import_file]` | A statement file is too large, unreadable or not in a recognised layout | Fix or remove the file |
| `[internal_error]` | A bug; details are in the server log only | Report it |

HTTP 429 is never retried by this server. Network errors and 5xx responses from TrueLayer are
retried twice with a short back-off.

## Tokens (TrueLayer)

TrueLayer issues an access token lasting about an hour and a long-lived refresh token, which it
**may replace on any refresh** (rotation).

- **The store always wins.** A token in the token store is used whatever the environment says. The
  seed `NATIONWIDE_TRUELAYER_REFRESH_TOKEN` is read **only when the store is empty** (first start on
  a new host). A stale seed left in the environment can therefore never override a freshly linked
  token.
- **To re-link**, run `nationwide-mcp-link link`: it overwrites the store. **To start again from a
  seed**, clear the store first with `nationwide-mcp-link reset` (or delete the token file).
- After a refresh the new tokens are kept in memory and used even if writing them to the store
  fails. The write is retried on the next call, the superseded refresh token is never sent again,
  and `get_connection_status` carries a `[token_store_error]` warning until the write succeeds. If
  the process stops before then, the rotated token is lost and you must re-link.
- One refresh runs at a time per process; a call that gets a 401 refreshes only if nobody else
  already has.
- Each bank is a separate TrueLayer connection with its own refresh token. If a token for another
  bank is supplied by mistake the server answers `[config_error]` instead of presenting that bank's
  data as Nationwide. Do not point two bank connections at the same token file.
- The generic name `TRUELAYER_REFRESH_TOKEN` is not read.
- Store options: `file` (default), `env` (read-only JSON in `NATIONWIDE_TOKEN_JSON`), `memory`, or
  `package.module:ClassName` implementing `load() -> dict` and `save(dict)`.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `[not_connected]` | Nothing in the token store and no seed. Run `nationwide-mcp-link link` with the same environment the server uses (`NATIONWIDE_TOKEN_FILE` in particular) |
| `[consent_expired]` right after linking | The server is reading a different token store than the helper wrote, or two processes share one store. Check `NATIONWIDE_TOKEN_STORE` / `NATIONWIDE_TOKEN_FILE` |
| A new seed token is ignored | By design the store wins. Run `nationwide-mcp-link reset`, then start the server |
| `[config_error] … belongs to another bank connection` | The token is for a different bank. `reset`, then `link` and choose Nationwide |
| `[config_error]` about client credentials | Wrong id/secret, or `TRUELAYER_ENV` does not match them |
| `[sca_required]` | The range reaches back further than the bank allows without re-authentication. Ask for the last 90 days |
| `[rate_limited]` | Too many unattended requests today. Wait; do not re-link |
| `[invalid_request] … range` | The range exceeds `NATIONWIDE_MAX_RANGE_DAYS` or the per-call request ceiling. Split it |
| `[token_store_error]` | The token file's folder is not writable, or the file is corrupt. Fix it promptly: the newest token may exist only in memory |
| Server refuses to start on `0.0.0.0` | Set `NATIONWIDE_MCP_AUTH_TOKEN` |
| HTTP 421 / "Invalid Host header" behind a proxy | Add the public host name to `NATIONWIDE_MCP_ALLOWED_HOSTS` |
| Spending shows as positive | A sign assumption is wrong for your provider (see [Unverified assumptions](#unverified-assumptions)); for Lunch Flow set `LUNCHFLOW_INVERT_SIGN=1`; otherwise please open an issue |
| The redirect never arrives | The redirect URI is not allow-listed, or port 3000 is taken. Use `--redirect-uri` or `--paste` |
| `[malformed_import_file]` | A statement file is too big or not in the expected layout. The message names the file |

Set `NATIONWIDE_MCP_LOG_LEVEL=INFO` for more detail on stderr.

## Development

```
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

The tests need no network access and no credentials. See [CONTRIBUTING.md](CONTRIBUTING.md) and
[CHANGELOG.md](CHANGELOG.md).

```
src/nationwide_mcp/
  server.py            MCP tools, stdio + streamable HTTP, bearer-token guard
  link.py              consent helper (console script nationwide-mcp-link)
  models.py            normalised shapes, minor units, London dates, stable ids
  errors.py            error codes
  token_store.py       file / env / memory / custom store
  providers/
    base.py            the Provider interface (read-only by contract)
    truelayer.py       TrueLayer Data API: accounts + cards (default)
    lunchflow.py       Lunch Flow personal API
    enablebanking.py   Enable Banking API
    statement.py       Nationwide CSV / OFX import
tests/                 mocked HTTP per provider, MCP in-process, both transports end to end
docs/research.md       dated notes on aggregators
examples/              synthetic sample statement for the quick start
```

## Licence

MIT. See [LICENSE](LICENSE). Provided as-is, without warranty of any kind.
