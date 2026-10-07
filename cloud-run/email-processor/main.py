"""Email Processor - Cloud Run Job.

Reads EMAIL_ID from environment, fetches email from Gmail, classifies, takes action.
"""

import base64
import json
import logging
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

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

# Label stamped on summaries this app emails to the user, so the archive-summaries
# job can find them by label instead of by subject (Gmail search drops emoji).
SUMMARY_LABEL = "ai-summary"


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


def apply_label(service, email_id: str, label_name: str, archive: bool = False) -> None:
    """Apply a label, optionally archive (remove from inbox)."""
    label_id = get_or_create_label(service, label_name)
    body = {"addLabelIds": [label_id]}
    if archive:
        body["removeLabelIds"] = ["INBOX"]
    service.users().messages().modify(userId="me", id=email_id, body=body).execute()


def label_summary_thread(service, sent_id: str, label_name: str = SUMMARY_LABEL) -> None:
    """Label a summary we just sent to ourselves.

    Sending to yourself produces two messages — the SENT copy and the delivered
    INBOX copy — so label the whole thread to be sure the inbox copy is tagged.

    Never fatal: the summary is already sent by this point, so a labeling hiccup
    must not fail the run. Worst case the summary is not auto-archived later.
    """
    try:
        label_id = get_or_create_label(service, label_name)
        sent = service.users().messages().get(userId="me", id=sent_id, format="minimal").execute()
        service.users().threads().modify(
            userId="me", id=sent["threadId"], body={"addLabelIds": [label_id]}
        ).execute()
    except Exception as e:
        logger.error(f"Failed to label summary thread for {sent_id}: {e}")


# Email categories and what each means. Single source of truth for both
# classifiers: rendered into the Claude prompt, and sent as the allowed choices
# to the OpenAI Decisions API.
CATEGORIES = {
    "marketing": (
        "Purpose is to drive ENGAGEMENT (clicks, purchases, signups). Even if it contains "
        "information, its goal is to get you to do something. Typically promotional, "
        "sales-driven, or trying to re-engage you with a product/service. Usually has an "
        "unsubscribe link. Examples: sales announcements, \"we miss you\" emails, product "
        "launches, limited-time offers, \"check out what's new\", app feature promotions, "
        "referral requests."
    ),
    "newsletter": (
        "Purpose is to INFORM. Information-dense content that delivers value through the "
        "content itself, not by driving you elsewhere. Often longer-form, educational, or "
        "curated content. Examples: blog digests, industry news roundups, educational "
        "content, personal essays from creators, curated links with commentary, research "
        "updates."
    ),
    "noti": (
        "Unimportant/noisy NOTIFICATIONS. Automated alerts that don't require attention or "
        "action. These are informational only, NOT actionable. If the notification requires "
        "or enables user action (downloading something, responding, reviewing important "
        "information), classify as 'other' instead. Examples: social media activity (likes, "
        "follows, comments), app badges, shipping updates, order confirmations, receipts, "
        "subscription renewals, \"someone viewed your profile\", automated system alerts, "
        "calendar reminders, read receipts, routine credit monitoring alerts (e.g., Experian, "
        "TransUnion, Equifax regular status updates without significant changes), privacy "
        "policy updates, terms of service updates (UNLESS they contain suspicious, sneaky, or "
        "significantly harmful changes—in that case, classify as 'other')."
    ),
    "recruiting": (
        "A human recruiter reaching out to YOU personally about a specific role or "
        "opportunity. The message reads like it was written (or at least tailored) by a "
        "person — addresses you by name, references your background, names a specific "
        "company/role, and invites a reply or call. May come from in-house recruiters, agency "
        "recruiters, or sourcers. Classify here ONLY for human outreach. Do NOT classify "
        "here: automated job-board alerts (LinkedIn Jobs digests, Indeed/Wellfound match "
        "emails, \"jobs you might like\"), generic newsletters from recruiting firms, or mass "
        "blasts with no personalization — those remain 'noti' or 'marketing' as appropriate."
    ),
    "other": (
        "Important notifications or personal emails that need attention and/or response. "
        "Includes notifications that enable or require user action, even if automated. Do NOT "
        "classify here unless it clearly doesn't fit above categories. Examples: password "
        "resets, 2FA codes, bank/payment alerts requiring action (unusual activity, fraud), "
        "account security alerts, credit monitoring alerts indicating significant changes "
        "(score drops, new accounts), direct messages from real people, direct social media "
        "comments from real people (they warrant response), calendar invites, support "
        "responses, shared files/passes to download, health portal messages, notifications "
        "that enable taking action or require review, Homeroom (school messaging app) emails "
        "carrying a message from the kindergarten class/teacher — these must stay in the "
        "inbox, so classify them 'other' even though they arrive as automated notification "
        "emails. Homeroom emails about OTHER grades/classes, or Homeroom account/product "
        "notifications with no kindergarten message content, follow the normal rules above."
    ),
}

# Decisions answers below this confidence fall back to "other" (stay in inbox):
# every other category archives or relabels, so "unsure" must mean "leave it".
DECISIONS_MIN_CONFIDENCE = 0.6
DECISIONS_URL = "https://api.openai.com/v1/decisions"


def classify_email_claude(subject: str, sender: str, body: str) -> dict:
    """Classify email using Claude."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    llm = ChatAnthropic(model="claude-opus-4-6", api_key=api_key, max_tokens=500)

    categories = "\n\n".join(f"- {name}: {desc}" for name, desc in CATEGORIES.items())
    prompt = f"""Classify this email by its PRIMARY PURPOSE into one of these categories:

{categories}

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


def _post_decisions(api_key: str, payload: dict) -> dict:
    """POST to the Decisions API, retrying rate limits / overload with backoff."""
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(5):
        req = urllib.request.Request(
            DECISIONS_URL,
            data=data,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 503, 529) and attempt < 4:
                time.sleep(0.5 * 2**attempt)
                continue
            raise RuntimeError(f"Decisions API {e.code}: {e.read()[:300]!r}") from e
    raise RuntimeError("Decisions API: retries exhausted")


def classify_email_decisions(subject: str, sender: str, body: str) -> dict:
    """Classify email with OpenAI's Decisions API (one typed choice question).

    Same return shape as classify_email_claude. The answer can only be one of
    CATEGORIES; refusals and low-confidence answers become "other".
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY not set")

    result = _post_decisions(
        api_key,
        {
            "model": "gpt-6-luna",
            "input": f"From: {sender}\nSubject: {subject}\nBody: {body[:4000]}",
            "questions": [
                {
                    "type": "choice",
                    "name": "category",
                    "instructions": "Classify this email by its PRIMARY PURPOSE.",
                    "choices": [
                        {"value": name, "description": desc} for name, desc in CATEGORIES.items()
                    ],
                }
            ],
        },
    )
    answer = result["answers"][0]
    if answer.get("type") != "choice":
        return {"category": "other", "confidence": 0.0, "reason": "decisions: refusal"}

    probs = sorted(answer.get("probabilities", []), key=lambda p: -p["probability"])[:3]
    reason = "decisions: " + ", ".join(f"{p['value']}={p['probability']:.2f}" for p in probs)
    confidence = float(answer.get("confidence", 0.0))
    category = answer["choice"]
    if category not in CATEGORIES or confidence < DECISIONS_MIN_CONFIDENCE:
        reason += f" (low confidence for {category}, kept in inbox)"
        category = "other"
    return {"category": category, "confidence": confidence, "reason": reason}


def classify_email(subject: str, sender: str, body: str) -> dict:
    """Classify with the backend chosen by CLASSIFIER: claude (default), shadow, decisions.

    shadow acts on Claude's answer but also asks Decisions and attaches its
    answer under "shadow" for comparison. A shadow failure never fails the run.
    """
    mode = os.getenv("CLASSIFIER", "claude")
    if mode == "decisions":
        return classify_email_decisions(subject, sender, body)

    result = classify_email_claude(subject, sender, body)
    if mode == "shadow":
        try:
            shadow = classify_email_decisions(subject, sender, body)
            shadow["agree"] = shadow["category"] == result.get("category")
        except Exception as e:
            shadow = {"error": str(e)}
        result["shadow"] = shadow
    return result


def summarize_newsletter(subject: str, sender: str, body: str) -> str:
    """Summarize a newsletter into an elevator-pitch length digest."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    llm = ChatAnthropic(model="claude-opus-4-6", api_key=api_key, max_tokens=1000)

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
        thread_id = email_data["thread_id"]
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
    if "shadow" in classification:
        log_structured(trace_id, email_id, "classifier_shadow", "success", {
            "claude": classification.get("category"),
            "decisions": classification["shadow"].get("category"),
            "agree": classification["shadow"].get("agree"),
            "decisions_reason": classification["shadow"].get("reason") or classification["shadow"].get("error"),
            "subject": subject,
            "from": sender,
        })

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
    elif category == "recruiting":
        # Label but leave in inbox so user can decide whether to reply
        try:
            apply_label(service, email_id, category, archive=False)
            log_structured(
                trace_id,
                email_id,
                "action",
                "success",
                {"action": "label_keep_inbox", "label": category},
            )
            logger.info(f"Applied 'recruiting' label, left in inbox: {email_id}")
        except Exception as e:
            logger.error(f"Failed to apply recruiting label: {e}")
            log_structured(trace_id, email_id, "action", "failure", {"error": str(e)})
    elif category == "newsletter":
        # Axios gets immediate 1:1 summary; all others batch into morning digest
        is_axios = "@axios.com" in sender.lower()

        if is_axios:
            # Summarize, email summary, label and archive original
            try:
                profile = service.users().getProfile(userId="me").execute()
                user_email = profile["emailAddress"]

                summary = summarize_newsletter(subject, sender, body)
                log_structured(
                    trace_id, email_id, "summarize", "success", {"summary_length": len(summary)}
                )

                summary_subject = f"🤖 {subject}"
                gmail_link = f"https://mail.google.com/mail/u/0/#all/{thread_id}"
                summary_body = (
                    f"Summary of newsletter from {sender}:\n\n"
                    f"{summary}\n\n"
                    f"---\n"
                    f"Original subject: {subject}\n"
                    f"View original: {gmail_link}"
                )
                sent_id = send_email(service, user_email, summary_subject, summary_body)
                label_summary_thread(service, sent_id)
                log_structured(trace_id, email_id, "send_summary", "success", {"sent_id": sent_id})

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
        else:
            # Queue for morning digest: label newsletter-pending + archive
            try:
                apply_label(service, email_id, "newsletter-pending", archive=True)
                log_structured(
                    trace_id,
                    email_id,
                    "action",
                    "success",
                    {"action": "queue_for_digest", "label": "newsletter-pending"},
                )
                logger.info(f"Queued newsletter for morning digest: {email_id}")
            except Exception as e:
                logger.error(f"Failed to queue newsletter for digest: {e}")
                log_structured(trace_id, email_id, "action", "failure", {"error": str(e)})

    logger.info(f"Done processing {email_id}")


if __name__ == "__main__":
    main()
