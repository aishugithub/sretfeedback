# ============================================================================
# admin/config_routes.py  —  Manage categories, templates & questions
#                            (spec Sections 6.2 & 6.3) with the data-integrity lock
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# This is the editable "content" side of Group C: the four categories and the
# questionnaires (templates) attached to them. The professor can:
#   * add a new category (e.g. "Internship") — spec 6.2,
#   * view a template's questions, add / edit / delete / reorder them, and edit
#     an answer scale's option weights (e.g. finally set "Discussed Late") — 6.3.
#
# THE CRITICAL RULE (spec 6.3): editing is FREE before responses start, but the
# moment the first response references a template version, that version LOCKS and
# is FROZEN, so live data can never be corrupted by a mid-cycle question change.
#
# HOW THE LOCK IS ENFORCED HERE:
#   * template_version.is_locked is flipped to 1 by the student submit path the
#     instant the first response for that version is written (see student/routes).
#   * These edit routes refuse to change a LOCKED version. To make further edits,
#     the professor clicks "Create editable copy" which CLONES the locked version
#     into a new version_no (is_locked=0). New responses use the new version; old
#     responses stay tied to the old one — CA1 wording stays attached to CA1 data
#     (spec Section 7). This is exactly the versioning the schema was built for.
# ----------------------------------------------------------------------------

from flask import render_template, request, redirect, url_for, flash

from db import get_master
from admin import admin_bp
import services


# ----------------------------------------------------------------------------
# _current_version(conn, template_id) — return the highest-numbered version row
# of a template (the one currently shown/edited). None if a template somehow has
# no version yet.
# ----------------------------------------------------------------------------
def _current_version(conn, template_id):
    return conn.execute(
        "SELECT * FROM template_version WHERE template_id=? "
        "ORDER BY version_no DESC LIMIT 1", (template_id,)).fetchone()


# ============================================================================
# CATEGORIES  (spec 6.2)
# ============================================================================

@admin_bp.route("/categories")
def categories_list():
    conn = get_master()
    # Show each category with how many templates hang off it, for context.
    rows = conn.execute(
        """
        SELECT c.*,
               (SELECT COUNT(*) FROM template t WHERE t.category_id=c.id) AS n_templates,
               (SELECT COUNT(*) FROM offering o WHERE o.category_id=c.id) AS n_offerings
        FROM category c ORDER BY c.id
        """).fetchall()
    conn.close()
    return render_template("categories_list.html", categories=rows)


@admin_bp.route("/categories/new", methods=["GET", "POST"])
def categories_new():
    conn = get_master()
    if request.method == "POST":
        code = request.form.get("code", "").strip().upper()
        name = request.form.get("name", "").strip()
        form_title = request.form.get("form_title", "").strip() or None
        report_key = request.form.get("report_key", "").strip() or None
        if not (code and name):
            conn.close()
            flash("Code and Name are required.", "error")
            return redirect(url_for("admin.categories_new"))
        try:
            conn.execute(
                "INSERT INTO category (code, name, form_title, report_key) "
                "VALUES (?, ?, ?, ?)", (code, name, form_title, report_key))
            # A brand-new category starts with an empty template + version 1 so
            # the professor can immediately start adding questions to it.
            cat_id = conn.execute(
                "SELECT id FROM category WHERE code=?", (code,)).fetchone()["id"]
            conn.execute("INSERT INTO template (category_id, name) VALUES (?, ?)",
                         (cat_id, f"{name} Feedback"))
            tpl_id = conn.execute(
                "SELECT id FROM template WHERE category_id=? ORDER BY id DESC LIMIT 1",
                (cat_id,)).fetchone()["id"]
            conn.execute(
                "INSERT INTO template_version (template_id, version_no, is_locked) "
                "VALUES (?, 1, 0)", (tpl_id,))
            conn.commit()
        except Exception as e:
            conn.close()
            flash(f"Could not add category (duplicate code?): {e}", "error")
            return redirect(url_for("admin.categories_new"))
        conn.close()
        flash(f"Category '{name}' added with an empty template.", "success")
        return redirect(url_for("admin.categories_list"))
    conn.close()
    return render_template("category_edit.html", category=None)


@admin_bp.route("/categories/<int:cat_id>/edit", methods=["GET", "POST"])
def categories_edit(cat_id):
    conn = get_master()
    cat = conn.execute("SELECT * FROM category WHERE id=?", (cat_id,)).fetchone()
    if cat is None:
        conn.close()
        flash("Category not found.", "error")
        return redirect(url_for("admin.categories_list"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        form_title = request.form.get("form_title", "").strip() or None
        report_key = request.form.get("report_key", "").strip() or None
        conn.execute(
            "UPDATE category SET name=?, form_title=?, report_key=? WHERE id=?",
            (name, form_title, report_key, cat_id))
        conn.commit()
        conn.close()
        flash("Category updated.", "success")
        return redirect(url_for("admin.categories_list"))
    conn.close()
    return render_template("category_edit.html", category=cat)


# ============================================================================
# TEMPLATES & QUESTIONS  (spec 6.3)
# ============================================================================

# ----------------------------------------------------------------------------
# GET /admin/templates — list every template with its category, current version
# number, question count and LOCK state, so the professor sees at a glance which
# forms are still editable and which have gone live.
# ----------------------------------------------------------------------------
@admin_bp.route("/templates")
def templates_list():
    conn = get_master()
    rows = conn.execute(
        """
        SELECT t.id, t.name, c.name AS category_name, c.code AS category_code
        FROM template t JOIN category c ON c.id=t.category_id
        ORDER BY c.id
        """).fetchall()
    templates = []
    for t in rows:
        v = _current_version(conn, t["id"])
        nq = conn.execute(
            "SELECT COUNT(*) n FROM question WHERE template_version_id=?",
            (v["id"],)).fetchone()["n"] if v else 0
        templates.append({"row": t, "version": v, "n_questions": nq})
    conn.close()
    return render_template("templates_list.html", templates=templates)


# ----------------------------------------------------------------------------
# GET /admin/templates/<template_id> — the template editor: show the current
# version's questions in order, whether it is locked, and per-question controls.
# ----------------------------------------------------------------------------
@admin_bp.route("/templates/<int:template_id>")
def template_detail(template_id):
    conn = get_master()
    tpl = conn.execute(
        "SELECT t.*, c.name AS category_name FROM template t "
        "JOIN category c ON c.id=t.category_id WHERE t.id=?",
        (template_id,)).fetchone()
    if tpl is None:
        conn.close()
        flash("Template not found.", "error")
        return redirect(url_for("admin.templates_list"))
    version = _current_version(conn, template_id)
    questions = services.questions_for_version(conn, version["id"]) if version else []
    # Scales offered in the "add question" dropdown.
    scales = conn.execute("SELECT id, code, name, is_free_text FROM scale ORDER BY code").fetchall()
    conn.close()
    return render_template("template_detail.html",
                           tpl=tpl, version=version, questions=questions,
                           scales=scales, locked=bool(version and version["is_locked"]))


# ----------------------------------------------------------------------------
# _guard_editable(conn, version) — shared guard for every mutating question
# route. Returns None if editing is allowed, or a redirect if the version is
# locked (with a flash telling the professor to create an editable copy).
# ----------------------------------------------------------------------------
def _guard_editable(version, template_id):
    if version is None:
        flash("This template has no editable version.", "error")
        return redirect(url_for("admin.templates_list"))
    if version["is_locked"]:
        flash("This form is LOCKED because responses have started arriving. "
              "Create an editable copy (new version) to make changes.", "error")
        return redirect(url_for("admin.template_detail", template_id=template_id))
    return None


# ----------------------------------------------------------------------------
# POST /admin/templates/<id>/questions/add — append a new question to the
# current (unlocked) version, at the end of the display order.
# ----------------------------------------------------------------------------
@admin_bp.route("/templates/<int:template_id>/questions/add", methods=["POST"])
def question_add(template_id):
    conn = get_master()
    version = _current_version(conn, template_id)
    guard = _guard_editable(version, template_id)
    if guard:
        conn.close(); return guard

    section = request.form.get("section", "").strip() or None
    text = request.form.get("text", "").strip()
    scale_id = request.form.get("scale_id", "").strip()
    if not (text and scale_id.isdigit()):
        conn.close()
        flash("Question text and a scale are required.", "error")
        return redirect(url_for("admin.template_detail", template_id=template_id))

    # New question goes at the end: next display_order = max+1.
    nxt = conn.execute(
        "SELECT COALESCE(MAX(display_order),0)+1 AS n FROM question "
        "WHERE template_version_id=?", (version["id"],)).fetchone()["n"]
    conn.execute(
        "INSERT INTO question (template_version_id, section, text, scale_id, display_order) "
        "VALUES (?, ?, ?, ?, ?)", (version["id"], section, text, int(scale_id), nxt))
    conn.commit(); conn.close()
    flash("Question added.", "success")
    return redirect(url_for("admin.template_detail", template_id=template_id))


# ----------------------------------------------------------------------------
# POST /admin/templates/<id>/questions/<qid>/edit — edit one question's section,
# text or scale (unlocked versions only).
# ----------------------------------------------------------------------------
@admin_bp.route("/templates/<int:template_id>/questions/<int:qid>/edit", methods=["POST"])
def question_edit(template_id, qid):
    conn = get_master()
    version = _current_version(conn, template_id)
    guard = _guard_editable(version, template_id)
    if guard:
        conn.close(); return guard
    section = request.form.get("section", "").strip() or None
    text = request.form.get("text", "").strip()
    scale_id = request.form.get("scale_id", "").strip()
    conn.execute(
        "UPDATE question SET section=?, text=?, scale_id=? "
        "WHERE id=? AND template_version_id=?",
        (section, text, int(scale_id) if scale_id.isdigit() else None,
         qid, version["id"]))
    conn.commit(); conn.close()
    flash("Question updated.", "success")
    return redirect(url_for("admin.template_detail", template_id=template_id))


# ----------------------------------------------------------------------------
# POST /admin/templates/<id>/questions/<qid>/delete — remove a question and
# renumber the rest so display_order stays contiguous (unlocked versions only).
# ----------------------------------------------------------------------------
@admin_bp.route("/templates/<int:template_id>/questions/<int:qid>/delete", methods=["POST"])
def question_delete(template_id, qid):
    conn = get_master()
    version = _current_version(conn, template_id)
    guard = _guard_editable(version, template_id)
    if guard:
        conn.close(); return guard
    conn.execute("DELETE FROM question WHERE id=? AND template_version_id=?",
                 (qid, version["id"]))
    # Renumber remaining questions 1..N in their existing order.
    remaining = conn.execute(
        "SELECT id FROM question WHERE template_version_id=? ORDER BY display_order",
        (version["id"],)).fetchall()
    for i, r in enumerate(remaining, start=1):
        conn.execute("UPDATE question SET display_order=? WHERE id=?", (i, r["id"]))
    conn.commit(); conn.close()
    flash("Question deleted.", "success")
    return redirect(url_for("admin.template_detail", template_id=template_id))


# ----------------------------------------------------------------------------
# POST /admin/templates/<id>/questions/<qid>/move — reorder by swapping this
# question's display_order with its neighbour (dir = 'up' or 'down').
# ----------------------------------------------------------------------------
@admin_bp.route("/templates/<int:template_id>/questions/<int:qid>/move", methods=["POST"])
def question_move(template_id, qid):
    conn = get_master()
    version = _current_version(conn, template_id)
    guard = _guard_editable(version, template_id)
    if guard:
        conn.close(); return guard
    direction = request.form.get("dir", "")
    me = conn.execute("SELECT * FROM question WHERE id=?", (qid,)).fetchone()
    if me:
        # Find the adjacent question in the requested direction.
        if direction == "up":
            nb = conn.execute(
                "SELECT * FROM question WHERE template_version_id=? AND display_order<? "
                "ORDER BY display_order DESC LIMIT 1",
                (version["id"], me["display_order"])).fetchone()
        else:
            nb = conn.execute(
                "SELECT * FROM question WHERE template_version_id=? AND display_order>? "
                "ORDER BY display_order ASC LIMIT 1",
                (version["id"], me["display_order"])).fetchone()
        if nb:
            # Swap the two display_order values.
            conn.execute("UPDATE question SET display_order=? WHERE id=?",
                         (nb["display_order"], me["id"]))
            conn.execute("UPDATE question SET display_order=? WHERE id=?",
                         (me["display_order"], nb["id"]))
            conn.commit()
    conn.close()
    return redirect(url_for("admin.template_detail", template_id=template_id))


# ----------------------------------------------------------------------------
# POST /admin/templates/<id>/new-version — CLONE a locked version into a fresh
# editable one (is_locked=0), copying every question verbatim. This is how the
# professor edits a form after it has gone live without touching past data.
# ----------------------------------------------------------------------------
@admin_bp.route("/templates/<int:template_id>/new-version", methods=["POST"])
def template_new_version(template_id):
    conn = get_master()
    old = _current_version(conn, template_id)
    if old is None:
        conn.close()
        flash("Template has no version to copy.", "error")
        return redirect(url_for("admin.templates_list"))
    # Create the next version number, unlocked.
    new_no = old["version_no"] + 1
    conn.execute(
        "INSERT INTO template_version (template_id, version_no, is_locked) "
        "VALUES (?, ?, 0)", (template_id, new_no))
    new_v = conn.execute(
        "SELECT id FROM template_version WHERE template_id=? AND version_no=?",
        (template_id, new_no)).fetchone()["id"]
    # Copy every question from the old version into the new one, verbatim.
    for q in conn.execute(
            "SELECT section, text, scale_id, display_order FROM question "
            "WHERE template_version_id=? ORDER BY display_order", (old["id"],)):
        conn.execute(
            "INSERT INTO question (template_version_id, section, text, scale_id, display_order) "
            "VALUES (?, ?, ?, ?, ?)",
            (new_v, q["section"], q["text"], q["scale_id"], q["display_order"]))
    conn.commit(); conn.close()
    flash(f"Created editable version {new_no}. Old version stays frozen with its "
          f"responses; new submissions will use this version.", "success")
    return redirect(url_for("admin.template_detail", template_id=template_id))


# ============================================================================
# SCALES & OPTION WEIGHTS  (needed for spec Open Item 14.2 — "Discussed Late")
# ============================================================================

# ----------------------------------------------------------------------------
# GET /admin/scales — list every answer scale with its options + weights, so the
# professor can inspect and adjust weights (e.g. finally set "Discussed Late").
# ----------------------------------------------------------------------------
@admin_bp.route("/scales")
def scales_list():
    conn = get_master()
    scales = conn.execute("SELECT * FROM scale ORDER BY code").fetchall()
    data = []
    for s in scales:
        opts = conn.execute(
            "SELECT * FROM scale_option WHERE scale_id=? ORDER BY display_order",
            (s["id"],)).fetchall()
        data.append({"scale": s, "options": opts})
    conn.close()
    return render_template("scales_list.html", data=data)


# ----------------------------------------------------------------------------
# POST /admin/scales/option/<option_id>/weight — update ONE option's weight (or
# fraction). Weights feed the Night-3 scoring engine; because answers store the
# option LABEL (not the weight), changing a weight here re-scores cleanly with no
# data migration. Blank weight is allowed (kept NULL) — the deliberate state for
# an unresolved item like "Discussed Late".
# ----------------------------------------------------------------------------
@admin_bp.route("/scales/option/<int:option_id>/weight", methods=["POST"])
def scale_option_weight(option_id):
    conn = get_master()
    weight = request.form.get("weight", "").strip()
    fraction = request.form.get("fraction", "").strip()
    # Empty string -> NULL; otherwise parse a float.
    w = float(weight) if weight not in ("",) else None
    f = float(fraction) if fraction not in ("",) else None
    conn.execute("UPDATE scale_option SET weight=?, fraction=? WHERE id=?",
                 (w, f, option_id))
    conn.commit(); conn.close()
    flash("Option weight updated.", "success")
    return redirect(url_for("admin.scales_list"))
