"""Auto-archive AI newsletter summaries after 48 hours."""

import logging
from typing import Any

from functions.gmail_client import GmailClient, LabelManager

logger = logging.getLogger(__name__)

SUMMARY_LABEL = "ai-summary"
SUMMARY_PREFIX = "🤖"

# Match on the label the senders apply, never on the subject: Gmail search silently
# drops emoji from `subject:` terms, so the old `subject:🤖` query degraded into a
# match-all and archived the entire inbox. The subject is re-checked in Python
# (where the emoji compares reliably) before anything is archived.
SUMMARY_QUERY = f"in:inbox label:{SUMMARY_LABEL} older_than:2d"


def archive_summaries(request: Any = None) -> dict[str, Any]:
    """Find and archive newsletter summaries older than 48 hours.

    Args:
        request: HTTP request (not used)

    Returns:
        dict with status and count of archived messages
    """
    try:
        client = GmailClient()

        # Ensure the label exists so the query always resolves, even on a fresh mailbox
        LabelManager(client.service).get_or_create_label(SUMMARY_LABEL)

        results = client.list_messages(query=SUMMARY_QUERY)
        messages = results.get("messages", [])

        if not messages:
            logger.info("No summaries to archive")
            return {"status": "success", "archived": 0}

        # Archive by removing INBOX label
        archived = 0
        skipped = 0
        for msg in messages:
            try:
                if not _is_summary(client, msg["id"]):
                    skipped += 1
                    logger.warning(
                        f"Skipping {msg['id']}: labeled '{SUMMARY_LABEL}' but subject "
                        f"does not start with '{SUMMARY_PREFIX}'"
                    )
                    continue

                client.service.users().messages().modify(
                    userId="me",
                    id=msg["id"],
                    body={"removeLabelIds": ["INBOX"]},
                ).execute()
                archived += 1
            except Exception as e:
                logger.error(f"Failed to archive message {msg['id']}: {e}")

        logger.info(f"Archived {archived} newsletter summaries ({skipped} skipped)")
        return {"status": "success", "archived": archived, "skipped": skipped}

    except Exception as e:
        error_msg = f"Error archiving summaries: {e}"
        logger.error(error_msg, exc_info=True)
        return {"status": "error", "error": error_msg}


def _is_summary(client: GmailClient, message_id: str) -> bool:
    """Confirm a message is one of ours by checking its subject in Python."""
    message = client.get_message_metadata(message_id)
    headers = message.get("payload", {}).get("headers", [])
    subject = next(
        (h["value"] for h in headers if h.get("name", "").lower() == "subject"),
        "",
    )
    return subject.startswith(SUMMARY_PREFIX)
