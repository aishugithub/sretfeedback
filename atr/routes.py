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
                   session, current_app)

import db
from db import get_master
from config import Config
from atr import atr_bp

import atr_workflow
import faculty_tokens
import consolidation   # Aug 2026: group an elective's per-programme offerings into ONE delivery
import auth_leaders
import notifications
import rbac
import datetime as _dt


def _ist(ts):
    """Show a stored UTC timestamp ('YYYY-MM-DD HH:MM:SS' from SQLite
    datetime('now')) in IST. India is a fixed UTC+5:30 (no DST), so this is exact
    whatever timezone the server runs in. Unparseable input is returned as-is."""
    if not ts:
        return ts
    try:
        d = _dt.datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
        return (d + _dt.timedelta(hours=5, minutes=30)).strftime(
            "%Y-%m-%d %H:%M:%S") + " IST"
    except Exception:
        return ts


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

    # Notify the reviewer that an ATR now awaits them (reuse the §9 layer).
    master = get_master()
    offering = master.execute(
        "SELECT * FROM offering WHERE id = ?", (offering_id,)).fetchone()
    try:
        # SELF-REVIEW policy: if the faculty who just filed is themselves the HOD
        # (or VD/Dean) who would review this ATR, auto-forward past their own level
        # first, so nobody endorses their own report. When it moves the ATR,
        # escalate_past_self already notified the true next reviewer — so we only
        # send the ordinary "awaits you" notice when NO self-skip happened.
        skipped = escalate_past_self(master, cy, cycle_row, offering, atr_id)
        if not skipped:
            atr_row = atr_workflow.get_atr(cy, atr_id)
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
    # Leaders never pick an archived cycle from their ATR dashboard dropdown;
    # archived cycles are done-and-recorded and drop out of the operational view.
    cycles = master.execute(
        "SELECT * FROM cycle WHERE status != 'ARCHIVED' "
        "ORDER BY academic_year, code").fetchall()

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
    all_offerings = []          # HOD "see ALL staff reports" (all bands, not just POOR)
    if selected is not None and os.path.exists(
            db.cycle_db_path(selected["academic_year"], selected["code"])):
        # Scope: only offerings this leader may see (Module 1 RBAC).
        visible = rbac.visible_offerings(master, leader, selected["code"])
        visible_ids = {o["id"]: o for o in visible}
        cy = _open_cycle_db(selected)
        atrs = cy.execute("SELECT * FROM atr").fetchall()
        cy.close()
        # Combined programme label ("E01, E02, E03") for every offering_id that is
        # part of a pooled elective delivery (Aug 2026), so both the ATR queue and
        # the all-reports list below show the whole class rather than one programme.
        elective_dept_by_oid = {}
        for _a, _g in consolidation.group_rows(visible).items():
            if _g["is_elective"]:
                _lbl = ", ".join(_g["dept_codes"])
                for _oid in _g["oids"]:
                    elective_dept_by_oid[_oid] = _lbl
        for a in atrs:
            if a["offering_id"] in visible_ids:     # RBAC filter
                # One ATR exists per delivery (created only at the anchor), so this
                # loop already yields one queue row per class. Show the combined
                # programme label for an elective anchor.
                o = dict(visible_ids[a["offering_id"]])
                if a["offering_id"] in elective_dept_by_oid:
                    o["dept_code"] = elective_dept_by_oid[a["offering_id"]]
                rows.append({
                    "atr": a, "offering": o,
                    "mine": a["current_owner_role"] == leader["role"],
                })

        # ---- HOD "see ALL their staff reports" (professor's rule, Aug 2026) ----
        # The ATR queue above shows only POOR courses (an ATR exists only for those).
        # The professor wants the HOD to also see EVERY course of every staff member
        # in their department — GOOD, POOR or Insufficient — with its report. We
        # reuse the SAME rbac.visible_offerings scope (so no leak past their dept),
        # attach each offering's band/score from offering_classification, and note
        # whether an ATR exists. The template links each row to the report download
        # route (atr.atr_report), which re-checks scope on every hit.
        atr_oids = {a["offering_id"] for a in atrs}
        cls_rows = master.execute(
            "SELECT offering_id, band, overall_score, n_responses "
            "FROM offering_classification WHERE cycle_code = ?",
            (selected["code"],)).fetchall()
        cls_map = {r["offering_id"]: r for r in cls_rows}
        # CONSOLIDATED (Aug 2026): collapse the visible offerings into deliveries so
        # a shared elective shows as ONE row (all its programmes), not one row per
        # programme. The delivery's band/score/count come from the single
        # classification row that lives on its responded anchor; the display row and
        # the report link use the smallest-id member (members_of pools it back).
        for _anchor, g in consolidation.group_rows(visible).items():
            info = None
            for oid in g["oids"]:                 # the one classified member = anchor
                if oid in cls_map:
                    info = cls_map[oid]
                    break
            disp = dict(g["rows"][0])             # smallest-id member for identity/link
            if g["is_elective"]:
                disp["dept_code"] = ", ".join(g["dept_codes"])
            all_offerings.append({
                "offering": disp,
                "band": info["band"] if info else None,
                "overall_score": info["overall_score"] if info else None,
                "n_responses": info["n_responses"] if info else None,
                # An ATR exists for the delivery if any member id has one (the anchor).
                "has_atr": any(oid in atr_oids for oid in g["oids"]),
            })

    # (Module 5) EXTERNAL / UNASSIGNED sections — only for college-wide leaders
    # (Vice Dean, Dean; allowed_dept_codes is None). These are offerings whose
    # FACULTY has no HOD: 'EXTERNAL' faculty (reviewed only by the VD, by design)
    # and 'UNASSIGNED' faculty (home department not set yet — a to-do). They are
    # deliberately outside the normal HOD ATR flow, so we surface them here in
    # their own sections with their GOOD/POOR band for the VD's attention.
    external_rows, unassigned_rows = [], []
    if rbac.allowed_dept_codes(leader) is None and selected is not None:
        # NB: we also select the columns consolidation.group_rows needs
        # (course_type, elective_basket, cycle_code, section) so these lists can be
        # collapsed to one row per delivery just like the others (Aug 2026).
        q = master.execute(
            "SELECT o.id, o.cycle_code, o.dept_code, o.course_code, o.course_name, "
            "       o.course_type, o.elective_basket, o.section, o.faculty, "
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
        # Collapse to deliveries: an elective's programme rows fold into one line.
        for _anchor, g in consolidation.group_rows(q).items():
            # The band/score live on the delivery's classified anchor — find the one
            # member row that carries a band (others have NULL from the LEFT JOIN).
            info_row = next((r for r in g["rows"] if r["band"] is not None), None)
            base = dict(g["rows"][0])
            if g["is_elective"]:
                base["dept_code"] = ", ".join(g["dept_codes"])
            base["band"] = info_row["band"] if info_row else None
            base["overall_score"] = info_row["overall_score"] if info_row else None
            base["n_responses"] = info_row["n_responses"] if info_row else None
            # All members share one faculty ⇒ one effective dept ⇒ one bucket.
            eff = g["rows"][0]["eff_dept"]
            (external_rows if eff == rbac.DEPT_EXTERNAL
             else unassigned_rows).append(base)

    master.close()
    # How many ATRs are in THIS leader's action queue right now (owner == role)?
    # Drives the "Endorse all (N)" button, shown only to whole-slate endorsers.
    my_count = sum(1 for r in rows if r["mine"])
    can_endorse_all = (leader["role"] or "").upper() in (
        atr_workflow.ROLE_VICE_DEAN, atr_workflow.ROLE_DEAN)
    return render_template("atr_dashboard.html", leader=leader, cycles=cycles,
                           selected=selected, rows=rows,
                           all_offerings=all_offerings,
                           external_rows=external_rows,
                           unassigned_rows=unassigned_rows,
                           my_count=my_count, can_endorse_all=can_endorse_all)


# ----------------------------------------------------------------------------
# GET /atr/report/<cycle_code>/<offering_id>.<fmt>  —  leader report download
# ----------------------------------------------------------------------------
# The HOD "see ALL their staff reports" download (professor's rule, Aug 2026).
# This is the leader-side twin of admin/reports.report_one: it builds the exact
# same Excel/PDF from the exact same scoring stack, but is gated by the §4 RBAC
# choke-point instead of the admin login — a HOD can download the report for any
# offering in THEIR department (all bands, not just POOR), and never one outside
# it. Scope is checked via rbac.in_scope against the FACULTY's home department
# (effective_dept), so it can never drift from what visible_offerings lists.
# fmt is 'xlsx' or 'pdf'.
# ----------------------------------------------------------------------------
@atr_bp.route("/atr/report/<cycle_code>/<int:offering_id>.<fmt>")
@leader_required
def atr_report(cycle_code, offering_id, fmt):
    if fmt not in ("xlsx", "pdf"):
        abort(404)
    leader = _current_leader()
    master = get_master()
    cyc = _cycle_by_code(master, cycle_code)
    if cyc is None:
        master.close(); abort(404)

    # RBAC: resolve the offering's effective department (the faculty's home dept)
    # and refuse if this leader may not see it. This is the SAME predicate the
    # dashboard list uses, so "listed ⇒ downloadable" holds and nothing else is.
    off = master.execute(
        "SELECT o.id, o.faculty_id, f.home_dept_code AS eff_dept "
        "FROM offering o LEFT JOIN faculty f ON f.emp_no = o.faculty_id "
        "WHERE o.id = ?", (offering_id,)).fetchone()
    if off is None:
        master.close(); abort(404)
    if not rbac.in_scope(leader, off["eff_dept"]):
        master.close(); abort(403)

    # Build the report exactly as the admin route does: score the offering, then
    # render Excel or PDF to a temp file and stream it. (Local imports keep the
    # blueprint's import-time surface small and match admin/reports.py's approach.)
    import io
    import tempfile
    import scoring
    import report_export
    from flask import send_file

    cy = _open_cycle_db(cyc)
    dl_weight = scoring.get_discussed_late_weight(master)
    # CONSOLIDATED (Aug 2026): resolve the offering to its whole delivery and pool
    # every member's responses, so a leader downloads the single consolidated
    # elective report (not one programme's slice). A CORE course pools to itself,
    # so its report is byte-for-byte what it was before.
    member_ids = consolidation.members_of(master, cy, cycle_code, offering_id)
    result = scoring.score_offering_group(master, cy, member_ids, dl_weight)
    cy.close(); master.close()

    # Test cycles stamp the same "TEST DATA" watermark the admin PDF uses (§9.1).
    if result is not None and cyc["is_test"]:
        result["watermark"] = "TESTING ONLY"
    if result is None:
        flash("Nothing to report for that offering (uncategorised or no responses).",
              "error")
        return redirect(url_for("atr.atr_dashboard", cycle=cycle_code))

    suffix = ".xlsx" if fmt == "xlsx" else ".pdf"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.close()
    if fmt == "xlsx":
        report_export.build_excel_report(result, tmp.name)
        mimetype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        report_export.build_pdf_report(result, tmp.name)
        mimetype = "application/pdf"
    download_name = report_export.safe_filename(result, fmt)
    with open(tmp.name, "rb") as fh:
        data = fh.read()
    os.unlink(tmp.name)                       # never leave temp files behind

    # ACTIVITY LOG (Module 3): record WHO (the leader, stamped by the hook) viewed
    # WHICH report — so HOD report access is auditable just like admin downloads.
    import activity_log
    activity_log.note(detail=f"{download_name} ({fmt.upper()}) — HOD/leader view",
                      cycle_code=cycle_code, target_type="offering",
                      target_id=offering_id)
    return send_file(io.BytesIO(data), mimetype=mimetype,
                     as_attachment=True, download_name=download_name)


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
    # Resolve each event's actor to a HUMAN NAME. atr_event.actor_user_id holds an
    # app_user.id (as text) for a leader, or the 'FACULTY' sentinel / NULL for the
    # faculty member (who has no login). Build an id->name map from the SAME Users &
    # Roles table the admin manages, so the audit trail reads "Dr Priya (HOD)"
    # instead of a bare "5". (Faculty stay anonymous-by-role — just "Faculty" — since
    # they act via a magic link and have no app_user row.)
    users = {str(u["id"]): {"name": (u["name"] or u["email"]), "role": u["role"]}
             for u in master.execute(
                 "SELECT id, name, email, role FROM app_user").fetchall()}
    events_view = []
    for e in events:
        aid = e["actor_user_id"]
        if aid in (None, "", "FACULTY"):
            label, role = "Faculty", None
        else:
            u = users.get(str(aid))
            label = u["name"] if u else ("User #%s" % aid)
            role = u["role"] if u else None
        events_view.append({"action": e["action"], "comment": e["comment"],
                            "at": _ist(e["at"]), "actor_label": label, "actor_role": role})
    # EXTERNAL-FACULTY rule: is this ATR for a course taught by the "External"
    # placeholder (faculty home dept 'EXT')? If so, there is no faculty narrative;
    # instead the HOD types his own action note here and endorses it up to the VD.
    # The template uses this flag to show a "HOD note" textarea on the Endorse form.
    ext_row = master.execute(
        "SELECT home_dept_code FROM faculty WHERE emp_no = ?",
        (offering["faculty_id"],)).fetchone()
    is_external = bool(ext_row) and (ext_row["home_dept_code"] == "EXT")
    master.close(); cy.close()
    actions = atr_workflow.legal_actions(atr_row["state"], leader["role"])
    return render_template("atr_review.html", leader=leader, cycle=cycle_row,
                           atr=atr_row, offering=offering, events=events_view,
                           actions=actions, is_external=is_external)


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
    # EXTERNAL-FACULTY rule: on an ENDORSE the HOD may supply a `body` (his action
    # note) — this is how an External-department ATR gets its narrative, since no
    # faculty ever filed one. It is read here and passed through to apply_transition
    # only for ENDORSE; on a normal ATR the form has no `body` field, so it stays
    # None and the faculty's existing narrative is preserved untouched.
    hod_note = (request.form.get("body", "") or "").strip() or None

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
            actor_user_id=leader["id"], comment=comment,
            # Only an ENDORSE may carry the HOD's note as the ATR body (external
            # faculty). A RETURN never rewrites the body; apply_transition also
            # leaves body untouched when this is None (the normal case).
            body=(hod_note if action == atr_workflow.ACTION_ENDORSE else None))
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
    recorded = False
    try:
        notifications.notify_state_change(
            master, cy, cycle_row, atr_after, action, new_state,
            reason=comment, faculty_link_jti=faculty_jti)
        # If THIS endorse was the Dean closing the last outstanding ATR, the whole
        # cycle is now done — advance it to RECORDED (same gate the bulk endorse uses).
        if (action == atr_workflow.ACTION_ENDORSE
                and new_state == atr_workflow.STATE_CLOSED
                and (leader["role"] or "").upper() == atr_workflow.ROLE_DEAN):
            recorded = _maybe_mark_recorded(master, cycle_row, cy)
        # SELF-REVIEW policy: if this endorse advanced the ATR to a level whose
        # reviewer is the course's OWN faculty (e.g. a teaching Vice Dean now at
        # PENDING_VD), auto-forward past them as well. No-op in the ordinary case.
        if action == atr_workflow.ACTION_ENDORSE:
            escalate_past_self(master, cy, cycle_row, offering, atr_id)
    finally:
        master.close(); cy.close()

    # ACTIVITY LOG (Module 3): the leader is in session, so the hook already stamps
    # WHO; this note adds WHAT precisely — endorse vs return, and the new state.
    import activity_log
    activity_log.note(detail=f"{action.title()} → {new_state} (ATR #{atr_id})"
                      + (" — cycle RECORDED" if recorded else ""),
                      cycle_code=cycle_code, target_type="atr", target_id=atr_id)
    msg = "Done — ATR is now %s." % new_state
    if recorded:
        msg += " All ATRs are now closed — the cycle is COMPLETE & RECORDED."
    flash(msg, "success")
    return redirect(url_for("atr.atr_dashboard", cycle=cycle_code))


# ----------------------------------------------------------------------------
# _maybe_mark_recorded(master, cycle_row, cy) -> bool
# ----------------------------------------------------------------------------
# The "cycle done and recorded" gate (the professor's requirement). Once the Dean
# has endorsed the LAST outstanding ATR, the whole endorsement process is complete
# for the cycle. When every ATR is CLOSED we advance the cycle's status to
# RECORDED — a new terminal-but-not-yet-archived state that (a) tells the admin the
# cycle is ready for its signed audit report + archive, and (b) is what the audit
# report and archive steps check before they will run. Idempotent and safe: it
# only writes when all ATRs are closed and the cycle is not already ARCHIVED.
# Returns True if it just moved the cycle to RECORDED.
# ----------------------------------------------------------------------------
def _maybe_mark_recorded(master, cycle_row, cy):
    summ = atr_workflow.cycle_atr_summary(cy)
    if summ["all_closed"] and cycle_row["status"] not in ("ARCHIVED", "RECORDED"):
        master.execute("UPDATE cycle SET status = 'RECORDED' WHERE id = ?",
                       (cycle_row["id"],))
        master.commit()
        return True
    return False


# ----------------------------------------------------------------------------
# _faculty_leader(master, offering) -> app_user Row | None
# ----------------------------------------------------------------------------
# The person teaching this course, IF they also hold a leadership role in the
# endorsement chain. We resolve the teacher's institutional email from the
# Faculty Master (same lookup distribution/notifications use) and match it — case-
# insensitively — to an ACTIVE app_user with a leader role. Returns None when the
# teacher holds no leadership role (the ordinary case) or has no email (external).
# ----------------------------------------------------------------------------
def _faculty_leader(master, offering):
    email = notifications.faculty_email_for(master, offering)
    if not email:
        return None
    return master.execute(
        "SELECT * FROM app_user WHERE lower(email) = lower(?) AND status = 'active' "
        "AND role IN ('HOD','VICE_DEAN','DEAN')", (email,)).fetchone()


# ----------------------------------------------------------------------------
# escalate_past_self(master, cy, cycle_row, offering, atr_id) -> int (skips)
# ----------------------------------------------------------------------------
# SELF-REVIEW policy (the professor's rule, Aug 2026): nobody may endorse their
# OWN ATR. A HOD who also teaches, a Vice Dean who teaches, a Dean who teaches —
# when one of their own courses lands POOR and they file an ATR, it must not stop
# at the level THEY occupy. This helper auto-forwards the ATR past every pending
# level whose reviewer is the course's own faculty:
#     teaching HOD   : PENDING_HOD  -> PENDING_VD   (Vice Dean reviews)
#     teaching VD     : PENDING_VD   -> PENDING_DEAN (Dean reviews)
#     teaching Dean   : PENDING_DEAN -> CLOSED       (auto-closed; no higher authority)
# Each hop is a REAL, legal ENDORSE through the pure state machine (so it is
# audited exactly like a manual endorse), attributed to that leader, carrying a
# clear "self-review skipped" note. We only skip a HOD level when the HOD's scope
# actually covers this course's department (a HOD teaching OUTSIDE the dept they
# head is reviewed normally by that dept's real HOD). After any skip we notify the
# true next reviewer and, if the Dean's own ATR auto-closed, run the RECORDED gate.
# Returns how many levels were skipped (0 = ordinary case, nothing changed).
# ----------------------------------------------------------------------------
def escalate_past_self(master, cy, cycle_row, offering, atr_id):
    leader = _faculty_leader(master, offering)
    if leader is None:
        return 0                                   # teacher holds no leader role
    role = (leader["role"] or "").upper()
    home_dept = rbac.effective_dept(master, offering)   # the course's owning dept
    skipped = 0
    for _ in range(4):                             # ≤3 levels; bound guards loops
        atr = atr_workflow.get_atr(cy, atr_id)
        owner = (atr["current_owner_role"] or "").upper()
        if not owner:
            break                                  # CLOSED — nothing pending
        if owner != role:
            break                                  # the pending reviewer is someone else
        # A HOD only self-reviews within their own department's scope.
        if owner == atr_workflow.ROLE_HOD and not rbac.in_scope(leader, home_dept):
            break
        # Advance one level via a legal, audited ENDORSE attributed to this leader.
        atr_workflow.apply_transition(
            cy, atr_id, atr_workflow.ACTION_ENDORSE, leader["role"],
            actor_user_id=leader["id"],
            comment="Auto-forwarded — the reviewer at this level is the course "
                    "faculty; self-review skipped per policy.")
        skipped += 1
    if skipped:
        cy.commit()
        atr_after = atr_workflow.get_atr(cy, atr_id)
        # Notify the REAL next reviewer (or record the closure). Best-effort: a mail
        # hiccup must not roll back a valid, audited state change.
        try:
            notifications.notify_state_change(
                master, cy, cycle_row, atr_after,
                atr_workflow.ACTION_ENDORSE, atr_after["state"],
                reason="Automatic escalation: the skipped reviewer is the course faculty.")
        except Exception:
            pass
        if atr_after["state"] == atr_workflow.STATE_CLOSED:
            _maybe_mark_recorded(master, cycle_row, cy)
    return skipped


# ----------------------------------------------------------------------------
# POST /atr/endorse-all/<cycle_code>  —  Dean / Vice-Dean bulk endorsement.
# ----------------------------------------------------------------------------
# The professor's rule: "the Dean cannot click on everybody — there has to be a
# button to endorse all", and the Vice-Dean gets the same (scoped to their depts).
# We endorse EVERY ATR currently in this leader's action queue (current_owner_role
# == their role) AND within their RBAC scope — nothing outside their remit is ever
# touched. Each endorsement goes through the SAME pure state machine
# (apply_transition), so every move is legal, correctly-actored and audit-logged
# exactly as a one-by-one click would be; there is no bulk shortcut around the FSM.
# HODs are intentionally excluded (they act item-by-item). When the Dean's bulk
# endorsement closes the last ATR, the cycle flips to RECORDED.
# ----------------------------------------------------------------------------
@atr_bp.route("/atr/endorse-all/<cycle_code>", methods=["POST"])
@leader_required
def atr_endorse_all(cycle_code):
    leader = _current_leader()
    role = (leader["role"] or "").upper()
    # Only whole-slate endorsers: Vice Dean (their departments) and Dean (college).
    if role not in (atr_workflow.ROLE_VICE_DEAN, atr_workflow.ROLE_DEAN):
        abort(403)

    master = get_master()
    cycle_row = _cycle_by_code(master, cycle_code)
    if cycle_row is None:
        master.close(); abort(404)
    if not os.path.exists(db.cycle_db_path(cycle_row["academic_year"],
                                           cycle_row["code"])):
        master.close()
        flash("No ATRs exist for this cycle yet.", "error")
        return redirect(url_for("atr.atr_dashboard", cycle=cycle_code))

    # RBAC scope: the offerings this leader may see. Bulk endorsement can only ever
    # touch ATRs whose offering is in this set — the §4 guarantee, applied in bulk.
    visible_ids = {o["id"] for o in rbac.visible_offerings(master, leader, cycle_code)}

    cy = _open_cycle_db(cycle_row)
    # The leader's action queue: ATRs whose CURRENT owner role is this leader's role
    # (PENDING_VD for a Vice-Dean, PENDING_DEAN for a Dean), intersected with scope.
    queue = [a for a in
             cy.execute("SELECT * FROM atr WHERE current_owner_role = ?",
                        (role,)).fetchall()
             if a["offering_id"] in visible_ids]

    if not queue:
        cy.close(); master.close()
        flash("Nothing is awaiting your endorsement right now.", "success")
        return redirect(url_for("atr.atr_dashboard", cycle=cycle_code))

    # Endorse each through the pure state machine (legal + audited every time).
    done, skipped, changed = 0, 0, []
    for a in queue:
        try:
            new_state = atr_workflow.apply_transition(
                cy, a["id"], atr_workflow.ACTION_ENDORSE, role,
                actor_user_id=leader["id"], comment="Bulk endorsement")
            changed.append((a["id"], new_state))
            done += 1
        except atr_workflow.IllegalTransition:
            skipped += 1
    cy.commit()

    # Notify the next level for each ATR (reuse the §9 layer, one email per ATR).
    base = request.url_root.rstrip("/")
    for atr_id, new_state in changed:
        atr_after = atr_workflow.get_atr(cy, atr_id)
        try:
            notifications.notify_state_change(
                master, cy, cycle_row, atr_after,
                atr_workflow.ACTION_ENDORSE, new_state, base_url=base)
        except Exception as e:            # a mail hiccup must not undo the endorsement
            current_app.logger.warning("endorse-all notify failed: %s", e)

    # If the Dean just cleared the last pending ATR, the cycle is done & recorded.
    recorded = _maybe_mark_recorded(master, cycle_row, cy) \
        if role == atr_workflow.ROLE_DEAN else False

    cy.close(); master.close()

    import activity_log
    activity_log.note(
        detail=("Endorse-all by %s: %d endorsed%s%s"
                % (role, done, (", %d skipped" % skipped) if skipped else "",
                   " — cycle COMPLETE & RECORDED" if recorded else "")),
        cycle_code=cycle_code, target_type="cycle", target_id=cycle_row["id"])

    msg = "Endorsed %d ATR(s) in one action." % done
    if skipped:
        msg += " %d were skipped (no longer in your queue)." % skipped
    if recorded:
        msg += " All ATRs are now closed — the cycle is COMPLETE & RECORDED."
    flash(msg, "success")
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
