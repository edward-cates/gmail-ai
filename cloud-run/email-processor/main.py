"""Email Processor - Cloud Run Job.

Reads EMAIL_ID from environment, classifies email, takes action, exits.
"""

import json
import logging
import os
import sys
import tempfile

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
]


def log_structured(trace_id: str, email_id: str, stage: str, result: str = "success", metadata: dict | None = None) -> None:
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


def get_or_create_label(service, label_name: str) -> str:
    """Get existing label ID or create new label."""
    results = service.users().labels().list(userId="me").execute()
    for label in results.get("labels", []):
        if label.get("name") == label_name:
            return label["id"]

    # Create it
    label_obj = {
        "name": label_name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }
    created = service.users().labels().create(userId="me", body=label_obj).execute()
    return created["id"]


def apply_label_and_archive(service, email_id: str, label_name: str) -> None:
    """Apply a label and archive (remove from inbox)."""
    label_id = get_or_create_label(service, label_name)
    service.users().messages().modify(
        userId="me",
        id=email_id,
        body={
            "addLabelIds": [label_id],
            "removeLabelIds": ["INBOX"],
        },
    ).execute()


def classify_email(subject: str, sender: str, body: str) -> dict:
    """Classify email using Claude."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    llm = ChatAnthropic(model="claude-sonnet-4-20250514", api_key=api_key, max_tokens=500)

    prompt = f"""Classify this email into one of these categories:
- marketing: Promotional emails, sales, ads
- newsletter: Regular content updates, blogs, news digests
- unimportant_notification: Low-value automated alerts, login notices
- other: Everything else (personal emails, important notifications)

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


def main():
    """Main entry point."""
    email_id = os.getenv("EMAIL_ID")
    trace_id = os.getenv("TRACE_ID", f"trace-{email_id}")
    subject = os.getenv("EMAIL_SUBJECT", "")
    sender = os.getenv("EMAIL_FROM", "")
    body = os.getenv("EMAIL_BODY", "")

    if not email_id:
        logger.error("EMAIL_ID environment variable not set")
        sys.exit(1)

    logger.info(f"Processing email: {email_id}")
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

    # ACTION: If marketing, apply label and archive
    category = classification.get("category", "other")
    if category == "marketing":
        try:
            service = get_gmail_service()
            apply_label_and_archive(service, email_id, "marketing")
            log_structured(trace_id, email_id, "action", "success", {"action": "label_and_archive", "label": "marketing"})
            logger.info(f"Applied 'marketing' label and archived {email_id}")
        except Exception as e:
            logger.error(f"Failed to apply label/archive: {e}")
            log_structured(trace_id, email_id, "action", "failure", {"error": str(e)})

    logger.info(f"Done processing {email_id}")


if __name__ == "__main__":
    main()
