# ============================================================================
# migrate_swap_agree_weights.py
# ----------------------------------------------------------------------------
# ONE-TIME (idempotent) DATA FIX — correct the AGREE5 scale weights in master.db
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# Live scoring reads each option's /10 weight straight from the master.db table
# `scale_option` (see scoring.load_offering_dataset -> the `so.weight` column).
# A student's stored answer is the option LABEL (e.g. "Agree"), never a number,
# so changing a label's weight in this table re-scores every past and future
# report automatically the next time it is generated — there is NO need to touch
# the anonymous per-cycle answer files, and no data is lost.
#
# WHAT IT CHANGES
# ----------------------------------------------------------------------------
# The four originally-approved workbooks scored "Moderately Agree" = 8 and plain
# "Agree" = 6, i.e. a *milder* positive outranked a firm agreement. That is
# backwards from the plain meaning and from the form's own option order
# (Strongly Agree, Agree, Moderately Agree, Disagree, Strongly Disagree). This
# migration swaps just those two weights on the shared AGREE5 scale:
#       Agree            : 6  ->  8
#       Moderately Agree : 8  ->  6
# Every other option (Strongly Agree 10, Disagree 4, Strongly Disagree 1) and
# every other scale (SYLLABUS, POST_ASSESS, the AE_* scales) is left untouched.
#
# WHY IT IS SAFE TO RE-RUN
# ----------------------------------------------------------------------------
# It sets the weights to their target values by (scale_code, label). Running it a
# second time finds them already correct and reports "already correct" without
# changing anything. It also makes a timestamped backup copy of the DB file
# before writing, so the pre-change state is always recoverable.
#
# USAGE
# ----------------------------------------------------------------------------
#   python migrate_swap_agree_weights.py                # uses app/data/master.db
#   python migrate_swap_agree_weights.py /path/master.db  # explicit DB (server)
# On PythonAnywhere the file lives at ~/feedback-system/data/master.db, so from
# ~/feedback-system run:  python migrate_swap_agree_weights.py data/master.db
# ============================================================================

import os
import sys
import sqlite3
import shutil
from datetime import datetime

# The intended final state: label -> correct /10 weight, for the AGREE5 scale.
# Only the two swapped labels strictly need changing, but listing all five makes
# the migration self-documenting and lets it *assert* the whole scale is right.
TARGET_AGREE5 = {
    "Strongly Agree": 10.0,
    "Agree": 8.0,               # corrected: was 6.0
    "Moderately Agree": 6.0,    # corrected: was 8.0
    "Disagree": 4.0,
    "Strongly Disagree": 1.0,
}


def _default_db_path():
    """Locate app/data/master.db relative to this file (matches config.MASTER_DB)."""
    base = os.path.dirname(os.path.abspath(__file__))   # the app/ folder
    return os.path.join(base, "data", "master.db")


def main(db_path):
    if not os.path.exists(db_path):
        print("ERROR: master.db not found at:", db_path)
        return 2

    # ---- Safety backup: copy the DB file before any write. -------------------
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{db_path}.bak-agreeswap-{ts}"
    shutil.copy2(db_path, backup)
    print("Backup written:", backup)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        # Find the AGREE5 scale id. If the DB has no such scale (unseeded), stop.
        row = con.execute("SELECT id FROM scale WHERE code = 'AGREE5'").fetchone()
        if row is None:
            print("ERROR: no scale with code 'AGREE5' in this DB — nothing to do.")
            return 3
        scale_id = row["id"]

        # Show the BEFORE state for the audit trail.
        print("\nBEFORE (AGREE5 weights):")
        before = con.execute(
            "SELECT label, weight FROM scale_option WHERE scale_id = ? "
            "ORDER BY display_order", (scale_id,)).fetchall()
        for r in before:
            print(f"   {r['label']:<20} {r['weight']}")

        # Apply the target weights label-by-label. UPDATE ... WHERE label matches
        # is inherently idempotent — a re-run simply writes the same values.
        changed = 0
        for label, target in TARGET_AGREE5.items():
            cur = con.execute(
                "UPDATE scale_option SET weight = ? "
                "WHERE scale_id = ? AND label = ? AND (weight IS NULL OR weight <> ?)",
                (target, scale_id, label, target))
            if cur.rowcount:
                changed += cur.rowcount
        con.commit()

        # Show the AFTER state and confirm it matches the target exactly.
        print("\nAFTER (AGREE5 weights):")
        after = con.execute(
            "SELECT label, weight FROM scale_option WHERE scale_id = ? "
            "ORDER BY display_order", (scale_id,)).fetchall()
        ok = True
        for r in after:
            tgt = TARGET_AGREE5.get(r["label"])
            flag = "" if (tgt is not None and abs((r["weight"] or -1) - tgt) < 1e-9) else "  <-- UNEXPECTED"
            if flag:
                ok = False
            print(f"   {r['label']:<20} {r['weight']}{flag}")

        if changed == 0:
            print("\nRESULT: already correct — no rows changed (safe re-run).")
        else:
            print(f"\nRESULT: updated {changed} weight(s).")
        print("Verification:", "PASS" if ok else "FAIL")
        return 0 if ok else 4
    finally:
        con.close()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else _default_db_path()
    print("Target DB:", path)
    raise SystemExit(main(path))
