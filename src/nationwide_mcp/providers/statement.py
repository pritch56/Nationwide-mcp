"""Statement-import provider: reads CSV / OFX files exported from Nationwide's
Internet Bank ("Download transactions") out of NATIONWIDE_STATEMENT_DIR.

No network, no credentials, no consent. One file = one account export; several
files for the same account (overlapping exports) are merged and de-duplicated.

CSV layout handled (header-driven, so column order does not matter):

    "Account Name:","FlexDirect ****1234"
    "Account Balance:","£1,234.56"
    "Available Balance: ","£1,234.56"

    "Date","Transaction type","Description","Paid out","Paid in","Balance"
    "09 Oct 2026","Visa purchase","TESCO STORES 1234","£12.34","","£1,222.22"

Credit-card exports ("Date","Transactions","Location","Paid out","Paid in")
and a generic signed "Amount" column are also accepted. The file is decoded as
UTF-8 with a Windows-1252 fallback.

UNVERIFIED: this layout was written from memory of the format, NOT from a real
export. Check the first import against your statement, in particular the sign
of a credit-card balance and of an overdrawn balance.

Safety: only regular files that resolve to a location inside the directory are
read (symlinks pointing elsewhere are refused), each file is capped at
NATIONWIDE_STATEMENT_MAX_BYTES, and parsed files are cached by
(path, mtime, size) so unchanged files are not parsed again.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..errors import ConfigError, MalformedImportError, NotFoundError, ProviderError
from ..models import SOURCE, clean, guess_account_type, last4, make_transaction, to_minor, window_result
from ._http import guard_malformed, strip_control
from .base import Provider

log = logging.getLogger("nationwide_mcp.statement")

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
MAX_FILES = 500
SUFFIXES = (".csv", ".ofx", ".qfx")
WHO = "The statement file"

_DATE_FORMATS = ("%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %y", "%d/%m/%y")
# Every pattern below is linear in the input: no nested or overlapping quantifiers.
_OFX_TAG = re.compile(r"<([A-Za-z0-9.]+)>([^<\r\n]*)")
_CR_DR = re.compile(r"(CR|DR)\.?$")
_SYMBOLS = re.compile(r"[\s£$€]|GBP")
_PLAIN_NUMBER = re.compile(r"[0-9][0-9,]*(?:\.[0-9]*)?|\.[0-9]+")


def _read_text(path: Path, max_bytes: int) -> str:
    with path.open("rb") as fh:
        raw = fh.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise MalformedImportError(
            f"{strip_control(path.name, 80)} is larger than the {max_bytes}-byte limit "
            "(NATIONWIDE_STATEMENT_MAX_BYTES). Export a shorter period."
        )
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


@lru_cache(maxsize=4096)
def _parse_day(text: str) -> date | None:
    value = " ".join(str(text).split())
    if not value or len(value) > 20:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _money(text: Any, currency: str = "GBP") -> int | None:
    """Signed minor units from a statement cell, or None for an empty / non-numeric cell.

    Negative forms understood: ``-£12.34``, ``£-12.34``, ``12.34-``, ``(12.34)``, ``12.34 DR``.
    A trailing ``CR`` means positive.
    """
    value = str(text or "").strip()
    if not value or len(value) > 40:
        return None
    negative = False
    marker = _CR_DR.search(value.upper())
    if marker:
        negative = marker.group(1) == "DR"
        value = value[: marker.start()].strip()
    if value.startswith("(") and value.endswith(")"):
        negative = True
        value = value[1:-1]
    value = _SYMBOLS.sub("", value)  # currency symbol and spaces; the sign survives
    if value.startswith("-") or value.endswith("-"):
        negative = True
    value = value.strip("-+")
    if not any(ch.isdigit() for ch in value):
        return None
    if not _PLAIN_NUMBER.fullmatch(value):  # e.g. 1e99 or 12.3.4: refuse, do not pick the digits out
        raise ValueError("not a plain decimal amount")
    minor = to_minor(value, currency)  # ValueError -> [malformed_import_file] in the caller
    return -minor if negative else minor


def _account_key(label: str) -> str:
    return "stmt_" + hashlib.sha256(label.strip().lower().encode("utf-8")).hexdigest()[:12]


@dataclass
class _ParsedFile:
    """Everything taken from one file. Immutable once built, so it can be cached."""

    file_name: str
    label: str
    name: str | None = None  # display-name override (OFX)
    mask: str | None = None
    currency: str = "GBP"
    has_balance: bool = False
    current_minor: int | None = None
    available_minor: int | None = None
    balance_as_of: str | None = None
    #: (fingerprint or None, transaction dict). A fingerprint de-duplicates a CSV row
    #: across overlapping exports; OFX rows carry a FITID instead.
    rows: list[tuple[tuple | None, dict[str, Any]]] = field(default_factory=list)


class _Account:
    def __init__(self, key: str, name: str):
        self.key = key
        self.name = name
        self.currency = "GBP"
        self.mask: str | None = None
        self.current_minor: int | None = None
        self.available_minor: int | None = None
        self.balance_as_of: str | None = None
        self.transactions: list[dict[str, Any]] = []
        self.files: list[str] = []
        self.seen: set[tuple] = set()


def _parse_csv(name: str, text: str, mtime: float) -> _ParsedFile:
    rows = [row for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]
    meta: dict[str, str] = {}
    header_at = None
    for index, row in enumerate(rows):
        cells = [c.strip().lower() for c in row]
        if "date" in cells and len(cells) >= 3:
            header_at = index
            break
        if len(row) >= 2:
            meta[row[0].strip().rstrip(":").strip().lower()] = row[1].strip()
    if header_at is None:
        raise MalformedImportError(f"{name}: no header row containing a 'Date' column; not a Nationwide CSV export?")

    label = clean(meta.get("account name")) or Path(name).stem
    parsed = _ParsedFile(file_name=name, label=label)
    key = _account_key(label)
    current = _money(meta.get("account balance"))
    if current is not None:
        parsed.has_balance = True
        parsed.current_minor = current
        parsed.available_minor = _money(meta.get("available balance"))
        parsed.balance_as_of = (
            datetime.fromtimestamp(mtime, UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        )

    header = [c.strip().lower() for c in rows[header_at]]

    def col(*names: str) -> int | None:
        for wanted in names:
            if wanted in header:
                return header.index(wanted)
        return None

    i_date = col("date")
    i_type = col("transaction type", "type")
    i_desc = col("description", "transactions", "transaction", "details")
    i_loc = col("location")
    i_out = col("paid out", "debit", "money out")
    i_in = col("paid in", "credit", "money in")
    i_amt = col("amount")
    i_bal = col("balance")

    def cell(row: list[str], index: int | None) -> str:
        return row[index].strip() if index is not None and index < len(row) else ""

    in_file: dict[tuple, int] = {}
    for row in rows[header_at + 1 :]:
        day = _parse_day(cell(row, i_date))
        if day is None:
            continue
        paid_out, paid_in = _money(cell(row, i_out)), _money(cell(row, i_in))
        if paid_out is not None or paid_in is not None:
            amount = abs(paid_in or 0) - abs(paid_out or 0)
        else:
            signed = _money(cell(row, i_amt))
            if signed is None:
                continue
            amount = signed
        kind = cell(row, i_type)
        description = cell(row, i_desc) or kind
        location = cell(row, i_loc)
        balance = _money(cell(row, i_bal))
        notes = "; ".join(
            x for x in (f"type: {kind}" if kind else "", f"location: {location}" if location else "") if x
        )
        # The running balance pins a row; the per-file occurrence number keeps
        # genuinely identical rows apart (card exports have no balance column).
        base = (day, amount, clean(description), kind, balance)
        occurrence = in_file.get(base, 0)
        in_file[base] = occurrence + 1
        extra = f"{kind}|{balance if balance is not None else ''}|{occurrence}"
        parsed.rows.append(
            (
                (day, amount, clean(description), extra),
                make_transaction(
                    account_id=key,
                    day=day,
                    amount_minor=amount,
                    currency=parsed.currency,
                    description=description,
                    merchant=None,
                    pending=False,
                    notes=notes or None,
                    hash_extra=extra,
                ),
            )
        )
    return parsed


def _ofx_section(text: str, upper: str, tag: str) -> dict[str, str]:
    """Tags inside the first <TAG> aggregate (bounded to 2000 characters: it only holds two fields)."""
    at = upper.find(f"<{tag}>")
    if at < 0:
        return {}
    return {k.upper(): v.strip() for k, v in _OFX_TAG.findall(text[at + len(tag) + 2 : at + 2000])[:2]}


def _parse_ofx(name: str, text: str) -> _ParsedFile:
    upper = text.upper()
    first: dict[str, str] = {}
    for tag, value in _OFX_TAG.findall(text[: 64 * 1024]):  # the statement header is at the top
        first.setdefault(tag.upper(), value.strip())
    if not first:
        raise MalformedImportError(f"{name}: no OFX tags found.")
    acct_id = first.get("ACCTID") or Path(name).stem
    mask = last4(acct_id)
    label = f"Nationwide ****{mask or acct_id}"
    parsed = _ParsedFile(file_name=name, label=label, mask=mask)
    parsed.currency = (first.get("CURDEF") or "GBP").upper()[:3]
    if first.get("ACCTTYPE"):
        parsed.name = f"{first['ACCTTYPE'].title()} ****{mask or ''}".strip()
    elif "<CREDITCARDMSGSRSV1>" in upper:
        parsed.name = f"Credit card ****{mask or ''}".strip()
    key = _account_key(label)

    ledger = _ofx_section(text, upper, "LEDGERBAL")
    if ledger.get("BALAMT"):
        parsed.has_balance = True
        parsed.current_minor = to_minor(ledger["BALAMT"], parsed.currency)
        as_of = _ofx_day(ledger.get("DTASOF", ""))
        parsed.balance_as_of = as_of.isoformat() if as_of else None
    available = _ofx_section(text, upper, "AVAILBAL")
    if available.get("BALAMT"):
        parsed.available_minor = to_minor(available["BALAMT"], parsed.currency)

    # Split on the opening tag (one linear pass) instead of a lazy regex per block.
    for block in upper_split(text, upper, "<STMTTRN>")[1:]:
        block = block[:4000]
        ends = [at for at in (block.upper().find(end) for end in ("</STMTTRN>", "</BANKTRANLIST>")) if at >= 0]
        if ends:
            block = block[: min(ends)]
        fields: dict[str, str] = {}
        for tag, value in _OFX_TAG.findall(block):
            fields.setdefault(tag.upper(), value.strip())
        day = _ofx_day(fields.get("DTPOSTED", ""))
        if day is None or not fields.get("TRNAMT"):
            continue
        payee, memo = clean(fields.get("NAME")), clean(fields.get("MEMO"))
        kind = clean(fields.get("TRNTYPE"))
        notes = "; ".join(x for x in (f"type: {kind}" if kind else "", memo if memo and memo != payee else "") if x)
        parsed.rows.append(
            (
                None,
                make_transaction(
                    account_id=key,
                    day=day,
                    amount_minor=to_minor(fields["TRNAMT"], parsed.currency),  # OFX amounts are already signed
                    currency=parsed.currency,
                    description=payee or memo or kind,
                    merchant=None,
                    pending=False,
                    notes=notes or None,
                    provider_ref=fields.get("FITID"),
                ),
            )
        )
    return parsed


def upper_split(text: str, upper: str, marker: str) -> list[str]:
    """``text.split(marker)`` ignoring case (``upper`` is ``text.upper()``, same length)."""
    if len(upper) != len(text):  # exotic characters changed length when upper-cased
        return re.split(re.escape(marker), text, flags=re.I)
    out, start = [], 0
    while True:
        at = upper.find(marker, start)
        if at < 0:
            out.append(text[start:])
            return out
        out.append(text[start:at])
        start = at + len(marker)


class StatementProvider(Provider):
    name = "statement"

    def __init__(self, directory: str | Path, max_bytes: int = DEFAULT_MAX_BYTES):
        if not directory:
            raise ConfigError("NATIONWIDE_STATEMENT_DIR is not set.")
        self._dir = Path(directory).expanduser()
        self._max_bytes = max(1, int(max_bytes))
        self._cache: dict[str, tuple[int, int, _ParsedFile]] = {}
        self._lock = threading.Lock()
        self.skipped: list[str] = []
        self.parse_count = 0  # how many files were actually parsed (tests, diagnostics)

    # -- loading ------------------------------------------------------------
    def _files(self) -> list[tuple[Path, os.stat_result]]:
        if not self._dir.is_dir():
            raise ConfigError(f"Statement directory {self._dir} does not exist.")
        root = self._dir.resolve()
        found: list[tuple[Path, os.stat_result]] = []
        skipped: list[str] = []
        for path in self._dir.iterdir():
            if path.suffix.lower() not in SUFFIXES:
                continue
            try:
                resolved = path.resolve(strict=True)
                info = resolved.stat()
            except OSError:
                skipped.append(path.name)
                continue
            # Refuse anything that is not a regular file inside the directory (symlinks out of it).
            if resolved.parent != root or not resolved.is_file():
                log.warning("ignoring %r: it resolves outside the statement directory", path.name)
                skipped.append(path.name)
                continue
            if info.st_size > self._max_bytes:
                raise MalformedImportError(
                    f"{strip_control(path.name, 80)} is larger than the {self._max_bytes}-byte limit "
                    "(NATIONWIDE_STATEMENT_MAX_BYTES). Export a shorter period."
                )
            found.append((resolved, info))
        if len(found) > MAX_FILES:
            raise MalformedImportError(f"More than {MAX_FILES} statement files in the directory; archive the old ones.")
        self.skipped = sorted(skipped)
        found.sort(key=lambda item: (item[1].st_mtime, item[0].name))
        return found

    def _parse(self, path: Path, info: os.stat_result) -> _ParsedFile:
        cached = self._cache.get(str(path))
        if cached and cached[:2] == (info.st_mtime_ns, info.st_size):
            return cached[2]
        name = strip_control(path.name, 80)
        try:
            text = _read_text(path, self._max_bytes)
            if path.suffix.lower() == ".csv":
                parsed = _parse_csv(name, text, info.st_mtime)
            else:
                parsed = _parse_ofx(name, text)
        except ProviderError:
            raise
        except OSError as exc:
            raise MalformedImportError(f"{name} could not be read ({type(exc).__name__}).") from None
        except (ValueError, ArithmeticError, csv.Error, IndexError) as exc:  # bad amounts, decimal overflow, bad CSV
            raise MalformedImportError(f"{name} could not be parsed ({type(exc).__name__}).") from None
        self.parse_count += 1
        self._cache[str(path)] = (info.st_mtime_ns, info.st_size, parsed)
        return parsed

    def _load(self) -> dict[str, _Account]:
        with self._lock:
            files = self._files()
            parsed_files = [self._parse(path, info) for path, info in files]
            for stale in set(self._cache) - {str(path) for path, _ in files}:
                del self._cache[stale]
        accounts: dict[str, _Account] = {}
        for parsed in parsed_files:  # oldest first, so the newest file's balance wins
            key = _account_key(parsed.label)
            account = accounts.get(key)
            if account is None:
                account = accounts[key] = _Account(key, parsed.label)
                if re.search(r"\d{4}", parsed.label):
                    account.mask = last4(re.sub(r"[^0-9]", "", parsed.label))
            account.files.append(parsed.file_name)
            account.currency = parsed.currency
            if parsed.mask:
                account.mask = parsed.mask
            if parsed.name:
                account.name = parsed.name
            if parsed.has_balance:
                account.current_minor = parsed.current_minor
                account.available_minor = parsed.available_minor
                account.balance_as_of = parsed.balance_as_of
            for fingerprint, txn in parsed.rows:
                if fingerprint is not None:
                    if fingerprint in account.seen:
                        continue  # same row already loaded from an overlapping export
                    account.seen.add(fingerprint)
                account.transactions.append(txn)
        return accounts

    # -- Provider API -------------------------------------------------------
    def _require(self, account_id: str) -> _Account:
        accounts = self._load()
        if not isinstance(account_id, str) or account_id not in accounts:
            raise NotFoundError("No statement files for that account. Call list_accounts.")
        return accounts[account_id]

    @guard_malformed(WHO)
    def list_accounts(self) -> list[dict[str, Any]]:
        return [
            {
                "id": a.key,
                "source": SOURCE,
                "name": clean(a.name) or "Nationwide account",
                "type": guess_account_type(a.name),
                "currency": a.currency,
                "institution": "Nationwide Building Society",
                "mask": a.mask,
            }
            for a in self._load().values()
        ]

    @guard_malformed(WHO)
    def get_balance(self, account_id: str) -> dict[str, Any]:
        account = self._require(account_id)
        balances = []
        for kind, value in (("current", account.current_minor), ("available", account.available_minor)):
            if value is not None:
                balances.append(
                    {"type": kind, "amount_minor": value, "currency": account.currency, "as_of": account.balance_as_of}
                )
        return {
            "account_id": account.key,
            "source": SOURCE,
            "currency": account.currency,
            "current_minor": account.current_minor,
            "available_minor": account.available_minor,
            "as_of": account.balance_as_of,
            "balances": balances,
            "notes": "Balance as printed in the most recent export file; it is only as fresh as that file.",
        }

    @guard_malformed(WHO)
    def fetch_transactions(
        self, account_id: str, since: date, before: date, include_pending: bool = False
    ) -> dict[str, Any]:
        account = self._require(account_id)
        # Exports contain booked items only: pending cannot be reported at all.
        return window_result(
            (dict(t) for t in account.transactions), since, before, include_pending, pending_supported=False
        )

    def connection_status(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "provider": self.name,
            "connected": False,
            "status": "NO_FILES",
            "consent_expires_at": None,
            "consent_expiry_is_estimate": False,
            "days_remaining": None,
            "needs_reauthorisation": False,
            "accounts": [],
            "message": "",
        }
        try:
            accounts = self._load()
        except ProviderError as exc:
            result["status"] = result["error_code"] = exc.code
            result["message"] = exc.message
            return result
        latest = None
        for account in accounts.values():
            days = [t["date"] for t in account.transactions]
            newest = max(days) if days else None
            latest = max(filter(None, [latest, newest]), default=None)
            result["accounts"].append({"id": account.key, "files": account.files, "latest_transaction_date": newest})
        if self.skipped:
            result["skipped_files"] = [strip_control(n, 80) for n in self.skipped]
        if accounts:
            result["connected"] = True
            result["status"] = "FILES_LOADED"
            result["latest_transaction_date"] = latest
            result["message"] = (
                f"File-import mode: no bank consent involved. Newest transaction on file is dated {latest}. "
                "Export a fresh CSV/OFX from the Nationwide Internet Bank to bring data up to date."
            )
        else:
            result["error_code"] = "not_connected"
            result["message"] = "No .csv/.ofx files found in the statement directory."
        return result


def _ofx_day(value: str) -> date | None:
    digits = re.sub(r"[^0-9]", "", value[:40])[:8]
    try:
        return datetime.strptime(digits, "%Y%m%d").date()
    except ValueError:
        return None
