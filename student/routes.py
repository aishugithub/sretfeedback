# ============================================================================
# student/routes.py  —  The anonymous student feedback flow (spec Section 8)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# This is the student-facing heart of the system. From a single emailed link the
# student:
#   1. lands on /f/<token>  -> we identify them (year/dept/section, never shown to
#      others) and list ALL their courses for this cycle, with tick marks for the
#      ones already done (pause/resume),
#   2. opens /f/<token>/course/<offering_id> -> the CORRECT category form for that
#      course, questions rendered VERBATIM from the frozen template version,
#   3. submits -> answers are written ANONYMOUSLY to Group B (response + answer),
#      and, as a SEPARATE write sharing no key, that course is ticked off in the
#      token's progress (Group A). When every course is done the token is marked
#      completed_all and a thank-you page shows; further submissions are refused.
#
# THE ANONYMITY MECHANIC, concretely (spec Section 5):
#   * Group B write:  INSERT response(offering_id, template_version_id, time) and
#                     its answer rows — NO token, NO reg_no, NO student id.
#   * Group A write:  UPDATE token.progress_json to mark offering_id "done".
#   These are two independent statements with no shared key, so the content of a
#   response can never be joined back to the person who submitted it.
#
# FINDING THE CYCLE FOR A TOKEN: tokens live in per-cycle db files. Given a raw
# token we scan the (few) known cycles from master.db and look it up; the match
# tells us which cycle db + which cycle config (open/closed, label) applies.
# ----------------------------------------------------------------------------

import json
from datetime import datetime
from flask import (render_template, request, redirect, url_for, abort, flash)

import db
from db import get_master
from student import student_bp
import services


# ----------------------------------------------------------------------------
# _find_token(token) -> (cycle_row, cycle_conn, token_row) or (None, None, None)
# ----------------------------------------------------------------------------
# Scan every cycle registered in master.db, open its per-cycle file (if present),
# and look for the token. Returns the cycle config row, an OPEN connection to
# that cycle's db (caller must close it), and the token row. Because tokens are
# long random strings, at most one cycle will contain a given token.
# ----------------------------------------------------------------------------
def _find_token(token):
    master = get_master()
    cycles = master.execute("SELECT * FROM cycle").fetchall()
    master.close()
    import os
    for c in cycles:
        path = db.cycle_db_path(c["academic_year"], c["code"])
        if not os.path.exists(path):
            continue
        cy = db.get_cycle(c["academic_year"], c["code"])
        row = cy.execute("SELECT * FROM token WHERE token=?", (token,)).fetchone()
        if row:
            return c, cy, row
        cy.close()
    return None, None, None


# ----------------------------------------------------------------------------
# _student_and_courses(token_row, cycle_row) -> (student_row, courses)
# ----------------------------------------------------------------------------
# Resolve the token's student from master.db and compute their course list for
# this cycle's academic year. Opens and closes its own master connection.
# ----------------------------------------------------------------------------
def _student_and_courses(token_row, cycle_row):
    # v3: students are scoped PER CYCLE, so look the student up by (reg_no,
    # cycle_code). Their course list is resolved from the cycle's allocation +
    # enrollment (no year derivation any more) — see services.courses_for_student.
    master = get_master()
    student = master.execute(
        "SELECT * FROM students WHERE reg_no=? AND cycle_code=?",
        (token_row["reg_no"], cycle_row["code"])).fetchone()
    courses = []
    if student is not None:
        courses = services.courses_for_student(master, student, cycle_row["code"])
    master.close()
    return student, courses


def _progress_dict(token_row):
    """Parse token.progress_json into a {offering_id(int): 'done'} dict safely."""
    try:
        raw = json.loads(token_row["progress_json"] or "{}")
    except ValueError:
        raw = {}
    return {int(k): v for k, v in raw.items()}


# ============================================================================
# STEP 1 — THE LANDING PAGE: list all the student's courses  (spec 8.1–8.2)
# ============================================================================
@student_bp.route("/f/<token>")
def landing(token):
    cycle, cy, tok = _find_token(token)
    if tok is None:
        # Unknown/типо'd token — a generic invalid page (no information leak).
        return render_template("student_invalid.html"), 404

    # Already fully done? Show the thank-you page and stop (spec 8.5).
    if tok["completed_all"]:
        cy.close()
        return render_template("student_done.html", cycle=cycle)

    student, courses = _student_and_courses(tok, cycle)
    progress = _progress_dict(tok)
    cy.close()

    if student is None:
        return render_template("student_invalid.html"), 404

    # Decorate each course with its done-state and whether it has a usable form
    # (a category assigned). We DON'T show the student's identity to anyone else,
    # but we do greet them by first name on their own private page.
    items = []
    for o in courses:
        items.append({
            "offering": o,
            "done": progress.get(o["id"]) == "done",
            "has_category": o["category_id"] is not None,
        })
    n_done = sum(1 for i in items if i["done"])
    return render_template("student_landing.html",
                           token=token, cycle=cycle, student=student,
                           items=items, n_done=n_done, n_total=len(items),
                           cycle_open=bool(cycle["is_open"]))


# ============================================================================
# STEP 2 — THE PER-COURSE FORM  (spec 8.3)
# ============================================================================
@student_bp.route("/f/<token>/course/<int:offering_id>")
def course_form(token, offering_id):
    cycle, cy, tok = _find_token(token)
    if tok is None:
        return render_template("student_invalid.html"), 404
    if tok["completed_all"]:
        cy.close()
        return render_template("student_done.html", cycle=cycle)
    progress = _progress_dict(tok)
    cy.close()

    master = get_master()
    offering = master.execute(
        "SELECT o.*, c.name AS category_name, c.id AS cat_id "
        "FROM offering o LEFT JOIN category c ON c.id=o.category_id WHERE o.id=?",
        (offering_id,)).fetchone()
    if offering is None or offering["category_id"] is None:
        master.close()
        flash("This course has no feedback form configured yet.", "error")
        return redirect(url_for("student.landing", token=token))

    # The frozen question snapshot the student will answer.
    tv_id = services.current_template_version_id(master, offering["category_id"])
    questions = services.questions_for_version(master, tv_id) if tv_id else []
    master.close()

    already_done = progress.get(offering_id) == "done"
    return render_template("student_course_form.html",
                           token=token, cycle=cycle, offering=offering,
                           questions=questions, template_version_id=tv_id,
                           already_done=already_done,
                           cycle_open=bool(cycle["is_open"]))


# ============================================================================
# STEP 3 — SUBMIT one course's feedback  (spec 8.4 — the two anonymous writes)
# ============================================================================
@student_bp.route("/f/<token>/course/<int:offering_id>/submit", methods=["POST"])
def course_submit(token, offering_id):
    cycle, cy, tok = _find_token(token)
    if tok is None:
        return render_template("student_invalid.html"), 404

    # Refuse if the cycle is closed or the student already finished everything.
    if not cycle["is_open"]:
        cy.close()
        flash("This feedback cycle is currently closed.", "error")
        return redirect(url_for("student.landing", token=token))
    if tok["completed_all"]:
        cy.close()
        return render_template("student_done.html", cycle=cycle)

    progress = _progress_dict(tok)
    # Guard against a double submit for the same course (idempotent tick-off).
    if progress.get(offering_id) == "done":
        cy.close()
        flash("You have already submitted feedback for that course.", "success")
        return redirect(url_for("student.landing", token=token))

    # ---- resolve the offering + its frozen question snapshot (master.db) -----
    master = get_master()
    offering = master.execute(
        "SELECT * FROM offering WHERE id=?", (offering_id,)).fetchone()
    if offering is None or offering["category_id"] is None:
        master.close(); cy.close()
        flash("This course has no feedback form configured.", "error")
        return redirect(url_for("student.landing", token=token))
    tv_id = services.current_template_version_id(master, offering["category_id"])
    questions = services.questions_for_version(master, tv_id)

    # ---- SERVER-SIDE MANDATORY VALIDATION (spec §11) -------------------------
    # Every rating (non-free-text) question is required. We re-check on the
    # server because a student can disable JS or POST directly. On any miss we
    # re-render the form with the unanswered ids highlighted and the values they
    # DID choose preserved — nothing is written and nothing is lost.
    prior = {}
    missing = []
    for q in questions:
        val = request.form.get(f"q{q['id']}", "").strip()
        prior[str(q["id"])] = val
        if not q["is_free_text"] and val == "":
            missing.append(q["id"])
    if missing:
        master.close(); cy.close()
        return render_template("student_course_form.html",
                               token=token, cycle=cycle, offering=offering,
                               questions=questions, template_version_id=tv_id,
                               already_done=False, cycle_open=bool(cycle["is_open"]),
                               errors=set(missing), prior=prior), 400

    # ---- STRAIGHT-LINING SIGNAL (spec §11.1) ---------------------------------
    # Mandating answers buys completeness, not honesty. Flag a submission whose
    # rating answers have ZERO variance (all identical) AND that was completed
    # implausibly fast (< 5s from opening). We only RECORD the flag; we never
    # block, since a fast genuine response is possible. Stored on the response.
    rating_vals = [request.form.get(f"q{q['id']}", "")
                   for q in questions if not q["is_free_text"]]
    opened_at = request.form.get("opened_at", "")
    fast = False
    try:
        fast = opened_at and (datetime.now().timestamp() * 1000 - float(opened_at)) < 5000
    except ValueError:
        fast = False
    straight_lined = 1 if (len(set(rating_vals)) == 1 and fast) else 0

    # Lock the template version the instant the first real response uses it, so the
    # professor can no longer edit it mid-cycle (spec 6.3).
    #
    # CONCURRENCY GUARD (502 fix): this used to run an UPDATE + commit on EVERY
    # submission, taking a master.db WRITE lock each time — even though only the
    # very first submission actually flips is_locked 0->1. When a whole class
    # submits together, that serialised every student on master.db's single writer
    # and was a prime cause of slow responses / broken-pipe 502s. We now READ
    # is_locked first (a WAL read never blocks a writer and vice-versa) and only
    # acquire the write lock while it is still 0. After the first student locks the
    # version, every later student skips the master write entirely — so the hot
    # path touches master.db read-only.
    tv_row = master.execute(
        "SELECT is_locked FROM template_version WHERE id=?", (tv_id,)).fetchone()
    if tv_row is not None and not tv_row["is_locked"]:
        master.execute(
            "UPDATE template_version SET is_locked=1 WHERE id=? AND is_locked=0", (tv_id,))
        master.commit()
    master.close()

    # Fetch the student's full course list NOW, BEFORE we open the cycle write
    # transaction below. It is a master.db read; doing it here (rather than in the
    # middle of the cycle write, as the code used to) means the cycle's single
    # write lock is held only for the handful of INSERT/UPDATE statements and their
    # commit — not while we round-trip to master.db. Shorter lock hold == less
    # contention when a whole class submits at once (502 fix). `all_ids` is every
    # course this student must complete; we compare against it after the writes.
    _student, _courses = _student_and_courses(tok, cycle)
    all_ids = {o["id"] for o in _courses}

    # ---- GROUP B WRITE (anonymous) -------------------------------------------
    # One `response` row (course + version + time, NO identity) and one `answer`
    # row per question. `value` stores the chosen option LABEL (or the free-text
    # comment); scoring maps labels->weights later, so nothing identifying and no
    # weight is duplicated here.
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = cy.cursor()
    # Defensive: older cycle DB files created before v3 lack quality_flag. Add it
    # once if missing so the INSERT below works everywhere.
    cols = [r["name"] for r in cy.execute("PRAGMA table_info(response)")]
    if "quality_flag" not in cols:
        cy.execute("ALTER TABLE response ADD COLUMN quality_flag INTEGER NOT NULL DEFAULT 0")
    cur.execute(
        "INSERT INTO response (offering_id, template_version_id, submitted_at, quality_flag) "
        "VALUES (?, ?, ?, ?)", (offering_id, tv_id, now, straight_lined))
    response_id = cur.lastrowid
    for q in questions:
        # Each question's answer arrives as form field "q<question_id>".
        value = request.form.get(f"q{q['id']}", "").strip()
        # Store even blank answers as NULL so "respondents" counts stay correct
        # in scoring (a skipped question is a non-answer, not a zero).
        cur.execute(
            "INSERT INTO answer (response_id, question_id, value) VALUES (?, ?, ?)",
            (response_id, q["id"], value if value != "" else None))

    # ---- GROUP A WRITE (participation) — SEPARATE, shares no key -------------
    # Tick this course off in the token's progress. This statement does not
    # reference response_id or any answer; the two writes are unlinkable.
    progress[offering_id] = "done"

    # Did this complete every course? `all_ids` was fetched BEFORE the cycle write
    # (above), so we do NOT reopen master.db in the middle of this open cycle
    # transaction. `done_ids` is pure in-memory dict work — no I/O — keeping the
    # write-lock window minimal. A student with 6 courses is "done" only at all 6.
    done_ids = {oid for oid, v in progress.items() if v == "done"}
    completed_all = all_ids.issubset(done_ids) and len(all_ids) > 0

    cur.execute(
        "UPDATE token SET progress_json=?, completed_all=?, completed_at=? WHERE id=?",
        (json.dumps({str(k): v for k, v in progress.items()}),
         1 if completed_all else 0,
         now if completed_all else None,
         tok["id"]))
    cy.commit(); cy.close()

    if completed_all:
        return render_template("student_done.html", cycle=cycle)
    flash("Thank you — that course's feedback was recorded. "
          "You can continue with the rest whenever you like.", "success")
    return redirect(url_for("student.landing", token=token))
