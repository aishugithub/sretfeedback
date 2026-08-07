# ============================================================================
# verify_v2_module1.py  —  THE VERIFICATION GATE for Version 2.0 · Module 1
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# verify_scoring.py proves the FROZEN scoring engine is exact. This is its
# Version 2.0 sibling: it proves the Module 1 delta (banding, RBAC, the IAM
# migration, the leader seed) behaves exactly as Design §4/§6/§17 require, BEFORE
# any of it is trusted against the professor's live data.
#
# Everything here runs against a THROWAWAY COPY of master.db (+ the cycle DBs) in
# a temp folder — we point Config.DATA_DIR / Config.MASTER_DB at the copy, so not
# one statement in this file can touch the real database. That copy is also how
# we prove the migration is NON-DESTRUCTIVE: we record Version 1.0 row counts,
# migrate, and assert they are unchanged.
#
# WHAT IT CHECKS (each prints OK / XX, and the run ends ALL PASS or FAILURES):
#   A. band()            — §6 thresholds, the min_responses guard, the None case.
#   B. rbac              — E01 HOD blocked from E02; VD sees its subset; Dean all.
#   C. migration         — idempotent (run twice, no error, no dup columns);
#                          non-destructive (V1 offering/student/cycle counts hold).
#   D. seed_leaders      — 13 accounts, correct roles/scopes, org tree wired,
#                          and idempotent (re-run adds nothing).
#   E. classification    — record_band upsert is idempotent; classify_cycle runs
#                          end-to-end on a real cycle and tallies consistently.
#
# Run with:  python verify_v2_module1.py   (from the app/ folder).
# ----------------------------------------------------------------------------

import os
import shutil
import sqlite3
import tempfile

# IMPORTANT: redirect the app's DB paths to a temp COPY *before* we open anything
# through db.get_master(). Config attributes are read at call time, so reassigning
# them here transparently reroutes every db helper to the sandbox copy.
from config import Config

# The real data dir (source of the copy) — resolved from the app/ location, the
# same anchor Config itself uses, so this works wherever the project lives.
_REAL_DATA_DIR = Config.DATA_DIR
_REAL_MASTER = Config.MASTER_DB

# Build a private sandbox and copy the live DBs into it.
_SANDBOX = tempfile.mkdtemp(prefix="afs_v2m1_verify_")
shutil.copy2(_REAL_MASTER, os.path.join(_SANDBOX, "master.db"))
for _f in os.listdir(_REAL_DATA_DIR):
    # Copy the per-cycle DB files too (needed for the classify_cycle test). We
    # skip WAL/SHM sidecars and the .prev backups — a plain master/cycle .db copy
    # is a consistent snapshot for read-mostly verification.
    if _f.startswith("cycle_") and _f.endswith(".db"):
        shutil.copy2(os.path.join(_REAL_DATA_DIR, _f), os.path.join(_SANDBOX, _f))

# Reroute the whole app at the sandbox for the rest of this process.
Config.DATA_DIR = _SANDBOX
Config.MASTER_DB = os.path.join(_SANDBOX, "master.db")

# Now it is safe to import the modules under test (they read Config at call time).
import db                      # noqa: E402  — must follow the Config reroute above
import rbac                    # noqa: E402
import classification          # noqa: E402
import migrate_v2_module1      # noqa: E402
import seed_leaders            # noqa: E402


# ----------------------------------------------------------------------------
# Tiny test harness (same spirit as verify_scoring.py: print OK/XX, tally, and
# end with ALL PASS / FAILURES). Kept dependency-free — no pytest needed.
# ----------------------------------------------------------------------------
_RESULTS = {"pass": 0, "fail": 0}


def check(label, condition, detail=""):
    ok = bool(condition)
    _RESULTS["pass" if ok else "fail"] += 1
    status = "OK " if ok else "XX "
    line = "   [%s] %s" % (status, label)
    if detail:
        line += "  — %s" % detail
    print(line)
    return ok


# ############################################################################
# SECTION A — band()  (Design §6)
# ############################################################################
def test_band():
    print("\nA. band() — §6 thresholds, guard, and the None case")

    # Common cycle settings: overall cut 8.0, section rule off, guard 10.
    THR, MINN = 8.0, 10

    # GOOD: healthy overall, enough responses, section rule off.
    check("overall 8.50 (>=8.0), n=20 -> GOOD",
          classification.band(8.50, 20, THR, None, [8.4, 9.0], MINN) == "GOOD",
          "clearly-good offering bands GOOD")

    # POOR by overall: 7.50 < 8.0.
    check("overall 7.50 (<8.0), n=20 -> POOR",
          classification.band(7.50, 20, THR, None, [7.0, 8.0], MINN) == "POOR",
          "the single ATR trigger fires")

    # GUARD: too few responses -> None even though overall is low.
    check("overall 5.00 but n=5 (<10) -> None",
          classification.band(5.00, 5, THR, None, [5.0], MINN) is None,
          "tiny sample is NOT banded (guard)")

    # BOUNDARY on the overall cut: exactly 8.0 is NOT < 8.0 -> GOOD.
    check("overall exactly 8.00 -> GOOD (strict <)",
          classification.band(8.00, 15, THR, None, [8.0], MINN) == "GOOD",
          "threshold is strict less-than")

    # BOUNDARY on the guard: n exactly == min_responses IS banded.
    check("n exactly == min_responses (10) -> banded, not None",
          classification.band(9.00, 10, THR, None, [9.0], MINN) == "GOOD",
          "guard is n >= min, inclusive")

    # SECTION rule ON: overall fine (9.0) but a section (7.0) below the floor 7.5.
    check("section 7.0 < threshold_section 7.5 -> POOR",
          classification.band(9.00, 20, THR, 7.5, [9.5, 7.0, 8.8], MINN) == "POOR",
          "critical-section rule can flag an otherwise-good overall")

    # SECTION rule OFF (None): a low section is ignored -> GOOD.
    check("section 3.0 but threshold_section None -> GOOD",
          classification.band(9.00, 20, THR, None, [9.5, 3.0], MINN) == "GOOD",
          "section rule off unless the cycle sets it")

    # Accepts scoring.py's dict-shaped sections too (not just bare numbers).
    dict_sections = [{"key": "faculty", "score": 7.0},
                     {"key": "syllabus", "score": 9.0}]
    check("dict-shaped section_scores accepted",
          classification.band(9.00, 20, THR, 7.5, dict_sections, MINN) == "POOR",
          "runner can pass result['section_scores'] straight in")


# ############################################################################
# SECTION B — rbac  (Design §4)
# ############################################################################
def test_rbac_pure():
    print("\nB. rbac — the §4 scope predicate (E01 cannot see E02)")

    hod_e01 = {"role": "HOD", "scope_dept_ids": "E01"}
    vd_subset = {"role": "VICE_DEAN", "scope_dept_ids": "E01,E03"}
    vd_all = {"role": "VICE_DEAN", "scope_dept_ids": "ALL"}
    dean = {"role": "DEAN", "scope_dept_ids": ""}
    hod_noscope = {"role": "HOD", "scope_dept_ids": ""}

    # THE headline guarantee.
    check("E01 HOD in_scope E01 == True", rbac.in_scope(hod_e01, "E01"))
    check("E01 HOD in_scope E02 == False (blocked)",
          rbac.in_scope(hod_e01, "E02") is False,
          "the promise of §4")

    # Vice Dean over a subset.
    check("VD{E01,E03} sees E03 == True", rbac.in_scope(vd_subset, "E03"))
    check("VD{E01,E03} sees E02 == False", rbac.in_scope(vd_subset, "E02") is False)

    # Whole-college roles.
    check("VD scope 'ALL' sees E02 == True", rbac.in_scope(vd_all, "E02"))
    check("Dean sees any dept == True", rbac.in_scope(dean, "E81"))

    # Fail-CLOSED: an HOD with no scope sees nothing (never everything).
    check("HOD with empty scope sees nothing (fail closed)",
          rbac.in_scope(hod_noscope, "E01") is False)

    # scope_clause shapes.
    c1, p1 = rbac.scope_clause(hod_e01)
    check("scope_clause(E01 HOD) -> single IN param",
          c1 == "o.dept_code IN (?)" and p1 == ["E01"], "%s / %s" % (c1, p1))
    c2, p2 = rbac.scope_clause(dean)
    check("scope_clause(Dean) -> 1=1 (all)", c2 == "1=1" and p2 == [])
    c3, p3 = rbac.scope_clause(hod_noscope)
    check("scope_clause(no-scope) -> 1=0 (none)", c3 == "1=0" and p3 == [])


# ############################################################################
# SECTION C — migration: idempotent + non-destructive  (Design §3.1, §6, §17)
# ############################################################################
def _table_columns(conn, table):
    return [r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)]


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def test_migration():
    print("\nC. migrate_v2_module1 — idempotent and non-destructive")

    conn = db.get_master()

    # Version 1.0 baseline counts, taken BEFORE we migrate.
    v1_offering = conn.execute("SELECT COUNT(*) n FROM offering").fetchone()["n"]
    v1_students = conn.execute("SELECT COUNT(*) n FROM students").fetchone()["n"]
    v1_cycle = conn.execute("SELECT COUNT(*) n FROM cycle").fetchone()["n"]

    # Run the migration TWICE against the same DB.
    migrate_v2_module1.migrate(conn)
    cols_after_1 = _table_columns(conn, "department")
    migrate_v2_module1.migrate(conn)          # second run must be a clean no-op
    cols_after_2 = _table_columns(conn, "department")
    conn.commit()

    # New columns present, and exactly ONCE (no duplication across the two runs).
    dept_cols = _table_columns(conn, "department")
    check("department has hod_user_id", "hod_user_id" in dept_cols)
    check("department has vice_dean_user_id", "vice_dean_user_id" in dept_cols)
    check("running twice did not duplicate department columns",
          cols_after_1 == cols_after_2 and
          dept_cols.count("hod_user_id") == 1 and
          dept_cols.count("vice_dean_user_id") == 1)

    cyc_cols = _table_columns(conn, "cycle")
    check("cycle has threshold_overall / threshold_section / min_responses",
          all(c in cyc_cols for c in
              ("threshold_overall", "threshold_section", "min_responses")))

    # New tables created.
    for t in ("app_user", "set_pw_token", "admin_log", "offering_classification"):
        check("table %s exists" % t, _table_exists(conn, t))

    # Defaults applied to existing cycle rows (8.0 / 10, section NULL).
    row = conn.execute(
        "SELECT threshold_overall, threshold_section, min_responses "
        "FROM cycle LIMIT 1").fetchone()
    check("existing cycle rows got defaults (8.0 / NULL / 10)",
          row["threshold_overall"] == 8.0 and row["threshold_section"] is None
          and row["min_responses"] == 10,
          "overall=%s section=%s min=%s" % (row["threshold_overall"],
                                            row["threshold_section"],
                                            row["min_responses"]))

    # NON-DESTRUCTIVE: Version 1.0 counts unchanged.
    check("offering count unchanged (%d)" % v1_offering,
          conn.execute("SELECT COUNT(*) n FROM offering").fetchone()["n"] == v1_offering)
    check("students count unchanged (%d)" % v1_students,
          conn.execute("SELECT COUNT(*) n FROM students").fetchone()["n"] == v1_students)
    check("cycle count unchanged (%d)" % v1_cycle,
          conn.execute("SELECT COUNT(*) n FROM cycle").fetchone()["n"] == v1_cycle)

    conn.close()


# ############################################################################
# SECTION D — seed_leaders: correct + idempotent  (Design §2, §17.2)
# ############################################################################
def test_seed():
    print("\nD. seed_leaders — 13 accounts, correct roles/scopes, org tree wired")

    conn = db.get_master()

    # Seed twice to prove idempotency (the second run must add nothing).
    seed_leaders.seed_leaders(conn)
    seed_leaders.seed_leaders(conn)
    conn.commit()

    n_dept = len(Config.DEPT_CODES)              # 11
    expected_total = n_dept + 2                  # + Vice Dean + Dean = 13

    total = conn.execute("SELECT COUNT(*) n FROM app_user").fetchone()["n"]
    check("app_user has exactly %d accounts (idempotent)" % expected_total,
          total == expected_total, "found %d" % total)

    n_hod = conn.execute(
        "SELECT COUNT(*) n FROM app_user WHERE role='HOD'").fetchone()["n"]
    n_vd = conn.execute(
        "SELECT COUNT(*) n FROM app_user WHERE role='VICE_DEAN'").fetchone()["n"]
    n_dean = conn.execute(
        "SELECT COUNT(*) n FROM app_user WHERE role='DEAN'").fetchone()["n"]
    check("role counts: %d HOD / 1 VICE_DEAN / 1 DEAN" % n_dept,
          n_hod == n_dept and n_vd == 1 and n_dean == 1,
          "HOD=%d VD=%d DEAN=%d" % (n_hod, n_vd, n_dean))

    # Every HOD is scoped to exactly its own department code.
    bad_scope = []
    for code in Config.DEPT_CODES:
        email = "hod.%s@sriher.edu.in" % code.lower()
        r = conn.execute(
            "SELECT scope_dept_ids FROM app_user WHERE email=?", (email,)).fetchone()
        if r is None or r["scope_dept_ids"] != code:
            bad_scope.append(code)
    check("each HOD scope == its own dept code", not bad_scope,
          "mismatches: %s" % bad_scope if bad_scope else "all correct")

    # Vice Dean & Dean are whole-college ('ALL').
    vd_scope = conn.execute(
        "SELECT scope_dept_ids FROM app_user WHERE email='vicedean@sriher.edu.in'"
    ).fetchone()["scope_dept_ids"]
    dean_scope = conn.execute(
        "SELECT scope_dept_ids FROM app_user WHERE email='dean@sriher.edu.in'"
    ).fetchone()["scope_dept_ids"]
    check("Vice Dean scope == 'ALL'", vd_scope == "ALL", vd_scope)
    check("Dean scope == 'ALL'", dean_scope == "ALL", dean_scope)

    # Passwords are unset (admin holds no passwords, §17.2).
    n_pw = conn.execute(
        "SELECT COUNT(*) n FROM app_user WHERE pw_hash != ''").fetchone()["n"]
    check("no account has a password set yet (pw_hash empty, §17.2)", n_pw == 0)

    # Org tree wired: every department points at an HOD and the single Vice Dean.
    vd_id = conn.execute(
        "SELECT id FROM app_user WHERE email='vicedean@sriher.edu.in'").fetchone()["id"]
    unwired = conn.execute(
        "SELECT COUNT(*) n FROM department "
        "WHERE hod_user_id IS NULL OR vice_dean_user_id IS NULL").fetchone()["n"]
    check("every department has hod_user_id + vice_dean_user_id set", unwired == 0)
    all_vd = conn.execute(
        "SELECT COUNT(*) n FROM department WHERE vice_dean_user_id != ?",
        (vd_id,)).fetchone()["n"]
    check("every department's vice_dean_user_id points at the one Vice Dean",
          all_vd == 0)

    # RBAC end-to-end against the seeded DB: an E01 HOD sees only E01 offerings.
    hod_e01 = conn.execute(
        "SELECT * FROM app_user WHERE email='hod.e01@sriher.edu.in'").fetchone()
    dean = conn.execute(
        "SELECT * FROM app_user WHERE email='dean@sriher.edu.in'").fetchone()
    vis_hod = rbac.visible_offerings(conn, hod_e01)
    vis_dean = rbac.visible_offerings(conn, dean)
    total_off = conn.execute("SELECT COUNT(*) n FROM offering").fetchone()["n"]
    leaked = [o["dept_code"] for o in vis_hod if o["dept_code"] != "E01"]
    check("visible_offerings(E01 HOD) returns ONLY E01 rows (no leak)",
          not leaked, "leaked depts: %s" % set(leaked) if leaked else "none")
    check("visible_offerings(Dean) returns ALL offerings (%d)" % total_off,
          len(vis_dean) == total_off, "dean sees %d" % len(vis_dean))

    conn.close()


# ############################################################################
# SECTION E — classification: upsert + end-to-end runner  (Design §6)
# ############################################################################
def test_classification_integration():
    print("\nE. classification — record_band upsert + classify_cycle runner")

    master = db.get_master()

    # --- record_band idempotency: two writes for the same (offering,cycle) ---
    # Use a real offering id so the FK is satisfied.
    off = master.execute("SELECT id FROM offering LIMIT 1").fetchone()
    if off is None:
        check("an offering exists to test record_band", False, "no offerings")
        master.close()
        return
    oid = off["id"]
    classification.record_band(master, oid, "ZZTEST", "GOOD", 8.5, 20,
                               8.0, None, 10, "first write")
    classification.record_band(master, oid, "ZZTEST", "POOR", 7.0, 20,
                               8.0, None, 10, "second write (upsert)")
    master.commit()
    rows = master.execute(
        "SELECT band, reason FROM offering_classification "
        "WHERE offering_id=? AND cycle_code='ZZTEST'", (oid,)).fetchall()
    check("record_band upsert keeps ONE row per (offering,cycle)", len(rows) == 1,
          "rows=%d" % len(rows))
    check("record_band upsert OVERWRITES the previous verdict",
          rows and rows[0]["band"] == "POOR" and rows[0]["reason"] == "second write (upsert)")
    # Clean up the synthetic test row so it never pollutes real data.
    master.execute("DELETE FROM offering_classification WHERE cycle_code='ZZTEST'")
    master.commit()

    # --- classify_cycle end-to-end on whichever cycle has responses ----------
    ran = False
    for c in master.execute("SELECT * FROM cycle ORDER BY code").fetchall():
        cycle_db_file = db.cycle_db_path(c["academic_year"], c["code"])
        if not os.path.exists(cycle_db_file):
            continue
        cy = db.get_cycle(c["academic_year"], c["code"])
        # Does this cycle DB even have a response table with rows?
        try:
            n_resp = cy.execute("SELECT COUNT(*) n FROM response").fetchone()["n"]
        except sqlite3.OperationalError:
            cy.close()
            continue
        if n_resp == 0:
            cy.close()
            continue

        summary = classification.classify_cycle(master, cy, c)
        master.commit()
        cy.close()
        ran = True

        # The tally must be internally consistent.
        parts = summary["good"] + summary["poor"] + summary["insufficient"] + summary["skipped"]
        check("classify_cycle(%s) ran; tally consistent" % c["code"],
              parts == summary["total"],
              "good=%d poor=%d insuff=%d skip=%d total=%d"
              % (summary["good"], summary["poor"], summary["insufficient"],
                 summary["skipped"], summary["total"]))

        # Every written verdict is a legal band value, and rows were persisted.
        written = master.execute(
            "SELECT band FROM offering_classification WHERE cycle_code=?",
            (c["code"],)).fetchall()
        bad = [w["band"] for w in written if w["band"] not in ("GOOD", "POOR", None)]
        check("all written bands are GOOD/POOR/NULL", not bad,
              "illegal: %s" % bad if bad else "%d rows written" % len(written))
        break

    if not ran:
        # Not a failure — just note we could not find live responses to classify.
        print("   [ -- ] classify_cycle: no cycle with responses found in the "
              "sandbox copy; skipped end-to-end (pure band() covered in A).")

    master.close()


# ############################################################################
# RUN ALL
# ############################################################################
def run():
    print("=" * 74)
    print("AFS Version 2.0 · Module 1 — VERIFICATION  (sandbox copy: %s)" % _SANDBOX)
    print("=" * 74)

    test_band()
    test_rbac_pure()
    test_migration()
    test_seed()
    test_classification_integration()

    print("\n" + "=" * 74)
    ok = _RESULTS["fail"] == 0
    print("RESULT: %s   (%d passed, %d failed)"
          % ("ALL PASS ✔" if ok else "FAILURES ✘",
             _RESULTS["pass"], _RESULTS["fail"]))
    print("=" * 74)
    return ok


def _cleanup():
    # Remove the sandbox copy; restore the real Config paths for tidiness.
    shutil.rmtree(_SANDBOX, ignore_errors=True)
    Config.DATA_DIR = _REAL_DATA_DIR
    Config.MASTER_DB = _REAL_MASTER


if __name__ == "__main__":
    try:
        ok = run()
    finally:
        _cleanup()
    raise SystemExit(0 if ok else 1)
