#!/usr/bin/env python3
"""
One-time YouTube login. Run this on your own computer, once.

1. In Google Cloud Console, finish the OAuth client (type: Desktop app) and
   download its JSON. Save it next to this file as client_secrets.json.
2. Run:  python setup_auth.py
3. A browser opens. Sign in with the account that owns the channel and allow
   the upload permission.
4. It prints three values. Put them in your repository secrets as
   YT_CLIENT_ID, YT_CLIENT_SECRET and YT_REFRESH_TOKEN. Then delete
   client_secrets.json and refresh_token.txt.
"""

from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def main() -> None:
    secrets_file = Path(__file__).with_name("client_secrets.json")
    if not secrets_file.exists():
        raise SystemExit(
            "client_secrets.json not found next to this script.\n"
            "Download it from Google Cloud Console -> APIs & Services -> Credentials."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_file), scopes=[SCOPE])
    # access_type=offline + prompt=consent is what makes the refresh token long-lived.
    credentials = flow.run_local_server(
        port=0, access_type="offline", prompt="consent", authorization_prompt_message=""
    )

    if not credentials.refresh_token:
        raise SystemExit("No refresh token returned. Re-run and make sure you click Allow.")

    Path(__file__).with_name("refresh_token.txt").write_text(credentials.refresh_token, encoding="utf-8")

    print("\n" + "=" * 70)
    print("Add these three repository secrets, then delete this output.\n")
    print(f"YT_CLIENT_ID     = {credentials.client_id}")
    print(f"YT_CLIENT_SECRET = {credentials.client_secret}")
    print(f"YT_REFRESH_TOKEN = {credentials.refresh_token}")
    print("=" * 70)
    print("\nThe refresh token is also saved in refresh_token.txt (delete it after copying).")


if __name__ == "__main__":
    main()
