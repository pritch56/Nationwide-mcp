"""Normalisation helpers shared by all providers.

The transaction shape:

    {id, source, account_id, date, amount_minor, currency, description,
     merchant, category, pending, notes}

* ``amount_minor`` is a signed integer in minor units; negative = money out.
* ``date`` is a calendar date (YYYY-MM-DD) in Europe/London local time.
* ``id`` is deterministic, so fetching the same data again yields the same ids.
* ``notes`` is ``None`` when empty, never ``""``.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("nationwide_mcp.models")

SOURCE = "nationwide"
LONDON = ZoneInfo("Europe/London")

#: ISO 4217 minor-unit exponents for every currency whose exponent is NOT 2.
#: Anything absent from this table is treated as having 2 decimal places.
ISO4217_NON_DEFAULT_EXPONENTS: dict[str, int] = {
    # 0 decimal places
    **dict.fromkeys(
        (
            "BIF",
            "CLP",
            "DJF",
            "GNF",
            "ISK",
            "JPY",
            "KMF",
            "KRW",
            "PYG",
            "RWF",
            "UGX",
            "UYI",
            "VND",
            "VUV",
            "XAF",
            "XOF",
            "XPF",
        ),
        0,
    ),
    # 3 decimal places
    **dict.fromkeys(("BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"), 3),
    # 4 decimal places
    **dict.fromkeys(("CLF", "UYW"), 4),
}

_MAX_ABS_MINOR = 10**15  # far above any real balance; rejects 1e999-style input


def currency_exponent(currency: str | None) -> int:
    return ISO4217_NON_DEFAULT_EXPONENTS.get((currency or "GBP").upper(), 2)


def to_minor(amount: Any, currency: str | None = "GBP") -> int:
    """Convert a decimal amount (str / int / float / Decimal) to minor units.

    Floats go through ``str()`` first so 12.34 does not become 1233. Currency
    symbols, thousands separators and whitespace are tolerated. Anything that is
    not a finite, sanely sized number raises ``ValueError``.
    """
    if isinstance(amount, bool) or amount is None:
        raise ValueError(f"not a monetary amount: {amount!r}")
    if isinstance(amount, Decimal):
        dec = amount
    else:
        text = str(amount).strip().replace(",", "").replace("£", "").replace(" ", "")
        text = text[1:] if text.startswith("+") else text
        try:
            dec = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"not a monetary amount: {str(amount)[:40]!r}") from exc
    if not dec.is_finite():
        raise ValueError("not a finite monetary amount")
    if dec.adjusted() > 20:  # checked first: scaling 1e999999 would overflow the decimal context
        raise ValueError("monetary amount out of range")
    try:
        minor = (dec * (Decimal(10) ** currency_exponent(currency))).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    except ArithmeticError as exc:  # InvalidOperation, Overflow, ...
        raise ValueError("monetary amount out of range") from exc
    if abs(minor) > _MAX_ABS_MINOR:
        raise ValueError("monetary amount out of range")
    return int(minor)


def parse_date(value: Any) -> date | None:
    """Parse a plain ISO calendar date (``YYYY-MM-DD``) or date object. Used for
    tool arguments and date-only provider fields; timestamps go through
    :func:`london_date`."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None


_FRACTION = re.compile(r"(\.\d{6})\d+")


def london_date(value: Any) -> date | None:
    """Calendar date in Europe/London for a date or timestamp.

    A timestamp that carries a UTC offset (or ``Z``) is converted to London
    time first, so ``2026-06-30T23:30:00Z`` is 1 July. A date-only value, or a
    timestamp without an offset, is taken to be London local time already.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return value
    else:
        text = str(value).strip()
        if len(text) == 10:
            return parse_date(text)
        text = _FRACTION.sub(r"\1", text.replace("Z", "+00:00").replace("z", "+00:00"))
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.date()
    return parsed.astimezone(LONDON).date()


def today_london(clock: Callable[[], float] = time.time) -> date:
    return datetime.fromtimestamp(clock(), LONDON).date()


def today_utc(clock: Callable[[], float] = time.time) -> date:
    return datetime.fromtimestamp(clock(), UTC).date()


def clean(text: Any) -> str | None:
    """Collapse whitespace; return None for empty values."""
    if text is None:
        return None
    out = " ".join(str(text).split())
    return out or None


def _digest(*parts: Any) -> str:
    joined = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:24]


def transaction_id(account_id: str, provider_ref: str) -> str:
    """Id derived from a reference the bank/aggregator guarantees is stable."""
    return f"{SOURCE}_{_digest('ref', account_id, provider_ref)}"


def fallback_transaction_id(
    account_id: str, day: str, amount_minor: int, description: str | None, seq: int, extra: str = ""
) -> str:
    """Id derived from content, for sources with no usable reference.

    ``seq`` disambiguates genuinely identical transactions on the same day
    (two £3.00 coffees). ``pending`` is deliberately *not* part of the hash so
    an item keeps its id when it moves from pending to booked unchanged.
    """
    return f"{SOURCE}_{_digest('content', account_id, day, amount_minor, description or '', extra, seq)}"


def make_transaction(
    *,
    account_id: str,
    day: date,
    amount_minor: int,
    currency: str | None,
    description: str | None,
    merchant: str | None = None,
    category: str | None = None,
    pending: bool = False,
    notes: str | None = None,
    provider_ref: str | None = None,
    hash_extra: str = "",
) -> dict[str, Any]:
    """Build a normalised transaction. ``id`` is filled by :func:`finalise`."""
    desc = clean(description) or clean(merchant) or ""
    return {
        "id": None,
        "source": SOURCE,
        "account_id": str(account_id),
        "date": day.isoformat(),
        "amount_minor": int(amount_minor),
        "currency": (currency or "GBP").upper(),
        "description": desc,
        "merchant": clean(merchant),
        "category": clean(category),
        "pending": bool(pending),
        "notes": clean(notes),
        "_ref": clean(provider_ref),
        "_extra": hash_extra,
    }


def finalise(transactions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign stable ids, drop exact repeats, drop private keys, sort oldest first.

    Two items that share a provider reference are the same transaction only if
    date, amount and description also match (that happens when overlapping
    request windows return it twice). If they differ, both are kept: the first
    keeps the reference id, each later one gets a content-plus-sequence id and a
    warning is logged, because the provider's "unique" reference was not unique.
    """
    out: list[dict[str, Any]] = []
    by_ref_id: dict[str, list[tuple]] = {}
    counters: dict[tuple, int] = {}
    for txn in transactions:
        ref = txn.pop("_ref", None)
        extra = txn.pop("_extra", "")
        content = (txn["date"], txn["amount_minor"], txn["description"])
        if ref:
            ref_id = transaction_id(txn["account_id"], ref)
            seen = by_ref_id.setdefault(ref_id, [])
            if content in seen:
                continue  # the same transaction returned twice
            if seen:
                key = (txn["account_id"], "collision", ref, *content)
                seq = counters.get(key, 0)
                counters[key] = seq + 1
                txn["id"] = fallback_transaction_id(txn["account_id"], *content, seq, f"collision:{ref}")
                log.warning(
                    "provider reference shared by %d different transactions on %s; kept all with derived ids",
                    len(seen) + 1,
                    txn["date"],
                )
            else:
                txn["id"] = ref_id
            seen.append(content)
        else:
            key = (txn["account_id"], *content, extra)
            seq = counters.get(key, 0)
            counters[key] = seq + 1
            txn["id"] = fallback_transaction_id(txn["account_id"], *content, seq, extra)
        out.append(txn)
    out.sort(key=lambda t: (t["date"], t["id"]))
    return out


def in_window(day: date, since: date, before: date) -> bool:
    """Half-open window: since <= day < before."""
    return since <= day < before


def window_result(
    transactions: Iterable[dict[str, Any]],
    since: date,
    before: date,
    include_pending: bool,
    *,
    pending_supported: bool = True,
) -> dict[str, Any]:
    """Finalise ``transactions`` (booked AND pending, any dates), keep the window,
    and report what was left out.

    Returns ``{transactions, pending_supported, pending_excluded,
    earliest_pending_date}``. Ids are assigned before pending items are removed,
    so a booked item's id does not depend on ``include_pending``.
    """
    ordered = sorted(transactions, key=lambda t: t["pending"])  # booked first: stable sequence numbers
    kept = [t for t in finalise(ordered) if in_window(date.fromisoformat(t["date"]), since, before)]
    pending = [t for t in kept if t["pending"]]
    return {
        "transactions": kept if include_pending else [t for t in kept if not t["pending"]],
        "pending_supported": bool(pending_supported),
        "pending_excluded": 0 if include_pending else len(pending),
        "earliest_pending_date": min((t["date"] for t in pending), default=None),
    }


def last4(identifier: Any) -> str | None:
    digits = "".join(ch for ch in str(identifier or "") if ch.isascii() and ch.isalnum())
    return digits[-4:] if len(digits) >= 4 else None


def guess_account_type(*hints: Any) -> str | None:
    text = " ".join(str(h).lower() for h in hints if h)
    if not text:
        return None
    if any(k in text for k in ("card", "credit", "visa", "mastercard")) and "debit" not in text:
        return "credit_card"
    if any(k in text for k in ("sav", "isa", "svgs", "bond", "loyalty", "reward")):
        return "savings"
    if any(k in text for k in ("flex", "current", "cacc", "checking")):
        return "current"
    return "other"
