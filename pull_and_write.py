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
from sheets import open_sheet, write_data, write_cash
from github_secret import update_repo_secret

ERROR_LOG = "qbo_error.log"


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

    # Persist the rotated refresh token so the next run can authenticate.
    if qbo.new_refresh_token and qbo.new_refresh_token != os.environ["QBO_REFRESH_TOKEN"]:
        update_repo_secret(
            os.environ["GH_REPO"], "QBO_REFRESH_TOKEN",
            qbo.new_refresh_token, os.environ["GH_PAT"],
        )
        print("Rotated QBO_REFRESH_TOKEN secret.")

    print(f"Wrote {len(rows)} transactions across {len(accounts)} bank accounts; "
          f"cash on hand = ${balance:,.0f}")


if __name__ == "__main__":
    main()
