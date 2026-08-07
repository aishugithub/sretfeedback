# ============================================================================
# atr/routes.py  —  Version 2.0 · Module 2 : faculty ATR + leader endorsement
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# These are the THIN web wrappers over Module 2's engine. They hold NO workflow
# logic of their own — every state change is delegated to atr_workflow (the §8
# state machine + audit), every token check to faculty_tokens/auth_leaders, every
# notification to notifications, and every access decision to rbac. Keeping the
# routes thin is deliberate: the correctness guarantees (return-one-level-down,
# audit-on-every-move, one-time tokens, "E01 never sees E02") all live in tested,
# pure-ish modules, so a route cannot accidentally violate them.
#
# TWO AUDIENCES:
#   * FACULTY (no login) reach /atr/file?token=<jti> from their result email; the
#     token is a single-purpose, expiring, one-time magic link bound to ONE
#     offering (faculty_tokens). They file/submit the ATR.
#   * LEADERS log in (/leader/login, password set via a one-time link) and get a
#     scoped dashboard + review screen where they endorse/return; HODs also send
#     reminders.
#
# CYCLE-SCOPED DATA: atr/atr_event/faculty_token live in the per-cycle DB. A bare
# token or (cycle_code, atr_id) tells us which cycle file to open; helpers below
# scan the registered cycles exactly like the student flow's _find_token does.
#
# ANONYMITY (spec §3.2): every screen here is about an OFFERING and shows only
# aggregate bands/scores; nothing reads a response, token or student.
# ----------------------------------------------------------------------------

import os
from functools import wraps

from flask import (render_template, request, redirect, url_for, abort, flash,
                   session)

import db
from db import get_master
from config import Config
from atr import atr_bp

import atr_workflow
import faculty_tokens
import auth_leaders
import notifications
import rbac


# ----------------------------------------------------------------------------
# _open_cycle_db(cycle_row) — open a per-cycle DB and make sure its schema
# (including the Module 2 atr/atr_event/faculty_token tables) exists. Same
# helper shape as admin/reports.py; the DDL is CREATE ... IF NOT EXISTS so this
# is a harmless no-op on an already-migrated file.
# ----------------------------------------------------------------------------
def _open_cycle_db(cycle_row):
    conn = db.get_cycle(cycle_row["academic_year"], cycle_row["code"])
    schema_path = os.path.join(Config.BASE_DIR, "schema_cycle.sql")
    with open(schema_path, "r", encoding="utf-8") as fh:
        conn.executescript(fh.read())
    conn.commit()
    return conn


# ----------------------------------------------------------------------------
# _cycle_by_code(master, cycle_code) -> Row | None — look up a cycle's config row
# by its code (e.g. 'CA1'), used to open the right per-cycle DB.
# ----------------------------------------------------------------------------
def _cycle_by_code(master, cycle_code):
    return master.execute(
        "SELECT * FROM cycle WHERE code = ?", (cycle_code,)).fetchone()


# ----------------------------------------------------------------------------
# _find_faculty_token(jti) -> (cycle_row, cycle_conn, token_row) | (None,...)
# ----------------------------------------------------------------------------
# A faculty magic link carries only the jti. Scan every registered cycle's per-
# cycle DB for a faculty_token with that jti (there is at most one, since jti is
# an unguessable PK). Returns the cycle config, an OPEN connection to that cycle
# (caller closes it), and the token row. Mirrors the student flow's _find_token.
# ----------------------------------------------------------------------------
def _find_faculty_token(jti):
    master = get_master()
    cycles = master.execute("SELECT * FROM cycle").fetchall()
    master.close()
    for c in cycles:
        path = db.cycle_db_path(c["academic_year"], c["code"])
        if not os.path.exists(path):
            continue
        cy = db.get_cycle(c["academic_year"], c["code"])
        row = cy.execute(
            "SELECT * FROM faculty_token WHERE jti = ?", (jti,)).fetchone()
        if row:
            return c, cy, row
        cy.close()
    return None, None, None


# ============================================================================
# FACULTY MAGIC-LINK ROUTES (no login) — the "File ATR" button (spec §5.2, §8.2)
# ============================================================================

# ----------------------------------------------------------------------------
# GET /atr/file  —  land on the filing form from the emailed magic link.
# Verifies the token (signature/expiry/one-time/purpose) and, if good, shows the
# offering's details + any existing ATR body, ready to submit. We do NOT mark the
# token used here — only a successful POST burns it (a mere page view must not
# consume the link).
# ----------------------------------------------------------------------------
@atr_bp.route("/atr/file", methods=["GET"])
def atr_file_form():
    jti = request.args.get("token", "").strip()
    cycle_row, cy, tok = _find_faculty_token(jti)
    if tok is None:
        return render_template("atr_invalid.html",
                               reason="This link is not recognised."), 404

    # Verify without an expected_offering_id (the token itself names the offering)
    # but insist on the ATR_FILE purpose (a VIEW link cannot file).
    vr = faculty_tokens.verify(cy, jti, expected_offering_id=None,
                               expected_purpose=faculty_tokens.PURPOSE_ATR_FILE)
    if not vr.ok:
        cy.close()
        return render_template("atr_invalid.html",
                               reason=_token_reason(vr.reason)), 403

    master = get_master()
    offering = master.execute(
        "SELECT * FROM offering WHERE id = ?", (tok["offering_id"],)).fetchone()
    master.close()

    # Show the current ATR (if one exists) so the faculty can revise a returned one.
    existing = atr_workflow.get_atr_for_offering(cy, tok["offering_id"])
    cy.close()
    return render_template("atr_file.html", token=jti, offering=offering,
                           cycle=cycle_row, atr=existing)


# ----------------------------------------------------------------------------
# POST /atr/file  —  submit (or re-submit) the ATR from the magic link.
# Re-verifies the token, ensures an EXPECTED atr row exists, applies the SUBMIT
# transition (EXPECTED/DRAFT -> PENDING_HOD, with the audit row), burns the
# one-time token, commits, then notifies the HOD (spec §9). All state logic is in
# atr_workflow; this route only orchestrates.
# ----------------------------------------------------------------------------
@atr_bp.route("/atr/file", methods=["POST"])
def atr_file_submit():
    jti = request.args.get("token", request.form.get("token", "")).strip()
    body = (request.form.get("body", "") or "").strip()

    cycle_row, cy, tok = _find_faculty_token(jti)
    if tok is None:
        return render_template("atr_invalid.html",
                               reason="This link is not recognised."), 404

    vr = faculty_tokens.verify(cy, jti,
                               expected_purpose=faculty_tokens.PURPOSE_ATR_FILE)
    if not vr.ok:
        cy.close()
        return render_template("atr_invalid.html",
                               reason=_token_reason(vr.reason)), 403

    if not body:
        cy.close()
        flash("Please describe the action you have taken before submitting.",
              "error")
        return redirect(url_for("atr.atr_file_form", token=jti))

    offering_id = tok["offering_id"]
    try:
        # Ensure the ATR row (EXPECTED) then move it to PENDING_HOD with an audit row.
        atr_id = atr_workflow.ensure_expected_atr(cy, offering_id, cycle_row["code"])
        atr_workflow.apply_transition(
            cy, atr_id, atr_workflow.ACTION_SUBMIT, atr_workflow.ROLE_FACULTY,
            actor_user_id=atr_workflow.ROLE_FACULTY, comment=None, body=body)
        faculty_tokens.mark_used(cy, jti)          # one-time link now spent
        cy.commit()
    except atr_workflow.IllegalTransition as e:
        cy.rollback(); cy.close()
        return render_template("atr_invalid.html",
                               reason="This ATR can no longer be submitted (%s)." % e), 409

    # Notify the HOD that an ATR now awaits them (reuse the §9 layer).
    atr_row = atr_workflow.get_atr(cy, atr_id)
    master = get_master()
    try:
        notifications.notify_state_change(
            master, cy, cycle_row, atr_row,
            atr_workflow.ACTION_SUBMIT, atr_row["state"])
    finally:
        master.close(); cy.close()

    # ACTIVITY LOG (Module 3): a faculty member acted via a magic link (no session),
    # so we ANNOUNCE the actor explicitly as FACULTY — the note override wins over
    # the hook's session-based default. Records which ATR was filed, for the trail.
    import activity_log
    try:
        _fac_email = tok["email"]        # faculty token may carry the address
    except (KeyError, IndexError):
        _fac_email = None
    activity_log.note(actor_type=activity_log.ACTOR_FACULTY,
                      actor_label=_fac_email or "faculty (magic link)",
                      detail=f"ATR #{atr_id} submitted to HOD",
                      cycle_code=cycle_row["code"], target_type="atr",
                      target_id=atr_id)
    return render_template("atr_done.html", heading="ATR submitted",
                           message="Your Action-Taken-Report has been submitted "
                                   "to your HOD for review. Thank you.")


# ----------------------------------------------------------------------------
# _token_reason(code) — turn a faculty_tokens.verify reason code into a friendly
# sentence for the invalid-link page. One place so the wording is consistent.
# ----------------------------------------------------------------------------
def _token_reason(code):
    return {
        "bad_signature": "This link is invalid.",
        "expired": "This link has expired. Ask your HOD to send a fresh one.",
        "used": "This link has already been used.",
        "wrong_offering": "This link is for a different subject.",
        "wrong_purpose": "This link cannot be used to file an ATR.",
    }.get(code, "This link cannot be used.")


# ============================================================================
# LEADER AUTH ROUTES (§5.1, §17.2) — set password (one-time link) + login/logout
# ============================================================================

# ----------------------------------------------------------------------------
# leader_required(f) — a small decorator that gates a route behind a logged-in
# leader session. If nobody is logged in it bounces to the login page. The
# session stores only the id/role/scope/email (set at login); the password hash
# never leaves the DB.
# ----------------------------------------------------------------------------
def leader_required(f):
    @wraps(f)
    def _wrap(*args, **kwargs):
        if not session.get("leader_id"):
            flash("Please log in to continue.", "error")
            return redirect(url_for("atr.leader_login"))
        return f(*args, **kwargs)
    return _wrap


# ----------------------------------------------------------------------------
# _current_leader() -> a dict shaped like an app_user (id/email/role/
# scope_dept_ids) built from the session, so rbac.* (which accepts any mapping
# with role + scope_dept_ids) and the templates can use it uniformly.
# ----------------------------------------------------------------------------
def _current_leader():
    return {
        "id": session.get("leader_id"),
        "email": session.get("leader_email"),
        "role": session.get("leader_role"),
        "scope_dept_ids": session.get("leader_scope", ""),
    }


# ----------------------------------------------------------------------------
# GET/POST /leader/login  —  email + password login (auth_leaders.authenticate).
# On success we stamp last_login_at (inside authenticate) and open a session.
# ----------------------------------------------------------------------------
@atr_bp.route("/leader/login", methods=["GET", "POST"])
def leader_login():
    if request.method == "GET":
        return render_template("leader_login.html")
    email = (request.form.get("email", "") or "").strip()
    password = request.form.get("password", "") or ""
    master = get_master()
    try:
        user = auth_leaders.authenticate(master, email, password)
        if user is None:
            master.close()
            # ACTIVITY LOG (Module 3): record FAILED logins too — a security-relevant
            # event. There is no session yet, so we label by the email that was tried.
            import activity_log
            activity_log.note(actor_label=(email or "unknown"),
                              detail="Failed leader login attempt")
            flash("Invalid email or password (or password not set yet).", "error")
            return redirect(url_for("atr.leader_login"))
        master.commit()   # persist last_login_at
        session["leader_id"] = user["id"]
        session["leader_email"] = user["email"]
        session["leader_role"] = user["role"]
        session["leader_scope"] = user["scope_dept_ids"]
    finally:
        master.close()
    return redirect(url_for("atr.atr_dashboard"))


# ----------------------------------------------------------------------------
# GET /leader/logout  —  clear the leader session.
# ----------------------------------------------------------------------------
@atr_bp.route("/leader/logout")
def leader_logout():
    for k in ("leader_id", "leader_email", "leader_role", "leader_scope"):
        session.pop(k, None)
    flash("Logged out.", "success")
    return redirect(url_for("atr.leader_login"))


# ----------------------------------------------------------------------------
# GET/POST /leader/set-password  —  redeem a one-time set/reset link and set the
# password (auth_leaders.redeem_set_pw_token). The jti comes from the emailed URL.
# ----------------------------------------------------------------------------
@atr_bp.route("/leader/set-password", methods=["GET", "POST"])
def leader_set_password():
    jti = request.args.get("token", request.form.get("token", "")).strip()
    master = get_master()
    ok, reason, row = auth_leaders.verify_set_pw_token(master, jti)
    if not ok:
        master.close()
        return render_template("atr_invalid.html",
                               reason=_token_reason(reason)), 403
    if request.method == "GET":
        user = master.execute("SELECT email FROM app_user WHERE id = ?",
                              (row["user_id"],)).fetchone()
        master.close()
        return render_template("leader_set_password.html", token=jti,
                               email=user["email"] if user else "")
    # POST: validate + save.
    pw1 = request.form.get("password", "") or ""
    pw2 = request.form.get("password2", "") or ""
    if len(pw1) < 8 or pw1 != pw2:
        master.close()
        flash("Passwords must match and be at least 8 characters.", "error")
        return redirect(url_for("atr.leader_set_password", token=jti))
    done, why = auth_leaders.redeem_set_pw_token(master, jti, pw1)
    if done:
        master.commit()
    master.close()
    if not done:
        return render_template("atr_invalid.html",
                               reason=_token_reason(why)), 403
    flash("Password set. You can now log in.", "success")
    return redirect(url_for("atr.leader_login"))


# ============================================================================
# LEADER DASHBOARD + REVIEW/ENDORSE/RETURN (§4 RBAC, §8 state machine, §9 notify)
# ============================================================================

# ----------------------------------------------------------------------------
# GET /atr/dashboard  —  the leader's scoped ATR queue. We list the offerings in
# the leader's RBAC scope for the chosen cycle (rbac.visible_offerings — the
# Module 1 choke-point), joined to their ATR state, so an E01 HOD only ever sees
# E01 ATRs. Marks which ones are in THIS leader's action queue (owner == role).
# ----------------------------------------------------------------------------
@atr_bp.route("/atr/dashboard")
@leader_required
def atr_dashboard():
    leader = _current_leader()
    master = get_master()
    cycles = master.execute(
        "SELECT * FROM cycle ORDER BY academic_year, code").fetchall()

    cycle_code = request.args.get("cycle", "").strip()
    selected = None
    if cycle_code:
        selected = _cycle_by_code(master, cycle_code)
    if selected is None:
        # Default to the first cycle whose per-cycle DB exists.
        for c in cycles:
            if os.path.exists(db.cycle_db_path(c["academic_year"], c["code"])):
                selected = c
                break
        if selected is None and cycles:
            selected = cycles[0]

    rows = []
    if selected is not None and os.path.exists(
            db.cycle_db_path(selected["academic_year"], selected["code"])):
        # Scope: only offerings this leader may see (Module 1 RBAC).
        visible = rbac.visible_offerings(master, leader, selected["code"])
        visible_ids = {o["id"]: o for o in visible}
        cy = _open_cycle_db(selected)
        atrs = cy.execute("SELECT * FROM atr").fetchall()
        cy.close()
        for a in atrs:
            if a["offering_id"] in visible_ids:     # RBAC filter
                o = visible_ids[a["offering_id"]]
                rows.append({
                    "atr": a, "offering": o,
                    "mine": a["current_owner_role"] == leader["role"],
                })

    # (Module 5) EXTERNAL / UNASSIGNED sections — only for college-wide leaders
    # (Vice Dean, Dean; allowed_dept_codes is None). These are offerings whose
    # FACULTY has no HOD: 'EXTERNAL' faculty (reviewed only by the VD, by design)
    # and 'UNASSIGNED' faculty (home department not set yet — a to-do). They are
    # deliberately outside the normal HOD ATR flow, so we surface them here in
    # their own sections with their GOOD/POOR band for the VD's attention.
    external_rows, unassigned_rows = [], []
    if rbac.allowed_dept_codes(leader) is None and selected is not None:
        q = master.execute(
            "SELECT o.id, o.dept_code, o.course_code, o.course_name, o.faculty, "
            "       o.faculty_id, f.home_dept_code AS eff_dept, "
            "       oc.band AS band, oc.overall_score AS overall_score, "
            "       oc.n_responses AS n_responses "
            "FROM offering o "
            "LEFT JOIN faculty f ON f.emp_no = o.faculty_id "
            "LEFT JOIN offering_classification oc "
            "       ON oc.offering_id = o.id AND oc.cycle_code = o.cycle_code "
            "WHERE o.cycle_code = ? "
            "  AND (f.home_dept_code IS NULL OR f.home_dept_code = ?) "
            "ORDER BY o.dept_code, o.course_code",
            (selected["code"], rbac.DEPT_EXTERNAL)).fetchall()
        for r in q:
            (external_rows if r["eff_dept"] == rbac.DEPT_EXTERNAL
             else unassigned_rows).append(r)

    master.close()
    return render_template("atr_dashboard.html", leader=leader, cycles=cycles,
                           selected=selected, rows=rows,
                           external_rows=external_rows,
                           unassigned_rows=unassigned_rows)


# ----------------------------------------------------------------------------
# _load_atr(cycle_code, atr_id) -> (cycle_row, cycle_conn, atr_row) | (None,...)
# Open the named cycle and load one ATR by id. Returns an OPEN connection the
# caller must close.
# ----------------------------------------------------------------------------
def _load_atr(cycle_code, atr_id):
    master = get_master()
    cycle_row = _cycle_by_code(master, cycle_code)
    master.close()
    if cycle_row is None:
        return None, None, None
    if not os.path.exists(db.cycle_db_path(cycle_row["academic_year"],
                                           cycle_row["code"])):
        return cycle_row, None, None
    cy = _open_cycle_db(cycle_row)
    atr_row = atr_workflow.get_atr(cy, atr_id)
    return cycle_row, cy, atr_row


# ----------------------------------------------------------------------------
# _guard_leader_on_atr(leader, master, atr_row) -> offering_row | None
# ----------------------------------------------------------------------------
# The §4 access check for a single ATR: the leader may touch it ONLY if the
# offering's department is in their RBAC scope. Returns the offering row when
# allowed, else None (the route turns that into a 403). This is what stops an E01
# HOD from opening an E02 ATR even by guessing its id.
# ----------------------------------------------------------------------------
def _guard_leader_on_atr(leader, master, atr_row):
    offering = master.execute(
        "SELECT * FROM offering WHERE id = ?", (atr_row["offering_id"],)).fetchone()
    if offering is None:
        return None
    if not rbac.in_scope(leader, rbac.effective_dept(master, offering)):
        return None
    return offering


# ----------------------------------------------------------------------------
# GET /atr/review/<cycle_code>/<atr_id>  —  view one ATR + its audit trail, with
# endorse/return controls IF this leader may act on it right now (legal_actions).
# ----------------------------------------------------------------------------
@atr_bp.route("/atr/review/<cycle_code>/<int:atr_id>")
@leader_required
def atr_review(cycle_code, atr_id):
    leader = _current_leader()
    cycle_row, cy, atr_row = _load_atr(cycle_code, atr_id)
    if atr_row is None:
        if cy:
            cy.close()
        abort(404)
    master = get_master()
    offering = _guard_leader_on_atr(leader, master, atr_row)
    if offering is None:
        master.close(); cy.close()
        abort(403)
    events = atr_workflow.events_for(cy, atr_id)
    master.close(); cy.close()
    actions = atr_workflow.legal_actions(atr_row["state"], leader["role"])
    return render_template("atr_review.html", leader=leader, cycle=cycle_row,
                           atr=atr_row, offering=offering, events=events,
                           actions=actions)


# ----------------------------------------------------------------------------
# POST /atr/review/<cycle_code>/<atr_id>/<action>  —  ENDORSE or RETURN.
# The heavy lifting is atr_workflow.apply_transition, which enforces both the
# legal-move rule and the correct-actor rule (so a leader cannot act out of turn
# or invent a shortcut) and writes the audit row. On success we notify the next
# level (or the level below, for a RETURN) via the §9 layer. RBAC is checked
# first via _guard_leader_on_atr.
# ----------------------------------------------------------------------------
@atr_bp.route("/atr/review/<cycle_code>/<int:atr_id>/<action>", methods=["POST"])
@leader_required
def atr_act(cycle_code, atr_id, action):
    action = action.upper()
    if action not in (atr_workflow.ACTION_ENDORSE, atr_workflow.ACTION_RETURN):
        abort(404)
    leader = _current_leader()
    comment = (request.form.get("comment", "") or "").strip() or None

    cycle_row, cy, atr_row = _load_atr(cycle_code, atr_id)
    if atr_row is None:
        if cy:
            cy.close()
        abort(404)
    master = get_master()
    offering = _guard_leader_on_atr(leader, master, atr_row)
    if offering is None:
        master.close(); cy.close()
        abort(403)

    # A RETURN must carry a reason (spec §9 "reason + link to revise").
    if action == atr_workflow.ACTION_RETURN and not comment:
        master.close(); cy.close()
        flash("Please give a reason when returning an ATR.", "error")
        return redirect(url_for("atr.atr_review", cycle_code=cycle_code, atr_id=atr_id))

    try:
        new_state = atr_workflow.apply_transition(
            cy, atr_id, action, leader["role"],
            actor_user_id=leader["id"], comment=comment)
        cy.commit()
    except atr_workflow.IllegalTransition as e:
        cy.rollback(); master.close(); cy.close()
        flash("That action is not allowed here (%s)." % e, "error")
        return redirect(url_for("atr.atr_review", cycle_code=cycle_code, atr_id=atr_id))

    # If the ATR came back to the faculty (HOD RETURN -> EXPECTED), mint a fresh
    # magic link so the notification email carries a working "revise" button.
    # (Module 5) Resolve the faculty's email from the Faculty Master so a RETURN
    # back to the faculty carries a working "revise" link even though the offering
    # itself no longer stores an email. This is the fix for "returning for rework
    # did not send an email": the address is now looked up, not read off the
    # (email-less) offering row.
    faculty_jti = None
    if new_state == atr_workflow.STATE_EXPECTED:
        fac_email = notifications.faculty_email_for(master, offering)
        if fac_email:
            faculty_jti, _exp = faculty_tokens.issue(
                cy, offering["id"], fac_email,
                purpose=faculty_tokens.PURPOSE_ATR_FILE)
            cy.commit()

    atr_after = atr_workflow.get_atr(cy, atr_id)
    try:
        notifications.notify_state_change(
            master, cy, cycle_row, atr_after, action, new_state,
            reason=comment, faculty_link_jti=faculty_jti)
    finally:
        master.close(); cy.close()

    # ACTIVITY LOG (Module 3): the leader is in session, so the hook already stamps
    # WHO; this note adds WHAT precisely — endorse vs return, and the new state.
    import activity_log
    activity_log.note(detail=f"{action.title()} → {new_state} (ATR #{atr_id})",
                      cycle_code=cycle_code, target_type="atr", target_id=atr_id)
    flash("Done — ATR is now %s." % new_state, "success")
    return redirect(url_for("atr.atr_dashboard", cycle=cycle_code))


# ----------------------------------------------------------------------------
# POST /atr/remind/<cycle_code>/<offering_id>  —  HOD "Send reminder" (spec §9).
# For a POOR offering still EXPECTED (not yet filed), the HOD nudges the faculty:
# we ensure the EXPECTED atr row, issue a FRESH magic link, email it, and log an
# atr_event(REMIND) — a reminder is NOT a state change, so it does not go through
# apply_transition (which rejects REMIND by design). RBAC-guarded.
# ----------------------------------------------------------------------------
@atr_bp.route("/atr/remind/<cycle_code>/<int:offering_id>", methods=["POST"])
@leader_required
def atr_remind(cycle_code, offering_id):
    leader = _current_leader()
    master = get_master()
    cycle_row = _cycle_by_code(master, cycle_code)
    if cycle_row is None:
        master.close(); abort(404)
    offering = master.execute(
        "SELECT * FROM offering WHERE id = ?", (offering_id,)).fetchone()
    if offering is None or not rbac.in_scope(leader, rbac.effective_dept(master, offering)):
        master.close(); abort(403)

    cy = _open_cycle_db(cycle_row)
    try:
        atr_id = atr_workflow.ensure_expected_atr(cy, offering_id, cycle_row["code"])
        # Only remind while still EXPECTED (nothing filed yet).
        atr_row = atr_workflow.get_atr(cy, atr_id)
        if atr_row["state"] != atr_workflow.STATE_EXPECTED:
            cy.close(); master.close()
            flash("An ATR has already been filed for that subject.", "error")
            return redirect(url_for("atr.atr_dashboard", cycle=cycle_code))
        fac_email = notifications.faculty_email_for(master, offering)
        jti, _exp = faculty_tokens.issue(
            cy, offering_id, fac_email,
            purpose=faculty_tokens.PURPOSE_ATR_FILE)
        atr_workflow.record_reminder(cy, atr_id, leader["id"],
                                     comment="HOD reminder")
        cy.commit()
        notifications.send_reminder_email(master, cycle_row, offering, jti)
    finally:
        cy.close(); master.close()
    flash("Reminder sent to the faculty.", "success")
    return redirect(url_for("atr.atr_dashboard", cycle=cycle_code))
