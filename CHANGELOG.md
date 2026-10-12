# Changelog

All notable changes are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/)
and the project aims to follow [Semantic Versioning](https://semver.org/).

## 0.1.0 - unreleased

First public version. Alpha: tested against mocks and local fakes only, never against a live API.

### Tools

- `get_connection_status`, `list_accounts`, `get_balance`, `list_transactions`; all read-only.
- `list_transactions` reports `pending_supported`, `pending_excluded` and `earliest_pending_date`,
  echoes the effective window, defaults to the last 30 days ending tomorrow (Europe/London), and
  rejects ranges longer than `NATIONWIDE_MAX_RANGE_DAYS` (default 400).
- `get_balance` returns `balance_minor` (with `current_minor` as a duplicate).
- `get_connection_status` never raises for provider, authentication, configuration or rate-limit
  problems; `consent_expires_at` is a UTC timestamp ending in `Z`.
- Error codes: `consent_expired`, `token_expired`, `not_connected`, `rate_limited`, `sca_required`,
  `invalid_request`, `account_not_found`, `provider_error`, `config_error`, `token_store_error`,
  `malformed_import_file`, `internal_error`.

### Providers

- TrueLayer Data API (accounts and cards), Lunch Flow, Enable Banking, and CSV / OFX statement files.
- Dates are Europe/London calendar dates; ISO 4217 minor-unit exponents.
- TrueLayer: 88-day request windows overlapping by one day; a hard ceiling of 30 upstream requests
  per tool call; a token in the store always wins over the `NATIONWIDE_TRUELAYER_REFRESH_TOKEN` seed;
  rotated tokens survive a failed save.
- Two different transactions sharing a provider id are both kept.

### Link helper

- `nationwide-mcp-link check | link | status | extend | reset`, with PKCE and strict `state` checks.

### Configuration names

Transport settings use the `NATIONWIDE_MCP_` prefix. Generic names used by pre-release copies are
not read:

| Not read | Use |
|---|---|
| `MCP_TRANSPORT` | `NATIONWIDE_MCP_TRANSPORT` |
| `MCP_HOST` | `NATIONWIDE_MCP_HOST` |
| `MCP_PORT`, `PORT` | `NATIONWIDE_MCP_PORT` |
| `MCP_AUTH_TOKEN` | `NATIONWIDE_MCP_AUTH_TOKEN` |
| `LOG_LEVEL` | `NATIONWIDE_MCP_LOG_LEVEL` |
| `TRUELAYER_REFRESH_TOKEN` | `NATIONWIDE_TRUELAYER_REFRESH_TOKEN` |
