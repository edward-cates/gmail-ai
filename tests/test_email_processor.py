"""Tests for email processor with mocking."""

from unittest.mock import MagicMock, Mock, patch

import pytest

from gmail_ai_unsub.cloud.email_processor import process_email
from gmail_ai_unsub.classifier.email_classifier import ClassificationResult


@pytest.fixture
def mock_config():
    """Mock Config object."""
    config = Mock()
    config.cloud_storage_bucket = "test-bucket"
    config.cloud_project_id = "test-project"
    config.llm_provider = "anthropic"
    config.llm_model = "claude-4-5-opus"
    config.llm_api_key_env = "ANTHROPIC_API_KEY"
    config.llm_temperature = None
    config.prompt_system = "Test system prompt"
    config.prompt_marketing_criteria = "Test marketing criteria"
    config.prompt_exclusions = "Test exclusions"
    config.prompt_user_preferences = ""
    config.gmail_credentials_file = None
    config.gmail_token_file = "/tmp/test-token.json"
    return config


@pytest.fixture
def mock_cloud_logger():
    """Mock CloudLogger."""
    logger = Mock()
    logger.log = Mock()
    return logger


@pytest.fixture
def mock_gmail_client():
    """Mock GmailClient."""
    client = Mock()
    client.service = Mock()
    return client


@pytest.fixture
def mock_classifier():
    """Mock EmailClassifier."""
    classifier = Mock()
    classifier.classify_sync = Mock(
        return_value=ClassificationResult(
            is_marketing=True, confidence=0.95, reason="Test reason"
        )
    )
    return classifier


def test_process_email_success(
    mock_config, mock_cloud_logger, mock_gmail_client, mock_classifier
):
    """Test successful email processing."""
    request_data = {"email_id": "email1", "trace_id": "trace1"}

    with patch("gmail_ai_unsub.cloud.email_processor.Config", return_value=mock_config), patch(
        "gmail_ai_unsub.cloud.email_processor.CloudLogger", return_value=mock_cloud_logger
    ), patch(
        "gmail_ai_unsub.cloud.email_processor.GmailClient", return_value=mock_gmail_client
    ), patch(
        "gmail_ai_unsub.cloud.email_processor.fetch_email_full"
    ) as mock_fetch, patch(
        "gmail_ai_unsub.cloud.email_processor.create_classifier", return_value=mock_classifier
    ), patch(
        "gmail_ai_unsub.cloud.email_processor._get_token_file", return_value="/tmp/token.json"
    ), patch(
        "gmail_ai_unsub.cloud.email_processor._get_api_key", return_value="test-api-key"
    ):
        mock_fetch.return_value = {
            "subject": "Test Email",
            "from": "test@example.com",
            "snippet": "Test snippet",
            "body": "Test body content",
        }

        result = process_email(request_data)

        assert result["status"] == "success"
        assert result["email_id"] == "email1"
        assert result["trace_id"] == "trace1"
        assert result["classification"]["is_marketing"] is True
        assert result["classification"]["confidence"] == 0.95

        # Verify logs
        log_calls = {call[1]["stage"]: call[1] for call in mock_cloud_logger.log.call_args_list}

        assert "task_start" in log_calls
        assert log_calls["task_start"]["result"] == "success"

        assert "classification" in log_calls
        assert log_calls["classification"]["result"] == "success"
        assert log_calls["classification"]["metadata"]["is_marketing"] is True
        assert log_calls["classification"]["metadata"]["confidence"] == 0.95


def test_process_email_missing_email_id(mock_config, mock_cloud_logger):
    """Test processing with missing email_id."""
    request_data = {"trace_id": "trace1"}

    with patch("gmail_ai_unsub.cloud.email_processor.Config", return_value=mock_config), patch(
        "gmail_ai_unsub.cloud.email_processor.CloudLogger", return_value=mock_cloud_logger
    ):
        result = process_email(request_data)

        assert result["status"] == "error"
        assert "email_id is required" in result["error"]


def test_process_email_missing_trace_id(mock_config, mock_cloud_logger):
    """Test processing with missing trace_id."""
    request_data = {"email_id": "email1"}

    with patch("gmail_ai_unsub.cloud.email_processor.Config", return_value=mock_config), patch(
        "gmail_ai_unsub.cloud.email_processor.CloudLogger", return_value=mock_cloud_logger
    ):
        result = process_email(request_data)

        assert result["status"] == "error"
        assert "trace_id is required" in result["error"]


def test_process_email_fetch_fails(mock_config, mock_cloud_logger, mock_gmail_client):
    """Test processing when email fetch fails."""
    request_data = {"email_id": "email1", "trace_id": "trace1"}

    with patch("gmail_ai_unsub.cloud.email_processor.Config", return_value=mock_config), patch(
        "gmail_ai_unsub.cloud.email_processor.CloudLogger", return_value=mock_cloud_logger
    ), patch(
        "gmail_ai_unsub.cloud.email_processor.GmailClient", return_value=mock_gmail_client
    ), patch(
        "gmail_ai_unsub.cloud.email_processor.fetch_email_full", return_value=None
    ), patch(
        "gmail_ai_unsub.cloud.email_processor._get_token_file", return_value="/tmp/token.json"
    ), patch(
        "gmail_ai_unsub.cloud.email_processor._get_api_key", return_value="test-api-key"
    ):
        result = process_email(request_data)

        assert result["status"] == "error"
        assert "Failed to fetch email" in result["error"]


def test_process_email_classification_failure(
    mock_config, mock_cloud_logger, mock_gmail_client, mock_classifier
):
    """Test processing when classification fails."""
    request_data = {"email_id": "email1", "trace_id": "trace1"}

    mock_classifier.classify_sync.side_effect = Exception("Classification failed")

    with patch("gmail_ai_unsub.cloud.email_processor.Config", return_value=mock_config), patch(
        "gmail_ai_unsub.cloud.email_processor.CloudLogger", return_value=mock_cloud_logger
    ), patch(
        "gmail_ai_unsub.cloud.email_processor.GmailClient", return_value=mock_gmail_client
    ), patch(
        "gmail_ai_unsub.cloud.email_processor.fetch_email_full"
    ) as mock_fetch, patch(
        "gmail_ai_unsub.cloud.email_processor.create_classifier", return_value=mock_classifier
    ), patch(
        "gmail_ai_unsub.cloud.email_processor._get_token_file", return_value="/tmp/token.json"
    ), patch(
        "gmail_ai_unsub.cloud.email_processor._get_api_key", return_value="test-api-key"
    ):
        mock_fetch.return_value = {
            "subject": "Test Email",
            "from": "test@example.com",
            "snippet": "Test snippet",
            "body": "Test body content",
        }

        result = process_email(request_data)

        assert result["status"] == "error"
        assert "Classification failed" in result["error"]


def test_process_email_missing_api_key(mock_config, mock_cloud_logger, mock_gmail_client):
    """Test processing when API key is missing."""
    request_data = {"email_id": "email1", "trace_id": "trace1"}

    with patch("gmail_ai_unsub.cloud.email_processor.Config", return_value=mock_config), patch(
        "gmail_ai_unsub.cloud.email_processor.CloudLogger", return_value=mock_cloud_logger
    ), patch(
        "gmail_ai_unsub.cloud.email_processor.GmailClient", return_value=mock_gmail_client
    ), patch(
        "gmail_ai_unsub.cloud.email_processor.fetch_email_full"
    ) as mock_fetch, patch(
        "gmail_ai_unsub.cloud.email_processor._get_token_file", return_value="/tmp/token.json"
    ), patch(
        "gmail_ai_unsub.cloud.email_processor._get_api_key", return_value=None
    ):
        mock_fetch.return_value = {
            "subject": "Test Email",
            "from": "test@example.com",
            "snippet": "Test snippet",
            "body": "Test body content",
        }

        result = process_email(request_data)

        assert result["status"] == "error"
        assert "API key not found" in result["error"]


def test_process_email_classification_not_marketing(
    mock_config, mock_cloud_logger, mock_gmail_client
):
    """Test processing when email is classified as not marketing."""
    request_data = {"email_id": "email1", "trace_id": "trace1"}

    classifier = Mock()
    classifier.classify_sync = Mock(
        return_value=ClassificationResult(
            is_marketing=False, confidence=0.8, reason="Personal email"
        )
    )

    with patch("gmail_ai_unsub.cloud.email_processor.Config", return_value=mock_config), patch(
        "gmail_ai_unsub.cloud.email_processor.CloudLogger", return_value=mock_cloud_logger
    ), patch(
        "gmail_ai_unsub.cloud.email_processor.GmailClient", return_value=mock_gmail_client
    ), patch(
        "gmail_ai_unsub.cloud.email_processor.fetch_email_full"
    ) as mock_fetch, patch(
        "gmail_ai_unsub.cloud.email_processor.create_classifier", return_value=classifier
    ), patch(
        "gmail_ai_unsub.cloud.email_processor._get_token_file", return_value="/tmp/token.json"
    ), patch(
        "gmail_ai_unsub.cloud.email_processor._get_api_key", return_value="test-api-key"
    ):
        mock_fetch.return_value = {
            "subject": "Personal Email",
            "from": "friend@example.com",
            "snippet": "Personal snippet",
            "body": "Personal body content",
        }

        result = process_email(request_data)

        assert result["status"] == "success"
        assert result["classification"]["is_marketing"] is False
        assert result["classification"]["confidence"] == 0.8

        # Verify classification log
        log_calls = {call[1]["stage"]: call[1] for call in mock_cloud_logger.log.call_args_list}
        assert "classification" in log_calls
        assert log_calls["classification"]["metadata"]["is_marketing"] is False
