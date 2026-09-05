# ============================================================================
# hod_bundles.py  —  Group scored reports BY THE HOD who owns each staff member
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# The Dean's ask (Sept 2026): every bulk report download should be organised by
# the HOD a teacher reports to — one PDF (or one zip folder) per department, with
# that department's staff inside. "The HOD a teacher reports to" is already a
# solved concept in this codebase: it is the FACULTY's home department
# (offering.faculty_id -> faculty.home_dept_code), which rbac.py uses to decide
# who may see and endorse a course. So grouping "by HOD" == grouping by that
# effective department. This module is the single place that builds that grouped,
# already-scored structure, so the admin reports page and the leaders' portal
# produce identical bundles and can never drift apart (the same reason
# consolidation.py centralises the elective-pooling rule).
#
# WHY IT ALSO FIXES THE OLD "DOWNLOAD ZIP HANGS FOREVER" BUG
# ----------------------------------------------------------------------------
# The previous atr_reports_zip iterated EVERY offering in scope and, for each one,
# called consolidation.members_of(), which internally re-ran
# deliveries_with_responses() — a full re-scan of the whole response table plus a
# re-fetch and re-grouping of all offerings — on every single iteration. That is
# O(courses x responses) of SQLite work on the networked (NFS) database, so at
# college scale it effectively never returned. This module instead groups the
# responded deliveries EXACTLY ONCE (like classification.classify_cycle does) and
# scores each delivery a single time — O(n) — so a whole-college bundle is fast.
#
# ANONYMITY BOUNDARY is untouched: identities/departments come from master.db;
# the per-cycle answer DB is read only through the frozen scoring stack, never for
# a token. This module opens no token table.
# ----------------------------------------------------------------------------

import consolidation
import scoring


# Friendly labels for the two "no HOD" buckets, matching rbac.py's own vocabulary
# (a faculty with home_dept_code = 'EXTERNAL' is a visiting/external teacher with
# no HOD; home_dept_code IS NULL means the department has not been set yet). These
# only ever appear for a college-wide leader (Vice Dean / Dean) or the admin,
# because a HOD's scope never contains either value.
DEPT_EXTERNAL = "EXTERNAL"
_LABEL_EXTERNAL = "EXTERNAL — External / visiting faculty (no HOD)"
_LABEL_UNASSIGNED = "UNASSIGNED — No department set"


# ----------------------------------------------------------------------------
# _effective_dept_map(master, cycle_code) -> { offering_id: dept_code|None }
# ----------------------------------------------------------------------------
# One query that resolves every offering in the cycle to its effective department
# (the FACULTY's home department). A LEFT JOIN so an offering whose faculty is not
# in the master, or has no home department, comes back as None (UNASSIGNED). Codes
# are upper-cased so they compare cleanly against an rbac allow-set.
# ----------------------------------------------------------------------------
def _effective_dept_map(master, cycle_code):
    rows = master.execute(
        "SELECT o.id AS oid, f.home_dept_code AS eff "
        "FROM offering o LEFT JOIN faculty f ON f.emp_no = o.faculty_id "
        "WHERE o.cycle_code = ?", (cycle_code,)).fetchall()
    out = {}
    for r in rows:
        eff = (r["eff"] or "").strip().upper() or None
        out[r["oid"]] = eff
    return out


# ----------------------------------------------------------------------------
# _dept_labels(master) -> { dept_code: "CODE — Name" }
# ----------------------------------------------------------------------------
# Human labels for the department folders / divider pages, read from the
# department table (the backbone of access control, which also stores each dept's
# short name). A code missing from the table falls back to just the code.
# ----------------------------------------------------------------------------
def _dept_labels(master):
    labels = {}
    try:
        for r in master.execute("SELECT code, name FROM department").fetchall():
            code = (r["code"] or "").strip().upper()
            name = (r["name"] or "").strip()
            labels[code] = f"{code} — {name}" if name else code
    except Exception:
        pass                      # very old DB without a department table
    return labels


# ----------------------------------------------------------------------------
# _sort_key_for_dept(code) — order real departments alphabetically, then EXTERNAL,
# then UNASSIGNED (None) last, so a bundle always reads dept-by-dept with the two
# "no HOD" buckets at the end.
# ----------------------------------------------------------------------------
def _sort_key_for_dept(code):
    if code is None:
        return (2, "")                 # UNASSIGNED last
    if code == DEPT_EXTERNAL:
        return (1, "")                 # EXTERNAL just before UNASSIGNED
    return (0, code)                   # real departments, alphabetical


# ----------------------------------------------------------------------------
# dept_sort_key(code) — PUBLIC ordering key reused by every on-screen report
# listing (admin "Feedback reports" and the leaders' dashboard) so they all sort
# by department E01 -> E81 identically. Normalises blanks to None first, so a
# missing code sorts with UNASSIGNED (last), EXTERNAL just before it, and every
# real E-code ascending. A joined elective label ("E01, E05") should be split on
# the comma by the caller and only its FIRST code passed here.
# ----------------------------------------------------------------------------
def dept_sort_key(code):
    return _sort_key_for_dept((code or "").strip().upper() or None)


# ----------------------------------------------------------------------------
# build_department_groups(master, cy, cycle_code, allowed_depts, dl_weight,
#                         is_test) -> list[ {code, label, results} ]
# ----------------------------------------------------------------------------
# THE entry point. Score every responded delivery in scope exactly once and
# bucket the results by effective department, returned as an ordered list:
#
#     [ { "code": "E01",                       # None for UNASSIGNED
#         "label": "E01 — CSE-AIML",           # divider text / folder name
#         "results": [ <scored dict>, ... ] }, # sorted by faculty, then course
#       ... ]                                   # depts ordered by _sort_key_for_dept
#
# Parameters:
#   * allowed_depts — a SET of upper-cased dept codes the caller may see, or None
#     meaning "no restriction" (Vice Dean / Dean, or an admin asking for ALL).
#     Pass rbac.allowed_dept_codes(leader) straight through for a leader. A HOD's
#     set never contains EXTERNAL/None, so those buckets are naturally excluded
#     for a HOD and included only for a college-wide caller.
#   * dl_weight — scoring.get_discussed_late_weight(master), passed in so the
#     numbers match every other report surface.
#   * is_test — when True, stamp each result with the same "TESTING ONLY"
#     watermark the single-report PDF uses.
#
# Deliveries that do not score (uncategorised / template-less) are skipped, just
# as classification and the single-report route skip them.
# ----------------------------------------------------------------------------
def build_department_groups(master, cy, cycle_code, allowed_depts,
                            dl_weight=None, is_test=False):
    eff_map = _effective_dept_map(master, cycle_code)
    labels = _dept_labels(master)

    # One grouping of the responded universe (electives already pooled). O(n).
    deliveries = consolidation.deliveries_with_responses(master, cy, cycle_code)

    buckets = {}                         # dept_code|None -> list[result]
    for anchor, g in deliveries.items():
        eff = eff_map.get(anchor)        # all members share one faculty ⇒ one dept
        # RBAC / scope filter. None allow-set means unrestricted (see everything).
        if allowed_depts is not None and eff not in allowed_depts:
            continue
        result = scoring.score_offering_group(master, cy, g["oids"], dl_weight)
        if result is None:
            continue                     # nothing scorable in this delivery
        if is_test:
            result["watermark"] = "TESTING ONLY"
        buckets.setdefault(eff, []).append(result)

    groups = []
    for code in sorted(buckets.keys(), key=_sort_key_for_dept):
        results = buckets[code]
        # Stable, readable order inside a department: by faculty name, then course.
        results.sort(key=lambda r: (
            (r["offering"]["faculty"] or "").lower()
            if "faculty" in r["offering"].keys() else "",
            (r["offering"]["course_code"] or "")
            if "course_code" in r["offering"].keys() else "",
        ))
        if code is None:
            label = _LABEL_UNASSIGNED
        elif code == DEPT_EXTERNAL:
            label = _LABEL_EXTERNAL
        else:
            label = labels.get(code, code)
        groups.append({"code": code, "label": label, "results": results})
    return groups


# ----------------------------------------------------------------------------
# flatten(groups) -> list[result]   — every result across all departments, in the
# same order the groups present them. Handy for a single-department caller that
# wants the flat <Staff>/<report>.pdf zip (report_export.build_staff_foldered_pdf_zip).
# ----------------------------------------------------------------------------
def flatten(groups):
    out = []
    for g in groups:
        out.extend(g.get("results") or [])
    return out
