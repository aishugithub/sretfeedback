# ============================================================================
# check_elective_grouping.py  —  DRY-RUN inspector for elective consolidation
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# This is a READ-ONLY diagnostic, not part of the running web app. It shows how
# consolidation.py groups a cycle's responded offerings into "deliveries" BEFORE
# you send anything — so you can eyeball, on real data, that each shared elective
# is pooled correctly (all its programme codes under one anchor) and that no CORE
# course (or a section-split course) was merged by mistake.
#
# It reads the SAME data the app reads (master.db for offering identities, the
# per-cycle DB for which offerings collected responses) and writes NOTHING. It is
# safe to run against a live cycle at any time.
#
# USAGE (from the app/ folder, same place run.py lives):
#     python check_elective_grouping.py <CYCLE_CODE>
#   e.g.
#     python check_elective_grouping.py CA1
# If you omit the code it lists the available cycles and picks the first one.
# ----------------------------------------------------------------------------

import sys

import db                     # the app's DB helpers (get_master / get_cycle)
import consolidation          # the grouping rule under test


def main():
    # ---- Resolve which cycle to inspect -------------------------------------
    wanted = sys.argv[1].strip() if len(sys.argv) > 1 else None

    master = db.get_master()
    cycles = master.execute(
        "SELECT * FROM cycle ORDER BY id").fetchall()
    if not cycles:
        print("No cycles found in master.db."); return

    if wanted:
        cyc = next((c for c in cycles if c["code"] == wanted), None)
        if cyc is None:
            print("Cycle %r not found. Available: %s"
                  % (wanted, ", ".join(c["code"] for c in cycles)))
            return
    else:
        # No code given: show the menu and default to the first cycle.
        print("Cycles available: %s" % ", ".join(c["code"] for c in cycles))
        cyc = cycles[0]
        print("No cycle code given — defaulting to %r.\n" % cyc["code"])

    # ---- Open the per-cycle answer DB (needed to know which offerings responded)
    path = db.cycle_db_path(cyc["academic_year"], cyc["code"])
    import os
    if not os.path.exists(path):
        print("No per-cycle DB for %s yet (no responses collected)." % cyc["code"])
        return
    cy = db.get_cycle(cyc["academic_year"], cyc["code"])

    # ---- Build the deliveries exactly as classification/distribution do -------
    groups = consolidation.deliveries_with_responses(master, cy, cyc["code"])

    # ---- Print a readable report --------------------------------------------
    n_deliv = len(groups)
    n_elec = sum(1 for g in groups.values() if g["is_elective"])
    n_pooled = sum(1 for g in groups.values() if len(g["oids"]) > 1)
    print("=" * 74)
    print("DELIVERIES for cycle %s  (%d total; %d elective; %d actually pooled)"
          % (cyc["code"], n_deliv, n_elec, n_pooled))
    print("=" * 74)

    for anchor, g in groups.items():
        kind = "ELECTIVE" if g["is_elective"] else "single"
        progs = ", ".join(g["dept_codes"]) or "-"
        pooled = " <== POOLED %d offerings" % len(g["oids"]) if len(g["oids"]) > 1 else ""
        print("anchor #%-5s [%-8s] %-12s  %-30s"
              % (anchor, kind, g["course_code"] or "(no code)",
                 (g["course_name"] or "")[:30]))
        print("     faculty : %s" % (g["faculty"] or "-"))
        print("     programmes: %-20s  member offering_ids: %s%s"
              % (progs, g["oids"], pooled))
        if g["basket"]:
            print("     basket  : %s" % g["basket"])
        print("")

    # ---- A focused sanity list: every delivery that pooled >1 offering -------
    print("-" * 74)
    print("POOLED electives (these will now be ONE report / band / ATR each):")
    any_pooled = False
    for anchor, g in groups.items():
        if len(g["oids"]) > 1:
            any_pooled = True
            print("  %-28s  %s  ->  anchor #%s"
                  % ((g["course_code"] or g["course_name"] or "")[:28],
                     ", ".join(g["dept_codes"]), anchor))
    if not any_pooled:
        print("  (none — no elective in this cycle has responses from >1 programme yet)")
    print("-" * 74)

    cy.close()
    master.close()


if __name__ == "__main__":
    main()
