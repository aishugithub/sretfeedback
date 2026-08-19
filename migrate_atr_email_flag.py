# ============================================================================
# migrate_atr_email_flag.py  —  add app_user.atr_email_enabled to the LIVE DB
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# The email-preference switch (opt a leader out of the per-ATR endorsement
# mails) is stored as ONE new column, `atr_email_enabled`, on master.db's
# `app_user` table. `schema_master.sql` already declares that column, so any
# BRAND-NEW database gets it automatically. But a database that was created
# BEFORE this change — e.g. the live one already running on PythonAnywhere —
# has an `app_user` table without the column, and `CREATE TABLE IF NOT EXISTS`
# never alters an existing table. This one-shot script closes that gap: it adds
# the column in place, defaulting every existing leader to 1 (= "keep sending",
# so behaviour is unchanged until an admin flips someone off on the Users &
# Roles page).
#
# WHY IT IS SAFE TO RUN ANY NUMBER OF TIMES (idempotent):
#   * It first reads PRAGMA table_info(app_user) to see whether the column is
#     already present. If it is, it does nothing and exits cleanly. So running
#     it twice, or on a fresh DB that already has the column, is harmless.
#   * SQLite's `ALTER TABLE ... ADD COLUMN` is a cheap metadata-only change; it
#     does not rewrite the table or touch existing rows' data.
#
# HOW TO RUN (on the PythonAnywhere Bash console, from the app folder):
#     cd ~/feedback-system/app && python migrate_atr_email_flag.py
# then reload the web app. No data is moved and the two-file anonymity split is
# untouched (this only edits master.db's leader table, never a cycle answer DB).
# ----------------------------------------------------------------------------

import sqlite3                     # standard-library driver (same as the app)
from config import Config          # Config.MASTER_DB — the one master.db path


# The column we are adding, spelled exactly as schema_master.sql declares it so
# a migrated DB is byte-for-byte equivalent to a freshly-created one.
COLUMN_NAME = "atr_email_enabled"
COLUMN_DDL = "INTEGER NOT NULL DEFAULT 1"


def _column_exists(conn, table, column):
    """Return True if `table` already has `column`. We read the table's schema
    via PRAGMA table_info, whose rows expose the column name at index 1 (['name']
    when row_factory is Row, but here we use plain tuples for zero dependencies)."""
    rows = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
    # Each row is (cid, name, type, notnull, dflt_value, pk) — name is row[1].
    return any(r[1] == column for r in rows)


def main():
    # Open the master DB directly (no WAL pragmas needed for a one-off DDL run).
    conn = sqlite3.connect(Config.MASTER_DB)
    try:
        if _column_exists(conn, "app_user", COLUMN_NAME):
            print("[migrate] app_user.%s already exists — nothing to do." % COLUMN_NAME)
            return

        # Add the column. Every existing leader row takes the DEFAULT (1), so no
        # one's notifications change until an admin edits them on Users & Roles.
        conn.execute("ALTER TABLE app_user ADD COLUMN %s %s" % (COLUMN_NAME, COLUMN_DDL))
        conn.commit()

        # Report how many rows now carry the default, purely as a reassurance line.
        n = conn.execute("SELECT COUNT(*) FROM app_user").fetchone()[0]
        print("[migrate] Added app_user.%s (default 1) — %d leader row(s) now "
              "opted IN to ATR emails by default." % (COLUMN_NAME, n))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
