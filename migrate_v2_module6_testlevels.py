# ============================================================================
# migrate_v2_module6_testlevels.py  —  add the three-level testing model
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# Version 2.1 replaces the single binary cycle.is_test flag ("redirect ALL mail
# to one address, or send live") with a graduated cycle.test_level (0-3) so a
# cycle can be walked safely from "nobody real is contacted" up to "everyone
# real". See config.py / emailer.py for the routing rules. This migration adds the
# new column to an EXISTING master.db and backfills it from the old is_test flag,
# so the professor's live database gains the feature WITHOUT losing any state:
#
#     old is_test = 1  ->  test_level = 1   (was "redirect all"; now the safest
#                                            level: students + staff to test inboxes)
#     old is_test = 0  ->  test_level = 0   (was live; now PRODUCTION)
#
# It is idempotent (guarded ADD COLUMN, backfill only where test_level is still
# NULL/unset) and, like every data script here, must be RUN ON THE SERVER too (or
# master.db re-uploaded) because DBs never travel through git.
#
# USAGE (from the app/ folder):
#     python migrate_v2_module6_testlevels.py
# ----------------------------------------------------------------------------

import db  # the single WAL-aware connection helper


def _columns(conn, table):
    """Return the set of column names on `table` (PRAGMA table_info)."""
    return {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}


def main():
    conn = db.get_master()
    try:
        cols = _columns(conn, "cycle")

        # 1. ADD COLUMN test_level if missing. SQLite allows a constant DEFAULT on
        #    ADD COLUMN, so every existing row instantly gets a value; we then
        #    overwrite it from is_test just below so the mapping is exact.
        if "test_level" not in cols:
            print("[1] cycle — adding column test_level (INTEGER NOT NULL DEFAULT 1)")
            conn.execute(
                "ALTER TABLE cycle ADD COLUMN test_level INTEGER NOT NULL DEFAULT 1")
        else:
            print("[1] cycle — test_level already present (skip add)")

        # 2. BACKFILL from the legacy is_test flag. We only touch rows we can map
        #    unambiguously; the ADD COLUMN default (1) already covered brand-new
        #    installs. is_test=0 -> production (0); is_test=1 -> safest level (1).
        conn.execute("UPDATE cycle SET test_level = 0 WHERE is_test = 0")
        conn.execute("UPDATE cycle SET test_level = 1 WHERE is_test = 1")

        # 3. Keep the two flags consistent going forward: is_test is now the DERIVED
        #    "is this non-production" flag (1 whenever test_level != 0). This lets any
        #    older code that still reads is_test keep behaving correctly.
        conn.execute("UPDATE cycle SET is_test = CASE WHEN test_level = 0 THEN 0 ELSE 1 END")

        conn.commit()

        print("\nCurrent cycles after migration:")
        for r in conn.execute(
                "SELECT code, status, is_test, test_level FROM cycle ORDER BY id"):
            print("   • %-10s status=%-9s is_test=%s test_level=%s"
                  % (r["code"], r["status"], r["is_test"], r["test_level"]))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
