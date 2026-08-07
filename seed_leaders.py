# ============================================================================
# seed_leaders.py  —  Version 2.0 · Module 1 : seed the placeholder leaders
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# Design §2/§13(D0) needs ~13 leader login accounts to exist before any leader
# dashboard, ATR endorsement or RBAC test can run. The real names/emails are an
# open question (§14.4), so — per the professor's instruction — we seed obvious
# PLACEHOLDER accounts now: one HOD per department code, one Vice Dean over the
# whole college, and one Dean. They are real, working rows (active, correctly
# scoped) that the professor can log in as once passwords are set; only the
# human name/email behind each is a placeholder to be edited later in the admin
# "Users & Roles" screen.
#
# WHAT IT CREATES (from Config.DEPT_CODES = 11 departments):
#   * 11 HODs   — email hod.<code>@sriher.edu.in (e.g. hod.e01@sriher.edu.in),
#                 role HOD, scope = that ONE department code.
#   * 1 Vice Dean — vicedean@sriher.edu.in, role VICE_DEAN, scope = 'ALL'.
#   * 1 Dean      — dean@sriher.edu.in,     role DEAN,      scope = 'ALL'.
#   => 13 accounts total.  (The design's "~14" was an estimate assuming ~12
#      departments; Config currently defines 11, so 11 + VD + Dean = 13. If a
#      12th department is added to Config later, re-running this seed adds its
#      HOD and the count becomes 14 — the seed follows the config, not a magic
#      number.)
#
# PASSWORD MODEL (Design §17.2): the admin holds NO passwords. Every account is
# created with an EMPTY pw_hash; the leader sets their own password later by
# clicking an emailed one-time set_pw_token link (that flow is Module 2/D4).
# An empty pw_hash here is therefore correct and expected, NOT a bug.
#
# IDEMPOTENT: emails are UNIQUE in app_user, so INSERT OR IGNORE means re-running
# never duplicates an account; the department wiring is a plain UPDATE that
# simply re-points to the same ids. Run it as many times as you like.
#
# Usage (from inside the app/ folder):  python seed_leaders.py
# (run migrate_v2_module1.py first so app_user and the department leader columns
#  exist — seed_leaders assumes the Module 1 schema is in place).
# ----------------------------------------------------------------------------

import db                     # the one master.db opener (WAL, FK, two-file split)
from config import Config     # DEPT_CODES — the single source of the department list
import rbac                   # SCOPE_ALL sentinel + role constants (one definition, shared)


# Placeholder identities for the two college-wide roles. Kept as constants so the
# seed, the tests and any later admin edit refer to the same strings.
VICE_DEAN_EMAIL = "vicedean@sriher.edu.in"
DEAN_EMAIL = "dean@sriher.edu.in"

# Who "created" these rows, for the created_by audit column. The seed is a system
# action (no human admin logged in), so we stamp a clear machine marker.
SEED_ACTOR = "system:seed_leaders"


# ----------------------------------------------------------------------------
# _upsert_user(conn, email, name, role, scope) -> int
# ----------------------------------------------------------------------------
# Insert one leader if absent (INSERT OR IGNORE on the UNIQUE email), then return
# its id whether it was just created or already existed. pw_hash is left as the
# column default ('' — password not yet set, per §17.2). Returning the id lets
# the caller wire it into the department table.
# ----------------------------------------------------------------------------
def _upsert_user(conn, email, name, role, scope):
    conn.execute(
        "INSERT OR IGNORE INTO app_user "
        "  (email, name, role, scope_dept_ids, pw_hash, status, created_by) "
        "VALUES (?, ?, ?, ?, '', 'active', ?)",
        (email, name, role, scope, SEED_ACTOR),
    )
    row = conn.execute("SELECT id FROM app_user WHERE email = ?", (email,)).fetchone()
    return row["id"]


# ----------------------------------------------------------------------------
# seed_leaders(conn) -> dict summary
# ----------------------------------------------------------------------------
# The public entry point. Seeds the Dean, the Vice Dean and one HOD per
# department, then wires department.hod_user_id / vice_dean_user_id. Does NOT
# commit — the caller (main() or a test) owns the transaction so the whole seed
# lands atomically. Returns counts for logging/verification.
# ----------------------------------------------------------------------------
def seed_leaders(conn):
    # ----- 1. Dean (whole college) ------------------------------------------
    dean_id = _upsert_user(conn, DEAN_EMAIL, "Placeholder Dean",
                           rbac.ROLE_DEAN, rbac.SCOPE_ALL)

    # ----- 2. Vice Dean (whole college; a second VD later = one more row) ----
    vice_dean_id = _upsert_user(conn, VICE_DEAN_EMAIL, "Placeholder Vice Dean",
                                rbac.ROLE_VICE_DEAN, rbac.SCOPE_ALL)

    # ----- 3. One HOD per department, scoped to that single E-code ----------
    hod_ids = {}   # dept_code -> app_user.id, used to wire department.hod_user_id
    for code, dept_name in Config.DEPT_CODES.items():
        email = "hod.%s@sriher.edu.in" % code.lower()        # e.g. hod.e01@sriher.edu.in
        display = "Placeholder HOD %s (%s)" % (code, dept_name)
        hod_ids[code] = _upsert_user(conn, email, display, rbac.ROLE_HOD, code)

    # ----- 4. Wire the org tree: each dept -> its HOD + the single Vice Dean --
    # A plain UPDATE keyed on the department code (its primary key). Re-running
    # simply re-points to the same ids, so this is idempotent alongside the
    # INSERT OR IGNORE above. Only departments the seed knows an HOD for are
    # touched; any dept not in Config is left exactly as it was.
    for code, hod_id in hod_ids.items():
        conn.execute(
            "UPDATE department SET hod_user_id = ?, vice_dean_user_id = ? "
            "WHERE code = ?",
            (hod_id, vice_dean_id, code),
        )

    # ----- 5. One SEED audit line (idempotent: only if not already present) --
    # admin_log records sensitive account actions (§17.3). We add a single SEED
    # marker the first time, guarded so repeated runs do not stack duplicates.
    already = conn.execute(
        "SELECT 1 FROM admin_log WHERE action = 'SEED' LIMIT 1"
    ).fetchone()
    if not already:
        conn.execute(
            "INSERT INTO admin_log (admin_user_id, action, target_user_id) "
            "VALUES (NULL, 'SEED', NULL)"
        )

    return {
        "dean_id": dean_id,
        "vice_dean_id": vice_dean_id,
        "hods": len(hod_ids),
        "total_accounts": conn.execute(
            "SELECT COUNT(*) AS n FROM app_user").fetchone()["n"],
    }


# ----------------------------------------------------------------------------
# main() — run the seed against the real master.db (opens + commits + closes).
# ----------------------------------------------------------------------------
def main():
    print("=" * 70)
    print("AFS Version 2.0 · Module 1 — seed placeholder leaders")
    print("=" * 70)
    conn = db.get_master()
    try:
        summary = seed_leaders(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print("Dean id           :", summary["dean_id"])
    print("Vice Dean id      :", summary["vice_dean_id"])
    print("HOD accounts      :", summary["hods"], "(one per department code)")
    print("Total app_user rows:", summary["total_accounts"])
    print("-" * 70)
    print("Passwords are intentionally UNSET (pw_hash=''); leaders set their own")
    print("via the emailed one-time link (Design §17.2). Edit names/emails later")
    print("in the admin Users & Roles screen (Module 2).")
    print("=" * 70)
    conn.close()


if __name__ == "__main__":
    main()
