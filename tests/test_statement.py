from datetime import date

import pytest

from nationwide_mcp.errors import ConfigError, MalformedImportError, NotFoundError
from nationwide_mcp.providers.statement import StatementProvider

POUND = "£"

CSV_WEEK_1 = f'''"Account Name:","FlexDirect ****5678"
"Account Balance:","{POUND}1,222.22"
"Available Balance: ","{POUND}1,722.22"

"Date","Transaction type","Description","Paid out","Paid in","Balance"
"01 Oct 2026","Bank credit","SALARY ACME LTD","","{POUND}1,500.00","{POUND}1,600.00"
"03 Oct 2026","Visa purchase","COFFEE SHOP","{POUND}3.00","","{POUND}1,597.00"
"03 Oct 2026","Visa purchase","COFFEE SHOP","{POUND}3.00","","{POUND}1,594.00"
"05 Oct 2026","Direct debit","COUNCIL TAX","{POUND}150.00","","{POUND}1,444.00"
'''

CSV_WEEK_2 = f'''"Account Name:","FlexDirect ****5678"
"Account Balance:","{POUND}1,431.66"
"Available Balance: ","{POUND}1,931.66"

"Date","Transaction type","Description","Paid out","Paid in","Balance"
"05 Oct 2026","Direct debit","COUNCIL TAX","{POUND}150.00","","{POUND}1,444.00"
"09 Oct 2026","Visa purchase","TESCO STORES 1234","{POUND}12.34","","{POUND}1,431.66"
'''

CARD_CSV = f'''"Account Name:","Member Credit Card ****4321"

"Date","Transactions","Location","Paid out","Paid in"
"04 Oct 2026","AMAZON UK","LONDON","{POUND}20.00",""
"04 Oct 2026","AMAZON UK","LONDON","{POUND}20.00",""
"06 Oct 2026","PAYMENT RECEIVED","","","{POUND}100.00"
'''

OFX = """OFXHEADER:100
DATA:OFXSGML
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>
<CURDEF>GBP
<BANKACCTFROM><BANKID>000000<ACCTID>0000009999<ACCTTYPE>SAVINGS</BANKACCTFROM>
<BANKTRANLIST>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20261002000000<TRNAMT>250.00<FITID>F001<NAME>TRANSFER FROM FLEX<MEMO>Monthly saving</STMTTRN>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261006<TRNAMT>-40.00<FITID>F002<NAME>WITHDRAWAL</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>5210.00<DTASOF>20261009</LEDGERBAL>
<AVAILBAL><BALAMT>5200.00<DTASOF>20261009</AVAILBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""


@pytest.fixture
def folder(tmp_path):
    (tmp_path / "flex-week1.csv").write_bytes(CSV_WEEK_1.encode("cp1252"))  # Nationwide's real encoding
    (tmp_path / "flex-week2.csv").write_bytes(CSV_WEEK_2.encode("cp1252"))
    (tmp_path / "card.csv").write_text(CARD_CSV, encoding="utf-8")
    (tmp_path / "savings.ofx").write_text(OFX, encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    return tmp_path


def by_name(provider):
    return {a["name"]: a for a in provider.list_accounts()}


def test_accounts_discovered_from_files(folder):
    accounts = by_name(StatementProvider(folder))
    assert set(accounts) == {"FlexDirect ****5678", "Member Credit Card ****4321", "Savings ****9999"}
    assert accounts["FlexDirect ****5678"]["type"] == "current" and accounts["FlexDirect ****5678"]["mask"] == "5678"
    assert accounts["Member Credit Card ****4321"]["type"] == "credit_card"
    assert accounts["Savings ****9999"]["type"] == "savings"
    assert all(a["source"] == "nationwide" and a["currency"] == "GBP" for a in accounts.values())


def test_csv_signs_dates_and_overlapping_exports_dedupe(folder):
    provider = StatementProvider(folder)
    account_id = by_name(provider)["FlexDirect ****5678"]["id"]
    txns = provider.list_transactions(account_id, date(2026, 10, 1), date(2026, 10, 11))
    assert [(t["date"], t["amount_minor"], t["description"]) for t in txns if t["description"] != "COFFEE SHOP"] == [
        ("2026-10-01", 150000, "SALARY ACME LTD"),
        ("2026-10-05", -15000, "COUNCIL TAX"),  # present in both files, returned once
        ("2026-10-09", -1234, "TESCO STORES 1234"),
    ]
    coffees = [t for t in txns if t["description"] == "COFFEE SHOP"]
    assert len(coffees) == 2 and coffees[0]["id"] != coffees[1]["id"]  # identical rows kept apart
    assert all(t["pending"] is False and t["merchant"] is None and t["category"] is None for t in txns)
    assert txns[0]["notes"] == "type: Bank credit"
    assert all(isinstance(t["amount_minor"], int) for t in txns)


def test_ids_stable_across_runs_and_across_files(folder, tmp_path_factory):
    provider = StatementProvider(folder)
    account_id = by_name(provider)["FlexDirect ****5678"]["id"]
    first = provider.list_transactions(account_id, date(2026, 10, 1), date(2026, 10, 11))
    again = StatementProvider(folder).list_transactions(account_id, date(2026, 10, 1), date(2026, 10, 11))
    assert [t["id"] for t in first] == [t["id"] for t in again]

    # Only the second export present: the council tax row must keep the same id.
    other = tmp_path_factory.mktemp("only-week2")
    (other / "export.csv").write_bytes(CSV_WEEK_2.encode("cp1252"))
    alone = StatementProvider(other).list_transactions(account_id, date(2026, 10, 1), date(2026, 10, 11))
    ids = {t["description"]: t["id"] for t in first}
    assert {t["description"]: t["id"] for t in alone} == {k: ids[k] for k in ("COUNCIL TAX", "TESCO STORES 1234")}


def test_window_is_half_open(folder):
    provider = StatementProvider(folder)
    account_id = by_name(provider)["FlexDirect ****5678"]["id"]
    txns = provider.list_transactions(account_id, date(2026, 10, 5), date(2026, 10, 9))
    assert [t["description"] for t in txns] == ["COUNCIL TAX"]  # 9 Oct excluded


def test_credit_card_csv_without_balance_column(folder):
    provider = StatementProvider(folder)
    account_id = by_name(provider)["Member Credit Card ****4321"]["id"]
    txns = provider.list_transactions(account_id, date(2026, 10, 1), date(2026, 10, 11))
    assert [t["amount_minor"] for t in txns] == [-2000, -2000, 10000]
    assert len({t["id"] for t in txns}) == 3
    assert txns[0]["notes"] == "location: LONDON"
    assert provider.get_balance(account_id)["current_minor"] is None


def test_balance_comes_from_newest_csv(folder):
    provider = StatementProvider(folder)
    account_id = by_name(provider)["FlexDirect ****5678"]["id"]
    import os

    os.utime(folder / "flex-week1.csv", (1_000_000_000, 1_000_000_000))  # make week 1 clearly older
    balance = provider.get_balance(account_id)
    assert balance["current_minor"] == 143166 and balance["available_minor"] == 193166


def test_ofx_parsing(folder):
    provider = StatementProvider(folder)
    account_id = by_name(provider)["Savings ****9999"]["id"]
    txns = provider.list_transactions(account_id, date(2026, 10, 1), date(2026, 10, 11))
    assert [(t["date"], t["amount_minor"], t["description"]) for t in txns] == [
        ("2026-10-02", 25000, "TRANSFER FROM FLEX"),
        ("2026-10-06", -4000, "WITHDRAWAL"),
    ]
    assert "Monthly saving" in txns[0]["notes"]
    balance = provider.get_balance(account_id)
    assert balance["current_minor"] == 521000 and balance["available_minor"] == 520000
    assert balance["as_of"] == "2026-10-09"


def test_status_and_errors(folder, tmp_path_factory):
    status = StatementProvider(folder).connection_status()
    assert status["connected"] is True and status["latest_transaction_date"] == "2026-10-09"
    assert status["needs_reauthorisation"] is False and status["consent_expires_at"] is None

    empty = tmp_path_factory.mktemp("empty")
    assert StatementProvider(empty).connection_status()["status"] == "NO_FILES"
    assert StatementProvider(empty / "missing").connection_status()["status"] == "config_error"
    with pytest.raises(ConfigError):
        StatementProvider("")
    with pytest.raises(NotFoundError):
        StatementProvider(folder).get_balance("stmt_unknown")

    bad = tmp_path_factory.mktemp("bad")
    (bad / "weird.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(MalformedImportError) as info:
        StatementProvider(bad).list_accounts()
    assert str(info.value).startswith("[malformed_import_file] ")
    assert StatementProvider(bad).connection_status()["status"] == "malformed_import_file"
