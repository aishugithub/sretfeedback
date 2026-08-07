# ============================================================================
# atr/__init__.py  —  Defines the `atr` blueprint (Version 2.0 · Module 2)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# Module 2 adds two new surfaces on top of the Version 1.0 admin + student
# blueprints:
#   * FACULTY act through magic links (no login): the "File ATR" button.
#   * LEADERS (HOD/VD/Dean) log in and endorse/return ATRs, and HODs send
#     reminders.
# Both live on THIS one blueprint, following the exact side-effect pattern the
# admin and student packages use: the blueprint is created here, and its routes
# are attached by importing atr.routes at the bottom (each @atr_bp.route binds
# on import). The app factory (app/__init__.py) does `from atr import atr_bp` and
# registers it once.
#
# There is NO url_prefix: the routes define friendly, absolute paths
# (/atr/file, /leader/login, /atr/review/<cycle>/<id>, ...) that match the links
# emailed to faculty and leaders — an emailed URL must not depend on a prefix.
# ----------------------------------------------------------------------------

from flask import Blueprint

# template_folder points at the shared templates directory one level up, so the
# ATR/leader templates live alongside the admin/student ones and reuse base.html.
atr_bp = Blueprint("atr", __name__, template_folder="../templates")

from atr import routes  # noqa: E402,F401  binds all the ATR + leader routes
