# ============================================================================
# config.py  —  Central configuration for the Feedback System
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# Almost every other module needs to know two things:
#   1. WHERE the SQLite database files live on disk, and
#   2. reference data (department codes, category-by-code-segment map).
# Centralising them here gives a single source of truth. The Flask app factory
# loads this object; db.py reads the paths; the importer/seed scripts import the
# maps directly. Change a path once here and the whole system follows.
# ----------------------------------------------------------------------------

import os  # standard library: build filesystem paths that work on any OS


# BASE_DIR = the folder that contains THIS file (the `app/` directory). We
# anchor every other path to it so the project can be moved anywhere and still
# find its own files.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# DATA_DIR = where the SQLite database files live (a dedicated `data/` folder so
# backup = "copy the data folder", spec Section 13).
DATA_DIR = os.path.join(BASE_DIR, "data")

# ARCHIVE_DIR = where closed cycle databases are moved on reset (spec 13).
ARCHIVE_DIR = os.path.join(BASE_DIR, "archive")


class Config:
    """Single configuration object handed to Flask via app.config.from_object().

    Every attribute is a class-level constant; Flask copies them into
    app.config, and our modules read them from there or import this class.
    """

    # SECRET_KEY signs Flask session cookies + flash messages. Override via env
    # on a real deployment; the fallback keeps the app runnable out of the box.
    SECRET_KEY = os.environ.get("FEEDBACK_SECRET_KEY", "dev-only-change-me")

    # Project paths exposed on the config object (Config.BASE_DIR etc.), mirroring
    # the module-level constants above. The importer + init_db anchor the
    # workbook and log paths to Config.BASE_DIR.
    BASE_DIR = BASE_DIR
    DATA_DIR = DATA_DIR
    ARCHIVE_DIR = ARCHIVE_DIR

    # DATABASE PATHS (spec Section 5 — the two-file split).
    # master.db = permanent store; the per-cycle DB name is derived at run time
    # from the active cycle row (see db.cycle_db_path).
    MASTER_DB = os.path.join(DATA_DIR, "master.db")

    # DEPARTMENT-CODE REFERENCE MAP (spec Section 3). These E-codes appear in the
    # allocation headers + student master; kept here for validation + the report
    # legend.
    DEPT_CODES = {
        "E01": "CSE-AIML",
        "E02": "CSE-CyberSecurity&IoT",
        "E03": "CSE-AIDA",
        "E05": "CSE-Medical(AIDA)",
        "E06": "ECE",
        "E52": "B.Sc CS-AIDA",
        "E61": "B.Sc Bioinformatics",
        "E62": "B.Sc Data Science",
        "E71": "M.Sc AI",
        "E73": "M.Sc Data Analytics",
        "E81": "M.Sc Medical Bioinformatics",
    }

    # NOTE (v3.1): the old CATEGORY_BY_CODE_SEGMENT auto-detection map was REMOVED
    # along with the legacy allocation_importer.py "Re-import" path. The feedback
    # category is now an EXPLICIT, required column in the allocation upload (read
    # and validated by allocation_rigid.py against the configured `category` table),
    # so there is nothing left to guess from a course code.

    # PROGRAMME MASTER (spec v3 §3) — code -> (full name, level). Display and
    # validation ONLY. No duration column: v3 §2.1 deleted graduation arithmetic,
    # so nothing here is ever used to compute a year of study. Seeded into the
    # `programme` table by init_db; the roster/allocation importers validate every
    # uploaded programme_code against it.
    PROGRAMMES = {
        "E01": ("B.Tech CSE (Artificial Intelligence and Machine Learning)", "B.Tech"),
        "E02": ("B.Tech CSE (Cyber Security and Internet of Things)", "B.Tech"),
        "E03": ("B.Tech CSE (Artificial Intelligence and Data Analytics)", "B.Tech"),
        "E05": ("B.Tech Computer Science and Medical Engineering (AI and Data Analytics)", "B.Tech"),
        "E06": ("B.Tech Electronics and Communication Engineering", "B.Tech"),
        "E52": ("B.Sc Computer Science (Artificial Intelligence and Data Analytics)", "B.Sc"),
        "E61": ("B.Sc Bio Informatics", "B.Sc"),
        "E62": ("B.Sc Data Science", "B.Sc"),
        "E71": ("M.Sc Artificial Intelligence", "M.Sc"),
        "E73": ("M.Sc Data Analytics", "M.Sc"),
        "E81": ("M.Sc Medical Bioinformatics", "M.Sc"),
    }

    # EMAIL (spec §4, §10). Student email is a pure concatenation
    # <reg_no>@STUDENT_EMAIL_DOMAIN — the ONLY derivation the system performs.
    STUDENT_EMAIL_DOMAIN = "sriher.edu.in"
    # Faculty emails are validated to be on an institutional domain at upload (§7.2).
    FACULTY_EMAIL_DOMAINS = ("sret.edu.in", "sriher.edu.in")

    # TEST-MODE SAFETY (spec §9.1, extended to the three-level model in v2.1).
    # A cycle now runs at one of four graduated TEST LEVELS (cycle.test_level):
    #   0 PRODUCTION : everyone real, no watermark (the promoted, live run).
    #   1 (safest)   : students  -> TEST_REDIRECT_EMAIL (student inbox);
    #                  faculty/leaders -> FACULTY_ALIAS_EMAIL (staff inbox).
    #   2            : students  -> TEST_REDIRECT_EMAIL; faculty/leaders REAL.
    #   3            : everyone REAL, but reports still carry the watermark.
    # The mailer (emailer.py) enforces this routing in code, so one un-edited
    # spreadsheet cell can never leak a real send while testing. Both addresses are
    # env-configurable; the defaults are safe placeholders.
    #
    # TEST_REDIRECT_EMAIL = the STUDENT test inbox (Levels 1 & 2 catch students here).
    TEST_REDIRECT_EMAIL = os.environ.get("FEEDBACK_TEST_EMAIL", "feedback-test@sret.edu.in")
    # FACULTY_ALIAS_EMAIL = the TEACHER/LEADER test inbox (Level 1 catches staff here).
    FACULTY_ALIAS_EMAIL = os.environ.get("FEEDBACK_FACULTY_ALIAS_EMAIL", "feedback-staff-test@sret.edu.in")

    # ------------------------------------------------------------------------
    # TRUSTED TIMESTAMP (v2.1) — the end-of-cycle audit PDF is digitally signed
    # (signing.py); optionally we also embed an RFC-3161 TRUSTED TIMESTAMP from a
    # Timestamp Authority (TSA), which cryptographically attests the document
    # existed at a given time (independent of the server clock) and underpins
    # long-term validation. This is OPT-IN via env: set FEEDBACK_TSA_URL to a TSA
    # endpoint (e.g. FreeTSA "https://freetsa.org/tsr", or a commercial TSA). If it
    # is unset OR unreachable, the report is still produced — signed, just without a
    # timestamp — so audit generation never fails. NOTE: PythonAnywhere's FREE tier
    # only permits whitelisted outbound hosts, so a TSA call will fail there unless
    # the host is whitelisted (a paid tier has open outbound access).
    TSA_URL = os.environ.get("FEEDBACK_TSA_URL", "").strip()

    # ------------------------------------------------------------------------
    # EMAIL TRANSPORT (v1.5). The mailer (emailer.py) picks a transport by
    # precedence: gmail-api > smtp > dev-outbox. None of these secrets live in
    # code — they are read from environment variables at runtime. Documented
    # here as the single reference; emailer.py / gmail_api.py read them directly.
    #
    #   GMAIL API (preferred — sends as feedback@sret.edu.in, no password):
    #     FEEDBACK_GMAIL_CLIENT_ID       OAuth client ID (Desktop app)
    #     FEEDBACK_GMAIL_CLIENT_SECRET   OAuth client secret
    #     FEEDBACK_GMAIL_REFRESH_TOKEN   minted once by app/get_refresh_token.py
    #     FEEDBACK_GMAIL_FROM            sender address (default feedback@sret.edu.in)
    #
    #   SMTP (fallback — needs a login/app-password, which our domain disables):
    #     FEEDBACK_SMTP_HOST / _PORT / _USER / _PASS / _FROM
    #
    #   If neither group is set, the mailer runs in dev-outbox mode (.eml files).
    # The default sender for the Gmail path, surfaced on the config object so the
    # admin UI can display "emails will be sent from ..." without re-reading env.
    GMAIL_FROM = os.environ.get("FEEDBACK_GMAIL_FROM", "feedback@sret.edu.in")
