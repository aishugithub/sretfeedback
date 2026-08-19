# ============================================================================
# admin/users.py  —  Version 2.0 · Admin "Users & Roles" screen (§17 IAM)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# Module 1 seeded ~13 leader accounts (one HOD per department, a Vice Dean and a
# Dean) into master.db.app_user, each with an EMPTY password (§17.2 — "the admin
# holds no passwords; users set their own"). Module 2 built the leader-facing
# side: /leader/login and /leader/set-password, plus the crypto/token plumbing in
# auth_leaders.py. But there was NO admin-facing page to actually MANAGE those
# accounts or to KICK OFF the "set your password" emails — issue_set_pw_token()
# existed yet was never called anywhere. This module is that missing page.
#
# WHAT IT ADDS (all under the existing admin blueprint, url_prefix="/admin"):
#     GET  /admin/users                     list every leader + status
#     GET  /admin/users/new                 blank create form
#     POST /admin/users/new                 create a leader
#     GET  /admin/users/<id>/edit           edit form (name/email/role/scope)
#     POST /admin/users/<id>/edit           save edits
#     POST /admin/users/<id>/invite         email ONE leader a set-password link
#     POST /admin/users/invite-all          email EVERY leader still without a pw
#     POST /admin/users/<id>/toggle         enable/disable a leader account
#
# HOW THE PIECES CONNECT (nothing here is re-invented):
#   * auth_leaders.issue_set_pw_token(master, user_id, purpose) mints the one-time
#     jti row in set_pw_token; we turn it into the absolute URL
#     <base>/leader/set-password?token=<jti> that the leader clicks.
#   * emailer.send_batch(...) is the SAME sender the student/ATR mail uses, so the
#     invite automatically flows through gmail-api > smtp > dev-outbox with no
#     extra transport code. (This is also a real end-to-end test of the Gmail
#     setup we just configured.)
#   * rbac.py defines the role constants + the scope model; we store scope exactly
#     as rbac expects (a single E-code for an HOD, a CSV or the 'ALL' sentinel for
#     a Vice Dean, and 'ALL' for the Dean).
#   * admin_log (Module 1 table) records each sensitive IAM action (§17.3), and
#     activity_log.note() adds a human detail line to the system-wide log.
#
# ANONYMITY: this screen touches ONLY master.db.app_user / set_pw_token — never a
# per-cycle answer file. It is entirely about the ~13 password leaders and cannot
# reach any student response. The two-file anonymity split is untouched.
# ----------------------------------------------------------------------------

import os                                   # read FEEDBACK_PUBLIC_BASE_URL fallback
from flask import (render_template, request, redirect, url_for, flash,
                   current_app)

from db import get_master                   # the ONE master.db opener (WAL, FK)
from config import Config                   # BASE_DIR (for the outbox) + DEPT_CODES
from admin import admin_bp                  # the shared admin blueprint
import auth_leaders                         # password hashing + set-pw tokens
import emailer                              # gmail-api > smtp > dev-outbox sender
import rbac                                 # ROLE_* constants + SCOPE_ALL sentinel
import activity_log                         # system-wide audit note()


# The three roles a leader account may hold, in display/rank order. Kept here as a
# single list so the form dropdown and the list-page sort agree on one ordering.
_ROLES = [rbac.ROLE_DEAN, rbac.ROLE_VICE_DEAN, rbac.ROLE_HOD]

# Human labels for the roles (the DB stores the bare constant; the UI shows this).
_ROLE_LABEL = {
    rbac.ROLE_DEAN: "Dean",
    rbac.ROLE_VICE_DEAN: "Vice Dean",
    rbac.ROLE_HOD: "HOD",
}

# Sort rank so the list shows Dean, then Vice Dean, then HODs (by dept), which is
# how people think of the org tree. A missing/odd role sorts last.
_ROLE_RANK = {rbac.ROLE_DEAN: 0, rbac.ROLE_VICE_DEAN: 1, rbac.ROLE_HOD: 2}


# ----------------------------------------------------------------------------
# _invite_base_url() -> str
# ----------------------------------------------------------------------------
# The absolute root the emailed set-password link must start with. A leader may
# open it from anywhere (an HOD on the college network, or off-site), so the link
# has to be an absolute, reachable URL — NOT localhost.
#
#   * Preferred: FEEDBACK_PUBLIC_BASE_URL (set once per deployment, e.g. the
#     PythonAnywhere URL). This matches notifications.public_base_url(), so the
#     leader set-password link and the ATR review links share one base.
#   * Fallback: request.url_root — the address the admin themselves used to reach
#     this page (e.g. http://192.168.1.7:5000/). On a LAN demo this is exactly the
#     laptop IP other machines use, so links work with zero configuration.
# ----------------------------------------------------------------------------
def _invite_base_url():
    env = os.environ.get("FEEDBACK_PUBLIC_BASE_URL", "").strip()
    if env:
        return env.rstrip("/")
    # request.url_root looks like "http://host:5000/"; drop the trailing slash.
    return request.url_root.rstrip("/")


# ----------------------------------------------------------------------------
# _set_pw_link(base, jti) -> str
# ----------------------------------------------------------------------------
# Build the one-time link a leader clicks to choose their password. The endpoint
# is atr.leader_set_password (GET /leader/set-password?token=<jti>) — defined in
# atr/routes.py. We assemble the query string by hand (rather than url_for(...,
# _external=True)) so it works whether or not we are inside a request context and
# always uses the deployment base URL, not Flask's guessed SERVER_NAME.
# ----------------------------------------------------------------------------
def _set_pw_link(base, jti):
    return "%s/leader/set-password?token=%s" % (base, jti)


# ----------------------------------------------------------------------------
# _invite_body(name, role, link, is_reset) -> str
# ----------------------------------------------------------------------------
# The plain-text body of the set/reset-password email. Deliberately simple and
# functional, matching the tone of notifications.py. Mentions the 7-day expiry
# (auth_leaders.SET_PW_TTL_DAYS) so the recipient knows to act promptly.
# ----------------------------------------------------------------------------
def _invite_body(name, role, link, is_reset=False):
    who = name or "Colleague"
    role_label = _ROLE_LABEL.get(role, role)
    action = ("reset" if is_reset else "set")
    return "\n".join([
        "Dear %s," % who,
        "",
        "You have been given %s access to the SRET Automated Feedback System."
        % role_label,
        "Please %s your password using the secure one-time link below:" % action,
        "",
        link,
        "",
        "This link is valid for %d days and can be used only once. If it "
        "expires, ask the feedback administrator to send a fresh one."
        % auth_leaders.SET_PW_TTL_DAYS,
        "",
        "After setting your password, log in at:",
        _login_link_from(link),
        "",
        "— Automated Feedback System, SRET",
    ])


# ----------------------------------------------------------------------------
# _login_link_from(set_pw_link) -> str — derive the /leader/login URL from the
# set-password URL's base, so the email can also point the leader at the login
# page without recomputing the base separately.
# ----------------------------------------------------------------------------
def _login_link_from(set_pw_link):
    base = set_pw_link.split("/leader/set-password", 1)[0]
    return "%s/leader/login" % base


# ----------------------------------------------------------------------------
# _admin_log(master, action, target_user_id) — write ONE row to admin_log, the
# §17.3 IAM audit trail (CREATE_USER | EDIT | DISABLE | ENABLE | RESEND_LINK ...).
# admin_user_id is NULL because the admin control panel is the trusted operator
# console (there is no separate admin login in this app); the ACTION + target is
# what matters for the trail. Does NOT commit — the caller owns the transaction.
# ----------------------------------------------------------------------------
def _admin_log(master, action, target_user_id=None):
    # v2.1: the console is now login-gated, so we can attribute each IAM action to
    # the SPECIFIC admin who performed it (session['admin_id']) instead of NULL —
    # this is what makes the two operators individually accountable. Falls back to
    # NULL for a system/CLI context where there is no session.
    from flask import session
    admin_user_id = session.get("admin_id")
    master.execute(
        "INSERT INTO admin_log (admin_user_id, action, target_user_id) "
        "VALUES (?, ?, ?)", (admin_user_id, action, target_user_id))


# ----------------------------------------------------------------------------
# _compute_scope(role, selected_codes) -> (scope_str, error_or_None)
# ----------------------------------------------------------------------------
# Turn the role + the department codes ticked in the form into the exact string
# rbac.py expects in app_user.scope_dept_ids:
#   * DEAN       -> always 'ALL' (a Dean is unconditionally college-wide).
#   * VICE_DEAN  -> 'ALL' if the ALL option (or nothing) was chosen, else the CSV
#                   of ticked E-codes (a VD may oversee a subset of departments).
#   * HOD        -> exactly ONE E-code; anything else is a validation error, so an
#                   HOD can never be accidentally scoped to many departments (that
#                   would break the "an E01 HOD can never see E02" guarantee).
# Returns (scope, None) on success or ('', "message") on a validation problem.
# ----------------------------------------------------------------------------
def _compute_scope(role, selected_codes):
    # Normalise: uppercase, drop blanks/dupes while preserving order.
    codes = []
    for c in selected_codes:
        c = (c or "").strip().upper()
        if c and c not in codes:
            codes.append(c)

    if role == rbac.ROLE_DEAN:
        return rbac.SCOPE_ALL, None

    if role == rbac.ROLE_VICE_DEAN:
        # 'ALL' anywhere (or nothing ticked) => whole college.
        if not codes or rbac.SCOPE_ALL in codes:
            return rbac.SCOPE_ALL, None
        return ",".join(codes), None

    if role == rbac.ROLE_HOD:
        # v2.2 (§18): an HOD may now hold ONE OR MORE real departments — exactly
        # like a Vice Dean's departmental subset — so the SAME person can head two
        # program codes (e.g. 'E01,E05'). We still forbid the 'ALL' sentinel for an
        # HOD (only VD/Dean are ever college-wide) and still require at least one
        # real, KNOWN department code. The multi-code value is stored as the same
        # CSV that rbac.allowed_dept_codes() already parses, so NO rbac change is
        # needed — this branch only relaxes the old "exactly one" rule that used to
        # block a shared HOD.
        real = [c for c in codes if c != rbac.SCOPE_ALL]
        if not real:
            return "", "An HOD must be scoped to at least one department."
        unknown = [c for c in real if c not in Config.DEPT_CODES]
        if unknown:
            return "", "Unknown department code(s): %s" % ", ".join(unknown)
        # `codes` is already upper-cased and de-duped (order preserved) above; join
        # into the CSV rbac expects. One code -> "E01"; several -> "E01,E05".
        return ",".join(real), None

    return "", "Unknown role: %s" % role


# ----------------------------------------------------------------------------
# _reconcile_hod_departments(master, user_id, role, scope) -> list[str] warnings
# ----------------------------------------------------------------------------
# v2.2 (§18.4): keep department.hod_user_id (the FORWARD pointer) in agreement
# with app_user.scope_dept_ids (the REVERSE scope). This is the heart of the
# shared-HOD change, so it is worth spelling out how the two facts relate:
#
#   * scope_dept_ids  — read by rbac.py to decide what an HOD may SEE / ENDORSE
#                       (authorization). Already a CSV; already multi-dept.
#   * department.hod_user_id — read by notifications._leader_email() to decide
#                       WHO RECEIVES a department's ATR endorsement mail, and by
#                       admin/status._hod_by_dept() to NAME the pending HOD
#                       (routing + display).
#
# If we only widened the scope, a two-department HOD would SEE the second dept but
# its endorsement mail would still go to whoever seed_leaders.py wired long ago.
# So on every create/edit we make the ticked scope the single source of truth and
# MIRROR it onto department.hod_user_id. Rules:
#
#   role == HOD : every dept in `scope` gets hod_user_id = this user; any dept the
#                 user USED to hold but is no longer scoped for is detached
#                 (-> NULL). A department has exactly ONE routing HOD, so claiming
#                 a dept currently held by a DIFFERENT HOD REASSIGNS it here (the
#                 professor's chosen "reassign + warn" policy): we point the dept
#                 at this user AND strip that dept from the previous holder's own
#                 scope, so authorization and routing stay in lock-step. Each such
#                 reassignment is returned as a human warning string.
#   role != HOD : a former HOD promoted to VD/Dean must no longer be any dept's
#                 routing HOD, so we detach every dept still pointing at them.
#
# Does NOT commit — the caller owns the transaction, so the app_user write and
# this mirror land together (they must never diverge).
# ----------------------------------------------------------------------------
def _reconcile_hod_departments(master, user_id, role, scope):
    warnings = []

    # The departments this user should own as routing HOD AFTER this save. Only an
    # HOD owns departments; any other role owns none (wanted stays empty).
    if role == rbac.ROLE_HOD:
        wanted = [c.strip().upper() for c in (scope or "").split(",") if c.strip()]
    else:
        wanted = []

    # 1. DETACH — any department this user currently holds but no longer should.
    #    (Covers both "dropped a dept from an HOD's scope" and "role changed away
    #    from HOD".) We NULL the pointer; notifications then treats that seat as
    #    unfilled until an HOD is assigned, exactly as for a brand-new dept.
    held_now = master.execute(
        "SELECT code FROM department WHERE hod_user_id = ?", (user_id,)).fetchall()
    for r in held_now:
        if r["code"] not in wanted:
            master.execute(
                "UPDATE department SET hod_user_id = NULL WHERE code = ?",
                (r["code"],))

    # 2. CLAIM — point each wanted department at this user, reassigning from any
    #    other HOD (and stripping it from that HOD's scope) so the "one dept = one
    #    HOD" invariant holds on BOTH the routing and the authorization path.
    for code in wanted:
        row = master.execute(
            "SELECT hod_user_id FROM department WHERE code = ?", (code,)).fetchone()
        if row is None:
            continue                      # unknown code (validated already; be safe)
        prev = row["hod_user_id"]
        if prev is not None and prev != user_id:
            # Look up the previous holder to (a) word the warning and (b) remove
            # this dept from THEIR scope so they no longer see it in rbac.
            prev_row = master.execute(
                "SELECT email, scope_dept_ids FROM app_user WHERE id = ?",
                (prev,)).fetchone()
            prev_email = prev_row["email"] if prev_row else ("user #%s" % prev)
            warnings.append(
                "Department %s was reassigned to this HOD (previously held by %s)."
                % (code, prev_email))
            if prev_row is not None:
                kept = [c.strip().upper()
                        for c in (prev_row["scope_dept_ids"] or "").split(",")
                        if c.strip() and c.strip().upper() != code]
                master.execute(
                    "UPDATE app_user SET scope_dept_ids = ? WHERE id = ?",
                    (",".join(kept), prev))
        if prev != user_id:
            master.execute(
                "UPDATE department SET hod_user_id = ? WHERE code = ?",
                (user_id, code))

    return warnings


# ----------------------------------------------------------------------------
# _validate_email(email) -> (clean_or_None, error_or_None) — a light e-mail check
# (the app deals in institutional addresses, not arbitrary input). We lower-case
# it because auth_leaders.authenticate() looks the address up lower-cased.
# ----------------------------------------------------------------------------
def _validate_email(email):
    e = (email or "").strip().lower()
    if not e or "@" not in e or e.startswith("@") or e.endswith("@"):
        return None, "A valid email address is required."
    return e, None


# ============================================================================
# LIST  —  GET /admin/users
# ============================================================================
# Show every leader with role, scope, whether a password has been set, and the
# last login. The template turns each row into edit / invite / enable-disable
# controls, and offers the two top-level actions: "Add leader" and "Send invites
# to all without a password".
# ----------------------------------------------------------------------------
@admin_bp.route("/users")
def users_list():
    master = get_master()
    rows = master.execute(
        "SELECT id, email, name, role, scope_dept_ids, "
        "       (pw_hash != '') AS has_pw, status, atr_email_enabled, "
        "       last_login_at, created_at "
        "FROM app_user"
    ).fetchall()
    master.close()

    # Sort in Python by (role rank, scope, email) so the org tree reads top-down.
    users = sorted(
        rows,
        key=lambda r: (_ROLE_RANK.get(r["role"], 9),
                       r["scope_dept_ids"] or "", r["email"]))

    # How many active leaders still have no password — drives the bulk button.
    pending = sum(1 for r in users if r["status"] == "active" and not r["has_pw"])

    return render_template(
        "users_list.html",
        users=users, pending=pending,
        role_label=_ROLE_LABEL, dept_codes=Config.DEPT_CODES)


# ============================================================================
# CREATE  —  GET/POST /admin/users/new
# ============================================================================
@admin_bp.route("/users/new", methods=["GET", "POST"])
def user_new():
    if request.method == "GET":
        # Blank form. `user=None` tells the template it is in "create" mode.
        return render_template(
            "user_edit.html", user=None, roles=_ROLES,
            role_label=_ROLE_LABEL, dept_codes=Config.DEPT_CODES,
            selected_codes=[])

    # ---- POST: validate then insert -----------------------------------------
    name = (request.form.get("name", "") or "").strip()
    role = (request.form.get("role", "") or "").strip().upper()
    email, e_err = _validate_email(request.form.get("email", ""))
    scope, s_err = _compute_scope(role, request.form.getlist("scope"))

    # Collect any validation problem and re-render the form with what was typed.
    err = e_err or (None if role in _ROLES else "Please choose a valid role.") or s_err
    if err:
        flash(err, "error")
        return render_template(
            "user_edit.html", user=_form_echo(None, name, email, role),
            roles=_ROLES, role_label=_ROLE_LABEL, dept_codes=Config.DEPT_CODES,
            selected_codes=request.form.getlist("scope"))

    master = get_master()
    # Guard the UNIQUE(email) constraint with a friendly message instead of a 500.
    exists = master.execute(
        "SELECT 1 FROM app_user WHERE email = ?", (email,)).fetchone()
    if exists:
        master.close()
        flash("A user with that email already exists.", "error")
        return render_template(
            "user_edit.html", user=_form_echo(None, name, email, role),
            roles=_ROLES, role_label=_ROLE_LABEL, dept_codes=Config.DEPT_CODES,
            selected_codes=request.form.getlist("scope"))

    # Email-preference switch at creation. An unchecked box means "don't send this
    # leader the per-ATR endorsement mails"; a ticked box (the default in the form)
    # means 1. HTML only POSTs a checkbox when it is ticked, so absence == 0.
    atr_email = 1 if request.form.get("atr_email") == "on" else 0
    # pw_hash stays '' — the leader sets their own password from the invite link.
    master.execute(
        "INSERT INTO app_user (email, name, role, scope_dept_ids, pw_hash, "
        "                      status, atr_email_enabled, created_by) "
        "VALUES (?, ?, ?, ?, '', 'active', ?, ?)",
        (email, name, role, scope, atr_email, "admin:users_page"))
    new_id = master.execute(
        "SELECT id FROM app_user WHERE email = ?", (email,)).fetchone()["id"]
    # v2.2 (§18.4): mirror the (possibly multi-department) scope onto
    # department.hod_user_id so ATR routing/status agree with what this HOD sees.
    # Runs inside the same transaction as the INSERT so the two never diverge.
    warns = _reconcile_hod_departments(master, new_id, role, scope)
    _admin_log(master, "CREATE_USER", new_id)
    master.commit()
    master.close()

    activity_log.note(detail="Created leader %s (%s, scope %s)"
                      % (email, role, scope))
    flash("Leader created. Use “Send invite” to email them a set-password link.",
          "success")
    # Surface any "reassigned from previous HOD" notes (the 'warn' half of the
    # professor's reassign+warn policy) as their own messages.
    for w in warns:
        flash(w, "success")
    return redirect(url_for("admin.users_list"))


# ============================================================================
# EDIT  —  GET/POST /admin/users/<id>/edit
# ============================================================================
@admin_bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
def user_edit(user_id):
    master = get_master()
    row = master.execute(
        "SELECT * FROM app_user WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        master.close()
        flash("No such user.", "error")
        return redirect(url_for("admin.users_list"))

    if request.method == "GET":
        master.close()
        # Pre-tick the current scope in the multi-select. A single HOD code or a
        # VD CSV both split cleanly; 'ALL' stays a single selected option.
        selected = [c.strip() for c in (row["scope_dept_ids"] or "").split(",") if c.strip()]
        return render_template(
            "user_edit.html", user=row, roles=_ROLES,
            role_label=_ROLE_LABEL, dept_codes=Config.DEPT_CODES,
            selected_codes=selected)

    # ---- POST: validate then update -----------------------------------------
    name = (request.form.get("name", "") or "").strip()
    role = (request.form.get("role", "") or "").strip().upper()
    email, e_err = _validate_email(request.form.get("email", ""))
    scope, s_err = _compute_scope(role, request.form.getlist("scope"))
    status = "disabled" if request.form.get("disabled") == "on" else "active"
    # Email-preference switch: ticked "Send ATR notification emails" -> 1, else 0.
    # A checkbox is absent from the POST when unticked, so "!= 'on'" reads as 0.
    atr_email = 1 if request.form.get("atr_email") == "on" else 0

    err = e_err or (None if role in _ROLES else "Please choose a valid role.") or s_err
    if err:
        master.close()
        flash(err, "error")
        return redirect(url_for("admin.user_edit", user_id=user_id))

    # Uniqueness check must EXCLUDE this same row (editing without changing email).
    clash = master.execute(
        "SELECT 1 FROM app_user WHERE email = ? AND id != ?",
        (email, user_id)).fetchone()
    if clash:
        master.close()
        flash("Another user already uses that email.", "error")
        return redirect(url_for("admin.user_edit", user_id=user_id))

    master.execute(
        "UPDATE app_user SET name = ?, email = ?, role = ?, scope_dept_ids = ?, "
        "                    status = ?, atr_email_enabled = ? WHERE id = ?",
        (name, email, role, scope, status, atr_email, user_id))
    # v2.2 (§18.4): re-mirror scope onto department.hod_user_id. This also handles
    # a role CHANGE away from HOD (the user is detached from every dept) and a
    # scope that DROPS a department (that dept's pointer is cleared) — all inside
    # the same transaction as the UPDATE above.
    warns = _reconcile_hod_departments(master, user_id, role, scope)
    _admin_log(master, "EDIT_USER", user_id)
    master.commit()
    master.close()

    activity_log.note(detail="Edited leader %s (%s, scope %s, %s, ATR-mail %s)"
                      % (email, role, scope, status,
                         "on" if atr_email else "off"))
    flash("Saved.", "success")
    for w in warns:
        flash(w, "success")
    return redirect(url_for("admin.users_list"))


# ============================================================================
# INVITE ONE  —  POST /admin/users/<id>/invite
# ============================================================================
# Mint a one-time set/reset link for a single leader and email it. If the leader
# already has a password this is a RESET; otherwise a first-time SET. Uses the
# shared emailer, so it goes out via whatever mode is configured (ideally
# gmail-api). Reports the mode + any error back to the admin via flash.
# ----------------------------------------------------------------------------
@admin_bp.route("/users/<int:user_id>/invite", methods=["POST"])
def user_invite(user_id):
    master = get_master()
    row = master.execute(
        "SELECT * FROM app_user WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        master.close()
        flash("No such user.", "error")
        return redirect(url_for("admin.users_list"))
    if row["status"] != "active":
        master.close()
        flash("That account is disabled — enable it before sending an invite.",
              "error")
        return redirect(url_for("admin.users_list"))

    is_reset = bool(row["pw_hash"])                 # already has a password?
    purpose = "RESET" if is_reset else "SET"
    # Mint the token (writes a set_pw_token row; we commit once the email is queued).
    jti = auth_leaders.issue_set_pw_token(master, user_id, purpose=purpose)
    link = _set_pw_link(_invite_base_url(), jti)
    body = _invite_body(row["name"], row["role"], link, is_reset=is_reset)
    subject = "[AFS] %s your Feedback System password" % ("Reset" if is_reset else "Set")

    # Send via the shared mailer. is_test=False -> a real, non-redirected send
    # (these invites are admin actions, not part of a test cycle).
    result = emailer.send_batch(
        Config.BASE_DIR, subject, [{"to": row["email"], "body": body}],
        is_test=False)

    # Only burn the transaction (keep the token) if at least the send was attempted.
    _admin_log(master, "RESEND_LINK" if is_reset else "INVITE", user_id)
    master.commit()
    master.close()

    _flash_send_result(result, one_addr=row["email"])
    activity_log.note(detail="%s link emailed to %s (mode %s)"
                      % (purpose, row["email"], result.get("mode")))
    return redirect(url_for("admin.users_list"))


# ============================================================================
# INVITE ALL PENDING  —  POST /admin/users/invite-all
# ============================================================================
# Email a first-time set-password link to EVERY active leader who has not set a
# password yet. Builds one token per leader, sends them as a single batch (one
# call to the mailer), and commits all the tokens together.
# ----------------------------------------------------------------------------
@admin_bp.route("/users/invite-all", methods=["POST"])
def user_invite_all():
    master = get_master()
    pending = master.execute(
        "SELECT id, email, name, role FROM app_user "
        "WHERE status = 'active' AND pw_hash = '' ORDER BY role, email"
    ).fetchall()

    if not pending:
        master.close()
        flash("Every active leader already has a password — nothing to send.",
              "success")
        return redirect(url_for("admin.users_list"))

    base = _invite_base_url()
    messages = []
    for u in pending:
        jti = auth_leaders.issue_set_pw_token(master, u["id"], purpose="SET")
        link = _set_pw_link(base, jti)
        messages.append({
            "to": u["email"],
            "body": _invite_body(u["name"], u["role"], link, is_reset=False),
        })

    result = emailer.send_batch(
        Config.BASE_DIR, "[AFS] Set your Feedback System password",
        messages, is_test=False)

    _admin_log(master, "INVITE_ALL", None)
    master.commit()
    master.close()

    _flash_send_result(result, count=len(messages))
    activity_log.note(detail="Bulk set-password invite to %d leaders (mode %s)"
                      % (len(messages), result.get("mode")))
    return redirect(url_for("admin.users_list"))


# ============================================================================
# TOGGLE ENABLE/DISABLE  —  POST /admin/users/<id>/toggle
# ============================================================================
# Flip a leader between 'active' and 'disabled'. We DISABLE rather than delete so
# the audit trail and any historical endorsements keep their foreign keys intact
# (a disabled account cannot log in — see auth_leaders.authenticate — but its past
# actions remain attributable). This is the §17 "deprovision" control.
# ----------------------------------------------------------------------------
@admin_bp.route("/users/<int:user_id>/toggle", methods=["POST"])
def user_toggle(user_id):
    master = get_master()
    row = master.execute(
        "SELECT status, email FROM app_user WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        master.close()
        flash("No such user.", "error")
        return redirect(url_for("admin.users_list"))

    new_status = "disabled" if row["status"] == "active" else "active"
    master.execute("UPDATE app_user SET status = ? WHERE id = ?",
                   (new_status, user_id))
    _admin_log(master, "DISABLE" if new_status == "disabled" else "ENABLE", user_id)
    master.commit()
    master.close()

    activity_log.note(detail="%s leader %s"
                      % ("Disabled" if new_status == "disabled" else "Enabled",
                         row["email"]))
    flash("Account %s." % ("disabled" if new_status == "disabled" else "enabled"),
          "success")
    return redirect(url_for("admin.users_list"))


# ----------------------------------------------------------------------------
# _flash_send_result(result, one_addr=None, count=None) — turn the mailer summary
# (mode / count / errors) into a friendly flash message, so the admin sees WHAT
# happened (real gmail-api send vs a dev-outbox file) and any per-recipient error.
# ----------------------------------------------------------------------------
def _flash_send_result(result, one_addr=None, count=None):
    mode = result.get("mode", "?")
    errs = result.get("errors") or []
    sent = result.get("count", 0)
    if errs:
        flash("Send finished with problems (mode %s): %s" % (mode, "; ".join(errs)),
              "error")
        return
    if mode == "dev-outbox":
        # No real email configured — the link was written to app/outbox/ as .eml.
        where = result.get("outbox", "app/outbox/")
        flash("No email is configured, so the invite was written to %s as a .eml "
              "file (dev mode). Set the FEEDBACK_GMAIL_* variables to send for real."
              % where, "error")
        return
    target = one_addr if one_addr else ("%d leader(s)" % (count or sent))
    flash("Invite sent to %s via %s." % (target, mode), "success")


# ----------------------------------------------------------------------------
# _form_echo(row, name, email, role) -> dict — a tiny stand-in "user" object so
# the edit template can re-render the values the admin just typed after a
# validation error, whether we are creating (row is None) or editing. It mimics
# the few keys the template reads from a real app_user row.
# ----------------------------------------------------------------------------
def _form_echo(row, name, email, role):
    base = {"id": (row["id"] if row else None),
            "name": name, "email": email, "role": role,
            "status": (row["status"] if row else "active")}
    return base
