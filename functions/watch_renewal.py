"""Cloud Function to renew Gmail Watch subscription - standalone."""

import logging
import os
from typing import Any

from functions.gmail_client import GmailClient

logger = logging.getLogger(__name__)


def renew_watch(request: Any = None) -> dict[str, Any]:
    """Renew Gmail Watch subscription.

    Args:
        request: HTTP request (not used)

    Returns:
        dict with status and details
    """
    try:
        # Get Pub/Sub topic from env
        topic = os.getenv("GMAIL_AI_PUBSUB_TOPIC")
        if not topic:
            project_id = os.getenv("GMAIL_AI_PROJECT_ID", "")
            topic = f"projects/{project_id}/topics/gmail-watch"

        # Initialize Gmail client
        client = GmailClient()

        logger.info("Renewing Gmail Watch...")
        logger.info(f"Topic: {topic}")

        # Stop existing watch
        try:
            client.service.users().stop(userId="me").execute()
            logger.info("Stopped existing watch")
        except Exception as e:
            logger.info(f"No existing watch to stop: {e}")

        # Set up fresh watch
        watch_request = {
            "topicName": topic,
            "labelIds": ["INBOX"],
        }

        result = client.service.users().watch(userId="me", body=watch_request).execute()

        expiration = result.get("expiration")
        history_id = result.get("historyId")

        logger.info(f"Gmail Watch renewed! History ID: {history_id}, Expiration: {expiration}")

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
