"""Email Processor - Cloud Run Job.

Reads EMAIL_ID from environment, fetches email from Gmail, classifies, takes action.
"""

import base64
import json
import logging
import os
import sys
import tempfile

from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.cloud import storage
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from langchain_anthropic import ChatAnthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

# Threshold for combining text and HTML content (HTML must be 2x longer to combine)
HTML_LENGTH_MULTIPLIER = 2.0


def log_structured(
    trace_id: str, email_id: str, stage: str, result: str = "success", metadata: dict | None = None
) -> None:
    """Log structured JSON to Cloud Logging."""
    log_data = {
        "trace_id": trace_id,
        "email_id": email_id,
        "stage": stage,
        "result": result,
        "service": "email-processor",
    }
    if metadata:
        log_data["metadata"] = metadata
    print(json.dumps(log_data), flush=True)


def get_gmail_service():
    """Get Gmail API service using token from Cloud Storage."""
    bucket_name = os.getenv("GMAIL_AI_STORAGE_BUCKET")
    project_id = os.getenv("GMAIL_AI_PROJECT_ID")

    client = storage.Client(project=project_id)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob("token.json")

    temp_file = os.path.join(tempfile.gettempdir(), "gmail_token.json")
    blob.download_to_filename(temp_file)

    creds = Credentials.from_authorized_user_file(temp_file, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    return build("gmail", "v1", credentials=creds)


def html_to_text(html_content: str) -> str:
    """Convert HTML to plain text, preserving readability."""
    try:
        soup = BeautifulSoup(html_content, "html.parser")

        # Remove script and style elements
        for element in soup(["script", "style"]):
            element.decompose()

        # Get text with some spacing
        text = soup.get_text(separator="\n", strip=True)

        # Clean up excessive whitespace
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"HTML parsing failed: {e}")
        return ""


def extract_email_parts(payload: dict) -> tuple[str, str]:
    """Extract text/plain and text/html content from email payload.

    Returns:
        Tuple of (text_body, html_body) where either may be empty string.
    """
    def extract_part_body(part: dict) -> str:
        """Extract and decode body data from a part."""
        data = part.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        return ""

    text_body = ""
    html_body = ""

    def process_parts(parts: list) -> tuple[str, str]:
        """Recursively process email parts to find text/plain and text/html."""
        nonlocal text_body, html_body
        for part in parts:
            mime_type = part.get("mimeType", "")

            if mime_type == "text/plain" and not text_body:
                text_body = extract_part_body(part)
            elif mime_type == "text/html" and not html_body:
                html_body = extract_part_body(part)
            elif "parts" in part:
                # Recursively process nested parts
                process_parts(part["parts"])
        return text_body, html_body

    if "parts" in payload:
        text_body, html_body = process_parts(payload["parts"])
    elif "body" in payload and payload["body"].get("data"):
        # Single part message
        mime_type = payload.get("mimeType", "")
        body_data = extract_part_body(payload)
        if mime_type == "text/plain":
            text_body = body_data
        elif mime_type == "text/html":
            html_body = body_data

    return text_body, html_body


def fetch_email(service, email_id: str) -> dict:
    """Fetch email details from Gmail."""
    message = service.users().messages().get(userId="me", id=email_id, format="full").execute()
    payload = message.get("payload", {})
    headers = payload.get("headers", [])

    def get_header(name: str) -> str:
        for h in headers:
            if h.get("name", "").lower() == name.lower():
                return h.get("value", "")
        return ""

    # Extract both text and HTML content
    text_body, html_body = extract_email_parts(payload)

    # Prefer text/plain, but fall back to HTML converted to text
    final_body = text_body
    if not final_body and html_body:
        final_body = html_to_text(html_body)

    # If we have both, and HTML has significantly more content, use both
    if text_body and html_body:
        html_text = html_to_text(html_body)
        # Combine both if HTML version has significantly more content
        if len(html_text) > len(text_body) * HTML_LENGTH_MULTIPLIER:
            final_body = text_body + "\n\n" + html_text

    # Fall back to snippet if no body found
    if not final_body:
        final_body = message.get("snippet", "")

    return {
        "subject": get_header("subject") or "(No subject)",
        "from": get_header("from"),
        "snippet": message.get("snippet", ""),
        "body": final_body,
    }


def get_or_create_label(service, label_name: str) -> str:
    """Get existing label ID or create new label."""
    results = service.users().labels().list(userId="me").execute()
    for label in results.get("labels", []):
        if label.get("name") == label_name:
            return label["id"]

    label_obj = {
        "name": label_name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }
    created = service.users().labels().create(userId="me", body=label_obj).execute()
    return created["id"]


def apply_label(service, email_id: str, label_name: str, archive: bool = False) -> None:
    """Apply a label, optionally archive (remove from inbox)."""
    label_id = get_or_create_label(service, label_name)
    body = {"addLabelIds": [label_id]}
    if archive:
        body["removeLabelIds"] = ["INBOX"]
    service.users().messages().modify(userId="me", id=email_id, body=body).execute()


def classify_email(subject: str, sender: str, body: str) -> dict:
    """Classify email using Claude."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    llm = ChatAnthropic(model="claude-sonnet-4-20250514", api_key=api_key, max_tokens=500)

    prompt = f"""Classify this email by its PRIMARY PURPOSE into one of these categories:

- marketing: Purpose is to drive ENGAGEMENT (clicks, purchases, signups). Even if it contains
  information, its goal is to get you to do something. Typically promotional, sales-driven,
  or trying to re-engage you with a product/service. Usually has an unsubscribe link.
  Examples: sales announcements, "we miss you" emails, product launches, limited-time offers,
  "check out what's new", app feature promotions, referral requests.

- newsletter: Purpose is to INFORM. Information-dense content that delivers value through the
  content itself, not by driving you elsewhere. Often longer-form, educational, or curated content.
  Examples: blog digests, industry news roundups, educational content, personal essays from creators,
  curated links with commentary, research updates.

- noti: Unimportant/noisy NOTIFICATIONS. Automated alerts that don't require attention.
  Examples: social media activity (likes, follows, comments), app badges, shipping updates,
  order confirmations, receipts, subscription renewals, "someone viewed your profile",
  automated system alerts, calendar reminders, read receipts, routine credit monitoring alerts
  (e.g., Experian, TransUnion, Equifax regular status updates without significant changes).

- other: Important notifications or personal emails that need attention and/or response. Do NOT classify here
  unless it clearly doesn't fit above categories.
  Examples: password resets, 2FA codes, bank/payment alerts requiring action (unusual activity, fraud),
  account security alerts, credit monitoring alerts indicating significant changes (score drops, new accounts),
  direct messages from real people, direct social media comments from real people (they warrant response),
  calendar invites, support responses.

Email:
From: {sender}
Subject: {subject}
Body: {body[:2000]}

Respond with JSON only:
{{"category": "...", "confidence": 0.0-1.0, "reason": "brief explanation"}}"""

    response = llm.invoke(prompt)
    content = response.content.strip()

    try:
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        return json.loads(content)
    except (json.JSONDecodeError, IndexError):
        return {"category": "other", "confidence": 0.0, "reason": f"Parse error: {content[:100]}"}


def summarize_newsletter(subject: str, sender: str, body: str) -> str:
    """Summarize a newsletter into an elevator-pitch length digest."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    llm = ChatAnthropic(model="claude-sonnet-4-20250514", api_key=api_key, max_tokens=1000)

    prompt = f"""You're summarizing a newsletter for someone who doesn't have time to read it.

Imagine you have their attention for an elevator ride—30 seconds, maybe a minute. What would you tell them?

Be direct. Be dense. No fluff, no "this newsletter covers...", no meta-commentary. Just the actual insights,
news, or takeaways they'd want to know. Use bullet points if it helps. If there are links worth clicking,
mention what they're for.

Newsletter:
From: {sender}
Subject: {subject}

{body[:8000]}

---
Write the summary now. Keep it short enough to read in under a minute."""

    response = llm.invoke(prompt)
    return response.content.strip()


def send_email(service, to: str, subject: str, body: str) -> str:
    """Send an email and return the message ID."""
    import base64
    from email.mime.text import MIMEText

    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return sent["id"]


def main():
    """Main entry point."""
    email_id = os.getenv("EMAIL_ID")
    trace_id = os.getenv("TRACE_ID", f"trace-{email_id}")

    if not email_id:
        logger.error("EMAIL_ID environment variable not set")
        sys.exit(1)

    logger.info(f"Processing email: {email_id}")

    # Get Gmail service and fetch email
    try:
        service = get_gmail_service()
        email_data = fetch_email(service, email_id)
        subject = email_data["subject"]
        sender = email_data["from"]
        body = email_data["body"]
    except Exception as e:
        logger.error(f"Failed to fetch email: {e}")
        log_structured(trace_id, email_id, "fetch", "failure", {"error": str(e)})
        sys.exit(1)

    log_structured(trace_id, email_id, "job_start", metadata={"subject": subject, "from": sender})

    # CLASSIFY
    try:
        classification = classify_email(subject, sender, body)
        logger.info(f"Classification: {classification}")
    except Exception as e:
        logger.error(f"Classification failed: {e}")
        log_structured(trace_id, email_id, "classification", "failure", {"error": str(e)})
        sys.exit(1)

    log_structured(trace_id, email_id, "classification", "success", classification)

    # ACTION based on category
    category = classification.get("category", "other")
    if category in ["marketing", "noti"]:
        # Label and archive
        try:
            apply_label(service, email_id, category, archive=True)
            log_structured(
                trace_id,
                email_id,
                "action",
                "success",
                {"action": "label_and_archive", "label": category},
            )
            logger.info(f"Applied '{category}' label and archived {email_id}")
        except Exception as e:
            logger.error(f"Failed to apply label/archive: {e}")
            log_structured(trace_id, email_id, "action", "failure", {"error": str(e)})
    elif category == "newsletter":
        # Summarize, email summary, label and archive original
        try:
            # Get user's email address
            profile = service.users().getProfile(userId="me").execute()
            user_email = profile["emailAddress"]

            # Summarize the newsletter
            summary = summarize_newsletter(subject, sender, body)
            log_structured(
                trace_id, email_id, "summarize", "success", {"summary_length": len(summary)}
            )

            # Send summary email (subject starts with 🤖 to skip processing)
            summary_subject = f"🤖 {subject}"
            gmail_link = f"https://mail.google.com/mail/u/0/#all/{email_id}"
            summary_body = (
                f"Summary of newsletter from {sender}:\n\n"
                f"{summary}\n\n"
                f"---\n"
                f"Original subject: {subject}\n"
                f"View original: {gmail_link}"
            )
            sent_id = send_email(service, user_email, summary_subject, summary_body)
            log_structured(trace_id, email_id, "send_summary", "success", {"sent_id": sent_id})

            # Label and archive original
            apply_label(service, email_id, category, archive=True)
            log_structured(
                trace_id,
                email_id,
                "action",
                "success",
                {"action": "summarize_and_archive", "label": category},
            )
            logger.info(f"Summarized, emailed, and archived newsletter {email_id}")
        except Exception as e:
            logger.error(f"Failed to process newsletter: {e}")
            log_structured(trace_id, email_id, "action", "failure", {"error": str(e)})

    logger.info(f"Done processing {email_id}")


if __name__ == "__main__":
    main()
