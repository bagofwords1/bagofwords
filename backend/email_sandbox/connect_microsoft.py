"""Microsoft 365 delegated sign-in via device code — NO PowerShell required.

Get a refresh token for the analyst mailbox by signing in interactively. This
is the no-PowerShell path: register an Entra app in the portal with DELEGATED
permissions (IMAP.AccessAsUser.All, SMTP.Send, offline_access) and enable
"Allow public client flows", then run:

    EMAIL_MS_TENANT=<tenant-id> EMAIL_MS_CLIENT_ID=<client-id> \
    python connect_microsoft.py

It prints a URL + code; open it, sign in AS the mailbox (or a delegate with
access), consent — and it prints the refresh token. Feed that into
check_mailbox.py as EMAIL_MS_REFRESH_TOKEN with EMAIL_AUTH=microsoft_delegated
to test the real SMTP/IMAP round-trip (from a network where 587/993 are open).
"""
import os
import sys
import time

import httpx

TENANT = os.environ["EMAIL_MS_TENANT"]
CLIENT_ID = os.environ["EMAIL_MS_CLIENT_ID"]
# offline_access => we get a refresh token; the Outlook scopes => IMAP + SMTP.
SCOPE = os.environ.get(
    "EMAIL_MS_SCOPE",
    "https://outlook.office365.com/IMAP.AccessAsUser.All "
    "https://outlook.office365.com/SMTP.Send offline_access",
)
BASE = os.environ.get("BOW_MS_LOGIN_BASE", "https://login.microsoftonline.com")


def main():
    with httpx.Client(timeout=30) as c:
        dc = c.post(
            f"{BASE}/{TENANT}/oauth2/v2.0/devicecode",
            data={"client_id": CLIENT_ID, "scope": SCOPE},
        )
        if dc.status_code != 200:
            print("devicecode request failed:", dc.status_code, dc.text[:400])
            sys.exit(1)
        d = dc.json()
        print("\n" + "=" * 60)
        print(d.get("message", f"Go to {d['verification_uri']} and enter {d['user_code']}"))
        print("=" * 60 + "\n", flush=True)

        interval = int(d.get("interval", 5))
        deadline = time.time() + int(d.get("expires_in", 900))
        while time.time() < deadline:
            time.sleep(interval)
            tr = c.post(
                f"{BASE}/{TENANT}/oauth2/v2.0/token",
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": CLIENT_ID,
                    "device_code": d["device_code"],
                },
            )
            body = tr.json()
            if tr.status_code == 200:
                print("Signed in. Refresh token (store this in BOW):\n")
                print(body["refresh_token"])
                print("\nAccess token prefix:", body["access_token"][:24], "...")
                return
            err = body.get("error")
            if err in ("authorization_pending", "slow_down"):
                if err == "slow_down":
                    interval += 5
                continue
            print("Sign-in failed:", err, body.get("error_description", "")[:300])
            sys.exit(1)
        print("Device code expired before sign-in completed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
