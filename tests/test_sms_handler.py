"""Tests for SMS handler Cloud Function."""

import base64
import hashlib
import hmac
from unittest.mock import MagicMock, patch

from functions.sms_handler import handle_sms, trigger_sms_morning


def _make_twilio_request(form_data, auth_token="test-token", valid_sig=True):
    """Build a mock Flask request with Twilio signature."""
    request = MagicMock()
    request.form = MagicMock()
    request.form.to_dict.return_value = form_data
    request.form.get = lambda key, default="": form_data.get(key, default)

    url = "https://us-central1-project.cloudfunctions.net/sms-handler"
    request.url = url

    sorted_params = "".join(f"{k}{v}" for k, v in sorted(form_data.items()))
    data_to_sign = url + sorted_params

    if valid_sig:
        sig = base64.b64encode(
            hmac.new(auth_token.encode(), data_to_sign.encode(), hashlib.sha1).digest()
        ).decode()
    else:
        sig = "invalid"

    request.headers = {"X-Twilio-Signature": sig}
    return request


@patch.dict("os.environ", {
    "TWILIO_AUTH_TOKEN": "test-token",
    "GMAIL_AI_PROJECT_ID": "test-project",
})
class TestHandleSms:
    """Tests for handle_sms()."""

    def test_invalid_signature_rejected(self):
        form = {"From": "+1234567890", "Body": "hello", "MessageSid": "SM123"}
        request = _make_twilio_request(form, valid_sig=False)
        result = handle_sms(request)
        assert result == ({"error": "invalid signature"}, 401)

    @patch("functions.sms_handler._trigger_sms_coach")
    def test_valid_sms_triggers_job(self, mock_trigger):
        form = {"From": "+1234567890", "Body": "Hit a new PR!", "MessageSid": "SM123"}
        request = _make_twilio_request(form)
        body, status, headers = handle_sms(request)
        assert status == 200
        assert "<Response></Response>" in body
        mock_trigger.assert_called_once()
        args = mock_trigger.call_args
        assert args[0][1] == "Hit a new PR!"  # inbound_body
        assert args[0][2] == "+1234567890"  # from_number

    @patch("functions.sms_handler._trigger_sms_coach")
    def test_empty_body_no_trigger(self, mock_trigger):
        form = {"From": "+1234567890", "Body": "", "MessageSid": "SM123"}
        request = _make_twilio_request(form)
        body, status, _headers = handle_sms(request)
        assert status == 200
        mock_trigger.assert_not_called()

    @patch("functions.sms_handler._trigger_sms_coach")
    def test_missing_signature_rejected(self, mock_trigger):
        request = MagicMock()
        request.headers = {}
        request.form = MagicMock()
        request.form.to_dict.return_value = {"Body": "hi"}
        request.form.get = lambda k, d="": {"Body": "hi"}.get(k, d)
        request.url = "https://example.com/sms"
        result = handle_sms(request)
        assert result == ({"error": "invalid signature"}, 401)
        mock_trigger.assert_not_called()


@patch.dict("os.environ", {"GMAIL_AI_PROJECT_ID": "test-project"})
class TestTriggerSmsMorning:
    """Tests for trigger_sms_morning()."""

    @patch("functions.sms_handler._trigger_sms_coach")
    def test_morning_trigger(self, mock_trigger):
        request = MagicMock()
        result = trigger_sms_morning(request)
        assert result["ok"] is True
        mock_trigger.assert_called_once()
        assert mock_trigger.call_args[1]["mode"] == "morning"

    @patch("functions.sms_handler._trigger_sms_coach")
    def test_morning_returns_trace_id(self, mock_trigger):
        request = MagicMock()
        result = trigger_sms_morning(request)
        assert "trace_id" in result
