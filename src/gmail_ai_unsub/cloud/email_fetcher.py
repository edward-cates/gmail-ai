"""Fetch email metadata and body from Gmail API."""

from typing import Any

from gmail_ai_unsub.gmail.client import GmailClient
from gmail_ai_unsub.unsubscribe.extractor import parse_email_body


def _extract_header(headers: list[dict[str, str]], name: str) -> str:
    """Extract header value by name (case-insensitive)."""
    for header in headers:
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


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
        payload = message.get("payload", {})
        headers = payload.get("headers", [])
        subject = _extract_header(headers, "subject")

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


def fetch_email_full(
    client: GmailClient, email_id: str
) -> dict[str, Any] | None:
    """Fetch full email (including body) from Gmail API.

    Args:
        client: Gmail API client
        email_id: Gmail message ID

    Returns:
        Dict with 'subject', 'from', 'snippet', and 'body' keys, or None if fetch fails
    """
    try:
        # Get full message (includes body)
        message = client.get_message(email_id, format="full")

        # Extract headers
        payload = message.get("payload", {})
        headers = payload.get("headers", [])
        subject = _extract_header(headers, "subject")
        from_address = _extract_header(headers, "from")

        # Gmail API provides snippet
        snippet = message.get("snippet", "")

        # Parse body
        body, _ = parse_email_body(message)

        return {
            "subject": subject or "(No subject)",
            "from": from_address,
            "snippet": snippet,
            "body": body,
        }

    except Exception as e:
        # Log error but don't raise (per design: no retry)
        import logging

        logger = logging.getLogger(__name__)
        logger.error(f"Failed to fetch full email for {email_id}: {e}")
        return None
