# ============================================================================
#                      (spec Section 11 — the "Feedback Report V2.0" layout)
# ============================================================================
# WHERE THIS FITS IN THE WHOLE APPLICATION
# ----------------------------------------------------------------------------
# scoring.py turns anonymous answers into numbers. THIS module turns those
# numbers into the two documents the college actually hands out per course /
# faculty (spec Section 11):
#
#     build_pdf_report(result, path)    -> a print-ready .pdf of the same
#
# Both consume the SAME `result` dict produced by scoring.score_offering(), so
# computation. A small shared helper, `build_view_model(result)`, arranges the
# raw scores into the exact blocks the report shows, in order:
#     1. Header/identity block   (AY · Year/Term · Course · Programme · Faculty)
#     2. No. of Students Feedback Recorded
#     3. Overall Score            (the grand overall from scoring.py)
#     4. Section scores            (one row per report section)
#     5. Section tables            (per-question option counts + average /10)
#
# (v3.2) At the professor's request the report was SLIMMED DOWN — the per-section
# BAR CHARTS, the OPEN-ENDED COMMENTS block, and the DEPARTMENT-CODE LEGEND were
# all removed, leaving a compact, numbers-only document. build_view_model still
# computes the legend/comments fields (scoring is untouched); the renderers below
# simply no longer draw them.
#
# Bulk generation (spec Section 11 "bulk for a whole batch") is provided by
# build_batch_* helpers that loop these per-offering builders and package the
#
# (pure Python, pip-installable, no system libraries) for PDF — both run happily
# on the professor's laptop with no extra OS packages (spec Section 8/13).
# ----------------------------------------------------------------------------

import io
import os
import zipfile


# reportlab — PDF document building. (v3.2: the per-section bar chart was removed,
# so the reportlab.graphics chart/shape imports are no longer needed.)
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, KeepTogether, Flowable)
from xml.sax.saxutils import escape as _xml_escape

from config import Config


# ============================================================================
# SECTION 1 — THE DEPARTMENT-CODE LEGEND (spec Section 11)
# ============================================================================
# The approved report prints this legend verbatim under the header. We keep the
# full descriptive names here (the config map holds short names for validation);
# these long forms match the text block in the V2.0 workbooks (Sheet2!A18).
# ----------------------------------------------------------------------------
DEPT_LEGEND = [
    ("E01", "B. Tech Computer Science and Engineering (Artificial Intelligence and Machine Learning)"),
    ("E02", "B. Tech Computer Science and Engineering (Cyber Security and Internet of Things)"),
    ("E03", "B. Tech Computer Science and Engineering (Artificial Intelligence and Data Analytics)"),
    ("E05", "B. Tech Computer Science and Medical Engineering (Artificial Intelligence and Data Analytics)"),
    ("E06", "B. Tech Electronics and Communication Engineering"),
    ("E52", "B. Sc Computer Science (Artificial Intelligence and Data Analytics)"),
    ("E61", "B. Sc Bio Informatics"),
    ("E62", "B. Sc Data Science"),
    ("E71", "M. Sc Artificial Intelligence"),
    ("E73", "M. Sc Data Analytics"),
    ("E81", "M. Sc Medical Bioinformatics"),
]


# ============================================================================
# SECTION 2 — THE SHARED VIEW MODEL
# ============================================================================
# Arrange one scored offering's `result` into the ordered blocks both renderers
# the same identity line, the same section order, and the same tables.
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# _name_titled(name) — move a trailing honorific to the FRONT (Aug 2026)
# ----------------------------------------------------------------------------
# The allocation stores teacher names with the title LAST, e.g. "Manoj Kumaran S
# Mr." or "Abhinand P A Dr.". Reports read better with the honorific in front —
# "Mr. Manoj Kumaran S", "Dr. Abhinand P A". We detect a final token that is a
# known honorific (with or without a dot, any case), normalise it to "Xx.", and
# move it to the front. Names without a trailing honorific are returned unchanged.
# ----------------------------------------------------------------------------
_HONORIFICS = {"dr": "Dr.", "mr": "Mr.", "ms": "Ms.", "mrs": "Mrs.",
               "prof": "Prof.", "miss": "Miss", "shri": "Shri", "smt": "Smt."}


def _name_titled(name):
    parts = (name or "").strip().split()
    if len(parts) < 2:
        return (name or "").strip()
    key = parts[-1].rstrip(".").lower()
    if key in _HONORIFICS:
        return _HONORIFICS[key] + " " + " ".join(parts[:-1])
    return " ".join(parts)


def _section_batch(section):
    """Section A -> 'Sec A / Batch 1', Section B -> 'Sec B / Batch 2', etc., so a
    report for one section of a split course clearly states which batch it covers.
    A course with no real section ('NA'/blank) returns '' (nothing to show)."""
    s = (section or "").strip().upper()
    if not s or s in ("NA", "N/A", "-", "NONE"):
        return ""
    idx = {"A": "1", "B": "2", "C": "3", "D": "4", "E": "5", "F": "6"}.get(s)
    return "Sec %s / Batch %s" % (s, idx) if idx else "Sec %s" % s


def build_view_model(result):
    o = result["offering"]

    # --- 2a. Header identity (mirrors workbook Sheet2!A4). -------------------
    # Year/Term is shown as "Y<n> - Sem <m>" using the offering's stored year and
    # semester. Missing pieces degrade gracefully to a blank rather than crash.
    year_term = f"Y{o['year_of_study']}"
    if o["semester"]:
        year_term += f" - Sem {o['semester']}"
    section = _section_batch(o["section"] if "section" in o.keys() else "")

    # ---- CONSOLIDATED-ELECTIVE header (Aug 2026 change) ----------------------
    # When this report was produced by scoring.score_offering_group over a pooled
    # elective delivery, `result["is_consolidated"]` is True and
    # `result["group_dept_codes"]` lists every programme code that sat in the one
    # class (e.g. ['E01','E02','E03']). In that case the PROGRAM CODE line shows
    # ALL of them and the PROGRAMME line names the elective basket it was pooled
    # across — instead of the anchor's single programme, which would misleadingly
    # read as if only that programme were surveyed. A normal (non-elective) report
    # keeps exactly its previous single-programme header.
    is_consolidated = bool(result.get("is_consolidated"))
    group_codes = result.get("group_dept_codes") or (
        [o["dept_code"]] if (o["dept_code"] or "") else [])
    basket = o["elective_basket"] if "elective_basket" in o.keys() else None
    if is_consolidated:
        dept_code_display = ", ".join(group_codes)
        programme_display = (
            "Elective basket %s — pooled across %s"
            % (("'%s'" % basket) if basket else "(elective)", ", ".join(group_codes)))
    else:
        # Show the OFFICIAL expanded programme name looked up by programme code
        # (Config.PROGRAMMES, kept current Aug 2026), falling back to the stored
        # value if the code is unknown.
        dept_code_display = o["dept_code"] or ""
        programme_display = (Config.PROGRAMMES.get(o["dept_code"] or "", (None,))[0]
                             or o["programme"] or "")

    header = {
        "section": section,
        "academic_year": o["academic_year"],
        "year_term": year_term,
        "course_code": o["course_code"] or "",
        "course_name": (o["course_name"] or "").upper(),
        "programme": programme_display,
        "dept_code": dept_code_display,
        "faculty": _name_titled(o["faculty"] or ""),
        "category": result.get("report_key") or "",
        "category_name": o["category_name"] if "category_name" in o.keys() else "",
        # Carried so a renderer could badge the report as a pooled elective; the
        # current layouts read it implicitly through the fields above.
        "is_consolidated": is_consolidated,
        "elective_basket": basket if is_consolidated else None,
    }

    # --- 2b. Section score rows (report section title + /10 score). ----------
    section_rows = [
        {"title": s["title"], "score": s["score"], "score_2dp": s["score_2dp"]}
        for s in result["section_scores"]
    ]

    # --- 2c. Per-section detail tables: for each report section, list its
    #         questions with the per-option counts, so we can draw the count
    #         tables + charts exactly as the workbook arranges them. Syllabus is
    #         a special case (a single fraction question) — we still show its
    #         option counts (100%/80%/60%).
    detail_sections = []
    pq = result["per_question"]
    # Index questions by id for quick lookup and to preserve display order.
    for s in result["section_scores"]:
        qblocks = []
        for qid in s["question_ids"]:
            info = pq.get(qid)
            if info is None or info["is_free_text"]:
                continue
            q = info["question"]
            # Ordered (label, count) pairs following the option display order.
            opts = sorted(q["options"], key=lambda op: op["display_order"])
            counts = [(op["label"], info["option_counts"].get(op["label"], 0))
                      for op in opts]
            qblocks.append({
                "text": q["text"],
                "counts": counts,
                "average": info["average"],
            })
        # Carry the SECTION score too. Some sections (notably Syllabus) score at
        # the section level and leave every per-question average None; the chart
        # falls back to this so such a section still shows a real bar.
        detail_sections.append({"title": s["title"], "questions": qblocks,
                                "score": s["score"]})

    return {
        "header": header,
        "legend": DEPT_LEGEND,
        "n_recorded": result["n_recorded"],
        "overall": result["overall"],
        "overall_2dp": result["overall_2dp"],
        "section_rows": section_rows,
        "detail_sections": detail_sections,
        "open_comments": result["open_comments"],
    }


def _cmt_markup(text):
    """Make a verbatim student comment safe to drop inside a ReportLab Paragraph.

    A Paragraph parses its text as light XML-ish markup, so a stray '&', '<' or
    '>' in a student's free text would raise a parse error and blank the whole
    report. We escape those three characters and then turn real newlines into
    <br/> line breaks so multi-line comments keep their shape on the page.
    """
    return _xml_escape(str(text)).replace("\r\n", "\n").replace("\n", "<br/>")


def _fmt(v):
    """Format a /10 score for display: 2 decimals, or '—' when unanswered."""
    return "—" if v is None else f"{v:.2f}"


# ============================================================================
# SECTION 3 — EXCEL RENDERER (matches Feedback Report V2.0 layout)
# ============================================================================

# Reusable styling constants (kept here so a restyle is one-line-per-look).


# SECTION 4 — PDF RENDERER (print-ready, matches the same layout)
# ============================================================================

def _pdf_styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle("Ident", parent=ss["Normal"], fontSize=9, leading=12))
    ss.add(ParagraphStyle("SecTitle", parent=ss["Heading2"], textColor=colors.HexColor("#0B3D68")))
    ss.add(ParagraphStyle("Cmt", parent=ss["Normal"], fontSize=9, leading=12, leftIndent=8))
    ss.add(ParagraphStyle("Legend", parent=ss["Normal"], fontSize=7.5, leading=10))
    # Centred, small header style for the option-count table columns, so long
    # labels ("Strongly Disagree") wrap tidily inside their column instead of
    # overlapping the next one.
    ss.add(ParagraphStyle("Hdr", parent=ss["Normal"], fontSize=7, leading=8,
                          alignment=1, fontName="Helvetica-Bold"))
    return ss


# (v3.2) The per-question average bar chart (_section_avg_chart) and its bar
# colour constant were REMOVED here as part of the report slim-down. The exact
# per-question averages are still shown — as the numeric "Avg" column in each
# section table below — so no information is lost, only the graphic.


# ----------------------------------------------------------------------------
# NumberedCanvas — a reportlab canvas that stamps a proper footer with
# "Page X of Y" (the TOTAL is only known once every page is laid out).
#
# HOW IT WORKS: reportlab draws pages one at a time and cannot know the total up
# front. We override showPage()/save() to BUFFER each page's state; at save() the
# total is known, so we replay every page drawing the footer with the real X of Y.
#
# (v3.2) The report used to be PADDED to an even page count with a captioned
# duplex-pad page and a trailing blank "remarks" page (old spec §13.2). At the
# professor's request that padding was REMOVED — reports now print at their true
# length (no blank pages), so "Page X of Y" reflects real content pages only.
# ----------------------------------------------------------------------------
from reportlab.pdfgen import canvas as _rl_canvas  # noqa: E402


class _ReportStart(Flowable):
    """A zero-height marker dropped in as the FIRST flowable of each report (and
    of each department divider in the by-HOD combined PDF).

    It draws nothing and takes no space, but at layout time it records — on the
    shared NumberedCanvas — the page number on which this unit begins. Those
    recorded boundaries are what NumberedCanvas.save() uses to pad every unit to
    an EVEN number of pages, so that when many reports are printed back-to-back on
    a duplex printer, each teacher's report starts on the FRONT of a fresh sheet
    and never on the back of the previous teacher's last page. Fits into the whole
    pipeline as the tiny hook that turns "one long story of many reports" into
    "many reports that each own a whole number of sheets".
    """
    width = 0
    height = 0

    def wrap(self, availWidth, availHeight):
        return (0, 0)              # occupies no space, forces no break of its own

    def draw(self):
        canv = self.canv
        if not hasattr(canv, "_report_starts"):
            canv._report_starts = []
        # canv._pageNumber is the 1-based page currently being laid out, i.e. the
        # first page of the report this marker introduces.
        canv._report_starts.append(canv._pageNumber)


class NumberedCanvas(_rl_canvas.Canvas):
    footer_left = ""          # set per-build: "faculty · course_code"
    _watermark = None         # set per-build: diagonal text, e.g. "TEST DATA"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []
        # Filled by _ReportStart.draw(): the 1-based page on which each report /
        # divider unit begins. Used by save() for even-page (duplex) padding.
        self._report_starts = []

    @staticmethod
    def _page_is_empty(state):
        """True when a buffered page drew NO real content. ReportLab keeps the
        page's PDF operators in `_code`; a content page shows text (Tj/TJ) or an
        image (Do), a phantom blank page shows neither. (The footer/watermark are
        added later during save-time replay, so they are not in `_code` yet and a
        phantom is still detectable here.)"""
        import re as _re
        code = state.get("_code")
        joined = "".join(code) if isinstance(code, list) else (code or "")
        return not _re.search(r"\)\s*Tj|\]\s*TJ|\bDo\b", joined)

    def showPage(self):
        # Buffer this page instead of emitting it, so we can revisit at save().
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        # Every buffered page is a real content page. The total is now known, so
        # we replay each one stamping the true "Page X of Y" footer.
        #
        # DUPLEX PADDING (restored Sept 2026, smarter than the old blanket pad):
        # each report/divider was marked at its first page by a _ReportStart
        # flowable, so self._report_starts holds the page each unit begins on. A
        # unit that spans an ODD number of pages gets ONE blank "intentionally
        # left blank" page appended, making every unit even. On a double-sided
        # printer that guarantees the next teacher's report starts on the front of
        # a new sheet. If no markers were placed (an unexpected caller), nothing
        # is padded and the output is byte-for-byte the old behaviour.
        states = self._saved_page_states
        # FIRST drop any TRAILING blank pages. ReportLab can emit an empty final
        # page when the last flowable draws nothing (e.g. a spacer that spills off
        # the foot of a full page). If we counted that phantom, an otherwise-even
        # report would look ODD and get a needless blank duplex pad after it. A
        # real content page always draws text/images; a phantom draws neither.
        while len(states) > 1 and self._page_is_empty(states[-1]):
            states.pop()
        total = len(states)
        starts = sorted(set(p for p in (getattr(self, "_report_starts", []) or [])
                            if p <= total))
        pad_after = set()          # 1-based content page numbers to pad AFTER
        for i, s in enumerate(starts):
            end = (starts[i + 1] - 1) if i + 1 < len(starts) else total
            if (end - s + 1) % 2 == 1:          # odd-length unit -> pad to even
                pad_after.add(end)
        for idx, state in enumerate(states):
            self.__dict__.update(state)
            if self._watermark:
                self._draw_watermark()
            self._draw_footer(total)
            super().showPage()                  # emit this real content page
            if (idx + 1) in pad_after:
                # reportlab has just started a fresh page; emit it COMPLETELY
                # BLANK — nothing drawn on it: no caption, no footer, no page
                # number — as the duplex separator, then carry on. "Page X of Y"
                # keeps counting real content pages only, so the numbering simply
                # skips the blank sheet.
                super().showPage()
        super().save()

    def _draw_watermark(self):
        """Large, faint, diagonal watermark across the page (spec §9.1 test mode)."""
        self.saveState()
        self.setFont("Helvetica-Bold", 60)
        self.setFillColor(colors.Color(0.85, 0.2, 0.2, alpha=0.16))
        self.translate(A4[0] / 2.0, A4[1] / 2.0)
        self.rotate(45)
        self.drawCentredString(0, 0, self._watermark)
        self.restoreState()

    def _draw_footer(self, total, page_num=None):
        """Two-line footer (spec §13.1, descriptive form):
          line 1 (just above a thin rule): the identity line on the LEFT and the
                 per-report "Page X of Y" on the RIGHT;
          line 2 (below): the confidentiality notice, small and centred.
        Because every report is now rendered as its OWN PDF (the by-HOD/batch
        combined files are built by merging these), `self._pageNumber`/`total`
        are already this one report's local page and page-count — so numbering
        restarts at "Page 1 of N" for each teacher, never running across the
        whole consolidated file."""
        page_num = self._pageNumber if page_num is None else page_num
        self.saveState()
        y = 11 * mm
        self.setStrokeColor(colors.HexColor("#b8c2cc"))
        self.setLineWidth(0.5)
        self.line(15 * mm, y + 3.5 * mm, A4[0] - 15 * mm, y + 3.5 * mm)  # thin rule
        left_x = 15 * mm
        right_x = A4[0] - 15 * mm
        # --- Page number (right), bold. Drawn first so we know its width and can
        #     keep the identity line from ever overlapping it.
        pnum = "Page %s of %s" % (page_num, total)
        self.setFont("Helvetica-Bold", 7)
        self.setFillColor(colors.HexColor("#333333"))
        self.drawRightString(right_x, y, pnum)
        # --- Identity line (left). Shrink the font (7 -> 5pt) to fit the space
        #     left of the page number; if it still will not fit, trim with an
        #     ellipsis. This guarantees the two never collide, however long a
        #     course name or elective code-list is.
        ident = self.footer_left or ""
        gap = 6 * mm
        avail = right_x - left_x - self.stringWidth(pnum, "Helvetica-Bold", 7) - gap
        size = 7.0
        while size > 5.0 and self.stringWidth(ident, "Helvetica", size) > avail:
            size -= 0.5
        if self.stringWidth(ident, "Helvetica", size) > avail:
            while ident and self.stringWidth(ident + "\u2026", "Helvetica", size) > avail:
                ident = ident[:-1]
            ident = ident + "\u2026"
        self.setFont("Helvetica", size)
        self.setFillColor(colors.HexColor("#333333"))
        self.drawString(left_x, y, ident)
        # --- Line 2: confidentiality notice, smaller and fainter, centred below.
        self.setFont("Helvetica", 6)
        self.setFillColor(colors.HexColor("#888888"))
        self.drawCentredString(A4[0] / 2.0, y - 4 * mm,
                               "Confidential — For faculty development purposes only")
        self.restoreState()


def _footer_left_for(result):
    """Descriptive footer identity line for ONE report, in the Dean's order:
        <HOD dept> | Dr. <Faculty> | <programme code> | <course code>
        | <Course Name> | <cycle label>

    Every field is read from the report's own identity row:
    - HOD dept  = the faculty's HOME department (the HOD the teacher reports to);
      scoring attaches it as offering["hod_dept_code"]. Falls back to the
      programme code if it is somehow missing.
    - programme code = the course's own programme (offering.dept_code); for a
      pooled elective it is the joined list of every programme in the delivery.
    - cycle label = e.g. "CA1 - Intermediate" (offering["cycle_label"]), so a
      printed report always says which assessment cycle it belongs to.
    Missing fields are skipped so the line never shows a dangling separator.
    """
    o = result["offering"]

    def g(key):                       # safe read from a sqlite Row OR a dict
        try:
            return o[key]
        except Exception:
            return None

    hod = str(g("hod_dept_code") or g("dept_code") or "").strip()
    faculty = _name_titled(g("faculty") or "") or "Faculty"
    if result.get("is_consolidated") and result.get("group_dept_codes"):
        prog = ", ".join(result["group_dept_codes"])
    else:
        prog = str(g("dept_code") or "").strip()
    course_code = str(g("course_code") or "").strip()
    course_name = str(g("course_name") or "").strip()
    cycle = str(g("cycle_label") or "").strip()
    fields = (hod, faculty, prog, course_code, course_name, cycle)
    return "  |  ".join([p for p in fields if p])


def build_pdf_report(result, path_or_buffer):
    """Render one offering's report to a PDF (a filesystem path or a BytesIO,
    the latter used by the combined-batch PDF so pages can be concatenated).

    Uses NumberedCanvas so every page carries the confidential footer with
    "Page X of Y". (v3.2: the report is no longer padded — it prints at its true
    length with no trailing blank pages.)
    """
    ss = _pdf_styles()
    doc = SimpleDocTemplate(path_or_buffer, pagesize=A4,
                            topMargin=14 * mm, bottomMargin=18 * mm,
                            leftMargin=15 * mm, rightMargin=15 * mm,
                            title="SRET Feedback Report")
    story = []
    _append_pdf_story(result, story, ss)

    # Bind the per-report footer-left text onto the canvas class for this build.
    footer_left = _footer_left_for(result)
    watermark = result.get("watermark")   # e.g. "TEST DATA" for test cycles (§9.1)

    class _C(NumberedCanvas):
        pass
    _C.footer_left = footer_left
    _C._watermark = watermark

    doc.build(story, canvasmaker=_C)
    return path_or_buffer


# ============================================================================
# SECTION 5 — FILENAMES + BULK (per-batch) GENERATION (spec Section 11)
# ============================================================================

def safe_filename(result, ext):
    """A tidy, unique-enough filename from the offering identity, e.g.
    'E02_Y3_CSE23CT301_Prof-Manoj.xlsx'. Non-filename characters are stripped."""
    o = result["offering"]
    # Include the offering id (when present) so two offerings that share a
    # dept/year/code/faculty — or a batch that repeats one — never collide on
    # the same archive filename.
    oid = o["id"] if ("id" in o.keys()) else None
    # For a pooled elective the anchor's single dept_code would be misleading in the
    # filename, so we label it 'ELECTIVE' (the report itself lists every programme).
    dept_part = "ELECTIVE" if result.get("is_consolidated") else (o["dept_code"] or "NA")
    parts = [dept_part, f"Y{o['year_of_study']}",
             o["course_code"] or "NOCODE", (o["faculty"] or "NoFaculty")]
    if oid is not None:
        parts.append(f"id{oid}")
    raw = "_".join(str(p) for p in parts)
    keep = "".join(ch if (ch.isalnum() or ch in "._-") else "-" for ch in raw)
    return f"{keep}.{ext}"


def _safe_folder(name):
    """Turn a staff display name into a safe ZIP folder name. We keep letters,
    digits, spaces, dot, dash and underscore (so "Dr. Priya R" stays readable) and
    replace anything else — path separators above all — with a dash, so a stray
    "/" in a name can never escape the archive folder."""
    name = (name or "Unknown Faculty").strip()
    keep = "".join(ch if (ch.isalnum() or ch in " ._-") else "-" for ch in name)
    keep = keep.strip(" .") or "Unknown Faculty"   # never an empty/blank folder
    return keep


def build_staff_foldered_pdf_zip(results, zip_path):
    """Download-all-reports ZIP, arranged by STAFF (Dean's ask, Sept 2026).

    Layout inside the .zip:
        <Staff Name>/<report>.pdf        # one folder per teacher
        <Staff Name>/<another report>.pdf
        <Other Staff>/<report>.pdf

    `results` is the list of scored-offering dicts (each already pooled to its
    delivery by the caller). We group them by the offering's faculty display name,
    render each report to an in-memory PDF, and write it under that teacher's
    folder. Filenames come from safe_filename() (which includes the offering id),
    so two courses by the same teacher never collide; as a belt-and-suspenders we
    still de-duplicate identical names within a folder.

    This reuses the SAME single-report builder (build_pdf_report) the on-screen
    "PDF" button uses, so a course's file here is byte-for-byte the report a HOD
    would download individually — the ZIP is purely a convenience wrapper.
    """
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        used_per_folder = {}                       # folder -> set(filenames used)
        for res in results:
            offering = res["offering"]
            folder = _safe_folder(offering["faculty"] if "faculty" in offering.keys()
                                  else None)
            fname = safe_filename(res, "pdf")
            # Guarantee uniqueness within the teacher's folder.
            seen = used_per_folder.setdefault(folder, set())
            base, ext = (fname.rsplit(".", 1) + ["pdf"])[:2]
            candidate, n = fname, 2
            while candidate in seen:
                candidate = f"{base}-{n}.{ext}"
                n += 1
            seen.add(candidate)

            buf = io.BytesIO()
            build_pdf_report(res, buf)
            # posixpath-style "/" is the correct ZIP separator on every OS.
            zf.writestr(f"{folder}/{candidate}", buf.getvalue())
    return zip_path


def _merge_pdfs(buffers, out_path):
    """Concatenate several in-memory single-report PDFs into ONE file, page for
    page, with pypdf. Each buffer is a fully-formed report — already even-paged
    (duplex-safe) and already carrying its OWN "Page X of Y" footer — so the
    merged file numbers every report independently and every report still opens
    on the front of a fresh sheet. This is how the by-HOD / batch combined PDFs
    are assembled now, instead of stitching one giant story (which numbered pages
    across the whole file and could not pad or number each report on its own)."""
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter()
    for buf in buffers:
        buf.seek(0)
        for page in PdfReader(buf).pages:
            writer.add_page(page)
    with open(out_path, "wb") as fh:
        writer.write(fh)
    return out_path


def _stub_pdf(text):
    """A one-page placeholder PDF (used when a batch/group has nothing to show)."""
    import io
    buf = io.BytesIO()
    ss = _pdf_styles()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=14 * mm, bottomMargin=14 * mm,
                            leftMargin=15 * mm, rightMargin=15 * mm)
    doc.build([Paragraph(text, ss["Ident"])])
    buf.seek(0)
    return buf


def _one_blank_page_pdf():
    """A single completely-blank A4 page. Used to pad a department divider to an
    even page count so the department's first report opens on a fresh sheet."""
    import io
    buf = io.BytesIO()
    c = _rl_canvas.Canvas(buf, pagesize=A4)
    c.showPage()          # emit exactly one blank page
    c.save()
    buf.seek(0)
    return buf


def _build_divider_pdf(label, count, watermark=None):
    """One department-divider page (big centred label + a one-line count) as its
    own 1-page PDF buffer. It carries NO footer and NO page number — it is a
    separator, not a report. The caller pads it to even with a blank page. A
    TEST-DATA watermark, if any, is drawn so a test batch is obvious."""
    import io
    ss = _pdf_styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=14 * mm, bottomMargin=14 * mm,
                            leftMargin=15 * mm, rightMargin=15 * mm,
                            title="Department divider")
    story = [Spacer(1, 90),
             Paragraph("<b>%s</b>" % (label or "Department"), ss["Title"]),
             Spacer(1, 8),
             Paragraph("%d course report(s) in this department" % count, ss["Ident"])]

    def _wm(canvas, _doc):
        if not watermark:
            return
        canvas.saveState()
        canvas.setFont("Helvetica-Bold", 60)
        canvas.setFillColor(colors.Color(0.85, 0.2, 0.2, alpha=0.16))
        canvas.translate(A4[0] / 2.0, A4[1] / 2.0)
        canvas.rotate(45)
        canvas.drawCentredString(0, 0, watermark)
        canvas.restoreState()

    doc.build(story, onFirstPage=_wm, onLaterPages=_wm)
    buf.seek(0)
    return buf


def build_batch_pdf(results, pdf_path):
    """Bulk PDF: one combined multi-page PDF for the whole batch (spec 11).

    Each report is rendered INDEPENDENTLY by build_pdf_report into an in-memory
    PDF — so it is even-paged (duplex-safe), phantom-page free, and carries its
    OWN per-report "Page X of Y" footer — and the finished report PDFs are then
    concatenated with pypdf (see _merge_pdfs). This replaces the old shared-story
    concatenation, which numbered pages across the whole file and could neither
    pad nor number each report on its own.
    """
    import io
    buffers = []
    for res in results:
        b = io.BytesIO()
        build_pdf_report(res, b)      # even-paged + own footer/numbering
        buffers.append(b)
    if not buffers:
        buffers.append(_stub_pdf("No reports to display."))
    return _merge_pdfs(buffers, pdf_path)


def _append_pdf_story(result, story, ss):
    """Append one offering's full report flowables to a shared `story` list.

    This is the same layout as build_pdf_report, factored so the combined-batch
    PDF can stack many offerings into one document without merging PDF files.
    """
    from reportlab.platypus import PageBreak, Image as RLImage  # local imports
    vm = build_view_model(result)
    # Mark the first page of THIS report so NumberedCanvas.save() can pad the
    # report to an even page count for correct double-sided printing (see the
    # _ReportStart / NumberedCanvas classes above). Must be the very first
    # flowable so it lands on the report's opening page.
    story.append(_ReportStart())
    # TITLE: the college banner image (as seen on all our documents), then just
    # "Course Feedback Report" — no version number, no "SRET" text. The banner
    # lives at app/static/banner.png; if it is somehow missing we fall back to a
    # plain text title so a report is never blank.
    banner_path = os.path.join(Config.BASE_DIR, "static", "banner.png")
    if os.path.exists(banner_path):
        # Banner art is 2000x400 (5:1). Fit it to the content width (~180mm) and
        # keep the aspect ratio so it never distorts.
        banner = RLImage(banner_path, width=180 * mm, height=36 * mm)
        banner.hAlign = "CENTER"
        story.append(banner)
        story.append(Spacer(1, 6))
    story.append(Paragraph("<b>Course Feedback Report</b>", ss["Title"]))
    story.append(Spacer(1, 6))
    h = vm["header"]
    ident_lines = (
        f"<b>ACADEMIC YEAR:</b> {h['academic_year']} &nbsp;&nbsp; "
        f"<b>YEAR / TERM:</b> {h['year_term']}"
        + (f" &nbsp;&nbsp; <b>SECTION:</b> {h['section']}" if h['section'] else "")
        + "<br/>"
        + f"<b>COURSE CODE:</b> {h['course_code']} &nbsp;&nbsp; "
        f"<b>COURSE NAME:</b> {h['course_name']}<br/>"
        f"<b>PROGRAM CODE:</b> {h['dept_code']} &nbsp;&nbsp; "
        f"<b>PROGRAMME:</b> {h['programme']}<br/>"
        f"<b>FACULTY NAME:</b> {h['faculty']}<br/>"
        f"<b>CATEGORY:</b> {h['category']} — {h['category_name']}"
    )
    story.append(Paragraph(ident_lines, ss["Ident"]))
    story.append(Spacer(1, 8))
    box = Table([
        ["No. of Students Feedback Recorded", str(vm["n_recorded"])],
        ["OVERALL SCORE (out of 10)", _fmt(vm["overall"])],
    ], colWidths=[110 * mm, 60 * mm])
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EEF1F4")),
        ("BACKGROUND", (1, 1), (1, 1), colors.HexColor("#0B3D68")),
        ("TEXTCOLOR", (1, 1), (1, 1), colors.white),
        ("FONTSIZE", (1, 1), (1, 1), 16),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#C9CED3")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        # The overall score is the single most important number in the report, so
        # centre it in its blue cell BOTH ways. Without an explicit ALIGN it was
        # left-aligned (hugging the grid line).
        ("ALIGN", (1, 1), (1, 1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        # Vertical fix: VALIGN MIDDLE reserves the 16pt font's descender space, so
        # the big digits sit ~4pt low relative to the label. Asymmetric padding
        # (less on top, more on bottom) raises them to the same optical centre as
        # the label text — measured level to within 0.2pt.
        ("TOPPADDING", (1, 1), (1, 1), 3),
        ("BOTTOMPADDING", (1, 1), (1, 1), 11),
    ]))
    story.append(box)
    story.append(Spacer(1, 10))
    sec_data = [["Section", "Score /10"]]
    for srow in vm["section_rows"]:
        sec_data.append([srow["title"], _fmt(srow["score"])])
    sec_tbl = Table(sec_data, colWidths=[130 * mm, 40 * mm])
    sec_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0B3D68")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#C9CED3")),
        ("ALIGN", (1, 0), (1, -1), "CENTER"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
    ]))
    story.append(sec_tbl)
    story.append(Spacer(1, 12))
    for sec in vm["detail_sections"]:
        if not sec["questions"]:
            continue
        block = [Paragraph(sec["title"], ss["SecTitle"])]
        labels = [lbl for (lbl, _c) in sec["questions"][0]["counts"]]
        # A leading "#" column numbers the questions Q1..Qn; the trailing "Avg"
        # column carries each question's average /10 — the exact numbers that used
        # to be drawn as a bar chart, kept as data after the v3.2 chart removal.
        # HEADER CELLS ARE PARAGRAPHS, not raw strings: a plain string does NOT
        # wrap inside a narrow ReportLab table cell, so long option labels like
        # "Strongly Disagree" used to spill over and overlap the neighbouring
        # columns ("Strongly DisagreAevg"). Wrapping each label in a Paragraph
        # with the centred header style lets it break onto two lines and stay
        # inside its own column.
        header_cells = ([Paragraph("#", ss["Hdr"]), Paragraph("Question", ss["Hdr"])]
                        + [Paragraph(lbl, ss["Hdr"]) for lbl in labels]
                        + [Paragraph("Avg", ss["Hdr"])])
        tdata = [header_cells]
        # A section that scores at section level (Syllabus) leaves per-question
        # averages None; for such a SINGLE-question section, show the section score
        # in the Avg cell instead of a blank. Multi-question sections keep None.
        sec_score = sec.get("score")
        single_q = len(sec["questions"]) == 1
        for idx, qb in enumerate(sec["questions"], start=1):
            qn = f"Q{idx}"
            disp_avg = qb["average"]
            if disp_avg is None and single_q:
                disp_avg = sec_score
            count_map = dict(qb["counts"])
            counts = [count_map.get(lbl, 0) for lbl in labels]
            tdata.append([qn, Paragraph(qb["text"], ss["Ident"])]
                         + [str(c) for c in counts] + [_fmt(disp_avg)])
        n_opt = len(labels)
        num_w = 8 * mm
        # Widths total unchanged (# 8 + question 70 + options 100 + avg 14 = 192).
        tbl = Table(
            tdata,
            colWidths=[num_w, 70 * mm] + [(100 * mm) / max(n_opt, 1)] * n_opt + [14 * mm])
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF1F4")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),   # bold Q# so it stands out
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C9CED3")),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("ALIGN", (0, 1), (0, -1), "CENTER"),              # centre the Q# column
            ("ALIGN", (2, 1), (-1, -1), "CENTER"),             # centre the count/avg columns
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        block.append(tbl)
        block.append(Spacer(1, 10))
        story.append(KeepTogether(block))

    # ------------------------------------------------------------------
    # CLOSING SECTION — OPEN-ENDED STUDENT COMMENTS  (restored Sept 2026)
    # ------------------------------------------------------------------
    # The student free-text comment box was re-enabled this cycle, and the Dean
    # asked that whatever students write be shown to the HOD verbatim. The scoring
    # stack collects these into result["open_comments"] WITHOUT scoring them (a
    # free-text question carries no /10 weight), build_view_model passes them on
    # as vm["open_comments"], and THIS is where they finally reach paper — always
    # as the LAST section of every report, so the qualitative feedback always sits
    # in the same place, right after the numeric section tables.
    #
    # This block is deliberately allowed to FLOW across page boundaries: a popular
    # course can collect many comments, and truncating them would throw feedback
    # away. Any overflow onto an extra page is then absorbed by the per-report
    # even-page padding in NumberedCanvas.save(), so the NEXT teacher's report
    # still begins on the front of a fresh sheet when the batch is printed duplex.
    comments = [c for c in (vm.get("open_comments") or []) if str(c).strip()]
    story.append(Spacer(1, 6))
    _cmt_title = Paragraph("Open-Ended Student Comments", ss["SecTitle"])
    if comments:
        # Number the comments so a HOD can cite "comment 3" in an ATR note, and
        # run each through _cmt_markup() so a stray & / < / > in a student's words
        # cannot break the report. Keep the heading glued to at least the first
        # comment so it is never stranded alone at the foot of a page.
        _first = Paragraph("1. " + _cmt_markup(comments[0]), ss["Cmt"])
        story.append(KeepTogether([_cmt_title, Spacer(1, 4), _first]))
        for _i, _c in enumerate(comments[1:], start=2):
            # Small gap BEFORE each subsequent comment, never AFTER the last one:
            # a trailing spacer at the foot of a full page can spill onto a new,
            # otherwise-empty page — which would then be miscounted and trigger a
            # needless duplex pad. Keeping the gap leading avoids that entirely.
            story.append(Spacer(1, 2))
            story.append(Paragraph("%d. %s" % (_i, _cmt_markup(_c)), ss["Cmt"]))
    else:
        # Always show the section (even with nothing in it) so a reader can tell
        # "no comments were left" apart from "the comments were dropped".
        story.append(KeepTogether([
            _cmt_title, Spacer(1, 4),
            Paragraph("<i>No written comments were submitted for this course.</i>",
                      ss["Cmt"]),
        ]))


# ============================================================================
# SECTION 5 — HOD-GROUPED BULK OUTPUTS  (Sept 2026, the Dean's ask)
# ----------------------------------------------------------------------------
# The college wants every bulk report download organised BY THE HOD who owns the
# staff — i.e. grouped by the faculty's home department (rbac.effective_dept),
# because that is exactly the person each teacher reports to. Two shapes are
# needed, both fed by the SAME pre-scored, pre-grouped `groups` structure that
# hod_bundles.build_department_groups() produces, so admin and leader downloads
# can never diverge:
#
#   groups = [
#     { "label":  "E01 — CSE-AIML",        # dept code + short name (folder/divider)
#       "results": [ <scored-offering dict>, ... ] },  # every delivery in that dept
#     { "label":  "E05 — CSE-Cyber", "results": [ ... ] },
#     ...
#   ]                                       # already ordered by dept code
#
#   * build_grouped_combined_pdf  -> ONE PDF, a divider page per department then
#     that department's course reports. For a single-department HOD this is just
#     their one section; for the Vice-Dean/Dean it is their whole scope with a
#     clear divider starting each department (the "whole scope, divider per dept"
#     choice, Sept 2026).
#   * build_hod_staff_foldered_pdf_zip -> a .zip nested TWO levels deep:
#         <Dept — Name>/<Staff Name>/<course report>.pdf
#     i.e. one folder per HOD's department, and inside it one sub-folder per staff
#     member with all their course PDFs. (A single-department caller can instead
#     use build_staff_foldered_pdf_zip above for a flat <Staff>/<report>.pdf zip.)
#
# Both reuse the frozen single-report builder build_pdf_report, so a file here is
# byte-for-byte the individually-downloaded report — no new scoring, no Excel.
# ============================================================================

def build_grouped_combined_pdf(groups, pdf_path):
    """One combined PDF, a divider page introducing each department, then that
    department's reports.

    Each report is rendered INDEPENDENTLY by build_pdf_report (even-paged, own
    per-report footer + "Page X of Y"); each department divider is its own 1-page
    PDF padded to an even 2 pages with a blank sheet. The pieces are concatenated
    with pypdf (see _merge_pdfs), so every teacher's report and every department
    opens on the front of a fresh duplex sheet and numbers itself. Empty groups
    (a department with no scorable responses) are skipped.
    """
    import io
    buffers = []
    for grp in groups:
        results = grp.get("results") or []
        if not results:
            continue
        # Inherit the TEST-DATA watermark from the first result that carries one.
        watermark = None
        for r in results:
            if hasattr(r, "get") and r.get("watermark"):
                watermark = r.get("watermark")
                break
        # Department divider (1 page) + a blank page -> even, so this department
        # (and its first report) opens on the front of a fresh sheet.
        buffers.append(_build_divider_pdf(grp.get("label") or "Department",
                                          len(results), watermark))
        buffers.append(_one_blank_page_pdf())
        # Then the department's reports, each its own even-paged, self-numbered PDF.
        for res in results:
            b = io.BytesIO()
            build_pdf_report(res, b)
            buffers.append(b)
    if not buffers:                     # nothing at all to render -> a stub page
        buffers.append(_stub_pdf("No reports to display."))
    return _merge_pdfs(buffers, pdf_path)


def build_hod_staff_foldered_pdf_zip(groups, zip_path):
    """A .zip nested <Dept — Name>/<Staff Name>/<report>.pdf (Sept 2026).

    Used for the Vice-Dean/Dean download, where each HOD's department is a top
    folder holding one sub-folder per staff member. `groups` is the SECTION 5
    structure. Names are sanitised by _safe_folder (so a stray path separator can
    never escape the archive), and filenames are de-duplicated within each staff
    sub-folder as a belt-and-suspenders (safe_filename already includes the id).
    """
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        used_per_leaf = {}                       # (dept, staff) -> set(filenames)
        for grp in groups:
            dept_folder = _safe_folder(grp.get("label") or "Department")
            for res in (grp.get("results") or []):
                offering = res["offering"]
                staff = _safe_folder(offering["faculty"] if "faculty" in offering.keys()
                                     else None)
                fname = safe_filename(res, "pdf")
                seen = used_per_leaf.setdefault((dept_folder, staff), set())
                base, ext = (fname.rsplit(".", 1) + ["pdf"])[:2]
                candidate, n = fname, 2
                while candidate in seen:
                    candidate = f"{base}-{n}.{ext}"
                    n += 1
                seen.add(candidate)

                buf = io.BytesIO()
                build_pdf_report(res, buf)
                # posixpath "/" is the correct ZIP separator on every OS.
                zf.writestr(f"{dept_folder}/{staff}/{candidate}", buf.getvalue())
    return zip_path
