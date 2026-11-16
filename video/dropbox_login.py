"""
One-time Dropbox sign-in for the price videos. Run it on your own computer:

    python3 video/dropbox_login.py

It asks for your Dropbox app's App key, opens Dropbox's "allow access" page, and turns the code Dropbox
shows you into a refresh token (which doesn't expire). Put both values into GitHub:
repository Settings -> Secrets and variables -> Actions -> New repository secret:
    DROPBOX_APP_KEY, DROPBOX_REFRESH_TOKEN

Uses PKCE, so the app secret is never needed. The app can only use its own folder (Apps / <app name>).
Nothing is saved to disk and nothing is sent anywhere except Dropbox.
"""

import base64
import hashlib
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
import webbrowser


def main():
    key = input("Dropbox App key: ").strip()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    url = "https://www.dropbox.com/oauth2/authorize?" + urllib.parse.urlencode({
        "client_id": key, "response_type": "code", "token_access_type": "offline",
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print("\nOpening Dropbox in your browser. If it doesn't open, paste this link into it:\n" + url + "\n")
    webbrowser.open(url)
    code = input("Click Allow, then paste the code Dropbox shows here: ").strip()
    data = urllib.parse.urlencode({"code": code, "grant_type": "authorization_code", "client_id": key,
                                   "code_verifier": verifier}).encode()
    try:
        with urllib.request.urlopen("https://api.dropboxapi.com/oauth2/token", data, timeout=30) as r:
            tokens = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Dropbox refused the code ({e.code}): {e.read().decode(errors='ignore')[:300]}\n"
                         "Run this again and paste the newest code (each code works once, for a few minutes).")
    if "files.content.write" not in tokens.get("scope", ""):
        print("\nWarning: this sign-in can't write files. In the app's Permissions tab tick "
              "files.content.write, press Submit, then run this again.\n")
    print("Done. Add these two repository secrets in GitHub (don't share them with anyone):\n")
    print("DROPBOX_APP_KEY       =", key)
    print("DROPBOX_REFRESH_TOKEN =", tokens["refresh_token"])


if __name__ == "__main__":
    main()
