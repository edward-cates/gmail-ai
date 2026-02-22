"""Tests for Trello-based muscle growth coach Cloud Run Job."""

import importlib.util
import json
import os
from unittest.mock import MagicMock, patch

import requests

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

    def test_morning_cards_format(self):
        result = coach._parse_json_response(json.dumps({
            "exercise": {
                "title": "Monday, Feb 6 — Push Day",
                "description": "## Bench Press\n4x8 @ 185",
                "checklist": ["Bench Press 4x8 @ 185"],
                "comment": "Let's go!",
            },
            "nutrition": {
                "title": "Monday, Feb 6 — Nutrition",
                "description": "## Meals",
                "checklist": ["Meal 1: Oatmeal + whey"],
            },
            "actions": [],
            "spec_update_instruction": None,
        }))
        assert result["exercise"]["title"] == "Monday, Feb 6 — Push Day"
        assert result["exercise"]["checklist"] == ["Bench Press 4x8 @ 185"]
        assert result["nutrition"]["checklist"] == ["Meal 1: Oatmeal + whey"]


class TestUtcToCt:
    """Tests for _utc_to_ct() timestamp conversion."""

    def test_converts_utc_to_central(self):
        # 2:00 PM UTC = 8:00 AM CT
        result = coach._utc_to_ct("2026-02-06T14:00:00.000Z")
        assert "8:00 am" in result

    def test_handles_iso_without_z(self):
        result = coach._utc_to_ct("2026-02-06T20:30:00")
        assert "2:30 pm" in result

    def test_empty_string(self):
        assert coach._utc_to_ct("") == ""

    def test_short_string(self):
        assert coach._utc_to_ct("2026") == "2026"


class TestReadBoardContext:
    """Tests for read_board_context()."""

    def test_formats_cards_and_comments(self):
        mock_trello = MagicMock()
        mock_trello.get_lists.return_value = [
            {"id": "list1", "name": "Exercise"},
        ]
        mock_trello.get_cards.return_value = [
            {"id": "card1", "name": "Monday Push", "desc": "Bench day", "idList": "list1"},
        ]
        mock_trello.get_card_checklists.return_value = [
            {"checkItems": [
                {"id": "ci1", "name": "Bench 4x8", "state": "complete"},
                {"id": "ci2", "name": "Incline 3x10", "state": "incomplete"},
            ]},
        ]
        mock_trello.get_card_comments.return_value = [
            {
                "date": "2026-02-06T14:00:00",
                "data": {"text": "Hit 225 on bench!"},
            },
            {
                "date": "2026-02-06T14:05:00",
                "data": {"text": "**[Coach]** Nice PR!"},
            },
        ]

        result = coach.read_board_context(mock_trello)
        assert "[Exercise] Monday Push" in result
        assert "card_id: card1" in result
        assert "[x] Bench 4x8" in result
        assert "[ ] Incline 3x10" in result
        assert "item_id: ci1" in result
        assert "Client:" in result
        assert "Coach:" in result
        assert "Hit 225 on bench!" in result
        assert "Nice PR!" in result

    def test_empty_board(self):
        mock_trello = MagicMock()
        mock_trello.get_cards.return_value = []
        mock_trello.get_lists.return_value = []

        result = coach.read_board_context(mock_trello)
        assert result == ""


class TestReadCardContext:
    """Tests for read_card_context()."""

    def test_formats_card_comments(self):
        mock_trello = MagicMock()
        mock_trello.get_card.return_value = {
            "name": "Tuesday Pull", "desc": "Back day",
        }
        mock_trello.get_card_comments.return_value = [
            {
                "date": "2026-02-06T10:00:00",
                "data": {"text": "Feeling sore from yesterday"},
            },
        ]

        result = coach.read_card_context(mock_trello, "card1")
        assert "Tuesday Pull" in result
        assert "Client:" in result
        assert "Feeling sore" in result


def _mock_api_response(text):
    """Create a mock Anthropic API response with the given text."""
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


class TestGenerateMorningCards:
    """Tests for generate_morning_cards()."""

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch.object(coach.anthropic, "Anthropic")
    def test_returns_cards(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = _mock_api_response(
            json.dumps({
                "exercise": {
                    "title": "Monday — Push Day",
                    "description": "## Bench\n4x8",
                    "checklist": ["Bench 4x8"],
                    "comment": "Let's go!",
                },
                "nutrition": {
                    "title": "Monday — Nutrition",
                    "description": "## Meals",
                    "checklist": ["Meal 1: Oatmeal"],
                },
                "actions": [],
                "spec_update_instruction": None,
            })
        )
        result = coach.generate_morning_cards("# Spec\nGoals: gain muscle", "")
        assert result["exercise"]["title"] == "Monday — Push Day"
        assert result["exercise"]["checklist"] == ["Bench 4x8"]
        assert result["nutrition"]["checklist"] == ["Meal 1: Oatmeal"]
        assert result["spec_update_instruction"] is None
        assert mock_client.messages.create.call_args[1]["model"] == "claude-opus-4-6"


class TestGenerateReply:
    """Tests for generate_reply()."""

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch.object(coach.anthropic, "Anthropic")
    def test_reply_without_spec_update(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = _mock_api_response(
            json.dumps({
                "message": "Nice work!",
                "actions": [],
                "spec_update_instruction": None,
            })
        )
        result = coach.generate_reply("# Spec", "", "", "Did 3x10 bench at 185", "Push Day")
        assert result["message"] == "Nice work!"
        assert result["spec_update_instruction"] is None

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch.object(coach.anthropic, "Anthropic")
    def test_reply_with_spec_update_instruction(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = _mock_api_response(
            json.dumps({
                "message": "225 5x5 is huge!",
                "actions": [],
                "spec_update_instruction": "Update squat PR to 225x5x5",
            })
        )
        result = coach.generate_reply("# Spec", "", "", "Hit 225 for 5x5!", "Leg Day")
        assert result["spec_update_instruction"] is not None
        assert "225" in result["spec_update_instruction"]


class TestApplySpecUpdate:
    """Tests for apply_spec_update() — Haiku-based spec editing."""

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch.object(coach.anthropic, "Anthropic")
    def test_applies_instruction(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = _mock_api_response(
            "# Spec\n## PRs\n- Squat: 225x5x5"
        )

        result = coach.apply_spec_update("# Spec\n## PRs\n- Squat: 205x5x5", "Update squat PR to 225x5x5")
        assert result == "# Spec\n## PRs\n- Squat: 225x5x5"
        assert mock_client.messages.create.call_args[1]["model"] == "claude-haiku-4-5-20251001"

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch.object(coach.anthropic, "Anthropic")
    def test_strips_whitespace(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = _mock_api_response(
            "  # Spec\nUpdated  \n"
        )

        result = coach.apply_spec_update("# Spec", "some update")
        assert result == "# Spec\nUpdated"


class TestExecuteActions:
    """Tests for _execute_actions()."""

    def test_check_item(self):
        mock_trello = MagicMock()
        coach._execute_actions(mock_trello, "t1", [
            {"action": "check_item", "card_id": "c1", "item_id": "i1"},
        ])
        mock_trello.set_check_item_state.assert_called_once_with("c1", "i1", "complete")

    def test_uncheck_item(self):
        mock_trello = MagicMock()
        coach._execute_actions(mock_trello, "t1", [
            {"action": "uncheck_item", "card_id": "c1", "item_id": "i1"},
        ])
        mock_trello.set_check_item_state.assert_called_once_with("c1", "i1", "incomplete")

    def test_archive_card(self):
        mock_trello = MagicMock()
        coach._execute_actions(mock_trello, "t1", [
            {"action": "archive_card", "card_id": "c1"},
        ])
        mock_trello.archive_card.assert_called_once_with("c1")

    def test_move_card(self):
        mock_trello = MagicMock()
        mock_trello.get_list_id.return_value = "list_exercise"
        coach._execute_actions(mock_trello, "t1", [
            {"action": "move_card", "card_id": "c1", "list": "Exercise"},
        ])
        mock_trello.move_card.assert_called_once_with("c1", "list_exercise")

    def test_comment(self):
        mock_trello = MagicMock()
        coach._execute_actions(mock_trello, "t1", [
            {"action": "comment", "card_id": "c1", "text": "Great job"},
        ])
        mock_trello.add_comment.assert_called_once_with("c1", "Great job")

    def test_update_card(self):
        mock_trello = MagicMock()
        coach._execute_actions(mock_trello, "t1", [
            {"action": "update_card", "card_id": "c1", "name": "New Name", "desc": "New desc"},
        ])
        mock_trello.update_card.assert_called_once_with("c1", name="New Name", desc="New desc")

    def test_create_card(self):
        mock_trello = MagicMock()
        mock_trello.get_list_id.return_value = "list_nutrition"
        mock_trello.create_card.return_value = {"id": "new_card"}
        mock_trello.create_checklist.return_value = {"id": "cl_1"}
        coach._execute_actions(mock_trello, "t1", [
            {
                "action": "create_card",
                "list": "Nutrition",
                "title": "Grocery Run",
                "description": "Weekly groceries",
                "checklist": ["Chicken breast", "Rice", "Broccoli"],
                "comment": "Stock up!",
            },
        ])
        mock_trello.create_card.assert_called_once_with(
            "list_nutrition", "Grocery Run", "Weekly groceries",
        )
        assert mock_trello.add_checklist_item.call_count == 3
        mock_trello.add_comment.assert_called_once_with("new_card", "Stock up!")

    def test_create_card_minimal(self):
        mock_trello = MagicMock()
        mock_trello.get_list_id.return_value = "list_exercise"
        mock_trello.create_card.return_value = {"id": "new_card"}
        coach._execute_actions(mock_trello, "t1", [
            {"action": "create_card", "list": "Exercise", "title": "Leg Day"},
        ])
        mock_trello.create_card.assert_called_once_with(
            "list_exercise", "Leg Day", "",
        )
        mock_trello.create_checklist.assert_not_called()
        mock_trello.add_comment.assert_not_called()

    def test_multiple_actions(self):
        mock_trello = MagicMock()
        mock_trello.get_list_id.return_value = "list_forum"
        coach._execute_actions(mock_trello, "t1", [
            {"action": "check_item", "card_id": "c1", "item_id": "i1"},
            {"action": "archive_card", "card_id": "c2"},
            {"action": "move_card", "card_id": "c3", "list": "Forum"},
        ])
        mock_trello.set_check_item_state.assert_called_once()
        mock_trello.archive_card.assert_called_once_with("c2")
        mock_trello.move_card.assert_called_once_with("c3", "list_forum")

    def test_action_failure_continues(self):
        mock_trello = MagicMock()
        mock_trello.archive_card.side_effect = Exception("API error")
        # Should not raise — logs error and continues
        coach._execute_actions(mock_trello, "t1", [
            {"action": "archive_card", "card_id": "c1"},
            {"action": "check_item", "card_id": "c2", "item_id": "i1"},
        ])
        # Second action still executed despite first failing
        mock_trello.set_check_item_state.assert_called_once()

    def test_empty_actions(self):
        mock_trello = MagicMock()
        coach._execute_actions(mock_trello, "t1", [])
        # No Trello calls made
        mock_trello.assert_not_called()


class TestHandleMorning:
    """Tests for handle_morning()."""

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch.object(coach, "TrelloClient")
    @patch.object(coach.anthropic, "Anthropic")
    def test_creates_exercise_and_nutrition_cards(self, mock_api_cls, mock_trello_cls):
        mock_trello = MagicMock()
        mock_trello_cls.return_value = mock_trello
        mock_trello.get_board_desc.return_value = "# Spec"
        mock_trello.get_cards.return_value = []
        mock_trello.get_lists.return_value = []
        mock_trello.get_list_id.side_effect = lambda name: f"list_{name.lower()}"
        mock_trello.create_card.side_effect = [
            {"id": "exercise_card"},
            {"id": "nutrition_card"},
            {"id": "forum_card"},
        ]
        mock_trello.create_checklist.side_effect = [
            {"id": "cl_exercise"},
            {"id": "cl_nutrition"},
        ]

        mock_client = MagicMock()
        mock_api_cls.return_value = mock_client
        mock_client.messages.create.return_value = _mock_api_response(
            json.dumps({
                "exercise": {
                    "title": "Monday — Push Day",
                    "description": "## Bench\n4x8 @ 185",
                    "checklist": ["Bench 4x8 @ 185", "Incline DB 3x10 @ 70"],
                    "comment": "Time to push!",
                },
                "nutrition": {
                    "title": "Monday — Nutrition",
                    "description": "## Meals",
                    "checklist": ["Meal 1: Oatmeal + whey"],
                },
                "forum": {
                    "title": "Monday — Check In",
                    "description": "",
                    "comment": "How are you feeling today?",
                },
                "actions": [],
                "spec_update_instruction": None,
            })
        )

        coach.handle_morning("trace-1")

        assert mock_trello.create_card.call_count == 3
        # Exercise card
        mock_trello.create_card.assert_any_call(
            "list_exercise", "Monday — Push Day", "## Bench\n4x8 @ 185",
        )
        # Nutrition card
        mock_trello.create_card.assert_any_call(
            "list_nutrition", "Monday — Nutrition", "## Meals",
        )
        # Forum card
        mock_trello.create_card.assert_any_call(
            "list_forum", "Monday — Check In", "",
        )
        assert mock_trello.add_checklist_item.call_count == 3  # 2 exercise + 1 nutrition
        # Exercise comment + forum comment
        assert mock_trello.add_comment.call_count == 2

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch.object(coach, "TrelloClient")
    @patch.object(coach.anthropic, "Anthropic")
    def test_skips_null_cards(self, mock_api_cls, mock_trello_cls):
        mock_trello = MagicMock()
        mock_trello_cls.return_value = mock_trello
        mock_trello.get_board_desc.return_value = "# Spec"
        mock_trello.get_cards.return_value = []
        mock_trello.get_lists.return_value = []

        mock_client = MagicMock()
        mock_api_cls.return_value = mock_client
        mock_client.messages.create.return_value = _mock_api_response(
            json.dumps({
                "exercise": None,
                "nutrition": {
                    "title": "Rest Day — Nutrition",
                    "description": "Recovery meals",
                    "checklist": ["High protein meals"],
                },
                "actions": [],
                "spec_update_instruction": None,
            })
        )

        mock_trello.get_list_id.side_effect = lambda name: f"list_{name.lower()}"
        mock_trello.create_card.return_value = {"id": "nutr_card"}
        mock_trello.create_checklist.return_value = {"id": "cl_1"}

        coach.handle_morning("trace-2")

        # Only nutrition card created
        mock_trello.create_card.assert_called_once()
        mock_trello.move_card.assert_not_called()


class TestHandleReply:
    """Tests for handle_reply()."""

    @patch.dict("os.environ", {
        "ANTHROPIC_API_KEY": "test-key",
        "COMMENT_TEXT": "Hit 225 on squats today",
        "CARD_ID": "card_abc",
    })
    @patch.object(coach, "TrelloClient")
    @patch.object(coach.anthropic, "Anthropic")
    def test_replies_to_comment(self, mock_api_cls, mock_trello_cls):
        mock_trello = MagicMock()
        mock_trello_cls.return_value = mock_trello
        mock_trello.get_board_desc.return_value = "# Spec"
        mock_trello.get_lists.return_value = []
        mock_trello.get_cards.return_value = []
        mock_trello.get_card.return_value = {"name": "Monday — Push", "desc": "Bench day"}
        mock_trello.get_card_comments.return_value = []

        mock_client = MagicMock()
        mock_api_cls.return_value = mock_client
        # First call: generate_reply (Opus), second call: apply_spec_update (Haiku)
        mock_client.messages.create.side_effect = [
            _mock_api_response(json.dumps({
                "message": "225 is a huge PR!",
                "actions": [{"action": "check_item", "card_id": "card_abc", "item_id": "ci1"}],
                "spec_update_instruction": "Update squat PR to 225x5x5",
            })),
            _mock_api_response("# Updated\n- Squat: 225"),
        ]

        coach.handle_reply("trace-3")

        mock_trello.add_comment.assert_called_once_with(
            "card_abc", "225 is a huge PR!"
        )
        # Check item action executed
        mock_trello.set_check_item_state.assert_called_once_with("card_abc", "ci1", "complete")
        # Spec updated via Haiku
        mock_trello.update_board_desc.assert_called_once_with("# Updated\n- Squat: 225")

    @patch.dict("os.environ", {"COMMENT_TEXT": "", "CARD_ID": ""})
    def test_skips_empty_comment(self):
        # Should not raise — just skips
        coach.handle_reply("trace-4")


class TestConsolidateSpec:
    """Tests for consolidate_spec() — sawtooth compression."""

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch.object(coach.anthropic, "Anthropic")
    def test_consolidates_spec(self, mock_cls):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = _mock_api_response(
            "# Compressed Spec\n- Goals: gain mass"
        )

        result = coach.consolidate_spec("x" * 15000)
        assert result == "# Compressed Spec\n- Goals: gain mass"
        assert mock_client.messages.create.call_args[1]["model"] == "claude-haiku-4-5-20251001"
        # Verify the prompt mentions the target size
        prompt = mock_client.messages.create.call_args[1]["messages"][0]["content"]
        assert "10000" in prompt or "10,000" in prompt

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch.object(coach.anthropic, "Anthropic")
    def test_apply_spec_triggers_consolidation(self, mock_cls):
        """Spec update producing >14K chars should trigger consolidation."""
        mock_trello = MagicMock()
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        # First call: apply_spec_update returns oversized spec
        # Second call: consolidate_spec returns compressed spec
        big_spec = "# Big\n" + "x" * 14500
        mock_client.messages.create.side_effect = [
            _mock_api_response(big_spec),
            _mock_api_response("# Compressed"),
        ]

        coach._apply_spec_if_needed(mock_trello, "t1", "# Spec", "add lots of stuff")

        assert mock_client.messages.create.call_count == 2
        mock_trello.update_board_desc.assert_called_once_with("# Compressed")

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch.object(coach.anthropic, "Anthropic")
    def test_apply_spec_skips_consolidation_under_threshold(self, mock_cls):
        """Spec update under 14K chars should not trigger consolidation."""
        mock_trello = MagicMock()
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = _mock_api_response("# Small spec")

        coach._apply_spec_if_needed(mock_trello, "t1", "# Spec", "small tweak")

        # Only one LLM call (apply_spec_update), no consolidation
        assert mock_client.messages.create.call_count == 1
        mock_trello.update_board_desc.assert_called_once_with("# Small spec")


class TestTrelloClientRequest:
    """Tests for TrelloClient._request: retries and credential sanitization."""

    @patch.dict("os.environ", {
        "TRELLO_API_KEY": "secret-key-123",
        "TRELLO_TOKEN": "secret-token-456",
        "TRELLO_COACH_BOARD_ID": "board123",
    })
    @patch.object(coach.requests, "request")
    def test_sanitizes_credentials_on_4xx(self, mock_request):
        """HTTP 4xx errors should not contain API key or token."""
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.reason = "Bad Request"
        mock_response.url = (
            "https://api.trello.com/1/boards/board123"
            "?key=secret-key-123&token=secret-token-456"
        )
        mock_response.raise_for_status.side_effect = requests.HTTPError(
            response=mock_response,
        )
        mock_request.return_value = mock_response

        client = coach.TrelloClient()
        try:
            client.get_board_desc()
            assert False, "Should have raised"
        except requests.HTTPError as e:
            error_msg = str(e)
            assert "secret-key-123" not in error_msg
            assert "secret-token-456" not in error_msg
            assert "400" in error_msg

    @patch.dict("os.environ", {
        "TRELLO_API_KEY": "k",
        "TRELLO_TOKEN": "t",
        "TRELLO_COACH_BOARD_ID": "b",
    })
    @patch.object(coach.requests, "request")
    def test_no_retry_on_4xx(self, mock_request):
        """Client errors (4xx) should fail immediately without retries."""
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.reason = "Bad Request"
        mock_response.url = "https://api.trello.com/1/boards/b"
        mock_response.raise_for_status.side_effect = requests.HTTPError(
            response=mock_response,
        )
        mock_request.return_value = mock_response

        client = coach.TrelloClient()
        try:
            client.get_board_desc()
        except requests.HTTPError:
            pass
        assert mock_request.call_count == 1

    @patch.dict("os.environ", {
        "TRELLO_API_KEY": "k",
        "TRELLO_TOKEN": "t",
        "TRELLO_COACH_BOARD_ID": "b",
    })
    @patch.object(coach.time, "sleep")
    @patch.object(coach.requests, "request")
    def test_retries_on_connection_error(self, mock_request, mock_sleep):
        """ConnectionError should be retried up to 3 times."""
        mock_request.side_effect = requests.ConnectionError("Connection reset")

        client = coach.TrelloClient()
        try:
            client.get_board_desc()
        except requests.ConnectionError:
            pass
        assert mock_request.call_count == 3
        assert mock_sleep.call_count == 2  # sleeps between retries

    @patch.dict("os.environ", {
        "TRELLO_API_KEY": "k",
        "TRELLO_TOKEN": "t",
        "TRELLO_COACH_BOARD_ID": "b",
    })
    @patch.object(coach.time, "sleep")
    @patch.object(coach.requests, "request")
    def test_retries_on_5xx(self, mock_request, mock_sleep):
        """Server errors (5xx) should be retried."""
        mock_response = MagicMock()
        mock_response.status_code = 502
        mock_response.reason = "Bad Gateway"
        mock_response.url = "https://api.trello.com/1/boards/b"
        mock_response.raise_for_status.side_effect = requests.HTTPError(
            response=mock_response,
        )
        mock_request.return_value = mock_response

        client = coach.TrelloClient()
        try:
            client.get_board_desc()
        except requests.HTTPError:
            pass
        assert mock_request.call_count == 3

    @patch.dict("os.environ", {
        "TRELLO_API_KEY": "k",
        "TRELLO_TOKEN": "t",
        "TRELLO_COACH_BOARD_ID": "b",
    })
    @patch.object(coach.time, "sleep")
    @patch.object(coach.requests, "request")
    def test_retry_succeeds_on_second_attempt(self, mock_request, mock_sleep):
        """Should return successfully if retry succeeds."""
        ok_response = MagicMock()
        ok_response.status_code = 200
        ok_response.raise_for_status.return_value = None
        ok_response.json.return_value = {"desc": "# Spec"}

        mock_request.side_effect = [
            requests.ConnectionError("Connection reset"),
            ok_response,
        ]

        client = coach.TrelloClient()
        result = client.get_board_desc()
        assert result == "# Spec"
        assert mock_request.call_count == 2
