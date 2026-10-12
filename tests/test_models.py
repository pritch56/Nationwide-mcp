from datetime import date

import pytest

from nationwide_mcp.models import (
    ISO4217_NON_DEFAULT_EXPONENTS,
    currency_exponent,
    finalise,
    guess_account_type,
    london_date,
    make_transaction,
    to_minor,
    window_result,
)


@pytest.mark.parametrize(
    "value,currency,expected",
    [
        ("12.34", "GBP", 1234),
        (12.34, "GBP", 1234),  # float must not become 1233
        (-0.07, "GBP", -7),
        ("-1,234.50", "GBP", -123450),
        ("£5", "GBP", 500),
        (19.99, "GBP", 1999),
        ("100", "JPY", 100),
        ("1.005", "GBP", 101),  # half-up
    ],
)
def test_to_minor(value, currency, expected):
    assert to_minor(value, currency) == expected


def test_to_minor_rejects_garbage():
    with pytest.raises(ValueError):
        to_minor("abc")


def _txn(**kw):
    base = dict(account_id="a1", day=date(2026, 10, 5), amount_minor=-300, currency="GBP", description="COFFEE")
    base.update(kw)
    return make_transaction(**base)


def test_ids_are_deterministic_and_unique_for_identical_rows():
    first = finalise([_txn(), _txn()])
    second = finalise([_txn(), _txn()])
    assert [t["id"] for t in first] == [t["id"] for t in second]
    assert len({t["id"] for t in first}) == 2  # two identical coffees stay two rows
    assert all(t["id"].startswith("nationwide_") for t in first)
    assert "_ref" not in first[0] and "_extra" not in first[0]


def test_reference_based_id_ignores_content_and_drops_exact_repeats():
    a = finalise([_txn(provider_ref="R1", description="A"), _txn(provider_ref="R1", description="A")])
    assert len(a) == 1  # the same transaction returned twice (overlapping windows)
    b = finalise([_txn(provider_ref="R1", amount_minor=-999)])
    assert a[0]["id"] == b[0]["id"]


def test_fallback_id_survives_pending_to_booked():
    pending = finalise([_txn(pending=True)])[0]
    booked = finalise([_txn(pending=False)])[0]
    assert pending["id"] == booked["id"]


def test_shape_and_ordering():
    out = finalise([_txn(day=date(2026, 10, 7)), _txn(day=date(2026, 10, 1), merchant=" Tesco  Stores ")])
    assert [t["date"] for t in out] == ["2026-10-01", "2026-10-07"]
    assert set(out[0]) == {
        "id",
        "source",
        "account_id",
        "date",
        "amount_minor",
        "currency",
        "description",
        "merchant",
        "category",
        "pending",
        "notes",
    }
    assert out[0]["source"] == "nationwide" and out[0]["merchant"] == "Tesco Stores"
    assert out[0]["category"] is None and out[0]["notes"] is None and out[0]["pending"] is False


def test_guess_account_type():
    assert guess_account_type("FlexDirect") == "current"
    assert guess_account_type("Member Credit Card") == "credit_card"
    assert guess_account_type("Loyalty Saver") == "savings"
    assert guess_account_type(None) is None


@pytest.mark.parametrize(
    "currency,exponent,amount,minor",
    [
        ("GBP", 2, "12.34", 1234),
        ("HUF", 2, "1500.50", 150050),  # HUF has 2 decimals in ISO 4217 (it was wrongly treated as 0)
        ("JPY", 0, "1500", 1500),
        ("KRW", 0, "1500", 1500),
        ("VND", 0, "1500", 1500),
        ("CLP", 0, "1500", 1500),
        ("ISK", 0, "1500", 1500),
        ("XOF", 0, "1500", 1500),
        ("BHD", 3, "1.234", 1234),
        ("KWD", 3, "1.234", 1234),
        ("OMR", 3, "1.234", 1234),
        ("JOD", 3, "1.234", 1234),
        ("TND", 3, "1.234", 1234),
        ("usd", 2, "1.10", 110),
        (None, 2, "1.10", 110),
    ],
)
def test_iso_4217_minor_unit_exponents(currency, exponent, amount, minor):
    assert currency_exponent(currency) == exponent and to_minor(amount, currency) == minor
    assert "HUF" not in ISO4217_NON_DEFAULT_EXPONENTS


@pytest.mark.parametrize(
    "value", ["NaN", "Infinity", "-inf", "1e999999", "1e30", None, True, "", "12.3.4", float("nan")]
)
def test_to_minor_rejects_non_finite_and_absurd_amounts(value):
    with pytest.raises(ValueError):
        to_minor(value)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-06-30T22:59:59Z", date(2026, 6, 30)),
        ("2026-06-30T23:00:00Z", date(2026, 7, 1)),  # midnight BST
        ("2026-06-30T23:30:00+00:00", date(2026, 7, 1)),
        ("2026-07-01T00:30:00+01:00", date(2026, 7, 1)),
        ("2026-03-29T00:59:59Z", date(2026, 3, 29)),  # the hour before the clocks go forward
        ("2026-10-24T23:30:00Z", date(2026, 10, 25)),  # still BST the night before the clocks go back
        ("2026-10-25T23:30:00Z", date(2026, 10, 25)),  # GMT again
        ("2026-12-31T23:59:59Z", date(2026, 12, 31)),  # winter: London is UTC
        ("2026-12-31T23:59:59.1234567Z", date(2026, 12, 31)),  # 7-digit fractions
        ("2026-07-01T23:30:00", date(2026, 7, 1)),  # no offset: taken as London local time
        ("2026-07-01", date(2026, 7, 1)),
        ("01/07/2026", None),
        ("", None),
        (None, None),
    ],
)
def test_london_date(value, expected):
    assert london_date(value) == expected


def test_two_different_transactions_with_one_provider_id_are_both_kept(caplog):
    """-5.00 COFFEE and -7.50 LUNCH with the same reference: the second used to vanish."""
    rows = [
        _txn(provider_ref="R1", amount_minor=-500, description="COFFEE"),
        _txn(provider_ref="R1", amount_minor=-750, description="LUNCH"),
        _txn(provider_ref="R1", amount_minor=-750, description="LUNCH", day=date(2026, 10, 6)),
    ]
    with caplog.at_level("WARNING", logger="nationwide_mcp.models"):
        out = finalise(rows)
    assert sorted((t["date"], t["description"], t["amount_minor"]) for t in out) == [
        ("2026-10-05", "COFFEE", -500),
        ("2026-10-05", "LUNCH", -750),
        ("2026-10-06", "LUNCH", -750),
    ]
    assert len({t["id"] for t in out}) == 3
    assert sum("provider reference shared" in r.message for r in caplog.records) == 2
    first = finalise([_txn(provider_ref="R1", amount_minor=-500, description="COFFEE")])[0]
    assert first["id"] in {t["id"] for t in out}  # the first holder keeps the plain reference id


def _mixed():
    return [
        _txn(day=date(2026, 10, 5), provider_ref="a"),
        _txn(day=date(2026, 10, 6), pending=True, description="P1"),
        _txn(day=date(2026, 10, 7), pending=True, description="P2"),
        _txn(day=date(2026, 10, 9), pending=True, description="OUTSIDE"),
        _txn(day=date(2026, 10, 7), notes="   "),
    ]


def test_window_result_counts_what_was_left_out():
    out = window_result(_mixed(), date(2026, 10, 1), date(2026, 10, 8), include_pending=False)
    assert [t["pending"] for t in out["transactions"]] == [False, False]
    assert (out["pending_excluded"], out["earliest_pending_date"], out["pending_supported"]) == (2, "2026-10-06", True)
    assert all(t["notes"] is None for t in out["transactions"])  # blank notes become null, never ""
    out = window_result(_mixed(), date(2026, 10, 1), date(2026, 10, 8), include_pending=True)
    assert len(out["transactions"]) == 4 and out["pending_excluded"] == 0
    out = window_result(_mixed(), date(2026, 10, 1), date(2026, 10, 8), False, pending_supported=False)
    assert out["pending_supported"] is False
