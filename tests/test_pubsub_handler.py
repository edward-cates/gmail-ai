"""Tests for Pub/Sub handler with mocking."""

from unittest.mock import MagicMock, Mock, patch

import pytest

from gmail_ai_unsub.cloud.pubsub_handler import _process_email_for_task, handle_pubsub


@pytest.fixture
def mock_config():
    """Mock Config object."""
    config = Mock()
    config.cloud_storage_bucket = "test-bucket"
    config.cloud_project_id = "test-project"
    config.cloud_processing_label = "🤖"
    config.cloud_tasks_queue = "test-queue"
    config.cloud_tasks_location = "us-central1"
    config.cloud_run_service = "test-service"
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
    client.list_messages = Mock(return_value={"messages": [{"id": "email1"}, {"id": "email2"}]})
    client.get_message_metadata = Mock(
        return_value={
            "labelIds": [],
            "payload": {
                "headers": [{"name": "Subject", "value": "Test Email"}],
            },
            "snippet": "Test snippet",
        }
    )
    return client


@pytest.fixture
def mock_label_manager():
    """Mock LabelManager."""
    manager = Mock()
    manager.get_or_create_label = Mock(return_value="label-id-123")
    manager.apply_label = Mock()
    return manager


@patch("gmail_ai_unsub.cloud.pubsub_handler.Config")
@patch("gmail_ai_unsub.cloud.pubsub_handler.CloudLogger")
@patch("gmail_ai_unsub.cloud.pubsub_handler.GmailClient")
@patch("gmail_ai_unsub.cloud.pubsub_handler.LabelManager")
@patch("gmail_ai_unsub.cloud.pubsub_handler.fetch_email_metadata")
@patch("gmail_ai_unsub.cloud.pubsub_handler._get_token_file")
def test_handle_pubsub_success(
    mock_get_token,
    mock_fetch_metadata,
    mock_label_manager_class,
    mock_gmail_client_class,
    mock_cloud_logger_class,
    mock_config_class,
    mock_config,
    mock_cloud_logger,
    mock_gmail_client,
    mock_label_manager,
):
    """Test successful Pub/Sub handling."""
    # Setup mocks
    mock_config_class.return_value = mock_config
    mock_cloud_logger_class.return_value = mock_cloud_logger
    mock_gmail_client_class.return_value = mock_gmail_client
    mock_label_manager_class.return_value = mock_label_manager
    mock_get_token.return_value = "/tmp/test-token.json"
    mock_fetch_metadata.return_value = {
        "subject": "Test Email",
        "snippet": "Test snippet",
    }

    # Create Pub/Sub event
    event = {"historyId": "12345"}

    # Call handler
    handle_pubsub(event, None)

    # Verify entry log was called
    entry_calls = [
        call
        for call in mock_cloud_logger.log.call_args_list
        if call[1]["stage"] == "entry" and call[1]["result"] == "success"
    ]
    assert len(entry_calls) == 1

    # Verify query log was called
    query_calls = [
        call
        for call in mock_cloud_logger.log.call_args_list
        if call[1]["stage"] == "query" and call[1]["result"] == "success"
    ]
    assert len(query_calls) == 1
    assert query_calls[0][1]["metadata"]["count"] == 2

    # Should process 2 emails
    assert mock_label_manager.apply_label.call_count == 2


@patch("gmail_ai_unsub.cloud.pubsub_handler.Config")
@patch("gmail_ai_unsub.cloud.pubsub_handler.CloudLogger")
def test_handle_pubsub_missing_history_id(mock_cloud_logger_class, mock_config_class, mock_config, mock_cloud_logger):
    """Test Pub/Sub handling with missing historyId."""
    mock_config_class.return_value = mock_config
    mock_cloud_logger_class.return_value = mock_cloud_logger

    event = {}  # No historyId

    handle_pubsub(event, None)

    # Should log failure
    failure_calls = [
        call
        for call in mock_cloud_logger.log.call_args_list
        if call[1]["stage"] == "entry" and call[1]["result"] == "failure"
    ]
    assert len(failure_calls) == 1
    assert "historyId" in failure_calls[0][1]["metadata"]["error"]


@patch("gmail_ai_unsub.cloud.pubsub_handler.Config")
@patch("gmail_ai_unsub.cloud.pubsub_handler.CloudLogger")
@patch("gmail_ai_unsub.cloud.pubsub_handler.GmailClient")
@patch("gmail_ai_unsub.cloud.pubsub_handler.LabelManager")
@patch("gmail_ai_unsub.cloud.pubsub_handler._get_token_file")
def test_handle_pubsub_no_emails(
    mock_get_token,
    mock_label_manager_class,
    mock_gmail_client_class,
    mock_cloud_logger_class,
    mock_config_class,
    mock_config,
    mock_cloud_logger,
    mock_gmail_client,
    mock_label_manager,
):
    """Test Pub/Sub handling when no emails found."""
    mock_config_class.return_value = mock_config
    mock_cloud_logger_class.return_value = mock_cloud_logger
    mock_gmail_client_class.return_value = mock_gmail_client
    mock_label_manager_class.return_value = mock_label_manager
    mock_get_token.return_value = "/tmp/test-token.json"
    mock_gmail_client.list_messages.return_value = {"messages": []}  # No emails

    event = {"historyId": "12345"}

    handle_pubsub(event, None)

    # Should log query with count 0
    query_calls = [
        call
        for call in mock_cloud_logger.log.call_args_list
        if call[1]["stage"] == "query" and call[1]["result"] == "success"
    ]
    assert len(query_calls) == 1
    assert query_calls[0][1]["metadata"]["count"] == 0

    # Should not process any emails
    mock_label_manager.apply_label.assert_not_called()


def test_process_email_for_task_success(
    mock_gmail_client, mock_label_manager, mock_cloud_logger, mock_config
):
    """Test successful email processing."""
    with patch("gmail_ai_unsub.cloud.pubsub_handler.fetch_email_metadata") as mock_fetch:
        mock_fetch.return_value = {
            "subject": "Test Email",
            "snippet": "Test snippet",
        }

        _process_email_for_task(
            client=mock_gmail_client,
            label_manager=mock_label_manager,
            email_id="email1",
            label_id="label-id-123",
            trace_id="trace-123",
            cloud_logger=mock_cloud_logger,
            config=mock_config,
        )

        # Verify mark was called
        mock_label_manager.apply_label.assert_called_once_with("email1", "label-id-123")

        # Verify logs
        log_calls = {call[1]["stage"]: call[1] for call in mock_cloud_logger.log.call_args_list}

        assert "mark" in log_calls
        assert log_calls["mark"]["result"] == "success"
        assert log_calls["mark"]["metadata"]["subject"] == "Test Email"

        assert "task_create" in log_calls


def test_process_email_for_task_already_processed(
    mock_gmail_client, mock_label_manager, mock_cloud_logger, mock_config
):
    """Test email processing when email already has 🤖 tag."""
    # Email already has the label
    mock_gmail_client.get_message_metadata.return_value = {
        "labelIds": ["label-id-123"],  # Already has the tag
    }

    _process_email_for_task(
        client=mock_gmail_client,
        label_manager=mock_label_manager,
        email_id="email1",
        label_id="label-id-123",
        trace_id="trace-123",
        cloud_logger=mock_cloud_logger,
        config=mock_config,
    )

    # Should skip and not mark
    mock_label_manager.apply_label.assert_not_called()

    # Should log skip
    skip_calls = [
        call
        for call in mock_cloud_logger.log.call_args_list
        if call[1]["stage"] == "skip" and call[1]["result"] == "success"
    ]
    assert len(skip_calls) == 1


def test_process_email_for_task_fetch_fails(
    mock_gmail_client, mock_label_manager, mock_cloud_logger, mock_config
):
    """Test email processing when metadata fetch fails."""
    mock_gmail_client.get_message_metadata.return_value = {"labelIds": []}  # Not processed

    with patch("gmail_ai_unsub.cloud.pubsub_handler.fetch_email_metadata") as mock_fetch:
        mock_fetch.return_value = None  # Fetch fails

        _process_email_for_task(
            client=mock_gmail_client,
            label_manager=mock_label_manager,
            email_id="email1",
            label_id="label-id-123",
            trace_id="trace-123",
            cloud_logger=mock_cloud_logger,
            config=mock_config,
        )

        # Should not mark (assertion failed)
        mock_label_manager.apply_label.assert_not_called()

        # Should log failure
        failure_calls = [
            call
            for call in mock_cloud_logger.log.call_args_list
            if call[1]["stage"] == "process" and call[1]["result"] == "failure"
        ]
        assert len(failure_calls) == 1


@patch("gmail_ai_unsub.cloud.pubsub_handler.Config")
@patch("gmail_ai_unsub.cloud.pubsub_handler.CloudLogger")
@patch("gmail_ai_unsub.cloud.pubsub_handler.GmailClient")
@patch("gmail_ai_unsub.cloud.pubsub_handler.LabelManager")
@patch("gmail_ai_unsub.cloud.pubsub_handler._get_token_file")
def test_handle_pubsub_base64_encoded(
    mock_get_token,
    mock_label_manager_class,
    mock_gmail_client_class,
    mock_cloud_logger_class,
    mock_config_class,
    mock_config,
    mock_cloud_logger,
):
    """Test Pub/Sub handling with base64 encoded message."""
    mock_config_class.return_value = mock_config
    mock_cloud_logger_class.return_value = mock_cloud_logger
    mock_gmail_client_class.return_value = Mock(list_messages=Mock(return_value={"messages": []}))
    mock_label_manager_class.return_value = Mock(get_or_create_label=Mock(return_value="label-id"))
    mock_get_token.return_value = "/tmp/test-token.json"

    with patch("gmail_ai_unsub.cloud.pubsub_handler.base64") as mock_base64:
        mock_base64.b64decode.return_value = b'{"historyId": "12345"}'

        event = {"data": "encoded-data"}

        handle_pubsub(event, None)

        # Should decode base64
        mock_base64.b64decode.assert_called_once_with("encoded-data")
