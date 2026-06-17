"""Write the Data tab and the cash-on-hand cell to the Google Sheet.

The dashboard formulas (SUMIFS over the Data tab, runway off cell B6) recompute
on their own, so this writer only has to refresh raw data + the cash balance.
"""
import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
HEADER = ["Date", "Month", "Bucket", "Line", "Vendor", "Amount"]


def open_sheet(sa_info, sheet_id):
    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    return gspread.authorize(creds).open_by_key(sheet_id)


def write_data(sheet, rows):
    """Full refresh of the Data tab (header + all transaction rows)."""
    ws = sheet.worksheet("Data")
    ws.clear()
    ws.update(range_name="A1", values=[HEADER] + rows, value_input_option="USER_ENTERED")


def write_cash(sheet, balance):
    """Drop the live bank balance into the dashboard's cash-on-hand cell (B6)."""
    ws = sheet.worksheet("Cash Dashboard")
    ws.update_acell("B6", round(float(balance), 2))


def write_refreshed(sheet):
    """Stamp the last-refreshed datetime into D6."""
    import datetime as dt
    ws = sheet.worksheet("Cash Dashboard")
    ws.update_acell("D6", dt.datetime.utcnow().strftime("%-d %b %Y %H:%M UTC"))
