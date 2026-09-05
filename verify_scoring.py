# ============================================================================
# verify_scoring.py  —  THE VERIFICATION GATE (spec Section 16, Phase 3)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# The spec is emphatic (Sections 2 & 16): our scoring must reproduce the four
# approved "Feedback Report V2.0" workbooks EXACTLY. This script is the proof.
# It:
#   1. Reads the ORIGINAL responses straight out of each workbook's Sheet1
#      (the same raw answers the approved formulas were computed from),
#   2. Builds the question metadata from seed_data.py (single source of truth for
#      sections + scale weights), mapping workbook columns 1:1 to template
#      questions (the column order matches the template order by construction),
#   3. Feeds them through scoring.compute_scores() — the very function the live
#      report engine uses — and
#   4. Compares every section score AND the overall score to the value the
#      workbook itself computes (read with data_only=True), asserting they match
#      to a tight tolerance (1e-9).
#
# If this script prints ALL PASS, the engine is faithful to the approved
# formulas and the report exports built on top of it are trustworthy. Run it
# with:  python verify_scoring.py   (from the app/ folder).
# ----------------------------------------------------------------------------

import os
import openpyxl

import seed_data
import scoring


# ----------------------------------------------------------------------------
# Build a { scale_code -> options list } lookup from seed_data.SCALES so we can
# attach the correct options (labels + weights + fractions) to each question.
# ----------------------------------------------------------------------------
def _scale_options():
    out = {}
    for sc in seed_data.SCALES:
        out[sc["code"]] = [
            {"label": lbl, "weight": w, "fraction": fr, "display_order": order}
            for (lbl, w, fr, order) in sc["options"]
        ]
    return out


SCALE_OPTIONS = _scale_options()
FREE_TEXT_SCALES = {sc["code"] for sc in seed_data.SCALES if sc["is_free_text"]}


# ----------------------------------------------------------------------------
# Build the `questions` list (as scoring.compute_scores expects) for a category,
# straight from the seed template definition. Question IDs are just the 1-based
# position, which is also the display order and the workbook column offset.
# ----------------------------------------------------------------------------
def build_questions(category_code):
    tpl = next(t for t in seed_data.TEMPLATES if t["category_code"] == category_code)
    questions = []
    for i, (section, text, scale_code) in enumerate(tpl["questions"], start=1):
        questions.append({
            "id": i,
            "section": section,
            "text": text,
            "scale_code": scale_code,
            "is_free_text": scale_code in FREE_TEXT_SCALES,
            "display_order": i,
            "options": SCALE_OPTIONS.get(scale_code, []),
        })
    return questions


# ----------------------------------------------------------------------------
# Read the raw response rows out of a workbook's Sheet1. Data columns start at
# column B (index 2) and run 1:1 with the template questions. We stop at the
# first fully-blank row (the aggregation block begins after a blank separator),
# so we capture exactly the student rows the approved formula ranged over.
# The syllabus column is stored numerically (1 / 0.8 / 0.6) in the workbook; we
# pass values through untouched — scoring._coerce_fraction handles the number
# form, and the option-label form used by the live student app.
# ----------------------------------------------------------------------------
def read_responses(path, sheet_name, n_questions, n_data_rows):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet_name]
    responses = []
    for row in range(2, 2 + n_data_rows):          # rows 2 .. (2+n-1)
        rec = {}
        any_value = False
        for qidx in range(1, n_questions + 1):     # question ids 1..n
            col = qidx + 1                          # +1 because col A is Timestamp
            val = ws.cell(row=row, column=col).value
            if val is not None and str(val).strip() != "":
                rec[qidx] = val
                any_value = True
        # Keep the row even if sparse, but skip an entirely empty trailing row.
        if any_value:
            responses.append(rec)
    return responses


# ----------------------------------------------------------------------------
# The four cases. For each: workbook path + sheet, the category, the number of
# scored+open questions, the data-row span the workbook formula used, and the
# EXPECTED section/overall values (read live from the workbook, below).
# ----------------------------------------------------------------------------
REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "Report")

CASES = [
    # (label, filename, sheet, category, n_questions, n_data_rows, avg_row,
    #    expected_cell_map {section_key/overall: worksheet cell})
    # avg_row = the worksheet row holding each question's per-question average;
    #           used only to borrow the Post-Assessment question's (unaffected)
    #           average when rebuilding Theory's mixed Exam section.
    ("Theory", "Feedback Report V2.0 - T.xlsx", "Sheet1", "THEORY", 13, 105, 113,
     {"syllabus": "C115", "faculty": "C116", "material": "C117",
      "exam": "C118", "overall": "C120"}),
    ("Lab", "Feedback Report V2.0 - L (1).xlsx", "Sheet 1", "LAB", 12, 68, 76,
     {"syllabus": "C78", "faculty": "C79", "resources": "C80",
      "exam": "C81", "overall": "C83"}),
    ("Skill", "Feedback Report V2.0 - SL.xlsx", "Sheet1", "SKILL", 14, 68, 76,
     {"syllabus": "C78", "faculty": "C79", "material": "C80",
      "assessment": "C81", "overall": "C83"}),
    ("AE", "Feedback Report V2.0 - AE.xlsx", "Sheet1", "AE", 9, 219, 227,
     {"training": "C240", "material": "C241", "knowledge": "C242",
      "overall_sec": "C243", "overall": "C245"}),
]


# ----------------------------------------------------------------------------
# CORRECTED-SCALE VERIFICATION (2026-09-05)
# ----------------------------------------------------------------------------
# History: this harness originally asserted the engine reproduced each approved
# workbook EXACTLY. On 2026-09-05 the AGREE5 scale was corrected so plain "Agree"
# (weight 8) outranks "Moderately Agree" (weight 6) -- the two weights the
# original workbooks had swapped. The workbooks stay correct for EVERYTHING else,
# so we change as little as possible:
#   * A section with NO agree-scale questions (Syllabus; every AE section) is
#     unaffected and is taken verbatim from the workbook cell.
#   * A section WITH agree-scale questions is rebuilt as the mean of its members,
#     where each AGREE5 member is recomputed with the corrected weights (a plain
#     weighted mean, independent of scoring.py's aggregation), and any non-AGREE5
#     member (only Theory's Post-Assessment question) keeps its untouched
#     workbook per-question average.
#   * The grand total is the mean of the (possibly-updated) section scores.
# The engine is asserted to equal this corrected expectation; the printout also
# shows the original workbook value so a reviewer sees exactly what moved.
# ----------------------------------------------------------------------------

# Corrected AGREE5 weights, pulled from seed_data (single source of truth).
AGREE5_W = {o["label"]: o["weight"] for o in SCALE_OPTIONS["AGREE5"]}

# POST_ASSESS weights (Theory "answer key" question), corrected 2026-09-05:
# Discussed Completely 10, Partially Discussed 6, Discussed Late 1, Not Discussed 0.
POST_W = {o["label"]: (o["weight"] if o["weight"] is not None else 0.0)
          for o in SCALE_OPTIONS["POST_ASSESS"]}
# The configurable "Discussed Late" weight the report callers pass into the
# scorer (scoring.get_discussed_late_weight); mirrored so verify matches prod.
DL_WEIGHT = POST_W.get("Discussed Late", 0.0)


def _col_letter(display_order):
    """Workbook data columns are A=Timestamp, B=Q1, C=Q2, ... so a question with
    display_order d sits in column chr(ord('A') + d)."""
    return chr(ord("A") + display_order)


def _agree5_avg(qid, responses):
    """Plain corrected-weight average for one AGREE5 question, computed straight
    from the raw responses (independent of scoring.py's aggregation path)."""
    counts = {}
    n = 0
    for r in responses:
        v = r.get(qid)
        if v is None:
            continue
        v = str(v).strip()
        if v == "":
            continue
        n += 1                      # any non-blank answer counts in the denominator
        counts[v] = counts.get(v, 0) + 1
    if n == 0:
        return None
    num = sum(c * AGREE5_W[l] for l, c in counts.items() if AGREE5_W.get(l) is not None)
    return num / n


def _post_assess_avg(qid, responses, n_recorded):
    """Corrected-weight average for the Post-Assessment question, using the
    approved denominator (the canonical recorded-student count) exactly as the
    engine does. Independent of scoring.py's aggregation path."""
    if not n_recorded:
        return None
    num = 0.0
    for r in responses:
        v = r.get(qid)
        if v is None:
            continue
        v = str(v).strip()
        if v == "":
            continue
        num += POST_W.get(v, 0.0)
    return num / n_recorded


def expected_corrected(report_key, questions, responses, ws_cached, avg_row, sec_cell, n_recorded):
    """Return ({section_key: expected_score}, expected_overall) under the
    corrected scale, using the workbook as truth for all unaffected parts."""
    struct = scoring.REPORT_STRUCTURE[report_key]
    sec_expected = {}
    for sec in struct:
        members = [q for q in questions if q["section"] in sec["sections"]]
        has_agree = any(q["scale_code"] == "AGREE5" for q in members)
        if not has_agree:
            cell = sec_cell.get(sec["key"])
            sec_expected[sec["key"]] = ws_cached[cell].value if cell else None
            continue
        vals = []
        for q in members:
            if q["scale_code"] == "AGREE5":
                vals.append(_agree5_avg(q["id"], responses))
            elif q["scale_code"] == "SYLLABUS":
                continue
            else:
                # Non-AGREE5 member = Theory's Post-Assessment question, whose
                # weights were ALSO corrected (Discussed Late 1, Not Discussed 0),
                # so we recompute it here rather than borrowing the workbook value.
                vals.append(_post_assess_avg(q["id"], responses, n_recorded))
        nums = [v for v in vals if v is not None]
        sec_expected[sec["key"]] = (sum(nums) / len(nums)) if nums else None
    sec_scores = [sec_expected[s["key"]] for s in struct]
    nums = [v for v in sec_scores if v is not None]
    overall = (sum(nums) / len(nums)) if nums else None
    return sec_expected, overall


def run():
    print("=" * 78)
    print("SCORING VERIFICATION - engine vs approved workbook (AGREE5 weights corrected)")
    print("=" * 78)
    all_ok = True
    TOL = 1e-9

    for (label, fname, sheet, cat, nq, nrows, avg_row, cell_map) in CASES:
        path = os.path.join(REPORT_DIR, fname)
        questions = build_questions(cat)
        responses = read_responses(path, sheet, nq, nrows)
        report_key = {"THEORY": "T", "LAB": "L", "SKILL": "SL", "AE": "AE"}[cat]

        result = scoring.compute_scores(report_key, questions, responses,
                                        discussed_late_weight=DL_WEIGHT)
        sec_by_key = {s["key"]: s["score"] for s in result["section_scores"]}
        grand_total = result["overall"]

        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb["Sheet1"] if "Sheet1" in wb.sheetnames else wb["Sheet 1"]

        struct = scoring.REPORT_STRUCTURE[report_key]
        sec_cell = {s["key"]: cell_map.get(s["key"]) for s in struct}
        if "overall_sec" in cell_map:
            sec_cell["overall"] = cell_map["overall_sec"]

        exp_sec, exp_overall = expected_corrected(
            report_key, questions, responses, ws, avg_row, sec_cell, result["n_recorded"])

        print("\n%s  (n_recorded computed = %s, responses read = %d)"
              % (label, result["n_recorded"], len(responses)))
        for key, exp_cell in cell_map.items():
            if key == "overall":
                gval, eval_ = grand_total, exp_overall
            elif key == "overall_sec":
                gval, eval_ = sec_by_key.get("overall"), exp_sec.get("overall")
            else:
                gval, eval_ = sec_by_key.get(key), exp_sec.get(key)

            wb_orig = ws[exp_cell].value

            if gval is None or eval_ is None:
                ok = (gval is None and eval_ is None)
                diff = "n/a"
            else:
                diff = abs(gval - eval_)
                ok = diff <= TOL
            all_ok = all_ok and ok
            status = "OK " if ok else "XX "
            gstr = "None" if gval is None else ("%.6f" % gval)
            estr = "None" if eval_ is None else ("%.6f" % eval_)
            moved = ""
            try:
                if wb_orig is not None and eval_ is not None and abs(float(wb_orig) - eval_) > 1e-9:
                    moved = "  (was %.6f in workbook; corrected)" % float(wb_orig)
            except (TypeError, ValueError):
                pass
            print("   [%s] %-12s engine=%s  expected=%s  d=%s%s"
                  % (status, key, gstr, estr, diff, moved))

    print("\n" + "=" * 78)
    print("RESULT:", "ALL PASS - engine matches the corrected expectation"
          if all_ok else "FAILURES - see XX lines above")
    print("=" * 78)
    return all_ok


if __name__ == "__main__":
    ok = run()
    raise SystemExit(0 if ok else 1)
