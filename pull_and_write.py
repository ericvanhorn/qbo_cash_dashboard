"""Pull cash-basis QBO actuals + bank balance, categorize, write to the dashboard.

Run weekly by GitHub Actions. Full-refresh each run (no incremental/dedup state)
because the data is small and a clean rewrite is the most robust approach.

On failure, a structured error log is written to qbo_error.log; the workflow
uploads it as a build artifact so it's retrievable after the console ages out.
"""
import csv
import datetime as dt
import json
import os
import re
import sys
import traceback

from qbo import QBO, QBOAuthError, parse_pl_detail
from sheets import open_sheet, write_data, write_cash, write_refreshed
from github_secret import update_repo_secret

ERROR_LOG = "qbo_error.log"

# Panelist incentives are paid via Tremendous. Cash leaves the bank when we fund
# the prepaid asset (1420); the monthly journal entry that moves prepaid -> expense
# (5400) is a reconciliation, not cash. So Panels = non-JE activity in 1420, plus
# any non-JE postings to the Panels expense accounts.
PREPAID_PANELS_ACCT = "1420"
JE_TYPES = {"journal entry"}


# Deliver Strategies (a.k.a. 2017 Strategies) is both our payroll provider (bills,
# 6000) and a subscriber (invoices, 4100). The two are netted by journal entry, so on
# a cash basis QBO books everything in one lump on the day of the offset. We instead
# recognize both sides gross on the bill / invoice date and report the net still
# owed to Deliver separately (it is subtracted from cash on hand).
DELIVER_VENDOR_LIKE = "Deliver Strategies%"
DELIVER_CUSTOMER_LIKE = "2017 Strategies%"
DELIVER_NAMES = ("deliver strategies", "2017 strategies")
DELIVER_ACCTS = ("6000", "4100")


def _is_deliver_row(t):
    name = (t.get("name") or "").lower()
    return acct4(t["account"]) in DELIVER_ACCTS and any(n in name for n in DELIVER_NAMES)


def _month_end(d):
    nxt = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return nxt - dt.timedelta(days=1)


def _split_dates(doc, today):
    """(paid_fraction, paid_date, open_fraction, open_date) for a bill/invoice.

    The settled part is dated on the document; any still-open balance is dated when
    the cash will actually move: the due date if it is still ahead, else month-end.
    """
    total = float(doc.get("TotalAmt", 0) or 0)
    bal = float(doc.get("Balance", 0) or 0)
    f_open = (bal / total) if total else 0.0
    due = doc.get("DueDate") or ""
    open_date = due if due and due >= today.isoformat() else _month_end(today).isoformat()
    return 1 - f_open, doc.get("TxnDate", ""), f_open, open_date


def deliver_gross_rows(bills, invoices, start, end, today=None):
    """Gross P&L rows for Deliver bills (payroll, 6000) and invoices (revenue, 4100).

    Settled amounts are dated on the bill/invoice; open balances are dated when the
    cash will move (see _split_dates). Returns (rows, payable) where payable is open
    bills minus open invoices (informational; positive = we owe Deliver).
    """
    today = today or dt.date.today()
    rows, payable = [], 0.0

    def emit(doc, line_amt, base):
        f_paid, d_paid, f_open, d_open = _split_dates(doc, today)
        for frac, d in ((f_paid, d_paid), (f_open, d_open)):
            amt = round(line_amt * frac, 2)
            if amt and start <= d <= end:
                rows.append(dict(base, date=d, amount=str(amt)))

    for b in bills:
        payable += float(b.get("Balance", 0) or 0)
        for ln in b.get("Line", []):
            det = ln.get("AccountBasedExpenseLineDetail")
            if not det:
                continue
            emit(b, float(ln.get("Amount", 0)), {
                "txn_type": "Bill", "name": "Deliver Strategies, LLC",
                "memo": ln.get("Description", ""),
                "account": det.get("AccountRef", {}).get("name", "6000 Payroll expenses")})
    for inv in invoices:
        payable -= float(inv.get("Balance", 0) or 0)
        for ln in inv.get("Line", []):
            if ln.get("DetailType") != "SalesItemLineDetail":
                continue
            emit(inv, float(ln.get("Amount", 0)), {
                "txn_type": "Invoice", "name": "2017 Strategies, LLC",
                "memo": ln.get("Description", ""), "account": "4100 Services"})
    return rows, payable


def is_je(t):
    return (t.get("txn_type") or "").strip().lower() in JE_TYPES


def panels_cash_rows(pl_txns, gl_txns):
    """Return P&L txns with Panels journal entries removed, plus 1420 funding rows.

    Funding rows are everything in 1420 that is NOT a journal entry (transfers,
    checks, deposits/refunds). Positive = cash out to Tremendous.
    """
    kept = [t for t in pl_txns
            if not (acct4(t["account"]) in ("5200", "5300", "5400") and is_je(t))]
    funding = [dict(t, account=f"{PREPAID_PANELS_ACCT} Prepaid panelist incentives")
               for t in gl_txns if not is_je(t)]
    return kept + funding


def load_map(path):
    """acct4 -> (bucket, line) from bucket_map.csv."""
    out = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            out[row["acct4"].strip()] = (row["bucket"].strip(), row["line"].strip())
    return out


def acct4(account):
    m = re.match(r"\s*(\d{4})", account or "")
    return m.group(1) if m else None


def classify(account, name, memo, bmap):
    """Tag a transaction with (bucket, line), mirroring the Map tab.

    6090 (contract labor) splits by counterparty: Mongoose is the CEO and gets
    his own line; everyone else rolls into 'Other contractors'.
    """
    a = acct4(account)
    if a == "6090":
        if "mongoose" in f"{name} {memo}".lower():
            return ("People", "Mongoose (CEO, contract)")
        return ("People", "Other contractors")
    if a in bmap:
        return bmap[a]
    return ("Overhead", "Overhead")  # safe default for any unmapped account


def to_float(x):
    try:
        return float(str(x).replace(",", "").replace("$", ""))
    except (ValueError, TypeError):
        return None


def _write_error_log(context, exc, intuit_tid=None):
    """Write a structured error entry to ERROR_LOG for artifact upload."""
    entry = {
        "timestamp": dt.datetime.utcnow().isoformat() + "Z",
        "context": context,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "intuit_tid": intuit_tid,
        "traceback": traceback.format_exc(),
    }
    # Include HTTP details if available on the exception.
    response = getattr(exc, "response", None)
    if response is not None:
        entry["http_status"] = response.status_code
        entry["http_url"] = response.url
        try:
            entry["http_body"] = response.json()
        except Exception:
            entry["http_body"] = response.text[:2000]

    with open(ERROR_LOG, "a") as f:
        f.write(json.dumps(entry, indent=2) + "\n")


def main():
    qbo = QBO(
        client_id=os.environ["QBO_CLIENT_ID"],
        client_secret=os.environ["QBO_CLIENT_SECRET"],
        refresh_token=os.environ["QBO_REFRESH_TOKEN"],
        realm_id=os.environ["QBO_REALM_ID"],
        env=os.environ.get("QBO_ENV", "production"),
    )

    try:
        qbo.refresh()
    except QBOAuthError as e:
        _write_error_log("token_refresh", e, qbo.last_intuit_tid)
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        _write_error_log("token_refresh", e, qbo.last_intuit_tid)
        print(f"FATAL: token refresh failed — see {ERROR_LOG}", file=sys.stderr)
        sys.exit(1)

    year = dt.date.today().year
    start = f"{year}-01-01"
    end = dt.date.today().isoformat()

    try:
        report = qbo.pl_detail(start, end, method="Cash")
    except QBOAuthError as e:
        _write_error_log("pl_detail", e, qbo.last_intuit_tid)
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        _write_error_log("pl_detail", e, qbo.last_intuit_tid)
        print(f"FATAL: P&L Detail fetch failed — see {ERROR_LOG}", file=sys.stderr)
        sys.exit(1)

    txns = parse_pl_detail(report)

    try:
        acct_id = qbo.account_id(PREPAID_PANELS_ACCT)
        if acct_id is None:
            raise RuntimeError(f"Account {PREPAID_PANELS_ACCT} not found in QBO")
        gl = parse_pl_detail(qbo.general_ledger(acct_id, start, end, method="Cash"))
    except QBOAuthError as e:
        _write_error_log("general_ledger", e, qbo.last_intuit_tid)
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        _write_error_log("general_ledger", e, qbo.last_intuit_tid)
        print(f"FATAL: General Ledger fetch failed — see {ERROR_LOG}", file=sys.stderr)
        sys.exit(1)
    txns = panels_cash_rows(txns, gl)

    try:
        vids = [v["Id"] for v in qbo.query_all("Vendor", f"DisplayName like '{DELIVER_VENDOR_LIKE}'")]
        cids = [c["Id"] for c in qbo.query_all("Customer", f"DisplayName like '{DELIVER_CUSTOMER_LIKE}'")]
        bills = [b for v in vids for b in qbo.query_all("Bill", f"VendorRef = '{v}'")]
        invoices = [i for c in cids for i in qbo.query_all("Invoice", f"CustomerRef = '{c}'")]
    except QBOAuthError as e:
        _write_error_log("deliver", e, qbo.last_intuit_tid)
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        _write_error_log("deliver", e, qbo.last_intuit_tid)
        print(f"FATAL: Deliver bill/invoice fetch failed — see {ERROR_LOG}", file=sys.stderr)
        sys.exit(1)
    deliver_payable = 0.0
    if not (vids and cids):
        print("WARNING: Deliver vendor or customer not found; skipping gross-up.",
              file=sys.stderr)
    else:
        txns = [t for t in txns if not _is_deliver_row(t)]
        d_rows, deliver_payable = deliver_gross_rows(bills, invoices, start, end)
        txns += d_rows
    bmap = load_map(os.path.join(os.path.dirname(__file__), "bucket_map.csv"))

    rows = []
    for t in txns:
        amount = to_float(t["amount"])
        if amount is None or not t["date"]:
            continue
        bucket, line = classify(t["account"], t["name"], t["memo"], bmap)
        try:
            label = dt.date.fromisoformat(t["date"]).strftime("%b'%y")  # e.g. Jun'26
        except ValueError:
            continue
        rows.append([t["date"], label, bucket, line, t["name"], amount])

    try:
        balance, accounts = qbo.bank_balance()
    except QBOAuthError as e:
        _write_error_log("bank_balance", e, qbo.last_intuit_tid)
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        _write_error_log("bank_balance", e, qbo.last_intuit_tid)
        print(f"FATAL: bank balance fetch failed — see {ERROR_LOG}", file=sys.stderr)
        sys.exit(1)

    sheet = open_sheet(json.loads(os.environ["GOOGLE_SA_KEY"]), os.environ["SHEET_ID"])
    write_data(sheet, rows)
    write_cash(sheet, balance)
    write_refreshed(sheet)

    # Persist the rotated refresh token so the next run can authenticate.
    if qbo.new_refresh_token and qbo.new_refresh_token != os.environ["QBO_REFRESH_TOKEN"]:
        update_repo_secret(
            os.environ["GH_REPO"], "QBO_REFRESH_TOKEN",
            qbo.new_refresh_token, os.environ["GH_PAT"],
        )
        print("Rotated QBO_REFRESH_TOKEN secret.")

    print(f"Wrote {len(rows)} transactions across {len(accounts)} bank accounts; "
          f"cash on hand = ${balance:,.0f}; open Deliver net payable = "
          f"${deliver_payable:,.0f} (booked in its payment month)")


if __name__ == "__main__":
    main()
