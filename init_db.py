# ============================================================================
# init_db.py  —  One-shot database initialiser
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# Run this ONCE to bring a fresh installation to life (or safely re-run it — it
# is idempotent). It performs Night-1 setup (spec Section 16, Phases 1 & the
# import half of Phase 2):
#   1. Create master.db and apply schema_master.sql (Group A + Group C tables).
#   2. Seed the four categories, all scales/weights, and the four verbatim
#      question sets (via seed_data.seed).
#   3. Insert the active academic_year row, the two cycles (CA1 / CA3), and the
#      E01..E81 department reference legend.
#   4. Create the per-cycle database for the active cycle and apply
#      schema_cycle.sql (Group A token + Group B response/answer tables), so the
#      anonymity split exists and can be verified.
#   5. Run the allocation auto-converter to unpivot the matrix workbook into the
#      clean `offering` roster (spec Section 4.1).
#
# Usage (from inside the app/ folder):  python init_db.py
# ----------------------------------------------------------------------------

import os

import db                     # connection helpers (WAL, two-file split)
from config import Config     # paths + reference data
import seed_data              # the verbatim categories/scales/questions loader
# NOTE (v3): the matrix auto-converter is no longer run at init. Roster and
# allocation are uploaded per cycle (§7). The old converter is kept only as an
# opt-in migration helper and is imported there, not here.


def _read_sql(filename):
    """Read a .sql file next to this script and return its text for executescript()."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# The allocation workbook is for "Academic Year 2026 - 2027"; seed that as the
# single active academic year (spec 4.2). start_year=2026 drives year-of-study.
DEFAULT_AY_LABEL = "AY 2026-27"
DEFAULT_START_YEAR = 2026


def _seed_academic_year_and_cycles(conn):
    """Insert the active academic year and its two cycles if absent."""
    cur = conn.cursor()
    row = cur.execute(
        "SELECT id FROM academic_year WHERE ay_label = ?", (DEFAULT_AY_LABEL,)
    ).fetchone()
    if not row:
        cur.execute(
            "INSERT INTO academic_year (ay_label, start_year, current_sem, is_active) "
            "VALUES (?, ?, 'odd', 1)",
            (DEFAULT_AY_LABEL, DEFAULT_START_YEAR),
        )
    # Two feedback cycles for this AY (spec 7); both start CLOSED.
    for code, label in [("CA1", "CA1 - Intermediate"), ("CA3", "CA3 - End-of-Course")]:
        exists = cur.execute(
            "SELECT id FROM cycle WHERE academic_year = ? AND code = ?",
            (DEFAULT_AY_LABEL, code),
        ).fetchone()
        if not exists:
            default_email = (
                "Dear {student_name},\n\n"
                "Please submit your {cycle_name} feedback for all your courses here:\n"
                "{link}\n\n"
                "Your feedback is completely anonymous.\n\n"
                "- SRET"
            )
            cur.execute(
                "INSERT INTO cycle (code, label, academic_year, email_body, is_open) "
                "VALUES (?, ?, ?, ?, 0)",
                (code, label, DEFAULT_AY_LABEL, default_email),
            )
    conn.commit()


def _seed_departments(conn):
    """Create + populate the E01..E81 department reference table (spec 3).

    Pure reference data (not part of the anonymity design), so created here on
    demand rather than in the schema file. Idempotent via INSERT OR IGNORE.
    Kept as the SHORT-label legend used on report headers.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS department ("
        "  code TEXT PRIMARY KEY,"
        "  name TEXT NOT NULL"
        ")"
    )
    for code, name in Config.DEPT_CODES.items():
        conn.execute("INSERT OR IGNORE INTO department (code, name) VALUES (?, ?)",
                     (code, name))
    conn.commit()


def _seed_programmes(conn):
    """Populate the PROGRAMME MASTER (spec v3 §3) from Config.PROGRAMMES.

    Display + validation only — full programme name and level (B.Tech/B.Sc/M.Sc),
    NO duration column (v3 §2.1 deleted graduation arithmetic). Every uploaded
    roster/allocation programme_code is later validated against this table.
    Idempotent via INSERT OR IGNORE so re-running init never clobbers edits.
    """
    for code, (name, level) in Config.PROGRAMMES.items():
        conn.execute(
            "INSERT OR IGNORE INTO programme (code, name, level) VALUES (?, ?, ?)",
            (code, name, level),
        )
    conn.commit()


def _active_cycle(conn):
    """Return (ay_label, 'CA1') for the cycle we materialise a DB file for."""
    ay = conn.execute(
        "SELECT ay_label FROM academic_year WHERE is_active = 1 LIMIT 1"
    ).fetchone()
    ay_label = ay["ay_label"] if ay else DEFAULT_AY_LABEL
    return ay_label, "CA1"


def main():
    print("=" * 70)
    print("Feedback System - database initialisation (Night 1)")
    print("=" * 70)

    # Step 1: master.db schema
    print("[1/5] Creating master.db and applying schema_master.sql ...")
    master = db.get_master()
    db.run_script(master, _read_sql("schema_master.sql"))

    # Step 2: seed config (categories, scales, verbatim questions)
    print("[2/5] Seeding categories, scales/weights, and verbatim questions ...")
    seed_data.seed(master)

    # Step 3: academic year + cycles + programme master + department legend
    print("[3/4] Seeding academic year, CA1/CA3 cycles, programme master, legend ...")
    _seed_academic_year_and_cycles(master)
    _seed_programmes(master)     # v3 §3 programme master (name + level, no duration)
    _seed_departments(master)    # short-label legend for report headers

    # Step 4: per-cycle database (anonymity split). Roster + allocation are now
    # UPLOADED per cycle (spec v3 §7), so init leaves students/offering EMPTY —
    # there is no auto-import of the matrix workbook any more. The old converter
    # survives only as an opt-in migration helper (see migrate_allocation.py).
    ay_label, cycle_code = _active_cycle(master)
    print("[4/4] Creating per-cycle DB for %s / %s and applying schema_cycle.sql ..."
          % (ay_label, cycle_code))
    cycle = db.get_cycle(ay_label, cycle_code)
    db.run_script(cycle, _read_sql("schema_cycle.sql"))
    cycle.close()

    master.close()

    print("-" * 70)
    print("Done. Files created:")
    print("  master.db      :", Config.MASTER_DB)
    print("  cycle DB       :", db.cycle_db_path(ay_label, cycle_code))
    print("Master tables    : programme, students (roster), roster_range,")
    print("                   offering (teaching assignments), enrollment,")
    print("                   category, template, template_version, question,")
    print("                   scale, scale_option, cycle, readiness_dismissal,")
    print("                   academic_year, department")
    print("Cycle  tables    : token (Group A) | response, answer (Group B)")
    print("Next: upload a roster (§7.1) then an allocation (§7.2) for the cycle.")
    print("=" * 70)


if __name__ == "__main__":
    main()
