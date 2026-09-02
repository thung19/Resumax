"""Tests confirming config.py's LLMConfig/LayoutConfig actually reach the
call sites they're documented to control.

Found by a design/structure audit: config.py defined LLM_TAILORING_MODEL/
LLM_ANALYSIS_MODEL/LLM_*_MAX_TOKENS/LAYOUT_* env vars with full docstrings,
but grepping the codebase for any read of `config.llm.*`/`config.layout.*`
outside config.py itself turned up zero matches -- every real call site
independently hardcoded its own model name/token limit/threshold instead.
Worse, TailoringEngine read a *different*, undocumented env var name
(ANTHROPIC_MODEL) than the one config.py documented (LLM_TAILORING_MODEL),
so a deployer following the documented config surface saw no effect at
all. These tests exercise the env-var -> config -> call-site path directly
(monkeypatching os.environ and clearing the config singleton) rather than
just asserting the dead dataclass fields parse correctly.
"""

import backend.config as config_module


def _reload_config(monkeypatch, **env):
    """Set env vars and force a fresh PipelineConfig to be built."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(config_module, "_global_config", None)
    return config_module.get_config()


class TestTailoringModelWiring:
    def test_llm_tailoring_model_env_var_is_honored(self, monkeypatch):
        _reload_config(monkeypatch, LLM_TAILORING_MODEL="claude-test-model")
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)

        from backend.tailoring.tailoring_engine import TailoringEngine
        engine = TailoringEngine()
        assert engine._model == "claude-test-model"

    def test_anthropic_model_env_var_still_takes_precedence(self, monkeypatch):
        # Backward compatibility: existing deployments that already set
        # ANTHROPIC_MODEL must keep working exactly as before.
        _reload_config(monkeypatch, LLM_TAILORING_MODEL="claude-config-model")
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-env-override")

        from backend.tailoring.tailoring_engine import TailoringEngine
        engine = TailoringEngine()
        assert engine._model == "claude-env-override"

    def test_explicit_constructor_arg_wins_over_everything(self, monkeypatch):
        _reload_config(monkeypatch, LLM_TAILORING_MODEL="claude-config-model")
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-env-override")

        from backend.tailoring.tailoring_engine import TailoringEngine
        engine = TailoringEngine(model="claude-explicit")
        assert engine._model == "claude-explicit"

    def test_default_unchanged_when_no_env_vars_set(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        monkeypatch.delenv("LLM_TAILORING_MODEL", raising=False)
        monkeypatch.setattr(config_module, "_global_config", None)

        from backend.tailoring.tailoring_engine import TailoringEngine
        engine = TailoringEngine()
        assert engine._model == "claude-sonnet-4-6"

    def test_max_tokens_sourced_from_config(self, monkeypatch):
        _reload_config(monkeypatch, LLM_TAILORING_MAX_TOKENS="1234")

        from backend.tailoring.tailoring_engine import TailoringEngine
        engine = TailoringEngine()
        assert engine._max_tokens == 1234


class TestAnalysisModelWiring:
    def test_llm_analysis_model_env_var_is_honored(self, monkeypatch):
        _reload_config(monkeypatch, LLM_ANALYSIS_MODEL="claude-analysis-test")

        from backend.analysis.job_analyzer import JobAnalyzer
        analyzer = JobAnalyzer(use_llm=True)
        assert analyzer._model == "claude-analysis-test"

    def test_explicit_constructor_arg_wins(self, monkeypatch):
        _reload_config(monkeypatch, LLM_ANALYSIS_MODEL="claude-analysis-test")

        from backend.analysis.job_analyzer import JobAnalyzer
        analyzer = JobAnalyzer(use_llm=True, model="claude-explicit-analysis")
        assert analyzer._model == "claude-explicit-analysis"

    def test_default_unchanged_when_no_env_var_set(self, monkeypatch):
        monkeypatch.delenv("LLM_ANALYSIS_MODEL", raising=False)
        monkeypatch.setattr(config_module, "_global_config", None)

        from backend.analysis.job_analyzer import JobAnalyzer
        analyzer = JobAnalyzer(use_llm=True)
        assert analyzer._model == "claude-haiku-4-5-20251001"


class TestLayoutConfigWiring:
    def test_tailor_defaults_match_layout_config(self, monkeypatch):
        monkeypatch.delenv("LAYOUT_MAX_BULLETS", raising=False)
        monkeypatch.delenv("LAYOUT_SINGLE_LINE", raising=False)
        monkeypatch.delenv("LAYOUT_MAX_CHARS", raising=False)
        monkeypatch.setattr(config_module, "_global_config", None)
        config = config_module.get_config()

        import inspect
        from backend.services.tailoring_service import TailoringService
        sig = inspect.signature(TailoringService.tailor)
        assert sig.parameters["max_bullets_per_entry"].default == config.layout.max_bullets_per_entry
        assert sig.parameters["enforce_single_line"].default == config.layout.enforce_single_line_bullets
        assert sig.parameters["max_bullet_chars"].default == config.layout.max_chars_per_line
