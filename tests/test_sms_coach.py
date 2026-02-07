"""Tests for SMS coach Cloud Run Job."""

import importlib.util
import json
import os
from unittest.mock import MagicMock, patch

# Load cloud-run/sms-coach/main.py as a unique module name to avoid
# collision with cloud-run/slack-processor/main.py (both are "main").
_spec = importlib.util.spec_from_file_location(
    "sms_coach_main",
    os.path.join(os.path.dirname(__file__), "..", "cloud-run", "sms-coach", "main.py"),
)
coach = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(coach)


class TestJsonParsing:
    """Tests for _parse_json_response()."""

    def test_clean_json(self):
        result = coach._parse_json_response('{"message": "hello", "spec_updates": null}')
        assert result["message"] == "hello"
        assert result["spec_updates"] is None

    def test_json_with_fences(self):
        result = coach._parse_json_response('```json\n{"message": "hi", "spec_updates": null}\n```')
        assert result["message"] == "hi"

    def test_json_with_preamble(self):
        result = coach._parse_json_response('Here is the response:\n{"message": "yo", "spec_updates": null}')
        assert result["message"] == "yo"

    def test_missing_message_raises(self):
        try:
            coach._parse_json_response('{"spec_updates": null}')
            assert False, "Should have raised"
        except ValueError:
            pass


class TestHistoryFormatting:
    """Tests for _format_history()."""

    def test_empty_history(self):
        assert coach._format_history([]) == ""

    def test_with_messages(self):
        history = [
            {"role": "coach", "text": "Morning!", "timestamp": "2026-02-06T13:00:00"},
            {"role": "user", "text": "Thanks!", "timestamp": "2026-02-06T14:00:00"},
        ]
        result = coach._format_history(history)
        assert "Coach" in result
        assert "Client" in result
        assert "Morning!" in result
        assert "Thanks!" in result

    def test_truncates_to_last_n(self):
        history = [
            {"role": "user", "text": f"msg {i}", "timestamp": f"2026-02-0{min(i, 9)}T00:00:00"}
            for i in range(30)
        ]
        result = coach._format_history(history, last_n=5)
        lines = [line for line in result.split("\n") if line.strip()]
        assert len(lines) == 5

    def test_morning_type_shown(self):
        history = [
            {"role": "coach", "text": "Rise and grind!", "timestamp": "2026-02-06T13:00:00", "type": "morning"},
        ]
        result = coach._format_history(history)
        assert "[morning]" in result


class TestGenerateMorningMessage:
    """Tests for generate_morning_message()."""

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch.object(coach, "ChatAnthropic")
    def test_returns_message(self, mock_cls):
        mock_llm = MagicMock()
        mock_cls.return_value = mock_llm
        mock_llm.invoke.return_value = MagicMock(
            content=json.dumps({"message": "Leg day! Time to crush it.", "spec_updates": None})
        )
        result = coach.generate_morning_message("# Spec\nGoals: gain muscle", [])
        assert result["message"] == "Leg day! Time to crush it."
        assert result["spec_updates"] is None
        assert mock_cls.call_args[1]["model"] == "claude-opus-4-6"

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch.object(coach, "ChatAnthropic")
    def test_empty_spec_handled(self, mock_cls):
        mock_llm = MagicMock()
        mock_cls.return_value = mock_llm
        mock_llm.invoke.return_value = MagicMock(
            content=json.dumps({"message": "Hey! I'm your coach. What are you working on?", "spec_updates": None})
        )
        result = coach.generate_morning_message("", [])
        assert "message" in result


class TestGenerateReply:
    """Tests for generate_reply()."""

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch.object(coach, "ChatAnthropic")
    def test_reply_without_spec_update(self, mock_cls):
        mock_llm = MagicMock()
        mock_cls.return_value = mock_llm
        mock_llm.invoke.return_value = MagicMock(
            content=json.dumps({"message": "Nice work!", "spec_updates": None})
        )
        result = coach.generate_reply("# Spec", [], "Did 3x10 bench at 185")
        assert result["message"] == "Nice work!"
        assert result["spec_updates"] is None

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch.object(coach, "ChatAnthropic")
    def test_reply_with_spec_update(self, mock_cls):
        mock_llm = MagicMock()
        mock_cls.return_value = mock_llm
        mock_llm.invoke.return_value = MagicMock(
            content=json.dumps({
                "message": "225 5x5 is huge! Updating your log.",
                "spec_updates": "# Updated spec\n## Progress\n- 225x5x5 squat",
            })
        )
        result = coach.generate_reply("# Spec", [], "Hit 225 for 5x5 on squats!")
        assert result["spec_updates"] is not None
        assert "225" in result["spec_updates"]


class TestHandleMorning:
    """Tests for handle_morning()."""

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch.object(coach, "append_history")
    @patch.object(coach, "send_sms")
    @patch.object(coach, "ChatAnthropic")
    def test_sends_and_records(self, mock_cls, mock_send, mock_history):
        mock_llm = MagicMock()
        mock_cls.return_value = mock_llm
        mock_llm.invoke.return_value = MagicMock(
            content=json.dumps({"message": "Push day!", "spec_updates": None})
        )
        mock_send.return_value = "SM_abc"

        coach.handle_morning("trace-1", "# Spec", [])

        mock_send.assert_called_once_with("Push day!")
        mock_history.assert_called_once()
        recorded = mock_history.call_args[0][0]
        assert recorded[0]["role"] == "coach"
        assert recorded[0]["text"] == "Push day!"
        assert recorded[0]["type"] == "morning"


class TestHandleReply:
    """Tests for handle_reply()."""

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key", "SMS_BODY": "Ate 180g protein today"})
    @patch.object(coach, "write_spec")
    @patch.object(coach, "append_history")
    @patch.object(coach, "send_sms")
    @patch.object(coach, "ChatAnthropic")
    def test_reply_with_spec_update_writes(self, mock_cls, mock_send, mock_history, mock_write):
        mock_llm = MagicMock()
        mock_cls.return_value = mock_llm
        mock_llm.invoke.return_value = MagicMock(
            content=json.dumps({
                "message": "Great protein intake!",
                "spec_updates": "# Updated spec",
            })
        )
        mock_send.return_value = "SM_def"

        coach.handle_reply("trace-2", "# Spec", [])

        mock_send.assert_called_once_with("Great protein intake!")
        mock_write.assert_called_once_with("# Updated spec")
        mock_history.assert_called_once()
        recorded = mock_history.call_args[0][0]
        assert len(recorded) == 2  # user msg + coach reply
        assert recorded[0]["role"] == "user"
        assert recorded[1]["role"] == "coach"
