"""Minimal QuickBooks Online client for the cash dashboard pull.

Pulls a cash-basis Profit & Loss Detail report (transaction-level) and the
current bank balances. Handles OAuth2 refresh-token rotation.
"""
import base64
import time
import requests

# Intuit's discovery document — endpoint URLs are fetched from here at startup
# rather than hardcoded, so we're safe if Intuit ever rotates them.
DISCOVERY_URL = "https://developer.api.intuit.com/.well-known/openid_configuration"
SANDBOX_DISCOVERY_URL = "https://developer.api.intuit.com/.well-known/openid_sandbox_configuration"

# Fallback values used only if the discovery fetch fails entirely.
_FALLBACK_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
_FALLBACK_PROD_BASE = "https://quickbooks.api.intuit.com"
_FALLBACK_SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"


def _fetch_endpoints(env):
    """Return (token_url, api_base) from Intuit's discovery document."""
    url = DISCOVERY_URL if env == "production" else SANDBOX_DISCOVERY_URL
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        doc = r.json()
        token_url = doc.get("token_endpoint", _FALLBACK_TOKEN_URL)
        # The discovery doc doesn't include the REST API base, so we still need
        # to derive it from the environment. We get the auth endpoints from
        # discovery and keep the API base as a known constant (it's stable).
        api_base = _FALLBACK_PROD_BASE if env == "production" else _FALLBACK_SANDBOX_BASE
        return token_url, api_base
    except Exception:
        return _FALLBACK_TOKEN_URL, (
            _FALLBACK_PROD_BASE if env == "production" else _FALLBACK_SANDBOX_BASE
        )


def _retry(fn, attempts=3, backoff=2.0):
    """Call fn(), retrying on transient network/server errors with exponential backoff."""
    last_exc = None
    for attempt in range(attempts):
        try:
            return fn()
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            # Don't retry auth/client errors — only transient server-side failures.
            if status is not None and status < 500:
                raise
            last_exc = e
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last_exc = e
        if attempt < attempts - 1:
            time.sleep(backoff * (2 ** attempt))
    raise last_exc


class QBOAuthError(Exception):
    """Raised for known QBO auth failures so callers can log something actionable."""


class QBO:
    def __init__(self, client_id, client_secret, refresh_token, realm_id,
                 env="production", minorversion="73"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.realm_id = realm_id
        self.minorversion = minorversion
        self.access_token = None
        # After refresh(), this holds the latest refresh token (QBO rotates it).
        self.new_refresh_token = refresh_token
        # Populated by refresh() so callers can log the last intuit_tid.
        self.last_intuit_tid = None

        self.token_url, self.base = _fetch_endpoints(env)

    def refresh(self):
        """Exchange the refresh token for a fresh access token.

        Raises QBOAuthError for expired-refresh-token / invalid-grant conditions
        so the caller can emit a diagnostic message instead of a bare traceback.
        """
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()

        def _do_refresh():
            r = requests.post(
                self.token_url,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
                timeout=30,
            )
            self.last_intuit_tid = r.headers.get("intuit_tid")
            # Surface actionable auth errors before raise_for_status swallows context.
            if r.status_code == 400:
                body = r.json() if r.content else {}
                err = body.get("error", "")
                if err in ("invalid_grant", "refresh_token_expired"):
                    raise QBOAuthError(
                        f"Refresh token rejected by QBO (error={err!r}). "
                        "The token has likely expired or been revoked — "
                        "re-run bootstrap_token.py and update the QBO_REFRESH_TOKEN secret. "
                        f"intuit_tid={self.last_intuit_tid}"
                    )
            r.raise_for_status()
            return r

        r = _retry(_do_refresh)
        tok = r.json()
        self.access_token = tok["access_token"]
        self.new_refresh_token = tok.get("refresh_token", self.refresh_token)

    def _get(self, path, params):
        def _do_get():
            r = requests.get(
                f"{self.base}{path}",
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Accept": "application/json",
                },
                params={**params, "minorversion": self.minorversion},
                timeout=60,
            )
            # Capture the transaction ID from every response for support/debugging.
            self.last_intuit_tid = r.headers.get("intuit_tid", self.last_intuit_tid)
            if r.status_code == 401:
                # Access token expired mid-run (shouldn't happen on a fresh refresh,
                # but make it explicit rather than surfacing a generic 401).
                raise QBOAuthError(
                    f"Access token rejected (401) on {path}. "
                    f"intuit_tid={self.last_intuit_tid}"
                )
            r.raise_for_status()
            return r

        return _retry(_do_get).json()

    def pl_detail(self, start_date, end_date, method="Cash"):
        """Profit & Loss Detail report — transaction-level rows, cash basis."""
        if start_date > end_date:
            raise ValueError(f"start_date {start_date!r} is after end_date {end_date!r}")
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
