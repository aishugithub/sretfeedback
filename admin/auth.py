# ============================================================================
# admin/auth.py  —  v2.1 : ADMIN login, logout, "forgot password", and the gate
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# Until now the whole /admin/* console was OPEN — anyone who could reach the URL
# was "the admin" (the person at the laptop). The professor asked for a real login
# with TWO named admin accounts (aishwarya@sret.edu.in, lekha@sret.edu.in) so the
# two operators are individually ACCOUNTABLE for what they do.
#
# HOW IT REUSES WHAT ALREADY EXISTS (nothing re-invented):
#   * Admin accounts are ordinary `app_user` rows with role = 'ADMIN' and scope
#     'ALL', seeded by seed_admins.py — so they get the SAME salted-PBKDF2 password
#     hashing and the SAME one-time "set / reset your password" email link the
#     leaders already use (auth_leaders.py + the /leader/set-password page). An
#     admin never has a password set by anyone else — they set their own.
#   * Login reuses auth_leaders.authenticate() but additionally REQUIRES role
#     ADMIN, so a leader (HOD/VD/Dean) can never log in at the admin door.
#   * "Forgot password" reuses auth_leaders.issue_set_pw_token(purpose='RESET')
#     and emails the SAME set-password link — that is the answer to "how do we get
#     back in if we forget our login": request a reset link at /admin/forgot. If
#     BOTH admins are ever locked out (e.g. email down), admin_reset.py is a
#     server-console fallback that prints a fresh link or sets a password directly.
#
# THE GATE: a single @admin_bp.before_request checks for a logged-in admin session
# on EVERY /admin/* request, except the handful of pre-login endpoints below. It is
# the one choke-point that closes the whole console at once, so no individual admin
# route can be forgotten and left open.
# ----------------------------------------------------------------------------

from flask import (render_template, request, redirect, url_for, flash, session)

from db import get_master
from config import Config
from admin import admin_bp
import auth_leaders          # PBKDF2 verify + set/reset-password tokens (reused)
import emailer               # gmail-api > smtp > dev-outbox sender (reused)
import rbac                  # ROLE_ADMIN
import activity_log          # audit note()
# Reuse the leaders' link builders so the admin reset link is byte-identical to
# the leader one (same /leader/set-password page redeems it — it is role-agnostic).
from admin.users import _invite_base_url, _set_pw_link


# Session keys that mark a logged-in admin. Kept as constants so the gate, the
# login route and activity_log all spell them the same way.
SESSION_ID = "admin_id"
SESSION_EMAIL = "admin_email"
SESSION_NAME = "admin_name"

# The ONLY admin endpoints reachable WITHOUT being logged in. Everything else under
# /admin/* is gated. (The set-password page lives on the atr blueprint, so a locked-
# out admin can always redeem an emailed reset link even while the console is shut.)
_PUBLIC_ENDPOINTS = {
    "admin.admin_login",
    "admin.admin_logout",
    "admin.admin_forgot",
}


# ----------------------------------------------------------------------------
# current_admin() -> dict | None — the logged-in admin (from the session), or None.
# A tiny accessor other admin code can use to know WHO is acting.
# ----------------------------------------------------------------------------
def _row_is_admin(row):
    """True if an app_user row carries admin access. Reads the is_admin flag,
    tolerating an older row that predates the column (then falls back to role)."""
    try:
        if row["is_admin"]:
            return True
    except (KeyError, IndexError, TypeError):
        pass
    # Fallback for a not-yet-migrated DB: a pure-admin sentinel role still counts.
    try:
        return (row["role"] or "").upper() == rbac.ROLE_ADMIN
    except (KeyError, IndexError, TypeError):
        return False


def current_admin():
    if not session.get(SESSION_ID):
        return None
    return {"id": session.get(SESSION_ID),
            "email": session.get(SESSION_EMAIL),
            "name": session.get(SESSION_NAME)}


# ----------------------------------------------------------------------------
# THE GATE — runs before every /admin/* request (blueprint-scoped before_request).
# ----------------------------------------------------------------------------
# Returning None lets the request proceed; returning a redirect short-circuits it.
# We let the pre-login endpoints through, allow the logged-in admin through, and
# bounce everyone else to the login page (remembering where they were headed so we
# can send them back after a successful login).
# ----------------------------------------------------------------------------
@admin_bp.before_request
def _require_admin_login():
    endpoint = request.endpoint or ""
    if endpoint in _PUBLIC_ENDPOINTS:
        return None                      # login / logout / forgot are always open
    if session.get(SESSION_ID):
        return None                      # a logged-in admin — proceed
    # Not logged in: remember the intended path and send to the login page.
    flash("Please log in to use the admin console.", "error")
    return redirect(url_for("admin.admin_login", next=request.full_path))


# ----------------------------------------------------------------------------
# GET/POST /admin/login  —  email + password, restricted to ADMIN accounts.
# ----------------------------------------------------------------------------
@admin_bp.route("/login", methods=["GET", "POST"])
def admin_login():
    # If already logged in, skip straight to the dashboard.
    if session.get(SESSION_ID) and request.method == "GET":
        return redirect(url_for("admin.dashboard"))

    if request.method == "GET":
        return render_template("admin_login.html",
                               next=request.args.get("next", ""))

    email = (request.form.get("email", "") or "").strip()
    password = request.form.get("password", "") or ""
    nxt = (request.form.get("next", "") or "").strip()

    master = get_master()
    try:
        user = auth_leaders.authenticate(master, email, password)
        # authenticate() succeeds for ANY active app_user with a matching password;
        # the admin door additionally REQUIRES the is_admin flag. This is what lets
        # the Vice Dean (role=VICE_DEAN, is_admin=1) log in here while a plain leader
        # (is_admin=0) cannot — access is the flag, never the org-tree role.
        if user is None or not _row_is_admin(user):
            master.close()
            activity_log.note(actor_label=(email or "unknown"),
                              detail="Failed ADMIN login attempt")
            flash("Invalid email or password, or not an admin account "
                  "(password may not be set yet — use “Forgot password”).", "error")
            return redirect(url_for("admin.admin_login", next=nxt))
        master.commit()                  # persist last_login_at stamped in authenticate
        session[SESSION_ID] = user["id"]
        session[SESSION_EMAIL] = user["email"]
        session[SESSION_NAME] = user["name"] or user["email"]
    finally:
        master.close()

    activity_log.note(detail="Admin %s logged in" % email)
    # Only honour a SAME-SITE relative next path (never an open redirect off-site).
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(url_for("admin.dashboard"))


# ----------------------------------------------------------------------------
# GET /admin/logout  —  clear the admin session.
# ----------------------------------------------------------------------------
@admin_bp.route("/logout")
def admin_logout():
    who = session.get(SESSION_EMAIL)
    for k in (SESSION_ID, SESSION_EMAIL, SESSION_NAME):
        session.pop(k, None)
    if who:
        activity_log.note(detail="Admin %s logged out" % who)
    flash("Logged out.", "success")
    return redirect(url_for("admin.admin_login"))


# ----------------------------------------------------------------------------
# GET/POST /admin/forgot  —  self-service password reset (the "forgot login" path).
# ----------------------------------------------------------------------------
# The admin types their email; if it matches an ACTIVE admin account we mint a
# one-time RESET link and email it (the same link the leaders use). We ALWAYS show
# the same neutral confirmation regardless of whether the email matched, so this
# page can never be used to discover which addresses are admin accounts.
# ----------------------------------------------------------------------------
@admin_bp.route("/forgot", methods=["GET", "POST"])
def admin_forgot():
    if request.method == "GET":
        return render_template("admin_forgot.html")

    email = (request.form.get("email", "") or "").strip().lower()
    neutral = ("If %s is a registered admin account, a password-reset link has "
               "been sent to it. The link is valid for %d days."
               % (email or "that address", auth_leaders.SET_PW_TTL_DAYS))

    master = get_master()
    try:
        row = master.execute(
            "SELECT * FROM app_user WHERE email = ? AND is_admin = 1 "
            "AND status = 'active'", (email,)).fetchone()
        if row is not None:
            jti = auth_leaders.issue_set_pw_token(master, row["id"], purpose="RESET")
            link = _set_pw_link(_invite_base_url(), jti)
            body = "\n".join([
                "Dear %s," % (row["name"] or "Admin"),
                "",
                "A password reset was requested for your SRET Feedback System "
                "ADMIN account. Set a new password using the secure one-time link "
                "below:",
                "", link, "",
                "This link is valid for %d days and can be used only once. If you "
                "did not request this, you can ignore this email — your current "
                "password remains unchanged." % auth_leaders.SET_PW_TTL_DAYS,
                "", "— Automated Feedback System, SRET",
            ])
            emailer.send_batch(
                Config.BASE_DIR, "[AFS] Reset your admin password",
                [{"to": row["email"], "body": body}], test_level=0)  # real send
            master.commit()
            activity_log.note(actor_label=email,
                              detail="Admin password-reset link issued")
    finally:
        master.close()

    flash(neutral, "success")
    return redirect(url_for("admin.admin_login"))
