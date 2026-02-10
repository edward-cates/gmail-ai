"""Tests for Slack processor Cloud Run Job."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

# Add cloud-run/slack-processor to path so we can import it standalone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cloud-run", "slack-processor"))

import main as processor  # noqa: E402


class TestBatchClassification:
    """Tests for batch_classify_messages()."""

    @patch.dict("os.environ", {"SLACK_AI_API_KEY": "test-key"})
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
            "description": "Team discussing REST vs GraphQL for API design.",
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

    @patch.dict("os.environ", {"SLACK_AI_API_KEY": "test-key"})
    @patch("main.ChatAnthropic")
    def test_batch_of_three_messages(self, mock_anthropic_cls):
        mock_llm = MagicMock()
        mock_anthropic_cls.return_value = mock_llm

        mock_response = MagicMock()
        mock_response.content = json.dumps([
            {"msg_idx": 1, "existing_topic_id": "card1", "topic_name": "Sprint", "priority": "worth_reading", "description": "Sprint going well."},
            {"msg_idx": 2, "existing_topic_id": None, "topic_name": "New feature", "priority": "needs_response", "description": "PR needs review from Edward."},
            {"msg_idx": 3, "existing_topic_id": "card1", "topic_name": "Sprint", "priority": "action_required", "description": "Sprint blockers identified."},
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
        # Second is new
        assert result[1]["existing_topic_id"] is None
        assert result[1]["description"] == "PR needs review from Edward."
        # Only ONE call to Claude
        assert mock_llm.invoke.call_count == 1

    @patch.dict("os.environ", {"SLACK_AI_API_KEY": "test-key"})
    @patch("main.ChatAnthropic")
    def test_raises_on_parse_error(self, mock_anthropic_cls):
        mock_llm = MagicMock()
        mock_anthropic_cls.return_value = mock_llm

        mock_response = MagicMock()
        mock_response.content = "this is not json"
        mock_llm.invoke.return_value = mock_response

        messages = [{"idx": 1, "text": "hello", "sender": "Alice", "channel": "general"}]
        import pytest
        with pytest.raises(ValueError, match="unparseable"):
            processor.batch_classify_messages(messages, [])


class TestNewCardTracking:
    """Tests for batch-internal card deduplication."""

    @patch.dict("os.environ", {"SLACK_AI_API_KEY": "k", "SLACK_BOT_TOKEN": "t", "TRELLO_API_KEY": "k", "TRELLO_TOKEN": "t", "TRELLO_BOARD_ID": "b"})
    def test_second_message_reuses_card_from_batch(self):
        """Two messages about the same new topic should use one card."""
        mock_trello = MagicMock()
        mock_trello.get_list_id.return_value = "list1"
        mock_trello.create_card.return_value = {"id": "new_card_1"}

        cards = []
        list_map = {}
        new_cards_by_topic = {}

        mock_slack = MagicMock()
        mock_slack.channel_link.return_value = "https://slack.com/archives/C1"
        mock_slack.message_link.return_value = "https://slack.com/archives/C1/p111"

        # First message creates the card
        classification1 = {
            "existing_topic_id": None,
            "topic_name": "New Feature",
            "priority": "worth_reading",
            "description": "Discussion about a new feature.",
        }
        msg1 = {"sender": "Alice", "channel": "general", "text": "New feature idea", "channel_id": "C1", "user_id": "U1", "ts": "111.222"}

        processor._apply_classification(classification1, msg1, "t1", mock_trello, mock_slack, cards, list_map, new_cards_by_topic)
        assert mock_trello.create_card.call_count == 1

        # Second message with same topic name should reuse
        classification2 = {
            "existing_topic_id": None,
            "topic_name": "New Feature",
            "priority": "worth_reading",
            "description": "Discussion about a new feature.",
        }
        msg2 = {"sender": "Bob", "channel": "general", "text": "I agree on the feature", "channel_id": "C1", "user_id": "U2", "ts": "222.333"}

        processor._apply_classification(classification2, msg2, "t2", mock_trello, mock_slack, cards, list_map, new_cards_by_topic)
        # Should NOT create a second card
        assert mock_trello.create_card.call_count == 1
        # Should add comment to existing card
        assert mock_trello.add_comment.call_count == 2
        # Both comments went to the same card
        card_ids = [call[0][0] for call in mock_trello.add_comment.call_args_list]
        assert card_ids == ["new_card_1", "new_card_1"]


class TestPriorityEscalation:
    """Tests for card escalation logic."""

    @patch.dict("os.environ", {"SLACK_AI_API_KEY": "k", "SLACK_BOT_TOKEN": "t", "TRELLO_API_KEY": "k", "TRELLO_TOKEN": "t", "TRELLO_BOARD_ID": "b"})
    def test_card_escalated_from_worth_reading_to_needs_response(self):
        mock_trello = MagicMock()
        mock_trello.get_list_id.return_value = "list_needs"

        cards = [{"id": "card1", "name": "Topic", "idList": "list_reading", "desc": "#general | stuff"}]
        list_map = {"list_reading": "Worth Reading", "list_needs": "Needs Response"}

        mock_slack = MagicMock()
        mock_slack.channel_link.return_value = "https://slack.com/archives/C1"
        mock_slack.message_link.return_value = "https://slack.com/archives/C1/p111"

        classification = {
            "existing_topic_id": "card1",
            "topic_name": "Topic",
            "priority": "needs_response",
            "description": "Someone needs Edward to respond.",
        }
        msg = {"sender": "Alice", "channel": "general", "text": "Can you review?", "channel_id": "C1", "user_id": "U1", "ts": "111.222"}

        processor._apply_classification(classification, msg, "t1", mock_trello, mock_slack, cards, list_map, {})
        mock_trello.move_card.assert_called_once_with("card1", "list_needs")

    @patch.dict("os.environ", {"SLACK_AI_API_KEY": "k", "SLACK_BOT_TOKEN": "t", "TRELLO_API_KEY": "k", "TRELLO_TOKEN": "t", "TRELLO_BOARD_ID": "b"})
    def test_card_not_demoted(self):
        mock_trello = MagicMock()
        mock_trello.get_list_id.return_value = "list_reading"

        cards = [{"id": "card1", "name": "Topic", "idList": "list_needs", "desc": "#general | stuff"}]
        list_map = {"list_needs": "Needs Response", "list_reading": "Worth Reading"}

        mock_slack = MagicMock()
        mock_slack.channel_link.return_value = "https://slack.com/archives/C1"
        mock_slack.message_link.return_value = "https://slack.com/archives/C1/p111"

        classification = {
            "existing_topic_id": "card1",
            "topic_name": "Topic",
            "priority": "worth_reading",
            "description": "FYI update.",
        }
        msg = {"sender": "Alice", "channel": "general", "text": "FYI", "channel_id": "C1", "user_id": "U1", "ts": "111.222"}

        processor._apply_classification(classification, msg, "t1", mock_trello, mock_slack, cards, list_map, {})
        mock_trello.move_card.assert_not_called()


class TestThreadHandling:
    """Tests for thread reply detection and force-grouping."""

    def test_find_card_by_thread_ts(self):
        """Should find a card whose description contains the thread_ts marker."""
        cards = [
            {"id": "card1", "desc": "Some description\n\n**Threads**\n- [msg](link) `ts:111.222`"},
            {"id": "card2", "desc": "Other stuff"},
        ]
        assert processor.find_card_by_thread_ts(cards, "111.222")["id"] == "card1"
        assert processor.find_card_by_thread_ts(cards, "999.999") is None

    def test_append_thread_entry_new_section(self):
        """Should add a Threads section if none exists."""
        desc = "Topic description here."
        result = processor.append_thread_entry(desc, "Alice", "general", "hello world", "https://link", "111.222")
        assert "**Threads**" in result
        assert '`ts:111.222`' in result
        assert "Alice in #general" in result

    def test_append_thread_entry_existing_section(self):
        """Should append to existing Threads section."""
        desc = "Description\n\n**Threads**\n- [First](link1) `ts:111.222`"
        result = processor.append_thread_entry(desc, "Bob", "backend", "new msg", "https://link2", "333.444")
        assert '`ts:111.222`' in result
        assert '`ts:333.444`' in result

    @patch.dict("os.environ", {"SLACK_AI_API_KEY": "k", "SLACK_BOT_TOKEN": "t", "TRELLO_API_KEY": "k", "TRELLO_TOKEN": "t", "TRELLO_BOARD_ID": "b"})
    def test_thread_reply_skips_classification(self):
        """Thread replies with a matching parent card should skip Opus."""
        mock_slack = MagicMock()
        mock_slack.channel_link.return_value = "https://slack.com/archives/C1"
        mock_slack.message_link.return_value = "https://slack.com/archives/C1/p333"

        mock_trello = MagicMock()
        cards = [{
            "id": "card1", "name": "Topic", "idList": "list1",
            "desc": "Description\n\n**Threads**\n- [msg](link) `ts:111.222`",
        }]

        msg = {
            "sender": "Bob", "channel": "general", "text": "agreed",
            "channel_id": "C1", "user_id": "U2", "ts": "333.444",
            "thread_ts": "111.222", "is_thread_reply": True,
        }

        result = processor._process_thread_reply(msg, mock_slack, mock_trello, cards, "batch-t")
        assert result is True
        mock_trello.add_comment.assert_called_once()
        assert "thread reply" in mock_trello.add_comment.call_args[0][1]
        mock_trello.update_card_desc.assert_called_once()

    @patch.dict("os.environ", {"SLACK_AI_API_KEY": "k", "SLACK_BOT_TOKEN": "t", "TRELLO_API_KEY": "k", "TRELLO_TOKEN": "t", "TRELLO_BOARD_ID": "b"})
    def test_thread_reply_no_parent_falls_back(self):
        """Thread replies with no matching parent should fall back to classification."""
        mock_slack = MagicMock()
        mock_trello = MagicMock()
        cards = [{"id": "card1", "desc": "no threads here"}]

        msg = {
            "sender": "Bob", "channel": "general", "text": "agreed",
            "channel_id": "C1", "user_id": "U2", "ts": "333.444",
            "thread_ts": "999.999", "is_thread_reply": True,
        }

        result = processor._process_thread_reply(msg, mock_slack, mock_trello, cards, "batch-t")
        assert result is False
        mock_trello.add_comment.assert_not_called()


class TestNoiseClassification:
    """Tests for noise message handling."""

    @patch.dict("os.environ", {"SLACK_AI_API_KEY": "k", "SLACK_BOT_TOKEN": "t", "TRELLO_API_KEY": "k", "TRELLO_TOKEN": "t", "TRELLO_BOARD_ID": "b"})
    def test_noise_message_skips_trello(self):
        """Messages classified as noise should not create cards or comments."""
        mock_trello = MagicMock()
        mock_slack = MagicMock()

        classification = {
            "existing_topic_id": None,
            "topic_name": "Thanks",
            "priority": "noise",
            "description": "Noise.",
        }
        msg = {"sender": "Alice", "channel": "general", "text": "thanks!", "channel_id": "C1", "user_id": "U1", "ts": "111.222"}

        processor._apply_classification(classification, msg, "t1", mock_trello, mock_slack, [], {}, {})

        mock_trello.create_card.assert_not_called()
        mock_trello.add_comment.assert_not_called()

    @patch.dict("os.environ", {"SLACK_AI_API_KEY": "k", "SLACK_BOT_TOKEN": "t", "TRELLO_API_KEY": "k", "TRELLO_TOKEN": "t", "TRELLO_BOARD_ID": "b"})
    def test_noise_with_existing_topic_still_skips(self):
        """Even if noise is linked to an existing topic, it should be dropped."""
        mock_trello = MagicMock()
        mock_slack = MagicMock()

        classification = {
            "existing_topic_id": "card1",
            "topic_name": "Sprint",
            "priority": "noise",
            "description": "Noise.",
        }
        msg = {"sender": "Bob", "channel": "general", "text": "ok!", "channel_id": "C1", "user_id": "U2", "ts": "222.333"}

        processor._apply_classification(classification, msg, "t1", mock_trello, mock_slack, [{"id": "card1", "name": "Sprint", "idList": "l1", "desc": ""}], {}, {})

        mock_trello.add_comment.assert_not_called()


class TestActionItems:
    """Tests for per-message action item checklist logic."""

    @patch.dict("os.environ", {"SLACK_AI_API_KEY": "k", "SLACK_BOT_TOKEN": "t", "TRELLO_API_KEY": "k", "TRELLO_TOKEN": "t", "TRELLO_BOARD_ID": "b"})
    def test_action_item_added_to_new_card(self):
        """When Opus returns an action_item, it should be added as a checklist item."""
        mock_trello = MagicMock()
        mock_trello.get_list_id.return_value = "list1"
        mock_trello.create_card.return_value = {"id": "card1"}
        mock_trello.get_checklists.return_value = []
        mock_trello.create_checklist.return_value = {"id": "cl1"}

        mock_slack = MagicMock()
        mock_slack.channel_link.return_value = "https://slack.com/archives/C1"
        mock_slack.message_link.return_value = "https://slack.com/archives/C1/p111"

        classification = {
            "existing_topic_id": None,
            "topic_name": "Auth PR",
            "priority": "needs_response",
            "description": "Alice needs Edward to review.",
            "action_item": "Review auth PR",
        }
        msg = {"sender": "Alice", "channel": "backend", "text": "Can you review the auth PR?", "channel_id": "C1", "user_id": "U1", "ts": "111.222"}

        processor._apply_classification(classification, msg, "t1", mock_trello, mock_slack, [], {}, {})

        mock_trello.create_checklist.assert_called_once_with("card1")
        mock_trello.add_checklist_item.assert_called_once_with("cl1", "Review auth PR")

    @patch.dict("os.environ", {"SLACK_AI_API_KEY": "k", "SLACK_BOT_TOKEN": "t", "TRELLO_API_KEY": "k", "TRELLO_TOKEN": "t", "TRELLO_BOARD_ID": "b"})
    def test_no_action_item_skips_checklist(self):
        """When action_item is null, no checklist operations should happen."""
        mock_trello = MagicMock()
        mock_trello.get_list_id.return_value = "list1"
        mock_trello.create_card.return_value = {"id": "card1"}

        mock_slack = MagicMock()
        mock_slack.channel_link.return_value = "https://slack.com/archives/C1"
        mock_slack.message_link.return_value = "https://slack.com/archives/C1/p111"

        classification = {
            "existing_topic_id": None,
            "topic_name": "Deploy update",
            "priority": "worth_reading",
            "description": "Bob deployed caching to staging.",
            "action_item": None,
        }
        msg = {"sender": "Bob", "channel": "backend", "text": "Deployed caching to staging", "channel_id": "C1", "user_id": "U1", "ts": "111.222"}

        processor._apply_classification(classification, msg, "t1", mock_trello, mock_slack, [], {}, {})

        mock_trello.get_checklists.assert_not_called()
        mock_trello.create_checklist.assert_not_called()
        mock_trello.add_checklist_item.assert_not_called()

    @patch.dict("os.environ", {"SLACK_AI_API_KEY": "k", "SLACK_BOT_TOKEN": "t", "TRELLO_API_KEY": "k", "TRELLO_TOKEN": "t", "TRELLO_BOARD_ID": "b"})
    def test_action_item_added_to_existing_card(self):
        """Action items on existing cards should use existing checklist if present."""
        mock_trello = MagicMock()
        mock_trello.get_list_id.return_value = "list1"
        mock_trello.get_checklists.return_value = [{"id": "cl_existing", "checkItems": []}]

        mock_slack = MagicMock()
        mock_slack.channel_link.return_value = "https://slack.com/archives/C1"
        mock_slack.message_link.return_value = "https://slack.com/archives/C1/p111"

        cards = [{"id": "card1", "name": "Topic", "idList": "list1", "desc": "desc"}]
        list_map = {"list1": "Needs Response"}

        classification = {
            "existing_topic_id": "card1",
            "topic_name": "Topic",
            "priority": "needs_response",
            "description": "Follow-up needed.",
            "action_item": "Respond to Alice re: timeline",
        }
        msg = {"sender": "Alice", "channel": "general", "text": "What's the timeline?", "channel_id": "C1", "user_id": "U1", "ts": "111.222"}

        processor._apply_classification(classification, msg, "t1", mock_trello, mock_slack, cards, list_map, {})

        mock_trello.create_checklist.assert_not_called()
        mock_trello.add_checklist_item.assert_called_once_with("cl_existing", "Respond to Alice re: timeline")


class TestShortMessageContext:
    """Tests for preceding message context fetching."""

    def test_short_message_gets_context(self):
        """Short non-thread messages should have preceding_context populated."""
        mock_slack = MagicMock()
        mock_slack.get_user_name.return_value = "Alice"
        mock_slack.get_channel_name.return_value = "general"
        mock_slack.resolve_mentions.side_effect = lambda t: t
        mock_slack.get_preceding_messages.return_value = [
            {"sender": "Bob", "text": "Can you approve the deploy?"},
            {"sender": "Carol", "text": "I'll handle staging"},
        ]

        events = [{"event": {"user": "U1", "channel": "C1", "text": "ok!", "ts": "111.222", "thread_ts": ""}, "trace_id": "t1"}]

        result = processor._build_message_context(events, mock_slack, "batch-t")

        assert len(result) == 1
        assert result[0]["preceding_context"] != ""
        assert "Bob" in result[0]["preceding_context"]
        assert "Can you approve the deploy?" in result[0]["preceding_context"]
        mock_slack.get_preceding_messages.assert_called_once_with("C1", "111.222", count=3)

    def test_long_message_no_context_fetch(self):
        """Messages >= 20 chars should NOT fetch preceding context."""
        mock_slack = MagicMock()
        mock_slack.get_user_name.return_value = "Alice"
        mock_slack.get_channel_name.return_value = "general"
        mock_slack.resolve_mentions.side_effect = lambda t: t

        events = [{"event": {"user": "U1", "channel": "C1", "text": "This is a normal length message", "ts": "111.222", "thread_ts": ""}, "trace_id": "t1"}]

        result = processor._build_message_context(events, mock_slack, "batch-t")

        assert result[0]["preceding_context"] == ""
        mock_slack.get_preceding_messages.assert_not_called()

    def test_thread_reply_no_context_fetch(self):
        """Thread replies should NOT fetch preceding context (they already have thread context)."""
        mock_slack = MagicMock()
        mock_slack.get_user_name.return_value = "Alice"
        mock_slack.get_channel_name.return_value = "general"
        mock_slack.resolve_mentions.side_effect = lambda t: t

        events = [{"event": {"user": "U1", "channel": "C1", "text": "ok!", "ts": "222.333", "thread_ts": "111.222"}, "trace_id": "t1"}]

        result = processor._build_message_context(events, mock_slack, "batch-t")

        assert result[0]["preceding_context"] == ""
        mock_slack.get_preceding_messages.assert_not_called()


class TestDedup:
    """Tests for 30-minute dedup logic."""

    def test_get_last_dedup_time_returns_none_on_missing(self):
        """Should return None if the dedup timestamp blob does not exist."""
        with patch("main.storage.Client") as mock_client:
            mock_blob = MagicMock()
            mock_blob.download_as_text.side_effect = Exception("Not found")
            mock_client.return_value.bucket.return_value.blob.return_value = mock_blob

            result = processor.get_last_dedup_time()
            assert result is None

    def test_set_last_dedup_time_writes_to_gcs(self):
        """Should write current ISO timestamp to GCS."""
        with patch("main.storage.Client") as mock_client:
            mock_blob = MagicMock()
            mock_client.return_value.bucket.return_value.blob.return_value = mock_blob

            processor.set_last_dedup_time()
            mock_blob.upload_from_string.assert_called_once()
            written = mock_blob.upload_from_string.call_args[0][0]
            # Should be a valid ISO timestamp
            datetime.fromisoformat(written)

    @patch.dict("os.environ", {"SLACK_AI_API_KEY": "test-key"})
    @patch("main.ChatAnthropic")
    def test_identify_duplicates_parses_groups(self, mock_anthropic_cls):
        """Should parse Opus response into dedup groups."""
        mock_llm = MagicMock()
        mock_anthropic_cls.return_value = mock_llm

        mock_response = MagicMock()
        mock_response.content = json.dumps([{
            "card_ids": ["card1", "card2"],
            "merged_name": "Merged Topic",
            "merged_description": "Combined description.",
        }])
        mock_llm.invoke.return_value = mock_response

        cards = [
            {"id": "card1", "name": "Topic A", "desc": "desc A", "comments_preview": ""},
            {"id": "card2", "name": "Topic A v2", "desc": "desc A again", "comments_preview": ""},
        ]

        result = processor.identify_duplicates(cards)
        assert len(result) == 1
        assert result[0]["card_ids"] == ["card1", "card2"]

    @patch.dict("os.environ", {"SLACK_AI_API_KEY": "test-key"})
    @patch("main.ChatAnthropic")
    def test_identify_duplicates_empty_when_no_dupes(self, mock_anthropic_cls):
        """Should return empty list when Opus finds no duplicates."""
        mock_llm = MagicMock()
        mock_anthropic_cls.return_value = mock_llm

        mock_response = MagicMock()
        mock_response.content = "[]"
        mock_llm.invoke.return_value = mock_response

        result = processor.identify_duplicates([
            {"id": "card1", "name": "Topic A", "desc": "desc", "comments_preview": ""},
        ])
        assert result == []

    def test_run_dedup_skips_when_not_due(self):
        """Should skip dedup if last run was less than 30 minutes ago."""
        with patch("main.get_last_dedup_time") as mock_get:
            mock_get.return_value = datetime.now(tz=timezone.utc) - timedelta(minutes=10)
            mock_trello = MagicMock()

            processor.run_dedup_if_due(mock_trello, "batch-t")
            mock_trello.get_cards.assert_not_called()

    def test_run_dedup_runs_when_due(self):
        """Should run dedup if last run was more than 30 minutes ago."""
        with (
            patch("main.get_last_dedup_time") as mock_get,
            patch("main.set_last_dedup_time") as mock_set,
            patch("main.identify_duplicates") as mock_identify,
        ):
            mock_get.return_value = datetime.now(tz=timezone.utc) - timedelta(minutes=45)
            mock_identify.return_value = []  # no duplicates

            mock_trello = MagicMock()
            mock_trello.get_cards.return_value = [
                {"id": "c1", "name": "T1", "idList": "l1", "desc": "d1"},
                {"id": "c2", "name": "T2", "idList": "l1", "desc": "d2"},
            ]
            mock_trello.get_lists.return_value = [{"id": "l1", "name": "Worth Reading"}]
            mock_trello.get_comments.return_value = []

            processor.run_dedup_if_due(mock_trello, "batch-t")
            mock_trello.get_cards.assert_called_once()
            mock_identify.assert_called_once()
            mock_set.assert_called_once()


class TestTimestampConversion:
    """Tests for _utc_to_ct timestamp conversion (if still present)."""
    pass
