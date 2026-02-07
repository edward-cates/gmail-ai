"""Tests for Trello-based muscle growth coach Cloud Run Job."""

import importlib.util
import json
import os
from unittest.mock import MagicMock, patch

# Load cloud-run/coach/main.py as a unique module name to avoid
# collision with other main.py modules.
_spec = importlib.util.spec_from_file_location(
    "coach_main",
    os.path.join(os.path.dirname(__file__), "..", "cloud-run", "coach", "main.py"),
)
coach = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(coach)


class TestJsonParsing:
    """Tests for _parse_json_response()."""

    def test_clean_json(self):
        result = coach._parse_json_response(
            '{"message": "hello", "spec_updates": null}'
        )
        assert result["message"] == "hello"
        assert result["spec_updates"] is None

    def test_json_with_fences(self):
        result = coach._parse_json_response(
            '```json\n{"message": "hi", "spec_updates": null}\n```'
        )
        assert result["message"] == "hi"

    def test_json_with_preamble(self):
        result = coach._parse_json_response(
            'Here is the response:\n{"message": "yo", "spec_updates": null}'
        )
        assert result["message"] == "yo"

    def test_morning_regimen_format(self):
        result = coach._parse_json_response(json.dumps({
            "card_title": "Monday, Feb 6 — Push Day",
            "regimen": "## Bench Press\n4x8 @ 185",
            "exercises": ["Bench Press 4x8 @ 185"],
            "coach_message": "Let's go!",
            "spec_updates": None,
        }))
        assert result["card_title"] == "Monday, Feb 6 — Push Day"
        assert result["exercises"] == ["Bench Press 4x8 @ 185"]


class TestReadBoardContext:
    """Tests for read_board_context()."""

    def test_formats_cards_and_comments(self):
        mock_trello = MagicMock()
        mock_trello.get_cards.return_value = [
            {"id": "card1", "name": "Monday Push", "desc": "Bench day"},
        ]
        mock_trello.get_my_member_id.return_value = "coach123"
        mock_trello.get_card_comments.return_value = [
            {
                "memberCreator": {"id": "user456", "fullName": "Edward"},
                "date": "2026-02-06T14:00:00",
                "data": {"text": "Hit 225 on bench!"},
            },
            {
                "memberCreator": {"id": "coach123", "fullName": "Coach Bot"},
                "date": "2026-02-06T14:05:00",
                "data": {"text": "Nice PR!"},
            },
        ]

        result = coach.read_board_context(mock_trello)
        assert "Monday Push" in result
        assert "Client (Edward)" in result
        assert "Coach (Coach Bot)" in result
        assert "Hit 225 on bench!" in result
        assert "Nice PR!" in result

    def test_empty_board(self):
        mock_trello = MagicMock()
        mock_trello.get_cards.return_value = []
        mock_trello.get_my_member_id.return_value = "coach123"

        result = coach.read_board_context(mock_trello)
        assert result == ""


class TestReadCardContext:
    """Tests for read_card_context()."""

    def test_formats_card_comments(self):
        mock_trello = MagicMock()
        mock_trello.get_card.return_value = {
            "name": "Tuesday Pull", "desc": "Back day",
        }
        mock_trello.get_my_member_id.return_value = "coach123"
        mock_trello.get_card_comments.return_value = [
            {
                "memberCreator": {"id": "user456", "fullName": "Edward"},
                "date": "2026-02-06T10:00:00",
                "data": {"text": "Feeling sore from yesterday"},
            },
        ]

        result = coach.read_card_context(mock_trello, "card1")
        assert "Tuesday Pull" in result
        assert "Client (Edward)" in result
        assert "Feeling sore" in result


class TestGenerateMorningRegimen:
    """Tests for generate_morning_regimen()."""

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch.object(coach, "ChatAnthropic")
    def test_returns_regimen(self, mock_cls):
        mock_llm = MagicMock()
        mock_cls.return_value = mock_llm
        mock_llm.invoke.return_value = MagicMock(
            content=json.dumps({
                "card_title": "Monday — Push Day",
                "regimen": "## Bench\n4x8",
                "exercises": ["Bench 4x8"],
                "coach_message": "Let's go!",
                "spec_updates": None,
            })
        )
        result = coach.generate_morning_regimen("# Spec\nGoals: gain muscle", "")
        assert result["card_title"] == "Monday — Push Day"
        assert result["exercises"] == ["Bench 4x8"]
        assert result["spec_updates"] is None
        assert mock_cls.call_args[1]["model"] == "claude-opus-4-6"


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
        result = coach.generate_reply("# Spec", "", "Did 3x10 bench at 185", "Push Day")
        assert result["message"] == "Nice work!"
        assert result["spec_updates"] is None

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch.object(coach, "ChatAnthropic")
    def test_reply_with_spec_update(self, mock_cls):
        mock_llm = MagicMock()
        mock_cls.return_value = mock_llm
        mock_llm.invoke.return_value = MagicMock(
            content=json.dumps({
                "message": "225 5x5 is huge!",
                "spec_updates": "# Updated spec\n## Progress\n- 225x5x5 squat",
            })
        )
        result = coach.generate_reply("# Spec", "", "Hit 225 for 5x5!", "Leg Day")
        assert result["spec_updates"] is not None
        assert "225" in result["spec_updates"]


class TestHandleMorning:
    """Tests for handle_morning()."""

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch.object(coach, "TrelloClient")
    @patch.object(coach, "ChatAnthropic")
    def test_creates_card_and_checklist(self, mock_llm_cls, mock_trello_cls):
        mock_trello = MagicMock()
        mock_trello_cls.return_value = mock_trello
        mock_trello.get_board_desc.return_value = "# Spec"
        mock_trello.get_cards.return_value = []
        mock_trello.get_my_member_id.return_value = "coach123"
        mock_trello.get_list_id.side_effect = lambda name: f"list_{name.lower()}"
        mock_trello.create_card.return_value = {"id": "new_card_123"}
        mock_trello.create_checklist.return_value = {"id": "cl_123"}

        mock_llm = MagicMock()
        mock_llm_cls.return_value = mock_llm
        mock_llm.invoke.return_value = MagicMock(
            content=json.dumps({
                "card_title": "Monday — Push Day",
                "regimen": "## Bench\n4x8 @ 185",
                "exercises": ["Bench 4x8 @ 185", "Incline DB 3x10 @ 70"],
                "coach_message": "Time to push!",
                "spec_updates": None,
            })
        )

        coach.handle_morning("trace-1")

        mock_trello.create_card.assert_called_once()
        mock_trello.create_checklist.assert_called_once_with("new_card_123")
        assert mock_trello.add_checklist_item.call_count == 2
        mock_trello.add_comment.assert_called_once_with("new_card_123", "Time to push!")

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch.object(coach, "TrelloClient")
    @patch.object(coach, "ChatAnthropic")
    def test_moves_active_cards_to_log(self, mock_llm_cls, mock_trello_cls):
        mock_trello = MagicMock()
        mock_trello_cls.return_value = mock_trello
        mock_trello.get_board_desc.return_value = "# Spec"
        mock_trello.get_my_member_id.return_value = "coach123"
        mock_trello.get_list_id.side_effect = lambda name: f"list_{name.lower()}"
        mock_trello.get_cards.return_value = [
            {"id": "old_card", "name": "Sunday — Rest", "idList": "list_active"},
        ]
        mock_trello.get_card_comments.return_value = []
        mock_trello.create_card.return_value = {"id": "new_card"}
        mock_trello.create_checklist.return_value = {"id": "cl_1"}

        mock_llm = MagicMock()
        mock_llm_cls.return_value = mock_llm
        mock_llm.invoke.return_value = MagicMock(
            content=json.dumps({
                "card_title": "Monday — Push",
                "regimen": "## Bench",
                "exercises": [],
                "coach_message": "",
                "spec_updates": None,
            })
        )

        coach.handle_morning("trace-2")

        mock_trello.move_card.assert_called_once_with("old_card", "list_log")


class TestHandleReply:
    """Tests for handle_reply()."""

    @patch.dict("os.environ", {
        "ANTHROPIC_API_KEY": "test-key",
        "COMMENT_TEXT": "Hit 225 on squats today",
        "CARD_ID": "card_abc",
    })
    @patch.object(coach, "TrelloClient")
    @patch.object(coach, "ChatAnthropic")
    def test_replies_to_comment(self, mock_llm_cls, mock_trello_cls):
        mock_trello = MagicMock()
        mock_trello_cls.return_value = mock_trello
        mock_trello.get_board_desc.return_value = "# Spec"
        mock_trello.get_card.return_value = {"name": "Monday — Push", "desc": "Bench day"}
        mock_trello.get_my_member_id.return_value = "coach123"
        mock_trello.get_card_comments.return_value = []

        mock_llm = MagicMock()
        mock_llm_cls.return_value = mock_llm
        mock_llm.invoke.return_value = MagicMock(
            content=json.dumps({
                "message": "225 is a huge PR! Updating your log.",
                "spec_updates": "# Updated\n- Squat: 225",
            })
        )

        coach.handle_reply("trace-3")

        mock_trello.add_comment.assert_called_once_with(
            "card_abc", "225 is a huge PR! Updating your log."
        )
        mock_trello.update_board_desc.assert_called_once_with("# Updated\n- Squat: 225")

    @patch.dict("os.environ", {"COMMENT_TEXT": "", "CARD_ID": ""})
    def test_skips_empty_comment(self):
        # Should not raise — just skips
        coach.handle_reply("trace-4")
