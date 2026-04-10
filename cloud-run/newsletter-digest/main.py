"""Newsletter Digest - Cloud Run Job.

Queries Gmail for newsletters labeled 'newsletter-pending', sends them to Claude
for consolidation into a top-10 digest, emails the result, and clears the label.
Triggered daily at 7 AM CT by Cloud Scheduler.
"""

import base64
import logging
import os
import tempfile
from email.mime.text import MIMEText

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

PENDING_LABEL = "newsletter-pending"
DIGESTED_LABEL = "newsletter-digested"


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
    """Convert HTML to plain text."""
    try:
        soup = BeautifulSoup(html_content, "html.parser")
        for element in soup(["script", "style"]):
            element.decompose()
        text = soup.get_text(separator="\n", strip=True)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"HTML parsing failed: {e}")
        return ""


def extract_email_parts(payload: dict) -> tuple[str, str]:
    """Extract text/plain and text/html content from email payload."""
    text_body = ""
    html_body = ""

    def extract_part_body(part: dict) -> str:
        data = part.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        return ""

    def process_parts(parts: list) -> None:
        nonlocal text_body, html_body
        for part in parts:
            mime_type = part.get("mimeType", "")
            if mime_type == "text/plain" and not text_body:
                text_body = extract_part_body(part)
            elif mime_type == "text/html" and not html_body:
                html_body = extract_part_body(part)
            elif "parts" in part:
                process_parts(part["parts"])

    if "parts" in payload:
        process_parts(payload["parts"])
    elif "body" in payload and payload["body"].get("data"):
        mime_type = payload.get("mimeType", "")
        body_data = extract_part_body(payload)
        if mime_type == "text/plain":
            text_body = body_data
        elif mime_type == "text/html":
            html_body = body_data

    return text_body, html_body


def fetch_email_body(service, email_id: str) -> dict:
    """Fetch full email content."""
    message = service.users().messages().get(userId="me", id=email_id, format="full").execute()
    payload = message.get("payload", {})
    headers = payload.get("headers", [])

    def get_header(name: str) -> str:
        for h in headers:
            if h.get("name", "").lower() == name.lower():
                return h.get("value", "")
        return ""

    text_body, html_body = extract_email_parts(payload)

    final_body = text_body
    if not final_body and html_body:
        final_body = html_to_text(html_body)
    if text_body and html_body:
        html_text = html_to_text(html_body)
        if len(html_text) > len(text_body) * 2.0:
            final_body = text_body + "\n\n" + html_text
    if not final_body:
        final_body = message.get("snippet", "")

    return {
        "subject": get_header("subject") or "(No subject)",
        "from": get_header("from"),
        "body": final_body,
        "thread_id": message.get("threadId", ""),
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


def move_label(service, email_id: str, from_label: str, to_label: str) -> None:
    """Remove one label and add another."""
    from_id = get_or_create_label(service, from_label)
    to_id = get_or_create_label(service, to_label)
    service.users().messages().modify(
        userId="me",
        id=email_id,
        body={"addLabelIds": [to_id], "removeLabelIds": [from_id]},
    ).execute()


def send_email(service, to: str, subject: str, body: str) -> str:
    """Send an email and return the message ID."""
    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return sent["id"]


def generate_digest(newsletters: list[dict]) -> str:
    """Send all newsletters to Claude and get a consolidated digest."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    llm = ChatAnthropic(model="claude-opus-4-6", api_key=api_key, max_tokens=2000)

    # Build the newsletter content block
    newsletter_blocks = []
    for i, nl in enumerate(newsletters, 1):
        newsletter_blocks.append(
            f"--- Newsletter {i} ---\n"
            f"From: {nl['from']}\n"
            f"Subject: {nl['subject']}\n\n"
            f"{nl['body'][:4000]}\n"
        )

    all_content = "\n".join(newsletter_blocks)

    prompt = f"""You have {len(newsletters)} newsletters from the past day. Your job is to distill
them into the 10 most societally relevant and meaningful things.

Not 10 per newsletter — 10 total, across all of them. Pick what actually matters. If fewer than 10
things are worth mentioning, that's fine — don't pad it.

For each item:
- Lead with the insight or news, not the source
- Keep it to 2-3 sentences max
- Note which newsletter it came from in parentheses at the end

Don't add an intro or outro. Just the numbered list.

Here are the newsletters:

{all_content}"""

    response = llm.invoke(prompt)
    return response.content.strip()


def generate_good_morning() -> str:
    """Generate a nice good morning message when there are no newsletters."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    llm = ChatAnthropic(model="claude-haiku-4-5-20251001", api_key=api_key, max_tokens=200)

    prompt = """Write a brief, warm good morning message (2-3 sentences). Be genuine and pleasant,
not cheesy. Mention that there were no newsletters to digest today. Keep it short."""

    response = llm.invoke(prompt)
    return response.content.strip()


def main():
    """Main entry point."""
    logger.info("Starting newsletter digest job")

    service = get_gmail_service()

    # Find all newsletter-pending emails
    get_or_create_label(service, PENDING_LABEL)
    query = f"label:{PENDING_LABEL}"
    result = service.users().messages().list(userId="me", q=query, maxResults=50).execute()
    messages = result.get("messages", [])

    logger.info(f"Found {len(messages)} pending newsletters")

    # Get user email
    profile = service.users().getProfile(userId="me").execute()
    user_email = profile["emailAddress"]

    if not messages:
        # No newsletters — send a good morning message
        greeting = generate_good_morning()
        send_email(service, user_email, "🤖 Good Morning!", greeting)
        logger.info("No pending newsletters — sent good morning email")
        return

    # Fetch full content of each newsletter
    newsletters = []
    for msg in messages:
        try:
            email_data = fetch_email_body(service, msg["id"])
            newsletters.append({**email_data, "id": msg["id"]})
        except Exception as e:
            logger.error(f"Failed to fetch newsletter {msg['id']}: {e}")

    if not newsletters:
        logger.error("Failed to fetch any newsletter content")
        return

    logger.info(f"Fetched {len(newsletters)} newsletters, generating digest...")

    # Generate the digest
    digest = generate_digest(newsletters)

    # Build and send the digest email
    sources = [f"  • {nl['subject']} ({nl['from']})" for nl in newsletters]
    sources_list = "\n".join(sources)

    digest_body = (
        f"{digest}\n\n"
        f"---\n"
        f"Compiled from {len(newsletters)} newsletters:\n"
        f"{sources_list}"
    )

    sent_id = send_email(service, user_email, "🤖 Morning Digest", digest_body)
    logger.info(f"Sent digest email: {sent_id}")

    # Move all processed newsletters from pending to digested
    for nl in newsletters:
        try:
            move_label(service, nl["id"], PENDING_LABEL, DIGESTED_LABEL)
        except Exception as e:
            logger.error(f"Failed to relabel newsletter {nl['id']}: {e}")

    logger.info(f"Done — digested {len(newsletters)} newsletters")


if __name__ == "__main__":
    main()
