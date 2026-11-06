from pull_and_write import panels_cash_rows


def _t(account, typ, amount):
    return {"account": account, "txn_type": typ, "amount": amount,
            "date": "2026-03-01", "name": "", "memo": ""}


def test_panels_cash_rows_drops_je_and_adds_funding():
    pl = [_t("5400 Panelist incentives", "Journal Entry", "-500"),
          _t("5400 Panelist incentives", "Expense", "100"),
          _t("6010 Wages", "Journal Entry", "50")]
    gl = [_t("1420", "Transfer", "2000"), _t("1420", "Journal Entry", "-1800")]
    out = panels_cash_rows(pl, gl)
    assert len(out) == 3
    assert not any(t["txn_type"] == "Journal Entry" and t["account"].startswith("5400") for t in out)
    assert any(t["account"].startswith("1420") and t["amount"] == "2000" for t in out)
    assert any(t["account"].startswith("6010") for t in out)


def test_deliver_open_balance_dated_at_payment_month():
    import datetime as dt
    from pull_and_write import deliver_gross_rows, _is_deliver_row
    bills = [{"TxnDate": "2026-09-30", "TotalAmt": 104000, "Balance": 14000, "Line": [
        {"Amount": 104000, "Description": "Q3+Q4",
         "AccountBasedExpenseLineDetail": {"AccountRef": {"name": "6000 Payroll expenses"}}}]}]
    invs = [{"TxnDate": "2026-11-05", "TotalAmt": 22500, "Balance": 0, "Line": [
        {"Amount": 22500, "DetailType": "SalesItemLineDetail"},
        {"Amount": 22500, "DetailType": "SubTotalLineDetail"}]}]
    rows, payable = deliver_gross_rows(bills, invs, "2026-01-01", "2026-12-31",
                                       today=dt.date(2026, 11, 6))
    assert payable == 14000
    got = sorted((r["date"], r["amount"]) for r in rows)
    assert got == [("2026-09-30", "90000.0"), ("2026-11-05", "22500.0"),
                   ("2026-11-30", "14000.0")]
    assert _is_deliver_row({"account": "6000 Payroll", "name": "Deliver Strategies, LLC"})
    assert not _is_deliver_row({"account": "6000 Payroll", "name": "Gusto"})
