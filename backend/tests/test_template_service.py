"""Tests for template_service.py's filesystem safety.

Regression: save_template/load_template/delete_template built a filesystem
path directly from a caller-supplied template_id (TEMPLATES_DIR /
f"{template_id}.json") with no validation. main.py's derivation for the
save endpoint only replaced spaces and hyphens, not "/" or ".." — and
since it comes from a JSON request body field (not a URL path segment),
it isn't even constrained the way a path parameter is. A name like
"../../etc/passwd" flowed straight through to an arbitrary-file write
(save), read (load), or delete.
"""

import pytest

from backend.models.format_template import FormatTemplate
from backend.services.template_service import (
    delete_template, load_template, save_template, slugify_template_id,
)


class TestTemplateIdValidation:
    @pytest.mark.parametrize("bad_id", [
        "../../etc/passwd",
        "../secret",
        "..",
        "a/b",
        "a\\b",
        "",
        "UPPERCASE",
        "has space",
        "semi;colon",
        "x" * 65,
    ])
    def test_unsafe_ids_rejected_on_save(self, bad_id, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "backend.services.template_service.TEMPLATES_DIR", tmp_path
        )
        with pytest.raises(ValueError):
            save_template(FormatTemplate(name="x"), bad_id)

    @pytest.mark.parametrize("bad_id", ["../../etc/passwd", "a/b", ".."])
    def test_unsafe_ids_rejected_on_load(self, bad_id, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "backend.services.template_service.TEMPLATES_DIR", tmp_path
        )
        with pytest.raises(ValueError):
            load_template(bad_id)

    @pytest.mark.parametrize("bad_id", ["../../etc/passwd", "a/b", ".."])
    def test_unsafe_ids_rejected_on_delete(self, bad_id, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "backend.services.template_service.TEMPLATES_DIR", tmp_path
        )
        with pytest.raises(ValueError):
            delete_template(bad_id)

    def test_safe_id_round_trips_normally(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "backend.services.template_service.TEMPLATES_DIR", tmp_path
        )
        save_template(FormatTemplate(name="My Template"), "my_template")
        loaded = load_template("my_template")
        assert loaded is not None
        assert loaded.name == "My Template"
        assert delete_template("my_template") is True

    def test_traversal_id_cannot_escape_templates_dir(self, tmp_path, monkeypatch):
        # Belt-and-suspenders: even if validation were ever bypassed,
        # confirm no file lands outside TEMPLATES_DIR for a traversal id.
        monkeypatch.setattr(
            "backend.services.template_service.TEMPLATES_DIR", tmp_path
        )
        outside_target = tmp_path.parent / "escaped.json"
        outside_target.unlink(missing_ok=True)
        with pytest.raises(ValueError):
            save_template(FormatTemplate(name="x"), "../escaped")
        assert not outside_target.exists()


class TestSlugifyTemplateId:
    def test_spaces_and_case_normalized(self):
        assert slugify_template_id("My Resume Template") == "my_resume_template"

    def test_path_traversal_characters_stripped(self):
        assert slugify_template_id("../../etc/passwd") == "etc_passwd"

    def test_result_always_matches_safe_pattern(self):
        import re
        for name in ["../../etc/passwd", "a/b\\c", "..", "", "   ", "!!!"]:
            slug = slugify_template_id(name)
            assert re.match(r"^[a-z0-9_]{1,64}$", slug), slug

    def test_slug_is_directly_usable_by_save_template(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "backend.services.template_service.TEMPLATES_DIR", tmp_path
        )
        slug = slugify_template_id("../../etc/passwd")
        save_template(FormatTemplate(name="x"), slug)  # must not raise
        assert (tmp_path / f"{slug}.json").exists()
