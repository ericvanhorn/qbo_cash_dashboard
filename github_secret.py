"""Persist QBO's rotated refresh token back into the repo's Actions secret.

QBO returns a fresh refresh token on each refresh and the old one eventually
stops working. Because a weekly run refreshes well within the 100-day window,
the integration stays alive indefinitely *as long as* we save the new token.
We do that by updating the QBO_REFRESH_TOKEN repo secret via the GitHub API
(secrets must be encrypted with the repo's public key before upload).

Requires a PAT (GH_PAT) with 'secrets: write' on the repo.
"""
import base64
import requests
from nacl import encoding, public


def update_repo_secret(repo, name, value, pat):
    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    pk = requests.get(
        f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
        headers=headers, timeout=30,
    )
    pk.raise_for_status()
    pk = pk.json()

    sealed = public.SealedBox(
        public.PublicKey(pk["key"].encode(), encoding.Base64Encoder())
    ).encrypt(value.encode())
    encrypted_value = base64.b64encode(sealed).decode()

    r = requests.put(
        f"https://api.github.com/repos/{repo}/actions/secrets/{name}",
        headers=headers,
        json={"encrypted_value": encrypted_value, "key_id": pk["key_id"]},
        timeout=30,
    )
    r.raise_for_status()
