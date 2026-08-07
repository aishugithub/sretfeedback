# ============================================================================
# migrate_v2_module5_faculty.py  —  V2.0 · Module 5 : the FACULTY MASTER table
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# Until now a faculty member had NO record of their own. Their name, email and
# employee-number were COPIED onto every offering row they teach (see the
# `offering` table's faculty / faculty_email / faculty_id columns). That
# denormalisation caused three real problems visible in the live data:
#   * faculty_email was blank on every offering, so the ATR/result emails had no
#     address to send to (the "0 faculty emails sent" symptom);
#   * the same person could appear under several employee-numbers (data-entry
#     drift), because nothing enforced one identity per person;
#   * changing a faculty email meant editing many rows (an update anomaly).
#
# This migration introduces the single source of truth for a faculty: the
# `faculty` table, keyed by the employee-number (`emp_no`). From here on the
# course-allocation file only needs to carry emp_no + name; email, phone and the
# home department are managed ONCE, here, and looked up by join at send time.
#
# HOME DEPARTMENT: `home_dept_code` records which department (hence which HOD)
# a faculty administratively belongs to. It is the key the ATR endorsement now
# routes on (a faculty's own HOD reviews their ATRs). It is NULLABLE — until it
# is filled, the app falls back to the course offering's own department, so
# nothing breaks before the mapping is entered on the admin page.
#
# DESIGN NOTES:
#   * emp_no is TEXT to match offering.faculty_id exactly (values like '16093'
#     or the legacy 'FAC001'); TEXT also preserves any leading zeros.
#   * ADDITIVE + IDEMPOTENT: CREATE TABLE IF NOT EXISTS only. Re-running never
#     touches existing rows. The actual people are loaded separately by
#     seed_faculty.py (data, not schema), mirroring how seed_leaders.py loads the
#     leader accounts after Module 1 creates their table.
#   * Runs inside ONE transaction; accepts an optional connection so a test can
#     migrate a throwaway COPY of master.db first (same pattern as Module 1).
#
# Usage (from inside the app/ folder):  python migrate_v2_module5_faculty.py
# ----------------------------------------------------------------------------

import db   # the ONE master.db opener (WAL, foreign keys, two-file split)


# ----------------------------------------------------------------------------
# _ensure_faculty_table(conn) — create the faculty master table if absent.
# ----------------------------------------------------------------------------
def _ensure_faculty_table(conn):
    print("[1] faculty  — the Faculty Master (one row per person, keyed by emp_no)")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS faculty (
            emp_no         TEXT PRIMARY KEY,          -- employee/staff number: the stable key
            name           TEXT,                      -- canonical display name
            email          TEXT,                      -- institutional email (managed here)
            phone          TEXT,                      -- contact number (optional)
            home_dept_code TEXT,                      -- their department (-> HOD who endorses); NULLABLE
            status         TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'inactive'
            created_by     TEXT,                      -- audit: who/what created the row
            created_at     TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (home_dept_code) REFERENCES department(code)
        );

        -- Fast lookup by email (used when resolving/deduping), and by home dept
        -- (used when listing a department's faculty on the admin page).
        CREATE INDEX IF NOT EXISTS idx_faculty_email     ON faculty(email);
        CREATE INDEX IF NOT EXISTS idx_faculty_home_dept ON faculty(home_dept_code);
        """
    )


# ----------------------------------------------------------------------------
# migrate(conn=None) — the public entry point (mirrors Module 1's shape).
# ----------------------------------------------------------------------------
def migrate(conn=None):
    owns_conn = conn is None
    if conn is None:
        conn = db.get_master()

    print("=" * 70)
    print("AFS Version 2.0 · Module 5 — Faculty Master (additive, idempotent)")
    print("=" * 70)
    try:
        _ensure_faculty_table(conn)
        conn.commit()
        print("-" * 70)
        print("Done. `faculty` table ready. Load people with:  python seed_faculty.py")
        print("=" * 70)
    except Exception:
        conn.rollback()
        raise

    if owns_conn:
        conn.close()
    return conn


if __name__ == "__main__":
    migrate()
