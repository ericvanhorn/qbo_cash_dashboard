"""One-time bootstrap to get your initial QBO refresh token + realm (company) ID.

You only run this once, locally, to seed the secrets. After that the GitHub
Action keeps the refresh token alive automatically.

Steps:
  1. In the Intuit developer portal, add this redirect URI to your app:
        https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl
     (or use the OAuth 2.0 Playground directly — it returns the same values).
  2. Set QBO_CLIENT_ID / QBO_CLIENT_SECRET in your shell, then run this script.
  3. Open the printed URL, authorize, and paste the FULL redirected URL back.

It prints QBO_REFRESH_TOKEN and QBO_REALM_ID to store as repo secrets.
"""
import base64
import os
import urllib.parse as up

import requests

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REDIRECT = "https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl"
SCOPE = "com.intuit.quickbooks.accounting"

cid = os.environ["QBO_CLIENT_ID"]
secret = os.environ["QBO_CLIENT_SECRET"]

consent = AUTH_URL + "?" + up.urlencode({
    "client_id": cid,
    "response_type": "code",
    "scope": SCOPE,
    "redirect_uri": REDIRECT,
    "state": "bootstrap",
})
print("\n1) Open this URL and authorize:\n\n" + consent + "\n")
redirected = input("2) Paste the full URL you were redirected to:\n> ").strip()

qs = up.parse_qs(up.urlparse(redirected).query)
code = qs["code"][0]
realm = qs["realmId"][0]

basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
r = requests.post(
    TOKEN_URL,
    headers={"Authorization": f"Basic {basic}",
             "Accept": "application/json",
             "Content-Type": "application/x-www-form-urlencoded"},
    data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT},
    timeout=30,
)
r.raise_for_status()
tok = r.json()

print("\n--- Store these as GitHub repo secrets ---")
print("QBO_REFRESH_TOKEN =", tok["refresh_token"])
print("QBO_REALM_ID      =", realm)
