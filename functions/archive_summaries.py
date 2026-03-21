"""Auto-archive AI newsletter summaries after 48 hours."""

import logging
from typing import Any

from functions.gmail_client import GmailClient

logger = logging.getLogger(__name__)

SUMMARY_QUERY = "in:inbox subject:🤖 older_than:2d"


def archive_summaries(request: Any = None) -> dict[str, Any]:
    """Find and archive newsletter summaries older than 48 hours.

    Args:
        request: HTTP request (not used)

    Returns:
        dict with status and count of archived messages
    """
    try:
        client = GmailClient()

        results = client.list_messages(query=SUMMARY_QUERY)
        messages = results.get("messages", [])

        if not messages:
            logger.info("No summaries to archive")
            return {"status": "success", "archived": 0}

        # Archive by removing INBOX label
        archived = 0
        for msg in messages:
            try:
                client.service.users().messages().modify(
                    userId="me",
                    id=msg["id"],
                    body={"removeLabelIds": ["INBOX"]},
                ).execute()
                archived += 1
            except Exception as e:
                logger.error(f"Failed to archive message {msg['id']}: {e}")

        logger.info(f"Archived {archived} newsletter summaries")
        return {"status": "success", "archived": archived}

    except Exception as e:
        error_msg = f"Error archiving summaries: {e}"
        logger.error(error_msg, exc_info=True)
        return {"status": "error", "error": error_msg}
