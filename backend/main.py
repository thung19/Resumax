"""Resumax Backend - FastAPI Server.

Phase 1: DOCX upload, parsing, and inspection endpoints.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

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

# --- Security constants ---
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_JD_CHARS = 50_000
MAX_EDIT_MESSAGE_CHARS = 5_000
RESUME_ID_PATTERN = re.compile(r"^[a-f0-9]{12}$")
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 20  # max LLM-calling requests per window


def _validate_resume_id(resume_id: str) -> None:
    """Validate resume_id is a safe hex string."""
    if not RESUME_ID_PATTERN.match(resume_id):
        raise HTTPException(400, "Invalid resume ID format")


# --- Rate limiter (in-memory, per-IP) ---
_rate_store: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(request: Request) -> None:
    """Simple sliding-window rate limiter for LLM endpoints."""
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW

    # Prune old entries
    _rate_store[client_ip] = [
        t for t in _rate_store[client_ip] if t > window_start
    ]

    if len(_rate_store[client_ip]) >= RATE_LIMIT_MAX:
        raise HTTPException(429, "Rate limit exceeded. Try again shortly.")

    _rate_store[client_ip].append(now)


# --- Global exception handler (prevent secret leakage) ---
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch unhandled exceptions to prevent internal details leaking."""
    logger.error(f"Unhandled error on {request.url.path}: {type(exc).__name__}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


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

_allowed_origins = os.environ.get(
    "CORS_ORIGINS", "http://localhost:3000,http://localhost:3001"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
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

    # Sanitize filename — strip path components to prevent traversal
    safe_name = Path(file.filename).name
    ext = Path(safe_name).suffix.lower()
    if ext not in (".docx",):
        raise HTTPException(400, "Unsupported format. Only .docx is accepted.")

    file_bytes = await file.read()

    # Validate file size
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, f"File too large. Maximum size is {MAX_UPLOAD_BYTES // (1024*1024)} MB.")

    # Validate DOCX magic bytes (DOCX is a ZIP file: starts with PK\x03\x04)
    if len(file_bytes) < 4 or file_bytes[:4] != b"PK\x03\x04":
        raise HTTPException(400, "Invalid file. Expected a .docx document.")

    # Save uploaded file
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    resume_id = uuid.uuid4().hex[:12]
    upload_path = UPLOADS_DIR / f"{resume_id}{ext}"
    upload_path.write_bytes(file_bytes)

    # Import
    importer = DocxImporter(file_bytes=file_bytes)
    ir = importer.import_resume()
    ir.source_filename = safe_name

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
    _validate_resume_id(resume_id)
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
    jd_text: str = Field(..., max_length=MAX_JD_CHARS)
    resume_id: str | None = None


class TailorRequest(BaseModel):
    jd_text: str = Field(..., max_length=MAX_JD_CHARS)
    use_llm: bool = True
    max_bullets_per_entry: int = Field(default=4, ge=1, le=10)
    enforce_one_page: bool = True
    one_line_bullets: bool = True
    enforce_single_line_bullets: bool = False
    max_bullet_chars: int = Field(default=0, ge=0, le=500)


class AcceptRejectRequest(BaseModel):
    bullet_id: str = Field(..., max_length=50)
    accepted: bool
    resolved: bool = True

    @field_validator("bullet_id")
    @classmethod
    def validate_bullet_id(cls, v: str) -> str:
        if not re.match(r"^[a-zA-Z0-9_-]+$", v):
            raise ValueError("Invalid bullet_id format")
        return v


class SkillChangeRequest(BaseModel):
    change_type: str = Field(..., max_length=20)
    category: str = Field(..., max_length=200)
    skill: str = Field(default="", max_length=200)
    accepted: bool

    @field_validator("change_type")
    @classmethod
    def validate_change_type(cls, v: str) -> str:
        if v not in ("reorder", "addition"):
            raise ValueError("change_type must be 'reorder' or 'addition'")
        return v


class FreeformEditRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=MAX_EDIT_MESSAGE_CHARS)


class SkillBatchAcceptRequest(BaseModel):
    accepted: bool = True


@app.post("/analyze-jd")
async def analyze_jd(req: AnalyzeJDRequest, request: Request):
    """Analyze a job description using hybrid LLM + deterministic analysis."""
    _check_rate_limit(request)
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
async def tailor_resume(resume_id: str, req: TailorRequest, request: Request):
    """Tailor a resume to a job description."""
    _check_rate_limit(request)
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
        one_line_bullets=req.one_line_bullets,
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


def _recalculate_coverage(resume_id: str, result: TailoringResult, ir: ResumeIR):
    """Recalculate coverage stats after tailoring changes.

    When bullets are accepted/rejected, the resume content changes, so we need
    to re-run the matcher to get updated coverage percentages.
    """
    jd = _jd_store.get(resume_id)
    if jd is None:
        # No JD available — coverage can't be recalculated
        return

    try:
        from backend.tailoring.matcher import Matcher

        # Clear previous coverage
        result.keyword_coverage = []

        # Re-run matcher with updated IR
        matcher = Matcher(jd, ir.content, None)  # No resume bank needed for matching
        match_result = matcher.match()

        # Update coverage percentages from matcher
        result.required_skill_coverage = match_result.required_coverage
        result.technical_keyword_coverage = match_result.technical_coverage
        result.responsibility_coverage = match_result.responsibility_coverage

        # Rebuild keyword coverage list
        all_jd_keywords = jd.all_keywords()
        resume_text = " ".join(
            c.tailored_text.lower()
            for c in result.bullet_changes
            if c.action != "remove"
        )
        for section in ir.content.sections:
            for cat in section.skill_categories:
                resume_text += " " + " ".join(s.lower() for s in cat.skills)

        for kw in all_jd_keywords[:30]:
            skill_info = next(
                (s for s in jd.all_skills_flat()
                 if s.name.lower() == kw.lower()),
                None,
            )
            importance = skill_info.importance if skill_info else 0.3

            if kw.lower() in resume_text:
                orig_text = " ".join(
                    c.original_text.lower() for c in result.bullet_changes
                )
                for section in ir.content.sections:
                    for cat in section.skill_categories:
                        orig_text += " " + " ".join(s.lower() for s in cat.skills)
                status = "matched" if kw.lower() in orig_text else "added"
                source = "present in resume" if status == "matched" else "added via rewrite"
            else:
                status = "missing"
                source = "not in resume bank"

            from backend.models.tailoring import KeywordCoverage
            result.keyword_coverage.append(KeywordCoverage(
                keyword=kw,
                importance=importance,
                status=status,
                source=source,
            ))
    except Exception as e:
        logger.warning(f"Failed to recalculate coverage for {resume_id}: {e}")


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

    # Recalculate coverage stats based on updated resume
    _recalculate_coverage(resume_id, result, tailored_ir)

    # Return result with updated coverage
    response = result.model_dump()
    return response


@app.post("/tailor/{resume_id}/skill")
async def accept_reject_skill(resume_id: str, req: SkillChangeRequest):
    """Accept or reject a skill change (reorder or addition)."""
    result = _tailoring_store.get(resume_id)
    if result is None:
        raise HTTPException(404, "No tailoring result found")

    if req.change_type == "reorder":
        result.reorder_accepted[req.category] = req.accepted
    elif req.change_type == "addition":
        key = f"{req.category}:{req.skill}"
        result.additions_accepted[key] = req.accepted

    # Re-apply tailoring
    ir = _load_ir(resume_id)
    service = TailoringService(use_llm=False)
    tailored_ir = service.apply_tailoring(ir, result)
    _tailored_ir_store[resume_id] = tailored_ir

    # Recalculate coverage stats based on updated resume
    _recalculate_coverage(resume_id, result, tailored_ir)

    # Return result with updated coverage
    response = result.model_dump()
    return response


@app.post("/tailor/{resume_id}/edit")
async def freeform_edit(resume_id: str, req: FreeformEditRequest, request: Request):
    """Send a custom message to the LLM to edit the resume."""
    _check_rate_limit(request)
    _validate_resume_id(resume_id)
    from backend.tailoring.tailoring_engine import TailoringEngine

    # Use tailored IR if available, otherwise original
    ir = _tailored_ir_store.get(resume_id)
    if ir is None:
        ir = _load_ir(resume_id)

    engine = TailoringEngine()
    engine_result = engine.freeform_edit(
        content=ir.content,
        user_message=req.message,
    )

    if not engine_result.llm_used:
        raise HTTPException(500, engine_result.llm_error or "LLM call failed")

    # Get or create tailoring result
    result = _tailoring_store.get(resume_id)
    if result is None:
        result = TailoringResult(resume_id=resume_id, job_title="")
        _tailoring_store[resume_id] = result

    # Merge new changes into existing result
    existing_ids = {c.bullet_id: i for i, c in enumerate(result.bullet_changes)}
    for change in engine_result.bullet_changes:
        if change.action != "rewrite":
            continue
        if change.bullet_id in existing_ids:
            idx = existing_ids[change.bullet_id]
            result.bullet_changes[idx].tailored_text = change.tailored_text
            result.bullet_changes[idx].action = "rewrite"
            result.bullet_changes[idx].reason = f"[edit] {change.reason}"
            result.bullet_changes[idx].accepted = True
            result.bullet_changes[idx].resolved = False
        else:
            change.reason = f"[edit] {change.reason}"
            result.bullet_changes.append(change)

    # Re-apply tailoring
    base_ir = _load_ir(resume_id)
    service = TailoringService(use_llm=False)
    tailored_ir = service.apply_tailoring(base_ir, result)
    _tailored_ir_store[resume_id] = tailored_ir

    # Save
    ir_path = GENERATED_DIR / f"{resume_id}_tailored_ir.json"
    ir_path.write_text(tailored_ir.model_dump_json(indent=2))

    return {
        "changes_applied": sum(
            1 for c in engine_result.bullet_changes if c.action == "rewrite"
        ),
        "duration_ms": engine_result.duration_ms,
        "result": result.model_dump(),
    }


@app.post("/tailor/{resume_id}/skills/accept-all")
async def accept_all_skills(resume_id: str, req: SkillBatchAcceptRequest):
    """Accept or reject all pending skill additions and reorders at once."""
    result = _tailoring_store.get(resume_id)
    if result is None:
        raise HTTPException(404, "No tailoring result found")

    # Accept/reject all pending additions
    for cat, skills in (result.added_skills or {}).items():
        for skill in skills:
            key = f"{cat}:{skill}"
            if key not in result.additions_accepted:
                result.additions_accepted[key] = req.accepted

    # Accept/reject all pending reorders
    for cat in result.reordered_skills:
        if cat not in result.reorder_accepted:
            result.reorder_accepted[cat] = req.accepted

    # Re-apply tailoring
    ir = _load_ir(resume_id)
    service = TailoringService(use_llm=False)
    tailored_ir = service.apply_tailoring(ir, result)
    _tailored_ir_store[resume_id] = tailored_ir

    # Recalculate coverage stats based on updated resume
    _recalculate_coverage(resume_id, result, tailored_ir)

    # Return result with updated coverage
    response = result.model_dump()
    return response


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


# --- Format templates ---

class SaveTemplateRequest(BaseModel):
    name: str
    description: str = ""


@app.post("/templates/save/{resume_id}")
async def save_format_template(resume_id: str, req: SaveTemplateRequest):
    """Save the formatting style of an uploaded resume as a reusable template."""
    from backend.services.template_service import extract_template, save_template

    ir = _load_ir(resume_id)
    template = extract_template(ir, name=req.name, description=req.description)

    template_id = req.name.lower().replace(" ", "_").replace("-", "_")
    save_template(template, template_id)

    return {
        "template_id": template_id,
        "name": template.name,
        "font": template.body_font_family,
        "size": template.body_font_size_pt,
        "spacing": "1.0x" if template.body_line_spacing_val == 240 else f"{template.body_line_spacing_val/240:.2f}x",
        "spacer": f"{template.spacer_size_half_pt / 2}pt",
        "margins": {
            "top": template.page.margin_top_in,
            "bottom": template.page.margin_bottom_in,
            "left": template.page.margin_left_in,
            "right": template.page.margin_right_in,
        },
        "styles": {role: s.model_dump() for role, s in template.styles.items()},
    }


@app.get("/templates")
async def list_format_templates():
    """List all available format templates."""
    from backend.services.template_service import list_templates
    return {"templates": list_templates()}


@app.get("/templates/{template_id}")
async def get_format_template(template_id: str):
    """Get a specific format template."""
    from backend.services.template_service import load_template
    template = load_template(template_id)
    if template is None:
        raise HTTPException(404, f"Template '{template_id}' not found")
    return template.model_dump()


@app.post("/templates/apply/{resume_id}/{template_id}")
async def apply_format_template(resume_id: str, template_id: str):
    """Apply a format template to a resume."""
    from backend.services.template_service import load_template, apply_template

    ir = _load_ir(resume_id)
    template = load_template(template_id)
    if template is None:
        raise HTTPException(404, f"Template '{template_id}' not found")

    apply_template(ir, template)

    # Save updated IR
    _store[resume_id] = ir
    ir_path = GENERATED_DIR / f"{resume_id}_ir.json"
    ir_path.write_text(ir.model_dump_json(indent=2))

    # Also update tailored IR if it exists
    if resume_id in _tailored_ir_store:
        tailored = _tailored_ir_store[resume_id]
        apply_template(tailored, template)
        tailored_path = GENERATED_DIR / f"{resume_id}_tailored_ir.json"
        tailored_path.write_text(tailored.model_dump_json(indent=2))

    return {
        "status": "applied",
        "template": template_id,
        "font": template.body_font_family,
        "size": template.body_font_size_pt,
    }


@app.delete("/templates/{template_id}")
async def delete_format_template(template_id: str):
    """Delete a format template."""
    from backend.services.template_service import delete_template
    if not delete_template(template_id):
        raise HTTPException(404, f"Template '{template_id}' not found")
    return {"status": "deleted"}


@app.get("/health")
async def health():
    return {"status": "ok", "phase": 5}
