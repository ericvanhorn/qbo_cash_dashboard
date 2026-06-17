"""Minimal QuickBooks Online client for the cash dashboard pull.

Pulls a cash-basis Profit & Loss Detail report (transaction-level) and the
current bank balances. Handles OAuth2 refresh-token rotation.
"""
import base64
import requests

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
PROD_BASE = "https://quickbooks.api.intuit.com"
SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"


class QBO:
    def __init__(self, client_id, client_secret, refresh_token, realm_id,
                 env="production", minorversion="73"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.realm_id = realm_id
        self.minorversion = minorversion
        self.base = PROD_BASE if env == "production" else SANDBOX_BASE
        self.access_token = None
        # After refresh(), this holds the latest refresh token (QBO rotates it).
        self.new_refresh_token = refresh_token

    def refresh(self):
        """Exchange the refresh token for a fresh access token."""
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        r = requests.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
            timeout=30,
        )
        r.raise_for_status()
        tok = r.json()
        self.access_token = tok["access_token"]
        # QBO returns a (sometimes new) refresh token on every refresh. Persist it.
        self.new_refresh_token = tok.get("refresh_token", self.refresh_token)

    def _get(self, path, params):
        r = requests.get(
            f"{self.base}{path}",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json",
            },
            params={**params, "minorversion": self.minorversion},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()

    def pl_detail(self, start_date, end_date, method="Cash"):
        """Profit & Loss Detail report — transaction-level rows, cash basis."""
        return self._get(
            f"/v3/company/{self.realm_id}/reports/ProfitAndLossDetail",
            {"accounting_method": method, "start_date": start_date, "end_date": end_date},
        )

    def bank_balance(self):
        """Sum of CurrentBalance across all Bank-type accounts = cash on hand.

        This is QBO's register balance (what you reconcile against), which is
        the right 'cash on hand' figure for the dashboard.
        """
        j = self._get(
            f"/v3/company/{self.realm_id}/query",
            {"query": "select * from Account where AccountType = 'Bank'"},
        )
        accounts = j.get("QueryResponse", {}).get("Account", [])
        total = sum(float(a.get("CurrentBalance", 0) or 0) for a in accounts)
        return total, accounts


def parse_pl_detail(report):
    """Flatten a P&L Detail report into transaction dicts.

    The Reports API returns a nested tree: Section rows carry an account name in
    their Header and contain child Rows; Data rows carry the actual transactions
    in ColData. We walk the tree, tracking the current account from the nearest
    enclosing section header, and emit one dict per Data row.
    """
    columns = [c.get("ColTitle", "") for c in report.get("Columns", {}).get("Column", [])]
    idx = {title: i for i, title in enumerate(columns)}

    def cell(coldata, *titles):
        for t in titles:
            i = idx.get(t)
            if i is not None and i < len(coldata):
                return coldata[i].get("value", "")
        return ""

    out = []

    def walk(rows_obj, account):
        for row in rows_obj.get("Row", []):
            acct = account
            header = row.get("Header")
            if header and header.get("ColData"):
                hv = header["ColData"][0].get("value", "")
                if hv:
                    acct = hv
            # Recurse into nested sections first.
            if row.get("Rows"):
                walk(row["Rows"], acct)
            # Emit data rows.
            cd = row.get("ColData")
            if cd and row.get("type") != "Section":
                out.append({
                    "date": cell(cd, "Date"),
                    "txn_type": cell(cd, "Transaction Type", "Transaction type"),
                    "name": cell(cd, "Name"),
                    "memo": cell(cd, "Memo/Description", "Memo", "Description"),
                    # Prefer the section account; fall back to an Account column.
                    "account": acct or cell(cd, "Account"),
                    "amount": cell(cd, "Amount"),
                })

    walk(report.get("Rows", {}), None)
    return out
