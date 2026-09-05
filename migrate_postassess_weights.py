# ============================================================================
# migrate_postassess_weights.py
# ----------------------------------------------------------------------------
# ONE-TIME (idempotent) DATA FIX — correct the POST_ASSESS scale weights
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# The Theory "answer key discussed after CA1" question uses the POST_ASSESS
# scale. Live scoring reads each option's /10 weight from master.db ->
# scale_option.weight (the "Discussed Late" weight additionally flows through
# scoring.get_discussed_late_weight(), which every report/classification caller
# already passes into the scorer). Because a student's stored answer is the
# option LABEL, changing a weight here re-scores every past and future report
# automatically on next generation — no per-cycle data migration, nothing lost.
#
# WHAT IT CHANGES (approved 2026-09-05)
# ----------------------------------------------------------------------------
# The approved formula scored "Not Discussed" = 1 and left "Discussed Late" at 0
# (an unresolved open item), so "never discussed at all" (1) perversely outscored
# "discussed, just late" (0). We correct the order to:
#       Discussed Completely 10 > Partially Discussed 6 > Discussed Late 1 > Not Discussed 0
# i.e. only two weights move:
#       Discussed Late : 0/NULL -> 1
#       Not Discussed  : 1      -> 0
# Discussed Completely (10) and Partially Discussed (6) are unchanged.
#
# SAFE TO RE-RUN: it sets the target weights by (scale_code, label); a second run
# finds them already correct and changes nothing. It backs up the DB file first.
#
# USAGE
#   python migrate_postassess_weights.py                 # app/data/master.db
#   python migrate_postassess_weights.py data/master.db  # explicit (server)
# ============================================================================

import os
import sys
import sqlite3
import shutil
from datetime import datetime

# Intended final state for the POST_ASSESS scale: label -> correct /10 weight.
TARGET_POST_ASSESS = {
    "Discussed Completely": 10.0,
    "Partially Discussed": 6.0,
    "Discussed Late": 1.0,      # corrected: was 0/NULL
    "Not Discussed": 0.0,       # corrected: was 1.0
}


def _default_db_path():
    base = os.path.dirname(os.path.abspath(__file__))   # the app/ folder
    return os.path.join(base, "data", "master.db")


def main(db_path):
    if not os.path.exists(db_path):
        print("ERROR: master.db not found at:", db_path)
        return 2

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{db_path}.bak-postassess-{ts}"
    shutil.copy2(db_path, backup)
    print("Backup written:", backup)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT id FROM scale WHERE code = 'POST_ASSESS'").fetchone()
        if row is None:
            print("ERROR: no scale with code 'POST_ASSESS' in this DB — nothing to do.")
            return 3
        scale_id = row["id"]

        print("\nBEFORE (POST_ASSESS weights):")
        for r in con.execute(
                "SELECT label, weight FROM scale_option WHERE scale_id = ? "
                "ORDER BY display_order", (scale_id,)).fetchall():
            print(f"   {r['label']:<22} {r['weight']}")

        changed = 0
        for label, target in TARGET_POST_ASSESS.items():
            cur = con.execute(
                "UPDATE scale_option SET weight = ? "
                "WHERE scale_id = ? AND label = ? AND (weight IS NULL OR weight <> ?)",
                (target, scale_id, label, target))
            if cur.rowcount:
                changed += cur.rowcount
        con.commit()

        print("\nAFTER (POST_ASSESS weights):")
        ok = True
        for r in con.execute(
                "SELECT label, weight FROM scale_option WHERE scale_id = ? "
                "ORDER BY display_order", (scale_id,)).fetchall():
            tgt = TARGET_POST_ASSESS.get(r["label"])
            bad = tgt is None or abs((r["weight"] if r["weight"] is not None else -1) - tgt) > 1e-9
            if bad:
                ok = False
            print(f"   {r['label']:<22} {r['weight']}{'  <-- UNEXPECTED' if bad else ''}")

        print("\nRESULT:", f"updated {changed} weight(s)." if changed
              else "already correct — no rows changed (safe re-run).")
        print("Verification:", "PASS" if ok else "FAIL")
        return 0 if ok else 4
    finally:
        con.close()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else _default_db_path()
    print("Target DB:", path)
    raise SystemExit(main(path))
