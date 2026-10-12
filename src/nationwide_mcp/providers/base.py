"""The provider interface every data source implements.

To add another aggregator: subclass :class:`Provider`, return the normalised
shapes documented below, raise the errors from ``nationwide_mcp.errors``, and
register the class in ``providers/__init__.py``. Providers are READ-ONLY by
contract: there is no method for moving money and none may be added.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any


class Provider(ABC):
    #: short machine name, e.g. "lunchflow"
    name: str = "base"

    @abstractmethod
    def list_accounts(self) -> list[dict[str, Any]]:
        """Return ``[{id, source, name, type, currency, institution, mask}]``.

        ``id`` and ``name`` are always present and non-empty. ``id`` should be
        stable across runs; see the README for what each provider guarantees
        across re-authorisations.
        """

    @abstractmethod
    def get_balance(self, account_id: str) -> dict[str, Any]:
        """Return ``{account_id, source, currency, current_minor,
        available_minor, as_of, balances: [{type, amount_minor, currency, as_of}]}``.
        The server adds ``balance_minor`` (= ``current_minor``).

        Sign convention: positive = the customer has money; negative =
        overdrawn / owed (a credit card with a £120 balance is -12000).
        """

    @abstractmethod
    def fetch_transactions(
        self, account_id: str, since: date, before: date, include_pending: bool = False
    ) -> dict[str, Any]:
        """Return ``{transactions, pending_supported, pending_excluded,
        earliest_pending_date}`` for the window ``since <= date < before``
        (London dates), oldest first. Build it with
        :func:`nationwide_mcp.models.window_result`.

        ``pending_supported`` is False when the source cannot report pending
        items at all. ``pending_excluded`` counts the in-window pending items
        left out because ``include_pending`` is false.
        """

    def list_transactions(
        self, account_id: str, since: date, before: date, include_pending: bool = False
    ) -> list[dict[str, Any]]:
        """Just the transactions of :meth:`fetch_transactions`."""
        return self.fetch_transactions(account_id, since, before, include_pending)["transactions"]

    @abstractmethod
    def connection_status(self) -> dict[str, Any]:
        """Return ``{provider, connected, status, consent_expires_at,
        days_remaining, needs_reauthorisation, message, ...}`` and, when not
        usable, ``error_code`` (one of ``nationwide_mcp.errors.ERROR_CODES``).

        Must not raise for an expired consent: report it instead.
        """

    def close(self) -> None:  # optional
        return None
