# ============================================================================
# scoring.py  —  The PER-CATEGORY scoring engine (spec Sections 10 & 11)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# Nights 1 & 2 built the data model and the collection flow: students submit,
# and their answers land — anonymously — in the per-cycle db as `response` +
# `answer` rows (Group B), while participation is tracked separately in `token`
# (Group A). Night 3's job is ANALYSIS: turn those raw answers into the exact
# numbers the four approved "Feedback Report V2.0" workbooks would produce, then
# (in report_export.py) render them as Excel + PDF.
#
# This module is the mathematical core. It is deliberately split into two layers:
#
#   1. A PURE function `compute_scores(report_key, questions, responses, ...)`
#      that takes plain Python data (no database, no Flask) and returns the full
#      score breakdown. Being pure makes it trivially testable — the Night-3
#      verification harness feeds it the ORIGINAL workbook responses and checks
#      the output matches the workbook to the decimal (see verify_scoring.py).
#
#   2. Thin DB adapters (`load_offering_dataset`, `score_offering`) that pull a
#      course-offering's questions from master.db and its anonymous answers from
#      the per-cycle db, then call the pure function. These honour the anonymity
#      boundary: we read Group C (config) from master and Group B (answers) from
#      the cycle file, and NEVER read the token table while scoring — the report
#      is about a COURSE, never about individual students.
#
# WHY REPRODUCE, NOT REDESIGN (spec Section 2 "verbatim formulas"): the college
# has already approved these formulas, quirks and all. Our credibility depends on
# matching them exactly — including the approved oddities documented below:
#   * "Moderately Agree" (weight 8) OUTRANKS "Agree" (weight 6).
#   * The Theory "Post Assessment" average divides by the SAME respondent count
#     the sheet uses for every question (the column-C count), not by its own
#     answered count, and "Discussed Late" contributes 0 by default.
#   * Syllabus (Q1) is scored as the mean of stored fractions (1.0/0.8/0.6) x 10,
#     not through the weighted-count path the other questions use.
# ----------------------------------------------------------------------------

# We only need the standard library here; the pure math layer has no dependency
# on Flask, openpyxl or SQLite, which is what keeps it easy to unit-test.
import json


# ============================================================================
# SECTION 1 — THE APPROVED REPORT STRUCTURE, PER CATEGORY (spec Sections 10-11)
# ============================================================================
# Each report has an ordered list of SECTIONS, and the "Overall Score" is the
# simple average of those section scores. The four categories group their
# questions differently, and one section (Theory's Exam/Assessment) folds a
# second question-`section` (Post Assessment) into itself — exactly as the
# workbook's `=AVERAGE(J113:M113)` does. Rather than hard-code cell ranges, we
# describe each report section declaratively:
#
#   key           : short id used internally
#   title         : the heading shown on the report (matches the workbook)
#   method        : how the section score is computed —
#                     "syllabus"  -> mean(fractions) * 10               (Q1)
#                     "agree_avg" -> mean of its questions' weighted averages
#                     "single"    -> the one question's weighted average
#   sections      : which question.section label(s) feed this report section
#                   (a report section can draw from more than one question
#                    section — that is how Theory's Exam/Assessment absorbs
#                    Post Assessment).
#
# The ORDER of this list is the order the sections appear on the report AND the
# order they are averaged for the Overall Score.
# ----------------------------------------------------------------------------

REPORT_STRUCTURE = {
    # ---- Theory (Report V2.0 - T) : Overall = avg(Syllabus, Faculty, Course
    #      Material, Exam/Assessment[+Post Assessment]) -----------------------
    "T": [
        {"key": "syllabus", "title": "Overall Syllabus Coverage for CA1",
         "method": "syllabus", "sections": ["Syllabus"]},
        {"key": "faculty",  "title": "Faculty Teaching",
         "method": "agree_avg", "sections": ["Faculty Teaching"]},
        {"key": "material", "title": "Course Material",
         "method": "agree_avg", "sections": ["Course Material"]},
        # NOTE the two question-sections feeding one report section: this mirrors
        # the workbook's =AVERAGE(J113:M113), where M113 is the Post Assessment
        # question sitting inside the Exam/Assessment block.
        {"key": "exam",     "title": "Exam Assessment",
         "method": "agree_avg", "sections": ["Exam / Assessment", "Post Assessment"]},
    ],

    # ---- Lab (Report V2.0 - L) : Overall = avg(Syllabus, Faculty, Resources,
    #      Exam/Assessment) ---------------------------------------------------
    "L": [
        {"key": "syllabus", "title": "Overall Syllabus Coverage for CA1",
         "method": "syllabus", "sections": ["Syllabus"]},
        {"key": "faculty",  "title": "Faculty Teaching",
         "method": "agree_avg", "sections": ["Faculty Teaching"]},
        {"key": "resources", "title": "Resources",
         "method": "agree_avg", "sections": ["Resources"]},
        {"key": "exam",     "title": "Exam / Assessment",
         "method": "agree_avg", "sections": ["Exam / Assessment"]},
    ],

    # ---- Skill Development (Report V2.0 - SL) : Overall = avg(Syllabus,
    #      Faculty, Course Material, Assessment/Resources) --------------------
    "SL": [
        {"key": "syllabus", "title": "Overall Syllabus Coverage till CA1",
         "method": "syllabus", "sections": ["Syllabus"]},
        {"key": "faculty",  "title": "Faculty Teaching",
         "method": "agree_avg", "sections": ["Faculty Teaching"]},
        {"key": "material", "title": "Course Material",
         "method": "agree_avg", "sections": ["Course Material"]},
        {"key": "assessment", "title": "Assessment / Resources",
         "method": "agree_avg", "sections": ["Assessment / Resources"]},
    ],

    # ---- Ability Enhancement (Report V2.0 - AE) : Overall = avg(Training,
    #      Material, Knowledge Level, Overall). Each AE section uses its OWN
    #      scale, so "agree_avg" (mean of the section's question averages) works
    #      for Training (4 questions) and reduces to the single-question average
    #      for the one-question sections. ------------------------------------
    "AE": [
        {"key": "training",  "title": "Training",
         "method": "agree_avg", "sections": ["Training"]},
        {"key": "material",  "title": "Course Material",
         "method": "single", "sections": ["Material"]},
        {"key": "knowledge", "title": "Knowledge Level",
         "method": "single", "sections": ["Knowledge Level"]},
        {"key": "overall",   "title": "Overall",
         "method": "single", "sections": ["Overall"]},
    ],
}


# ============================================================================
# SECTION 2 — SMALL MATH HELPERS
# ============================================================================

def _mean(values):
    """Arithmetic mean of a list, IGNORING None entries.

    This mirrors Excel's AVERAGE(), which silently skips blank/empty cells. In
    our world a None question-average means "no one answered that question", and
    just like the workbook it must not drag the section score down to zero — it
    is simply left out of the average. Returns None if EVERY value is None (an
    entirely unanswered section), so callers can render a clean blank.
    """
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    return sum(nums) / len(nums)


def _round2(v):
    """Round to 2 decimals for display, passing None through untouched.

    The stored/verified value keeps full float precision; this is only for the
    on-report presentation, matching how the workbook cells display ~2 dp.
    """
    return None if v is None else round(v, 2)


# ============================================================================
# SECTION 3 — THE PURE SCORING FUNCTION (the verification target)
# ============================================================================
# compute_scores(report_key, questions, responses, discussed_late_weight)
#
# Inputs (all plain Python — no DB, no Flask):
#   report_key : 'T' | 'L' | 'SL' | 'AE' — selects the REPORT_STRUCTURE above.
#   questions  : ordered list of dicts, one per question, each:
#                  { "id": int,
#                    "section": str,          # e.g. 'Faculty Teaching'
#                    "text": str,             # verbatim question text
#                    "scale_code": str,       # e.g. 'AGREE5','SYLLABUS','OPEN'
#                    "is_free_text": bool,
#                    "display_order": int,    # 1-based order on the form
#                    "options": [ {"label","weight","fraction","display_order"} ] }
#   responses  : list of dicts, one per submitted form, mapping
#                  question_id -> chosen value string (or the open comment).
#                An absent key OR a blank/empty string means "not answered".
#   discussed_late_weight : the CONFIGURABLE weight for the Theory Post-
#                Assessment option "Discussed Late" (spec Open Item 14.2).
#                None (default) reproduces the approved workbook, where
#                "Discussed Late" contributes 0 to the numerator.
#
# Output: a dict with everything the report needs (per-question stats, per-
# section scores, the overall score, the recorded-student count, and the raw
# option-count tables). See the assembled `result` at the bottom for the shape.
# ----------------------------------------------------------------------------

def compute_scores(report_key, questions, responses, discussed_late_weight=None):
    # ----- 3a. Index questions and pick the CANONICAL respondent question -----
    # The workbooks measure "No. of Students Feedback Recorded" as
    #   (total data rows) - COUNTBLANK(column C)
    # i.e. the number of people who answered the SECOND question on the form
    # (spreadsheet column C = the 2nd data field, after Timestamp+Q1). In every
    # one of the four templates that second question is a substantive rated one
    # (Theory/Lab/Skill: the first Faculty-Teaching row; AE: "Teaching
    # Capacity"). We reproduce that exactly by treating the question at
    # display_order == 2 as the canonical "did this student respond" indicator.
    by_order = {q["display_order"]: q for q in questions}
    canonical_q = by_order.get(2)  # the 2nd question; None only if a tiny custom form

    def answered_count(question_id):
        """How many responses gave a NON-BLANK value to this question.

        This is the denominator the workbook builds with
        `total_rows - COUNTBLANK(col)` for every ordinary question average.
        A value counts as answered when the key exists and is not an empty /
        whitespace-only string.
        """
        n = 0
        for r in responses:
            v = r.get(question_id)
            if v is not None and str(v).strip() != "":
                n += 1
        return n

    # The canonical recorded-student count used for the header AND as the special
    # denominator for the Theory Post-Assessment question (see below).
    n_recorded = answered_count(canonical_q["id"]) if canonical_q else len(responses)

    # ----- 3b. Per-question statistics ---------------------------------------
    # For every non-free-text question we compute:
    #   * option_counts : label -> how many responses chose it (the report's
    #                     section tables show these raw counts).
    #   * average       : the /10 weighted average, per the approved formula for
    #                     that question's scale.
    # Free-text questions produce no average; their values are collected as the
    # verbatim open comments instead.
    per_question = {}          # question_id -> {counts, average, ...}
    open_comments = []         # verbatim non-empty free-text answers

    for q in questions:
        qid = q["id"]

        # --- Free-text (the open comment) : collect verbatim, no scoring. -----
        if q["is_free_text"] or q["scale_code"] == "OPEN":
            for r in responses:
                v = r.get(qid)
                if v is not None and str(v).strip() != "":
                    open_comments.append(str(v).strip())
            per_question[qid] = {"question": q, "is_free_text": True,
                                 "option_counts": {}, "average": None,
                                 "answered": 0}
            continue

        # --- Tally how many responses chose each option label. ----------------
        # We seed the dict with every option at 0 so the report shows a full
        # table (including options nobody picked), matching the workbook layout.
        option_counts = {opt["label"]: 0 for opt in q["options"]}
        # For the syllabus question, values may arrive as the option LABEL
        # ("80%", how the live student form stores them) OR as a raw NUMBER
        # (0.8 / 80, how the source workbooks store them). Pre-build a
        # fraction -> label map so either form tallies into the right column.
        frac_to_label = None
        if q["scale_code"] == "SYLLABUS":
            frac_to_label = {opt["fraction"]: opt["label"]
                             for opt in q["options"] if opt["fraction"] is not None}

        for r in responses:
            v = r.get(qid)
            if v is None:
                continue
            v = str(v).strip()
            if v in option_counts:
                option_counts[v] += 1
            elif frac_to_label is not None:
                # Try the numeric form: coerce to a 0-1 fraction and map to label.
                num = _coerce_fraction(v)
                if num is not None and num in frac_to_label:
                    option_counts[frac_to_label[num]] += 1
            # A non-blank value that matches no known option is intentionally not
            # counted in the numerator (no matching weight), exactly like a
            # COUNTIF that finds no match — but it is still "answered" (non-blank)
            # for the denominator, faithfully reproducing the sheet's behaviour.

        n_ans = answered_count(qid)

        # --- Compute the weighted /10 average for this question, by scale. ----
        average = _compute_question_average(
            q, option_counts, n_ans, n_recorded, discussed_late_weight)

        per_question[qid] = {
            "question": q,
            "is_free_text": False,
            "option_counts": option_counts,
            "answered": n_ans,
            "average": average,
        }

    # ----- 3c. Roll questions up into the report's SECTIONS ------------------
    # Using the declarative REPORT_STRUCTURE, each report section pulls the
    # relevant questions and combines them per its `method`. We keep both the
    # section score AND the list of contributing question-averages, so the report
    # can show the section breakdown if desired.
    structure = REPORT_STRUCTURE[report_key]
    section_scores = []       # ordered list of {key,title,score,questions:[...]}

    for sec in structure:
        # Gather the questions whose question.section is in this report section.
        member_qs = [q for q in questions if q["section"] in sec["sections"]]

        if sec["method"] == "syllabus":
            # Syllabus score = mean(stored fractions of non-blank answers) x 10.
            # (spec 10.1 ; workbook =AVERAGE(B2:B106)*10). Only ONE syllabus
            # question exists, but we loop for generality.
            score = _syllabus_section_score(member_qs, responses)

        else:
            # agree_avg / single : the section score is the mean of its member
            # questions' /10 averages (AVERAGE ignores unanswered ones). For a
            # single-question section this is just that question's average.
            q_avgs = [per_question[q["id"]]["average"] for q in member_qs]
            score = _mean(q_avgs)

        section_scores.append({
            "key": sec["key"],
            "title": sec["title"],
            "method": sec["method"],
            "score": score,
            "score_2dp": _round2(score),
            "question_ids": [q["id"] for q in member_qs],
        })

    # ----- 3d. The Overall Score = mean of the section scores ----------------
    # (spec 10.1/10.2 ; workbook =AVERAGE(C115:C118) for T, =AVERAGE(C240:C243)
    # for AE, etc.) A single, consistent rule across all four categories.
    overall = _mean([s["score"] for s in section_scores])

    # ----- 3e. Assemble the result the report layer will render --------------
    return {
        "report_key": report_key,
        "n_recorded": n_recorded,            # "No. of Students Feedback Recorded"
        "n_responses": len(responses),       # total submitted forms (for info)
        "overall": overall,                  # full-precision overall score
        "overall_2dp": _round2(overall),     # display value
        "section_scores": section_scores,    # ordered sections + scores
        "per_question": per_question,        # per-question counts + averages
        "open_comments": open_comments,      # verbatim comments
        "discussed_late_weight": discussed_late_weight,  # echo the config used
    }


# ----------------------------------------------------------------------------
# _compute_question_average(question, option_counts, n_answered, n_recorded,
#                           discussed_late_weight)
# ----------------------------------------------------------------------------
# The heart of the per-question math. Which formula applies depends on the
# question's scale_code:
#
#   AGREE5 / AE_TRAINING / AE_MATERIAL / AE_KNOWLEDGE / AE_OVERALL:
#       average = (Σ option_count[label] * weight[label]) / n_answered
#     — the standard weighted-count average. Because each option's weight lives
#       in scale_option (seed_data.py), a later weight correction re-scores
#       cleanly with no data migration.
#
#   POST_ASSESS (Theory Post Assessment):
#       average = (DC*10 + PD*6 + ND*1 [+ DL*late_weight]) / n_recorded
#     — TWO approved quirks reproduced here: (a) the denominator is the CANONICAL
#       recorded-student count (workbook divides by the column-C count, not M's
#       own), and (b) "Discussed Late" contributes 0 unless the professor has
#       configured a weight (spec Open Item 14.2).
#
#   SYLLABUS is handled separately (see _syllabus_section_score) and returns
#   None here, because it is scored at the SECTION level as mean(fractions)*10,
#   not as a per-question weighted average.
# ----------------------------------------------------------------------------
def _compute_question_average(question, option_counts, n_answered, n_recorded,
                              discussed_late_weight):
    scale = question["scale_code"]

    # Syllabus is scored at section level, not here.
    if scale == "SYLLABUS":
        return None

    # ---- Theory Post Assessment : the special denominator + Discussed Late ---
    if scale == "POST_ASSESS":
        # Guard against divide-by-zero when nobody has responded yet.
        if n_recorded == 0:
            return None
        numerator = 0.0
        for opt in question["options"]:
            label = opt["label"]
            count = option_counts.get(label, 0)
            if label == "Discussed Late":
                # The configurable open item. Default None -> weight 0 (approved).
                w = discussed_late_weight if discussed_late_weight is not None else 0.0
            else:
                # Discussed Completely=10, Partially Discussed=6, Not Discussed=1
                # (a NULL weight in the DB would mean "unset"; treat as 0 safely).
                w = opt["weight"] if opt["weight"] is not None else 0.0
            numerator += count * w
        # DIVIDE BY THE CANONICAL RESPONDENT COUNT (the approved quirk).
        return numerator / n_recorded

    # ---- All ordinary weighted scales (agree + the four AE scales) -----------
    if n_answered == 0:
        return None
    numerator = 0.0
    for opt in question["options"]:
        w = opt["weight"]
        if w is None:
            continue  # an option with no weight contributes nothing
        numerator += option_counts.get(opt["label"], 0) * w
    return numerator / n_answered


# ----------------------------------------------------------------------------
# _syllabus_section_score(member_questions, responses)
# ----------------------------------------------------------------------------
# Syllabus coverage (Q1) is stored per-answer as a FRACTION (100%->1.0, 80%->0.8,
# 60%->0.6). The approved section score is the mean of those fractions across all
# non-blank answers, times 10 (spec 10.1 ; workbook =AVERAGE(B2:B106)*10).
#
# We translate each stored answer value back to its fraction via the question's
# own scale_option table (so if the labels ever change, the mapping still holds).
# Answers may already be numeric (as the raw workbook stores them) OR the option
# label like "80%" (as our student form stores them) — we handle both.
# ----------------------------------------------------------------------------
def _syllabus_section_score(member_questions, responses):
    fractions = []
    for q in member_questions:
        # Build a label -> fraction lookup for this syllabus question.
        frac_by_label = {opt["label"]: opt["fraction"] for opt in q["options"]}
        for r in responses:
            v = r.get(q["id"])
            if v is None or str(v).strip() == "":
                continue
            v = str(v).strip()
            if v in frac_by_label and frac_by_label[v] is not None:
                # Stored as the option label, e.g. "80%".
                fractions.append(frac_by_label[v])
            else:
                # Stored as a raw number (the workbook path): "1", "0.8", "80%".
                num = _coerce_fraction(v)
                if num is not None:
                    fractions.append(num)
    if not fractions:
        return None
    return (sum(fractions) / len(fractions)) * 10.0


def _coerce_fraction(v):
    """Best-effort convert a raw syllabus cell to a 0-1 fraction.

    Accepts "1", "0.8", "80%", "100 %" etc. A value > 1 is read as a percent
    and divided by 100 (so "80" -> 0.8). Returns None if it cannot be parsed.
    """
    s = str(v).strip().replace("%", "").strip()
    try:
        num = float(s)
    except ValueError:
        return None
    if num > 1.0:
        num = num / 100.0
    return num


# ============================================================================
# SECTION 4 — DB ADAPTERS (bridge the pure math to the two-file database)
# ============================================================================
# These pull the data the pure function needs, honouring the anonymity split:
#   * questions + options + offering identity come from master.db (Group C/A),
#   * anonymous responses/answers come from the per-cycle db (Group B).
# We never touch the token table here — a report is about a COURSE.
# ----------------------------------------------------------------------------

def load_offering_dataset(master, cycle, offering_id):
    """Assemble (offering_row, report_key, questions, responses) for one course.

    `master` : an open master.db connection  (offering + config).
    `cycle`  : an open per-cycle db connection (response + answer).
    Returns None if the offering does not exist. If the offering has no category
    yet (uncategorised), report_key is None and the caller should skip it.
    """
    # --- The offering identity (header block) + its category/report key. ------
    offering = master.execute(
        """
        SELECT o.*, c.code AS category_code, c.name AS category_name,
               c.report_key AS report_key
        FROM offering o
        LEFT JOIN category c ON c.id = o.category_id
        WHERE o.id = ?
        """,
        (offering_id,),
    ).fetchone()
    if offering is None:
        return None
    report_key = offering["report_key"]

    # --- The questions of the CURRENT template version for this category. -----
    # A response is stamped with the exact template_version it answered; for a
    # clean single-version cycle they all share one version. We load questions
    # per the versions actually present in the responses so the report always
    # matches the questions the students really saw.
    questions_by_version = {}

    def questions_for_version(tv_id):
        if tv_id in questions_by_version:
            return questions_by_version[tv_id]
        qrows = master.execute(
            """
            SELECT q.id, q.section, q.text, q.display_order,
                   s.code AS scale_code, s.is_free_text
            FROM question q
            JOIN scale s ON s.id = q.scale_id
            WHERE q.template_version_id = ?
            ORDER BY q.display_order
            """,
            (tv_id,),
        ).fetchall()
        qs = []
        for q in qrows:
            options = []
            if not q["is_free_text"]:
                options = [dict(o) for o in master.execute(
                    "SELECT so.label AS label, so.weight AS weight, "
                    "so.fraction AS fraction, so.display_order AS display_order "
                    "FROM scale_option so JOIN question qq ON qq.scale_id = so.scale_id "
                    "WHERE qq.id = ? ORDER BY so.display_order", (q["id"],)).fetchall()]
            qs.append({
                "id": q["id"], "section": q["section"], "text": q["text"],
                "display_order": q["display_order"], "scale_code": q["scale_code"],
                "is_free_text": bool(q["is_free_text"]), "options": options,
            })
        questions_by_version[tv_id] = qs
        return qs

    # --- The anonymous responses for this offering, with their answers. -------
    resp_rows = cycle.execute(
        "SELECT id, template_version_id FROM response WHERE offering_id = ?",
        (offering_id,),
    ).fetchall()

    responses = []
    template_version_id = None
    for rr in resp_rows:
        template_version_id = rr["template_version_id"]
        ans = cycle.execute(
            "SELECT question_id, value FROM answer WHERE response_id = ?",
            (rr["id"],),
        ).fetchall()
        responses.append({a["question_id"]: a["value"] for a in ans})

    # Pick the question set: the version the responses used, else the offering's
    # current template version (so an empty offering still shows its questions).
    if template_version_id is not None:
        questions = questions_for_version(template_version_id)
    elif offering["category_id"] is not None:
        import services
        tv = services.current_template_version_id(master, offering["category_id"])
        questions = questions_for_version(tv) if tv else []
    else:
        questions = []

    return offering, report_key, questions, responses


def score_offering(master, cycle, offering_id, discussed_late_weight=None):
    """Full pipeline for one offering: load data, run the pure scorer, and
    attach the offering identity so the report layer has everything in one bag.

    Returns None for a missing or uncategorised offering (nothing to score).
    """
    loaded = load_offering_dataset(master, cycle, offering_id)
    if loaded is None:
        return None
    offering, report_key, questions, responses = loaded
    if report_key is None or not questions:
        return None  # uncategorised or template-less: cannot score meaningfully

    result = compute_scores(report_key, questions, responses, discussed_late_weight)
    result["offering"] = offering
    result["questions"] = questions
    return result


# ============================================================================
# SECTION 4b — CONSOLIDATED (ELECTIVE) SCORING : pool many offerings into ONE
# ============================================================================
# score_offering_group(master, cycle, offering_ids, discussed_late_weight)
# ----------------------------------------------------------------------------
# WHY THIS EXISTS (Aug 2026 — the elective-consolidation change): a shared
# elective is stored as several `offering` rows (one per programme code) that are
# really ONE class. `score_offering` above scores a single offering over only that
# programme's students, which is exactly what produced three thin reports for one
# elective. This function scores a whole DELIVERY — the set of offering_ids that
# consolidation.py grouped together — as one report.
#
# HOW IT STAYS MATHEMATICALLY CORRECT: it does NOT average the three programmes'
# scores together (an average-of-averages is wrong when the programmes have
# different head-counts). Instead it POOLS the raw responses — concatenating every
# member offering's list of submitted forms — and calls the SAME frozen pure
# `compute_scores` once over the combined list. Every denominator (the "students
# recorded" count, each question's answered-count, the syllabus fraction mean) is
# then computed over the whole class exactly as if those students had always been
# one offering. Because compute_scores is untouched, verify_scoring.py still holds
# and a single-offering group scores identically to score_offering().
#
# WHY POOLING IS SAFE ACROSS THE MEMBERS: all members of an elective delivery are
# the same course in the same category, so they share one template_version, and a
# question's `id` is stable across offerings of that category (questions belong to
# the template version, not the offering). So the response dicts from different
# members use the SAME question-id keys and pool cleanly.
#
# RETURNS the usual `result` dict (same shape score_offering returns) PLUS three
# consolidation fields the report layer reads to render a pooled header:
#   result["group_offering_ids"] : the member ids that were pooled (sorted)
#   result["is_consolidated"]    : True when more than one offering was pooled
#   result["group_dept_codes"]   : every programme code in the delivery, in order
# `result["offering"]` is set to the ANCHOR (smallest-id) member's identity row, so
# the report filename/footer are stable, while the header also shows all programmes.
# Returns None if none of the ids yield a scoreable dataset.
# ----------------------------------------------------------------------------
def score_offering_group(master, cycle, offering_ids, discussed_late_weight=None):
    # Normalise to a sorted, de-duplicated id list; the smallest is the anchor.
    ids = sorted(set(int(o) for o in offering_ids))
    if not ids:
        return None

    pooled_responses = []          # every member's submitted forms, concatenated
    report_key = None              # the shared category's report layout key
    questions = None               # the shared question set (same template version)
    anchor_offering = None         # identity row of the smallest-id member
    dept_codes = []                # programme codes, in ascending-id member order

    for oid in ids:
        loaded = load_offering_dataset(master, cycle, oid)
        if loaded is None:
            continue               # missing offering — skip it, pool the rest
        offering, rk, qs, responses = loaded

        # Record each member's programme code for the consolidated header, keeping
        # the ascending-id order and dropping blanks/duplicates.
        dc = (offering["dept_code"] or "").strip() if "dept_code" in offering.keys() else ""
        if dc and dc not in dept_codes:
            dept_codes.append(dc)

        # The first member that actually has a category + questions fixes the
        # report layout and question set for the whole pool. Because ids is sorted
        # ascending, that first usable member IS the anchor's dataset, so the
        # report identity is the anchor's.
        if rk is not None and qs:
            if report_key is None:
                report_key, questions = rk, qs
                anchor_offering = offering
            pooled_responses.extend(responses)

    # Nothing scoreable across the whole delivery (all uncategorised/template-less).
    if report_key is None or not questions:
        return None

    # ONE call to the frozen engine over the POOLED responses — the whole point.
    result = compute_scores(report_key, questions, pooled_responses,
                            discussed_late_weight)
    result["offering"] = anchor_offering       # stable anchor identity
    result["questions"] = questions
    # Consolidation metadata the report/view layer uses to render the pooled header.
    result["group_offering_ids"] = ids
    result["is_consolidated"] = len(ids) > 1
    result["group_dept_codes"] = dept_codes
    return result


# ----------------------------------------------------------------------------
# get_discussed_late_weight(master)  —  read the configurable open item
# ----------------------------------------------------------------------------
# The "Discussed Late" weight is stored, when the professor sets it, on the
# POST_ASSESS scale option itself (scale_option.weight for label 'Discussed
# Late'). Until then it is NULL, meaning "use the approved default of 0". This
# helper surfaces whatever is currently configured so the scorer and the admin
# UI agree on one source of truth.
# ----------------------------------------------------------------------------
def get_discussed_late_weight(master):
    row = master.execute(
        """
        SELECT so.weight AS w
        FROM scale_option so JOIN scale s ON s.id = so.scale_id
        WHERE s.code = 'POST_ASSESS' AND so.label = 'Discussed Late'
        """
    ).fetchone()
    return row["w"] if row and row["w"] is not None else None
