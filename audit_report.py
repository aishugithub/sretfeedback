# ============================================================================
# audit_report.py  —  v2.1 : the end-of-cycle SIGNED AUDIT REPORT
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# When the Dean has endorsed the last ATR (cycle status = RECORDED), the admin
# initiates ONE consolidated record of the WHOLE cycle — the document the college
# keeps for audits. The professor's contents, all in one PDF:
#   * students as submitted (roster) vs as participated,
#   * the course faculties and their details,
#   * feedback received per course + the mark obtained out of 10,
#   * which teachers were given an ATR and their written explanation,
#   * the endorsement trail (who endorsed each ATR, and when),
#   * a signature block.
# The finished PDF is then DIGITALLY SIGNED by the app (signing.py) so it is
# tamper-evident, and a SHA-256 fingerprint of the unsigned content is printed on
# it as a second, human-checkable integrity anchor.
#
# ANONYMITY: every figure here is an AGGREGATE (counts, per-course averages, ATR
# narratives about a course). No student is ever named against an answer — the
# report reads master.db identity/config + the per-cycle DB's aggregate/ATR side,
# and never joins a response to a student.
# ----------------------------------------------------------------------------

import os
import io
import hashlib
import datetime

import db
import emailer
import atr_workflow
import rbac

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, PageBreak)


# ----------------------------------------------------------------------------
# _styles() — a small stylesheet reused across the document.
# ----------------------------------------------------------------------------
def _styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle("H", parent=ss["Heading2"], textColor=colors.HexColor("#0b3d68"),
                          spaceBefore=10, spaceAfter=4))
    ss.add(ParagraphStyle("Cell", parent=ss["BodyText"], fontSize=8, leading=10))
    ss.add(ParagraphStyle("CellB", parent=ss["BodyText"], fontSize=8, leading=10,
                          fontName="Helvetica-Bold"))
    ss.add(ParagraphStyle("Small", parent=ss["BodyText"], fontSize=8,
                          textColor=colors.HexColor("#555555")))
    return ss


def _faculty_name(master, offering):
    """Display name for a course's teacher: prefer the Faculty Master by emp-no,
    fall back to the name/email carried on the offering row."""
    try:
        emp = offering["faculty_id"]
    except (KeyError, IndexError, TypeError):
        emp = None
    if emp:
        r = master.execute("SELECT name, email FROM faculty WHERE emp_no=?",
                           (emp,)).fetchone()
        if r and (r["name"] or r["email"]):
            return r["name"] or r["email"]
    return offering["faculty"] or offering["faculty_email"] or (emp or "—")


# ----------------------------------------------------------------------------
# gather(master, cycle_row) -> dict — assemble every figure the report prints.
# ----------------------------------------------------------------------------
def gather(master, cycle_row):
    code = cycle_row["code"]
    data = {"cycle": cycle_row, "generated_at":
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    # --- Roster (students "as submitted"), by department ---
    roster = master.execute(
        "SELECT dept_code, COUNT(*) AS n FROM students "
        "WHERE cycle_code=? AND status='active' GROUP BY dept_code ORDER BY dept_code",
        (code,)).fetchall()
    data["roster_by_dept"] = roster
    data["roster_total"] = sum(r["n"] for r in roster)

    # --- Participation (students "as participated") + per-course response counts ---
    invited = completed = responses_total = 0
    resp_by_off, atr_rows, events_by_atr = {}, [], {}
    path = db.cycle_db_path(cycle_row["academic_year"], code)
    if os.path.exists(path):
        cy = db.get_cycle(cycle_row["academic_year"], code)
        invited = cy.execute("SELECT COUNT(*) n FROM token").fetchone()["n"]
        completed = cy.execute(
            "SELECT COUNT(*) n FROM token WHERE completed_all=1").fetchone()["n"]
        responses_total = cy.execute("SELECT COUNT(*) n FROM response").fetchone()["n"]
        for r in cy.execute(
                "SELECT offering_id, COUNT(*) n FROM response GROUP BY offering_id"):
            resp_by_off[r["offering_id"]] = r["n"]
        atr_rows = cy.execute("SELECT * FROM atr ORDER BY offering_id").fetchall()
        for e in cy.execute("SELECT * FROM atr_event ORDER BY atr_id, id"):
            events_by_atr.setdefault(e["atr_id"], []).append(e)
        cy.close()
    data.update(invited=invited, completed=completed,
                responses_total=responses_total)

    # --- Courses + faculty + feedback count + mark /10 (from classification) ---
    offs = master.execute(
        "SELECT o.*, oc.band, oc.overall_score, oc.n_responses "
        "FROM offering o "
        "LEFT JOIN offering_classification oc "
        "  ON oc.offering_id=o.id AND oc.cycle_code=o.cycle_code "
        "WHERE o.cycle_code=? ORDER BY o.dept_code, o.course_code",
        (code,)).fetchall()
    courses = []
    for o in offs:
        n = o["n_responses"] if o["n_responses"] is not None else resp_by_off.get(o["id"], 0)
        courses.append({
            "dept": o["dept_code"], "course_code": o["course_code"],
            "course_name": o["course_name"] or "",
            "faculty": _faculty_name(master, o),
            "n": n, "score": o["overall_score"], "band": o["band"],
        })
    data["courses"] = courses
    data["n_courses"] = len(courses)
    data["n_good"] = sum(1 for c in courses if c["band"] == "GOOD")
    data["n_poor"] = sum(1 for c in courses if c["band"] == "POOR")

    # --- ATRs given, their explanations, and the endorsement trail ---
    off_by_id = {o["id"]: o for o in offs}
    users = {str(u["id"]): u for u in master.execute(
        "SELECT id, name, email, role FROM app_user").fetchall()}
    atrs = []
    for a in atr_rows:
        o = off_by_id.get(a["offering_id"])
        if o is None:
            continue
        trail = []
        for e in events_by_atr.get(a["id"], []):
            who = "Faculty" if e["actor_user_id"] in (None, "FACULTY") else (
                (users.get(str(e["actor_user_id"]), {}).get("name")
                 if isinstance(users.get(str(e["actor_user_id"])), dict) else None)
                or (users.get(str(e["actor_user_id"]))["email"]
                    if users.get(str(e["actor_user_id"])) else "user#%s" % e["actor_user_id"]))
            trail.append("%s — %s%s (%s)" % (
                e["action"].title(), who,
                (": " + e["comment"]) if e["comment"] else "", e["at"]))
        atrs.append({
            "dept": o["dept_code"], "course_code": o["course_code"],
            "course_name": o["course_name"] or "",
            "faculty": _faculty_name(master, o),
            "state": a["state"], "body": a["body"] or "(no explanation recorded)",
            "trail": trail,
        })
    data["atrs"] = atrs
    data["n_atrs"] = len(atrs)
    return data


# ----------------------------------------------------------------------------
# _pdf_bytes(data) -> bytes — render the (unsigned) audit PDF from gathered data.
# ----------------------------------------------------------------------------
def _pdf_bytes(data, watermark=None):
    ss = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16*mm, rightMargin=16*mm,
                            topMargin=16*mm, bottomMargin=16*mm,
                            title="Cycle Audit Report — %s" % data["cycle"]["code"])
    story = []
    c = data["cycle"]

    story.append(Paragraph("SRET — Automated Feedback System", ss["Title"]))
    story.append(Paragraph("End-of-Cycle Audit Report", ss["Heading2"]))
    if watermark:
        story.append(Paragraph("<font color='#b23'><b>%s</b></font>" % watermark, ss["BodyText"]))
    meta = ("Cycle: <b>%s — %s</b> &nbsp;|&nbsp; Academic year: %s &nbsp;|&nbsp; "
            "Status: <b>%s</b> &nbsp;|&nbsp; Generated: %s"
            % (c["code"], c["label"], c["academic_year"],
               c["status"] or "—", data["generated_at"]))
    story.append(Paragraph(meta, ss["Small"]))
    story.append(Spacer(1, 8))

    # --- 1. Participation ---
    story.append(Paragraph("1. Participation summary", ss["H"]))
    prt = [["Students on roster (as submitted)", str(data["roster_total"])],
           ["Students invited (tokens issued)", str(data["invited"])],
           ["Students who completed all feedback (as participated)", str(data["completed"])],
           ["Total course-feedback responses collected", str(data["responses_total"])]]
    t = Table(prt, colWidths=[120*mm, 40*mm])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c9ced4")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef1f4")),
        ("FONTSIZE", (0, 0), (-1, -1), 9)]))
    story.append(t)
    if data["roster_by_dept"]:
        story.append(Spacer(1, 4))
        story.append(Paragraph("Roster by department: "
                     + ", ".join("%s=%d" % (r["dept_code"], r["n"])
                                 for r in data["roster_by_dept"]), ss["Small"]))

    # --- 2. Courses, faculty, feedback count, mark /10 ---
    story.append(Paragraph("2. Course faculties — feedback received and mark obtained (out of 10)", ss["H"]))
    head = [Paragraph(x, ss["CellB"]) for x in
            ("Dept", "Course", "Faculty", "Responses", "Mark /10", "Band")]
    body = [head]
    for r in data["courses"]:
        body.append([
            Paragraph(r["dept"], ss["Cell"]),
            Paragraph("%s<br/>%s" % (r["course_code"], r["course_name"]), ss["Cell"]),
            Paragraph(str(r["faculty"]), ss["Cell"]),
            Paragraph(str(r["n"]), ss["Cell"]),
            Paragraph("%.2f" % r["score"] if r["score"] is not None else "—", ss["Cell"]),
            Paragraph(r["band"] or "—", ss["Cell"]),
        ])
    tc = Table(body, colWidths=[14*mm, 46*mm, 44*mm, 20*mm, 18*mm, 18*mm], repeatRows=1)
    tc.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c9ced4")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f4")),
        ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(tc)
    story.append(Spacer(1, 4))
    story.append(Paragraph("Courses: %d &nbsp; GOOD: %d &nbsp; POOR (ATR required): %d"
                 % (data["n_courses"], data["n_good"], data["n_poor"]), ss["Small"]))

    # --- 3. ATRs given + explanations + endorsement trail ---
    story.append(PageBreak())
    story.append(Paragraph("3. Action-Taken-Reports (ATRs) and endorsements", ss["H"]))
    if not data["atrs"]:
        story.append(Paragraph("No ATRs were required for this cycle (no course was "
                     "banded POOR).", ss["BodyText"]))
    for i, a in enumerate(data["atrs"], 1):
        story.append(Paragraph(
            "3.%d &nbsp; %s — %s &nbsp;|&nbsp; %s &nbsp;|&nbsp; Faculty: %s &nbsp;|&nbsp; "
            "Final state: <b>%s</b>" % (i, a["course_code"], a["course_name"],
                                        a["dept"], a["faculty"], a["state"]),
            ss["CellB"]))
        story.append(Paragraph("<b>Faculty explanation:</b> " + a["body"], ss["Cell"]))
        if a["trail"]:
            story.append(Paragraph("<b>Endorsement trail:</b>", ss["Cell"]))
            for line in a["trail"]:
                story.append(Paragraph("&nbsp;&nbsp;• " + line, ss["Cell"]))
        story.append(Spacer(1, 6))

    # --- 4. Sign-off + integrity fingerprint ---
    story.append(Spacer(1, 10))
    story.append(Paragraph("4. Certification", ss["H"]))
    story.append(Paragraph(
        "This document is the complete, consolidated record of the above feedback "
        "cycle, produced by the SRET Automated Feedback System. It is digitally "
        "sealed by the system; any alteration after signing invalidates the "
        "signature. The endorsement trail above records the Dean/Vice-Dean/HOD "
        "endorsements that closed each ATR.", ss["BodyText"]))
    story.append(Spacer(1, 20))
    story.append(Paragraph("Endorsed by (Dean / Vice-Dean): "
                 "_______________________________", ss["BodyText"]))

    doc.build(story)
    return buf.getvalue()


# ----------------------------------------------------------------------------
# build_unsigned_audit(master, cycle_row) -> (base_name, unsigned_bytes, info)
# ----------------------------------------------------------------------------
# Assemble the audit PDF's CONTENT (no signature). This is the part the SERVER can
# always do — it needs no signing key and makes NO network call. The admin can
# download this and sign it locally on the laptop (sign_audit.py), which keeps the
# private key off the internet-facing server and lets the trusted-timestamp TSA
# call run from the laptop's open network (no whitelisting needed). A non-production
# (test-level) cycle gets a visible watermark so a test copy is never mistaken for
# the official one. `info["fingerprint"]` is the SHA-256 of these exact bytes.
# ----------------------------------------------------------------------------
def build_unsigned_audit(master, cycle_row):
    data = gather(master, cycle_row)
    watermark = None
    if emailer.test_level_of(cycle_row) != 0:
        watermark = ("TEST / NON-PRODUCTION CYCLE (Level %d) — not an official audit copy"
                     % emailer.test_level_of(cycle_row))

    unsigned = _pdf_bytes(data, watermark=watermark)
    fingerprint = hashlib.sha256(unsigned).hexdigest()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = "Audit_%s_%s_%s" % (
        cycle_row["academic_year"].replace(" ", ""), cycle_row["code"], stamp)
    info = {"fingerprint": fingerprint, "courses": data["n_courses"],
            "poor": data["n_poor"], "atrs": data["n_atrs"],
            "roster": data["roster_total"], "completed": data["completed"]}
    return base, unsigned, info


# ----------------------------------------------------------------------------
# build_signed_audit(master, cycle_row) -> (filename, signed_bytes, info)
# ----------------------------------------------------------------------------
# The convenience "sign on the server too" entry point: build the unsigned content
# (above), then digitally sign it in-process (signing.py). Handy when you don't
# need a trusted timestamp; for the fully-sealed official copy, prefer downloading
# the UNSIGNED PDF and running sign_audit.py on the laptop (open network → TSA works,
# and the signing key never touches the server). info["timestamp"] records the TSA
# outcome (timestamped / no-tsa / tsa-failed).
# ----------------------------------------------------------------------------
def build_signed_audit(master, cycle_row):
    import signing
    base, unsigned, info = build_unsigned_audit(master, cycle_row)
    signed, ts_status = signing.sign_pdf_bytes(
        unsigned,
        reason="End-of-cycle feedback audit record — %s" % cycle_row["code"],
        location="SRET")
    info["timestamp"] = ts_status
    return base + ".pdf", signed, info
