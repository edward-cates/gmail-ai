"""Tests for the email-processor classifier (Claude / OpenAI Decisions / shadow)."""

import importlib.util
import io
import json
import os
import sys
import urllib.error
from unittest.mock import MagicMock, patch

# bs4 is only in the job's own requirements.txt, not the dev env; the classifier
# doesn't use it, so stub it if missing.
try:
    import bs4  # noqa: F401
except ImportError:
    sys.modules["bs4"] = MagicMock()

# Load cloud-run/email-processor/main.py under a unique name so it doesn't
# collide with the other cloud-run "main" modules in sys.modules.
_spec = importlib.util.spec_from_file_location(
    "email_processor_main",
    os.path.join(os.path.dirname(__file__), "..", "cloud-run", "email-processor", "main.py"),
)
processor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(processor)


def _decision(choice="marketing", confidence=0.93, type_="choice"):
    """A fake urlopen() context manager returning a Decisions response."""
    answer = {"type": type_, "name": "category"}
    if type_ == "choice":
        answer |= {
            "choice": choice,
            "confidence": confidence,
            "probabilities": [
                {"value": choice, "probability": confidence},
                {"value": "other", "probability": 1 - confidence},
            ],
        }
    resp = MagicMock()
    resp.__enter__.return_value.read.return_value = json.dumps({"answers": [answer]}).encode()
    return resp


ENV = {"OPENAI_API_KEY": "test-key"}


class TestDecisionsClassifier:
    @patch.dict("os.environ", ENV)
    def test_maps_choice_to_category(self):
        with patch.object(processor.urllib.request, "urlopen", return_value=_decision()) as op:
            result = processor.classify_email_decisions("50% off!", "shop@x.com", "Sale today")
        assert result["category"] == "marketing"
        assert result["confidence"] == 0.93
        assert "marketing=0.93" in result["reason"]

        sent = json.loads(op.call_args.args[0].data)
        assert sent["model"] == "gpt-6-luna"
        values = [c["value"] for c in sent["questions"][0]["choices"]]
        assert values == ["marketing", "newsletter", "noti", "recruiting", "other"]
        assert "Homeroom" in sent["questions"][0]["choices"][4]["description"]

    @patch.dict("os.environ", ENV)
    def test_low_confidence_stays_in_inbox(self):
        with patch.object(processor.urllib.request, "urlopen", return_value=_decision("noti", 0.4)):
            result = processor.classify_email_decisions("s", "f", "b")
        assert result["category"] == "other"
        assert "kept in inbox" in result["reason"]

    @patch.dict("os.environ", ENV)
    def test_refusal_is_other(self):
        with patch.object(processor.urllib.request, "urlopen", return_value=_decision(type_="refusal")):
            result = processor.classify_email_decisions("s", "f", "b")
        assert result["category"] == "other"

    @patch.dict("os.environ", ENV)
    def test_http_error_raises(self):
        err = urllib.error.HTTPError(processor.DECISIONS_URL, 401, "nope", {}, io.BytesIO(b"bad key"))
        with patch.object(processor.urllib.request, "urlopen", side_effect=err):
            try:
                processor.classify_email_decisions("s", "f", "b")
                raise AssertionError("expected RuntimeError")
            except RuntimeError as e:
                assert "401" in str(e)

    @patch.dict("os.environ", ENV)
    def test_retries_rate_limit(self):
        err = urllib.error.HTTPError(processor.DECISIONS_URL, 429, "slow", {}, io.BytesIO(b""))
        with patch.object(processor.urllib.request, "urlopen", side_effect=[err, _decision()]), \
                patch.object(processor.time, "sleep"):
            assert processor.classify_email_decisions("s", "f", "b")["category"] == "marketing"


class TestClassifierModes:
    CLAUDE = {"category": "newsletter", "confidence": 0.9, "reason": "r"}

    @patch.dict("os.environ", {"CLASSIFIER": "claude"})
    def test_default_uses_claude_only(self):
        with patch.object(processor, "classify_email_claude", return_value=dict(self.CLAUDE)), \
                patch.object(processor, "classify_email_decisions") as dec:
            assert processor.classify_email("s", "f", "b")["category"] == "newsletter"
        dec.assert_not_called()

    @patch.dict("os.environ", {"CLASSIFIER": "decisions"})
    def test_decisions_mode(self):
        with patch.object(processor, "classify_email_decisions", return_value={"category": "noti"}), \
                patch.object(processor, "classify_email_claude") as claude:
            assert processor.classify_email("s", "f", "b")["category"] == "noti"
        claude.assert_not_called()

    @patch.dict("os.environ", {"CLASSIFIER": "shadow"})
    def test_shadow_acts_on_claude_and_records_decisions(self):
        with patch.object(processor, "classify_email_claude", return_value=dict(self.CLAUDE)), \
                patch.object(processor, "classify_email_decisions", return_value={"category": "marketing"}):
            result = processor.classify_email("s", "f", "b")
        assert result["category"] == "newsletter"
        assert result["shadow"] == {"category": "marketing", "agree": False}

    @patch.dict("os.environ", {"CLASSIFIER": "shadow"})
    def test_shadow_failure_never_fails_run(self):
        with patch.object(processor, "classify_email_claude", return_value=dict(self.CLAUDE)), \
                patch.object(processor, "classify_email_decisions", side_effect=RuntimeError("boom")):
            result = processor.classify_email("s", "f", "b")
        assert result["category"] == "newsletter"
        assert result["shadow"] == {"error": "boom"}
