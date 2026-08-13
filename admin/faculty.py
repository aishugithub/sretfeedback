# ============================================================================
# admin/faculty.py  —  Version 2.0 · Module 5 : the "Manage Faculty" admin screen
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# Module 5 introduced the `faculty` table — the single source of truth for a
# teacher's name, email, phone and HOME DEPARTMENT (which HOD administratively
# owns them, and hence endorses their ATRs). This module is the admin-facing
# page that maintains that roster: add a faculty, edit their details, change the
# department they belong to, deactivate, or remove.
#
# WHY A DEDICATED PAGE (the professor's own reasoning): faculty details — email
# especially, and department, which changes over time — must be editable in ONE
# place, not re-typed into the course-allocation file every cycle. The allocation
# file now only needs emp-no + name; everything else is owned here and joined in
# at send time (distribution / ATR notifications resolve email + endorsing HOD
# from this table via offering.faculty_id = faculty.emp_no).
#
# All DB access is master.db only (faculty is identity/config, never a student
# answer), so the anonymity two-file split is untouched.
# ----------------------------------------------------------------------------

from flask import render_template, request, redirect, url_for, flash

from db import get_master
from config import Config          # DEPT_CODES — the department drop-down source
from admin import admin_bp         # the shared admin blueprint
import activity_log                # system-wide audit note()
import rbac                        # DEPT_EXTERNAL sentinel (external/no-HOD faculty)


# ----------------------------------------------------------------------------
# _dept_label(code) -> str — a friendly "E01 — CSE-AIML" for display; a dash when
# no home department is set yet; a clear label for the EXTERNAL (no-HOD) marker.
# ----------------------------------------------------------------------------
def _dept_label(code):
    if not code:
        return "—"
    if code == rbac.DEPT_EXTERNAL:
        return "External (no HOD)"
    return "%s — %s" % (code, Config.DEPT_CODES.get(code, "?"))


# ----------------------------------------------------------------------------
# _dept_context(master) -> (options, labels, valid)   (Aug 2026 — DB-driven)
# ----------------------------------------------------------------------------
# Build the department drop-down LIVE from the `department` table joined to its
# HOD in app_user, so:
#   * every department currently in the DB appears (E04, SH, EXT, E63 … — no more
#     stale hard-coded list that missed E04), and
#   * each option shows WHO the faculty would report to, e.g.
#     "E05 — CSE-Medical(AIDA)  ·  HOD: Dr. V. Nandakumar".
# Returns:
#   options : list of (code, dropdown_label)  — ordered, for <select> menus
#   labels  : dict  code -> cell_label        — for the roster table cell
#   valid   : set   of codes                  — for form validation
# A department with no HOD seated yet shows "· HOD: not set yet".
# ----------------------------------------------------------------------------
def _dept_context(master):
    rows = master.execute(
        "SELECT d.code, d.name, u.name AS hod_name "
        "FROM department d "
        "LEFT JOIN app_user u ON u.id = d.hod_user_id AND u.status = 'active' "
        "ORDER BY d.code"
    ).fetchall()
    options, labels, valid = [], {}, set()
    for r in rows:
        hod = r["hod_name"] or "not set yet"
        options.append((r["code"], "%s — %s  ·  HOD: %s" % (r["code"], r["name"], hod)))
        labels[r["code"]] = "%s — %s · HOD: %s" % (r["code"], r["name"], hod)
        valid.add(r["code"])
    return options, labels, valid


# ----------------------------------------------------------------------------
# _clean(form) -> (fields_dict, error_or_None)
# ----------------------------------------------------------------------------
# Read + validate the add/edit form. emp_no and name are required; email is
# recommended (a faculty with no email simply can't be mailed until one is set,
# which the list flags). A chosen home department must be a real dept code.
# ----------------------------------------------------------------------------
def _clean(form, valid_codes=None):
    emp_no = (form.get("emp_no", "") or "").strip()
    name   = (form.get("name", "") or "").strip()
    email  = (form.get("email", "") or "").strip().lower()
    phone  = (form.get("phone", "") or "").strip()
    dept   = (form.get("home_dept_code", "") or "").strip().upper()
    status = "inactive" if form.get("inactive") == "on" else "active"

    if not emp_no:
        return None, "Employee number is required (it is the unique key)."
    if not name:
        return None, "Name is required."
    if email and "@" not in email:
        return None, "That email address doesn't look valid."
    # An empty department is allowed (means "Unassigned — not set yet"); a
    # non-empty one must be a LIVE department code (validated against the DB via
    # valid_codes) OR the special EXTERNAL marker (a faculty with deliberately no
    # HOD, reviewed only by the Vice Dean). Falling back to Config.DEPT_CODES keeps
    # old callers working if a set was not supplied.
    allowed = valid_codes if valid_codes is not None else set(Config.DEPT_CODES)
    if dept and dept != rbac.DEPT_EXTERNAL and dept not in allowed:
        return None, "Unknown department code: %s" % dept
    return {"emp_no": emp_no, "name": name, "email": email, "phone": phone,
            "home_dept_code": (dept or None), "status": status}, None


# ============================================================================
# LIST  —  GET /admin/faculty
# ============================================================================
# Show the faculty roster with a search box and a department filter. Each row
# shows how many offerings currently reference that emp_no (a quick sign of
# whether the allocation is linked to this master record yet), whether an email
# is on file, and the home department.
# ----------------------------------------------------------------------------
@admin_bp.route("/faculty")
def faculty_list():
    master = get_master()

    # Optional filters (all parameterised).
    q = (request.args.get("q", "") or "").strip()
    dept = (request.args.get("dept", "") or "").strip().upper()

    clauses, params = [], []
    if q:
        clauses.append("(emp_no LIKE ? OR name LIKE ? OR email LIKE ?)")
        params += ["%%%s%%" % q, "%%%s%%" % q, "%%%s%%" % q]
    if dept:
        clauses.append("home_dept_code = ?"); params.append(dept)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    rows = master.execute(
        "SELECT * FROM faculty %s ORDER BY name COLLATE NOCASE" % where, params
    ).fetchall()

    # Count offerings per emp_no so the admin can see which master records are
    # actually wired to the current allocation (offering.faculty_id = emp_no).
    counts = {r["faculty_id"]: r["n"] for r in master.execute(
        "SELECT faculty_id, COUNT(*) AS n FROM offering "
        "WHERE faculty_id IS NOT NULL AND faculty_id != '' GROUP BY faculty_id"
    ).fetchall()}

    # Totals for the summary line.
    n_total = master.execute("SELECT COUNT(*) n FROM faculty").fetchone()["n"]
    n_no_email = master.execute(
        "SELECT COUNT(*) n FROM faculty WHERE email IS NULL OR email=''").fetchone()["n"]
    n_no_dept = master.execute(
        "SELECT COUNT(*) n FROM faculty WHERE home_dept_code IS NULL").fetchone()["n"]
    # DB-driven department options + labels (with each HOD's name) for the filter
    # drop-down and the "HOD (Reporting to)" column.
    dept_options, dept_labels, _valid = _dept_context(master)
    master.close()

    return render_template(
        "faculty_list.html", faculty=rows, counts=counts,
        dept_options=dept_options, dept_labels=dept_labels,
        q=q, dept=dept, n_total=n_total, n_no_email=n_no_email, n_no_dept=n_no_dept)


# ============================================================================
# ADD  —  GET/POST /admin/faculty/new
# ============================================================================
@admin_bp.route("/faculty/new", methods=["GET", "POST"])
def faculty_new():
    master = get_master()
    dept_options, _labels, valid = _dept_context(master)

    if request.method == "GET":
        master.close()
        return render_template("faculty_edit.html", fac=None,
                               dept_options=dept_options)

    fields, err = _clean(request.form, valid_codes=valid)
    if err:
        master.close()
        flash(err, "error")
        return render_template("faculty_edit.html", fac=request.form,
                               dept_options=dept_options)

    if master.execute("SELECT 1 FROM faculty WHERE emp_no = ?",
                      (fields["emp_no"],)).fetchone():
        master.close()
        flash("A faculty with employee number %s already exists." % fields["emp_no"],
              "error")
        return render_template("faculty_edit.html", fac=request.form,
                               dept_options=dept_options)

    master.execute(
        "INSERT INTO faculty (emp_no, name, email, phone, home_dept_code, "
        "                     status, created_by) VALUES (?,?,?,?,?,?,?)",
        (fields["emp_no"], fields["name"], fields["email"], fields["phone"],
         fields["home_dept_code"], fields["status"], "admin:faculty_page"))
    master.commit(); master.close()

    activity_log.note(detail="Added faculty %s (%s)"
                      % (fields["emp_no"], fields["name"]))
    flash("Faculty added.", "success")
    return redirect(url_for("admin.faculty_list"))


# ============================================================================
# EDIT / CHANGE DEPARTMENT  —  GET/POST /admin/faculty/<emp_no>/edit
# ============================================================================
# The emp_no (the key that links to offerings) is shown read-only; everything
# else — name, email, phone, HOME DEPARTMENT, active/inactive — is editable. This
# is where you re-assign a faculty who has moved departments.
# ----------------------------------------------------------------------------
@admin_bp.route("/faculty/<emp_no>/edit", methods=["GET", "POST"])
def faculty_edit(emp_no):
    master = get_master()
    row = master.execute("SELECT * FROM faculty WHERE emp_no = ?", (emp_no,)).fetchone()
    if row is None:
        master.close()
        flash("No such faculty.", "error")
        return redirect(url_for("admin.faculty_list"))

    dept_options, _labels, valid = _dept_context(master)

    if request.method == "GET":
        master.close()
        return render_template("faculty_edit.html", fac=row,
                               dept_options=dept_options)

    fields, err = _clean(request.form, valid_codes=valid)
    if err:
        master.close()
        flash(err, "error")
        return redirect(url_for("admin.faculty_edit", emp_no=emp_no))

    # emp_no itself is not changed here (it is the link to offerings); we update
    # everything else on the existing row.
    master.execute(
        "UPDATE faculty SET name=?, email=?, phone=?, home_dept_code=?, status=? "
        "WHERE emp_no=?",
        (fields["name"], fields["email"], fields["phone"],
         fields["home_dept_code"], fields["status"], emp_no))
    master.commit(); master.close()

    activity_log.note(detail="Edited faculty %s (dept %s, %s)"
                      % (emp_no, fields["home_dept_code"], fields["status"]))
    flash("Saved.", "success")
    return redirect(url_for("admin.faculty_list"))


# ============================================================================
# REMOVE  —  POST /admin/faculty/<emp_no>/delete
# ============================================================================
# Hard-delete a faculty master row. We warn (in the template's confirm) that any
# offerings still referencing this emp_no will no longer resolve an email/HOD
# until reassigned. For a faculty who has merely left, prefer marking them
# INACTIVE on the edit screen (keeps the record); delete is for genuine mistakes.
# ----------------------------------------------------------------------------
@admin_bp.route("/faculty/<emp_no>/delete", methods=["POST"])
def faculty_delete(emp_no):
    master = get_master()
    row = master.execute("SELECT name FROM faculty WHERE emp_no = ?", (emp_no,)).fetchone()
    if row is None:
        master.close()
        flash("No such faculty.", "error")
        return redirect(url_for("admin.faculty_list"))
    master.execute("DELETE FROM faculty WHERE emp_no = ?", (emp_no,))
    master.commit(); master.close()

    activity_log.note(detail="Removed faculty %s (%s)" % (emp_no, row["name"]))
    flash("Faculty removed.", "success")
    return redirect(url_for("admin.faculty_list"))
