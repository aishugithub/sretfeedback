# ============================================================================
# admin_reset.py  —  BREAK-GLASS admin password recovery (server console)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# "How do we get back in if we forget our login?" — THREE answers, safest first:
#   1. Normal:   go to /admin/forgot, type your admin email, click the emailed link.
#   2. If email  is down but you can still reach the site: this script can PRINT a
#      fresh one-time set-password link you paste into a browser (no email needed).
#   3. Total lockout (both admins out, email down): this script can DIRECTLY set a
#      password from the server console — the ultimate fallback. It requires shell
#      access to the server, which only the operators have, so it is a safe
#      break-glass rather than a public backdoor.
#
# It ONLY ever touches app_user rows whose role is ADMIN, so it can never be used
# to hijack a leader (HOD/VD/Dean) account. Like every data script here it runs
# against the LOCAL master.db, so use it on the SERVER for the live database.
#
# USAGE (from the app/ folder):
#     python admin_reset.py list                     # show the admin accounts
#     python admin_reset.py link  <email>            # print a one-time reset LINK
#     python admin_reset.py setpw <email> <password> # set a password directly
# ----------------------------------------------------------------------------

import os
import sys

import db
import auth_leaders
import rbac


def _base_url():
    return (os.environ.get("FEEDBACK_PUBLIC_BASE_URL", "").strip().rstrip("/")
            or "http://localhost:5000")


def _find_admin(conn, email):
    """Return the admin app_user row for `email`, or None. Restricting to
    is_admin=1 is the safety rail — this tool can touch no non-admin account
    (and it can legitimately reset the Vice-Dean-who-is-also-admin's password)."""
    return conn.execute(
        "SELECT * FROM app_user WHERE email = ? AND is_admin = 1",
        (email.strip().lower(),)).fetchone()


def cmd_list(conn):
    rows = conn.execute(
        "SELECT email, name, status, (pw_hash != '') AS has_pw, last_login_at "
        "FROM app_user WHERE role = ? ORDER BY email", (rbac.ROLE_ADMIN,)).fetchall()
    if not rows:
        print("No ADMIN accounts exist. Run:  python seed_admins.py")
        return
    print("ADMIN accounts:")
    for r in rows:
        print("  %-24s %-10s password:%-3s last_login:%s"
              % (r["email"], r["status"], "yes" if r["has_pw"] else "NO",
                 r["last_login_at"] or "never"))


def cmd_link(conn, email):
    row = _find_admin(conn, email)
    if row is None:
        print("No ADMIN account with email %r. Try:  python admin_reset.py list" % email)
        return
    purpose = "RESET" if row["pw_hash"] else "SET"
    jti = auth_leaders.issue_set_pw_token(conn, row["id"], purpose=purpose)
    conn.commit()
    print("One-time %s link for %s (valid %d days):"
          % (purpose, row["email"], auth_leaders.SET_PW_TTL_DAYS))
    print("  %s/leader/set-password?token=%s" % (_base_url(), jti))


def cmd_setpw(conn, email, password):
    if len(password) < 8:
        print("Refusing: choose a password of at least 8 characters.")
        return
    row = _find_admin(conn, email)
    if row is None:
        print("No ADMIN account with email %r. Try:  python admin_reset.py list" % email)
        return
    conn.execute("UPDATE app_user SET pw_hash = ? WHERE id = ?",
                 (auth_leaders.hash_password(password), row["id"]))
    conn.commit()
    print("Password set directly for %s. You can now log in at %s/admin/login."
          % (row["email"], _base_url()))


def main(argv):
    if not argv or argv[0] not in ("list", "link", "setpw"):
        print(__doc__)
        return
    conn = db.get_master()
    try:
        if argv[0] == "list":
            cmd_list(conn)
        elif argv[0] == "link":
            if len(argv) < 2:
                print("Usage: python admin_reset.py link <email>"); return
            cmd_link(conn, argv[1])
        elif argv[0] == "setpw":
            if len(argv) < 3:
                print("Usage: python admin_reset.py setpw <email> <password>"); return
            cmd_setpw(conn, argv[1], argv[2])
    finally:
        conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])
