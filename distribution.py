# ============================================================================
# distribution.py  —  Version 2.0 · Module 2 · §7 : result distribution job
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# After a cycle's scoring (scoring.py) and banding (Module 1's classification.py)
# are finalised, THIS job fans the results out by email (spec §7):
#
#   * FACULTY  → their OWN offerings, POOR ones flagged, with a per-poor-subject
#                "File ATR" magic-link button (faculty_tokens.issue).
#   * HOD      → a departmental roll-up (only their department).
#   * VD/Dean  → a college-wide roll-up.
#
# Two hard rules from the brief:
#   1. It REUSES the existing report_export.py (the proven Excel/PDF layout) and
#      emailer.send_batch (transport + the §9.1 test-cycle hard-redirect). We do
#      not re-implement scoring, report formatting, or mail transport.
#   2. Leader routing MUST pass through Module 1's rbac.py — every leader roll-up
#      is built from rbac.visible_offerings(master, user, cycle_code), so an E01
#      HOD is handed E01 rows and NOTHING else. The RBAC choke-point is the only
#      thing that decides who sees which department; distribution just obeys it.
#
# ANONYMITY (spec §3.2): every recipient is a faculty member (offering
# .faculty_email) or a leader (app_user.email); every figure is an AGGREGATE
# score/band from offering_classification. No student, token, or response is ever
# touched here — scoring already read the anonymous side and returned only numbers.
# ----------------------------------------------------------------------------

import os
import tempfile

import emailer
import faculty_tokens
import atr_workflow
import classification
import rbac
import report_export
import scoring
from config import Config
from notifications import public_base_url, atr_file_url, faculty_email_for


# ----------------------------------------------------------------------------
# classified_offerings(master, cycle_code) -> dict offering_id -> Row
# ----------------------------------------------------------------------------
# The offerings that actually have a GOOD/POOR/insufficient verdict for this
# cycle — i.e. the ones Module 1's classification.classify_cycle recorded in the
# offering_classification table (master.db). This is the universe distribution
# reports on; an offering with no verdict (no responses) is simply not mailed.
# Returned as a dict keyed by offering_id for O(1) band lookup while building
# each recipient's roll-up.
# ----------------------------------------------------------------------------
def classified_offerings(master, cycle_code):
    rows = master.execute(
        "SELECT * FROM offering_classification WHERE cycle_code = ?",
        (cycle_code,)).fetchall()
    return {r["offering_id"]: r for r in rows}


# ----------------------------------------------------------------------------
# plan_faculty(master, cycle_code) -> dict faculty_email -> [offering_id, ...]
# ----------------------------------------------------------------------------
# PLAN (no send) the faculty fan-out: group every classified offering by its
# faculty_email. The grouping IS the scope guarantee for faculty — a teacher's
# message can only ever contain offerings whose faculty_email equals theirs, so
# "faculty see only their own subjects" holds by construction (there is no cross-
# faculty path). Offerings with no faculty_email are grouped under None and
# skipped by the sender (nobody to mail). Split out as a pure planner so the
# tests can assert the routing without sending anything.
# ----------------------------------------------------------------------------
def plan_faculty(master, cycle_code):
    classified = classified_offerings(master, cycle_code)
    plan = {}
    for oid in classified:
        off = master.execute(
            "SELECT id, faculty_email FROM offering WHERE id = ?", (oid,)).fetchone()
        if off is None:
            continue
        email = off["faculty_email"]
        plan.setdefault(email, []).append(oid)
    for email in plan:
        plan[email].sort()
    return plan


# ----------------------------------------------------------------------------
# plan_leader(master, user, cycle_code) -> [offering_id, ...]
# ----------------------------------------------------------------------------
# PLAN (no send) one leader's roll-up: the classified offerings this leader is
# allowed to see. Built STRICTLY from rbac.visible_offerings — the Module 1
# choke-point — intersected with the classified set. This is the single line
# that makes "an E01 HOD never receives E02 data" true for distribution: the
# offering list comes from RBAC, not from any query distribution writes itself.
# The tests call this directly to assert HOD ⊆ own dept and Dean == all.
# ----------------------------------------------------------------------------
def plan_leader(master, user, cycle_code):
    classified = classified_offerings(master, cycle_code)
    visible = rbac.visible_offerings(master, user, cycle_code)
    return sorted(o["id"] for o in visible if o["id"] in classified)


# ----------------------------------------------------------------------------
# _band_line(master, oid, cls_row) -> str — one human line for a roll-up body,
# e.g. "E01  CSE23CT201  Cloud Computing  [POOR]  overall=7.40  n=32". Pure
# formatting over the offering identity + its classification row.
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# _section_batch(section) -> "Sec A/Batch 1" | "Sec B/Batch 2" | "" (Aug 2026)
# ----------------------------------------------------------------------------
# The professor's clarity rule: a lab/course split across sections produces one
# scored offering PER SECTION, so a teacher can see two lines for the "same"
# course. Section A is Batch 1, Section B is Batch 2, and so on — spelling this
# out on every line ("Sec A/Batch 1") removes the confusion. A course with no
# real section (stored 'NA'/blank) yields an empty string, so single-section
# courses stay clean.
def _section_batch(section):
    s = (section or "").strip().upper()
    if not s or s in ("NA", "N/A", "-", "NONE"):
        return ""
    idx = {"A": "1", "B": "2", "C": "3", "D": "4", "E": "5", "F": "6"}.get(s)
    return "Sec %s/Batch %s" % (s, idx) if idx else "Sec %s" % s


def _band_line(master, oid, cls_row):
    off = master.execute(
        "SELECT dept_code, course_code, course_name, faculty, section "
        "FROM offering WHERE id = ?", (oid,)).fetchone()
    band = cls_row["band"] or "INSUFFICIENT"
    score = cls_row["overall_score"]
    score_s = ("%.2f" % score) if score is not None else "n/a"
    # The section/batch tag gets its own column so two sections of one course read
    # as "Sec A/Batch 1" and "Sec B/Batch 2" instead of two identical lines.
    sect = _section_batch(off["section"] if "section" in off.keys() else "")
    return "  %-4s %-12s %-26s %-14s [%s]  overall=%s  n=%s" % (
        off["dept_code"], off["course_code"],
        (off["course_name"] or "")[:26], sect, band, score_s, cls_row["n_responses"])


# ----------------------------------------------------------------------------
# _score(master, cycle, cycle_row, oid, dl_weight, cache) -> result | None
# ----------------------------------------------------------------------------
# Score ONE offering (frozen scoring engine), cached by offering id. On a test
# cycle it stamps the 'TEST DATA' watermark on the result so any PDF built from it
# carries the mark (§9.1). Returns None for an unscoreable offering.
# ----------------------------------------------------------------------------
def _score(master, cycle, cycle_row, oid, dl_weight, cache):
    if oid in cache:
        return cache[oid]
    res = scoring.score_offering(master, cycle, oid, dl_weight)
    # Watermark at EVERY non-production level (1, 2, 3) — only a promoted level-0
    # production cycle prints clean, official copies (emailer.is_watermarked).
    if res is not None and emailer.is_watermarked(cycle_row):
        res["watermark"] = "TESTING ONLY"
    cache[oid] = res
    return res


# ----------------------------------------------------------------------------
# _combined_pdf(results, faculty_label, cycle_row) -> (filename, bytes)
# ----------------------------------------------------------------------------
# Build ONE combined PDF containing ALL of a teacher's course reports (v3.4 — the
# decision was a single PDF per teacher, not one per course). Reuses the existing
# multi-report builder (report_export.build_batch_pdf), which already lays each
# course out with the confidential footer and "Page X of Y" across the whole file.
# ----------------------------------------------------------------------------
def _combined_pdf(results, faculty_label, cycle_row):
    import io
    buf = io.BytesIO()
    report_export.build_batch_pdf(results, buf)
    safe = "".join(ch if ch.isalnum() else "_" for ch in (faculty_label or "faculty"))
    return ("Feedback_%s_%s.pdf" % (cycle_row["code"], safe[:40]), buf.getvalue())


# ----------------------------------------------------------------------------
# _faculty_body(master, cycle, cycle_row, faculty_email, oids, classified, base,
#               intro) -> str
# ----------------------------------------------------------------------------
# Build ONE teacher's single email body (the chosen design: one email per teacher,
# with the ATR link INLINE in the same message — not a separate ATR email). It is:
#   * a friendly intro (the admin's editable text, or a default),
#   * one line per course with its score + GOOD/POOR band,
#   * and, immediately under each POOR course, an "ACTION REQUIRED" line carrying a
#     freshly-issued one-time "File ATR" magic link.
# For every POOR course it also ensures the EXPECTED atr row exists so the HOD sees
# it pending even before the teacher clicks. The per-course report PDFs are attached
# by the caller. Writes tokens/atr rows to the per-cycle DB; the caller commits.
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# _is_external_offering(master, offering_id) -> bool   (external-faculty rule)
# ----------------------------------------------------------------------------
# True when this offering is taught by the "External" placeholder faculty, i.e.
# the faculty's home department is the External dept code 'EXT'. These courses
# have no real person to file an ATR, so they follow the HOD-filed path
# (ensure_hod_filed_atr) instead of the normal faculty EXPECTED path, and they
# never get a faculty results email or File-ATR link (there is no email). We
# resolve it through the Faculty Master by emp-no, exactly like effective_dept.
# ----------------------------------------------------------------------------
def _is_external_offering(master, offering_id):
    row = master.execute(
        "SELECT f.home_dept_code AS hd "
        "FROM offering o LEFT JOIN faculty f ON f.emp_no = o.faculty_id "
        "WHERE o.id = ?", (offering_id,)).fetchone()
    return bool(row) and (row["hd"] == "EXT")


def _faculty_body(master, cycle, cycle_row, faculty_email, oids, classified,
                  base, intro):
    lines = [intro.strip() if (intro and intro.strip()) else "Dear Faculty,", ""]
    lines += ["Your student-feedback results for %s:" % cycle_row["label"], ""]
    any_poor = False
    for oid in oids:
        cls = classified[oid]
        lines.append(_band_line(master, oid, cls))
        if cls["band"] == classification.BAND_POOR:
            any_poor = True
            # EXTERNAL-FACULTY rule: a course taught by the "External" placeholder
            # has no real teacher to file an ATR. Create the ATR in the HOD-filed
            # path (PENDING_HOD, owner HOD) and add NO faculty File-ATR link — the
            # EXT HOD writes the note and submits it upward instead. (In production
            # external faculty have a blank email so _faculty_body never runs for
            # them; this guard also covers the test-redirect case, where blank-email
            # teachers ARE included, so we still never mint them a dead link.)
            if _is_external_offering(master, oid):
                atr_workflow.ensure_hod_filed_atr(cycle, oid, cycle_row["code"])
                lines.append("      → EXTERNAL faculty: HOD will file the ATR note.")
            else:
                # Normal path: ensure the EXPECTED ATR row exists (shows on the HOD
                # dashboard immediately) and mint a one-time File-ATR link, placed
                # right under this course.
                atr_workflow.ensure_expected_atr(cycle, oid, cycle_row["code"])
                jti, _exp = faculty_tokens.issue(cycle, oid, faculty_email,
                                                 purpose=faculty_tokens.PURPOSE_ATR_FILE)
                lines.append("      → ACTION REQUIRED — file your ATR: %s"
                             % atr_file_url(base, jti))
    lines += ["", "Your feedback report for all your courses is attached as a single PDF."]
    if any_poor:
        lines.append("Course(s) marked [POOR] require an Action-Taken-Report (ATR) "
                     "via the link shown under them above.")
    lines += ["", "— Automated Feedback System, SRET"]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# _leader_body(master, cycle_row, oids, title) -> str — a roll-up body for a
# leader: a header plus one _band_line per offering in their RBAC scope, with a
# small GOOD/POOR/insufficient tally. Pure formatting.
# ----------------------------------------------------------------------------
def _leader_body(master, cycle_row, oids, title):
    classified = classified_offerings(master, cycle_row["code"])
    good = poor = insf = 0
    body_lines = []
    for oid in oids:
        cls = classified[oid]
        body_lines.append(_band_line(master, oid, cls))
        if cls["band"] == classification.BAND_GOOD:
            good += 1
        elif cls["band"] == classification.BAND_POOR:
            poor += 1
        else:
            insf += 1
    header = ["%s — %s" % (title, cycle_row["label"]), "",
              "Subjects: %d   GOOD: %d   POOR: %d   Insufficient: %d"
              % (len(oids), good, poor, insf), ""]
    footer = ["", "POOR subjects require an ATR from the faculty; you will be "
              "notified when one is filed for your review.",
              "", "— Automated Feedback System, SRET"]
    return "\n".join(header + body_lines + footer)


# ----------------------------------------------------------------------------
# distribute_cycle(master, cycle, cycle_row, base_url=None, roles=None,
#                  classify_first=True, email_intro=None) -> summary
# ----------------------------------------------------------------------------
# THE job (spec §7). Steps:
#   0. (optional) run Module 1's classification.classify_cycle so the bands are
#      fresh before we fan out — the job "runs after scoring+banding".
#   1. FACULTY fan-out: ONE email per teacher covering all their courses, with a
#      SINGLE combined report PDF attached, and — inline in that same email — a
#      one-time "File ATR" link under every POOR course (plus an EXPECTED atr row
#      ensured per POOR course so the HOD sees it pending).
#   2. LEADER roll-ups: for every seated HOD (dept roll-up), the Vice Dean and the
#      Dean (college roll-up) — each built through rbac.visible_offerings so scope
#      is enforced by the Module 1 choke-point.
# All sends go through emailer.send_batch. Commits the per-cycle DB once (token
# issues + ensured ATR rows). Returns a summary the admin screen logs.
#
# `roles` optionally restricts which audiences to send to (subset of
# {'FACULTY','HOD','VICE_DEAN','DEAN'}); default is all four. `email_intro` is the
# admin's editable greeting/preamble that tops each faculty email; None uses a
# sensible default. `redirect_to` (TESTING) sends EVERY faculty email to that one
# address instead of the teachers — and includes teachers whose email column is
# blank — so a dry run can be checked in one inbox. When set, it takes precedence
# over the cycle's is_test redirect so mail lands exactly where you asked.
# ----------------------------------------------------------------------------
def distribute_cycle(master, cycle, cycle_row, base_url=None, roles=None,
                     classify_first=True, email_intro=None, redirect_to=None):
    base = (base_url or public_base_url()).rstrip("/")
    cycle_code = cycle_row["code"]
    redirect_to = (redirect_to or "").strip() or None
    want = set(roles) if roles else {
        atr_workflow.ROLE_FACULTY, atr_workflow.ROLE_HOD,
        atr_workflow.ROLE_VICE_DEAN, atr_workflow.ROLE_DEAN}

    summary = {"cycle": cycle_code, "classified": 0,
               "faculty_emails": 0, "atr_links": 0, "leader_emails": 0,
               "errors": [], "mode": None}

    # ---- 0. Ensure bands are fresh (reuse Module 1; idempotent upsert) -------
    if classify_first:
        classification.classify_cycle(master, cycle, cycle_row)
        master.commit()

    classified = classified_offerings(master, cycle_code)
    summary["classified"] = len(classified)
    if not classified:
        return summary  # nothing scored/banded → nothing to distribute

    # ---- 0b. EXTERNAL-FACULTY ATRs (professor's rule) -----------------------
    # Independently of the faculty fan-out below (which may be skipped, or may
    # never run for a blank-email External teacher in production), make sure every
    # POOR course taught by the External placeholder has a HOD-filed ATR waiting in
    # the EXT HOD's queue (PENDING_HOD). This guarantees the External department's
    # poor courses always reach Dr Arul Chezhian for his note → Vice Dean, whether
    # or not any email goes out. Idempotent, so re-running distribution is safe.
    for oid in classified:
        if (classified[oid]["band"] == classification.BAND_POOR
                and _is_external_offering(master, oid)):
            atr_workflow.ensure_hod_filed_atr(cycle, oid, cycle_code)
    cycle.commit()

    # ---- 1. FACULTY fan-out (one email each, ONE combined PDF, inline ATR) ----
    if atr_workflow.ROLE_FACULTY in want:
        dl_weight = scoring.get_discussed_late_weight(master)
        score_cache = {}                     # oid -> result | None

        # Group classified offerings by TEACHER identity. Prefer the faculty email;
        # fall back to faculty_id then name, so teachers still split into separate
        # emails even when the email column is blank (common while testing).
        groups = {}
        for oid in classified:
            off = master.execute(
                "SELECT faculty, faculty_email, faculty_id FROM offering WHERE id=?",
                (oid,)).fetchone()
            if off is None:
                continue
            # (Module 5) Resolve the address from the Faculty Master by emp-no —
            # the allocation no longer carries emails, so this is what gives the
            # teacher's results email somewhere to go.
            email = (faculty_email_for(master, off) or "").strip()
            key = (email or ("fid:%s" % (off["faculty_id"] or ""))
                   or ("name:%s" % (off["faculty"] or "")) or ("oid:%s" % oid))
            g = groups.setdefault(key, {"email": email,
                                        "faculty": (off["faculty"] or ""), "oids": []})
            g["oids"].append(oid)

        # Where each teacher's mail goes:
        #   redirect_to set  -> that one test inbox (blank-email teachers included);
        #   else real email  -> the teacher (the mailer still re-routes it to the
        #                       staff test inbox at Level 1, per the level table);
        #   else, at Level 1 -> the staff test inbox, so blank-email teachers are
        #                       STILL delivered for the tester to inspect;
        #   else             -> skipped (nowhere real to send).
        # The staff test inbox at Level 1 comes from the same routing table the
        # mailer obeys, so distribution and the mailer can never disagree.
        level = emailer.test_level_of(cycle_row)
        test_fallback = emailer.redirect_target(level, "faculty")  # staff inbox @L1, else None

        messages = []
        for g in groups.values():
            to_addr = redirect_to or g["email"] or test_fallback
            if not to_addr:
                continue
            oids = sorted(g["oids"])
            # Score each course, then build ONE combined PDF for this teacher.
            results = [r for r in
                       (_score(master, cycle, cycle_row, oid, dl_weight, score_cache)
                        for oid in oids) if r is not None]
            attachments = ([_combined_pdf(results, g["faculty"] or g["email"], cycle_row)]
                           if results else [])
            body = _faculty_body(master, cycle, cycle_row, g["email"], oids,
                                 classified, base, email_intro)
            # When redirecting for a test, note who the mail was really meant for.
            if redirect_to and (g["email"] or g["faculty"]):
                body = ("*** TEST REDIRECT — intended for: %s ***\n\n"
                        % (g["email"] or g["faculty"])) + body
            summary["atr_links"] += sum(
                1 for oid in oids
                if classified[oid]["band"] == classification.BAND_POOR)
            messages.append({"to": to_addr, "body": body,
                             "attachments": attachments})

        # Persist the tokens + ensured ATR rows written by _faculty_body, once.
        cycle.commit()
        if messages:
            # If the admin typed an explicit redirect_to, mail already all points
            # there — pass test_level=0 so the mailer does NOT redirect a second
            # time. Otherwise hand the mailer the cycle's real level + the 'faculty'
            # audience so it applies the §9 routing (real at L0/L2/L3, staff inbox
            # at L1).
            send_level = 0 if redirect_to else level
            res = emailer.send_batch(
                Config.BASE_DIR,
                "[AFS] Your feedback results — %s" % cycle_row["label"],
                messages, test_level=send_level, audience="faculty")
            summary["faculty_emails"] = res["count"]
            summary["mode"] = res["mode"]
            summary["errors"] += res.get("errors", [])

    # ---- 2. LEADER roll-ups (routed through rbac) ----------------------------
    leader_messages = []
    # HODs: one per department that has a seated HOD; each scoped to its dept.
    if atr_workflow.ROLE_HOD in want:
        hods = master.execute(
            "SELECT DISTINCT u.* FROM app_user u WHERE u.role = 'HOD' "
            "AND u.status = 'active'").fetchall()
        for hod in hods:
            oids = plan_leader(master, hod, cycle_code)
            if not oids:
                continue
            body = _leader_body(master, cycle_row, oids,
                                "Departmental feedback roll-up")
            leader_messages.append({"to": hod["email"], "body": body})

    # Vice Dean(s) + Dean: college-wide roll-up (scope resolves to all via rbac).
    for role in (atr_workflow.ROLE_VICE_DEAN, atr_workflow.ROLE_DEAN):
        if role not in want:
            continue
        leaders = master.execute(
            "SELECT * FROM app_user WHERE role = ? AND status = 'active'",
            (role,)).fetchall()
        for ldr in leaders:
            oids = plan_leader(master, ldr, cycle_code)
            if not oids:
                continue
            title = ("College feedback roll-up (Vice Dean)"
                     if role == atr_workflow.ROLE_VICE_DEAN
                     else "College feedback roll-up (Dean)")
            body = _leader_body(master, cycle_row, oids, title)
            leader_messages.append({"to": ldr["email"], "body": body})

    if leader_messages:
        # Leaders route like faculty: real at Levels 0/2/3, staff test inbox at L1.
        res = emailer.send_batch(
            Config.BASE_DIR,
            "[AFS] Feedback roll-up — %s" % cycle_row["label"],
            leader_messages, test_level=emailer.test_level_of(cycle_row),
            audience="leader")
        summary["leader_emails"] = res["count"]
        summary["mode"] = summary["mode"] or res["mode"]
        summary["errors"] += res.get("errors", [])

    return summary
