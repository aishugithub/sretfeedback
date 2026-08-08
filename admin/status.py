# ============================================================================
# admin/status.py  —  v2.1 : the cycle STATUS / tracking board (with filters)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# The professor's requirement: "a page where status is shown" — student
# participation AND, crucially, the TEACHER side of a cycle: who was sent their
# results/ATR, who has NOT yet submitted their ATR, and which HODs (and VD/Dean)
# have NOT acted on the ATRs in front of them — with "all kinds of filtering".
#
# WHAT IT SHOWS, for one selected cycle:
#   1. A participation summary (students invited / completed / still pending),
#      computed from the per-cycle token table — the SAME numbers the detailed
#      Participation page shows, condensed to tiles here.
#   2. An ATR tracking table: one row per POOR course (a POOR band is the single
#      ATR trigger), showing the faculty, the endorsing HOD, the ATR state, WHO it
#      is waiting on right now, and whether the faculty has submitted yet.
#   3. Filters over that table: by department, by ATR state, by a "waiting on"
#      bucket (faculty not submitted / HOD not acted / VD / Dean / done), and a
#      free-text search over faculty + course.
#
# DATA SOURCES (anonymity preserved): master.db for offering / classification /
# faculty / org-tree, and the per-cycle DB for the token progress + atr/atr_event
# state. It never reads a response or joins identity to answers.
# ----------------------------------------------------------------------------

import os

from flask import render_template, request, redirect, url_for, flash

import db
from db import get_master
from config import Config
from admin import admin_bp
import rbac
import atr_workflow


def _open_cycle_db(cycle_row):
    """Open the per-cycle DB and ensure its schema (atr/atr_event/token) exists.
    CREATE ... IF NOT EXISTS makes this a harmless no-op on an existing file."""
    conn = db.get_cycle(cycle_row["academic_year"], cycle_row["code"])
    path = os.path.join(Config.BASE_DIR, "schema_cycle.sql")
    with open(path, "r", encoding="utf-8") as fh:
        conn.executescript(fh.read())
    conn.commit()
    return conn


# ----------------------------------------------------------------------------
# _waiting_on(state) -> (bucket_key, human) — translate an ATR state into "who
# has not acted yet", the exact question the professor wants to filter on.
# ----------------------------------------------------------------------------
def _waiting_on(state):
    return {
        atr_workflow.STATE_EXPECTED:     ("faculty", "Faculty — not submitted"),
        atr_workflow.STATE_DRAFT:        ("faculty", "Faculty — draft, not submitted"),
        atr_workflow.STATE_PENDING_HOD:  ("hod",     "HOD — not acted"),
        atr_workflow.STATE_PENDING_VD:   ("vd",      "Vice-Dean — not acted"),
        atr_workflow.STATE_PENDING_DEAN: ("dean",    "Dean — not acted"),
        atr_workflow.STATE_CLOSED:       ("done",    "Closed"),
    }.get(state, ("faculty", "Faculty — not submitted"))


# ----------------------------------------------------------------------------
# _hod_by_dept(master) -> dict dept_code -> {name, email}
# The seated HOD for each department, from the Module 1 org tree, so the board can
# name the person a pending ATR is waiting on (not just "the HOD").
# ----------------------------------------------------------------------------
def _hod_by_dept(master):
    rows = master.execute(
        "SELECT d.code AS code, u.name AS name, u.email AS email "
        "FROM department d LEFT JOIN app_user u ON u.id = d.hod_user_id").fetchall()
    return {r["code"]: {"name": r["name"], "email": r["email"]} for r in rows}


# ============================================================================
# GET /admin/status  —  the status / tracking board.
# ============================================================================
@admin_bp.route("/status")
def status_page():
    conn = get_master()

    # Cycle picker (archived cycles excluded, like every other operational page).
    cycles = conn.execute(
        "SELECT * FROM cycle WHERE status != 'ARCHIVED' ORDER BY id").fetchall()
    cycle_code = (request.args.get("cycle", "").strip()
                  or (cycles[0]["code"] if cycles else ""))
    c = conn.execute("SELECT * FROM cycle WHERE code = ?", (cycle_code,)).fetchone()
    if c is None:
        conn.close()
        flash("No cycle selected.", "error")
        return redirect(url_for("admin.cycles_list"))

    # ---- Filters (all optional; combine with AND) --------------------------
    f_dept = request.args.get("dept", "").strip().upper()
    f_state = request.args.get("state", "").strip().upper()
    f_wait = request.args.get("waiting", "").strip().lower()   # faculty/hod/vd/dean/done
    f_q = request.args.get("q", "").strip().lower()

    # ---- 1. Participation summary (from the per-cycle token table) ----------
    part = {"tokens": 0, "completed": 0, "pending": 0, "has_db": False}
    atr_by_off, submit_at = {}, {}
    path = db.cycle_db_path(c["academic_year"], c["code"])
    if os.path.exists(path):
        cy = _open_cycle_db(c)
        part["has_db"] = True
        part["tokens"] = cy.execute("SELECT COUNT(*) n FROM token").fetchone()["n"]
        part["completed"] = cy.execute(
            "SELECT COUNT(*) n FROM token WHERE completed_all=1").fetchone()["n"]
        part["pending"] = part["tokens"] - part["completed"]
        # ATR state per offering, and the earliest SUBMIT time (when the faculty filed).
        for a in cy.execute("SELECT * FROM atr").fetchall():
            atr_by_off[a["offering_id"]] = a
        for e in cy.execute(
                "SELECT atr_id, MIN(at) AS filed FROM atr_event "
                "WHERE action='SUBMIT' GROUP BY atr_id").fetchall():
            submit_at[e["atr_id"]] = e["filed"]
        cy.close()

    # ---- 2. ATR tracking rows (one per POOR course) ------------------------
    poor = conn.execute(
        "SELECT o.*, oc.band, oc.overall_score, oc.n_responses "
        "FROM offering o "
        "JOIN offering_classification oc "
        "  ON oc.offering_id = o.id AND oc.cycle_code = o.cycle_code "
        "WHERE o.cycle_code = ? AND oc.band = 'POOR' "
        "ORDER BY o.dept_code, o.course_code", (c["code"],)).fetchall()

    hod_map = _hod_by_dept(conn)

    rows, tally = [], {"total": 0, "not_submitted": 0, "hod": 0, "vd": 0,
                       "dean": 0, "done": 0}
    dept_set = set()
    for o in poor:
        atr = atr_by_off.get(o["id"])
        state = atr["state"] if atr else atr_workflow.STATE_EXPECTED
        bucket, wait_human = _waiting_on(state)
        eff_dept = rbac.effective_dept(conn, o) or o["dept_code"]
        hod = hod_map.get(eff_dept, {"name": None, "email": None})
        filed = submit_at.get(atr["id"]) if atr else None
        submitted = state not in (atr_workflow.STATE_EXPECTED, atr_workflow.STATE_DRAFT)

        # Roll the summary tiles over the FULL POOR set (before row filtering).
        tally["total"] += 1
        if bucket == "faculty":
            tally["not_submitted"] += 1
        elif bucket in ("hod", "vd", "dean", "done"):
            tally[bucket] += 1
        dept_set.add(eff_dept)

        rec = {"offering": o, "state": state, "bucket": bucket,
               "waiting": wait_human, "submitted": submitted, "filed": filed,
               "eff_dept": eff_dept, "hod": hod,
               "atr_id": atr["id"] if atr else None,
               "updated": atr["updated_at"] if atr else None}

        # ---- apply the filters ----
        if f_dept and eff_dept != f_dept:
            continue
        if f_state and state != f_state:
            continue
        if f_wait and bucket != f_wait:
            continue
        if f_q:
            hay = " ".join(str(x or "").lower() for x in
                           (o["faculty"], o["faculty_email"], o["faculty_id"],
                            o["course_code"], o["course_name"]))
            if f_q not in hay:
                continue
        rows.append(rec)

    conn.close()

    return render_template(
        "status.html", cycle=c, cycles=cycles, rows=rows, tally=tally,
        part=part, states=atr_workflow.ALL_STATES,
        depts=sorted(dept_set),
        f_dept=f_dept, f_state=f_state, f_wait=f_wait, f_q=f_q)
