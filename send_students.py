#!/usr/bin/env python3
# ============================================================================
# send_students.py  —  One-shot student feedback-link blast, from a CONSOLE
# ============================================================================
# WHY THIS EXISTS (Aug 2026)
# ----------------------------------------------------------------------------
# The admin "Tokens" page can generate + email each student their private
# /f/<token> link, but it does that INSIDE a web request. On PythonAnywhere a web
# request is killed after ~5 minutes, and ~1,400 sequential Gmail API sends take
# longer than that — so a browser one-shot times out half-way. This standalone
# script does the SAME work (ensure a token per student, then email the link) but
# runs in a Bash console, where there is no request timeout. It reuses the very
# same building blocks the web route uses (services.new_token, emailer.render_body,
# emailer.send_batch), so a student receives exactly the same mail either way.
#
# It pairs with two robustness fixes made the same day:
#   * gmail_api.send_via_gmail_api now RETRIES on 429/5xx with backoff, and
#   * emailer.send_batch now paces sends (FEEDBACK_SEND_DELAY_SEC, default 0.25s)
# so a full-cohort blast stays under Gmail's per-second rate cap and no student is
# silently dropped. Together they make the one-shot reliable.
#
# SAFETY: the send obeys the cycle's TEST LEVEL exactly like the web route — at
# test levels 1/2 every mail is hard-redirected to the test inbox, never a real
# student. Do a --dry-run first to see the counts without sending anything.
#
# USAGE (run from the app folder on the server):
#   python send_students.py <CYCLE_CODE>                 # send to the whole cohort
#   python send_students.py <CYCLE_CODE> --dry-run       # count only, send nothing
#   python send_students.py <CYCLE_CODE> --dept E01      # one department
#   python send_students.py <CYCLE_CODE> --year 4        # one year of study
#   python send_students.py <CYCLE_CODE> --base https://yoursite.pythonanywhere.com
# The link base defaults to notifications.public_base_url(); override with --base
# if that is not your public address.
# ----------------------------------------------------------------------------

import os
import sys
import csv
import argparse
from datetime import datetime

# App modules — imported flat, exactly as the web routes do.
import db                                   # get_master / get_cycle / cycle_db_path
from config import Config                   # BASE_DIR (to locate schema_cycle.sql)
import services                             # new_token()
import emailer                              # render_body / send_batch / test_level_of
import notifications                        # public_base_url()


def _open_cycle_db(cycle_row):
    """Open the per-cycle DB (where the token table lives) and make sure its
    schema exists — a copy of the web app's helper so this script stands alone."""
    conn = db.get_cycle(cycle_row["academic_year"], cycle_row["code"])
    with open(os.path.join(Config.BASE_DIR, "schema_cycle.sql"), encoding="utf-8") as fh:
        conn.executescript(fh.read())
    conn.commit()
    return conn


def _select_batch(conn, cycle_code, dept, year):
    """Eligible students for this cycle (active), optionally narrowed by
    department (programme code) or year of study — same query as the Tokens page."""
    clauses = ["cycle_code=?", "status='active'"]
    params = [cycle_code]
    if dept:
        clauses.append("dept_code=?"); params.append(dept)
    if year:
        clauses.append("year_of_study=?"); params.append(int(year))
    sql = "SELECT * FROM students WHERE " + " AND ".join(clauses)
    return conn.execute(sql, params).fetchall()


def main():
    ap = argparse.ArgumentParser(description="One-shot student feedback-link blast.")
    ap.add_argument("cycle_code", help="e.g. CA1 / the cycle's code")
    ap.add_argument("--dept", default="", help="programme code filter, e.g. E01")
    ap.add_argument("--year", default="", help="year_of_study filter, e.g. 4")
    ap.add_argument("--base", default="", help="public base URL for the /f/<token> links")
    ap.add_argument("--dry-run", action="store_true", help="count only; send nothing")
    args = ap.parse_args()

    master = db.get_master()
    c = master.execute("SELECT * FROM cycle WHERE code=?", (args.cycle_code,)).fetchone()
    if c is None:
        sys.exit(f"ERROR: no cycle with code {args.cycle_code!r}")

    # Where the /f/<token> links point. Default to the app's configured public URL.
    base = (args.base or notifications.public_base_url() or "").rstrip("/")
    if not base:
        sys.exit("ERROR: no base URL — pass --base https://yoursite...")

    cy = _open_cycle_db(c)
    batch = _select_batch(master, c["code"], args.dept.strip(), args.year.strip())
    print(f"Cycle {c['code']} ({c['label']}) — {len(batch)} eligible student(s). "
          f"Link base: {base}")

    # 1) Ensure a token per student (idempotent — reuse any existing one), and
    #    build the per-student message list.
    created = skipped = no_email = 0
    messages = []
    for s in batch:
        row = cy.execute("SELECT token FROM token WHERE reg_no=?", (s["reg_no"],)).fetchone()
        if row:
            tok = row["token"]; skipped += 1
        else:
            tok = services.new_token()
            cy.execute("INSERT INTO token (token, reg_no) VALUES (?, ?)", (tok, s["reg_no"]))
            created += 1
        if not s["email"]:
            no_email += 1
            continue
        link = f"{base}/f/{tok}"
        body = emailer.render_body(c["email_body"], s["name"], c["label"], link)
        messages.append({"to": s["email"], "body": body})
    cy.commit(); cy.close()
    print(f"Tokens: {created} new, {skipped} existing. "
          f"{len(messages)} email(s) to send; {no_email} student(s) have no email.")

    if args.dry_run:
        print("DRY RUN — nothing sent.")
        master.close()
        return

    # 2) Send. send_batch applies the cycle's TEST-LEVEL routing (test inbox at
    #    levels 1/2), paces itself (FEEDBACK_SEND_DELAY_SEC), and the Gmail layer
    #    retries transient rate-limit errors. It prints progress every 100 sends.
    subject = f"SRET Feedback — {c['label']}"
    result = emailer.send_batch(Config.BASE_DIR, subject, messages,
                                test_level=emailer.test_level_of(c),
                                audience="student")
    master.close()

    # 3) Report + persist any failures so you can re-run on just those.
    print(f"\nDONE — mode={result['mode']} sent={result['count']} "
          f"failed={len(result['errors'])} redirected={result.get('redirected')}")
    if result["errors"]:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        fn = f"send_failures_{c['code']}_{ts}.csv"
        with open(fn, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh); w.writerow(["recipient_and_error"])
            for e in result["errors"]:
                w.writerow([e])
        print(f"{len(result['errors'])} failure(s) written to {fn} — "
              f"re-run to retry (already-sent students keep their token, "
              f"delivery is idempotent per your setup).")


if __name__ == "__main__":
    main()
