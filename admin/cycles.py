# ============================================================================
# admin/cycles.py  —  Cycles, per-cycle email text, token generation, emailing,
#                     and the live participation view (spec Sections 6.4–6.7, 8, 12)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# This module drives the *operational* half of a feedback round:
#   6.4  create / open / close a cycle (CA1 Intermediate, CA3 End-of-Course),
#   6.5  edit the per-cycle email body (with {placeholders}),
#   6.6  generate one token per student per cycle for a selected batch, and email
#        each student their single private link (.../f/<token>),
#   6.7  a live participation view — submitted vs pending per offering — plus a
#        one-click reminder to everyone who hasn't finished.
#
# THE TWO-DATABASE DANCE (spec Section 5):
#   * The `cycle` config row (label, email text, open/closed) lives in master.db.
#   * The `token` rows (who got a link, what they've completed) live in the
#     PER-CYCLE db file (cycle_<AY>_<CA>.db), created on demand here.
#   * Participation is computed ENTIRELY from identity-side data (students +
#     offerings in master.db, token.progress in the cycle db). It never reads the
#     anonymous answers, so "who is pending" can be known without ever linking a
#     person to their responses.
# ----------------------------------------------------------------------------

import os
import json
from flask import render_template, request, redirect, url_for, flash, current_app

import db
from db import get_master, get_cycle
from config import Config
from admin import admin_bp
import services
import emailer


def _read_cycle_schema():
    """Read schema_cycle.sql (token + response + answer DDL) as text."""
    path = os.path.join(Config.BASE_DIR, "schema_cycle.sql")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _open_cycle_db(cycle_row):
    """Open (creating + schema-applying if needed) the per-cycle db for a cycle.

    We always executescript the schema first: every statement is CREATE TABLE IF
    NOT EXISTS, so this is a harmless no-op on an existing file and guarantees a
    freshly-created cycle file has its token/response/answer tables.
    """
    conn = get_cycle(cycle_row["academic_year"], cycle_row["code"])
    conn.executescript(_read_cycle_schema())
    conn.commit()
    return conn


# ============================================================================
# CYCLES: list / create / open / close / edit email  (spec 6.4 & 6.5)
# ============================================================================

@admin_bp.route("/cycles")
def cycles_list():
    conn = get_master()
    cycles = conn.execute("SELECT * FROM cycle ORDER BY academic_year, code").fetchall()
    # Annotate each cycle with how many tokens exist and how many finished, by
    # peeking into its per-cycle db (creating nothing if the file is absent).
    data = []
    for c in cycles:
        n_tokens = n_done = n_responses = 0
        path = db.cycle_db_path(c["academic_year"], c["code"])
        if os.path.exists(path):
            cy = _open_cycle_db(c)
            n_tokens = cy.execute("SELECT COUNT(*) n FROM token").fetchone()["n"]
            n_done = cy.execute(
                "SELECT COUNT(*) n FROM token WHERE completed_all=1").fetchone()["n"]
            n_responses = cy.execute("SELECT COUNT(*) n FROM response").fetchone()["n"]
            cy.close()
        data.append({"row": c, "n_tokens": n_tokens, "n_done": n_done,
                     "n_responses": n_responses, "has_db": os.path.exists(path)})
    ay = conn.execute("SELECT * FROM academic_year WHERE is_active=1 LIMIT 1").fetchone()
    conn.close()
    return render_template("cycles_list.html", data=data, ay=ay)


@admin_bp.route("/cycles/new", methods=["POST"])
def cycles_new():
    code = request.form.get("code", "").strip().upper()
    label = request.form.get("label", "").strip()
    conn = get_master()
    ay = conn.execute("SELECT ay_label FROM academic_year WHERE is_active=1 LIMIT 1").fetchone()
    ay_label = ay["ay_label"] if ay else "AY 2026-27"
    if not (code and label):
        conn.close()
        flash("Cycle code and label are required.", "error")
        return redirect(url_for("admin.cycles_list"))
    # is_test (spec §9): a checkbox on the create form. A test cycle sends all
    # mail to the redirect address and is excluded from real analytics.
    is_test = 1 if request.form.get("is_test") == "on" else 0
    default_email = ("Dear {student_name},\n\n"
                     "Please submit your {cycle_name} feedback for all your courses here:\n"
                     "{link}\n\nYour feedback is completely anonymous.\n\n- SRET")
    try:
        conn.execute(
            "INSERT INTO cycle (code, label, academic_year, email_body, is_open, "
            "status, is_test) VALUES (?, ?, ?, ?, 0, 'DRAFT', ?)",
            (code, label, ay_label, default_email, is_test))
        conn.commit()
    except Exception as e:
        conn.close()
        flash(f"Could not create cycle (duplicate code for this AY?): {e}", "error")
        return redirect(url_for("admin.cycles_list"))
    conn.close()
    flash(f"Cycle {code} created (DRAFT{', TEST' if is_test else ''}). "
          f"Pass the Readiness Check, then open it.", "success")
    return redirect(url_for("admin.cycles_list"))


@admin_bp.route("/cycles/<int:cycle_id>/toggle", methods=["POST"])
def cycles_toggle(cycle_id):
    """Open or close a cycle. Opening also materialises its per-cycle db file so
    tokens can be generated and the student form can accept submissions."""
    conn = get_master()
    c = conn.execute("SELECT * FROM cycle WHERE id=?", (cycle_id,)).fetchone()
    if c is None:
        conn.close(); flash("Cycle not found.", "error")
        return redirect(url_for("admin.cycles_list"))
    new_state = 0 if c["is_open"] else 1
    # READINESS GATE (spec §8.6, §9): a real cycle can only OPEN once its cached
    # readiness_state is 'READY'. Test cycles bypass the gate (they exist to be
    # exercised). Closing is always allowed.
    if new_state == 1 and not c["is_test"] and c["readiness_state"] != "READY":
        conn.close()
        flash(f"Cannot open {c['code']}: Readiness Check is "
              f"{c['readiness_state']}. Resolve all flagged items first.", "error")
        return redirect(url_for("admin.readiness_page", cycle=c["code"]))
    status = "OPEN" if new_state == 1 else "CLOSED"
    conn.execute("UPDATE cycle SET is_open=?, status=? WHERE id=?",
                 (new_state, status, cycle_id))
    conn.commit()
    if new_state == 1:
        cy = _open_cycle_db(c); cy.close()   # ensure the answer/token file exists
    conn.close()
    # ACTIVITY LOG (Module 3): enrich the auto-recorded entry with the specifics —
    # which cycle and whether it was opened or closed — so the audit trail reads in
    # plain English. This single optional line does NOT write the row itself; the
    # after_request hook does, folding in this note (see activity_log.note).
    import activity_log
    activity_log.note(detail=f"Cycle {c['code']} set to {status}",
                      cycle_code=c["code"], target_type="cycle", target_id=cycle_id)
    flash(f"Cycle {c['code']} is now {status}.", "success")
    return redirect(url_for("admin.cycles_list"))


@admin_bp.route("/cycles/<int:cycle_id>/email", methods=["GET", "POST"])
def cycles_email(cycle_id):
    """Edit the per-cycle email body. The textarea IS the WYSIWYG field the spec
    asks for (plain-text email); placeholders {student_name}/{cycle_name}/{link}
    are filled per student at send time. A live preview is shown on GET."""
    conn = get_master()
    c = conn.execute("SELECT * FROM cycle WHERE id=?", (cycle_id,)).fetchone()
    if c is None:
        conn.close(); flash("Cycle not found.", "error")
        return redirect(url_for("admin.cycles_list"))
    if request.method == "POST":
        body = request.form.get("email_body", "")
        conn.execute("UPDATE cycle SET email_body=? WHERE id=?", (body, cycle_id))
        conn.commit(); conn.close()
        flash("Email text saved.", "success")
        return redirect(url_for("admin.cycles_email", cycle_id=cycle_id))
    conn.close()
    # A sample rendering so the professor sees exactly what a student receives.
    preview = emailer.render_body(c["email_body"], "Priya Kumar", c["label"],
                                  "http://<laptop-ip>:5000/f/EXAMPLE-TOKEN")
    return render_template("cycle_email.html", cycle=c, preview=preview)


# ============================================================================
# TOKENS + EMAIL  (spec 6.6, 8, 12)
# ============================================================================

# ----------------------------------------------------------------------------
# GET /admin/cycles/<id>/tokens — the batch picker: choose programme / dept /
# year, preview how many ELIGIBLE students match, then generate + optionally
# email. We show the eligible count live so a batch is never sent blind.
# ----------------------------------------------------------------------------
@admin_bp.route("/cycles/<int:cycle_id>/tokens", methods=["GET"])
def tokens_page(cycle_id):
    conn = get_master()
    c = conn.execute("SELECT * FROM cycle WHERE id=?", (cycle_id,)).fetchone()
    if c is None:
        conn.close(); flash("Cycle not found.", "error")
        return redirect(url_for("admin.cycles_list"))
    # Distinct values for the batch filter dropdowns (from the student master).
    programmes = conn.execute(
        "SELECT DISTINCT programme FROM students ORDER BY programme").fetchall()
    depts = conn.execute(
        "SELECT DISTINCT dept_code FROM students ORDER BY dept_code").fetchall()
    ay = conn.execute("SELECT * FROM academic_year WHERE is_active=1 LIMIT 1").fetchone()
    conn.close()

    # --- Live mail status for the pre-send banner ---------------------------
    # We compute EXACTLY what will happen the moment "Generate & send" is clicked,
    # from the same two switches the mailer itself obeys, so the banner can never
    # disagree with reality:
    #   1. mail_live  — is real SMTP configured? (emailer.smtp_settings()['enabled'],
    #      True iff FEEDBACK_SMTP_HOST is set). If False, messages are written to
    #      app/outbox/ as .eml files and NOTHING leaves the laptop.
    #   2. is_test    — is this a TEST cycle? If so the mailer hard-redirects every
    #      message to Config.TEST_REDIRECT_EMAIL, so students are never contacted.
    # The dangerous combination — real students actually get email — is ONLY
    # mail_live AND not is_test; the template highlights precisely that case.
    m = emailer.active_mode()                    # gmail-api > smtp > dev-outbox
    mail = {
        "live": m["live"],                       # True = real mail (gmail-api OR smtp)
        "mode": m["mode"],
        "host": m["host"],                       # shown when live, for reassurance
        "from_addr": m["from_addr"],
        "is_test": bool(c["is_test"]),           # test cycle -> recipients redirected
        "test_redirect": Config.TEST_REDIRECT_EMAIL,
        # The one true "real students will be emailed" flag the banner keys on.
        "reaches_students": m["live"] and not bool(c["is_test"]),
    }
    return render_template("tokens_page.html", cycle=c, programmes=programmes,
                           depts=depts, ay=ay, dept_names=Config.DEPT_CODES,
                           mail=mail)


# ----------------------------------------------------------------------------
# _select_batch(conn, cycle_code, programme, dept, year) — return the ELIGIBLE
# students matching the chosen batch filter (spec v3). "Eligible" now simply
# means "in this cycle's roster with status='active'" — excluded (TC/withdrawn)
# students are filtered out, and the year is the STORED roster year, not derived.
# ----------------------------------------------------------------------------
def _select_batch(conn, cycle_code, programme, dept, year):
    clauses = ["cycle_code=?", "status='active'"]
    params = [cycle_code]
    if dept:                      # dept_code IS the programme code (E01…E81)
        clauses.append("dept_code=?"); params.append(dept)
    if programme:                 # optional level filter ('B.Tech'/'B.Sc'/'M.Sc')
        clauses.append("programme=?"); params.append(programme)
    if year:                      # exact stored year_of_study
        clauses.append("year_of_study=?"); params.append(int(year))
    where = "WHERE " + " AND ".join(clauses)
    return conn.execute(f"SELECT * FROM students {where}", params).fetchall()


# ----------------------------------------------------------------------------
# POST /admin/cycles/<id>/tokens/generate — create one token per eligible
# student in the batch (idempotent: a student who already has a token for this
# cycle is skipped), then, if "send email" was ticked, email each of them their
# link. The link is built from the request host so it carries the laptop's LAN
# address automatically.
# ----------------------------------------------------------------------------
@admin_bp.route("/cycles/<int:cycle_id>/tokens/generate", methods=["POST"])
def tokens_generate(cycle_id):
    conn = get_master()
    c = conn.execute("SELECT * FROM cycle WHERE id=?", (cycle_id,)).fetchone()
    if c is None:
        conn.close(); flash("Cycle not found.", "error")
        return redirect(url_for("admin.cycles_list"))

    programme = request.form.get("programme", "").strip()
    dept = request.form.get("dept", "").strip()
    year = request.form.get("year", "").strip()
    do_send = request.form.get("send_email") == "on"

    batch = _select_batch(conn, c["code"], programme, dept, year)

    # Open the per-cycle db (Group A token table lives here).
    cy = _open_cycle_db(c)

    created = 0
    skipped = 0
    to_email = []   # (email, name, token) tuples for the mail step
    for s in batch:
        existing = cy.execute(
            "SELECT token FROM token WHERE reg_no=?", (s["reg_no"],)).fetchone()
        if existing:
            skipped += 1
            tok = existing["token"]
        else:
            tok = services.new_token()
            cy.execute("INSERT INTO token (token, reg_no) VALUES (?, ?)",
                       (tok, s["reg_no"]))
            created += 1
        if s["email"]:
            to_email.append((s["email"], s["name"], tok))
    cy.commit(); cy.close()
    conn.close()

    msg = f"Batch of {len(batch)} eligible student(s): {created} new token(s), {skipped} already had one."

    # ---- optional email step -------------------------------------------------
    if do_send and to_email:
        # request.url_root is like "http://192.168.1.7:5000/" — exactly the LAN
        # address students need. We build each student's /f/<token> link off it.
        root = request.url_root.rstrip("/")
        messages = []
        for (email, name, tok) in to_email:
            link = f"{root}/f/{tok}"
            body = emailer.render_body(c["email_body"], name, c["label"], link)
            messages.append({"to": email, "body": body})
        subject = f"SRET Feedback — {c['label']}"
        # Pass the cycle's is_test flag so the mailer HARD-REDIRECTS every message
        # to the test address in code (spec §9.1) — never a real send from a test
        # cycle, regardless of the addresses in the batch.
        result = emailer.send_batch(Config.BASE_DIR, subject, messages,
                                    is_test=bool(c["is_test"]))
        if result["mode"] == "dev-outbox":
            msg += (f" Email DEV mode: {result['count']} message(s) written to "
                    f"{result['outbox']} (no SMTP configured).")
        else:
            msg += f" Emailed {result['count']} student(s) via SMTP."
        if result["errors"]:
            msg += f" {len(result['errors'])} send error(s) — see server log."
            current_app.logger.warning("Email errors: %s", result["errors"])
    elif do_send:
        msg += " (No students in this batch had an email address.)"

    # ACTIVITY LOG (Module 3): record WHICH batch and HOW MANY tokens, so the audit
    # trail shows exactly what was generated/sent, not just "generated tokens". The
    # after_request hook writes the row; this note supplies the detail + scope.
    import activity_log
    scope = " · ".join(p for p in (programme, dept, year) if p) or "all"
    activity_log.note(
        detail=f"{created} new, {skipped} existing token(s) for [{scope}]"
               + ("; emails sent" if (do_send and to_email) else ""),
        cycle_code=c["code"], target_type="cycle", target_id=cycle_id)

    flash(msg, "success")
    return redirect(url_for("admin.tokens_page", cycle_id=cycle_id))


# ============================================================================
# PARTICIPATION VIEW + REMINDERS  (spec 6.7)
# ============================================================================

# ----------------------------------------------------------------------------
# GET /admin/cycles/<id>/participation — the live "submitted vs pending" board.
#
# Computed purely from identity-side data:
#   * every token -> its student -> that student's expected courses (offerings),
#   * token.progress_json tells us which of those courses are DONE.
# From that we roll up two views:
#   (a) per-student: done / total, completed_all flag (drives reminders),
#   (b) per-offering: expected vs submitted vs pending.
# No answer row is ever read, so anonymity of CONTENT is untouched.
# ----------------------------------------------------------------------------
@admin_bp.route("/cycles/<int:cycle_id>/participation")
def participation(cycle_id):
    conn = get_master()
    c = conn.execute("SELECT * FROM cycle WHERE id=?", (cycle_id,)).fetchone()
    if c is None:
        conn.close(); flash("Cycle not found.", "error")
        return redirect(url_for("admin.cycles_list"))

    path = db.cycle_db_path(c["academic_year"], c["code"])
    if not os.path.exists(path):
        conn.close()
        flash("No tokens generated for this cycle yet.", "error")
        return redirect(url_for("admin.cycles_list"))
    cy = _open_cycle_db(c)
    tokens = cy.execute("SELECT * FROM token").fetchall()
    cy.close()

    # Per-offering accumulators: offering_id -> {"expected":n, "submitted":n}.
    per_offering = {}
    per_student = []
    n_completed_all = 0

    for t in tokens:
        student = conn.execute(
            "SELECT * FROM students WHERE reg_no=? AND cycle_code=?",
            (t["reg_no"], c["code"])).fetchone()
        if student is None:
            continue  # token for a since-excluded/removed student; skip defensively
        courses = services.courses_for_student(conn, student, c["code"])
        try:
            progress = json.loads(t["progress_json"] or "{}")
        except ValueError:
            progress = {}
        done_ids = {int(k) for k, v in progress.items() if v == "done"}
        total = len(courses)
        done = sum(1 for o in courses if o["id"] in done_ids)
        if t["completed_all"]:
            n_completed_all += 1
        per_student.append({
            "reg_no": student["reg_no"], "name": student["name"],
            "email": student["email"], "dept_code": student["dept_code"],
            "done": done, "total": total,
            "completed_all": bool(t["completed_all"]),
        })
        # Roll into the per-offering board.
        for o in courses:
            acc = per_offering.setdefault(
                o["id"], {"offering": o, "expected": 0, "submitted": 0})
            acc["expected"] += 1
            if o["id"] in done_ids:
                acc["submitted"] += 1

    conn.close()

    # Turn the per-offering dict into a sorted list with pending computed.
    offering_rows = []
    for oid, acc in per_offering.items():
        acc["pending"] = acc["expected"] - acc["submitted"]
        offering_rows.append(acc)
    offering_rows.sort(key=lambda a: (a["offering"]["dept_code"],
                                      a["offering"]["year_of_study"],
                                      a["offering"]["course_code"]))
    # Sort students: unfinished first (so reminders targets are on top).
    per_student.sort(key=lambda s: (s["completed_all"], s["reg_no"]))

    # Same live-mail status the Tokens page shows, so the "Send reminder" button
    # carries an identical pre-send warning (see tokens_page for the full rationale).
    m = emailer.active_mode()                    # gmail-api > smtp > dev-outbox
    mail = {
        "live": m["live"],
        "host": m["host"],
        "from_addr": m["from_addr"],
        "is_test": bool(c["is_test"]),
        "test_redirect": Config.TEST_REDIRECT_EMAIL,
        "reaches_students": m["live"] and not bool(c["is_test"]),
    }
    return render_template("participation.html", cycle=c,
                           per_student=per_student, offering_rows=offering_rows,
                           n_tokens=len(tokens), n_completed_all=n_completed_all,
                           mail=mail)


# ============================================================================
# RESULTS / DISTRIBUTION  (Version 2.0 · §6/§7) — the post-close workflow
# ============================================================================
# Once a cycle is CLOSED, this is where the professor: sets the POOR threshold,
# runs scoring + classification, reviews the per-teacher/per-course band list, and
# sends every teacher their results (one email each, a report PDF per course, with
# an inline "File ATR" link under any POOR course). The heavy lifting lives in the
# already-built, already-tested classification.py + distribution.py; these routes
# only surface them and handle the form round-trips.
# ----------------------------------------------------------------------------

def _run_classify(master, c):
    """Run classification for cycle `c` using its current thresholds, committing
    the verdicts to master.db. Returns a human summary line for the flash. Safe to
    call when no responses exist yet (returns a gentle note instead of erroring)."""
    import classification
    path = db.cycle_db_path(c["academic_year"], c["code"])
    if not os.path.exists(path):
        return "No responses collected yet — nothing to classify."
    cy = _open_cycle_db(c)
    try:
        summary = classification.classify_cycle(master, cy, c)
        master.commit()                       # classify_cycle writes, caller commits
    finally:
        cy.close()
    return ("Classified %d course(s): %d GOOD, %d POOR, %d insufficient (n below "
            "minimum)." % (summary["total"], summary["good"], summary["poor"],
                           summary["insufficient"]))


def _default_intro(c):
    """The default faculty-email preamble when the admin hasn't set one."""
    return ("Dear Faculty,\n\nPlease find your student-feedback results for %s "
            "below. The detailed report for each of your courses is attached as a "
            "PDF." % c["label"])


# ----------------------------------------------------------------------------
# GET /admin/cycles/<id>/distribute — the Results / Distribution control page.
# ----------------------------------------------------------------------------
@admin_bp.route("/cycles/<int:cycle_id>/distribute", methods=["GET"])
def distribute_page(cycle_id):
    conn = get_master()
    c = conn.execute("SELECT * FROM cycle WHERE id=?", (cycle_id,)).fetchone()
    if c is None:
        conn.close(); flash("Cycle not found.", "error")
        return redirect(url_for("admin.cycles_list"))

    # The classified per-course rows, joined to the offering identity. POOR first
    # (that's where the professor's attention goes), then by dept / faculty / course.
    rows = conn.execute(
        """
        SELECT oc.offering_id, oc.band, oc.overall_score, oc.n_responses, oc.reason,
               o.faculty, o.faculty_email, o.course_code, o.course_name,
               o.dept_code, o.year_of_study
        FROM offering_classification oc
        JOIN offering o ON o.id = oc.offering_id
        WHERE oc.cycle_code = ?
        ORDER BY (oc.band='POOR') DESC, o.dept_code, o.faculty, o.course_code
        """,
        (c["code"],)).fetchall()

    tally = {"GOOD": 0, "POOR": 0, "INSUF": 0}
    faculty, poor_faculty = set(), set()
    for r in rows:
        if r["band"] == "GOOD":
            tally["GOOD"] += 1
        elif r["band"] == "POOR":
            tally["POOR"] += 1
        else:
            tally["INSUF"] += 1
        if r["faculty_email"]:
            faculty.add(r["faculty_email"])
            if r["band"] == "POOR":
                poor_faculty.add(r["faculty_email"])

    intro = (c["dist_intro"] if ("dist_intro" in c.keys() and c["dist_intro"])
             else _default_intro(c))

    # Same live-mail status banner the Tokens/Participation pages show, so the
    # professor knows whether a send goes to real inboxes or the dev outbox.
    m = emailer.active_mode()                    # gmail-api > smtp > dev-outbox
    mail = {
        "live": m["live"],
        "from_addr": m["from_addr"],
        "is_test": bool(c["is_test"]),
        "reaches_faculty": m["live"] and not bool(c["is_test"]),
        "test_redirect": Config.TEST_REDIRECT_EMAIL,   # the default test address
    }
    conn.close()
    return render_template("distribute.html", cycle=c, rows=rows, tally=tally,
                           n_faculty=len(faculty), n_poor_faculty=len(poor_faculty),
                           intro=intro, mail=mail)


# ----------------------------------------------------------------------------
# POST /admin/cycles/<id>/distribute/thresholds — save the POOR level (and the
# optional section cut + minimum responses), then RE-CLASSIFY with the new values.
# ----------------------------------------------------------------------------
@admin_bp.route("/cycles/<int:cycle_id>/distribute/thresholds", methods=["POST"])
def distribute_thresholds(cycle_id):
    conn = get_master()
    c = conn.execute("SELECT * FROM cycle WHERE id=?", (cycle_id,)).fetchone()
    if c is None:
        conn.close(); flash("Cycle not found.", "error")
        return redirect(url_for("admin.cycles_list"))

    # Parse the three numbers defensively — a stray value falls back to the sane
    # default rather than erroring (overall 8.0, section off, min 10).
    def _f(name, default):
        raw = request.form.get(name, "").strip()
        if raw == "":
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    overall = _f("threshold_overall", 8.0)
    section_raw = request.form.get("threshold_section", "").strip()
    section = None if section_raw == "" else _f("threshold_section", None)
    min_raw = request.form.get("min_responses", "").strip()
    try:
        min_responses = int(min_raw) if min_raw else 10
    except ValueError:
        min_responses = 10

    conn.execute(
        "UPDATE cycle SET threshold_overall=?, threshold_section=?, min_responses=? "
        "WHERE id=?", (overall, section, min_responses, cycle_id))
    conn.commit()

    c = conn.execute("SELECT * FROM cycle WHERE id=?", (cycle_id,)).fetchone()
    note = _run_classify(conn, c)
    conn.close()
    flash("Saved: a course is POOR when its overall is below %.2f. %s"
          % (overall, note), "success")
    return redirect(url_for("admin.distribute_page", cycle_id=cycle_id))


# ----------------------------------------------------------------------------
# POST /admin/cycles/<id>/distribute/classify — (re)run scoring + classification
# WITHOUT changing thresholds (e.g. after late submissions).
# ----------------------------------------------------------------------------
@admin_bp.route("/cycles/<int:cycle_id>/distribute/classify", methods=["POST"])
def distribute_classify(cycle_id):
    conn = get_master()
    c = conn.execute("SELECT * FROM cycle WHERE id=?", (cycle_id,)).fetchone()
    if c is None:
        conn.close(); flash("Cycle not found.", "error")
        return redirect(url_for("admin.cycles_list"))
    note = _run_classify(conn, c)
    conn.close()
    flash(note, "success")
    return redirect(url_for("admin.distribute_page", cycle_id=cycle_id))


# ----------------------------------------------------------------------------
# POST /admin/cycles/<id>/distribute/send — THE send. Emails every teacher their
# results (one email, a PDF per course, inline ATR link under POOR courses), and
# optionally the HOD/Dean roll-ups. Runs classification first so bands are fresh.
# ----------------------------------------------------------------------------
@admin_bp.route("/cycles/<int:cycle_id>/distribute/send", methods=["POST"])
def distribute_send(cycle_id):
    import distribution
    import atr_workflow
    import activity_log
    conn = get_master()
    c = conn.execute("SELECT * FROM cycle WHERE id=?", (cycle_id,)).fetchone()
    if c is None:
        conn.close(); flash("Cycle not found.", "error")
        return redirect(url_for("admin.cycles_list"))

    # Persist the edited email preamble so it survives for next time.
    intro = request.form.get("intro", "").strip()
    conn.execute("UPDATE cycle SET dist_intro=? WHERE id=?", (intro, cycle_id))
    conn.commit()

    # Optional TEST redirect: send every faculty email to this one address instead
    # of the teachers (and include teachers whose email column is blank). Typed on
    # the page, so no environment variable is needed.
    redirect_to = request.form.get("redirect_to", "").strip() or None

    # Which audiences? Faculty always; leader roll-ups only if the box is ticked.
    roles = [atr_workflow.ROLE_FACULTY]
    if request.form.get("send_leaders") == "on":
        roles += [atr_workflow.ROLE_HOD, atr_workflow.ROLE_VICE_DEAN,
                  atr_workflow.ROLE_DEAN]

    # Build the ATR links off the address the admin is browsing (the LAN URL), so
    # the /atr/file links resolve for faculty on the same network — exactly how the
    # student token links are built on the Tokens page.
    base = request.url_root.rstrip("/")

    path = db.cycle_db_path(c["academic_year"], c["code"])
    if not os.path.exists(path):
        conn.close(); flash("No responses collected for this cycle yet.", "error")
        return redirect(url_for("admin.distribute_page", cycle_id=cycle_id))

    cy = _open_cycle_db(c)
    try:
        summary = distribution.distribute_cycle(
            conn, cy, c, base_url=base, roles=roles, email_intro=intro,
            redirect_to=redirect_to)
    finally:
        cy.close()
        conn.close()

    # Enrich the activity-log entry with the outcome.
    activity_log.note(
        detail="%d faculty email(s), %d ATR link(s), %d leader roll-up(s) [mode=%s]"
               % (summary["faculty_emails"], summary["atr_links"],
                  summary["leader_emails"], summary["mode"]),
        cycle_code=c["code"], target_type="cycle", target_id=cycle_id)

    errbit = (" %d error(s) — see server log." % len(summary["errors"])
              if summary["errors"] else "")
    flash("Distribution complete (%s): %d faculty email(s) sent, including %d "
          "ATR link(s); %d leader roll-up(s).%s"
          % (summary["mode"], summary["faculty_emails"], summary["atr_links"],
             summary["leader_emails"], errbit), "success")
    return redirect(url_for("admin.distribute_page", cycle_id=cycle_id))


# ----------------------------------------------------------------------------
# POST /admin/cycles/<id>/remind — email a reminder to everyone who has NOT yet
# completed all their forms. Reuses the same per-cycle email body + link, so the
# reminder carries the student's own resume link (pause/resume, spec Section 8).
# ----------------------------------------------------------------------------
@admin_bp.route("/cycles/<int:cycle_id>/remind", methods=["POST"])
def participation_remind(cycle_id):
    conn = get_master()
    c = conn.execute("SELECT * FROM cycle WHERE id=?", (cycle_id,)).fetchone()
    if c is None:
        conn.close(); flash("Cycle not found.", "error")
        return redirect(url_for("admin.cycles_list"))
    cy = _open_cycle_db(c)
    pending = cy.execute("SELECT * FROM token WHERE completed_all=0").fetchall()
    cy.close()

    root = request.url_root.rstrip("/")
    messages = []
    for t in pending:
        student = conn.execute(
            "SELECT name, email FROM students WHERE reg_no=?", (t["reg_no"],)).fetchone()
        if student and student["email"]:
            link = f"{root}/f/{t['token']}"
            body = emailer.render_body(c["email_body"], student["name"], c["label"], link)
            # A gentle reminder prefix so it reads as a nudge, not a first ask.
            body = "Reminder: you have pending course feedback.\n\n" + body
            messages.append({"to": student["email"], "body": body})
    conn.close()

    if not messages:
        flash("No pending students with an email address to remind.", "success")
        return redirect(url_for("admin.participation", cycle_id=cycle_id))

    subject = f"Reminder — SRET Feedback {c['label']}"
    # SAFETY: pass the cycle's is_test flag so reminders obey the SAME hard
    # redirect as the initial token send. Without this, a reminder on a TEST cycle
    # would bypass the test address and reach real students — the exact accident
    # the test flag exists to prevent.
    result = emailer.send_batch(Config.BASE_DIR, subject, messages,
                                is_test=bool(c["is_test"]))
    if result["mode"] == "dev-outbox":
        flash(f"Reminder DEV mode: {result['count']} message(s) written to {result['outbox']}.",
              "success")
    else:
        flash(f"Reminded {result['count']} pending student(s) via SMTP.", "success")
    return redirect(url_for("admin.participation", cycle_id=cycle_id))


# ============================================================================
# ARCHIVE & RESET  (spec Sections 6.9 & 13)
# ============================================================================
# Close a cycle for good and start the NEXT clean cycle, WITHOUT ever touching
# master.db (the permanent Student Master + configuration persist across cycles).
#
# What "archive & reset" does, step by step:
#   1. Force the cycle CLOSED (no further submissions).
#   2. CHECKPOINT the per-cycle db so WAL side-files (-wal/-shm) are folded back
#      into the single .db file — otherwise the moved copy could miss the most
#      recent, still-in-WAL responses.
#   3. MOVE (not copy) the per-cycle db file into app/archive/, renamed with the
#      cycle code + a timestamp, e.g. cycle_2026-27_CA1__archived-20260719-2210.db.
#      The archive remains fully readable forever if the college ever needs it.
#   4. Recreate a FRESH, EMPTY per-cycle db at the original path so the very same
#      cycle row can start collecting again clean (e.g. a re-run), or so the next
#      cycle (CA3) — which uses its OWN filename — is unaffected.
#
# Because only the cycle FILE is moved and master.db is never opened for writing
# here, the "confidential archive-and-move step never touches the master" rule
# (spec Section 13) is guaranteed structurally.
# ----------------------------------------------------------------------------

@admin_bp.route("/cycles/<int:cycle_id>/archive", methods=["POST"])
def cycles_archive(cycle_id):
    conn = get_master()
    c = conn.execute("SELECT * FROM cycle WHERE id=?", (cycle_id,)).fetchone()
    if c is None:
        conn.close(); flash("Cycle not found.", "error")
        return redirect(url_for("admin.cycles_list"))

    # A tiny safety valve: require the admin to type the cycle code to confirm,
    # so a stray click cannot archive live data. The confirm field is on the
    # cycles page form.
    confirm = request.form.get("confirm_code", "").strip().upper()
    if confirm != c["code"].upper():
        conn.close()
        flash(f"Archive not confirmed — type '{c['code']}' to confirm.", "error")
        return redirect(url_for("admin.cycles_list"))

    # 1. Force the cycle closed in master.db (config-only write — allowed).
    conn.execute("UPDATE cycle SET is_open=0 WHERE id=?", (cycle_id,))
    conn.commit()

    path = db.cycle_db_path(c["academic_year"], c["code"])
    if not os.path.exists(path):
        conn.close()
        flash("This cycle has no data file yet — nothing to archive.", "error")
        return redirect(url_for("admin.cycles_list"))

    # 2. Checkpoint WAL so the single .db file is complete before we move it.
    cy = get_cycle(c["academic_year"], c["code"])
    cy.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    cy.close()

    # 3. Move the file into the archive folder with a timestamped name.
    os.makedirs(Config.ARCHIVE_DIR, exist_ok=True)
    stamp = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.basename(path).rsplit(".db", 1)[0]
    archive_name = f"{base}__archived-{stamp}.db"
    archive_path = os.path.join(Config.ARCHIVE_DIR, archive_name)
    import shutil
    shutil.move(path, archive_path)
    # Also move any leftover WAL side-files defensively (should be gone after the
    # TRUNCATE checkpoint, but we tidy up just in case).
    for side in ("-wal", "-shm"):
        if os.path.exists(path + side):
            try:
                os.remove(path + side)
            except OSError:
                pass

    # 4. Recreate a fresh, empty per-cycle db at the original path.
    fresh = _open_cycle_db(c)   # applies schema_cycle.sql to a new empty file
    fresh.close()

    conn.close()
    flash(f"Cycle {c['code']} archived to {archive_name} and reset to a fresh, "
          f"empty cycle. The Student Master and all configuration are untouched.",
          "success")
    return redirect(url_for("admin.cycles_list"))
