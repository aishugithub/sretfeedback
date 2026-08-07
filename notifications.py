# ============================================================================
# notifications.py  —  Version 2.0 · Module 2 · §9 : state-change emails + reminders
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# The ATR state machine (atr_workflow.py) moves an ATR up the endorsement chain.
# Every time it moves, SOMEONE new must act next — and they need to be told, by
# email, with a link to do it. This module is the §9 notification layer: given a
# transition that just happened, it works out WHO must act next and sends them
# the right message, reusing the existing emailer.send_batch (so all the test/
# real/outbox transport logic — and the test-cycle hard-redirect — comes for
# free and is never re-implemented).
#
# THE §9 TABLE this module implements, exactly:
#   | Event                     | Recipient          | Contains                 |
#   | Faculty SUBMIT            | HOD                | review/endorse/return    |
#   | HOD ENDORSE               | Vice Dean          | review/endorse/return    |
#   | VD ENDORSE                | Dean               | review/endorse/return    |
#   | Any RETURN                | the level BELOW    | reason + link to revise  |
#   | Dean ENDORSE (CLOSED)     | Faculty (+ HOD)    | acknowledgement          |
#
# We derive the recipient from the NEW state (the state's "owner" is exactly who
# must act next), which keeps this module in lock-step with the FSM: add a state
# and the routing follows automatically. The one special case is CLOSED, which
# has no "next actor" but is an acknowledgement to the faculty + HOD.
#
# ROUTING TO THE RIGHT LEADER uses the Module 1 org tree: department.hod_user_id
# / vice_dean_user_id and the single DEAN app_user. Because an ATR is bound to an
# offering, and the offering carries its dept_code, the correct HOD/VD are a
# join away — an E01 ATR can only ever notify E01's HOD.
#
# ANONYMITY (spec §3.2): every recipient here is a FACULTY member (offering
# .faculty_email) or a LEADER (app_user.email). No student address is ever
# touched; nothing reads the anonymous answer side.
# ----------------------------------------------------------------------------

import os   # read the public base URL from the environment (no secret in code)

import emailer          # the ONE mail sender (gmail-api > smtp > dev-outbox)
import atr_workflow     # state/role constants + STATE_OWNER
import rbac             # effective_dept() — route by the faculty's home department
from config import Config


# ----------------------------------------------------------------------------
# faculty_email_for(master, offering_row) -> str | None   (v2.0 · Module 5)
# ----------------------------------------------------------------------------
# Resolve a faculty's CURRENT email from the Faculty Master by employee number
# (the single source of truth), falling back to any email cached on the offering
# row. This is why the result/ATR mail now has an address to send to even though
# the allocation file no longer carries emails.
# ----------------------------------------------------------------------------
def faculty_email_for(master, offering_row):
    try:
        emp = offering_row["faculty_id"]
    except (KeyError, IndexError, TypeError):
        emp = None
    if emp:
        try:
            r = master.execute(
                "SELECT email FROM faculty WHERE emp_no = ?", (emp,)).fetchone()
            if r and r["email"]:
                return r["email"]
        except Exception:
            pass
    try:
        return offering_row["faculty_email"]
    except (KeyError, IndexError, TypeError):
        return None


# ----------------------------------------------------------------------------
# _endorsing_dept(master, offering_row) -> str   (v2.0 · Module 5)
# ----------------------------------------------------------------------------
# The department whose HOD/Vice Dean endorse this offering's ATR: the FACULTY's
# home department. Falls back to the course's own department only when the
# faculty's home dept is not a real code (unset or EXTERNAL), so a stray ATR still
# reaches a real HOD rather than nobody. External/unassigned faculty have no email
# and cannot file an ATR, so in practice this fallback is a belt-and-braces guard.
# ----------------------------------------------------------------------------
def _endorsing_dept(master, offering_row):
    eff = rbac.effective_dept(master, offering_row)
    if eff and eff != rbac.DEPT_EXTERNAL:
        return eff
    return offering_row["dept_code"]


# ----------------------------------------------------------------------------
# public_base_url() — the externally reachable root of the app, used to build
# clickable links in emails (an email link must be absolute, and the app may be
# on PythonAnywhere, not localhost). Read from FEEDBACK_PUBLIC_BASE_URL so it is
# configured per deployment with no code change (consistent with the app's
# env-var convention, spec §11/§16). The localhost default keeps dev runnable.
# ----------------------------------------------------------------------------
def public_base_url():
    return os.environ.get("FEEDBACK_PUBLIC_BASE_URL", "http://localhost:5000").rstrip("/")


# ----------------------------------------------------------------------------
# atr_review_url(base, atr_id) — the LEADER link to review/endorse/return one
# ATR. Leaders are logged in, so this is a plain (non-token) app URL under the
# atr blueprint; RBAC on the route decides whether they may act.
# ----------------------------------------------------------------------------
def atr_review_url(base, cycle_code, atr_id):
    return "%s/atr/review/%s/%s" % (base, cycle_code, atr_id)


# ----------------------------------------------------------------------------
# atr_file_url(base, jti) — the FACULTY magic link (carries the one-time token).
# This is the "File ATR" / "revise" button target; the token binds it to one
# offering (see faculty_tokens.py).
# ----------------------------------------------------------------------------
def atr_file_url(base, jti):
    return "%s/atr/file?token=%s" % (base, jti)


# ----------------------------------------------------------------------------
# _leader_email(master, role, dept_code) -> email | None
# ----------------------------------------------------------------------------
# Resolve the address of the leader who owns a given role for a given department,
# via the Module 1 org tree:
#   HOD        -> department.hod_user_id       -> app_user.email
#   VICE_DEAN  -> department.vice_dean_user_id  -> app_user.email
#   DEAN       -> the single app_user with role DEAN (college-wide)
# Returns None if the seat is unfilled (e.g. a dept with no HOD wired yet), so
# the caller can record a clean "no recipient" rather than crash.
# ----------------------------------------------------------------------------
def _leader_email(master, role, dept_code):
    if role == atr_workflow.ROLE_DEAN:
        row = master.execute(
            "SELECT email FROM app_user WHERE role = ? AND status = 'active' "
            "ORDER BY id LIMIT 1", (atr_workflow.ROLE_DEAN,)).fetchone()
        return row["email"] if row else None

    col = ("hod_user_id" if role == atr_workflow.ROLE_HOD
           else "vice_dean_user_id")
    row = master.execute(
        "SELECT u.email AS email "
        "FROM department d JOIN app_user u ON u.id = d.%s "
        "WHERE d.code = ? AND u.status = 'active'" % col,
        (dept_code,)).fetchone()
    return row["email"] if row else None


# ----------------------------------------------------------------------------
# _offering(master, offering_id) -> Row — the offering identity (dept, faculty,
# course) an ATR is about. Read from master.db only (the anonymity boundary is
# untouched — this is the COURSE, never a student).
# ----------------------------------------------------------------------------
def _offering(master, offering_id):
    return master.execute(
        "SELECT * FROM offering WHERE id = ?", (offering_id,)).fetchone()


# ----------------------------------------------------------------------------
# recipients_for(master, offering_row, action, new_state) -> list[(email, role)]
# ----------------------------------------------------------------------------
# THE §9 routing decision, as a pure lookup over the NEW state (plus the CLOSED
# special case). Returns a list of (email, role_label) pairs — a list because
# the CLOSED acknowledgement goes to two people (faculty + HOD). Skips any seat
# that resolves to None (unfilled leader), so distribution/notification degrades
# gracefully instead of failing.
#
#   new_state PENDING_HOD   -> the dept HOD          (SUBMIT, or VD RETURN down)
#   new_state PENDING_VD    -> the Vice Dean         (HOD ENDORSE, or Dean RETURN down)
#   new_state PENDING_DEAN  -> the Dean              (VD ENDORSE)
#   new_state EXPECTED      -> the faculty           (HOD RETURN reaches faculty)
#   new_state CLOSED        -> the faculty AND HOD   (Dean ENDORSE acknowledgement)
# ----------------------------------------------------------------------------
def recipients_for(master, offering_row, action, new_state):
    # Route by the FACULTY's home department (Module 5), and take the faculty's
    # address from the Faculty Master — not the (now email-less) allocation.
    dept = _endorsing_dept(master, offering_row)
    faculty_email = faculty_email_for(master, offering_row)
    out = []

    if new_state == atr_workflow.STATE_PENDING_HOD:
        e = _leader_email(master, atr_workflow.ROLE_HOD, dept)
        if e:
            out.append((e, atr_workflow.ROLE_HOD))

    elif new_state == atr_workflow.STATE_PENDING_VD:
        e = _leader_email(master, atr_workflow.ROLE_VICE_DEAN, dept)
        if e:
            out.append((e, atr_workflow.ROLE_VICE_DEAN))

    elif new_state == atr_workflow.STATE_PENDING_DEAN:
        e = _leader_email(master, atr_workflow.ROLE_DEAN, dept)
        if e:
            out.append((e, atr_workflow.ROLE_DEAN))

    elif new_state == atr_workflow.STATE_EXPECTED:
        # HOD returned to the faculty (the ONLY return that reaches faculty).
        if faculty_email:
            out.append((faculty_email, atr_workflow.ROLE_FACULTY))

    elif new_state == atr_workflow.STATE_CLOSED:
        # Acknowledgement to faculty + their HOD.
        if faculty_email:
            out.append((faculty_email, atr_workflow.ROLE_FACULTY))
        e = _leader_email(master, atr_workflow.ROLE_HOD, dept)
        if e:
            out.append((e, atr_workflow.ROLE_HOD))

    return out


# ----------------------------------------------------------------------------
# _body(offering_row, action, new_state, role, link, reason) -> str
# ----------------------------------------------------------------------------
# Compose the plain-text email body for one recipient. Kept deliberately simple
# and functional (UI polish is out of scope for Module 2); it states what
# happened, the course it concerns, any return-reason, and the action link.
# ----------------------------------------------------------------------------
def _body(offering_row, action, new_state, role, link, reason):
    course = "%s — %s" % (offering_row["course_code"],
                          offering_row["course_name"] or "")
    faculty = offering_row["faculty"] or offering_row["faculty_email"] or "the faculty"
    lines = []

    if new_state == atr_workflow.STATE_CLOSED:
        lines.append("The Action-Taken-Report (ATR) for the following subject "
                     "has been fully endorsed and is now CLOSED:")
    elif action == atr_workflow.ACTION_RETURN:
        lines.append("An Action-Taken-Report (ATR) has been RETURNED to you for "
                     "revision:")
    elif action == atr_workflow.ACTION_SUBMIT:
        lines.append("A faculty member has SUBMITTED an Action-Taken-Report (ATR) "
                     "that now awaits your review:")
    else:  # ENDORSE moving up the chain
        lines.append("An Action-Taken-Report (ATR) has been endorsed and now "
                     "awaits your review:")

    lines.append("")
    lines.append("  Course : %s" % course)
    lines.append("  Faculty: %s" % faculty)
    lines.append("  Dept   : %s" % offering_row["dept_code"])
    lines.append("  Status : %s" % new_state)
    if reason:
        lines.append("  Reason : %s" % reason)
    lines.append("")
    if link:
        verb = ("Open your report" if role == atr_workflow.ROLE_FACULTY
                and new_state == atr_workflow.STATE_CLOSED
                else ("Revise your ATR" if action == atr_workflow.ACTION_RETURN
                      and role == atr_workflow.ROLE_FACULTY
                      else "Review this ATR"))
        lines.append("%s: %s" % (verb, link))
    lines.append("")
    lines.append("— Automated Feedback System, SRET")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# notify_state_change(master, cycle, cycle_row, atr_row, action, new_state,
#                     reason=None, faculty_link_jti=None, base_url=None)
#                     -> summary dict
# ----------------------------------------------------------------------------
# Send the §9 email(s) triggered by a transition that just occurred. Given the
# ATR that changed and the new state, it resolves the recipient(s), builds each
# body (with the correct link — a leader review URL, or a faculty magic link when
# the ATR came back to the faculty), and hands the whole lot to emailer.send_batch
# as ONE batch (so a test cycle's hard-redirect applies uniformly).
#
# LINKS: leaders get atr_review_url(atr_id). When the recipient is the FACULTY
# (an HOD RETURN, or a CLOSED acknowledgement), we need a magic link; the caller
# passes a freshly-issued token's jti as `faculty_link_jti` (issued via
# faculty_tokens.issue in the route/job that triggered this), which we turn into
# atr_file_url. If none is supplied for a faculty recipient, the email still goes
# out without a button (the acknowledgement case needs no action).
#
# Reuses emailer.send_batch with is_test from the cycle row, so nothing about
# transport or the §9.1 test-redirect is re-implemented here.
# ----------------------------------------------------------------------------
def notify_state_change(master, cycle, cycle_row, atr_row, action, new_state,
                        reason=None, faculty_link_jti=None, base_url=None):
    base = (base_url or public_base_url()).rstrip("/")
    offering_row = _offering(master, atr_row["offering_id"])
    if offering_row is None:
        return {"count": 0, "errors": ["offering %s not found" % atr_row["offering_id"]],
                "recipients": []}

    recips = recipients_for(master, offering_row, action, new_state)

    messages = []
    labelled = []   # (email, role) parallel list for the caller/tests to inspect
    for email, role in recips:
        if role == atr_workflow.ROLE_FACULTY:
            link = (atr_file_url(base, faculty_link_jti)
                    if faculty_link_jti else None)
        else:
            link = atr_review_url(base, atr_row["cycle_code"], atr_row["id"])
        messages.append({
            "to": email,
            "body": _body(offering_row, action, new_state, role, link, reason),
        })
        labelled.append((email, role))

    subject = "[AFS] ATR %s — %s (%s)" % (
        new_state, offering_row["course_code"], cycle_row["code"])

    if not messages:
        # No recipient (e.g. unfilled seat / missing faculty email). Report it
        # rather than send nothing silently.
        return {"count": 0, "errors": [], "recipients": [], "mode": "none"}

    summary = emailer.send_batch(
        Config.BASE_DIR, subject, messages,
        is_test=bool(cycle_row["is_test"]))
    summary["recipients"] = labelled
    return summary


# ----------------------------------------------------------------------------
# send_reminder_email(master, cycle_row, offering_row, jti, base_url=None)
#                     -> summary dict
# ----------------------------------------------------------------------------
# The email half of the HOD "Send reminder" action (spec §9). The audit half
# (atr_event REMIND) is written by atr_workflow.record_reminder; the caller does
# both. This emails the faculty a FRESH ATR magic link (whose jti is passed in,
# issued by faculty_tokens.issue) so a stuck/expired link is replaced. Reuses
# emailer.send_batch with the cycle's test flag.
# ----------------------------------------------------------------------------
def send_reminder_email(master, cycle_row, offering_row, jti, base_url=None):
    base = (base_url or public_base_url()).rstrip("/")
    link = atr_file_url(base, jti)
    course = "%s — %s" % (offering_row["course_code"],
                          offering_row["course_name"] or "")
    body = "\n".join([
        "This is a reminder that an Action-Taken-Report (ATR) is expected for a "
        "subject where the feedback was flagged for attention:",
        "",
        "  Course : %s" % course,
        "  Dept   : %s" % offering_row["dept_code"],
        "",
        "Please file your ATR using the link below:",
        link,
        "",
        "— Automated Feedback System, SRET",
    ])
    subject = "[AFS] Reminder: ATR expected — %s (%s)" % (
        offering_row["course_code"], cycle_row["code"])
    to = faculty_email_for(master, offering_row)   # from the Faculty Master (Module 5)
    if not to:
        return {"count": 0, "errors": ["offering has no faculty_email"],
                "recipients": []}
    summary = emailer.send_batch(
        Config.BASE_DIR, subject,
        [{"to": to, "body": body}],
        is_test=bool(cycle_row["is_test"]))
    summary["recipients"] = [(to, atr_workflow.ROLE_FACULTY)]
    return summary
