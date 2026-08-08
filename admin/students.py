# ============================================================================
# admin/students.py  —  Roster upload + per-cycle student list + exclusions
#                       (spec v3 §7.1, §7.7)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# In v3 the `students` table is the PER-CYCLE roster, not a permanent master.
# These routes are the "for whom" front door:
#   * download the roster template (one canonical version, §7),
#   * upload it — a DRY RUN first (parse+validate+reconcile), then an explicit
#     Confirm that commits the expanded students for the cycle (§7.1),
#   * list/search the cycle roster,
#   * EXCLUDE a student mid-cycle (TC / withdrawal) or a bulk paste of them, and
#     UNDO an exclusion — the §7.7 mechanics: cancel their email, invalidate
#     their outstanding token, drop them from expected counts, while KEEPING any
#     feedback they already gave (it is anonymous and cannot be identified).
#
# There is no academic-year switch and no graduation close-out any more — v3
# deleted year derivation (§2.1). All DB access here is master.db (identity),
# except the token invalidation on exclusion, which must reach the per-cycle db.
# ----------------------------------------------------------------------------

import os
from flask import render_template, request, redirect, url_for, flash, send_file

import db
from db import get_master
from config import Config
from admin import admin_bp
import student_importer


# Where uploaded roster files are staged so a dry-run and its later Confirm both
# read the same bytes (the Confirm posts back the staged filename).
_TMP_DIR = os.path.join(Config.BASE_DIR, "uploads_tmp")


def _cycle_by_code(conn, code):
    return conn.execute("SELECT * FROM cycle WHERE code=?", (code,)).fetchone()


def _default_cycle_code(conn):
    row = conn.execute(
        "SELECT code FROM cycle WHERE status != 'ARCHIVED' "
        "ORDER BY id LIMIT 1").fetchone()
    return row["code"] if row else "CA1"


# ----------------------------------------------------------------------------
# GET /admin/students — list the roster for one cycle (chosen by ?cycle=CODE).
# Filters: q (reg search), dept (programme code), year, status.
# ----------------------------------------------------------------------------
@admin_bp.route("/students")
def students_list():
    conn = get_master()
    cycle_code = request.args.get("cycle", "").strip() or _default_cycle_code(conn)

    q = request.args.get("q", "").strip()
    dept = request.args.get("dept", "").strip()
    year = request.args.get("year", "").strip()
    status = request.args.get("status", "").strip()

    clauses, params = ["cycle_code = ?"], [cycle_code]
    if q:
        clauses.append("reg_no LIKE ?"); params.append(f"%{q}%")
    if dept:
        clauses.append("dept_code = ?"); params.append(dept)
    if year:
        clauses.append("year_of_study = ?"); params.append(year)
    if status:
        clauses.append("status = ?"); params.append(status)
    where = "WHERE " + " AND ".join(clauses)

    rows = conn.execute(
        f"SELECT * FROM students {where} "
        f"ORDER BY dept_code, year_of_study, section, reg_no", params).fetchall()

    depts = conn.execute(
        "SELECT DISTINCT dept_code FROM students WHERE cycle_code=? ORDER BY dept_code",
        (cycle_code,)).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) n FROM students WHERE cycle_code=? AND status='active'",
        (cycle_code,)).fetchone()["n"]
    excluded = conn.execute(
        "SELECT COUNT(*) n FROM students WHERE cycle_code=? AND status='excluded'",
        (cycle_code,)).fetchone()["n"]
    # Archived cycles are excluded from the students-page cycle picker.
    cycles = conn.execute(
        "SELECT code, label FROM cycle WHERE status != 'ARCHIVED' "
        "ORDER BY id").fetchall()
    conn.close()
    return render_template("students_list.html",
                           students=rows, depts=depts, total=total,
                           excluded=excluded, cycle_code=cycle_code, cycles=cycles,
                           f_q=q, f_dept=dept, f_year=year, f_status=status,
                           dept_names=Config.DEPT_CODES)


# ----------------------------------------------------------------------------
# GET /admin/students/template?cycle=CODE — download the roster template (§7).
# ----------------------------------------------------------------------------
@admin_bp.route("/students/template")
def students_template():
    conn = get_master()
    cycle_code = request.args.get("cycle", "").strip() or _default_cycle_code(conn)
    c = _cycle_by_code(conn, cycle_code)
    label = f"{c['academic_year']} {cycle_code}" if c else cycle_code
    conn.close()
    os.makedirs(_TMP_DIR, exist_ok=True)
    path = os.path.join(_TMP_DIR, f"studentRoster_{cycle_code}.xlsx")
    student_importer.build_roster_template(path, label)
    return send_file(path, as_attachment=True,
                     download_name=f"studentRoster_{cycle_code}.xlsx")


# ----------------------------------------------------------------------------
# GET/POST /admin/students/upload — roster upload with a dry-run gate (§7.1).
# POST without 'confirm' = DRY RUN (stages the file, shows reconciliation).
# POST with 'confirm' + 'staged' = COMMIT the previously-staged file.
# ----------------------------------------------------------------------------
@admin_bp.route("/students/upload", methods=["GET", "POST"])
def students_upload():
    conn = get_master()
    cycle_code = (request.values.get("cycle", "").strip()
                  or _default_cycle_code(conn))

    if request.method == "POST":
        os.makedirs(_TMP_DIR, exist_ok=True)
        confirm = request.form.get("confirm") == "yes"

        if confirm:
            # COMMIT step — read the staged file the dry run validated.
            staged = request.form.get("staged", "")
            path = os.path.join(_TMP_DIR, os.path.basename(staged))
            if not staged or not os.path.exists(path):
                conn.close()
                flash("Staged file missing — please re-upload.", "error")
                return redirect(url_for("admin.students_upload", cycle=cycle_code))
            summary = student_importer.import_roster(path, conn, cycle_code, commit=True)
            conn.close()
            flash(f"Roster committed: {summary['inserted']} students for {cycle_code}.",
                  "success")
            return redirect(url_for("admin.students_list", cycle=cycle_code))

        # DRY RUN step — stage the upload, validate, show reconciliation.
        file = request.files.get("file")
        if not file or file.filename == "":
            conn.close()
            flash("No file selected.", "error")
            return redirect(url_for("admin.students_upload", cycle=cycle_code))
        staged_name = f"roster_{cycle_code}_{os.path.basename(file.filename)}"
        path = os.path.join(_TMP_DIR, staged_name)
        file.save(path)
        try:
            summary = student_importer.import_roster(path, conn, cycle_code, commit=False)
        except Exception as e:
            conn.close()
            flash(f"Could not read the workbook: {e}", "error")
            return redirect(url_for("admin.students_upload", cycle=cycle_code))
        conn.close()
        return render_template("students_upload.html", summary=summary,
                               cycle_code=cycle_code, staged=staged_name)

    conn.close()
    return render_template("students_upload.html", summary=None,
                           cycle_code=cycle_code, staged=None)


# ----------------------------------------------------------------------------
# POST /admin/students/exclude — exclude one or many students (§7.7). Accepts a
# single reg_no or a textarea 'bulk' of many. Effects: status='excluded' (+audit
# reason/date), and the outstanding token is invalidated so the delivered link
# stops working. Feedback already submitted is kept (anonymous, unidentifiable).
# ----------------------------------------------------------------------------
@admin_bp.route("/students/exclude", methods=["POST"])
def students_exclude():
    conn = get_master()
    cycle_code = request.form.get("cycle", "").strip() or _default_cycle_code(conn)
    reason = request.form.get("reason", "TC").strip()
    effective = request.form.get("effective", "").strip() or None

    regs = []
    single = request.form.get("reg_no", "").strip()
    if single:
        regs.append(single.upper())
    bulk = request.form.get("bulk", "")
    for token in bulk.replace(",", " ").split():
        regs.append(token.strip().upper())
    regs = [r for r in dict.fromkeys(regs) if r]   # de-dupe, keep order

    n = 0
    for reg in regs:
        cur = conn.execute(
            "UPDATE students SET status='excluded', excl_reason=?, excl_effective=?, "
            "excl_by='admin', excl_at=datetime('now') "
            "WHERE reg_no=? AND cycle_code=?",
            (reason, effective, reg, cycle_code))
        if cur.rowcount:
            n += 1
    conn.commit()

    # Invalidate outstanding tokens in the cycle db so delivered links die (§7.7).
    c = _cycle_by_code(conn, cycle_code)
    killed = 0
    if c and regs:
        path = db.cycle_db_path(c["academic_year"], c["code"])
        if os.path.exists(path):
            cy = db.get_cycle(c["academic_year"], c["code"])
            for reg in regs:
                cur = cy.execute("DELETE FROM token WHERE reg_no=?", (reg,))
                killed += cur.rowcount
            cy.commit(); cy.close()
    conn.close()
    flash(f"Excluded {n} student(s); {killed} outstanding link(s) invalidated. "
          f"Any feedback already submitted is kept (anonymous).", "success")
    return redirect(url_for("admin.students_list", cycle=cycle_code, status="excluded"))


# ----------------------------------------------------------------------------
# POST /admin/students/include — UNDO an exclusion (spec §7.7: reversible).
# Restores status='active' and clears the audit reason. A fresh token can be
# re-issued from the Tokens page afterwards.
# ----------------------------------------------------------------------------
@admin_bp.route("/students/include", methods=["POST"])
def students_include():
    conn = get_master()
    cycle_code = request.form.get("cycle", "").strip() or _default_cycle_code(conn)
    reg = request.form.get("reg_no", "").strip().upper()
    conn.execute(
        "UPDATE students SET status='active', excl_reason=NULL, excl_effective=NULL, "
        "excl_by=NULL, excl_at=NULL WHERE reg_no=? AND cycle_code=?",
        (reg, cycle_code))
    conn.commit(); conn.close()
    flash(f"Re-included {reg}. Re-issue their token from the Tokens page if needed.",
          "success")
    return redirect(url_for("admin.students_list", cycle=cycle_code))


# ----------------------------------------------------------------------------
# POST /admin/students/seed-demo — TEST DATA button. Synthesises a small roster
# for every cohort in the cycle's allocation so the flow can be demonstrated
# without a real roster file. Demo reg numbers are prefixed 'DEMO'.
# ----------------------------------------------------------------------------
@admin_bp.route("/students/seed-demo", methods=["POST"])
def students_seed_demo():
    conn = get_master()
    cycle_code = request.form.get("cycle", "").strip() or _default_cycle_code(conn)
    made = student_importer.generate_demo_roster(conn, cycle_code, per_group=6)
    conn.close()
    flash(f"Seeded {made} demo student(s) for {cycle_code} (prefixed 'DEMO'). "
          f"Testing only.", "success")
    return redirect(url_for("admin.students_list", cycle=cycle_code))
