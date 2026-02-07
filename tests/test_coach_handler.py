"""Tests for coach Trello webhook handler Cloud Function."""

import json
from unittest.mock import MagicMock, patch

from functions.coach_handler import (
    handle_trello_webhook,
    trigger_coach_morning,
)


def _make_request(method="POST", json_data=None):
    """Create a mock Flask request."""
    req = MagicMock()
    req.method = method
    req.get_json.return_value = json_data
    return req


class TestHandleTrelloWebhook:
    """Tests for handle_trello_webhook()."""

    def test_head_returns_200(self):
        req = _make_request(method="HEAD")
        result = handle_trello_webhook(req)
        assert result == ({"ok": True}, 200)

    def test_non_comment_action_skipped(self):
        req = _make_request(json_data={
            "action": {"type": "updateCard", "data": {}},
        })
        body, status = handle_trello_webhook(req)
        assert status == 200
        assert body["skipped"] == "updateCard"

    def test_coach_comment_skipped(self):
        req = _make_request(json_data={
            "action": {
                "type": "commentCard",
                "memberCreator": {"id": "user456", "fullName": "Edward"},
                "data": {
                    "text": "**[Coach]** Great job on that PR!",
                    "card": {"id": "card1"},
                    "board": {"id": "board1"},
                },
            },
        })
        body, status = handle_trello_webhook(req)
        assert status == 200
        assert body["skipped"] == "coach_comment"

    @patch("functions.coach_handler._resolve_board_id", return_value="expected_board")
    def test_wrong_board_skipped(self, _mock_board):
        req = _make_request(json_data={
            "action": {
                "type": "commentCard",
                "memberCreator": {"id": "user456", "fullName": "Edward"},
                "data": {
                    "text": "Hello",
                    "card": {"id": "card1"},
                    "board": {"id": "wrong_board"},
                },
            },
        })
        body, status = handle_trello_webhook(req)
        assert status == 200
        assert body["skipped"] == "wrong_board"

    @patch("functions.coach_handler._resolve_board_id", return_value="board1")
    @patch("functions.coach_handler._trigger_coach_job")
    def test_valid_comment_triggers_job(self, mock_trigger, _mock_board):
        req = _make_request(json_data={
            "action": {
                "type": "commentCard",
                "memberCreator": {"id": "user456", "fullName": "Edward"},
                "data": {
                    "text": "Hit 225 on squats!",
                    "card": {"id": "card_abc"},
                    "board": {"id": "board1"},
                },
            },
        })
        body, status = handle_trello_webhook(req)
        assert status == 200
        assert "trace_id" in body
        mock_trigger.assert_called_once()
        call_args = mock_trigger.call_args
        assert call_args[0][1] == "Hit 225 on squats!"  # comment_text
        assert call_args[0][2] == "card_abc"  # card_id

    def test_empty_comment_skipped(self):
        req = _make_request(json_data={
            "action": {
                "type": "commentCard",
                "memberCreator": {"id": "user456", "fullName": "Edward"},
                "data": {
                    "text": "",
                    "card": {"id": "card1"},
                    "board": {"id": "board1"},
                },
            },
        })
        body, status = handle_trello_webhook(req)
        assert status == 200
        assert body["skipped"] == "empty"


class TestTriggerCoachMorning:
    """Tests for trigger_coach_morning()."""

    @patch("functions.coach_handler._trigger_coach_job")
    def test_triggers_morning_job(self, mock_trigger):
        req = MagicMock()
        result = trigger_coach_morning(req)
        assert result["ok"] is True
        assert "trace_id" in result
        mock_trigger.assert_called_once()
        call_kwargs = mock_trigger.call_args
        assert call_kwargs[1]["mode"] == "morning"
