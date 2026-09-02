"""Endpoint-level tests for backend/main.py.

Found by a design/structure audit. Covers two fixes:

1. /inspect/{resume_id}/formatting built a filesystem path from resume_id
   without calling the same _validate_resume_id() every other path-
   building endpoint uses first -- safe in practice (it also checked
   _store.get() first, and _store only ever contains server-generated
   uuid4-hex keys), but inconsistent defense-in-depth. Now validates
   explicitly like the rest of the file.

2. LayoutSettingsUpdate had no ge/le bounds on any numeric field, unlike
   TailorRequest -- a negative/zero margin or font size would flow
   straight into ReportLab/python-docx's Pt()/Inches() constructors,
   risking an uncaught 500 rather than a clean validation error.
"""

from fastapi.testclient import TestClient

from backend.main import app

client = TestClient(app)


class TestInspectFormattingValidatesResumeId:
    def test_malformed_resume_id_rejected_before_touching_disk(self):
        resp = client.get("/inspect/../../etc/passwd/formatting")
        # Path traversal segments get URL-routed away by FastAPI itself
        # for a path param without a `:path` converter, but a same-length
        # malformed id must still be rejected by validation.
        assert resp.status_code in (400, 404)

    def test_non_hex_resume_id_rejected_with_400(self):
        resp = client.get("/inspect/not-a-valid-resume-id/formatting")
        assert resp.status_code == 400
        assert "Invalid resume ID" in resp.json()["detail"]

    def test_well_formed_but_unknown_resume_id_still_404s(self):
        # A syntactically valid (12 hex chars) but never-uploaded id should
        # still 404, not 400 -- validation passing isn't the same as it
        # existing.
        resp = client.get("/inspect/aaaaaaaaaaaa/formatting")
        assert resp.status_code == 404


class TestLayoutSettingsUpdateBounds:
    def test_negative_margin_rejected(self):
        resp = client.post(
            "/layout/aaaaaaaaaaaa/settings",
            json={"margin_top": -1.0},
        )
        assert resp.status_code == 422

    def test_zero_font_size_rejected(self):
        resp = client.post(
            "/layout/aaaaaaaaaaaa/settings",
            json={"body_size_pt": 0},
        )
        assert resp.status_code == 422

    def test_absurdly_large_font_size_rejected(self):
        resp = client.post(
            "/layout/aaaaaaaaaaaa/settings",
            json={"heading_size_pt": 10000},
        )
        assert resp.status_code == 422

    def test_reasonable_values_pass_validation(self):
        # Should get past request validation (a 404 for the unknown resume
        # id is fine and expected -- this only checks the Pydantic layer).
        resp = client.post(
            "/layout/aaaaaaaaaaaa/settings",
            json={"margin_top": 1.0, "body_size_pt": 11.0, "line_spacing": 1.15},
        )
        assert resp.status_code != 422
