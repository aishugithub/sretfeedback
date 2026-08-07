# ============================================================================
# seed_data.py  —  Load the four categories, their scales/weights, and the four
#                  VERBATIM question sets into master.db (spec Sections 9 & 10)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# This module populates Group C (configuration) of master.db. It is run once by
# init_db.py after the schema is created. Everything here is transcribed
# EXACTLY from the approved Google-Form PDFs (question text + option labels) and
# the approved "Feedback Report V2.0" workbooks (the scoring weights). The two
# non-negotiables from the spec are honoured:
#   * VERBATIM questions  — no rewording (Core principle, Section 2)
#   * VERBATIM formulas   — the exact 10/8/6/4/1 etc. weights (Section 10)
#
# Data model recap (see schema_master.sql):
#   category -> template -> template_version(v1) -> question(s) -> scale
#   scale    -> scale_option(s)  (label + weight/fraction + display_order)
#
# The whole seed is IDEMPOTENT: if a category already exists we skip it, so
# re-running init_db.py will not create duplicates.
# ============================================================================

# ----------------------------------------------------------------------------
# SECTION 1 — THE ANSWER SCALES AND THEIR WEIGHTS (spec Section 10)
# ----------------------------------------------------------------------------
# Each scale is a dict:
#   code         : stable key used by questions to reference this scale
#   name         : description shown in the admin UI
#   is_free_text : True only for the open-comment "scale" (it has no options)
#   options      : list of (label, weight, fraction, display_order)
#                    - weight   = the /10 scoring weight (spec 10). None if N/A.
#                    - fraction = only for the syllabus scale (1.0/0.8/0.6).
#                    - display_order = left-to-right position AS PRINTED on the
#                                      form (which, for the agree scale, is NOT
#                                      the same as weight order — see below).
#
# CRITICAL APPROVED QUIRK (spec 10.1): on the agree scale, "Moderately Agree"
# carries weight 8 while plain "Agree" carries weight 6 — so Moderately Agree
# OUTRANKS Agree. The form prints them in the order SA, A, MA, D, SD, which is
# why display_order (2 for Agree, 3 for Moderately Agree) differs from the
# weight ranking. Storing both columns reproduces layout AND scoring faithfully.
# ----------------------------------------------------------------------------

SCALES = [
    {
        # The 5-point agree scale shared by Theory, Lab and Skill matrix rows.
        "code": "AGREE5",
        "name": "5-point Agree scale (Strongly Agree … Strongly Disagree)",
        "is_free_text": False,
        "options": [
            # (label,               weight, fraction, display_order)
            ("Strongly Agree",       10.0,   None,     1),
            ("Agree",                 6.0,   None,     2),  # printed 2nd, weight 6
            ("Moderately Agree",      8.0,   None,     3),  # printed 3rd, weight 8 (> Agree)
            ("Disagree",              4.0,   None,     4),
            ("Strongly Disagree",     1.0,   None,     5),
        ],
    },
    {
        # Q1 syllabus-coverage scale. Scored as a FRACTION, not a weight:
        # section score = average(fractions) x 10  (spec 10.1).
        "code": "SYLLABUS",
        "name": "Syllabus coverage (100% / 80% / 60%)",
        "is_free_text": False,
        "options": [
            ("100%", None, 1.0, 1),
            ("80%",  None, 0.8, 2),
            ("60%",  None, 0.6, 3),
        ],
    },
    {
        # Theory-only "Post Assessment" scale (spec 10.1). Discussed Completely
        # = 10, Partially Discussed = 6, Not Discussed = 1. "Discussed Late" is
        # an OPEN ITEM (spec Section 14.2): the approved formula currently
        # ignores it (contributes 0 but still counts in the denominator), so we
        # store its weight as NULL and let the professor set it later.
        "code": "POST_ASSESS",
        "name": "Post Assessment (answer-key discussion)",
        "is_free_text": False,
        "options": [
            ("Discussed Completely", 10.0, None, 1),
            ("Discussed Late",       None, None, 2),  # OPEN ITEM — weight to be confirmed
            ("Partially Discussed",   6.0, None, 3),
            ("Not Discussed",         1.0, None, 4),
        ],
    },
    {
        # AE "Training" scale (spec 10.2): 10 / 8 / 6 / 4 / 2.
        "code": "AE_TRAINING",
        "name": "AE Training (Excellent … Not Satisfied)",
        "is_free_text": False,
        "options": [
            ("Excellent",     10.0, None, 1),
            ("Very Good",      8.0, None, 2),
            ("Good",           6.0, None, 3),
            ("Satisfactory",   4.0, None, 4),
            ("Not Satisfied",  2.0, None, 5),
        ],
    },
    {
        # AE "Material" scale (spec 10.2): 10 / 6 / 3.
        "code": "AE_MATERIAL",
        "name": "AE Material (Provided & Useful … Not Provided)",
        "is_free_text": False,
        "options": [
            ("Provided & Useful",        10.0, None, 1),
            ("Provided But not Useful",   6.0, None, 2),
            ("Not Provided",              3.0, None, 3),
        ],
    },
    {
        # AE "Knowledge Level" scale (spec 10.2): 10 / 7 / 4.
        "code": "AE_KNOWLEDGE",
        "name": "AE Knowledge Level (Advanced / Intermediate / Basic)",
        "is_free_text": False,
        "options": [
            ("Advanced",     10.0, None, 1),
            ("Intermediate",  7.0, None, 2),
            ("Basic",         4.0, None, 3),
        ],
    },
    {
        # AE "Overall" scale (spec 10.2): 10 / 7 / 4 / 1.
        "code": "AE_OVERALL",
        "name": "AE Overall (Excellent / Good / Satisfactory / Not Satisfactory)",
        "is_free_text": False,
        "options": [
            ("Excellent",        10.0, None, 1),
            ("Good",              7.0, None, 2),
            ("Satisfactory",      4.0, None, 3),
            ("Not Satisfactory",  1.0, None, 4),
        ],
    },
    {
        # The open-ended comment. A "scale" with no options; is_free_text flags
        # the student form to render a textarea and the scorer to skip it.
        "code": "OPEN",
        "name": "Open-ended comment (free text)",
        "is_free_text": True,
        "options": [],
    },
]


# ----------------------------------------------------------------------------
# SECTION 2 — THE FOUR CATEGORIES (spec Section 3)
# ----------------------------------------------------------------------------
CATEGORIES = [
    {"code": "THEORY", "name": "Theory",            "form_title": "Intermediate Feedback for Theory Courses", "report_key": "T"},
    {"code": "LAB",    "name": "Lab",               "form_title": "Intermediate Feedback for Lab Courses",    "report_key": "L"},
    {"code": "SKILL",  "name": "Skill Development",  "form_title": "Intermediate Feedback for Skill Courses",  "report_key": "SL"},
    {"code": "AE",     "name": "Ability Enhancement","form_title": "Intermediate Feedback for PCP,PCD_AE",     "report_key": "AE"},
]


# ----------------------------------------------------------------------------
# SECTION 3 — THE FOUR QUESTION SETS, VERBATIM (spec Section 9)
# ----------------------------------------------------------------------------
# Each template is a dict:
#   category_code : which category it belongs to
#   name          : the template's display name
#   questions     : ordered list of (section, text, scale_code)
# A "matrix" question with N rows on the form becomes N question entries that
# share the same `section`, because each row is averaged independently (spec 10).
# Text is transcribed exactly from the form PDFs (the doubled-letter rendering
# in the raw PDF is a display artefact only; the underlying wording is below).
# ----------------------------------------------------------------------------

TEMPLATES = [
    # ======================= THEORY (CT) — spec 9.1 =======================
    {
        "category_code": "THEORY",
        "name": "Theory Feedback (Intermediate)",
        "questions": [
            # Q1 Syllabus (single oval: 100/80/60)
            ("Syllabus", "The instructor covered all the syllabus portions for CA1", "SYLLABUS"),
            # Q2 Faculty Teaching (5 agree-scale rows)
            ("Faculty Teaching", "The course instructor was well organized and prepared", "AGREE5"),
            ("Faculty Teaching", "The course instructor communicated clearly, used class time efficiently and taught well", "AGREE5"),
            ("Faculty Teaching", "The instructor encouraged discussions and responded to questions", "AGREE5"),
            ("Faculty Teaching", "The instructor showed an interest in helping students learn and approachable for any help", "AGREE5"),
            ("Faculty Teaching", "The instructor is punctual to the class", "AGREE5"),
            # Q3 Course Material (2 agree-scale rows)
            ("Course Material", "The course was organized in a manner that helped me to understand the underlying concepts", "AGREE5"),
            ("Course Material", "Complete course materials are available in Google Classroom", "AGREE5"),
            # Q4 Exam / Assessment (3 agree-scale rows)
            ("Exam / Assessment", "Exams were based on material covered in assignments and lectures", "AGREE5"),
            ("Exam / Assessment", "The assessment is challenging & helped to put the concepts into practice", "AGREE5"),
            ("Exam / Assessment", "The assessment questions improved my problem solving skills & applying the concepts lead to critical thinking skills", "AGREE5"),
            # Q5 Post Assessment (1 row, its own 4-option scale)
            ("Post Assessment", "The answer key of CA is discussed immediately after CA 1 - exam", "POST_ASSESS"),
            # Q6 Open comment
            ("Open", "Are any specific things about this course that could be improved to better support student learning?", "OPEN"),
        ],
    },

    # ========================= LAB (CL) — spec 9.2 =========================
    {
        "category_code": "LAB",
        "name": "Lab Feedback (Intermediate)",
        "questions": [
            ("Syllabus", "The Lab Experiments / activities covered all the syllabus portions for CA1", "SYLLABUS"),
            # Faculty Teaching (5 rows) — note the Lab-specific wording/order
            ("Faculty Teaching", "The instructor is punctual to the class", "AGREE5"),
            ("Faculty Teaching", "The instructor was well organized and prepared for the Lab Sessions", "AGREE5"),
            ("Faculty Teaching", "The Laboratory manual was useful in completing the lab experiments", "AGREE5"),
            ("Faculty Teaching", "The experiments practiced / activities during the laboratory / practice sessions was useful to understand the theory concepts", "AGREE5"),
            ("Faculty Teaching", "The laboratory experiments / activities are related with the real world applications", "AGREE5"),
            # Resources (2 rows)
            ("Resources", "Support and help, during lab and for lab reports, were sufficient to successfully complete and analyze experiments", "AGREE5"),
            ("Resources", "Lab resources / Materials (equipment, software, information, instructions, etc.) were sufficient to provide a positive experience", "AGREE5"),
            # Exam / Assessment (3 rows)
            ("Exam / Assessment", "Lab expectations (goals, tasks, reports, deadlines, etc.) were clear and realistic", "AGREE5"),
            ("Exam / Assessment", "The laboratory activities helps in enhancing your learning in this course (e.g., taught specific skills, provided experience with real equipment and data, provided hands-on experience, increased my understanding of the material)", "AGREE5"),
            ("Exam / Assessment", "The Instructor used fair and standard methods for evaluating lab reports", "AGREE5"),
            # Open comment
            ("Open", "Are any specific things about this course that could be improved to better support student learning?", "OPEN"),
        ],
    },

    # ==================== SKILL DEVELOPMENT (SL) — spec 9.3 ====================
    {
        "category_code": "SKILL",
        "name": "Skill Development Feedback (Intermediate)",
        "questions": [
            ("Syllabus", "The Lab Experiments / activities covered all the syllabus portions till CA1", "SYLLABUS"),
            # Faculty Teaching (5 rows) — SAME 5 as Theory (spec 9.3)
            ("Faculty Teaching", "The course instructor was well organized and prepared", "AGREE5"),
            ("Faculty Teaching", "The course instructor communicated clearly, used class time efficiently and taught well", "AGREE5"),
            ("Faculty Teaching", "The instructor encouraged discussions and responded to questions", "AGREE5"),
            ("Faculty Teaching", "The instructor showed an interest in helping students learn and approachable for any help", "AGREE5"),
            ("Faculty Teaching", "The instructor is punctual to the class", "AGREE5"),
            # Course Material (2 rows) — SAME 2 as Theory (spec 9.3)
            ("Course Material", "The course was organized in a manner that helped me to understand the underlying concepts", "AGREE5"),
            ("Course Material", "Complete course materials are available in Google Classroom", "AGREE5"),
            # Assessment / Resources (5 rows)
            ("Assessment / Resources", "Lab expectations (goals, tasks, reports, deadlines, etc.) were clear and realistic", "AGREE5"),
            ("Assessment / Resources", "The laboratory activities helps in enhancing your learning in this course (e.g., taught specific skills, provided experience with real equipment and data, provided hands-on experience, increased my understanding of the material)", "AGREE5"),
            ("Assessment / Resources", "The Instructor used fair and standard methods for evaluating lab reports", "AGREE5"),
            ("Assessment / Resources", "Support and help, during lab and for lab reports, were sufficient to successfully complete and analyze experiments", "AGREE5"),
            ("Assessment / Resources", "Lab resources / Materials (equipment, software, information, instructions, etc.) were sufficient to provide a positive experience", "AGREE5"),
            # Open comment
            ("Open", "Are any specific things about this course that could be improved to better support student learning?", "OPEN"),
        ],
    },

    # ================= ABILITY ENHANCEMENT — AE (PCP/PCD) — spec 9.4 =================
    {
        "category_code": "AE",
        "name": "Ability Enhancement (PCP / PCD) Feedback (Intermediate)",
        "questions": [
            # Training (4 rows, AE_TRAINING scale)
            ("Training", "Technical Knowledge", "AE_TRAINING"),
            ("Training", "Teaching Capacity", "AE_TRAINING"),
            ("Training", "Communication", "AE_TRAINING"),
            ("Training", "Audibility", "AE_TRAINING"),
            # Material (1 row, AE_MATERIAL scale)
            ("Material", "Practice Material", "AE_MATERIAL"),
            # Knowledge Level (1 row, AE_KNOWLEDGE scale)
            ("Knowledge Level", "Knowledge Level Acquired", "AE_KNOWLEDGE"),
            # Overall (1 row, AE_OVERALL scale)
            ("Overall", "Overall Performance of the Trainer", "AE_OVERALL"),
            # Open comment
            ("Open", "Are any specific things about this course that could be improved to better support student learning?", "OPEN"),
        ],
    },
]


# ----------------------------------------------------------------------------
# SECTION 4 — THE SEEDING FUNCTION
# ----------------------------------------------------------------------------
def seed(conn):
    """
    Insert all scales, categories, templates (with a frozen version 1) and
    their verbatim questions into the given master.db connection.

    `conn` is an open sqlite3 connection to master.db (provided by init_db.py).
    The function is IDEMPOTENT: it checks for existing rows by their unique
    `code` and skips anything already present, so it is safe to re-run.
    """
    cur = conn.cursor()

    # --- 4a. Scales + their options ---------------------------------------
    # scale_id_by_code lets the question loader below translate a scale `code`
    # (e.g. "AGREE5") into the numeric scale.id it must store on each question.
    scale_id_by_code = {}
    for sc in SCALES:
        # Has this scale already been seeded? (idempotency check)
        row = cur.execute("SELECT id FROM scale WHERE code = ?", (sc["code"],)).fetchone()
        if row:
            scale_id_by_code[sc["code"]] = row["id"]
            continue  # already present — leave it and its options untouched
        # Insert the scale header row.
        cur.execute(
            "INSERT INTO scale (code, name, is_free_text) VALUES (?, ?, ?)",
            (sc["code"], sc["name"], 1 if sc["is_free_text"] else 0),
        )
        sid = cur.lastrowid                 # the new scale.id
        scale_id_by_code[sc["code"]] = sid
        # Insert each option of this scale (label + weight + fraction + order).
        for (label, weight, fraction, order) in sc["options"]:
            cur.execute(
                "INSERT INTO scale_option (scale_id, label, weight, fraction, display_order) "
                "VALUES (?, ?, ?, ?, ?)",
                (sid, label, weight, fraction, order),
            )

    # --- 4b. Categories ----------------------------------------------------
    cat_id_by_code = {}
    for cat in CATEGORIES:
        row = cur.execute("SELECT id FROM category WHERE code = ?", (cat["code"],)).fetchone()
        if row:
            cat_id_by_code[cat["code"]] = row["id"]
            continue
        cur.execute(
            "INSERT INTO category (code, name, form_title, report_key) VALUES (?, ?, ?, ?)",
            (cat["code"], cat["name"], cat["form_title"], cat["report_key"]),
        )
        cat_id_by_code[cat["code"]] = cur.lastrowid

    # --- 4c. Templates + version 1 + questions -----------------------------
    for tpl in TEMPLATES:
        cat_id = cat_id_by_code[tpl["category_code"]]
        # Does a template already exist for this category? (idempotency)
        row = cur.execute(
            "SELECT id FROM template WHERE category_id = ? AND name = ?",
            (cat_id, tpl["name"]),
        ).fetchone()
        if row:
            continue  # this template (and its v1 + questions) is already seeded
        # Insert the template header.
        cur.execute(
            "INSERT INTO template (category_id, name) VALUES (?, ?)",
            (cat_id, tpl["name"]),
        )
        template_id = cur.lastrowid
        # Insert the FROZEN version 1 of this template (spec Section 5/7).
        cur.execute(
            "INSERT INTO template_version (template_id, version_no, is_locked) VALUES (?, 1, 0)",
            (template_id,),
        )
        tv_id = cur.lastrowid
        # Insert every question of this template, preserving display order.
        for order, (section, text, scale_code) in enumerate(tpl["questions"], start=1):
            cur.execute(
                "INSERT INTO question (template_version_id, section, text, scale_id, display_order) "
                "VALUES (?, ?, ?, ?, ?)",
                (tv_id, section, text, scale_id_by_code[scale_code], order),
            )

    # Persist everything in one transaction.
    conn.commit()
