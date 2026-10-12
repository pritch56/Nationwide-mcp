# Contributing

Thanks for helping. This is a small project; a short issue describing the problem is the best start.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

`uv run ruff format .` applies the formatting. All three checks must pass before a change is merged.

## Rules

- **No real credentials or bank data, anywhere.** Not in tests, fixtures, issues, pull requests,
  logs or screenshots. That includes client secrets, refresh and access tokens, API keys, private
  keys, account numbers, sort codes, real transaction descriptions and real statement files. Use
  obviously made-up values.
- **Tests must not touch the network** or need an account with any provider. Mock HTTP with
  `httpx.MockTransport` (see `tests/conftest.py`) or the local fake server in `tests/test_e2e.py`.
- **The server stays read-only.** No tool or provider method that moves money or changes anything
  at the bank or the aggregator will be accepted.
- **Every behaviour change needs a test** that fails without it.
- Keep the shared shapes stable: the tool results, the error codes, and the token-store interface
  (`load()`, `save()`, `get()`, `put()` and the stored document layout). Adding optional fields is
  fine; renaming or removing is a breaking change.
- If you have run the code against a real provider and can confirm or correct one of the README's
  "Unverified assumptions", say exactly what you observed (with any identifying data removed).

## Style

- Line length 120, formatted and linted by ruff (configuration in `pyproject.toml`).
- Errors raised to callers are `ProviderError` subclasses from `nationwide_mcp/errors.py` with a
  message that says what to do and contains no secrets.
- Third-party text that is echoed to a terminal or an error message goes through `strip_control`.
