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


class TestReadMemories:
    """Tests for read_memories()."""

    def test_reads_memory_cards_and_comments(self):
        mock_trello = MagicMock()
        mock_trello.get_lists.return_value = [
            {"id": "list1", "name": "Exercise"},
            {"id": "mem_list", "name": "Memories"},
        ]
        mock_trello.get_cards.return_value = [
            {"id": "c1", "name": "Push Day", "desc": "", "idList": "list1"},
            {"id": "m1", "name": "Personal Records", "desc": "", "idList": "mem_list"},
        ]
        mock_trello.get_card_comments.return_value = [
            {
                "date": "2026-02-10T14:00:00",
                "data": {"text": "**[Coach]** Squat PR: 225x5x5"},
            },
            {
                "date": "2026-02-15T10:00:00",
                "data": {"text": "**[Coach]** Bench PR: 185x4x8"},
            },
        ]

        result = coach.read_memories(mock_trello)
        assert "Personal Records" in result
        assert "card_id: m1" in result
        assert "Squat PR: 225x5x5" in result
        assert "Bench PR: 185x4x8" in result
        # Coach prefix should be stripped
        assert "**[Coach]**" not in result
        # Non-memory cards should not appear
        assert "Push Day" not in result

    def test_returns_empty_when_no_memories_list(self):
        mock_trello = MagicMock()
        mock_trello.get_lists.return_value = [
            {"id": "list1", "name": "Exercise"},
        ]

        result = coach.read_memories(mock_trello)
        assert result == ""

    def test_returns_empty_when_no_memory_cards(self):
        mock_trello = MagicMock()
        mock_trello.get_lists.return_value = [
            {"id": "mem_list", "name": "Memories"},
        ]
        mock_trello.get_cards.return_value = []

        result = coach.read_memories(mock_trello)
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


class TestBuildBoardSummary:
    """Tests for build_board_summary()."""

    def test_formats_cards_with_list_and_activity(self):
        mock_trello = MagicMock()
        mock_trello.get_lists.return_value = [
            {"id": "list1", "name": "Exercise"},
        ]
        mock_trello.get_cards.return_value = [
            {
                "id": "c1", "name": "Push Day", "idList": "list1",
                "dateLastActivity": "2026-02-06T14:00:00.000Z",
            },
        ]

        result = coach.build_board_summary(mock_trello)
        assert "[Exercise] Push Day" in result
        assert "card_id: c1" in result
        assert "last_activity:" in result

    def test_excludes_memories_cards(self):
        mock_trello = MagicMock()
        mock_trello.get_lists.return_value = [
            {"id": "list1", "name": "Exercise"},
            {"id": "mem_list", "name": "Memories"},
        ]
        mock_trello.get_cards.return_value = [
            {"id": "c1", "name": "Push Day", "idList": "list1", "dateLastActivity": ""},
            {"id": "c2", "name": "PRs", "idList": "mem_list", "dateLastActivity": ""},
        ]

        result = coach.build_board_summary(mock_trello)
        assert "Push Day" in result
        assert "PRs" not in result

    def test_empty_board(self):
        mock_trello = MagicMock()
        mock_trello.get_lists.return_value = []
        mock_trello.get_cards.return_value = []

        result = coach.build_board_summary(mock_trello)
        assert result == ""


def _mock_api_response(text):
    """Create a mock Anthropic API response with the given text."""
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


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


class TestExecuteTool:
    """Tests for execute_tool()."""

    def test_read_card(self):
        mock_trello = MagicMock()
        mock_trello.get_card.return_value = {"name": "Push Day", "desc": "Bench"}
        mock_trello.get_card_checklists.return_value = [
            {"checkItems": [
                {"id": "i1", "name": "Bench 4x8", "state": "complete"},
                {"id": "i2", "name": "Incline 3x10", "state": "incomplete"},
            ]},
        ]
        mock_trello.get_card_comments.return_value = [
            {"date": "2026-02-06T14:00:00", "data": {"text": "Done!"}},
        ]

        result = coach.execute_tool(mock_trello, "t1", "read_card", {"card_id": "c1"})
        assert "Push Day" in result
        assert "[x] Bench 4x8" in result
        assert "[ ] Incline 3x10" in result
        assert "item_id: i1" in result
        assert "Done!" in result

    def test_archive_card(self):
        mock_trello = MagicMock()
        result = coach.execute_tool(mock_trello, "t1", "archive_card", {"card_id": "c1"})
        mock_trello.archive_card.assert_called_once_with("c1")
        assert "archived" in result.lower()

    def test_check_item(self):
        mock_trello = MagicMock()
        result = coach.execute_tool(
            mock_trello, "t1", "check_item", {"card_id": "c1", "item_id": "i1"},
        )
        mock_trello.set_check_item_state.assert_called_once_with("c1", "i1", "complete")
        assert "checked" in result.lower()

    def test_uncheck_item(self):
        mock_trello = MagicMock()
        result = coach.execute_tool(
            mock_trello, "t1", "uncheck_item", {"card_id": "c1", "item_id": "i1"},
        )
        mock_trello.set_check_item_state.assert_called_once_with("c1", "i1", "incomplete")
        assert "unchecked" in result.lower()

    def test_move_card(self):
        mock_trello = MagicMock()
        mock_trello.get_list_id.return_value = "list_exercise"
        result = coach.execute_tool(
            mock_trello, "t1", "move_card", {"card_id": "c1", "list_name": "Exercise"},
        )
        mock_trello.move_card.assert_called_once_with("c1", "list_exercise")
        assert "Exercise" in result

    def test_comment_on_card(self):
        mock_trello = MagicMock()
        result = coach.execute_tool(
            mock_trello, "t1", "comment_on_card", {"card_id": "c1", "text": "Nice!"},
        )
        mock_trello.add_comment.assert_called_once_with("c1", "Nice!")
        assert "posted" in result.lower()

    def test_update_card(self):
        mock_trello = MagicMock()
        result = coach.execute_tool(
            mock_trello, "t1", "update_card",
            {"card_id": "c1", "name": "New Name", "desc": "New desc"},
        )
        mock_trello.update_card.assert_called_once_with("c1", name="New Name", desc="New desc")
        assert "updated" in result.lower()

    def test_create_card(self):
        mock_trello = MagicMock()
        mock_trello.get_list_id.return_value = "list_exercise"
        mock_trello.create_card.return_value = {"id": "new_card"}
        mock_trello.create_checklist.return_value = {"id": "cl1"}
        result = coach.execute_tool(mock_trello, "t1", "create_card", {
            "list_name": "Exercise",
            "title": "Leg Day",
            "description": "Squats",
            "checklist": ["Squat 4x8"],
            "comment": "Go heavy!",
        })
        mock_trello.create_card.assert_called_once()
        assert "new_card" in result

    def test_create_card_minimal(self):
        mock_trello = MagicMock()
        mock_trello.get_list_id.return_value = "list_exercise"
        mock_trello.create_card.return_value = {"id": "new_card"}
        result = coach.execute_tool(mock_trello, "t1", "create_card", {
            "list_name": "Exercise",
            "title": "Leg Day",
        })
        mock_trello.create_card.assert_called_once()
        mock_trello.create_checklist.assert_not_called()
        mock_trello.add_comment.assert_not_called()

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch.object(coach.anthropic, "Anthropic")
    def test_update_spec(self, mock_cls):
        mock_trello = MagicMock()
        mock_trello.get_board_desc.return_value = "# Spec"
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = _mock_api_response("# Updated Spec")

        result = coach.execute_tool(
            mock_trello, "t1", "update_spec", {"instruction": "Add PR"},
        )
        assert "updated" in result.lower()
        mock_trello.update_board_desc.assert_called_once()

    def test_add_reaction(self):
        mock_trello = MagicMock()
        result = coach.execute_tool(
            mock_trello, "t1", "add_reaction",
            {"action_id": "a1", "emoji": "muscle"},
        )
        mock_trello.add_reaction.assert_called_once_with("a1", "muscle")
        assert "reaction" in result.lower()

    def test_tool_error_returns_string(self):
        mock_trello = MagicMock()
        mock_trello.archive_card.side_effect = Exception("API error")
        result = coach.execute_tool(mock_trello, "t1", "archive_card", {"card_id": "c1"})
        assert "Error:" in result

    def test_unknown_tool(self):
        mock_trello = MagicMock()
        result = coach.execute_tool(mock_trello, "t1", "unknown_tool", {})
        assert "Unknown tool" in result


class TestRunAgent:
    """Tests for run_agent()."""

    def test_single_turn_end(self):
        """Agent that ends immediately without tools."""
        mock_client = MagicMock()
        mock_trello = MagicMock()

        response = MagicMock()
        response.stop_reason = "end_turn"
        response.content = [MagicMock(type="text", text="Done")]
        mock_client.messages.create.return_value = response

        coach.run_agent(mock_client, mock_trello, "t1", "system", "user")
        assert mock_client.messages.create.call_count == 1

    def test_tool_use_then_end(self):
        """Agent that uses a tool then ends."""
        mock_client = MagicMock()
        mock_trello = MagicMock()
        mock_trello.archive_card.return_value = {}

        # First response: tool_use
        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.id = "tu_1"
        tool_block.name = "archive_card"
        tool_block.input = {"card_id": "c1"}

        first_response = MagicMock()
        first_response.stop_reason = "tool_use"
        first_response.content = [tool_block]

        # Second response: end_turn
        second_response = MagicMock()
        second_response.stop_reason = "end_turn"
        second_response.content = [MagicMock(type="text", text="Done")]

        mock_client.messages.create.side_effect = [first_response, second_response]

        coach.run_agent(mock_client, mock_trello, "t1", "system", "user")

        assert mock_client.messages.create.call_count == 2
        mock_trello.archive_card.assert_called_once_with("c1")

    def test_max_iterations_cap(self):
        """Agent that loops forever hits the 25-iteration cap."""
        mock_client = MagicMock()
        mock_trello = MagicMock()
        mock_trello.archive_card.return_value = {}

        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.id = "tu_1"
        tool_block.name = "archive_card"
        tool_block.input = {"card_id": "c1"}

        response = MagicMock()
        response.stop_reason = "tool_use"
        response.content = [tool_block]

        mock_client.messages.create.return_value = response

        coach.run_agent(mock_client, mock_trello, "t1", "system", "user")
        assert mock_client.messages.create.call_count == 25

    def test_unexpected_stop_reason(self):
        """Agent handles unexpected stop reasons gracefully."""
        mock_client = MagicMock()
        mock_trello = MagicMock()

        response = MagicMock()
        response.stop_reason = "max_tokens"
        response.content = [MagicMock(type="text", text="truncated")]
        mock_client.messages.create.return_value = response

        coach.run_agent(mock_client, mock_trello, "t1", "system", "user")
        assert mock_client.messages.create.call_count == 1

    def test_multi_step_read_then_create(self):
        """Agent reads a card, then creates a new card, then ends."""
        mock_client = MagicMock()
        mock_trello = MagicMock()
        mock_trello.get_card.return_value = {"name": "Old Card", "desc": "Details"}
        mock_trello.get_card_checklists.return_value = []
        mock_trello.get_card_comments.return_value = []
        mock_trello.get_list_id.return_value = "list_exercise"
        mock_trello.create_card.return_value = {"id": "new_card_1"}

        # Turn 1: read_card
        read_block = MagicMock()
        read_block.type = "tool_use"
        read_block.id = "tu_1"
        read_block.name = "read_card"
        read_block.input = {"card_id": "c1"}
        resp1 = MagicMock(stop_reason="tool_use", content=[read_block])

        # Turn 2: create_card
        create_block = MagicMock()
        create_block.type = "tool_use"
        create_block.id = "tu_2"
        create_block.name = "create_card"
        create_block.input = {"list_name": "Exercise", "title": "Push Day"}
        resp2 = MagicMock(stop_reason="tool_use", content=[create_block])

        # Turn 3: done
        resp3 = MagicMock(stop_reason="end_turn")
        resp3.content = [MagicMock(type="text", text="All done")]

        mock_client.messages.create.side_effect = [resp1, resp2, resp3]

        coach.run_agent(mock_client, mock_trello, "t1", "system prompt", "user msg")

        assert mock_client.messages.create.call_count == 3
        mock_trello.get_card.assert_called_once_with("c1")
        mock_trello.create_card.assert_called_once()
        # Verify message threading: 3rd call should have 5 messages
        # (user, assistant+tool_use, tool_result, assistant+tool_use, tool_result)
        third_call_messages = mock_client.messages.create.call_args_list[2][1]["messages"]
        assert len(third_call_messages) == 5
        assert third_call_messages[0]["role"] == "user"
        assert third_call_messages[1]["role"] == "assistant"
        assert third_call_messages[2]["role"] == "user"  # tool_result
        assert third_call_messages[3]["role"] == "assistant"
        assert third_call_messages[4]["role"] == "user"  # tool_result

    def test_parallel_tool_use(self):
        """Agent calls two tools in one response (comment + reaction)."""
        mock_client = MagicMock()
        mock_trello = MagicMock()

        comment_block = MagicMock()
        comment_block.type = "tool_use"
        comment_block.id = "tu_1"
        comment_block.name = "comment_on_card"
        comment_block.input = {"card_id": "c1", "text": "Nice work!"}
        reaction_block = MagicMock()
        reaction_block.type = "tool_use"
        reaction_block.id = "tu_2"
        reaction_block.name = "add_reaction"
        reaction_block.input = {"action_id": "a1", "emoji": "muscle"}

        resp1 = MagicMock(stop_reason="tool_use", content=[comment_block, reaction_block])
        resp2 = MagicMock(stop_reason="end_turn")
        resp2.content = [MagicMock(type="text", text="Done")]

        mock_client.messages.create.side_effect = [resp1, resp2]

        coach.run_agent(mock_client, mock_trello, "t1", "system", "user")

        assert mock_client.messages.create.call_count == 2
        mock_trello.add_comment.assert_called_once_with("c1", "Nice work!")
        mock_trello.add_reaction.assert_called_once_with("a1", "muscle")
        # Both tool results sent back in one message
        second_call_messages = mock_client.messages.create.call_args_list[1][1]["messages"]
        tool_result_msg = second_call_messages[2]
        assert tool_result_msg["role"] == "user"
        assert len(tool_result_msg["content"]) == 2
        assert tool_result_msg["content"][0]["tool_use_id"] == "tu_1"
        assert tool_result_msg["content"][1]["tool_use_id"] == "tu_2"

    def test_tool_error_flows_back_to_llm(self):
        """Tool failure returns error string; LLM sees it and ends."""
        mock_client = MagicMock()
        mock_trello = MagicMock()
        mock_trello.archive_card.side_effect = Exception("Card not found")

        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.id = "tu_1"
        tool_block.name = "archive_card"
        tool_block.input = {"card_id": "bad_id"}
        resp1 = MagicMock(stop_reason="tool_use", content=[tool_block])

        resp2 = MagicMock(stop_reason="end_turn")
        resp2.content = [MagicMock(type="text", text="That card doesn't exist")]

        mock_client.messages.create.side_effect = [resp1, resp2]

        coach.run_agent(mock_client, mock_trello, "t1", "system", "user")

        assert mock_client.messages.create.call_count == 2
        # Verify error string was sent back as tool_result
        second_call_messages = mock_client.messages.create.call_args_list[1][1]["messages"]
        tool_result_content = second_call_messages[2]["content"][0]["content"]
        assert "Error:" in tool_result_content
        assert "Card not found" in tool_result_content


class TestHandleMorning:
    """Tests for handle_morning()."""

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch.object(coach, "run_agent")
    @patch.object(coach, "_get_client")
    @patch.object(coach, "TrelloClient")
    def test_calls_run_agent_with_context(self, mock_trello_cls, mock_get_client, mock_run_agent):
        mock_trello = MagicMock()
        mock_trello_cls.return_value = mock_trello
        mock_trello.get_board_desc.return_value = "# Spec"
        mock_trello.get_cards.return_value = []
        mock_trello.get_lists.return_value = []

        coach.handle_morning("trace-1")

        mock_run_agent.assert_called_once()
        call_args = mock_run_agent.call_args[0]
        system_prompt = call_args[3]
        user_message = call_args[4]
        assert "coach" in system_prompt.lower()
        assert "create_card" in system_prompt
        assert "# Spec" in user_message


class TestHandleReply:
    """Tests for handle_reply()."""

    @patch.dict("os.environ", {
        "ANTHROPIC_API_KEY": "test-key",
        "COMMENT_TEXT": "Hit 225 on squats today",
        "CARD_ID": "card_abc",
        "ACTION_ID": "action_123",
    })
    @patch.object(coach, "run_agent")
    @patch.object(coach, "_get_client")
    @patch.object(coach, "TrelloClient")
    def test_calls_run_agent_and_reacts(self, mock_trello_cls, mock_get_client, mock_run_agent):
        mock_trello = MagicMock()
        mock_trello_cls.return_value = mock_trello
        mock_trello.get_board_desc.return_value = "# Spec"
        mock_trello.get_cards.return_value = []
        mock_trello.get_lists.return_value = []
        mock_trello.get_card.return_value = {"name": "Leg Day", "desc": "Squats"}
        mock_trello.get_card_comments.return_value = []

        coach.handle_reply("trace-3")

        mock_run_agent.assert_called_once()
        call_args = mock_run_agent.call_args[0]
        system_prompt = call_args[3]
        user_message = call_args[4]
        assert "coach" in system_prompt.lower()
        assert "comment_on_card" in system_prompt
        assert "card_abc" in system_prompt
        assert "add_reaction" in system_prompt
        assert "action_123" in system_prompt
        assert "Hit 225" in user_message
        assert "card_abc" in user_message
        # Checkmark reaction added after agent completes
        mock_trello.add_reaction.assert_called_once_with("action_123", "white_check_mark")

    @patch.dict("os.environ", {
        "ANTHROPIC_API_KEY": "test-key",
        "COMMENT_TEXT": "Hello",
        "CARD_ID": "card_abc",
        "ACTION_ID": "",
    })
    @patch.object(coach, "run_agent")
    @patch.object(coach, "_get_client")
    @patch.object(coach, "TrelloClient")
    def test_skips_reaction_without_action_id(self, mock_trello_cls, mock_get_client, mock_run_agent):
        mock_trello = MagicMock()
        mock_trello_cls.return_value = mock_trello
        mock_trello.get_board_desc.return_value = "# Spec"
        mock_trello.get_cards.return_value = []
        mock_trello.get_lists.return_value = []
        mock_trello.get_card.return_value = {"name": "Forum", "desc": ""}
        mock_trello.get_card_comments.return_value = []

        coach.handle_reply("trace-5")

        mock_run_agent.assert_called_once()
        # No add_reaction instruction in system prompt
        system_prompt = mock_run_agent.call_args[0][3]
        assert "add_reaction" not in system_prompt
        # No reaction call
        mock_trello.add_reaction.assert_not_called()

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
        assert mock_client.messages.create.call_args[1]["model"] == "claude-opus-4-6"
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
