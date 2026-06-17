# Mintt Cash Dashboard — automated refresh

A weekly GitHub Action pulls **cash-basis** actuals and bank balances from
QuickBooks Online, tags every transaction into a budget bucket, and writes them
into the Google Sheet that backs the cash dashboard. The dashboard's formulas do
the rest, so the view is always current with zero manual entry.

```
.github/workflows/cash-dashboard.yml   # weekly cron + manual run
qbo_cash_dashboard/
  pull_and_write.py    # orchestrator (run by the Action)
  qbo.py               # QBO client + report parser
  sheets.py            # Google Sheets writer
  github_secret.py     # rotates the refresh token back into a repo secret
  bucket_map.csv       # account -> bucket -> line (edit this to recategorize)
  bootstrap_token.py   # one-time: get your initial refresh token + realm ID
  requirements.txt
```

## How it works

1. Refresh the QBO access token (and capture the rotated refresh token).
2. Pull a **Profit & Loss Detail** report with `accounting_method=Cash` for the
   current calendar year — transaction-level, money counted when it moves.
3. Pull the current balance of every **Bank** account = cash on hand.
4. Map each transaction to a bucket/line via `bucket_map.csv` (6090 splits
   Mongoose onto his own line by counterparty).
5. Full-refresh the Sheet's **Data** tab and write cash on hand to **B6**.
6. If QBO rotated the refresh token, save the new one back to the repo secret.

Full refresh each run — no incremental state to drift. The data is small.

## One-time setup

### A. QuickBooks
1. Create an app at the Intuit developer portal; grab the **Client ID** and
   **Client Secret** (production keys).
2. Run the bootstrap locally to get your first refresh token + realm ID:
   ```bash
   export QBO_CLIENT_ID=... QBO_CLIENT_SECRET=...
   python qbo_cash_dashboard/bootstrap_token.py
   ```

### B. Google Sheet
1. Import `Mintt_Cash_Dashboard.xlsx` into Google Sheets once (File → Import →
   Upload). The SUMIFS formulas carry over and become live.
2. Create a Google Cloud **service account**, enable the Sheets API, download its
   JSON key.
3. **Share the Sheet** with the service account's email (Editor).
4. Copy the Sheet ID from its URL (`/d/<SHEET_ID>/edit`).

### C. GitHub
Create a fine-grained **PAT** with `secrets: write` on this repo (used only to
save the rotated QBO token), then add these repo secrets:

| Secret | Value |
| --- | --- |
| `QBO_CLIENT_ID` | from Intuit |
| `QBO_CLIENT_SECRET` | from Intuit |
| `QBO_REFRESH_TOKEN` | from bootstrap |
| `QBO_REALM_ID` | from bootstrap |
| `GOOGLE_SA_KEY` | the full service-account JSON (paste as-is) |
| `SHEET_ID` | the Sheet ID |
| `GH_PAT` | the PAT above |

Then open the **Actions** tab and run *Cash Dashboard Refresh* once via
**Run workflow** to confirm it works. It runs every Monday thereafter.

## ⚠️ One dashboard tweak before the first run

The Action matches transactions to month columns by exact label
(`Jan'26`, `Feb'26`, …). The shipped dashboard labels the current month
`Jun'26*` (asterisk = partial month). **Change that one header cell to the plain
`Jun'26`** so the join lands — and move the "partial month" caveat to the note
row under the title. After that, every column header should be the plain
`Mon'YY` form.

## Changing categories

Edit `bucket_map.csv` and commit. The next run re-tags everything. Keep it in
sync with the dashboard's **Map** tab so the two never disagree.
