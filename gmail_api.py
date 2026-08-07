# ============================================================================
# gmail_api.py  —  Send mail as feedback@sret.edu.in via the Gmail API (OAuth2)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION  (Version 1.5)
# ----------------------------------------------------------------------------
# Up to v1.0 the app had exactly two ways to deliver mail (see emailer.py):
#     * SMTP  (smtplib, needs a login password / app-password), and
#     * dev-outbox  (writes .eml files to disk, sends nothing).
#
# On our institutional Google Workspace domain, App Passwords are DISABLED by
# the admin (the account shows "The setting you are looking for is not available
# for your account"), so the SMTP path cannot authenticate as
# feedback@sret.edu.in.  This module is the v1.5 answer: instead of SMTP with a
# password, we talk to the **Gmail REST API over HTTPS** using an **OAuth2
# refresh token** that was granted once by logging in AS feedback@sret.edu.in.
#
# Why this specific design:
#   * No password is ever stored — only a refresh token, which is scoped to the
#     single permission "gmail.send" (it cannot read any mailbox).
#   * HTTPS to googleapis.com is on PythonAnywhere's FREE-tier allow-list, so it
#     works without upgrading the account (raw third-party SMTP/HTTP APIs such as
#     SendGrid are NOT on that allow-list — this is why we stay inside Google).
#   * Mail is genuinely sent + DKIM-signed by Workspace as feedback@sret.edu.in,
#     so it passes spam checks even when delivered to the external student domain
#     sriher.edu.in.
#
# DEPENDENCIES: NONE beyond the Python standard library.  We deliberately use
# urllib.request instead of google-api-python-client so there is nothing extra to
# pip-install on the free tier (matches requirements.txt's "small deps" rule).
#
# HOW emailer.py USES THIS FILE:
#   1. emailer.send_batch() calls gmail_settings() to decide if this mode is on.
#   2. If on, it calls send_via_gmail_api(from_addr, to, subject, body) once per
#      student.  The short-lived access token is fetched once and cached here, so
#      a whole batch reuses a single token instead of re-authenticating per mail.
# ----------------------------------------------------------------------------

import os                       # read configuration from environment variables
import json                     # parse Google's JSON token / API responses
import time                     # track access-token expiry for caching
import base64                   # Gmail API wants the message base64url-encoded
import urllib.parse             # URL-encode the token-request form body
import urllib.request           # make the HTTPS POST calls (stdlib, honours proxy)
import urllib.error             # catch HTTP errors and surface a clear message
from email.message import EmailMessage  # build a correctly-formatted RFC822 email


# ----------------------------------------------------------------------------
# Google endpoints. These are stable, documented URLs.
#   TOKEN_URI : where we swap the long-lived refresh token for a short-lived
#               access token (valid ~1 hour).
#   SEND_URI  : the Gmail "send message" endpoint. "users/me" means "the account
#               that owns the token", i.e. feedback@sret.edu.in.
# Both are under googleapis.com, which is allow-listed on PythonAnywhere free.
# ----------------------------------------------------------------------------
TOKEN_URI = "https://oauth2.googleapis.com/token"
SEND_URI = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

# The one and only OAuth scope this app requests. "gmail.send" lets the token
# SEND mail as the account — it grants NO read access to the inbox. Keep this in
# sync with get_refresh_token.py, or the refresh token will be for a different
# permission set and sends will be rejected.
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


# ----------------------------------------------------------------------------
# _access_token_cache — module-level cache so we do NOT hit Google's token
# endpoint for every single student in a batch. We store the token string and
# the wall-clock time it stops being valid. A batch of 300 mails then costs ONE
# token request instead of 300.
# ----------------------------------------------------------------------------
_access_token_cache = {"token": None, "expires_at": 0.0}


# ----------------------------------------------------------------------------
# gmail_settings() — read the Gmail-API configuration from environment variables.
#
# Env vars (set these on PythonAnywhere; never commit them):
#   FEEDBACK_GMAIL_CLIENT_ID      OAuth client ID   (from the Google Cloud project)
#   FEEDBACK_GMAIL_CLIENT_SECRET  OAuth client secret
#   FEEDBACK_GMAIL_REFRESH_TOKEN  the token minted once by get_refresh_token.py
#   FEEDBACK_GMAIL_FROM           the sender address (defaults to feedback@sret.edu.in)
#
# "enabled" is True only when the three credential parts are ALL present. This is
# what emailer.py checks to decide whether to use this mode at all. If any part
# is missing we return enabled=False and emailer.py falls back to SMTP/dev-outbox.
# ----------------------------------------------------------------------------
def gmail_settings():
    client_id = os.environ.get("FEEDBACK_GMAIL_CLIENT_ID", "").strip()
    client_secret = os.environ.get("FEEDBACK_GMAIL_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("FEEDBACK_GMAIL_REFRESH_TOKEN", "").strip()
    from_addr = os.environ.get("FEEDBACK_GMAIL_FROM", "feedback@sret.edu.in").strip()
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "from_addr": from_addr,
        # Real mode iff we have all three secret parts to obtain an access token.
        "enabled": bool(client_id and client_secret and refresh_token),
    }


# ----------------------------------------------------------------------------
# _http_post_form(url, form_dict) — tiny helper: POST an application/x-www-form-
# urlencoded body and return the parsed JSON response. Used for the token swap.
# Raises RuntimeError with Google's error text if the call fails, so callers get
# a readable message instead of a bare HTTPError.
# ----------------------------------------------------------------------------
def _http_post_form(url, form_dict):
    # Encode the dict as key=value&key=value and then to raw bytes (HTTP bodies
    # are bytes, not str).
    data = urllib.parse.urlencode(form_dict).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        # timeout guards against a hung network call stalling the whole batch.
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Google returns a JSON error body (e.g. invalid_grant); include it so the
        # admin route can show WHY (expired token, wrong secret, etc.).
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"token endpoint HTTP {e.code}: {body}")


# ----------------------------------------------------------------------------
# _get_access_token(cfg) — return a valid short-lived access token, refreshing
# from Google only when the cached one is missing or about to expire.
#
# The OAuth "refresh token grant": we present client_id + client_secret +
# refresh_token and Google hands back an access_token good for ~3600 seconds.
# We cache it and subtract a 60-second safety margin so we never send with a
# token that expires mid-request.
# ----------------------------------------------------------------------------
def _get_access_token(cfg):
    now = time.time()
    # Reuse the cached token if it is still comfortably valid.
    if _access_token_cache["token"] and now < _access_token_cache["expires_at"]:
        return _access_token_cache["token"]

    # Otherwise ask Google for a fresh access token using the refresh token.
    resp = _http_post_form(TOKEN_URI, {
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": cfg["refresh_token"],
        "grant_type": "refresh_token",
    })

    token = resp.get("access_token")
    if not token:
        # No token in the response means the credentials/refresh token are bad.
        raise RuntimeError(f"no access_token in token response: {resp}")

    # Cache it, expiring 60s early to avoid edge-of-validity failures.
    expires_in = int(resp.get("expires_in", 3600))
    _access_token_cache["token"] = token
    _access_token_cache["expires_at"] = now + expires_in - 60
    return token


# ----------------------------------------------------------------------------
# _build_raw_message(from_addr, to_addr, subject, body) — construct a standard
# email and return it in Gmail's required form: base64url of the full RFC822
# bytes. We reuse email.message.EmailMessage (same class emailer.py uses) so the
# formatting/encoding of headers and body is identical across all send modes.
# ----------------------------------------------------------------------------
def _build_raw_message(from_addr, to_addr, subject, body, attachments=None):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr          # feedback@sret.edu.in — matches the token owner
    msg["To"] = to_addr
    msg.set_content(body)            # plain-text body (same as the SMTP path)
    # (v3.3) Attach any report PDFs. We reuse emailer.add_attachments so the MIME
    # typing is identical to the SMTP/dev paths (imported lazily to avoid any
    # import cycle — emailer imports this module inside its send function).
    if attachments:
        from emailer import add_attachments
        add_attachments(msg, attachments)
    # Gmail API expects the raw message base64URL-encoded (URL-safe alphabet).
    # decode("ascii") turns the base64 bytes back into a str for the JSON body.
    return base64.urlsafe_b64encode(bytes(msg)).decode("ascii")


# ----------------------------------------------------------------------------
# send_via_gmail_api(cfg, from_addr, to_addr, subject, body) — send ONE email.
#
# emailer.send_batch() calls this once per student. It:
#   1. gets (or reuses) an access token,
#   2. builds the base64url raw message,
#   3. POSTs it to the Gmail send endpoint with a Bearer token.
# Returns the Gmail message id on success; raises RuntimeError on failure so the
# caller can record a per-recipient error and carry on with the rest of the batch.
# ----------------------------------------------------------------------------
def send_via_gmail_api(cfg, from_addr, to_addr, subject, body, attachments=None):
    access_token = _get_access_token(cfg)
    raw = _build_raw_message(from_addr, to_addr, subject, body, attachments)

    # The Gmail send endpoint takes a JSON body: {"raw": "<base64url message>"}.
    payload = json.dumps({"raw": raw}).encode("utf-8")
    req = urllib.request.Request(
        SEND_URI,
        data=payload,
        headers={
            "Authorization": f"Bearer {access_token}",  # proves we own the account
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("id", "")   # Gmail's id for the sent message
    except urllib.error.HTTPError as e:
        # Surface Google's JSON error (quota, invalid recipient, revoked scope…).
        body_txt = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"gmail send HTTP {e.code}: {body_txt}")


# ----------------------------------------------------------------------------
# self_test() — a NO-NETWORK sanity check used by the verification step. It only
# exercises the pure, offline pieces (config parsing + raw message building) so
# it can run in CI / the sandbox without real credentials. Returns True on pass.
# ----------------------------------------------------------------------------
def self_test():
    raw = _build_raw_message(
        "feedback@sret.edu.in", "student@sriher.edu.in",
        "Hello", "Body with link https://example.test/f/abc",
    )
    # Round-trip the base64url back to bytes and confirm our headers survived.
    decoded = base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8", "replace")
    assert "From: feedback@sret.edu.in" in decoded, "From header missing"
    assert "To: student@sriher.edu.in" in decoded, "To header missing"
    assert "Subject: Hello" in decoded, "Subject header missing"
    return True
