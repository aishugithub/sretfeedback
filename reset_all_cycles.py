# ============================================================================
# reset_all_cycles.py  —  wipe ALL cycles + their tied data, keep everything global
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# After testing, you want a clean slate: remove every cycle and everything TIED to
# a cycle (the allocation/offerings, the student roster, enrollment, the derived
# GOOD/POOR bands, the readiness dismissals, the per-cycle answer/ATR databases, and
# the archived cycle files) — WITHOUT touching the things that live across cycles
# (the Faculty Master, the leader/admin logins, departments, programmes, the
# feedback templates/scales/questions, the academic-year label, and the IAM tables).
# Then you create ONE fresh cycle and upload the real data.
#
# WHAT IT DELETES (all cycle-scoped):
#   master.db tables : cycle, students, roster_range, offering, enrollment,
#                      offering_classification, readiness_dismissal
#   files            : data/cycle_*.db (+ -wal/-shm/-journal/.prev* side files),
#                      archive/*.db  and  archive/Audit_*.pdf
# WHAT IT KEEPS (global): faculty, app_user (leaders+admins), department, programme,
#   category, template, template_version, question, scale, scale_option,
#   academic_year, set_pw_token, admin_log — and activity_log (unless --clear-activity).
#
# SAFETY: it will NOT do anything unless you pass --yes (or preview with --dry-run).
# It is idempotent — running it again on an already-clean system changes nothing.
# Databases never travel through git, so RUN IT ON THE SERVER TOO (same command).
#
# USAGE (from the app/ folder):
#     python reset_all_cycles.py --dry-run        # preview only, touch nothing
#     python reset_all_cycles.py --yes            # do it
#     python reset_all_cycles.py --yes --clear-activity    # also wipe the activity log
#     python reset_all_cycles.py --yes --unlock-templates  # also unlock templates
# ----------------------------------------------------------------------------

import os
import sys
import glob

import db
from config import Config


# The master.db tables to empty, in an order that reads sensibly (children first,
# though there are no cross-table SQL FKs among these, so order is cosmetic).
CYCLE_TABLES = [
    "offering_classification",   # derived bands/marks
    "enrollment",                # elective attachments
    "readiness_dismissal",       # readiness overrides
    "offering",                  # the allocation
    "roster_range",              # the uploaded roster ranges
    "students",                  # the expanded roster
    "cycle",                     # the cycles themselves (last)
]


def _rm(path):
    """Remove a file, returning True if gone, False if it couldn't be removed."""
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def main(argv):
    dry = "--dry-run" in argv
    do = "--yes" in argv
    clear_activity = "--clear-activity" in argv
    unlock_templates = "--unlock-templates" in argv

    if not (dry or do):
        print(__doc__)
        print(">>> Nothing done. Re-run with --dry-run to preview, or --yes to apply.")
        return

    conn = db.get_master()
    try:
        # ---- 1. Count what is about to be removed (for the report) -----------
        print("Cycles and cycle-tied data " + ("that WOULD be removed:" if dry else "being removed:"))
        for t in CYCLE_TABLES:
            n = conn.execute("SELECT COUNT(*) n FROM %s" % t).fetchone()["n"]
            print("   %-26s %d row(s)" % (t, n))
        n_act = conn.execute("SELECT COUNT(*) n FROM activity_log").fetchone()["n"]
        if clear_activity:
            print("   %-26s %d row(s)  (activity log — will be cleared)" % ("activity_log", n_act))
        else:
            print("   activity_log KEPT (%d rows) — pass --clear-activity to wipe it too." % n_act)

        # Files that will be removed.
        cyc_files = sorted(glob.glob(os.path.join(Config.DATA_DIR, "cycle_*.db*")))
        arch_files = sorted(glob.glob(os.path.join(Config.ARCHIVE_DIR, "*.db"))
                            + glob.glob(os.path.join(Config.ARCHIVE_DIR, "Audit_*.pdf")))
        print("   per-cycle db files : %d" % len(cyc_files))
        print("   archive files      : %d" % len(arch_files))

        # ---- template lock status (informational / optional unlock) ---------
        try:
            locked = conn.execute(
                "SELECT COUNT(*) n FROM template_version WHERE is_locked=1").fetchone()["n"]
        except Exception:
            locked = 0
        if locked:
            if unlock_templates:
                print("   template_version   : %d locked -> will be UNLOCKED" % locked)
            else:
                print("   NOTE: %d template version(s) are LOCKED from testing. They stay "
                      "locked; add --unlock-templates to make questions editable again." % locked)

        if dry:
            print("\n--dry-run: nothing was changed.")
            return

        # ---- 2. Delete the cycle-scoped rows --------------------------------
        for t in CYCLE_TABLES:
            conn.execute("DELETE FROM %s" % t)
        if clear_activity:
            conn.execute("DELETE FROM activity_log")
        if unlock_templates and locked:
            conn.execute("UPDATE template_version SET is_locked=0 WHERE is_locked=1")
        conn.commit()
        # Reclaim space and reset AUTOINCREMENT high-water marks for a tidy fresh start.
        try:
            conn.execute("DELETE FROM sqlite_sequence")
            conn.commit()
            conn.execute("VACUUM")
        except Exception:
            pass

        # ---- 3. Remove the per-cycle db files and the archive ---------------
        failed = []
        for f in cyc_files + arch_files:
            if not _rm(f):
                failed.append(f)

        print("\nDone. All cycles and cycle-tied data removed; global data kept "
              "(faculty, leaders/admins, departments, programmes, templates, "
              "academic year, IAM).")
        if failed:
            print("\nThese files could not be auto-deleted (delete them manually — they "
                  "are harmless orphans, referenced by no cycle):")
            for f in failed:
                print("   " + f)
        print("\nYou can now create ONE fresh cycle and upload the real roster + allocation.")
    finally:
        conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])
