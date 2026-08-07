# ============================================================================
# auth_leaders.py  —  Version 2.0 · Module 2 · §5.1/§17.2 : leader passwords
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# The ~13 leader logins (HODs, Vice Dean, Dean) seeded in Module 1 have EMPTY
# passwords by design (§17.2 — "the admin holds no passwords; users set their
# own"). Before a leader can endorse/return an ATR they must (a) set a password
# via a one-time emailed link, then (b) log in. This module is the small, self-
# contained crypto + token plumbing for exactly that, using ONLY the standard
# library and the Module 1 `set_pw_token` table (no bcrypt/argon2 — the Module 1
# handoff explicitly specifies hashlib.pbkdf2_hmac, a stdlib PBKDF2).
#
# WHAT IT PROVIDES:
#   hash_password / verify_password  — PBKDF2-HMAC-SHA256, salted, stored as one
#                                       self-describing string in app_user.pw_hash.
#   issue_set_pw_token / redeem_...   — the one-time "set your password" link,
#                                       reusing the existing set_pw_token table +
#                                       the app's single token minter
#                                       (services.new_token) — the SAME magic-link
#                                       pattern the student links use.
#   authenticate                      — email + password → the app_user row, or None.
#
# SECURITY NOTES (§11, §17.2): passwords are stored ONLY as a one-way salted
# PBKDF2 hash — a stolen backup reveals nothing. Verification is constant-time
# (hmac.compare_digest). The set-password token is unguessable, expiring and
# one-time. No secret is ever logged or emailed except the one-time link itself.
# ----------------------------------------------------------------------------

import hashlib     # PBKDF2-HMAC-SHA256 (stdlib) — the approved hasher
import hmac        # constant-time comparison of hashes
import os          # os.urandom for a per-password random salt
from datetime import datetime, timedelta

import services    # services.new_token() — the app's single token minter


# PBKDF2 parameters. 200k iterations of SHA-256 is a sensible 2025-era cost for a
# handful of interactive leader logins on a laptop/PythonAnywhere host. Stored in
# the hash string so a future cost bump can be verified against old hashes.
_PBKDF2_ALGO = "sha256"
_PBKDF2_ITERS = 200_000
_SALT_BYTES = 16

# Set-password links live 7 days — long enough for a leader to act on the email,
# short enough to limit exposure. Overridable per call.
SET_PW_TTL_DAYS = 7


# ----------------------------------------------------------------------------
# hash_password(plain) -> str
# ----------------------------------------------------------------------------
# Produce a self-describing hash string of the form
#     pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex>
# so verify_password() can read back the exact algorithm/iterations/salt that
# were used, and a later parameter change stays backward-compatible. A fresh
# random salt per call means two leaders with the same password get different
# hashes (no rainbow-table shortcut).
# ----------------------------------------------------------------------------
def hash_password(plain):
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, plain.encode("utf-8"), salt, _PBKDF2_ITERS)
    return "pbkdf2_%s$%d$%s$%s" % (
        _PBKDF2_ALGO, _PBKDF2_ITERS, salt.hex(), dk.hex())


# ----------------------------------------------------------------------------
# verify_password(plain, stored) -> bool
# ----------------------------------------------------------------------------
# Re-derive the hash of `plain` using the algorithm/iterations/salt recorded in
# `stored`, and compare in CONSTANT TIME (hmac.compare_digest) so a timing side-
# channel cannot leak how much of the hash matched. An empty/malformed stored
# hash (e.g. a leader who has not set a password yet) safely returns False.
# ----------------------------------------------------------------------------
def verify_password(plain, stored):
    if not stored or "$" not in stored:
        return False
    try:
        algo_part, iters_s, salt_hex, hash_hex = stored.split("$")
        algo = algo_part.split("_", 1)[1]          # 'pbkdf2_sha256' -> 'sha256'
        iters = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, IndexError):
        return False
    dk = hashlib.pbkdf2_hmac(algo, plain.encode("utf-8"), salt, iters)
    return hmac.compare_digest(dk, expected)


# ----------------------------------------------------------------------------
# _now_iso() / _parse_iso(s) — ISO timestamp helpers matching SQLite's
# datetime('now') text format, so token expiry compares cleanly against rows.
# ----------------------------------------------------------------------------
def _now_iso():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _parse_iso(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


# ----------------------------------------------------------------------------
# issue_set_pw_token(master, user_id, purpose='SET', ttl_days=..) -> jti
# ----------------------------------------------------------------------------
# Mint a one-time "set/reset your password" link for a leader, storing it in the
# Module 1 set_pw_token table (jti, user_id, purpose, expires_at, used_at). The
# jti goes into the emailed URL (/leader/set-password?token=<jti>). purpose is
# 'SET' (first-time) or 'RESET' (forgot password) — both redeem the same way.
# Does NOT commit; the caller owns the transaction.
# ----------------------------------------------------------------------------
def issue_set_pw_token(master, user_id, purpose="SET", ttl_days=SET_PW_TTL_DAYS):
    jti = services.new_token()
    expires_at = (datetime.utcnow() + timedelta(days=ttl_days)).strftime(
        "%Y-%m-%d %H:%M:%S")
    master.execute(
        "INSERT INTO set_pw_token (jti, user_id, purpose, expires_at, used_at) "
        "VALUES (?, ?, ?, ?, NULL)",
        (jti, user_id, purpose, expires_at))
    return jti


# ----------------------------------------------------------------------------
# verify_set_pw_token(master, jti, now=None) -> (ok, reason, row)
# ----------------------------------------------------------------------------
# Gate a set-password link the same way faculty_tokens.verify gates a faculty
# link: reject an unknown jti ('bad_signature'), a past-expiry one ('expired'),
# or an already-redeemed one ('used'); otherwise return the row. Does not mark it
# used — redemption (redeem_set_pw_token) does that once the password is saved.
# ----------------------------------------------------------------------------
def verify_set_pw_token(master, jti, now=None):
    row = master.execute(
        "SELECT * FROM set_pw_token WHERE jti = ?", (jti,)).fetchone()
    if row is None:
        return False, "bad_signature", None
    now_dt = now if now is not None else datetime.utcnow()
    exp = _parse_iso(row["expires_at"])
    if exp is None or now_dt > exp:
        return False, "expired", row
    if row["used_at"] is not None:
        return False, "used", row
    return True, None, row


# ----------------------------------------------------------------------------
# redeem_set_pw_token(master, jti, new_password) -> (ok, reason)
# ----------------------------------------------------------------------------
# The atomic "set my password" step: verify the token, write the PBKDF2 hash onto
# the app_user row, and burn the token (used_at) so the link cannot be replayed.
# Does NOT commit; the caller wraps it so the hash-write and the token-burn land
# together (a password is never set without consuming its link, and vice versa).
# ----------------------------------------------------------------------------
def redeem_set_pw_token(master, jti, new_password):
    ok, reason, row = verify_set_pw_token(master, jti)
    if not ok:
        return False, reason
    pw_hash = hash_password(new_password)
    master.execute("UPDATE app_user SET pw_hash = ? WHERE id = ?",
                   (pw_hash, row["user_id"]))
    master.execute("UPDATE set_pw_token SET used_at = ? WHERE jti = ?",
                   (_now_iso(), jti))
    return True, None


# ----------------------------------------------------------------------------
# authenticate(master, email, password) -> app_user Row | None
# ----------------------------------------------------------------------------
# Email + password login. Returns the app_user row on success, else None. Rejects
# disabled accounts and accounts with no password set yet. Stamps last_login_at
# on success (does NOT commit — the caller commits). This is the check the
# /leader/login route uses before opening a leader session.
# ----------------------------------------------------------------------------
def authenticate(master, email, password):
    row = master.execute(
        "SELECT * FROM app_user WHERE email = ?", (email.strip().lower(),)
    ).fetchone()
    # Fall back to a case-sensitive match if the lower() form missed (seeds use
    # lowercased emails, but be forgiving of a hand-typed mixed-case address).
    if row is None:
        row = master.execute(
            "SELECT * FROM app_user WHERE email = ?", (email.strip(),)).fetchone()
    if row is None or row["status"] != "active":
        return None
    if not verify_password(password, row["pw_hash"]):
        return None
    master.execute("UPDATE app_user SET last_login_at = ? WHERE id = ?",
                   (_now_iso(), row["id"]))
    return row
