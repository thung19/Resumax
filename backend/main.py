"""Resumax Backend - FastAPI Server.

Phase 1: DOCX upload, parsing, and inspection endpoints.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response

from pydantic import BaseModel

load_dotenv(Path(__file__).resolve().parent / ".env")

from backend.analysis.formatting_detector import FormattingDetector
from backend.analysis.job_analyzer import JobAnalyzer
from backend.analysis.semantic_classifier import SemanticClassifier
from backend.importers.docx_importer import DocxImporter
from backend.models.job_description import JobAnalysis
from backend.models.resume_ir import ResumeIR
from backend.models.tailoring import TailoringResult
from backend.renderers.html_renderer import HtmlRenderer
from backend.renderers.docx_renderer import DocxRenderer
from backend.renderers.pdf_renderer import PdfRenderer
from backend.renderers.text_renderer import TextRenderer
from backend.tailoring.page_fitter import PageFitter
from backend.services.resume_bank_service import generate_bank_from_ir, save_bank, load_bank
from backend.services.tailoring_service import TailoringService

app = FastAPI(title="Resumax", version="0.1.0")


def _estimate_line_chars(ir: ResumeIR) -> int:
    """Estimate how many characters fit on one line based on the resume's
    actual page layout and font. Uses the longest existing bullet that
    visually fits on one line as the ground truth."""
    # Method 1: Use existing bullet lengths as ground truth
    # The original resume's bullets all fit on one line, so the max
    # existing bullet length IS the single-line capacity.
    max_existing = 0
    for section in ir.content.sections:
        for entry in section.experience_entries:
            for b in entry.bullets:
                max_existing = max(max_existing, len(b.text))
        for entry in section.project_entries:
            for b in entry.bullets:
                max_existing = max(max_existing, len(b.text))

    if max_existing > 0:
        return max_existing

    # Method 2: Estimate from page geometry
    page = ir.layout.page
    content_width_in = page.width_in - page.margin_left_in - page.margin_right_in
    # Conservative estimate: ~16 chars per inch at 10pt serif
    return int(content_width_in * 16)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory store for parsed resumes (Phase 1 only)
_store: dict[str, ResumeIR] = {}

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
GENERATED_DIR = DATA_DIR / "generated"


@app.post("/upload")
async def upload_resume(file: UploadFile = File(...)):
    """Upload a .docx resume, parse it, and return the Resume IR."""
    if not file.filename:
        raise HTTPException(400, "No filename provided")

    ext = Path(file.filename).suffix.lower()
    if ext not in (".docx",):
        raise HTTPException(
            400,
            f"Unsupported format: {ext}. Phase 1 supports .docx only.",
        )

    file_bytes = await file.read()

    # Save uploaded file
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    resume_id = uuid.uuid4().hex[:12]
    upload_path = UPLOADS_DIR / f"{resume_id}{ext}"
    upload_path.write_bytes(file_bytes)

    # Import
    importer = DocxImporter(file_bytes=file_bytes)
    ir = importer.import_resume()
    ir.source_filename = file.filename

    # Run formatting detection for enrichment
    # Re-parse paragraphs for the detector
    import zipfile
    from io import BytesIO
    from docx.oxml.ns import qn
    from lxml import etree
    from backend.importers.docx_importer import (
        _extract_paragraph_formatting,
        _parse_numbering,
    )

    numbering_map = _parse_numbering(file_bytes)
    with zipfile.ZipFile(BytesIO(file_bytes), "r") as z:
        doc_xml = z.read("word/document.xml")
    root = etree.fromstring(doc_xml)
    body = root.find(qn("w:body"))
    paragraphs = []
    if body is not None:
        for p in body.findall(qn("w:p")):
            paragraphs.append(_extract_paragraph_formatting(p, numbering_map))

    detector = FormattingDetector(paragraphs)
    detection_result = detector.detect()
    ir.layout = detector.refine_layout(ir.layout)

    # Run semantic classification
    classifier = SemanticClassifier()
    ir.content = classifier.reclassify(ir.content)

    # Store
    _store[resume_id] = ir

    # Save IR as JSON
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    ir_path = GENERATED_DIR / f"{resume_id}_ir.json"
    ir_path.write_text(ir.model_dump_json(indent=2))

    return {
        "resume_id": resume_id,
        "filename": file.filename,
        "sections_found": len(ir.content.sections),
        "styles_detected": list(ir.layout.styles.keys()),
        "diagnostics": [d.model_dump() for d in ir.diagnostics],
        "formatting_summary": detection_result["style_summary"],
    }


def _load_ir(resume_id: str) -> ResumeIR:
    """Load a ResumeIR from memory or disk."""
    ir = _store.get(resume_id)
    if ir is not None:
        return ir
    ir_path = GENERATED_DIR / f"{resume_id}_ir.json"
    if ir_path.exists():
        ir = ResumeIR.model_validate_json(ir_path.read_text())
        _store[resume_id] = ir
        return ir
    raise HTTPException(404, "Resume not found")


@app.get("/inspect/{resume_id}")
async def inspect_resume(resume_id: str):
    """Return the full Resume IR for inspection."""
    return _load_ir(resume_id).model_dump()


@app.get("/inspect/{resume_id}/content")
async def inspect_content(resume_id: str):
    """Return only the Content Schema."""
    return _load_ir(resume_id).content.model_dump()


@app.get("/inspect/{resume_id}/layout")
async def inspect_layout(resume_id: str):
    """Return only the Layout Schema."""
    return _load_ir(resume_id).layout.model_dump()


@app.get("/preview/{resume_id}", response_class=HTMLResponse)
async def preview_resume(resume_id: str):
    """Return an HTML preview of the resume."""
    ir = _load_ir(resume_id)
    renderer = HtmlRenderer(ir)
    return renderer.render()


@app.get("/export/{resume_id}/docx")
async def export_docx(resume_id: str):
    """Export the resume as a .docx file."""
    ir = _load_ir(resume_id)
    renderer = DocxRenderer(ir)
    docx_bytes = renderer.render()

    filename = ir.source_filename or "resume.docx"
    if not filename.endswith(".docx"):
        filename = filename.rsplit(".", 1)[0] + "_tailored.docx"
    else:
        filename = filename.rsplit(".", 1)[0] + "_tailored.docx"

    # Save to generated dir
    export_path = GENERATED_DIR / f"{resume_id}_export.docx"
    export_path.write_bytes(docx_bytes)

    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/export/{resume_id}/pdf")
async def export_pdf(resume_id: str):
    """Export the resume as a PDF file."""
    ir = _load_ir(resume_id)
    renderer = PdfRenderer(ir)
    pdf_bytes = renderer.render()
    info = renderer.get_overflow_info()

    filename = (ir.source_filename or "resume").rsplit(".", 1)[0] + ".pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Page-Count": str(info.page_count),
            "X-Overflow": str(info.overflow),
        },
    )


@app.get("/export/{resume_id}/txt")
async def export_txt(resume_id: str):
    """Export the resume as plain text."""
    ir = _load_ir(resume_id)
    renderer = TextRenderer(ir)
    text = renderer.render()

    filename = (ir.source_filename or "resume").rsplit(".", 1)[0] + ".txt"

    return Response(
        content=text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/export/{resume_id}/tailored/pdf")
async def export_tailored_pdf(resume_id: str):
    """Export the tailored resume as PDF."""
    ir = _tailored_ir_store.get(resume_id)
    if ir is None:
        raise HTTPException(404, "No tailored resume found")

    renderer = PdfRenderer(ir)
    pdf_bytes = renderer.render()
    info = renderer.get_overflow_info()

    filename = (ir.source_filename or "resume").rsplit(".", 1)[0] + "_tailored.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Page-Count": str(info.page_count),
            "X-Overflow": str(info.overflow),
        },
    )


@app.get("/export/{resume_id}/tailored/txt")
async def export_tailored_txt(resume_id: str):
    """Export the tailored resume as plain text."""
    ir = _tailored_ir_store.get(resume_id)
    if ir is None:
        raise HTTPException(404, "No tailored resume found")

    renderer = TextRenderer(ir)
    text = renderer.render()

    return Response(
        content=text,
        media_type="text/plain; charset=utf-8",
    )


@app.post("/fit/{resume_id}")
async def fit_page(resume_id: str):
    """Run the page fitter on the tailored resume."""
    ir = _tailored_ir_store.get(resume_id)
    if ir is None:
        ir = _load_ir(resume_id)

    tailoring = _tailoring_store.get(resume_id)

    fitter = PageFitter(ir, target_pages=1, tailoring_result=tailoring)
    fitted_ir, report = fitter.fit()

    _tailored_ir_store[resume_id] = fitted_ir

    return {
        "fits": report.fits,
        "page_count": report.page_count,
        "actions_taken": report.actions_taken,
        "bullets_shortened": report.bullets_shortened,
        "bullets_removed": report.bullets_removed,
        "spacing_adjusted": report.spacing_adjusted,
        "font_adjusted": report.font_adjusted,
    }


@app.get("/inspect/{resume_id}/formatting")
async def inspect_formatting(resume_id: str):
    """Return detected formatting patterns (requires re-parsing the uploaded file)."""
    ir = _store.get(resume_id)
    if ir is None:
        raise HTTPException(404, "Resume not found (must be in memory for formatting analysis)")

    upload_path = UPLOADS_DIR / f"{resume_id}.docx"
    if not upload_path.exists():
        raise HTTPException(404, "Original upload file not found")

    import zipfile
    from io import BytesIO
    from docx.oxml.ns import qn
    from lxml import etree
    from backend.importers.docx_importer import (
        _extract_paragraph_formatting,
        _parse_numbering,
    )

    file_bytes = upload_path.read_bytes()
    numbering_map = _parse_numbering(file_bytes)
    with zipfile.ZipFile(BytesIO(file_bytes), "r") as z:
        doc_xml = z.read("word/document.xml")
    root = etree.fromstring(doc_xml)
    body = root.find(qn("w:body"))

    paragraphs = []
    if body is not None:
        for p in body.findall(qn("w:p")):
            paragraphs.append(_extract_paragraph_formatting(p, numbering_map))

    detector = FormattingDetector(paragraphs)
    result = detector.detect()

    # Serialize patterns
    patterns_out = {}
    for role, pattern in result["patterns"].items():
        patterns_out[role] = {
            "role": pattern.role,
            "count": pattern.count,
            "examples": pattern.example_texts,
            "signature": {
                "font": pattern.signature.font_family,
                "size_pt": pattern.signature.font_size_pt,
                "bold": pattern.signature.bold,
                "italic": pattern.signature.italic,
                "alignment": pattern.signature.alignment,
                "has_bullet": pattern.signature.has_bullet,
                "has_tabs": pattern.signature.has_tabs,
                "all_caps": pattern.signature.is_all_caps,
            },
        }

    return {
        "patterns": patterns_out,
        "relationships": result["relationships"],
        "summary": result["style_summary"],
    }


# --- Phase 4: Tailoring endpoints ---

# In-memory stores for JD analysis and tailoring results
_jd_store: dict[str, JobAnalysis] = {}
_tailoring_store: dict[str, TailoringResult] = {}
_tailored_ir_store: dict[str, ResumeIR] = {}


class AnalyzeJDRequest(BaseModel):
    jd_text: str
    resume_id: str | None = None


class TailorRequest(BaseModel):
    jd_text: str
    use_llm: bool = True
    max_bullets_per_entry: int = 4
    enforce_one_page: bool = True
    enforce_single_line_bullets: bool = True
    max_bullet_chars: int = 0  # 0 = auto-detect from resume layout


class AcceptRejectRequest(BaseModel):
    bullet_id: str
    accepted: bool
    resolved: bool = True  # set to False to undo/unresolve


@app.post("/analyze-jd")
async def analyze_jd(req: AnalyzeJDRequest):
    """Analyze a job description using hybrid LLM + deterministic analysis."""
    use_llm = bool(os.environ.get("ANTHROPIC_API_KEY"))
    analyzer = JobAnalyzer(use_llm=use_llm)
    analysis = analyzer.analyze(req.jd_text)

    jd_id = req.resume_id or uuid.uuid4().hex[:12]
    _jd_store[jd_id] = analysis

    return {
        "jd_id": jd_id,
        "job_title": analysis.job_title,
        "company": analysis.company,
        "location": analysis.location,
        "job_type": analysis.job_type,
        "analysis_method": "hybrid (LLM + deterministic)" if analyzer.llm_used else "deterministic only",
        "llm_error": analyzer.llm_error,
        "programming_languages": [s.model_dump() for s in analysis.programming_languages],
        "frameworks": [s.model_dump() for s in analysis.frameworks],
        "databases": [s.model_dump() for s in analysis.databases],
        "infrastructure": [s.model_dump() for s in analysis.infrastructure],
        "tools": [s.model_dump() for s in analysis.tools],
        "methodologies": [s.model_dump() for s in analysis.methodologies],
        "domain_knowledge": [s.model_dump() for s in analysis.domain_knowledge],
        "soft_skills": [s.model_dump() for s in analysis.soft_skills],
        "responsibilities": [r.model_dump() for r in analysis.responsibilities],
        "required_skills": [s.model_dump() for s in analysis.required_skills],
        "preferred_skills": [s.model_dump() for s in analysis.preferred_skills],
        "ats_phrases": analysis.ats_phrases[:30],
        "all_keywords": analysis.all_keywords()[:40],
    }


@app.post("/tailor/{resume_id}")
async def tailor_resume(resume_id: str, req: TailorRequest):
    """Tailor a resume to a job description."""
    ir = _load_ir(resume_id)

    # Analyze JD
    service = TailoringService(use_llm=req.use_llm)
    jd = service.analyze_jd(req.jd_text)
    _jd_store[resume_id] = jd

    # Generate bank
    bank = load_bank(resume_id)
    if bank is None:
        bank = generate_bank_from_ir(ir)
        save_bank(bank, resume_id)

    # Auto-detect max bullet chars from the resume's actual formatting
    max_chars = req.max_bullet_chars
    if max_chars == 0:
        max_chars = _estimate_line_chars(ir)

    # Run tailoring
    result = service.tailor(
        ir, jd, bank,
        max_bullets_per_entry=req.max_bullets_per_entry,
        enforce_single_line=req.enforce_single_line_bullets,
        max_bullet_chars=max_chars,
    )
    result.resume_id = resume_id

    # Store result
    _tailoring_store[resume_id] = result

    # Apply tailoring to produce a new IR
    tailored_ir = service.apply_tailoring(ir, result)

    # Enforce one-page if requested
    fit_report = None
    if req.enforce_one_page:
        fitter = PageFitter(tailored_ir, target_pages=1, tailoring_result=result)
        tailored_ir, report = fitter.fit()
        fit_report = {
            "fits": report.fits,
            "page_count": report.page_count,
            "actions_taken": report.actions_taken,
        }

    _tailored_ir_store[resume_id] = tailored_ir

    # Save tailored IR
    ir_path = GENERATED_DIR / f"{resume_id}_tailored_ir.json"
    ir_path.write_text(tailored_ir.model_dump_json(indent=2))

    resp = result.model_dump()
    resp["fit_report"] = fit_report
    return resp


@app.get("/tailor/{resume_id}/result")
async def get_tailoring_result(resume_id: str):
    """Get the tailoring result."""
    result = _tailoring_store.get(resume_id)
    if result is None:
        raise HTTPException(404, "No tailoring result found for this resume")
    return result.model_dump()


@app.post("/tailor/{resume_id}/accept")
async def accept_reject_bullet(resume_id: str, req: AcceptRejectRequest):
    """Accept or reject a tailored bullet."""
    result = _tailoring_store.get(resume_id)
    if result is None:
        raise HTTPException(404, "No tailoring result found")

    found = False
    for change in result.bullet_changes:
        if change.bullet_id == req.bullet_id:
            change.accepted = req.accepted
            change.resolved = req.resolved
            found = True
            break
    if not found:
        raise HTTPException(404, f"Bullet {req.bullet_id} not found")

    # Re-apply tailoring with updated accepts/rejects
    ir = _load_ir(resume_id)
    service = TailoringService(use_llm=False)
    tailored_ir = service.apply_tailoring(ir, result)
    _tailored_ir_store[resume_id] = tailored_ir

    # Save updated tailored IR
    ir_path = GENERATED_DIR / f"{resume_id}_tailored_ir.json"
    ir_path.write_text(tailored_ir.model_dump_json(indent=2))

    return result.model_dump()


@app.get("/preview/{resume_id}/tailored", response_class=HTMLResponse)
async def preview_tailored(resume_id: str, diff: bool = False):
    """Preview the tailored resume. Pass ?diff=true for inline change highlighting."""
    ir = _tailored_ir_store.get(resume_id)
    if ir is None:
        raise HTTPException(404, "No tailored resume found")
    tailoring = _tailoring_store.get(resume_id) if diff else None
    renderer = HtmlRenderer(ir, diff_changes=tailoring)
    return renderer.render()


@app.get("/export/{resume_id}/tailored/docx")
async def export_tailored_docx(resume_id: str):
    """Export the tailored resume as .docx."""
    ir = _tailored_ir_store.get(resume_id)
    if ir is None:
        raise HTTPException(404, "No tailored resume found")

    renderer = DocxRenderer(ir)
    docx_bytes = renderer.render()

    orig_name = ir.source_filename or "resume"
    filename = orig_name.rsplit(".", 1)[0] + "_tailored.docx"

    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Formatting controls ---


class LayoutSettingsUpdate(BaseModel):
    # Page margins (inches)
    margin_top: float | None = None
    margin_bottom: float | None = None
    margin_left: float | None = None
    margin_right: float | None = None
    # Font
    font_family: str | None = None
    # Font sizes (points)
    name_size_pt: float | None = None
    heading_size_pt: float | None = None
    body_size_pt: float | None = None
    # Spacing
    line_spacing: float | None = None  # multiplier (1.0 = single, 1.15, 1.5, 2.0)
    spacer_size_pt: float | None = None  # gap between sections/entries
    section_gap_pt: float | None = None  # spacer before section headings
    entry_gap_pt: float | None = None  # spacer between entries
    # Bullet
    bullet_symbol: str | None = None
    bullet_indent_in: float | None = None


@app.get("/layout/{resume_id}/settings")
async def get_layout_settings(resume_id: str):
    """Get current layout settings for editing."""
    ir = _load_ir(resume_id)
    layout = ir.layout
    page = layout.page

    # Extract current values from styles
    name_style = layout.styles.get("name")
    heading_style = layout.styles.get("section_heading_with_rule") or layout.styles.get("section_heading")
    bullet_style = layout.styles.get("bullet")

    # Determine spacer sizes from elements
    spacer_sizes = []
    for el in layout.elements:
        if el.element_type.value == "spacer" and el.spacer_size_half_pt:
            spacer_sizes.append(el.spacer_size_half_pt / 2.0)

    # Font family
    font = "Garamond"
    for role in ["name", "entry_header", "bullet"]:
        s = layout.styles.get(role)
        if s and s.font.family and s.font.family.lower() not in ("arial", "minorhansi"):
            font = s.font.family
            break

    return {
        "margins": {
            "top": page.margin_top_in,
            "bottom": page.margin_bottom_in,
            "left": page.margin_left_in,
            "right": page.margin_right_in,
        },
        "font_family": font,
        "sizes": {
            "name_pt": name_style.font.size_pt if name_style else 26.0,
            "heading_pt": heading_style.font.size_pt if heading_style else 12.0,
            "body_pt": bullet_style.font.size_pt if bullet_style else 10.0,
        },
        "spacing": {
            "line_spacing": 1.0,
            "spacer_sizes_pt": sorted(set(spacer_sizes)) if spacer_sizes else [5.0],
        },
        "page": {
            "width": page.width_in,
            "height": page.height_in,
        },
        "element_count": len(layout.elements),
        "hyperlinks": layout.hyperlinks,
    }


@app.post("/layout/{resume_id}/settings")
async def update_layout_settings(resume_id: str, update: LayoutSettingsUpdate):
    """Update layout settings. Changes are applied to the stored IR."""
    ir = _load_ir(resume_id)
    layout = ir.layout

    # Update margins
    if update.margin_top is not None:
        layout.page.margin_top_in = update.margin_top
    if update.margin_bottom is not None:
        layout.page.margin_bottom_in = update.margin_bottom
    if update.margin_left is not None:
        layout.page.margin_left_in = update.margin_left
    if update.margin_right is not None:
        layout.page.margin_right_in = update.margin_right

    # Update font family across all styles and elements
    if update.font_family is not None:
        for style in layout.styles.values():
            style.font.family = update.font_family
        for el in layout.elements:
            for run in el.paragraph_format.runs:
                if run.font_family:
                    run.font_family = update.font_family

    # Update font sizes
    if update.name_size_pt is not None:
        s = layout.styles.get("name")
        if s:
            s.font.size_pt = update.name_size_pt
        for el in layout.elements:
            if el.element_type.value == "name":
                for run in el.paragraph_format.runs:
                    run.font_size_half_pt = int(update.name_size_pt * 2)

    if update.heading_size_pt is not None:
        for key in ["section_heading", "section_heading_with_rule"]:
            s = layout.styles.get(key)
            if s:
                s.font.size_pt = update.heading_size_pt
        for el in layout.elements:
            if el.element_type.value == "section_heading":
                for run in el.paragraph_format.runs:
                    if run.font_size_half_pt:
                        run.font_size_half_pt = int(update.heading_size_pt * 2)

    if update.body_size_pt is not None:
        half = int(update.body_size_pt * 2)
        for key in ["bullet", "entry_header", "entry_subheader", "skills_row"]:
            s = layout.styles.get(key)
            if s:
                s.font.size_pt = update.body_size_pt

    # Update line spacing
    if update.line_spacing is not None:
        line_val = int(update.line_spacing * 240)
        for el in layout.elements:
            pf = el.paragraph_format
            if pf.line_value is not None:
                pf.line_value = line_val

    # Update spacer sizes
    if update.spacer_size_pt is not None:
        half = int(update.spacer_size_pt * 2)
        for el in layout.elements:
            if el.element_type.value == "spacer" and el.spacer_size_half_pt:
                el.spacer_size_half_pt = half
                for run in el.paragraph_format.runs:
                    if run.font_size_half_pt:
                        run.font_size_half_pt = half

    # Save updated IR
    _store[resume_id] = ir
    ir_path = GENERATED_DIR / f"{resume_id}_ir.json"
    ir_path.write_text(ir.model_dump_json(indent=2))

    return {"status": "ok", "updated": update.model_dump(exclude_none=True)}


@app.get("/health")
async def health():
    return {"status": "ok", "phase": 5}
