"""Fetch email metadata from Gmail API."""

from typing import Any

from gmail_ai_unsub.gmail.client import GmailClient


def fetch_email_metadata(
    client: GmailClient, email_id: str
) -> dict[str, Any] | None:
    """Fetch email metadata (subject and snippet) from Gmail API.

    Args:
        client: Gmail API client
        email_id: Gmail message ID

    Returns:
        Dict with 'subject' and 'snippet' keys, or None if fetch fails
    """
    try:
        # Use metadata format for efficiency (includes snippet)
        message = client.get_message_metadata(email_id)

        # Extract subject from headers
        # With metadata format, headers are in payload.headers
        subject = ""
        payload = message.get("payload", {})
        headers = payload.get("headers", [])
        
        for header in headers:
            if header.get("name", "").lower() == "subject":
                subject = header.get("value", "")
                break
        
        # If no subject found in headers, try alternative locations
        if not subject:
            # Some emails might have subject in a different location
            # Check payload.parts for multipart messages
            parts = payload.get("parts", [])
            for part in parts:
                part_headers = part.get("headers", [])
                for header in part_headers:
                    if header.get("name", "").lower() == "subject":
                        subject = header.get("value", "")
                        break
                if subject:
                    break

        # Gmail API provides snippet in metadata response
        snippet = message.get("snippet", "")

        return {
            "subject": subject or "(No subject)",
            "snippet": snippet,
        }

    except Exception as e:
        # Log error but don't raise (per design: no retry)
        import logging

        logger = logging.getLogger(__name__)
        logger.error(f"Failed to fetch email metadata for {email_id}: {e}")
        return None
