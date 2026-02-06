"""Tests for Slack event handler Cloud Function."""

import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

from functions.slack_handler import handle_slack, trigger_slack_batch


def _make_request(body, signing_secret="test-secret", valid_sig=True):
    """Build a mock Flask request with Slack signature."""
    request = MagicMock()
    body_str = json.dumps(body)
    request.get_data.return_value = body_str
    request.get_json.return_value = body

    timestamp = str(int(time.time()))
    if valid_sig:
        sig_basestring = f"v0:{timestamp}:{body_str}"
        sig = "v0=" + hmac.new(
            signing_secret.encode(), sig_basestring.encode(), hashlib.sha256
        ).hexdigest()
    else:
        sig = "v0=invalid"

    request.headers = {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": sig,
    }
    return request


@patch.dict("os.environ", {"SLACK_SIGNING_SECRET": "test-secret", "GMAIL_AI_STORAGE_BUCKET": "test-bucket", "GMAIL_AI_PROJECT_ID": "test-project"})
class TestHandleSlack:
    """Tests for handle_slack()."""

    def test_url_verification(self):
        body = {"type": "url_verification", "challenge": "abc123"}
        request = _make_request(body)
        result = handle_slack(request)
        assert result == {"challenge": "abc123"}

    def test_invalid_signature_rejected(self):
        body = {"type": "event_callback", "event": {"type": "message", "text": "hi"}}
        request = _make_request(body, valid_sig=False)
        result = handle_slack(request)
        assert result == ({"error": "invalid signature"}, 401)

    @patch("functions.slack_handler._store_pending_event")
    @patch("functions.slack_handler._log_structured")
    def test_message_event_stored(self, mock_log, mock_store):
        body = {
            "type": "event_callback",
            "event": {
                "type": "message",
                "channel": "C123",
                "user": "U456",
                "text": "hello world",
                "ts": "1234567890.123456",
            },
        }
        request = _make_request(body)
        result = handle_slack(request)
        assert result == {"ok": True}
        mock_store.assert_called_once()
        # Check event was passed
        stored_event = mock_store.call_args[0][1]
        assert stored_event["text"] == "hello world"
        # Check text preview is logged
        log_calls = [c for c in mock_log.call_args_list if c[0][1] == "event_received"]
        assert len(log_calls) == 1
        assert log_calls[0][1]["metadata"]["text_preview"] == "hello world"

    @patch("functions.slack_handler._store_pending_event")
    def test_bot_message_skipped(self, mock_store):
        body = {
            "type": "event_callback",
            "event": {
                "type": "message",
                "bot_id": "B123",
                "channel": "C123",
                "text": "bot says hi",
            },
        }
        request = _make_request(body)
        result = handle_slack(request)
        assert result == {"ok": True}
        mock_store.assert_not_called()

    @patch("functions.slack_handler._store_pending_event")
    def test_message_edit_skipped(self, mock_store):
        body = {
            "type": "event_callback",
            "event": {
                "type": "message",
                "subtype": "message_changed",
                "channel": "C123",
            },
        }
        request = _make_request(body)
        result = handle_slack(request)
        assert result == {"ok": True}
        mock_store.assert_not_called()

    @patch("functions.slack_handler._store_pending_event")
    def test_reaction_event_stored(self, mock_store):
        body = {
            "type": "event_callback",
            "event": {
                "type": "reaction_added",
                "user": "U456",
                "reaction": "thumbsup",
                "item": {"type": "message", "channel": "C123", "ts": "111.222"},
                "event_ts": "333.444",
            },
        }
        request = _make_request(body)
        result = handle_slack(request)
        assert result == {"ok": True}
        mock_store.assert_called_once()

    @patch("functions.slack_handler._store_pending_event")
    def test_empty_text_skipped(self, mock_store):
        body = {
            "type": "event_callback",
            "event": {"type": "message", "channel": "C123", "user": "U456", "text": ""},
        }
        request = _make_request(body)
        result = handle_slack(request)
        assert result == {"ok": True}
        mock_store.assert_not_called()


@patch.dict("os.environ", {"GMAIL_AI_PROJECT_ID": "test-project", "GMAIL_AI_STORAGE_BUCKET": "test-bucket"})
class TestTriggerSlackBatch:
    """Tests for trigger_slack_batch()."""

    @patch("functions.slack_handler.run_v2")
    @patch("functions.slack_handler.storage")
    def test_skips_when_no_pending(self, mock_storage, mock_run):
        mock_client = MagicMock()
        mock_storage.Client.return_value = mock_client
        mock_bucket = MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_bucket.list_blobs.return_value = []  # no pending events

        request = MagicMock()
        result = trigger_slack_batch(request)
        assert result == {"ok": True, "pending": 0}
        mock_run.JobsClient.assert_not_called()

    @patch("functions.slack_handler.run_v2")
    @patch("functions.slack_handler.storage")
    def test_triggers_job_when_pending(self, mock_storage, mock_run):
        mock_client = MagicMock()
        mock_storage.Client.return_value = mock_client
        mock_bucket = MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_bucket.list_blobs.return_value = [MagicMock()]  # one pending blob

        mock_jobs = MagicMock()
        mock_run.JobsClient.return_value = mock_jobs
        mock_operation = MagicMock()
        mock_operation.metadata.name = "exec-123"
        mock_jobs.run_job.return_value = mock_operation

        request = MagicMock()
        result = trigger_slack_batch(request)
        assert result["ok"] is True
        assert "execution" in result
        mock_jobs.run_job.assert_called_once()
