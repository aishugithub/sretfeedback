# ============================================================================
# admin/__init__.py  —  Defines the shared `admin` blueprint and wires in every
#                       admin route module (spec Section 6)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# Night 1 had a single admin file (roster only). Night 2 grows the admin area to
# the FULL Section-6 control panel: students, categories, templates/questions,
# cycles, email text, token generation, and the live participation view. That is
# far too much for one file, so we split it into focused modules that ALL attach
# their routes to ONE blueprint defined here:
#
#     admin/__init__.py      -> creates `admin_bp` (this file)
#     admin/routes.py        -> dashboard + roster (offerings) screens
#     admin/students.py      -> Student Master: upload / list / edit / close-out
#     admin/config_routes.py -> categories + templates/questions (with the lock)
#     admin/cycles.py        -> cycles, email text, tokens, participation
#
# The app factory (app/__init__.py) does `from admin import admin_bp` and
# registers it once. Importing the route modules at the bottom of THIS file is
# what actually binds their @admin_bp.route handlers onto the blueprint — a
# common Flask pattern that avoids circular imports (each module imports the
# already-created `admin_bp` from here, not the other way round).
# ----------------------------------------------------------------------------

from flask import Blueprint

# The one blueprint every admin screen hangs off. url_prefix="/admin" means each
# route lives under /admin/... ; template_folder points at the shared templates
# directory one level up.
admin_bp = Blueprint("admin", __name__, url_prefix="/admin",
                     template_folder="../templates")

# Import the route modules AFTER admin_bp exists so their decorators can bind to
# it. These imports are for their side effects (registering routes).
from admin import routes          # noqa: E402,F401  dashboard + roster
from admin import students        # noqa: E402,F401  per-cycle roster + exclusions
from admin import uploads         # noqa: E402,F401  allocation + enrollment + readiness (v3)
from admin import config_routes   # noqa: E402,F401  categories + templates
from admin import cycles          # noqa: E402,F401  cycles + tokens + participation + archive
from admin import reports         # noqa: E402,F401  scoring + Excel/PDF report export
from admin import users           # noqa: E402,F401  Users & Roles: leaders + set-pw invites (§17)
from admin import faculty         # noqa: E402,F401  Manage Faculty: the faculty master (Module 5)
from admin import auth            # noqa: E402,F401  ADMIN login/logout/forgot + the login gate (v2.1)
from admin import status          # noqa: E402,F401  cycle status / ATR tracking board (v2.1)
