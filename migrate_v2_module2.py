# ============================================================================
# migrate_v2_module2.py  —  Version 2.0 · Module 2 schema migration (ADDITIVE)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# Module 1 migrated the PERMANENT master.db (leaders, IAM, banding record).
# Module 2's new tables — atr, atr_event, faculty_token — are CYCLE-SCOPED
# (spec §3.1), so they live in each per-cycle database (cycle_<AY>_<CA>.db),
# right beside `response`/`answer`, and archive with the cycle. schema_cycle.sql
# already declares them, so a cycle DB CREATED from now on comes up carrying
# them. This script is the counterpart for cycle DBs that ALREADY EXIST — it
# brings each forward IN PLACE, adding only the three ATR tables and never
# touching the Version 1.0 token/response/answer tables or the anonymity split.
#
# Because every table here is `CREATE TABLE IF NOT EXISTS`, the whole migration
# is idempotent: run it once, twice, ten times → identical end state, no error,
# no duplicate table, and — crucially — no row of feedback data is ever touched
# (CREATE IF NOT EXISTS on an already-present table is a pure no-op).
#
# Two ways to run it (from inside the app/ folder):
#   python migrate_v2_module2.py                 # migrate EVERY registered cycle DB
#   python migrate_v2_module2.py 2026-27 DEMOCA1 # migrate ONE cycle by AY + code
#
# It reuses db.get_cycle()/db.cycle_db_path() so WAL + foreign_keys + the data
# directory are exactly the ones the rest of the app uses — no bespoke handling.
# ----------------------------------------------------------------------------

import os     # existence checks for per-cycle files
import sys    # optional command-line (AY, code) argument

import db     # the ONE place that opens SQLite (WAL, FK, two-file split)


# ----------------------------------------------------------------------------
# The exact DDL for the three Module 2 tables. Kept byte-for-byte in step with
# the same three CREATE statements in schema_cycle.sql (a fresh cycle DB uses
# that file; an existing one uses this), so both paths converge on an identical
# structure. If you change one, change the other — SQLite has no single "apply
# the schema delta" primitive, so the two mechanisms must be kept in sync by
# hand, exactly as Module 1 did for master.db.
# ----------------------------------------------------------------------------
ATR_TABLES_DDL = """
CREATE TABLE IF NOT EXISTS atr (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    offering_id        INTEGER NOT NULL,
    cycle_code         TEXT    NOT NULL,
    state              TEXT    NOT NULL DEFAULT 'EXPECTED',
    current_owner_role TEXT,
    body               TEXT,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (offering_id)
);

CREATE TABLE IF NOT EXISTS atr_event (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    atr_id        INTEGER NOT NULL,
    actor_user_id TEXT,
    action        TEXT NOT NULL,
    comment       TEXT,
    at            TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (atr_id) REFERENCES atr(id)
);

CREATE TABLE IF NOT EXISTS faculty_token (
    jti           TEXT PRIMARY KEY,
    offering_id   INTEGER NOT NULL,
    faculty_email TEXT,
    purpose       TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    used_at       TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# ----------------------------------------------------------------------------
# migrate_cycle_conn(conn) — apply the ATR DDL to an ALREADY-OPEN cycle
# connection. Split out so the tests can hand us a connection to a throwaway
# COPY of a cycle DB (proving non-destructiveness before we ever touch a real
# file). Does NOT commit — the caller owns the transaction.
# ----------------------------------------------------------------------------
def migrate_cycle_conn(conn):
    conn.executescript(ATR_TABLES_DDL)


# ----------------------------------------------------------------------------
# _table_names(conn) -> set[str] — the current tables in a cycle DB, used only
# for the run log so the operator can see the three ATR tables are present after
# the migration.
# ----------------------------------------------------------------------------
def _table_names(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r["name"] for r in rows}


# ----------------------------------------------------------------------------
# migrate_one(ay_label, cycle_code) — migrate a SINGLE named cycle DB in place.
# Skips cleanly (with a message) if that cycle's file does not exist yet — a
# DRAFT cycle whose per-cycle DB has never been created has nothing to migrate;
# it will be born with the tables via schema_cycle.sql when it is first opened.
# Wraps the change in one transaction so the file never lands half-migrated.
# ----------------------------------------------------------------------------
def migrate_one(ay_label, cycle_code):
    path = db.cycle_db_path(ay_label, cycle_code)
    if not os.path.exists(path):
        print("    · %s / %s — no per-cycle DB yet, skipped (created on first open)"
              % (ay_label, cycle_code))
        return False

    conn = db.get_cycle(ay_label, cycle_code)
    try:
        migrate_cycle_conn(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    present = _table_names(conn)
    conn.close()
    have = [t for t in ("atr", "atr_event", "faculty_token") if t in present]
    print("    + %s / %s — ATR tables present: %s" % (ay_label, cycle_code, ", ".join(have)))
    return True


# ----------------------------------------------------------------------------
# migrate_all() — migrate EVERY cycle registered in master.db. We read the cycle
# list from master (the same source the student flow and reports use), then
# migrate each one's per-cycle DB. This is what the 5 AM job / the professor
# runs with no arguments.
# ----------------------------------------------------------------------------
def migrate_all():
    master = db.get_master()
    cycles = master.execute(
        "SELECT academic_year, code FROM cycle ORDER BY academic_year, code").fetchall()
    master.close()

    migrated = 0
    for c in cycles:
        if migrate_one(c["academic_year"], c["code"]):
            migrated += 1
    return migrated, len(cycles)


# ----------------------------------------------------------------------------
# main() — the command-line entry point. With two args (AY, code) it migrates
# just that cycle; with none, every registered cycle.
# ----------------------------------------------------------------------------
def main():
    print("=" * 70)
    print("AFS Version 2.0 · Module 2 — per-cycle DB migration (ATR tables, additive)")
    print("=" * 70)

    if len(sys.argv) == 3:
        ay, code = sys.argv[1], sys.argv[2]
        # Accept a bare '2026-27' or a full 'AY 2026-27'; cycle_db_path normalises.
        migrate_one(ay, code)
    else:
        migrated, total = migrate_all()
        print("-" * 70)
        print("Migrated %d of %d registered cycle DB(s). "
              "Version 1.0 response/answer/token data untouched." % (migrated, total))
    print("=" * 70)


if __name__ == "__main__":
    main()
