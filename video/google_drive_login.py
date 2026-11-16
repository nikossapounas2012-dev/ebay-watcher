"""
One-time Google Drive sign-in for the price videos. Run it on your own computer:

    python3 video/google_drive_login.py

It asks for the OAuth client ID and secret you created in Google Cloud (a "Desktop app" client), opens
Google's sign-in page in your browser, and prints a refresh token. Put the three values into GitHub:
repository Settings -> Secrets and variables -> Actions -> New repository secret:
    GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET, GDRIVE_REFRESH_TOKEN

Access is limited to the "drive.file" scope: the watcher can only see and change files it created itself,
not anything else in your Drive. Revoke it any time at https://myaccount.google.com/permissions.
Nothing is saved to disk and nothing is sent anywhere except Google.
"""

import base64
import getpass
import hashlib
import http.server
import json
import secrets
import urllib.parse
import urllib.request
import webbrowser

SCOPE = "https://www.googleapis.com/auth/drive.file"


def main():
    client_id = input("OAuth client ID: ").strip()
    client_secret = getpass.getpass("OAuth client secret (hidden): ").strip()

    received = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            received.update(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = "code" in received
            self.wfile.write(("<h2>" + ("Signed in - you can close this tab and go back to the terminal."
                                        if ok else "Sign-in failed: " + received.get("error", ["?"])[0])
                              + "</h2>").encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print("\nOpening Google sign-in in your browser. If it doesn't open, paste this link into it:\n" + url + "\n")
    webbrowser.open(url)
    while "code" not in received and "error" not in received:
        server.handle_request()
    if "error" in received or received.get("state", [""])[0] != state:
        raise SystemExit(f"Sign-in failed: {received.get('error', ['state mismatch'])[0]}")

    data = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret, "code": received["code"][0],
        "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": redirect}).encode()
    with urllib.request.urlopen("https://oauth2.googleapis.com/token", data, timeout=30) as r:
        tokens = json.load(r)
    if "refresh_token" not in tokens:
        raise SystemExit("Google did not return a refresh token - remove the app's access at "
                         "https://myaccount.google.com/permissions and run this again.")
    print("Done. Add these three repository secrets in GitHub (don't share them with anyone):\n")
    print("GDRIVE_CLIENT_ID      =", client_id)
    print("GDRIVE_CLIENT_SECRET  = (the secret you just typed)")
    print("GDRIVE_REFRESH_TOKEN  =", tokens["refresh_token"])


if __name__ == "__main__":
    main()
