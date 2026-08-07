# ============================================================================
# migrate_v2_module3_logging.py  —  V2.0 · Module 3 migration (ADDITIVE)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# schema_master.sql now declares the FINAL shape of master.db, including the new
# `activity_log` table, so a FRESH install (init_db.py) already comes up with it.
# But the professor's LIVE master.db was created earlier and holds real data
# (offerings, students, cycles, leader logins). We must NOT rebuild it — we bring
# it forward IN PLACE, adding ONLY the new logging table + its indexes and never
# touching a single existing column, row, index or the two-file anonymity split.
#
# This is the EXISTING-DB counterpart of the schema file, in the exact idempotent
# style of migrate_v2_module1.py / _module2.py: run it once, twice, ten times →
# identical end state, no error, no duplicate. It reuses db.get_master() so WAL +
# foreign-key enforcement + the data-dir location are the same ones the rest of
# the app uses.
#
# WHAT IT ADDS (Design · Module 3 — the system-wide audit trail):
#   1. activity_log            (NEW)  who did what, when, where     (CREATE IF NOT EXISTS)
#   2. ix_activity_*           (NEW)  four viewer indexes           (CREATE IF NOT EXISTS)
#
# Usage (from inside the app/ folder):  python migrate_v2_module3_logging.py
# ----------------------------------------------------------------------------

import db  # the ONE place that opens master.db (WAL, FK enforcement, two-file split)


# ----------------------------------------------------------------------------
# _table_exists(conn, table) -> bool
# ----------------------------------------------------------------------------
# Ask SQLite's catalog whether a table already exists, so our "add only what's
# missing" logic can report cleanly what it did (the CREATE TABLE IF NOT EXISTS
# below is itself safe either way; this is just for an honest, auditable run log).
def _table_exists(conn, table):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    return row is not None


# ----------------------------------------------------------------------------
# _ensure_activity_log(conn)  — create the audit table + its indexes.
# ----------------------------------------------------------------------------
# CREATE TABLE / INDEX IF NOT EXISTS with the SAME definitions as
# schema_master.sql, so a fresh install and a migrated live DB converge on an
# identical structure. Kept byte-for-byte in step with the schema file (as the
# Module 1/2 tables are). See activity_log.py for how each column is populated and
# for the anonymity exclusion of the student flow.
def _ensure_activity_log(conn):
    existed = _table_exists(conn, "activity_log")
    print("[1] activity_log  — system-wide operations audit trail (Module 3)")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS activity_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            at          TEXT NOT NULL DEFAULT (datetime('now')),
            actor_type  TEXT NOT NULL,        -- 'ADMIN' | 'LEADER' | 'FACULTY' | 'SYSTEM'
            actor_id    INTEGER,              -- app_user.id when a leader; else NULL
            actor_label TEXT,                 -- email / 'admin-console'
            action      TEXT NOT NULL,        -- friendly verb, e.g. 'Opened / closed a cycle'
            endpoint    TEXT,                 -- Flask endpoint, e.g. 'admin.cycles_toggle'
            method      TEXT,                 -- HTTP method
            path        TEXT,                 -- request path
            status      INTEGER,              -- HTTP status returned
            cycle_code  TEXT,                 -- cycle context, when known
            target_type TEXT,                 -- optional object kind
            target_id   TEXT,                 -- optional object id (text)
            detail      TEXT,                 -- optional human note
            ip          TEXT                  -- requester IP
        );
        CREATE INDEX IF NOT EXISTS ix_activity_at     ON activity_log (at);
        CREATE INDEX IF NOT EXISTS ix_activity_actor  ON activity_log (actor_label);
        CREATE INDEX IF NOT EXISTS ix_activity_action ON activity_log (action);
        CREATE INDEX IF NOT EXISTS ix_activity_cycle  ON activity_log (cycle_code);
        """
    )
    print("    %s activity_log (+4 indexes)"
          % ("· already present — ensured" if existed else "+ created"))


# ----------------------------------------------------------------------------
# migrate(conn=None)  —  the public entry point.
# ----------------------------------------------------------------------------
# Runs inside ONE transaction so the DB never lands half-migrated. Accepts an
# optional connection (tests pass a throwaway COPY of master.db to prove non-
# destructiveness before we touch the real file); with no argument it opens the
# real master.db via db.get_master(). Returns the connection so a caller/test can
# keep inspecting it. Mirrors migrate_v2_module1.migrate() exactly.
def migrate(conn=None):
    owns_conn = conn is None            # did WE open it? then WE close it.
    if conn is None:
        conn = db.get_master()

    print("=" * 70)
    print("AFS Version 2.0 · Module 3 — master.db migration (additive, idempotent)")
    print("=" * 70)

    try:
        _ensure_activity_log(conn)
        conn.commit()                   # one atomic commit for the whole delta
        print("-" * 70)
        print("Migration complete. Existing tables/rows untouched; activity_log added.")
        print("=" * 70)
    except Exception:
        conn.rollback()                 # leave the DB exactly as we found it on error
        raise

    if owns_conn:
        conn.close()
    return conn


if __name__ == "__main__":
    migrate()
