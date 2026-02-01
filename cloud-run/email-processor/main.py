"""Email Processor - Cloud Run Job.

Reads EMAIL_ID from environment, classifies email, logs result, exits.
"""

import json
import logging
import os
import sys

from langchain_anthropic import ChatAnthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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

    logger.info(json.dumps(log_data))


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

    # Parse JSON
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

    # LOG: Start
    log_structured(trace_id, email_id, "job_start", metadata={"subject": subject, "from": sender})

    # CLASSIFY
    try:
        classification = classify_email(subject, sender, body)
        logger.info(f"Classification: {classification}")
    except Exception as e:
        logger.error(f"Classification failed: {e}")
        log_structured(trace_id, email_id, "classification", "failure", {"error": str(e)})
        sys.exit(1)

    # LOG: Result
    log_structured(trace_id, email_id, "classification", "success", classification)

    logger.info(f"Done processing {email_id}")


if __name__ == "__main__":
    main()
