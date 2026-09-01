"""Tests for the batch-trim verb-variety guidance
(backend/tailoring/tailoring_engine.py's batch_trim_bullets +
backend/prompts.py's BATCH_TRIM_* prompts).

Regression (live bug report): tailored resumes kept converging on the
same leading verb ("Built") across many bullets. _enforce_verb_variety
(tailoring_service.py) fixes this after the fact, but the actual source
was the batch-trim LLM call itself: under character-budget pressure it
had no way to know what verbs other, non-overflowing bullets in the same
resume already used, and its own worked example in the prompt literally
demonstrated collapsing to "Built". This tests that the prompt now
carries that information through to the actual API request.
"""

import json
from unittest.mock import MagicMock, patch

from backend.tailoring.tailoring_engine import TailoringEngine


def _mock_response(text: str) -> MagicMock:
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


class TestBatchTrimVerbVariety:
    def test_avoid_leading_verbs_included_in_prompt_sent_to_llm(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        captured = {}

        def fake_create(**kwargs):
            captured["messages"] = kwargs["messages"]
            return _mock_response(json.dumps({"trimmed_bullets": [
                {"bullet_id": "b1", "trimmed_text": "Engineered a thing", "final_char_count": 19},
            ]}))

        with patch("anthropic.Anthropic") as MockClient:
            MockClient.return_value.messages.create.side_effect = fake_create
            engine = TailoringEngine()
            engine.batch_trim_bullets(
                bullets=[{
                    "bullet_id": "b1",
                    "text": "Built a thing that does something long enough to overflow",
                    "break_index": 10,
                    "max_chars": 40,
                    "keywords": [],
                }],
                avoid_leading_verbs=["Built", "Built", "Developed"],
            )

        user_msg = captured["messages"][0]["content"]
        assert "VERB VARIETY" in user_msg
        assert "Built" in user_msg
        assert "Developed" in user_msg

    def test_no_verb_variety_note_when_nothing_to_avoid(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        captured = {}

        def fake_create(**kwargs):
            captured["messages"] = kwargs["messages"]
            return _mock_response(json.dumps({"trimmed_bullets": [
                {"bullet_id": "b1", "trimmed_text": "Built a thing", "final_char_count": 13},
            ]}))

        with patch("anthropic.Anthropic") as MockClient:
            MockClient.return_value.messages.create.side_effect = fake_create
            engine = TailoringEngine()
            engine.batch_trim_bullets(
                bullets=[{
                    "bullet_id": "b1",
                    "text": "Built a thing",
                    "break_index": 5,
                    "max_chars": 40,
                    "keywords": [],
                }],
                avoid_leading_verbs=None,
            )

        user_msg = captured["messages"][0]["content"]
        # The static rule text mentions "VERB VARIETY note below if
        # present" unconditionally -- what must NOT appear is the actual
        # dynamic note itself, added only when there's something to avoid.
        assert "already start with:" not in user_msg

    def test_system_prompt_no_longer_defaults_example_to_built(self):
        from backend.prompts import BATCH_TRIM_USER_V2_XML
        # The worked "aggressive rephrase" example used to output "Built"
        # verbatim, teaching the model that's the go-to answer. It's fine
        # for "Built" to appear as *a* label (e.g. "don't default to
        # Built"), but it must not be the example's actual Output: line.
        assert 'Output: "Built ' not in BATCH_TRIM_USER_V2_XML
