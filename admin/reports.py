# ============================================================================
# admin/reports.py  —  Generate + download the per-offering / per-batch reports
#                      (spec Section 6.8 & 11)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# This is the admin front door to Night 3's analysis stack:
#     scoring.py        -> the exact approved numbers
#     report_export.py  -> those numbers as Excel + print-ready PDF
#
# The screens here let the professor:
#   * pick a cycle and see which offerings have collected responses,
#   * download ONE offering's report as Excel or PDF, and
#   * BULK-generate a whole batch (programme / dept / year) as a .zip of Excel
#     files or a single combined multi-page PDF (spec Section 11).
#
# ANONYMITY BOUNDARY (spec Section 5): reporting reads Group C config + Group A
# offering identity from master.db and the anonymous Group B answers from the
# per-cycle db. It NEVER opens the token table — a report is about a COURSE, so
# no student can be linked to any answer even while a report is produced.
# ----------------------------------------------------------------------------

import os
import io
import tempfile
from datetime import datetime

from flask import (render_template, request, redirect, url_for, flash,
                   send_file, abort)

import db
from db import get_master
from config import Config
from admin import admin_bp
import scoring
import report_export
import services  # noqa: F401  (imported by scoring's adapter; keep available)


# ----------------------------------------------------------------------------
# _open_cycle_db(cycle_row) — open a per-cycle db, ensuring its schema exists.
# (A local copy of the helper in cycles.py so this module stands alone; both are
# harmless no-ops on an existing file because the DDL is CREATE ... IF NOT EXISTS.)
# ----------------------------------------------------------------------------
def _open_cycle_db(cycle_row):
    conn = db.get_cycle(cycle_row["academic_year"], cycle_row["code"])
    schema_path = os.path.join(Config.BASE_DIR, "schema_cycle.sql")
    with open(schema_path, "r", encoding="utf-8") as fh:
        conn.executescript(fh.read())
    conn.commit()
    return conn


# ----------------------------------------------------------------------------
# _offerings_with_responses(master, cycle) — the list of offerings that have at
# least one submitted response in this cycle (only these can be reported). We
# read the DISTINCT offering_ids from Group B, then fetch their identity rows
# from master.db so the screen shows human course/faculty names.
# ----------------------------------------------------------------------------
def _offerings_with_responses(master, cycle):
    rows = cycle.execute(
        "SELECT offering_id, COUNT(*) AS n FROM response GROUP BY offering_id"
    ).fetchall()
    counts = {r["offering_id"]: r["n"] for r in rows}
    if not counts:
        return []
    # Fetch identities in one query using the collected ids.
    placeholders = ",".join("?" * len(counts))
    offerings = master.execute(
        f"""
        SELECT o.*, c.name AS category_name, c.report_key AS report_key
        FROM offering o LEFT JOIN category c ON c.id = o.category_id
        WHERE o.id IN ({placeholders})
        ORDER BY o.dept_code, o.year_of_study, o.course_code
        """,
        list(counts.keys()),
    ).fetchall()
    return [{"offering": o, "n_responses": counts[o["id"]]} for o in offerings]


# ============================================================================
# GET /admin/reports  —  the reports landing page (choose cycle -> see offerings)
# ============================================================================
@admin_bp.route("/reports")
def reports_home():
    master = get_master()
    # Hide archived cycles from the report picker (visible only on Cycles page).
    cycles = master.execute(
        "SELECT * FROM cycle WHERE status != 'ARCHIVED' "
        "ORDER BY academic_year, code").fetchall()

    # Which cycle is selected? Default to the first that has a db file.
    cycle_id = request.args.get("cycle_id", "").strip()
    selected = None
    if cycle_id:
        selected = master.execute(
            "SELECT * FROM cycle WHERE id=?", (cycle_id,)).fetchone()
    if selected is None and cycles:
        # Auto-pick the first cycle whose per-cycle db exists.
        for c in cycles:
            if os.path.exists(db.cycle_db_path(c["academic_year"], c["code"])):
                selected = c
                break
        if selected is None:
            selected = cycles[0]

    items = []
    programmes = depts = years = []
    if selected is not None and os.path.exists(
            db.cycle_db_path(selected["academic_year"], selected["code"])):
        cy = _open_cycle_db(selected)
        items = _offerings_with_responses(master, cy)
        cy.close()
        # Batch-filter dropdown values, taken from the reportable offerings.
        programmes = sorted({it["offering"]["programme"] for it in items})
        depts = sorted({it["offering"]["dept_code"] for it in items})
        years = sorted({it["offering"]["year_of_study"] for it in items})

    master.close()
    return render_template("reports.html", cycles=cycles, selected=selected,
                           items=items, programmes=programmes, depts=depts,
                           years=years, dept_names=Config.DEPT_CODES)


# ============================================================================
# GET /admin/reports/<cycle_id>/<offering_id>.<fmt>  —  ONE offering's report
# fmt is 'xlsx' or 'pdf'. We score the offering and stream the file back.
# ============================================================================
@admin_bp.route("/reports/<int:cycle_id>/<int:offering_id>.<fmt>")
def report_one(cycle_id, offering_id, fmt):
    if fmt not in ("xlsx", "pdf"):
        abort(404)
    master = get_master()
    cyc = master.execute("SELECT * FROM cycle WHERE id=?", (cycle_id,)).fetchone()
    if cyc is None:
        master.close(); abort(404)
    cy = _open_cycle_db(cyc)

    # The configurable "Discussed Late" weight (spec Open Item 14.2) is read once
    # and passed in, so the report matches whatever the professor has set.
    dl_weight = scoring.get_discussed_late_weight(master)
    result = scoring.score_offering(master, cy, offering_id, dl_weight)
    cy.close(); master.close()
    # Test cycles stamp a diagonal "TEST DATA" watermark on the PDF (spec §9.1).
    if result is not None and cyc["is_test"]:
        result["watermark"] = "TEST DATA — NOT FOR CIRCULATION"
    if result is None:
        flash("Nothing to report for that offering (uncategorised or no responses).",
              "error")
        return redirect(url_for("admin.reports_home", cycle_id=cycle_id))

    # Render to a temp file then stream it. (Temp file, not BytesIO, so openpyxl
    # and reportlab can both write by path; we clean it after send.)
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
    # send_file streams the bytes; we read into memory first so we can delete the
    # temp file immediately (avoids leaving temp files on the laptop).
    with open(tmp.name, "rb") as fh:
        data = fh.read()
    os.unlink(tmp.name)
    # ACTIVITY LOG (Module 3): record WHICH report was downloaded, in which format,
    # for which cycle — so "who exported what" is answerable at a glance. This is a
    # GET, allowed through by activity_log.SENSITIVE_GET; the hook writes the row.
    import activity_log
    activity_log.note(detail=f"{download_name} ({fmt.upper()})",
                      cycle_code=cyc["code"], target_type="offering",
                      target_id=offering_id)
    return send_file(io.BytesIO(data), mimetype=mimetype,
                     as_attachment=True, download_name=download_name)


# ============================================================================
# GET /admin/reports/<cycle_id>/bulk.<fmt>  —  BULK per-batch generation
# Query params: programme, dept, year (any subset) filter which reportable
# offerings are included. fmt 'zip' => a .zip of Excel files; 'pdf' => one
# combined multi-page PDF (spec Section 11 "bulk generation for a whole batch").
# ============================================================================
@admin_bp.route("/reports/<int:cycle_id>/bulk.<fmt>")
def report_bulk(cycle_id, fmt):
    if fmt not in ("zip", "pdf"):
        abort(404)
    master = get_master()
    cyc = master.execute("SELECT * FROM cycle WHERE id=?", (cycle_id,)).fetchone()
    if cyc is None:
        master.close(); abort(404)
    cy = _open_cycle_db(cyc)

    programme = request.args.get("programme", "").strip()
    dept = request.args.get("dept", "").strip()
    year = request.args.get("year", "").strip()

    items = _offerings_with_responses(master, cy)
    # Apply the optional batch filters.
    def keep(o):
        if programme and o["programme"] != programme:
            return False
        if dept and o["dept_code"] != dept:
            return False
        if year and str(o["year_of_study"]) != year:
            return False
        return True
    items = [it for it in items if keep(it["offering"])]

    dl_weight = scoring.get_discussed_late_weight(master)
    results = []
    for it in items:
        res = scoring.score_offering(master, cy, it["offering"]["id"], dl_weight)
        if res is not None and cyc["is_test"]:
            res["watermark"] = "TEST DATA — NOT FOR CIRCULATION"
        if res is not None:
            results.append(res)
    cy.close(); master.close()

    if not results:
        flash("No reportable offerings match that batch filter.", "error")
        return redirect(url_for("admin.reports_home", cycle_id=cycle_id))

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    if fmt == "zip":
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip"); tmp.close()
        report_export.build_batch_excel_zip(results, tmp.name)
        mimetype = "application/zip"
        name = f"feedback_reports_{cyc['code']}_{stamp}.zip"
    else:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf"); tmp.close()
        report_export.build_batch_pdf(results, tmp.name)
        mimetype = "application/pdf"
        name = f"feedback_reports_{cyc['code']}_{stamp}.pdf"

    with open(tmp.name, "rb") as fh:
        data = fh.read()
    os.unlink(tmp.name)
    # ACTIVITY LOG (Module 3): record the batch export — how many reports, which
    # filter, which format — so a bulk download reads clearly in the audit trail.
    import activity_log
    scope = " · ".join(p for p in (programme, dept, year) if p) or "all"
    activity_log.note(detail=f"{len(results)} report(s) [{scope}] as {fmt.upper()}",
                      cycle_code=cyc["code"], target_type="cycle", target_id=cycle_id)
    return send_file(io.BytesIO(data), mimetype=mimetype,
                     as_attachment=True, download_name=name)
