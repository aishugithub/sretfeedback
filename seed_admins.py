# ============================================================================
# seed_admins.py  —  grant the two operators ADMIN console access (v2.1)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# The admin console is login-gated (admin/auth.py). Admin access is the SEPARATE
# `app_user.is_admin` flag — deliberately NOT the org-tree `role` — so a person can
# be an administrator AND still hold a leader role (the Vice Dean here is both).
#
# This script grants is_admin=1 to the two named operators, WITHOUT disturbing any
# existing leader role/scope/password:
#   * If the email already exists (e.g. aishwarya@ is the Vice Dean), we ONLY set
#     is_admin=1 and make sure the account is active — her VICE_DEAN role, scope,
#     and password are left exactly as they were, so she keeps endorsing ATRs.
#   * If the email is new (lekha@), we create a pure-admin account: role='ADMIN'
#     (a sentinel that appears in NO HOD/VD/Dean roll-up), scope 'ALL', is_admin=1,
#     no password yet — she sets her own via the printed link or /admin/forgot.
#
# Idempotent; run it on the SERVER too (data never travels via git). It assumes the
# is_admin column exists — run  python migrate_admin_flag.py  first on an old DB.
#
# USAGE:  python seed_admins.py
# ----------------------------------------------------------------------------

import os

import db
import auth_leaders
import rbac


# (name, email) of the two operators who may use the admin console.
ADMINS = [
    ("Aishwarya", "aishwarya@sret.edu.in"),   # also the Vice Dean — role preserved
    ("Lekha",     "lekha@sret.edu.in"),        # pure admin
]


def _base_url():
    return (os.environ.get("FEEDBACK_PUBLIC_BASE_URL", "").strip().rstrip("/")
            or "http://localhost:5000")


def main():
    conn = db.get_master()
    base = _base_url()
    try:
        print("Granting ADMIN console access (is_admin=1):\n")
        for name, email in ADMINS:
            email_l = email.strip().lower()
            row = conn.execute(
                "SELECT * FROM app_user WHERE email = ?", (email_l,)).fetchone()
            if row is None:
                # Brand-new pure-admin account (no org-tree duties).
                conn.execute(
                    "INSERT INTO app_user (email, name, role, is_admin, "
                    "  scope_dept_ids, pw_hash, status, created_by) "
                    "VALUES (?, ?, ?, 1, ?, '', 'active', 'seed_admins')",
                    (email_l, name, rbac.ROLE_ADMIN, rbac.SCOPE_ALL))
                row = conn.execute(
                    "SELECT * FROM app_user WHERE email = ?", (email_l,)).fetchone()
                print("  + created pure-admin  %-24s (%s)" % (email_l, name))
            else:
                # Existing account: grant admin access WITHOUT touching role/scope/pw.
                conn.execute(
                    "UPDATE app_user SET is_admin = 1, status = 'active' WHERE id = ?",
                    (row["id"],))
                print("  = granted admin to    %-24s (%s) — role %s preserved"
                      % (email_l, name, row["role"]))

            has_pw = bool(row["pw_hash"])
            jti = auth_leaders.issue_set_pw_token(
                conn, row["id"], purpose=("RESET" if has_pw else "SET"))
            print("      %s password link: %s/leader/set-password?token=%s\n"
                  % ("RESET" if has_pw else "SET", base, jti))

        conn.commit()
        print("Done. Admins can also self-serve at %s/admin/forgot." % base)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
