"""The documentation must keep up with the code: every setting documented, no stale names."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "\n".join(p.read_text(encoding="utf-8") for p in sorted((ROOT / "src").rglob("*.py")))
README = (ROOT / "README.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")

# Names that appear in the source as string literals and are read from the environment.
ENV_NAMES = sorted(
    set(re.findall(r'"((?:NATIONWIDE|TRUELAYER|LUNCHFLOW|ENABLEBANKING)_[A-Z0-9_]+)"', SOURCE))
    - {"NATIONWIDE_PROVIDER_ID"}  # a module constant in link.py, not a setting
)
REMOVED = ("MCP_TRANSPORT", "MCP_HOST", "MCP_PORT", "MCP_AUTH_TOKEN", "LOG_LEVEL", "TRUELAYER_REFRESH_TOKEN")


def test_every_setting_is_documented():
    assert len(ENV_NAMES) > 30
    missing_readme = [name for name in ENV_NAMES if f"`{name}`" not in README]
    missing_example = [name for name in ENV_NAMES if not re.search(rf"^#? ?{name}=", ENV_EXAMPLE, re.M)]
    assert missing_readme == [] and missing_example == []


def test_removed_generic_names_are_not_read_by_the_code():
    for name in REMOVED:
        assert not re.search(rf'"{name}"', SOURCE), name
    assert '"PORT"' not in SOURCE
    for line in ENV_EXAMPLE.splitlines():
        assert not line.startswith(tuple(f"{name}=" for name in REMOVED)), line


def test_error_codes_and_contract_notes_are_documented():
    from nationwide_mcp.errors import ERROR_CODES

    for code in ERROR_CODES:
        assert f"`[{code}]`" in README, code
    for needle in (
        "earliest_pending_date",
        "pending_excluded",
        "pending_supported",
        "balance_minor",
        "Error executing tool",
        "de-duplicate on `id`",
        "14 days",
        "${TRUELAYER_CLIENT_SECRET}",
    ):
        assert needle.lower() in README.lower(), needle


def test_no_inline_secrets_or_machine_specific_paths_in_the_docs():
    for text in (README, ENV_EXAMPLE, SOURCE):
        assert not re.search(r"[A-Z]:\\\\?[A-Za-z]+\\\\?mcp", text)
    assert not re.search(r'"TRUELAYER_CLIENT_SECRET":\s*"(?!\$\{)', README)
    assert not re.search(r"\b\d+ tests\b", README)  # no hard-coded test counts
