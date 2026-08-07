# ============================================================================
# readiness.py  —  The Readiness Check gate (spec v3 §8)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# After both uploads are committed, a cycle CANNOT be opened until this check
# passes. It is the single most valuable safety feature in the system: the last
# moment a data error costs nothing to fix, before 2000 emails go out and the
# same error costs a great deal.
#
# It proves the two uploads AGREE with each other by checking BOTH directions:
#
#   FORWARD (§8.1)  — every teaching assignment must resolve to a non-zero
#                     audience. CORE/LAB/PROJECT resolve from the roster by
#                     (dept, year, section); ELECTIVE from the enrollment upload.
#                     Zero students => that faculty gets an empty report. (Error.)
#                     An elective with NO enrollment at all => Pending. (Blocks.)
#
#   REVERSE (§8.4)  — every roster class (dept, year, section) must have at least
#                     one teaching assignment pointing at it. Zero => an entire
#                     class opens a form with nothing in it. (Error.)
#
# Plus soft warnings (§8.5): count far from expected_students, faculty email
# missing/malformed, and a separate count of excluded students so a reduced
# audience is visibly explained rather than looking like a bug.
#
# Severity (§8.3): 🔴 error (zero students) and 🟠 pending (elective awaiting
# enrollment) BLOCK the cycle; 🟡 warning does not but must be acknowledged. An
# error may be dismissed with a written reason (readiness_dismissal), which the
# engine reads back so a deliberately-empty elective stops blocking.
#
# This module is PURE LOGIC (no Flask). The admin route renders `compute()`'s
# result; `persist_state()` caches READY/NOT_READY on the cycle so the "Open
# cycle" button can be disabled (spec §8.6, §9).
# ----------------------------------------------------------------------------


# Rough per-account daily external-recipient cap (spec §10.1) — surfaced so the
# admin sees whether a full send needs staggering.
DAILY_EMAIL_CAP = 2000


def _active_count_for_group(master, cycle_code, dept, year, section):
    """Count ACTIVE (non-excluded) roster students an offering with this
    (dept, year, section) reaches. A 'NA' offering section reaches the whole
    year/dept; a concrete section reaches only that section (spec §7.4)."""
    if section in (None, "NA"):
        return master.execute(
            "SELECT COUNT(*) n FROM students WHERE cycle_code=? AND dept_code=? "
            "AND year_of_study=? AND status='active'",
            (cycle_code, dept, year)).fetchone()["n"]
    return master.execute(
        "SELECT COUNT(*) n FROM students WHERE cycle_code=? AND dept_code=? "
        "AND year_of_study=? AND status='active' AND section=?",
        (cycle_code, dept, year, section)).fetchone()["n"]


def _elective_enrolled_count(master, cycle_code, offering_id):
    """Count ACTIVE enrolled students for an elective offering (excluded students
    do not count toward its audience)."""
    return master.execute(
        "SELECT COUNT(*) n FROM enrollment e "
        "JOIN students s ON s.reg_no = e.reg_no AND s.cycle_code = e.cycle_code "
        "WHERE e.cycle_code=? AND e.offering_id=? AND s.status='active'",
        (cycle_code, offering_id)).fetchone()["n"]


def compute(master, cycle_code):
    """Run the full Readiness Check and return a structured result dict.

    Returns keys:
      state         : 'READY' / 'NOT_READY'
      banner        : one-line human summary
      tiles         : summary counts (courses, faculty, students, forms, emails)
      rows          : per-assignment rows for the full table
      flagged       : the flagged items, grouped, shown first in the UI
      reverse       : the students-with-no-courses items
      excluded      : count of excluded students (shown separately)
    """
    dismissals = {d["check_key"] for d in master.execute(
        "SELECT check_key FROM readiness_dismissal WHERE cycle_code=?",
        (cycle_code,))}

    offerings = master.execute(
        "SELECT * FROM offering WHERE cycle_code=? "
        "ORDER BY dept_code, year_of_study, section, course_code", (cycle_code,)
    ).fetchall()

    rows, flagged = [], []
    errors = pendings = warnings = 0
    total_forms = 0

    for o in offerings:
        oid = o["id"]
        if o["course_type"] == "ELECTIVE":
            # An elective with NO enrollment rows at all is 'pending' (awaiting the
            # upload); with enrollment rows but zero ACTIVE members it is an error.
            any_enroll = master.execute(
                "SELECT COUNT(*) n FROM enrollment WHERE cycle_code=? AND offering_id=?",
                (cycle_code, oid)).fetchone()["n"]
            found = _elective_enrolled_count(master, cycle_code, oid)
            if any_enroll == 0:
                status, sev = "PENDING", "amber"
            elif found == 0:
                status, sev = "ERROR", "red"
            else:
                status, sev = "OK", "ok"
        else:
            found = _active_count_for_group(
                master, cycle_code, o["dept_code"], o["year_of_study"], o["section"])
            status = "OK" if found > 0 else "ERROR"
            sev = "ok" if found > 0 else "red"

        check_key = f"{status.lower()}:{oid}"
        dismissed = check_key in dismissals
        if dismissed and status in ("ERROR", "PENDING"):
            sev, status = "dismissed", "DISMISSED"

        # Soft warnings: count far from expected, or bad faculty email.
        wmsgs = []
        if o["expected_students"] and status == "OK" and o["course_type"] != "ELECTIVE":
            if abs(found - o["expected_students"]) > max(3, 0.2 * o["expected_students"]):
                wmsgs.append(f"found {found} vs expected {o['expected_students']}")
        if not o["faculty_email"]:
            wmsgs.append("no faculty email")

        row = {
            "offering_id": oid,
            "course_code": o["course_code"],
            "course_name": o["course_name"],
            "faculty": o["faculty"],
            "prog": o["dept_code"], "year": o["year_of_study"], "section": o["section"],
            "type": o["course_type"],
            "found": found,
            "expected": o["expected_students"],
            "status": status, "severity": sev,
            "warnings": wmsgs,
        }
        rows.append(row)

        if sev == "red":
            errors += 1
            flagged.append({**row, "why": "resolves to zero students"})
        elif sev == "amber":
            pendings += 1
            flagged.append({**row, "why": "elective awaiting enrollment upload"})
        if wmsgs and sev in ("ok", "dismissed"):
            warnings += 1
        if status in ("OK", "DISMISSED"):
            total_forms += found if o["course_type"] == "ELECTIVE" else found

    # REVERSE CHECK (§8.4): roster classes with no teaching assignment.
    reverse = []
    groups = master.execute(
        "SELECT dept_code, year_of_study, section, COUNT(*) n FROM students "
        "WHERE cycle_code=? AND status='active' "
        "GROUP BY dept_code, year_of_study, section", (cycle_code,)
    ).fetchall()
    for g in groups:
        # Non-elective offerings reaching this class.
        n_direct = master.execute(
            "SELECT COUNT(*) n FROM offering WHERE cycle_code=? AND course_type!='ELECTIVE' "
            "AND dept_code=? AND year_of_study=? "
            "AND (section='NA' OR section=?)",
            (cycle_code, g["dept_code"], g["year_of_study"], g["section"])
        ).fetchone()["n"]
        # Electives that enroll at least one member of this class.
        n_elective = master.execute(
            "SELECT COUNT(DISTINCT o.id) n FROM offering o "
            "JOIN enrollment e ON e.offering_id=o.id AND e.cycle_code=o.cycle_code "
            "JOIN students s ON s.reg_no=e.reg_no AND s.cycle_code=e.cycle_code "
            "WHERE o.cycle_code=? AND s.dept_code=? AND s.year_of_study=? AND s.section=?",
            (cycle_code, g["dept_code"], g["year_of_study"], g["section"])
        ).fetchone()["n"]
        if n_direct + n_elective == 0:
            key = f"reverse:{g['dept_code']}:{g['year_of_study']}:{g['section']}"
            if key not in dismissals:
                errors += 1
                reverse.append({
                    "prog": g["dept_code"], "year": g["year_of_study"],
                    "section": g["section"], "students": g["n"],
                    "check_key": key,
                    "why": "class has no teaching assignment — empty feedback form",
                })

    excluded = master.execute(
        "SELECT COUNT(*) n FROM students WHERE cycle_code=? AND status='excluded'",
        (cycle_code,)).fetchone()["n"]
    n_students = master.execute(
        "SELECT COUNT(*) n FROM students WHERE cycle_code=? AND status='active'",
        (cycle_code,)).fetchone()["n"]
    n_faculty = master.execute(
        "SELECT COUNT(DISTINCT faculty_id) n FROM offering WHERE cycle_code=?",
        (cycle_code,)).fetchone()["n"]

    state = "READY" if (errors == 0 and pendings == 0) else "NOT_READY"
    if state == "READY":
        banner = (f"READY — all {len(offerings)} assignments resolved · "
                  f"{n_students} students · {total_forms} forms to issue")
    else:
        bits = []
        if errors:
            bits.append(f"{errors} error(s)")
        if pendings:
            bits.append(f"{pendings} elective(s) awaiting enrollment")
        banner = "NOT READY — " + " · ".join(bits)

    tiles = {
        "assignments": len(offerings),
        "faculty": n_faculty,
        "students": n_students,
        "excluded": excluded,
        "forms": total_forms,
        "emails": n_students,               # one email per active student (§10)
        "email_cap": DAILY_EMAIL_CAP,
        "needs_stagger": n_students > DAILY_EMAIL_CAP,
        "errors": errors, "pending": pendings, "warnings": warnings,
    }

    return {
        "state": state, "banner": banner, "tiles": tiles,
        "rows": rows, "flagged": flagged, "reverse": reverse, "excluded": excluded,
    }


def persist_state(master, cycle_code, state):
    """Cache the computed READY/NOT_READY on the cycle (spec §8.6) so the
    'Open cycle' action can be gated without recomputing on every page."""
    master.execute("UPDATE cycle SET readiness_state=? WHERE code=?",
                   (state, cycle_code))
    master.commit()


def dismiss(master, cycle_code, check_key, reason, by="admin"):
    """Record an explicit, reasoned dismissal of a readiness error (spec §8.3)."""
    master.execute(
        "INSERT OR REPLACE INTO readiness_dismissal "
        "(cycle_code, offering_id, check_key, reason, dismissed_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (cycle_code, _oid_from_key(check_key), check_key, reason, by))
    master.commit()


def _oid_from_key(check_key):
    """Extract an offering id from keys like 'error:42' (None for reverse keys)."""
    parts = check_key.split(":")
    if len(parts) == 2 and parts[1].isdigit():
        return int(parts[1])
    return None
