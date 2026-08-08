# ============================================================================
# migrate_defaults_7_5_0.py  —  one-off data update: institution default band
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# The professor changed the INSTITUTION-WIDE defaults for the GOOD/POOR banding:
#   * POOR when overall < 7.5   (was 8.0)
#   * minimum responses = 0     (was 10 — i.e. band EVERY scored course, no
#                                tiny-sample guard)
#
# Two things had to change for that to take effect everywhere:
#   1. CODE defaults — done in schema_master.sql / migrate_v2_module1.py (fresh
#      installs) and admin/cycles.py + classification.py (new cycles + fallbacks).
#   2. EXISTING DATA — the cycle rows ALREADY in the live master.db still carry
#      the OLD 8.0 / 10 values that were written when those columns were first
#      added (SQLite cannot retro-change a stored value by editing a DEFAULT).
#
# THIS SCRIPT does (2): it rewrites the stored per-cycle thresholds to the new
# defaults — but ONLY for rows that still hold the UNTOUCHED old default pair
# (threshold_overall = 8.0 AND min_responses = 10). Any cycle where the professor
# deliberately typed a different number is LEFT ALONE, so a one-off "make the new
# defaults real for my current cycles" never silently clobbers a considered choice.
#
# It is idempotent: re-running it changes nothing once the rows already read 7.5/0.
# Data lives outside git (master.db is git-ignored), so — like the other data
# scripts — this must be RUN ON THE SERVER too (or the db re-uploaded) to take
# effect there; running it on the laptop only updates the laptop's master.db.
#
# USAGE (from the app/ folder, same place run.py lives):
#     python migrate_defaults_7_5_0.py            # apply
#     python migrate_defaults_7_5_0.py --dry-run  # preview, write nothing
# ----------------------------------------------------------------------------

import sys

import db  # the single connection helper (WAL + row factory), never raw sqlite3


# The exact "untouched old default" pair we are willing to overwrite. We compare
# against these so a hand-set value (e.g. a cycle the professor pinned at 8.0 with
# min 5) is never mistaken for an un-migrated default and rewritten.
OLD_OVERALL = 8.0
OLD_MIN = 10

# The new institution defaults we migrate those rows to.
NEW_OVERALL = 7.5
NEW_MIN = 0


def main(dry_run: bool) -> None:
    conn = db.get_master()  # permanent identity/config db — where the cycle rows live
    try:
        # Find every cycle still carrying BOTH old defaults together. Using a
        # tolerance on the float compare (8.0 stored as REAL) avoids a 7.9999 miss.
        rows = conn.execute(
            "SELECT id, code, academic_year, threshold_overall, min_responses "
            "FROM cycle "
            "WHERE ABS(threshold_overall - ?) < 1e-9 AND min_responses = ?",
            (OLD_OVERALL, OLD_MIN),
        ).fetchall()

        if not rows:
            print("Nothing to migrate — no cycle still holds the old 8.0 / 10 "
                  "default pair. (Already migrated, or all values are custom.)")
            return

        print("Cycles that will move from 8.0 / 10  ->  7.5 / 0:")
        for r in rows:
            print("   • %s  (%s)  id=%s" % (r["code"], r["academic_year"], r["id"]))

        if dry_run:
            print("\n--dry-run: no changes written.")
            return

        # Apply the update to exactly those rows (by id, so the WHERE cannot drift).
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            "UPDATE cycle SET threshold_overall = ?, min_responses = ? "
            "WHERE id IN (%s)" % placeholders,
            [NEW_OVERALL, NEW_MIN] + ids,
        )
        conn.commit()
        print("\nUpdated %d cycle(s) to the new institution default (POOR < 7.5, "
              "min responses 0)." % len(ids))
    finally:
        conn.close()


if __name__ == "__main__":
    # A tiny, dependency-free flag parse so the script needs no argparse ceremony.
    main(dry_run=("--dry-run" in sys.argv))
