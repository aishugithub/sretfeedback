# ============================================================================
# student/__init__.py  —  Defines the `student` blueprint (the anonymous form)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# This package holds the ONLY surface a student ever touches: the feedback form
# served at /f/<token> (spec Section 8). It is kept separate from the admin
# package so its routes, and its security posture (no admin powers, no identity
# leakage into answers), stay clearly isolated.
#
# The blueprint is created here and its routes are attached in student/routes.py
# (imported at the bottom, the same side-effect pattern the admin package uses).
# Note there is NO url_prefix="/admin"; the student link is deliberately short
# and friendly: /f/<token>.
# ----------------------------------------------------------------------------

from flask import Blueprint

# No url_prefix — routes define their own paths (/f/<token> etc.). template_folder
# points at the shared templates directory one level up.
student_bp = Blueprint("student", __name__, template_folder="../templates")

from student import routes  # noqa: E402,F401  binds the /f/<token> routes
