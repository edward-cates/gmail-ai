"""Tests for Slack processor Cloud Run Job."""

import json
import os
import sys
from unittest.mock import MagicMock, patch

# Add cloud-run/slack-processor to path so we can import it standalone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud-run", "slack-processor"))

import main as processor  # noqa: E402


class TestBatchClassification:
    """Tests for batch_classify_messages()."""

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch("main.ChatAnthropic")
    def test_single_message_classification(self, mock_anthropic_cls):
        mock_llm = MagicMock()
        mock_anthropic_cls.return_value = mock_llm

        mock_response = MagicMock()
        mock_response.content = json.dumps([{
            "msg_idx": 1,
            "existing_topic_id": None,
            "topic_name": "API design discussion",
            "priority": "worth_reading",
            "action_items": [],
            "summary": "Team discussing API design choices.",
        }])
        mock_llm.invoke.return_value = mock_response

        messages = [{"idx": 1, "text": "Should we use REST or GraphQL?", "sender": "Alice", "channel": "backend"}]
        topics = [{"id": "card1", "name": "Sprint planning", "list_name": "Worth Reading"}]

        result = processor.batch_classify_messages(messages, topics)

        assert len(result) == 1
        assert result[0]["topic_name"] == "API design discussion"
        assert result[0]["priority"] == "worth_reading"
        # Verify Opus model used
        mock_anthropic_cls.assert_called_once()
        assert mock_anthropic_cls.call_args[1]["model"] == "claude-opus-4-6"

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch("main.ChatAnthropic")
    def test_batch_of_three_messages(self, mock_anthropic_cls):
        mock_llm = MagicMock()
        mock_anthropic_cls.return_value = mock_llm

        mock_response = MagicMock()
        mock_response.content = json.dumps([
            {"msg_idx": 1, "existing_topic_id": "card1", "topic_name": "Sprint", "priority": "worth_reading", "action_items": [], "summary": "Sprint update"},
            {"msg_idx": 2, "existing_topic_id": None, "topic_name": "New feature", "priority": "needs_response", "action_items": ["Review the PR"], "summary": "PR needs review"},
            {"msg_idx": 3, "existing_topic_id": "card1", "topic_name": "Sprint", "priority": "action_required", "action_items": [], "summary": "More sprint details"},
        ])
        mock_llm.invoke.return_value = mock_response

        messages = [
            {"idx": 1, "text": "Sprint going well", "sender": "Alice", "channel": "general"},
            {"idx": 2, "text": "Can you review my PR?", "sender": "Bob", "channel": "backend"},
            {"idx": 3, "text": "Sprint blockers", "sender": "Carol", "channel": "general"},
        ]
        topics = [{"id": "card1", "name": "Sprint planning", "list_name": "Worth Reading"}]

        result = processor.batch_classify_messages(messages, topics)

        assert len(result) == 3
        # First and third match existing topic
        assert result[0]["existing_topic_id"] == "card1"
        assert result[2]["existing_topic_id"] == "card1"
        # Second is new with action item
        assert result[1]["existing_topic_id"] is None
        assert result[1]["action_items"] == ["Review the PR"]
        # Only ONE call to Claude
        assert mock_llm.invoke.call_count == 1

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch("main.ChatAnthropic")
    def test_fallback_on_parse_error(self, mock_anthropic_cls):
        mock_llm = MagicMock()
        mock_anthropic_cls.return_value = mock_llm

        mock_response = MagicMock()
        mock_response.content = "this is not json"
        mock_llm.invoke.return_value = mock_response

        messages = [{"idx": 1, "text": "hello", "sender": "Alice", "channel": "general"}]
        result = processor.batch_classify_messages(messages, [])

        assert len(result) == 1
        assert result[0]["priority"] == "worth_reading"
        assert result[0]["existing_topic_id"] is None


class TestDescriptionUpdates:
    """Tests for Haiku description update functions."""

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch("main.ChatAnthropic")
    def test_summary_update(self, mock_anthropic_cls):
        mock_llm = MagicMock()
        mock_anthropic_cls.return_value = mock_llm

        mock_response = MagicMock()
        mock_response.content = "**Summary**\nUpdated summary.\n\n**Reactions**\nNo reactions yet."
        mock_llm.invoke.return_value = mock_response

        result = processor.update_description_summary(
            "", "backend", "We should refactor the auth module", "Alice"
        )

        assert "**Summary**" in result
        assert "**Reactions**" in result
        # Verify Haiku model used
        assert mock_anthropic_cls.call_args[1]["model"] == "claude-3-5-haiku-20241022"

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch("main.ChatAnthropic")
    def test_reactions_update(self, mock_anthropic_cls):
        mock_llm = MagicMock()
        mock_anthropic_cls.return_value = mock_llm

        mock_response = MagicMock()
        mock_response.content = "**Summary**\nExisting summary.\n\n**Reactions**\nAlice liked your refactoring idea."
        mock_llm.invoke.return_value = mock_response

        current_desc = "**Summary**\nExisting summary.\n\n**Reactions**\nNo reactions yet."
        result = processor.update_description_reactions(
            current_desc, "Alice", "thumbsup", "Let's refactor auth"
        )

        assert "**Summary**" in result
        assert "Alice" in result


class TestReactionFiltering:
    """Tests for reaction processing — only reacts on user's own messages."""

    @patch.dict("os.environ", {
        "ANTHROPIC_API_KEY": "k", "SLACK_BOT_TOKEN": "t",
        "TRELLO_API_KEY": "k", "TRELLO_TOKEN": "t", "TRELLO_BOARD_ID": "b",
        "GMAIL_AI_STORAGE_BUCKET": "bucket", "GMAIL_AI_PROJECT_ID": "proj",
    })
    @patch("main.update_description_reactions")
    @patch("main.requests")
    def test_reaction_on_others_message_skipped(self, mock_requests, mock_update):
        """Reactions on other people's messages should be skipped."""
        # Mock Slack API responses
        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if "auth.test" in url:
                resp.json.return_value = {"ok": True, "user_id": "U_ME"}
            elif "conversations.history" in url:
                resp.json.return_value = {"ok": True, "messages": [
                    {"user": "U_OTHER", "text": "someone else's message"}
                ]}
            else:
                resp.json.return_value = {"ok": True}
            return resp
        mock_requests.get.side_effect = mock_get

        slack = processor.SlackClient()
        trello = MagicMock()
        cards = []
        list_map = {}

        events = [{
            "event": {
                "type": "reaction_added",
                "user": "U_REACTOR",
                "reaction": "thumbsup",
                "item": {"channel": "C123", "ts": "111.222"},
            },
            "trace_id": "test-trace",
        }]

        processor.process_reactions(events, slack, trello, cards, list_map, "batch-trace")
        mock_update.assert_not_called()

    @patch.dict("os.environ", {
        "ANTHROPIC_API_KEY": "k", "SLACK_BOT_TOKEN": "t",
        "TRELLO_API_KEY": "k", "TRELLO_TOKEN": "t", "TRELLO_BOARD_ID": "b",
        "GMAIL_AI_STORAGE_BUCKET": "bucket", "GMAIL_AI_PROJECT_ID": "proj",
    })
    @patch("main.update_description_reactions")
    @patch("main.requests")
    def test_reaction_on_my_message_processed(self, mock_requests, mock_update):
        """Reactions on my messages should update the card description."""
        mock_update.return_value = "**Summary**\ntest\n\n**Reactions**\nPositive."

        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if "auth.test" in url:
                resp.json.return_value = {"ok": True, "user_id": "U_ME"}
            elif "conversations.history" in url:
                resp.json.return_value = {"ok": True, "messages": [
                    {"user": "U_ME", "text": "my message"}
                ]}
            elif "users.info" in url:
                resp.json.return_value = {"ok": True, "user": {"real_name": "Reactor"}}
            elif "conversations.info" in url:
                resp.json.return_value = {"ok": True, "channel": {"name": "general"}}
            else:
                resp.json.return_value = {"ok": True}
            return resp
        mock_requests.get.side_effect = mock_get

        # Mock Trello
        mock_trello = MagicMock()
        cards = [{"id": "card1", "name": "Test Topic", "idList": "list1", "desc": "#general | summary"}]
        list_map = {"list1": "Worth Reading"}

        events = [{
            "event": {
                "type": "reaction_added",
                "user": "U_REACTOR",
                "reaction": "thumbsup",
                "item": {"channel": "C123", "ts": "111.222"},
            },
            "trace_id": "test-trace",
        }]

        processor.process_reactions(events, MagicMock(
            get_authed_user_id=MagicMock(return_value="U_ME"),
            get_message=MagicMock(return_value={"user": "U_ME", "text": "my message"}),
            get_user_name=MagicMock(return_value="Reactor"),
            get_channel_name=MagicMock(return_value="general"),
        ), mock_trello, cards, list_map, "batch-trace")

        mock_update.assert_called_once()
        mock_trello.update_card_desc.assert_called_once()


class TestNewCardTracking:
    """Tests for batch-internal card deduplication."""

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k", "SLACK_BOT_TOKEN": "t", "TRELLO_API_KEY": "k", "TRELLO_TOKEN": "t", "TRELLO_BOARD_ID": "b"})
    @patch("main.update_description_summary")
    def test_second_message_reuses_card_from_batch(self, mock_summary):
        """Two messages about the same new topic should use one card."""
        mock_summary.return_value = "**Summary**\ntest\n\n**Reactions**\nNo reactions yet."

        mock_trello = MagicMock()
        mock_trello.get_list_id.return_value = "list1"
        mock_trello.create_card.return_value = {"id": "new_card_1"}
        mock_trello.get_checklists.return_value = []

        cards = []
        list_map = {}
        new_cards_by_topic = {}

        # First message creates the card
        classification1 = {
            "existing_topic_id": None,
            "topic_name": "New Feature",
            "priority": "worth_reading",
            "action_items": [],
        }
        msg1 = {"sender": "Alice", "channel": "general", "text": "New feature idea", "channel_id": "C1", "user_id": "U1"}

        processor._apply_classification(classification1, msg1, "t1", mock_trello, cards, list_map, new_cards_by_topic)
        assert mock_trello.create_card.call_count == 1

        # Second message with same topic name should reuse
        classification2 = {
            "existing_topic_id": None,
            "topic_name": "New Feature",
            "priority": "worth_reading",
            "action_items": [],
        }
        msg2 = {"sender": "Bob", "channel": "general", "text": "I agree on the feature", "channel_id": "C1", "user_id": "U2"}

        processor._apply_classification(classification2, msg2, "t2", mock_trello, cards, list_map, new_cards_by_topic)
        # Should NOT create a second card
        assert mock_trello.create_card.call_count == 1
        # Should add comment to existing card
        assert mock_trello.add_comment.call_count == 2
        # Both comments went to the same card
        card_ids = [call[0][0] for call in mock_trello.add_comment.call_args_list]
        assert card_ids == ["new_card_1", "new_card_1"]


class TestPriorityEscalation:
    """Tests for card escalation logic."""

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k", "SLACK_BOT_TOKEN": "t", "TRELLO_API_KEY": "k", "TRELLO_TOKEN": "t", "TRELLO_BOARD_ID": "b"})
    @patch("main.update_description_summary")
    def test_card_escalated_from_worth_reading_to_needs_response(self, mock_summary):
        mock_summary.return_value = "**Summary**\ntest\n\n**Reactions**\nNo reactions yet."

        mock_trello = MagicMock()
        mock_trello.get_list_id.return_value = "list_needs"
        mock_trello.get_checklists.return_value = []

        cards = [{"id": "card1", "name": "Topic", "idList": "list_reading", "desc": "#general | stuff"}]
        list_map = {"list_reading": "Worth Reading", "list_needs": "Needs Response"}

        classification = {
            "existing_topic_id": "card1",
            "topic_name": "Topic",
            "priority": "needs_response",
            "action_items": [],
        }
        msg = {"sender": "Alice", "channel": "general", "text": "Can you review?", "channel_id": "C1", "user_id": "U1"}

        processor._apply_classification(classification, msg, "t1", mock_trello, cards, list_map, {})
        mock_trello.move_card.assert_called_once_with("card1", "list_needs")

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k", "SLACK_BOT_TOKEN": "t", "TRELLO_API_KEY": "k", "TRELLO_TOKEN": "t", "TRELLO_BOARD_ID": "b"})
    @patch("main.update_description_summary")
    def test_card_not_demoted(self, mock_summary):
        mock_summary.return_value = "**Summary**\ntest\n\n**Reactions**\nNo reactions yet."

        mock_trello = MagicMock()
        mock_trello.get_list_id.return_value = "list_reading"
        mock_trello.get_checklists.return_value = []

        cards = [{"id": "card1", "name": "Topic", "idList": "list_needs", "desc": "#general | stuff"}]
        list_map = {"list_needs": "Needs Response", "list_reading": "Worth Reading"}

        classification = {
            "existing_topic_id": "card1",
            "topic_name": "Topic",
            "priority": "worth_reading",
            "action_items": [],
        }
        msg = {"sender": "Alice", "channel": "general", "text": "FYI", "channel_id": "C1", "user_id": "U1"}

        processor._apply_classification(classification, msg, "t1", mock_trello, cards, list_map, {})
        mock_trello.move_card.assert_not_called()
