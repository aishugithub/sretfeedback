# ============================================================================
# admin/uploads.py  —  Allocation + enrollment uploads and the Readiness gate
#                      (spec v3 §7.2, §7.3, §8)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# The roster ("for whom") is handled in admin/students.py. This module handles
# the rest of the pre-cycle pipeline:
#   * §7.2  the rigid COURSE ALLOCATION upload (dry-run -> confirm),
#   * §7.3  the ELECTIVE ENROLLMENT upload (pick the faculty group, then upload),
#   * §8    the READINESS CHECK page — the hard gate that must pass before a
#           cycle can open, with reasoned dismissals and a CSV export.
#
# Every upload validates before writing and shows a reconciliation the admin
# confirms. All access is master.db (identity/config), so the anonymity split is
# untouched.
# ----------------------------------------------------------------------------

import os
import io
import csv
from flask import (render_template, request, redirect, url_for, flash,
                   send_file, Response)

from db import get_master
from config import Config
from admin import admin_bp
import allocation_rigid
import enrollment_importer
import readiness

_TMP_DIR = os.path.join(Config.BASE_DIR, "uploads_tmp")


def _default_cycle_code(conn):
    row = conn.execute(
        "SELECT code FROM cycle WHERE status != 'ARCHIVED' "
        "ORDER BY id LIMIT 1").fetchone()
    return row["code"] if row else "CA1"


def _cycle_by_code(conn, code):
    return conn.execute("SELECT * FROM cycle WHERE code=?", (code,)).fetchone()


# ============================================================================
# ALLOCATION UPLOAD  (spec §7.2)
# ============================================================================
@admin_bp.route("/allocation/template")
def allocation_template():
    conn = get_master()
    cycle_code = request.args.get("cycle", "").strip() or _default_cycle_code(conn)
    c = _cycle_by_code(conn, cycle_code)
    label = f"{c['academic_year']} {cycle_code}" if c else cycle_code
    conn.close()
    os.makedirs(_TMP_DIR, exist_ok=True)
    path = os.path.join(_TMP_DIR, f"courseAllocation_{cycle_code}.xlsx")
    allocation_rigid.build_allocation_template(path, label)
    return send_file(path, as_attachment=True,
                     download_name=f"courseAllocation_{cycle_code}.xlsx")


@admin_bp.route("/allocation", methods=["GET", "POST"])
def allocation_upload():
    conn = get_master()
    cycle_code = request.values.get("cycle", "").strip() or _default_cycle_code(conn)

    if request.method == "POST":
        os.makedirs(_TMP_DIR, exist_ok=True)
        if request.form.get("confirm") == "yes":
            staged = request.form.get("staged", "")
            path = os.path.join(_TMP_DIR, os.path.basename(staged))
            if not staged or not os.path.exists(path):
                conn.close(); flash("Staged file missing — re-upload.", "error")
                return redirect(url_for("admin.allocation_upload", cycle=cycle_code))
            summary = allocation_rigid.import_allocation_rigid(path, conn, cycle_code, commit=True)
            conn.close()
            # ACTIVITY LOG (Module 3): record the committed import + its count, so the
            # audit trail distinguishes a real data change from a mere preview.
            import activity_log
            activity_log.note(detail=f"{summary['inserted']} teaching assignments committed",
                              cycle_code=cycle_code, target_type="cycle")
            flash(f"Allocation committed: {summary['inserted']} teaching "
                  f"assignments for {cycle_code}.", "success")
            return redirect(url_for("admin.allocation_upload", cycle=cycle_code))

        file = request.files.get("file")
        if not file or file.filename == "":
            conn.close(); flash("No file selected.", "error")
            return redirect(url_for("admin.allocation_upload", cycle=cycle_code))
        staged_name = f"alloc_{cycle_code}_{os.path.basename(file.filename)}"
        path = os.path.join(_TMP_DIR, staged_name)
        file.save(path)
        try:
            summary = allocation_rigid.import_allocation_rigid(path, conn, cycle_code, commit=False)
        except Exception as e:
            conn.close(); flash(f"Could not read the workbook: {e}", "error")
            return redirect(url_for("admin.allocation_upload", cycle=cycle_code))
        conn.close()
        return render_template("allocation_upload.html", summary=summary,
                               cycle_code=cycle_code, staged=staged_name)

    # Archived cycles are excluded from the upload/readiness pickers.
    cycles = conn.execute(
        "SELECT code, label FROM cycle WHERE status != 'ARCHIVED' "
        "ORDER BY id").fetchall()
    n_offerings = conn.execute(
        "SELECT COUNT(*) n FROM offering WHERE cycle_code=?", (cycle_code,)).fetchone()["n"]
    conn.close()
    return render_template("allocation_upload.html", summary=None,
                           cycle_code=cycle_code, cycles=cycles,
                           n_offerings=n_offerings, staged=None)


# ============================================================================
# ELECTIVE ENROLLMENT UPLOAD  (spec §7.3)
# ============================================================================
@admin_bp.route("/enrollment", methods=["GET", "POST"])
def enrollment_upload():
    conn = get_master()
    cycle_code = request.values.get("cycle", "").strip() or _default_cycle_code(conn)

    if request.method == "POST":
        os.makedirs(_TMP_DIR, exist_ok=True)
        offering_id = int(request.form.get("offering_id", "0") or 0)

        # ---- CONFIRM & COMMIT -------------------------------------------------
        # The dry run passed either pasted TEXT (carried back in the hidden
        # `staged_text` field) or a staged FILE (`staged`). Commit whichever it was.
        if request.form.get("confirm") == "yes":
            staged_text = request.form.get("staged_text", "")
            if staged_text.strip():
                summary = enrollment_importer.import_enrollment_text(
                    staged_text, conn, cycle_code, offering_id, commit=True)
            else:
                staged = request.form.get("staged", "")
                path = os.path.join(_TMP_DIR, os.path.basename(staged))
                if not staged or not os.path.exists(path):
                    conn.close(); flash("Staged data missing — re-enter.", "error")
                    return redirect(url_for("admin.enrollment_upload", cycle=cycle_code))
                summary = enrollment_importer.import_enrollment(
                    path, conn, cycle_code, offering_id, commit=True)
            conn.close()
            # ACTIVITY LOG (Module 3): record the committed enrollment + its count.
            import activity_log
            activity_log.note(detail=f"{summary['inserted']} students enrolled into offering #{offering_id}",
                              cycle_code=cycle_code, target_type="offering",
                              target_id=offering_id)
            flash(f"Enrollment committed: {summary['inserted']} students.", "success")
            return redirect(url_for("admin.enrollment_upload", cycle=cycle_code))

        # ---- DRY RUN (validate before committing) -----------------------------
        if not offering_id:
            conn.close(); flash("Pick a course / faculty group first.", "error")
            return redirect(url_for("admin.enrollment_upload", cycle=cycle_code))

        pasted = (request.form.get("pasted", "") or "").strip()
        file = request.files.get("file")
        if pasted:
            # PASTE path (preferred): validate the pasted register numbers and echo
            # them back in a hidden field so Confirm can commit the exact same set.
            summary = enrollment_importer.import_enrollment_text(
                pasted, conn, cycle_code, offering_id, commit=False)
            electives = _elective_offerings(conn, cycle_code)
            conn.close()
            return render_template("enrollment_upload.html", summary=summary,
                                   cycle_code=cycle_code, electives=electives,
                                   selected=offering_id, staged=None, staged_text=pasted)
        elif file and file.filename:
            # FILE path (still supported as a fallback).
            staged_name = f"enr_{cycle_code}_{offering_id}_{os.path.basename(file.filename)}"
            path = os.path.join(_TMP_DIR, staged_name)
            file.save(path)
            try:
                summary = enrollment_importer.import_enrollment(
                    path, conn, cycle_code, offering_id, commit=False)
            except Exception as e:
                conn.close(); flash(f"Could not read the workbook: {e}", "error")
                return redirect(url_for("admin.enrollment_upload", cycle=cycle_code))
            electives = _elective_offerings(conn, cycle_code)
            conn.close()
            return render_template("enrollment_upload.html", summary=summary,
                                   cycle_code=cycle_code, electives=electives,
                                   selected=offering_id, staged=staged_name, staged_text=None)
        else:
            conn.close()
            flash("Paste the register numbers (or choose a file).", "error")
            return redirect(url_for("admin.enrollment_upload", cycle=cycle_code))

    electives = _elective_offerings(conn, cycle_code)
    # Archived cycles are excluded from the upload/readiness pickers.
    cycles = conn.execute(
        "SELECT code, label FROM cycle WHERE status != 'ARCHIVED' "
        "ORDER BY id").fetchall()
    conn.close()
    return render_template("enrollment_upload.html", summary=None,
                           cycle_code=cycle_code, cycles=cycles,
                           electives=electives, selected=None, staged=None,
                           staged_text=None)


def _elective_offerings(conn, cycle_code):
    """Elective teaching assignments for the dropdown. Includes programme code,
    year and section so the SAME course_code taught by the SAME faculty to two
    different sections is unambiguous (the admin picks the exact offering, never
    a guess). Ordered so the list reads programme -> year -> section -> course."""
    return conn.execute(
        "SELECT o.id, o.course_code, o.course_name, o.elective_basket, o.faculty, "
        "       o.dept_code, o.year_of_study, o.section, "
        "(SELECT COUNT(*) FROM enrollment e WHERE e.offering_id=o.id) AS enrolled "
        "FROM offering o WHERE o.cycle_code=? AND o.course_type='ELECTIVE' "
        "ORDER BY o.dept_code, o.year_of_study, o.section, o.course_code, o.faculty",
        (cycle_code,)).fetchall()


# ============================================================================
# READINESS CHECK  (spec §8) — the gate
# ============================================================================
@admin_bp.route("/readiness", methods=["GET"])
def readiness_page():
    conn = get_master()
    cycle_code = request.args.get("cycle", "").strip() or _default_cycle_code(conn)
    result = readiness.compute(conn, cycle_code)
    readiness.persist_state(conn, cycle_code, result["state"])
    # Archived cycles are excluded from the upload/readiness pickers.
    cycles = conn.execute(
        "SELECT code, label FROM cycle WHERE status != 'ARCHIVED' "
        "ORDER BY id").fetchall()
    conn.close()
    return render_template("readiness.html", r=result, cycle_code=cycle_code,
                           cycles=cycles)


@admin_bp.route("/readiness/dismiss", methods=["POST"])
def readiness_dismiss():
    conn = get_master()
    cycle_code = request.form.get("cycle", "").strip() or _default_cycle_code(conn)
    check_key = request.form.get("check_key", "").strip()
    reason = request.form.get("reason", "").strip()
    if check_key and reason:
        readiness.dismiss(conn, cycle_code, check_key, reason)
        flash(f"Dismissed {check_key} with reason recorded.", "success")
    else:
        flash("A written reason is required to dismiss an item.", "error")
    conn.close()
    return redirect(url_for("admin.readiness_page", cycle=cycle_code))


@admin_bp.route("/readiness/export")
def readiness_export():
    """CSV of the full readiness table for whoever must fix source data (§8.6)."""
    conn = get_master()
    cycle_code = request.args.get("cycle", "").strip() or _default_cycle_code(conn)
    result = readiness.compute(conn, cycle_code)
    conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["course_code", "course_name", "faculty", "programme", "year",
                "section", "type", "found", "expected", "status"])
    for row in result["rows"]:
        w.writerow([row["course_code"], row["course_name"], row["faculty"],
                    row["prog"], row["year"], row["section"], row["type"],
                    row["found"], row["expected"], row["status"]])
    for rev in result["reverse"]:
        w.writerow(["(no course)", "", "", rev["prog"], rev["year"],
                    rev["section"], "REVERSE", 0, rev["students"], "ERROR"])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f"attachment; filename=readiness_{cycle_code}.csv"})
