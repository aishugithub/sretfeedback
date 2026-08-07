# ============================================================================
# get_refresh_token.py  —  ONE-TIME helper to mint a Gmail send refresh token
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION  (Version 1.5)
# ----------------------------------------------------------------------------
# gmail_api.py needs a long-lived OAuth2 *refresh token* for feedback@sret.edu.in
# so the server can send mail without a password. That refresh token is created
# ONCE, by a human, by logging in and clicking "Allow". This script performs that
# one-time consent flow and prints the refresh token for you to paste into your
# PythonAnywhere environment variables.
#
# RUN THIS ON YOUR OWN LAPTOP, NOT ON PYTHONANYWHERE — it opens a browser and
# listens on http://localhost for Google's redirect, which a laptop can do and a
# shared web host cannot.
#
# It uses ONLY the Python standard library (no google client libraries), matching
# the project's "small deps" philosophy.
#
# ---------------------------------------------------------------------------
# PREREQUISITES (done once in the Google Cloud console — see
# EMAIL-GMAIL-API-SETUP.md for the click-by-click):
#   1. A Google Cloud project with the **Gmail API enabled**.
#   2. An **OAuth consent screen** published to "Production" (so the refresh
#      token does NOT expire after 7 days).
#   3. An **OAuth client ID of type "Desktop app"** — gives you a client_id and
#      client_secret. Paste them below (env vars or the interactive prompts).
#
# USAGE:
#   Set the two env vars then run, e.g. on Windows PowerShell:
#       $env:FEEDBACK_GMAIL_CLIENT_ID="....apps.googleusercontent.com"
#       $env:FEEDBACK_GMAIL_CLIENT_SECRET="GOCSPX-...."
#       python get_refresh_token.py
#   ...or just run `python get_refresh_token.py` and type them when prompted.
#
#   A browser opens -> log in AS feedback@sret.edu.in -> click Allow.
#   The script then prints your FEEDBACK_GMAIL_REFRESH_TOKEN.
# ----------------------------------------------------------------------------

import os                       # read client id/secret from the environment
import sys                      # exit with a message on error
import json                     # parse Google's token response
import time                     # small pause before opening the browser
import webbrowser               # open the consent page in the default browser
import urllib.parse             # build the auth URL / encode the token form
import urllib.request           # exchange the auth code for tokens (HTTPS POST)
import urllib.error             # readable errors if the exchange fails
import http.server              # tiny local server to catch Google's redirect
import threading                # run that server without blocking the main flow

# Must match gmail_api.GMAIL_SEND_SCOPE exactly — the refresh token is only ever
# valid for the scope(s) consented here. "gmail.send" = send-only, no inbox read.
SCOPE = "https://www.googleapis.com/auth/gmail.send"

# Google's OAuth endpoints for the "installed/desktop app" authorization-code flow.
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"

# We ask Google to redirect back to a loopback address on the local machine. Port
# 0 lets the OS pick any free port; we read the actual port after binding.
REDIRECT_HOST = "127.0.0.1"


# ----------------------------------------------------------------------------
# _CodeCatcher — a one-request HTTP handler that captures the ?code=... Google
# appends to the redirect URL, stores it, and shows the user a "you can close
# this tab" page. We stash the code on the server object so the main thread can
# read it after the request is handled.
# ----------------------------------------------------------------------------
class _CodeCatcher(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        # Parse the query string of the redirect Google just made to us.
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        # Hand the authorization code (or error) back to the server instance.
        self.server.auth_code = params.get("code", [None])[0]
        self.server.auth_error = params.get("error", [None])[0]
        # Reply to the browser so the user sees a friendly confirmation.
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = ("<h2>All done.</h2><p>You can close this tab and return to the "
               "terminal.</p>")
        self.wfile.write(msg.encode("utf-8"))

    # Silence the default per-request logging so the terminal stays clean.
    def log_message(self, *args):
        return


# ----------------------------------------------------------------------------
# main() — orchestrates the whole one-time flow.
# ----------------------------------------------------------------------------
def main():
    # 1) Collect the client credentials (env vars first, else prompt).
    client_id = os.environ.get("FEEDBACK_GMAIL_CLIENT_ID", "").strip()
    client_secret = os.environ.get("FEEDBACK_GMAIL_CLIENT_SECRET", "").strip()
    if not client_id:
        client_id = input("Paste your OAuth client ID: ").strip()
    if not client_secret:
        client_secret = input("Paste your OAuth client secret: ").strip()
    if not client_id or not client_secret:
        sys.exit("Both client ID and client secret are required. Aborting.")

    # 2) Start the local redirect-catcher on a random free port.
    server = http.server.HTTPServer((REDIRECT_HOST, 0), _CodeCatcher)
    server.auth_code = None
    server.auth_error = None
    port = server.server_address[1]                 # the OS-chosen port
    redirect_uri = f"http://{REDIRECT_HOST}:{port}/"
    # Serve exactly one request (the redirect) in a background thread.
    threading.Thread(target=server.handle_request, daemon=True).start()

    # 3) Build the consent URL and open it in the browser.
    #    access_type=offline  -> we get a refresh token (not just an access token)
    #    prompt=consent       -> force Google to RE-issue a refresh token even if
    #                            this account consented before (otherwise repeat
    #                            runs return no refresh_token).
    auth_url = AUTH_URI + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    })
    print("\nOpening your browser to authorise sending as feedback@sret.edu.in ...")
    print("If it does not open, paste this URL manually:\n\n" + auth_url + "\n")
    time.sleep(1)
    webbrowser.open(auth_url)

    # 4) Wait for the browser redirect to deliver the authorization code.
    print("Waiting for you to log in and click Allow ...")
    while server.auth_code is None and server.auth_error is None:
        time.sleep(0.3)
    if server.auth_error:
        sys.exit(f"Authorisation failed: {server.auth_error}")
    auth_code = server.auth_code

    # 5) Exchange the authorization code for tokens (this is where the refresh
    #    token is issued). Standard OAuth "authorization_code" grant.
    form = urllib.parse.urlencode({
        "code": auth_code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URI, data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            tokens = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.exit(f"Token exchange failed: HTTP {e.code}: "
                 f"{e.read().decode('utf-8', 'replace')}")

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        # Almost always caused by a prior consent without prompt=consent; we set
        # prompt=consent above specifically to avoid this.
        sys.exit("No refresh_token returned. Re-run and ensure you fully "
                 "approved the consent screen.")

    # 6) Success — print exactly what to set on PythonAnywhere.
    print("\n" + "=" * 70)
    print("SUCCESS. Set these environment variables on PythonAnywhere:")
    print("=" * 70)
    print(f"FEEDBACK_GMAIL_CLIENT_ID={client_id}")
    print(f"FEEDBACK_GMAIL_CLIENT_SECRET={client_secret}")
    print(f"FEEDBACK_GMAIL_REFRESH_TOKEN={refresh_token}")
    print("FEEDBACK_GMAIL_FROM=feedback@sret.edu.in")
    print("=" * 70)
    print("Keep the refresh token secret — treat it like a password.")


if __name__ == "__main__":
    main()
