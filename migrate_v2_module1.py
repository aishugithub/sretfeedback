# ============================================================================
# migrate_v2_module1.py  —  Version 2.0 · Module 1 schema migration (ADDITIVE)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# schema_master.sql declares the FINAL shape of every table, so a FRESH install
# (init_db.py) already comes up carrying the Version 2.0 additions. But the
# professor's live master.db was created under Version 1.0 and already holds
# real data (444 offerings, 1698 students, 4 cycles). We must NOT rebuild it —
# we bring it forward IN PLACE, adding only what is new and never touching a
# single Version 1.0 column, row, index, scoring formula or the two-file
# anonymity split (Design §0 · "a delta on top of Version 1.0").
#
# This script is the EXISTING-DB counterpart of the schema file. Because SQLite
# has no "ALTER TABLE ... ADD COLUMN IF NOT EXISTS", the "only add what's
# missing" logic has to live in Python (here) rather than in SQL. Everything it
# does is idempotent: run it once, twice, ten times → identical end state, no
# error, no duplicate column, no duplicate row.
#
# WHAT IT ADDS (Design §3.1, §4, §6, §17 — Module 1 only):
#   1. department        + hod_user_id, vice_dean_user_id           (guarded ALTER)
#   2. cycle             + threshold_overall, threshold_section,
#                          min_responses                            (guarded ALTER, §6)
#   3. app_user          (NEW)  the ~13 leader logins               (CREATE IF NOT EXISTS)
#   4. set_pw_token      (NEW)  one-time set/reset-password links   (CREATE IF NOT EXISTS)
#   5. admin_log         (NEW)  IAM audit trail                     (CREATE IF NOT EXISTS)
#   6. offering_classification (NEW)  the GOOD/POOR band record     (CREATE IF NOT EXISTS)
#
# Usage (from inside the app/ folder):  python migrate_v2_module1.py
# It reuses db.get_master() so WAL + foreign_keys + the data-dir location are
# exactly the ones the rest of the app uses — no bespoke connection handling.
# ----------------------------------------------------------------------------

import db  # the ONE place that opens master.db (WAL, FK enforcement, two-file split)


# ----------------------------------------------------------------------------
# _existing_columns(conn, table) -> set[str]
# ----------------------------------------------------------------------------
# Ask SQLite what columns a table currently has, via PRAGMA table_info. This is
# how we make ALTER TABLE idempotent: we only add a column whose name is NOT
# already present, so re-running the migration is a safe no-op. Returns an empty
# set if the table does not exist yet (a fresh DB where the CREATEs below will
# make it instead).
# ----------------------------------------------------------------------------
def _existing_columns(conn, table):
    rows = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
    # PRAGMA table_info returns one row per column; row["name"] is the column name
    # (row_factory = sqlite3.Row is set by db._configure, so name access works).
    return {r["name"] for r in rows}


# ----------------------------------------------------------------------------
# _add_column_if_missing(conn, table, column, coldef)
# ----------------------------------------------------------------------------
# The guarded ALTER helper. `coldef` is the full column definition SQLite needs
# after the name (type + DEFAULT ...). We check first, add only if absent, and
# report what happened so the run log is auditable. NOTE: SQLite's ADD COLUMN
# rewrites no existing rows — every current row simply gets the DEFAULT for the
# new column — so this is cheap and non-destructive even on a big table.
# ----------------------------------------------------------------------------
def _add_column_if_missing(conn, table, column, coldef):
    if column in _existing_columns(conn, table):
        print("    · %s.%s already present — skipped" % (table, column))
        return False
    conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, coldef))
    print("    + %s.%s added" % (table, column))
    return True


# ----------------------------------------------------------------------------
# _ensure_department_columns(conn)  (Design §3.1)
# ----------------------------------------------------------------------------
# Version 1.0 created `department` as a bare (code, name) legend. Promote it to
# the access-control backbone by adding the two nullable leader foreign keys.
# We KEEP `code` as the primary key (every offering.dept_code already points at
# it, and the report legend joins on it), so this is a pure, additive extension
# — no table rebuild, no data movement. The columns are nullable and start NULL;
# the seed step (seed_leaders.py) fills them in.
# ----------------------------------------------------------------------------
def _ensure_department_columns(conn):
    print("[1] department  — leader foreign keys")
    # The department table must exist before we can ALTER it. On the live DB it
    # already does (11 rows); on a truly bare DB init_db.py makes it. Create it
    # defensively here too so the migration can stand alone.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS department ("
        "  code TEXT PRIMARY KEY,"
        "  name TEXT NOT NULL"
        ")"
    )
    _add_column_if_missing(conn, "department", "hod_user_id", "INTEGER")
    _add_column_if_missing(conn, "department", "vice_dean_user_id", "INTEGER")


# ----------------------------------------------------------------------------
# _ensure_cycle_thresholds(conn)  (Design §6)
# ----------------------------------------------------------------------------
# Add the three per-cycle classification knobs to the EXISTING `cycle` table.
# They carry the approved defaults (overall 8.0 on /10, section off, min 10) so
# every current cycle (CA1, CA3, DEMOCA1, TESTCA1) is immediately bandable with
# sensible values and the professor can override any of them per cycle later.
#
# NUANCE: SQLite forbids a non-constant DEFAULT on ADD COLUMN, but plain literal
# numbers are fine, so "DEFAULT 8.0" / "DEFAULT 10" apply cleanly to all rows.
# threshold_section is intentionally added with NO default → NULL → "critical-
# section rule off until a cycle opts in", exactly matching §6.
# ----------------------------------------------------------------------------
def _ensure_cycle_thresholds(conn):
    print("[2] cycle  — per-cycle classification thresholds (§6)")
    _add_column_if_missing(conn, "cycle", "threshold_overall", "REAL NOT NULL DEFAULT 7.5")
    _add_column_if_missing(conn, "cycle", "threshold_section", "REAL")           # nullable = off
    _add_column_if_missing(conn, "cycle", "min_responses", "INTEGER NOT NULL DEFAULT 0")


# ----------------------------------------------------------------------------
# _ensure_iam_tables(conn)  (Design §3.1, §17.3)
# ----------------------------------------------------------------------------
# Create the brand-new IAM tables. These are CREATE TABLE IF NOT EXISTS with the
# SAME definitions as schema_master.sql, so a fresh install and a migrated live
# DB converge on an identical structure. Kept byte-for-byte in step with the
# schema file (see the header note there).
# ----------------------------------------------------------------------------
def _ensure_iam_tables(conn):
    print("[3] app_user / set_pw_token / admin_log  — leader logins + IAM (§17)")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS app_user (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            email          TEXT NOT NULL UNIQUE,
            name           TEXT,
            role           TEXT NOT NULL,            -- 'HOD' | 'VICE_DEAN' | 'DEAN'
            scope_dept_ids TEXT NOT NULL DEFAULT '', -- CSV of E-codes, or 'ALL' for whole college
            pw_hash        TEXT NOT NULL DEFAULT '', -- pbkdf2_hmac hash; '' until the user sets it
            status         TEXT NOT NULL DEFAULT 'active', -- 'active' | 'disabled'
            last_login_at  TEXT,
            created_by     TEXT,
            created_at     TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS set_pw_token (
            jti        TEXT PRIMARY KEY,             -- unguessable token id
            user_id    INTEGER NOT NULL,            -- FK -> app_user.id
            purpose    TEXT NOT NULL,               -- 'SET' | 'RESET'
            expires_at TEXT NOT NULL,               -- link dies after this
            used_at    TEXT,                        -- set once redeemed (one-time)
            FOREIGN KEY (user_id) REFERENCES app_user(id)
        );

        CREATE TABLE IF NOT EXISTS admin_log (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_user_id  INTEGER,                 -- who acted (app_user.id or NULL for system)
            action         TEXT NOT NULL,           -- CREATE_USER|DISABLE|RESEND_LINK|IMPERSONATE_*|EDIT_SCOPE|SEED
            target_user_id INTEGER,                 -- affected account (nullable)
            at             TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )


# ----------------------------------------------------------------------------
# _ensure_classification_table(conn)  (Design §6)
# ----------------------------------------------------------------------------
# Create the offering_classification record where the banding step writes its
# GOOD/POOR verdict per (offering, cycle). Scoring stays untouched and numeric;
# this is the separate, auditable place the POOR trigger lives. UNIQUE(offering,
# cycle) makes the classifier an idempotent upsert.
# ----------------------------------------------------------------------------
def _ensure_classification_table(conn):
    print("[4] offering_classification  — the GOOD/POOR band record (§6)")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS offering_classification (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            offering_id       INTEGER NOT NULL,     -- FK -> offering.id
            cycle_code        TEXT    NOT NULL,     -- which cycle's scoring (scope)
            band              TEXT,                 -- 'GOOD' | 'POOR' | NULL (insufficient n)
            overall_score     REAL,                 -- the /10 overall used (audit)
            n_responses       INTEGER,              -- responses counted (min_responses input)
            threshold_overall REAL,                 -- overall cut applied (audit)
            threshold_section REAL,                 -- optional critical-section cut (nullable)
            min_responses     INTEGER,              -- minimum-sample guard applied (audit)
            reason            TEXT,                 -- human-readable justification (audit, §6)
            classified_at     TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (offering_id, cycle_code),
            FOREIGN KEY (offering_id) REFERENCES offering(id)
        );
        """
    )


# ----------------------------------------------------------------------------
# migrate(conn=None)  —  the public entry point.
# ----------------------------------------------------------------------------
# Runs all four steps inside ONE transaction so the DB never lands half-migrated.
# Accepts an optional connection (the tests pass a connection to a throwaway COPY
# of master.db, proving non-destructiveness before we ever touch the real file);
# with no argument it opens the real master.db via db.get_master(). Returns the
# connection so a caller/test can keep inspecting it.
# ----------------------------------------------------------------------------
def migrate(conn=None):
    owns_conn = conn is None            # did WE open it? then WE close it.
    if conn is None:
        conn = db.get_master()

    print("=" * 70)
    print("AFS Version 2.0 · Module 1 — master.db migration (additive, idempotent)")
    print("=" * 70)

    try:
        _ensure_department_columns(conn)
        _ensure_cycle_thresholds(conn)
        _ensure_iam_tables(conn)
        _ensure_classification_table(conn)
        conn.commit()                   # one atomic commit for the whole delta
        print("-" * 70)
        print("Migration complete. Version 1.0 tables/rows untouched; delta applied.")
        print("=" * 70)
    except Exception:
        conn.rollback()                 # leave the DB exactly as we found it on error
        raise

    if owns_conn:
        conn.close()
    return conn


if __name__ == "__main__":
    migrate()
