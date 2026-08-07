# ============================================================================
# migrate_v2_module4_distribution.py  —  V2.0 · Module 4 migration (ADDITIVE)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# Module 4 surfaces the result-distribution job (distribution.py) in the admin
# console: a "Results / Distribution" page where the professor sets the POOR
# threshold, runs classification, reviews the teacher list, edits the faculty
# email preamble, and sends. The engine already existed; only the UI was missing.
#
# The one DATA change it needs is a place to persist that editable email preamble
# per cycle — the new `cycle.dist_intro` column (the STUDENT invitation text lives
# in `cycle.email_body`; this is the FACULTY results-email preamble, kept separate
# so the two never collide). Everything else Module 4 needs already exists
# (threshold_overall / _section / min_responses were added by Module 1).
#
# This is the EXISTING-DB counterpart of the schema file, in the same idempotent,
# additive style as the earlier migrations: run it once, twice, ten times → the
# same end state, no error, no data touched.
#
# WHAT IT ADDS:
#   1. cycle.dist_intro   (guarded ALTER)  — the faculty-email preamble text
#
# Usage (from inside the app/ folder):  python migrate_v2_module4_distribution.py
# ----------------------------------------------------------------------------

import db  # the ONE place that opens master.db (WAL, FK enforcement, two-file split)


def _existing_columns(conn, table):
    """Column names currently on `table`, so an ALTER can be made idempotent."""
    return {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}


def _add_column_if_missing(conn, table, column, coldef):
    """Add one column only if it is absent; report what happened for the run log."""
    if column in _existing_columns(conn, table):
        print("    · %s.%s already present — skipped" % (table, column))
        return False
    conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, coldef))
    print("    + %s.%s added" % (table, column))
    return True


def migrate(conn=None):
    """Add cycle.dist_intro in one transaction. Accepts an optional connection
    (tests pass a throwaway copy); with none it opens the real master.db."""
    owns_conn = conn is None
    if conn is None:
        conn = db.get_master()

    print("=" * 70)
    print("AFS Version 2.0 · Module 4 — master.db migration (additive, idempotent)")
    print("=" * 70)
    try:
        print("[1] cycle.dist_intro  — faculty result-email preamble (§7)")
        _add_column_if_missing(conn, "cycle", "dist_intro", "TEXT")
        conn.commit()
        print("-" * 70)
        print("Migration complete. Existing tables/rows untouched; column added.")
        print("=" * 70)
    except Exception:
        conn.rollback()
        raise

    if owns_conn:
        conn.close()
    return conn


if __name__ == "__main__":
    migrate()
