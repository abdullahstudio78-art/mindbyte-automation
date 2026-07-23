"""
MindByte Automation - one-time interactive TikTok OAuth setup.

Run this LOCALLY (on your own machine, not in GitHub Actions) exactly once
to produce a TIKTOK_REFRESH_TOKEN. It will:

  1. Print an authorization URL for you to open in a browser and log in
     with the TikTok account you registered the developer app under.
  2. After you approve, TikTok redirects to your app's configured redirect
     URI with a `?code=...` in the URL - paste that code back into this
     script when prompted.
  3. Exchange the code for an access token + refresh token, and print the
     refresh token for you to save as the TIKTOK_REFRESH_TOKEN GitHub
     Actions secret (Settings -> Secrets and variables -> Actions).

Refresh tokens are valid for 365 days and rotate on every use, but
tiktok_publish.py's normal refresh-token grant calls don't require you to
repeat this step - as long as the workflow runs at least once within any
365-day window, the token stays valid indefinitely. You only need to redo
this script if the refresh token is ever revoked or expires from disuse.

Usage:
    export TIKTOK_CLIENT_KEY=...
    export TIKTOK_CLIENT_SECRET=...
    # Must exactly match the Redirect URI registered on the app's Login Kit page:
    export TIKTOK_REDIRECT_URI=https://abdullahstudio78-art.github.io/mindbyte-automation/oauth-callback.html
    python tiktok_auth.py
"""

import os
import urllib.parse

import requests

CLIENT_KEY = os.environ.get("TIKTOK_CLIENT_KEY", "").strip()
CLIENT_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET", "").strip()
REDIRECT_URI = os.environ.get("TIKTOK_REDIRECT_URI", "").strip()

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"

# video.publish is what the Content Posting API needs to post videos on the
# user's behalf. user.info.basic is added so the account can be identified
# in logs/debugging - harmless to include.
SCOPES = "video.publish,user.info.basic"


def build_authorize_url() -> str:
    params = {
        "client_key": CLIENT_KEY,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "state": "mindbyte-setup",
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str) -> dict:
    resp = requests.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"},
        data={
            "client_key": CLIENT_KEY,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    if not (CLIENT_KEY and CLIENT_SECRET and REDIRECT_URI):
        print("Set TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET, and TIKTOK_REDIRECT_URI first (see the")
        print("module docstring at the top of this file for how).")
        return

    print("1. Open this URL in a browser and log in / approve access:\n")
    print(build_authorize_url())
    print("\n2. After approving, you'll land on your redirect URI with a URL like:")
    print(f"   {REDIRECT_URI}?code=XXXXX&state=mindbyte-setup")
    print("   Copy just the code value (everything after 'code=' and before '&').\n")

    code = input("Paste the code here: ").strip()
    if not code:
        print("No code entered - aborting.")
        return

    result = exchange_code(code)
    if "access_token" not in result:
        print(f"Token exchange failed: {result}")
        return

    print("\nSuccess! Save this as the TIKTOK_REFRESH_TOKEN GitHub Actions secret:\n")
    print(result["refresh_token"])
    print(f"\n(access token also issued, expires in {result.get('expires_in')}s - not needed, ")
    print("tiktok_publish.py refreshes a fresh one automatically on every pipeline run.)")


if __name__ == "__main__":
    main()
