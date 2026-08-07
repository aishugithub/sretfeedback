# ============================================================================
# faculty_tokens.py  —  Version 2.0 · Module 2 · §5.2 : faculty magic links
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# Faculty NEVER log in with a password (spec §2, §5.2). Instead, the ATR button
# in a faculty member's distribution email carries a magic link — a signed,
# single-purpose, expiring, one-time token bound to exactly ONE offering. Click
# it and you get a narrow session that can act only on that one offering's ATR;
# nothing else. The same mechanism powers the HOD "Send reminder" button (it
# just issues a fresh link).
#
# REUSING THE APP'S EXISTING TOKEN MECHANISM (important — we did NOT invent a
# parallel scheme). The app's student links are OPAQUE RANDOM tokens: a long,
# unguessable string minted with `secrets.token_urlsafe` (services.new_token())
# and stored in a DB table (`token`), then VERIFIED BY DB LOOKUP. Module 1's
# `set_pw_token` follows the identical shape (a random `jti` row with expiry +
# one-time `used_at`). So faculty tokens use the SAME pattern, not an HMAC:
#   * the SECRET is the unguessable random `jti` itself — an attacker cannot
#     forge a jti that exists in the table, so "valid signature" == "this jti is
#     a real, un-tampered row" (there is no separate signature to check);
#   * EXPIRY is the `expires_at` column;
#   * ONE-TIME use is the `used_at` column (stamped on redemption);
#   * SCOPE is the `offering_id` column — verify() is told which offering the
#     caller is trying to act on and rejects a token minted for a different one.
# This matches services.new_token() + the `token`/`set_pw_token` tables exactly.
#
# The faculty_token table is CYCLE-SCOPED (it lives in the per-cycle DB beside
# atr/atr_event), because a faculty link is about one offering in one cycle and
# archives with that cycle — see schema_cycle.sql.
#
# ANONYMITY (spec §3.2): a faculty token references an OFFERING and a faculty
# email (identity of the teacher, which the offering already carries), never a
# student or a response. Nothing here can reach the anonymous answer side.
# ----------------------------------------------------------------------------

from datetime import datetime, timedelta   # expiry math (stdlib only)

import services   # reuse services.new_token() — the app's ONE token minter


# Purpose constants — the two kinds of faculty link (spec §3.1). ATR_FILE is a
# state-changing, one-time link (the "File ATR" button → a SUBMIT). VIEW is a
# read-only link to an offering's own report and may be reused until it expires.
# Named constants so the issuer, verifier and tests share one spelling.
PURPOSE_ATR_FILE = "ATR_FILE"
PURPOSE_VIEW = "VIEW"

# Default lifetimes. A filing link lives long enough for a busy faculty member to
# get to it (14 days), matching the "act from anywhere, within the cycle window"
# intent (spec §16). Overridable per call. Kept here as the single source.
DEFAULT_TTL_DAYS = 14


# ----------------------------------------------------------------------------
# VerifyResult — a tiny, explicit result object so callers (routes, tests) get a
# clear (ok, reason, row) triple instead of juggling exceptions or None. `ok` is
# the go/no-go; `reason` is a short machine-stable code for the FAILURE case
# (used by tests and logging); `row` is the faculty_token DB row on success.
# ----------------------------------------------------------------------------
class VerifyResult:
    def __init__(self, ok, reason=None, row=None):
        self.ok = ok
        self.reason = reason      # 'bad_signature' | 'expired' | 'used' | 'wrong_offering' | 'wrong_purpose'
        self.row = row

    def __bool__(self):
        # Lets callers write `if verify(...):` naturally.
        return self.ok

    def __repr__(self):
        return "VerifyResult(ok=%r, reason=%r)" % (self.ok, self.reason)


# ----------------------------------------------------------------------------
# _now_iso() / _parse_iso(s) — timestamp helpers. We store expiry as an ISO
# 'YYYY-MM-DD HH:MM:SS' string, the SAME text format SQLite's datetime('now')
# uses, so comparisons against DB-written timestamps are apples-to-apples.
# ----------------------------------------------------------------------------
def _now_iso():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _parse_iso(s):
    # Tolerant parse: accept the fractional-seconds form too, just in case.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


# ----------------------------------------------------------------------------
# issue(cycle, offering_id, faculty_email, purpose=ATR_FILE, ttl_days=..,
#       now=None) -> (jti, expires_at)
# ----------------------------------------------------------------------------
# Mint ONE faculty magic-link token for one offering and store it in the per-
# cycle faculty_token table. Returns the token id (`jti`, which goes into the
# link URL as ?token=<jti>) and its expiry. The jti is produced by
# services.new_token() — the very same unguessable-random minter the student
# links use — so there is one token-generation code path in the whole app.
# Does NOT commit; the caller owns the transaction (so issuing a batch of links
# during distribution is one atomic write).
#
#   cycle          an open per-cycle connection (where faculty_token lives)
#   offering_id    the ONE offering this link may act on (its narrow scope)
#   faculty_email  who it is for (audit; taken from offering.faculty_email)
#   purpose        ATR_FILE (default, one-time SUBMIT) or VIEW (read-only)
#   ttl_days       lifetime in days (default 14)
#   now            optional injected 'now' (a datetime) so tests can mint an
#                  already-expired token deterministically; defaults to utcnow.
# ----------------------------------------------------------------------------
def issue(cycle, offering_id, faculty_email, purpose=PURPOSE_ATR_FILE,
          ttl_days=DEFAULT_TTL_DAYS, now=None):
    jti = services.new_token()                       # the app's single token minter
    base = now if now is not None else datetime.utcnow()
    expires_at = (base + timedelta(days=ttl_days)).strftime("%Y-%m-%d %H:%M:%S")
    cycle.execute(
        "INSERT INTO faculty_token (jti, offering_id, faculty_email, purpose, "
        "                           expires_at, used_at) "
        "VALUES (?, ?, ?, ?, ?, NULL)",
        (jti, offering_id, faculty_email, purpose, expires_at),
    )
    return jti, expires_at


# ----------------------------------------------------------------------------
# verify(cycle, jti, expected_offering_id=None, expected_purpose=ATR_FILE,
#        now=None) -> VerifyResult
# ----------------------------------------------------------------------------
# The gate every faculty action passes through. It rejects, in order:
#   * bad_signature  — no row for this jti (an unknown/garbage/forged token).
#                      With opaque random tokens this IS the signature check: a
#                      jti that is not in the table cannot have been issued by us.
#   * wrong_purpose  — the token was minted for a different purpose (e.g. a VIEW
#                      link cannot be used to FILE an ATR).
#   * wrong_offering — the token is bound to a different offering than the one
#                      the caller is trying to act on (narrow-scope enforcement,
#                      §5.2 — "every action cryptographically tied to one subject").
#   * expired        — past expires_at.
#   * used           — already redeemed (one-time-use for ATR_FILE links).
# Only if all checks pass does it return ok=True with the row. This function does
# NOT mark the token used — redemption is a separate, explicit step (mark_used)
# taken once the action actually succeeds, so a token is not burned by a mere
# page load that later fails.
# ----------------------------------------------------------------------------
def verify(cycle, jti, expected_offering_id=None,
           expected_purpose=PURPOSE_ATR_FILE, now=None):
    row = cycle.execute(
        "SELECT * FROM faculty_token WHERE jti = ?", (jti,)).fetchone()

    # 1. Unknown token → cannot have been issued by us → treat as a bad signature.
    if row is None:
        return VerifyResult(False, "bad_signature")

    # 2. Purpose must match what the caller expects (VIEW link can't SUBMIT).
    if expected_purpose is not None and row["purpose"] != expected_purpose:
        return VerifyResult(False, "wrong_purpose", row)

    # 3. Scope: the token must be for the offering the caller is acting on.
    if (expected_offering_id is not None
            and int(row["offering_id"]) != int(expected_offering_id)):
        return VerifyResult(False, "wrong_offering", row)

    # 4. Expiry: compare against the SAME clock the tests can inject.
    now_dt = now if now is not None else datetime.utcnow()
    exp = _parse_iso(row["expires_at"])
    if exp is None or now_dt > exp:
        return VerifyResult(False, "expired", row)

    # 5. One-time use: an already-redeemed ATR_FILE link is dead.
    if row["used_at"] is not None:
        return VerifyResult(False, "used", row)

    return VerifyResult(True, None, row)


# ----------------------------------------------------------------------------
# mark_used(cycle, jti, now=None) -> None
# ----------------------------------------------------------------------------
# Burn a one-time token by stamping used_at, AFTER the action it authorised has
# succeeded. Kept separate from verify() so a token is never consumed by a link
# preview or a failed submit — only a completed SUBMIT (or reminder redemption)
# marks it used. Idempotent-safe: stamping an already-used token is harmless, but
# verify() will already have blocked a second real use. Does NOT commit.
# ----------------------------------------------------------------------------
def mark_used(cycle, jti, now=None):
    stamp = (now.strftime("%Y-%m-%d %H:%M:%S")
             if now is not None else _now_iso())
    cycle.execute(
        "UPDATE faculty_token SET used_at = ? WHERE jti = ? AND used_at IS NULL",
        (stamp, jti),
    )
