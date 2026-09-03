# ============================================================================
# cycle_back_to_test.py  —  move a PRODUCTION cycle back to TEST LEVEL 1
# ============================================================================
# WHY THIS EXISTS
#   A cycle is promoted to production (test_level=0, is_test=0) only via the
#   admin "Purge & promote to production" action, and the Cycles page then HIDES
#   the "Set level" control for a level-0 cycle by design (production is meant to
#   be a one-way door). But sometimes — e.g. after new changes land and you want
#   one more regression pass at Level 1 before the real run — you need to walk it
#   BACK to a test level. There is deliberately no UI button for that, so this
#   one-off maintenance script does it safely.
#
# WHAT IT CHANGES (and nothing else):
#   cycle.test_level : 0 -> 1        (Level 1 = the safest test level; ALL email
#                                     is redirected to the test inboxes)
#   cycle.is_test    : 0 -> 1        (kept in lock-step, exactly as the app's own
#                                     cycles_set_test_level route does)
#   It does NOT touch the roster, offerings, faculty, templates, the cycle's
#   email text/thresholds, its per-cycle feedback DB, or any other cycle. It is
#   the precise inverse of the two flag lines the purge flips — nothing more.
#
# THE SAFETY GUARD (important):
#   Going back to a test level is only safe when the cycle has NOT yet collected
#   real production feedback — because your NEXT "Purge & promote to production"
#   will WIPE the per-cycle feedback DB. So this script REFUSES to run if:
#     * the cycle is currently OPEN (close it first), or
#     * its per-cycle DB already holds any responses (real feedback at risk).
#   You can override with a typed confirmation only if you are certain (see below).
#
# ANONYMITY: reads the per-cycle DB only to COUNT responses (no answer content is
#   read or joined to anyone); writes only to master.db's cycle row.
#
# USAGE (on PythonAnywhere, from ~/feedback-system):
#     python cycle_back_to_test.py CA1
#   Omit the code to auto-pick when there is exactly one non-archived cycle:
#     python cycle_back_to_test.py
#   Override the "has responses" guard ONLY if you are sure those responses are
#   disposable test data you intend to purge anyway:
#     python cycle_back_to_test.py CA1 --force
#   Then Reload the web app. After this, the Cycles page shows the cycle at
#   TEST L1 again, with the Set-level and Purge controls back.
# ============================================================================

import os
import sys

import db


def _pick_cycle(conn, code):
    """Resolve the target cycle row. With an explicit code, use it. Without one,
    auto-pick when exactly one non-archived cycle exists; otherwise list them and
    stop, so we never guess which cycle to touch."""
    if code:
        row = conn.execute("SELECT * FROM cycle WHERE code=?", (code,)).fetchone()
        if row is None:
            print("No cycle with code %r found. Known cycles:" % code)
            for r in conn.execute("SELECT code,label,status FROM cycle ORDER BY id"):
                print("   - %s (%s) [%s]" % (r["code"], r["label"], r["status"]))
        return row
    rows = conn.execute(
        "SELECT * FROM cycle WHERE status != 'ARCHIVED' ORDER BY id").fetchall()
    if len(rows) == 1:
        return rows[0]
    print("Please pass the cycle code explicitly (more than one non-archived "
          "cycle exists):")
    for r in rows:
        print("   python cycle_back_to_test.py %s" % r["code"])
    return None


def _response_count(cycle_row):
    """How many responses are in this cycle's per-cycle DB (0 if the file is
    absent or empty). Used purely as a safety gate."""
    path = db.cycle_db_path(cycle_row["academic_year"], cycle_row["code"])
    if not os.path.exists(path):
        return 0
    cy = db.get_cycle(cycle_row["academic_year"], cycle_row["code"])
    try:
        return cy.execute("SELECT COUNT(*) FROM response").fetchone()[0]
    except Exception:
        return 0                       # table missing on a brand-new empty file
    finally:
        cy.close()


def main():
    args = [a for a in sys.argv[1:]]
    force = "--force" in args
    codes = [a for a in args if not a.startswith("--")]
    code = codes[0] if codes else None

    conn = db.get_master()
    try:
        c = _pick_cycle(conn, code)
        if c is None:
            return

        n_resp = _response_count(c)
        print("=" * 66)
        print("Cycle %s (%s)" % (c["code"], c["label"]))
        print("  current: test_level=%s  is_test=%s  status=%s  is_open=%s"
              % (c["test_level"], c["is_test"], c["status"], c["is_open"]))
        print("  responses in per-cycle DB: %d" % n_resp)
        print("-" * 66)

        if c["test_level"] == 1 and c["is_test"] == 1:
            print("Already at Test Level 1 — nothing to do.")
            return

        # GUARD 1 — never flip an OPEN cycle (real students could be answering).
        if c["is_open"]:
            print("REFUSED: the cycle is OPEN. Close it on the Cycles page first,")
            print("then re-run this script.")
            return

        # GUARD 2 — never risk real production feedback. Going to test then
        # re-purging wipes the per-cycle DB, so refuse when responses exist.
        if n_resp > 0 and not force:
            print("REFUSED: this cycle already has %d response(s) in its per-cycle" % n_resp)
            print("DB. Moving it back to test and later purging would DELETE that")
            print("feedback. If those responses are disposable test data you intend")
            print("to purge anyway, re-run with --force:")
            print("      python cycle_back_to_test.py %s --force" % c["code"])
            return

        # The one change: level 0 -> 1, is_test 0 -> 1. (status/is_open are already
        # DRAFT/closed after a purge; we leave them untouched.)
        conn.execute("UPDATE cycle SET test_level=1, is_test=1 WHERE id=?", (c["id"],))
        conn.commit()

        after = conn.execute("SELECT test_level,is_test,status,is_open FROM cycle "
                             "WHERE id=?", (c["id"],)).fetchone()
        print("DONE. %s is now: test_level=%s  is_test=%s  status=%s  is_open=%s"
              % (c["code"], after["test_level"], after["is_test"],
                 after["status"], after["is_open"]))
        print("-" * 66)
        print("Next: Reload the web app, then on the Cycles page Open the cycle,")
        print("generate tokens and run your Level-1 regression (all mail is")
        print("redirected to the test inboxes). When it passes, use")
        print("'Purge & promote to production' to wipe the test feedback and go")
        print("live at Level 0 again.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
