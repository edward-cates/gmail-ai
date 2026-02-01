"""Cloud Function to renew Gmail Watch subscription.

This function is triggered by Cloud Scheduler to automatically renew
the Gmail Watch subscription before it expires (every 6 days).
"""

import logging
import os
import tempfile
from typing import Any

from google.cloud import storage

from gmail_ai_unsub.config import Config
from gmail_ai_unsub.gmail.client import GmailClient

logger = logging.getLogger(__name__)


def _is_cloud_environment() -> bool:
    """Check if we're running in a cloud environment."""
    return bool(os.getenv("FUNCTION_TARGET") or os.getenv("K_SERVICE") or os.getenv("GAE_ENV"))


def _get_config_file() -> str | None:
    """Get config file path, loading from Cloud Storage if in cloud environment."""
    if _is_cloud_environment():
        bucket_name = os.getenv("GMAIL_AI_STORAGE_BUCKET", "gmail-ai-logs")
        project_id = os.getenv("GMAIL_AI_PROJECT_ID", "")

        if bucket_name and project_id:
            try:
                storage_client = storage.Client(project=project_id)
                bucket = storage_client.bucket(bucket_name)
                blob = bucket.blob("config.toml")

                if blob.exists():
                    temp_dir = tempfile.gettempdir()
                    temp_config_file = os.path.join(temp_dir, "gmail_config.toml")
                    blob.download_to_filename(temp_config_file)
                    logger.info(f"Loaded config from Cloud Storage: gs://{bucket_name}/config.toml")
                    return temp_config_file
            except Exception as e:
                logger.warning(f"Failed to load config from Cloud Storage: {e}")

    return None


def _get_token_file(config: Config) -> str:
    """Get token file path, loading from Cloud Storage if in cloud environment."""
    if _is_cloud_environment():
        bucket_name = config.cloud_storage_bucket
        if bucket_name:
            try:
                storage_client = storage.Client(project=config.cloud_project_id)
                bucket = storage_client.bucket(bucket_name)
                blob = bucket.blob("token.json")

                if blob.exists():
                    temp_dir = tempfile.gettempdir()
                    temp_token_file = os.path.join(temp_dir, "gmail_token.json")
                    blob.download_to_filename(temp_token_file)
                    logger.info(f"Loaded token from Cloud Storage: gs://{bucket_name}/token.json")
                    return temp_token_file
            except Exception as e:
                logger.warning(f"Failed to load token from Cloud Storage: {e}")

    return config.gmail_token_file


def renew_watch(request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Cloud Function entry point for HTTP trigger to renew Gmail Watch.

    Args:
        request: HTTP request (not used, but required for Cloud Functions HTTP trigger)

    Returns:
        dict with status and details about the renewal
    """
    try:
        # Initialize config
        config_path = os.getenv("GMAIL_AI_CONFIG_PATH") or _get_config_file()
        config = Config(config_path) if config_path else Config()

        # Initialize Gmail client
        token_file = _get_token_file(config)
        client = GmailClient(
            credentials_file=config.gmail_credentials_file,
            token_file=token_file,
            use_default_credentials=True,
        )

        # Get Pub/Sub topic from config
        topic = config.cloud_pubsub_topic
        if not topic:
            error_msg = "cloud.pubsub_topic not set in config.toml"
            logger.error(error_msg)
            return {"status": "error", "error": error_msg}

        logger.info("Renewing Gmail Watch...")
        logger.info(f"Topic: {topic}")

        # Step 1: Stop any existing watch
        try:
            client.service.users().stop(userId="me").execute()
            logger.info("Stopped existing watch (if any)")
        except Exception as e:
            logger.info(f"No existing watch to stop, or error: {e}")

        # Step 2: Set up a fresh watch
        watch_request = {
            "topicName": topic,
            "labelIds": ["INBOX"],
        }

        result = client.service.users().watch(userId="me", body=watch_request).execute()

        expiration = result.get("expiration")
        history_id = result.get("historyId")

        logger.info(f"Gmail Watch renewed successfully!")
        logger.info(f"History ID: {history_id}")
        logger.info(f"Expiration: {expiration} (Unix timestamp in milliseconds)")

        return {
            "status": "success",
            "history_id": history_id,
            "expiration": expiration,
            "topic": topic,
        }

    except Exception as e:
        error_msg = f"Error renewing Gmail Watch: {e}"
        logger.error(error_msg, exc_info=True)
        return {"status": "error", "error": error_msg}
