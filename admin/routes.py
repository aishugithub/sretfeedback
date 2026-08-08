# ============================================================================
# admin/routes.py  —  Dashboard + the offering roster screens (spec Section 6)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# This module holds the admin HOME (dashboard) and the offering roster view/edit
# pages from Night 1. It binds its routes to the shared `admin_bp` created in
# admin/__init__.py (the split-blueprint pattern; see that file). All DB access
# is master.db only, so the anonymity two-file split is respected here.
# ----------------------------------------------------------------------------

from flask import (render_template, request, redirect, url_for, flash)

from db import get_master
from config import Config
from admin import admin_bp


# ----------------------------------------------------------------------------
# GET /admin/  — the admin DASHBOARD: one landing page linking to every
# Section-6 control, with a live health summary.
# ----------------------------------------------------------------------------
@admin_bp.route("/")
def dashboard():
    conn = get_master()

    # The health metrics count cycle-scoped data (offerings + students both carry
    # a cycle_code), so the dashboard now shows ONE cycle's numbers, chosen from a
    # dropdown (?cycle=CODE) and defaulting to the first cycle — consistent with
    # the Offerings and Students pages. Without this the tiles summed every cycle
    # together, which was misleading once more than one cycle had data.
    # Archived cycles are hidden from every operational dropdown (the point of
    # archiving); they remain visible only on the Cycles page and the audit log.
    cycles = conn.execute(
        "SELECT * FROM cycle WHERE status != 'ARCHIVED' ORDER BY id").fetchall()
    cycle_code = (request.args.get("cycle", "").strip()
                  or (cycles[0]["code"] if cycles else "CA1"))

    n_offerings = conn.execute(
        "SELECT COUNT(*) n FROM offering WHERE cycle_code=?",
        (cycle_code,)).fetchone()["n"]
    n_uncat = conn.execute(
        "SELECT COUNT(*) n FROM offering WHERE cycle_code=? AND category_id IS NULL",
        (cycle_code,)).fetchone()["n"]
    n_students = conn.execute(
        "SELECT COUNT(*) n FROM students WHERE cycle_code=?",
        (cycle_code,)).fetchone()["n"]
    n_active = conn.execute(
        "SELECT COUNT(*) n FROM students WHERE cycle_code=? AND status='active'",
        (cycle_code,)).fetchone()["n"]
    ay = conn.execute(
        "SELECT * FROM academic_year WHERE is_active=1 LIMIT 1").fetchone()
    conn.close()
    return render_template("dashboard.html",
                           n_offerings=n_offerings, n_uncat=n_uncat,
                           n_students=n_students, n_active=n_active,
                           cycles=cycles, ay=ay, cycle_code=cycle_code)


# ----------------------------------------------------------------------------
# GET /admin/roster — list every offering, with simple filters. LEFT JOIN
# category so the human category name shows (blank when auto-detection could not
# decide). All filters are parameterised (safe from SQL injection).
# ----------------------------------------------------------------------------
@admin_bp.route("/roster")
def roster_list():
    conn = get_master()

    # Offerings are CYCLE-SCOPED (offering.cycle_code), exactly like the student
    # roster and the allocation upload. Previously this page had NO cycle filter,
    # so it listed every cycle's offerings jumbled together. We now scope to one
    # cycle chosen from a dropdown (?cycle=CODE), defaulting to the first cycle —
    # mirroring the Students page — so what you see always belongs to one cycle.
    # Archived cycles are hidden from every operational dropdown (the point of
    # archiving); they remain visible only on the Cycles page and the audit log.
    cycles = conn.execute(
        "SELECT * FROM cycle WHERE status != 'ARCHIVED' ORDER BY id").fetchall()
    cycle_code = (request.args.get("cycle", "").strip()
                  or (cycles[0]["code"] if cycles else "CA1"))

    dept = request.args.get("dept", "").strip()
    year = request.args.get("year", "").strip()
    programme = request.args.get("programme", "").strip()

    # cycle_code is ALWAYS part of the filter; dept/year/programme are optional.
    clauses, params = ["o.cycle_code = ?"], [cycle_code]
    if dept:
        clauses.append("o.dept_code = ?"); params.append(dept)
    if year:
        clauses.append("o.year_of_study = ?"); params.append(year)
    if programme:
        clauses.append("o.programme = ?"); params.append(programme)
    where = "WHERE " + " AND ".join(clauses)

    rows = conn.execute(
        f"""
        SELECT o.*, c.name AS category_name
        FROM offering o
        LEFT JOIN category c ON c.id = o.category_id
        {where}
        ORDER BY o.programme, o.year_of_study, o.dept_code, o.section, o.course_code
        """,
        params,
    ).fetchall()

    # The dept/year/programme dropdowns are scoped to THIS cycle, so they only
    # offer choices that actually exist in the cycle being viewed.
    depts = conn.execute(
        "SELECT DISTINCT dept_code FROM offering WHERE cycle_code=? ORDER BY dept_code",
        (cycle_code,)).fetchall()
    years = conn.execute(
        "SELECT DISTINCT year_of_study FROM offering WHERE cycle_code=? ORDER BY year_of_study",
        (cycle_code,)).fetchall()
    programmes = conn.execute(
        "SELECT DISTINCT programme FROM offering WHERE cycle_code=? ORDER BY programme",
        (cycle_code,)).fetchall()

    # Counts are cycle-scoped too, so "N offerings" and "need a category" describe
    # only the selected cycle rather than every cycle at once.
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM offering WHERE cycle_code=?",
        (cycle_code,)).fetchone()["n"]
    uncategorised = conn.execute(
        "SELECT COUNT(*) AS n FROM offering WHERE cycle_code=? AND category_id IS NULL",
        (cycle_code,)).fetchone()["n"]
    conn.close()

    return render_template(
        "roster_list.html",
        rows=rows, depts=depts, years=years, programmes=programmes,
        f_dept=dept, f_year=year, f_programme=programme,
        total=total, uncategorised=uncategorised,
        dept_names=Config.DEPT_CODES,
        cycles=cycles, cycle_code=cycle_code,
    )


# ----------------------------------------------------------------------------
# GET/POST /admin/roster/<offering_id>/edit — edit one offering.
# ----------------------------------------------------------------------------
@admin_bp.route("/roster/<int:offering_id>/edit", methods=["GET", "POST"])
def roster_edit(offering_id: int):
    conn = get_master()
    categories = conn.execute(
        "SELECT id, name FROM category ORDER BY id").fetchall()

    if request.method == "POST":
        course_code = request.form.get("course_code", "").strip()
        course_name = request.form.get("course_name", "").strip()
        faculty = request.form.get("faculty", "").strip()
        section = request.form.get("section", "").strip() or None
        semester = request.form.get("semester", "").strip()
        category_id = request.form.get("category_id", "").strip()

        category_id_val = int(category_id) if category_id else None
        semester_val = int(semester) if semester.isdigit() else None

        # Remember which cycle this offering belongs to so we can return the admin
        # to the SAME cycle view after saving (the list is cycle-scoped now).
        row = conn.execute(
            "SELECT cycle_code FROM offering WHERE id=?", (offering_id,)).fetchone()
        back_cycle = row["cycle_code"] if row else None

        conn.execute(
            "UPDATE offering SET course_code=?, course_name=?, faculty=?, "
            "section=?, semester=?, category_id=? WHERE id=?",
            (course_code, course_name, faculty, section, semester_val,
             category_id_val, offering_id),
        )
        conn.commit()
        conn.close()
        flash("Offering updated.", "success")
        return redirect(url_for("admin.roster_list", cycle=back_cycle))

    offering = conn.execute(
        "SELECT * FROM offering WHERE id=?", (offering_id,)).fetchone()
    conn.close()
    if offering is None:
        flash("Offering not found.", "error")
        return redirect(url_for("admin.roster_list"))

    return render_template("roster_edit.html",
                           offering=offering, categories=categories)


# ----------------------------------------------------------------------------
# GET /admin/activity — the ACTIVITY LOG viewer (Version 2.0 · Module 3).
# ----------------------------------------------------------------------------
# A read-only window onto master.db.activity_log: "who did what, when, where" across
# the whole system. This route does NO writing — it only reads and displays — and
# lives in the admin blueprint so it sits behind the same admin surface as every
# other control-panel page. The four filters (actor, action, cycle, day) are passed
# straight to activity_log.recent(), which parameterises them (no injection surface).
# The list is capped (newest first) so the page stays fast after months of activity.
@admin_bp.route("/activity")
def activity_log_view():
    import activity_log                       # imported here to avoid any import cycle
    conn = get_master()

    # Read the current filter choices from the query string (all optional). Empty
    # strings become None so recent() treats them as "no filter".
    f_actor  = request.args.get("actor", "").strip() or None
    f_action = request.args.get("action", "").strip() or None
    f_cycle  = request.args.get("cycle", "").strip() or None
    f_day    = request.args.get("day", "").strip() or None

    rows = activity_log.recent(conn, actor=f_actor, action=f_action,
                               cycle=f_cycle, day=f_day, limit=300)

    # Dropdown choices are built from values that actually occur, so the professor
    # filters by real actions/actors rather than guessing.
    actions = activity_log.distinct_actions(conn)
    actors  = activity_log.distinct_actors(conn)
    cycles  = conn.execute("SELECT code FROM cycle ORDER BY id").fetchall()
    summary = activity_log.summary(conn)        # the at-a-glance header numbers
    conn.close()

    return render_template("activity_log.html",
                           rows=rows, actions=actions, actors=actors,
                           cycles=cycles, summary=summary,
                           f_actor=f_actor, f_action=f_action,
                           f_cycle=f_cycle, f_day=f_day)
