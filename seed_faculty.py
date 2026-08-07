# ============================================================================
# seed_faculty.py  —  V2.0 · Module 5 : load the real Faculty Master roster
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# migrate_v2_module5_faculty.py creates the empty `faculty` table; THIS script
# fills it with the real people, exactly as seed_leaders.py fills the leader
# accounts after Module 1 creates their table. The roster lives in a plain
# tab-separated file (app/data/faculty_roster.tsv) — data kept OUT of the code
# so it can be updated without touching Python, and kept in the git-ignored
# data/ folder because it contains personal contact details (phone/email).
#
# The file's columns are:  emp_no <tab> name <tab> phone <tab> email
# (home_dept_code is intentionally NOT in the roster — the department mapping is
#  entered later on the admin "Manage Faculty" page; until then it stays NULL and
#  the app falls back to the course's own department for ATR routing.)
#
# IDEMPOTENT + NON-DESTRUCTIVE:
#   * emp_no is the PRIMARY KEY, so we UPSERT: a NEW emp_no is inserted; an
#     EXISTING one has its name/phone/email refreshed from the file, but its
#     admin-entered `home_dept_code` and `status` are PRESERVED (never clobbered).
#   * This means you can re-run the seed after editing the .tsv to fix a name or
#     email, and you will not lose the department assignments you made by hand.
#
# Usage (from inside the app/ folder):  python seed_faculty.py
# ----------------------------------------------------------------------------

import os
import db                      # the one master.db opener
from config import Config      # DATA_DIR — where the roster file lives

# The roster file sits beside the databases in app/data/.
ROSTER_PATH = os.path.join(Config.DATA_DIR, "faculty_roster.tsv")

# Audit marker for the created_by column (a system load, no human admin).
SEED_ACTOR = "seed_faculty"


# ----------------------------------------------------------------------------
# _read_roster(path) -> list[dict] — parse the tab-separated roster file. Skips
# the header line and any blank lines; trims surrounding whitespace on every
# field (the source paste had stray spaces). Returns one dict per faculty.
# ----------------------------------------------------------------------------
def _read_roster(path):
    people = []
    with open(path, "r", encoding="utf-8") as fh:
        for i, raw in enumerate(fh):
            line = raw.rstrip("\n")
            if not line.strip():
                continue                          # skip blank lines
            parts = [p.strip() for p in line.split("\t")]
            # Header row (starts with the literal 'emp_no') is skipped.
            if i == 0 and parts and parts[0].lower() == "emp_no":
                continue
            # Be forgiving of rows with missing trailing columns.
            emp_no = parts[0] if len(parts) > 0 else ""
            name   = parts[1] if len(parts) > 1 else ""
            phone  = parts[2] if len(parts) > 2 else ""
            email  = parts[3] if len(parts) > 3 else ""
            if not emp_no:
                continue                          # a row with no key is unusable
            people.append({"emp_no": emp_no, "name": name,
                           "phone": phone, "email": email.lower()})
    return people


# ----------------------------------------------------------------------------
# _upsert(conn, person) — insert a new faculty, or refresh the contact fields of
# an existing one WITHOUT disturbing home_dept_code / status. Implemented with
# SQLite's ON CONFLICT so it is a single atomic statement per person.
# ----------------------------------------------------------------------------
def _upsert(conn, person):
    conn.execute(
        """
        INSERT INTO faculty (emp_no, name, email, phone, home_dept_code,
                             status, created_by)
        VALUES (:emp_no, :name, :email, :phone, NULL, 'active', :actor)
        ON CONFLICT(emp_no) DO UPDATE SET
            name  = excluded.name,
            email = excluded.email,
            phone = excluded.phone
            -- NOTE: home_dept_code and status are deliberately left untouched,
            -- so a re-seed never wipes the department you assigned by hand.
        """,
        {**person, "actor": SEED_ACTOR},
    )


# ----------------------------------------------------------------------------
# seed_faculty(conn) -> dict summary — load every roster row. Does NOT commit
# (the caller owns the transaction, matching seed_leaders.py).
# ----------------------------------------------------------------------------
def seed_faculty(conn):
    people = _read_roster(ROSTER_PATH)
    for p in people:
        _upsert(conn, p)
    total = conn.execute("SELECT COUNT(*) AS n FROM faculty").fetchone()["n"]
    no_dept = conn.execute(
        "SELECT COUNT(*) AS n FROM faculty WHERE home_dept_code IS NULL"
    ).fetchone()["n"]
    return {"loaded": len(people), "total_faculty": total, "without_dept": no_dept}


# ----------------------------------------------------------------------------
# main() — run against the real master.db (open + commit + close).
# ----------------------------------------------------------------------------
def main():
    print("=" * 70)
    print("AFS Version 2.0 · Module 5 — seed Faculty Master from", ROSTER_PATH)
    print("=" * 70)
    conn = db.get_master()
    try:
        summary = seed_faculty(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print("Rows read from file :", summary["loaded"])
    print("Total faculty now   :", summary["total_faculty"])
    print("Without a home dept :", summary["without_dept"],
          "(set these on the admin Manage Faculty page)")
    print("=" * 70)
    conn.close()


if __name__ == "__main__":
    main()
