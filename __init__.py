# ============================================================================
# __init__.py  —  The Flask application factory
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# This turns the `app/` folder into a Python package AND defines create_app(),
# the "application factory" that builds and returns a configured Flask app.
# Using a factory (rather than a module-level `app = Flask(...)`) lets us create
# fresh app instances for tests and keeps configuration in one predictable place.
#
# WHAT IT WIRES TOGETHER (Night 2):
#   * loads Config (paths, dept map, category map),
#   * registers the `admin` blueprint (dashboard, roster, students, categories,
#     templates, scales, cycles, tokens, participation),
#   * registers the `student` blueprint (the anonymous /f/<token> feedback flow),
#   * adds a root route that redirects to the admin dashboard.
# ----------------------------------------------------------------------------

import os   # used by the .env loader below and throughout config.py


# ----------------------------------------------------------------------------
# _load_dotenv(path)  —  minimal, dependency-free ".env" loader (runs on import)
# ----------------------------------------------------------------------------
# WHY THIS LIVES HERE (and not only in run.py):
#   Every secret the app needs (FEEDBACK_GMAIL_CLIENT_ID / _SECRET /
#   _REFRESH_TOKEN / _FROM, FEEDBACK_SMTP_*, FEEDBACK_SECRET_KEY) is read from
#   ENVIRONMENT VARIABLES — see gmail_api.gmail_settings() and config.py. If they
#   are absent, emailer.send_batch() silently falls back to writing .eml files
#   into app/outbox/ (dev mode) and sends NO real email.
#
#   This module (`app/__init__.py`) is imported by EVERY way of starting the app:
#     * locally via run.py  (`from __init__ import create_app`), and
#     * on PythonAnywhere via the WSGI file (which also imports create_app and
#       never runs run.py).
#   So loading .env HERE, at import time, guarantees the secrets are present for
#   all entry points — this is the fix for "it still writes to the outbox on the
#   server." It runs BEFORE `from config import Config` below, so even the values
#   config.py reads at import time (SECRET_KEY, GMAIL_FROM, ...) see the file.
#
# RULES IT RESPECTS:
#   * No new dependency — pure standard library (matches requirements.txt).
#   * NEVER overrides a variable already set in the real environment, so on
#     PythonAnywhere you may EITHER ship an app/.env OR set the vars in the WSGI
#     file; whichever is set as a real env var always wins over the file.
#
# app/.env format: one KEY=VALUE per line; blank lines and '#' comments ignored;
# optional surrounding quotes around the value are stripped. It holds live
# credentials, so it is git-ignored — never commit it.
# ----------------------------------------------------------------------------
def _load_dotenv(path):
    if not os.path.exists(path):
        return   # no file -> rely on real env vars (or dev-outbox fallback)
    with open(path, "r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue                       # skip blanks and comments
            if "=" not in line:
                continue                       # ignore malformed lines
            key, value = line.split("=", 1)    # split on FIRST '=' only
            key = key.strip()
            value = value.strip()
            # Strip a single pair of wrapping quotes, if present.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            # Do not clobber a variable the real environment already provides.
            if key and key not in os.environ:
                os.environ[key] = value


# Load the .env that sits right next to this file, BEFORE config is imported.
_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


from flask import Flask, redirect, url_for
from config import Config


def create_app() -> Flask:
    # Create the Flask app. template_folder/static_folder default to the
    # templates/ and static/ folders next to this file.
    app = Flask(__name__)

    # Pull every UPPER_CASE attribute of Config into app.config (SECRET_KEY,
    # database paths, etc.). One line, single source of truth.
    app.config.from_object(Config)

    # Register the admin blueprint. In Night 2 the admin area is split across
    # several modules that all bind to ONE blueprint defined in admin/__init__.py;
    # importing `admin_bp` from the package triggers those modules to attach
    # their routes. Importing inside the factory avoids circular-import problems.
    from admin import admin_bp
    app.register_blueprint(admin_bp)

    # Register the STUDENT flow blueprint (the anonymous feedback form served at
    # /f/<token>) — the only surface students reach from their emailed link.
    from student import student_bp
    app.register_blueprint(student_bp)

    # Register the ATR blueprint (Version 2.0 · Module 2): the faculty magic-link
    # "File ATR" surface and the leader (HOD/VD/Dean) login + endorse/return
    # dashboard. Like the others, importing the package triggers its route
    # modules to bind onto atr_bp; registering it once wires them into the app.
    from atr import atr_bp
    app.register_blueprint(atr_bp)

    # Install the SYSTEM-WIDE ACTIVITY LOG (Version 2.0 · Module 3). This one line
    # registers an after_request hook that automatically records every state-
    # changing action — who did it, what, when, where — into master.db.activity_log
    # (see activity_log.py). It deliberately EXCLUDES the student feedback flow so
    # the anonymity guarantee is untouched. Installed last so the hook wraps the
    # requests of every blueprint registered above.
    import activity_log
    activity_log.install(app)

    # Root URL -> the admin dashboard (the professor's home screen). Students
    # never visit "/"; they arrive directly at their /f/<token> link.
    @app.route("/")
    def index():
        return redirect(url_for("admin.dashboard"))

    return app


# Allow "python __init__.py" debugging; documented entry point is run.py.
if __name__ == "__main__":
    create_app().run(debug=True)
