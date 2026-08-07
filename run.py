# ============================================================================
# run.py  —  Development/LAN entry point for the Flask web app
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# This is the script the professor runs to START the web server once the
# database has been initialised (python init_db.py). It builds the Flask app via
# the application factory (app/__init__.py -> create_app) and serves it.
#
#   host="0.0.0.0"  -> listen on ALL network interfaces, so other devices on the
#                      college LAN (student phones, lab PCs) can reach the admin
#                      page and — later — the student form at the laptop's LAN IP
#                      (spec Section 8/13: "your laptop on the college LAN").
#   port=5000       -> the default Flask port; change if 5000 is taken.
#   debug=True      -> auto-reload on code changes + helpful error pages while
#                      building. Turn OFF for a real collection session.
#
# Usage (from inside the app/ folder):
#     python run.py
# Then browse to http://localhost:5000/ (redirects to the admin roster), or from
# another LAN device to http://<laptop-lan-ip>:5000/.
# ============================================================================

from __init__ import create_app   # the application factory
# NOTE: the app/.env file (which holds the FEEDBACK_GMAIL_* secrets) is loaded
# automatically by app/__init__.py the moment it is imported on the line above —
# see the _load_dotenv() call at the top of that file. That means the secrets are
# loaded no matter HOW the app is started (this run.py locally, or PythonAnywhere's
# WSGI file, which imports create_app directly and never runs this script). So
# there is nothing to set up here.

# Build the configured Flask application.
app = create_app()

if __name__ == "__main__":
    # 0.0.0.0 exposes the app to the LAN; keep debug on only during the build.
    app.run(host="0.0.0.0", port=5000, debug=True)
