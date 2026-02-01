"""Email Processor Cloud Run Service.

Simple service: LOG → CLASSIFY → LOG
"""

import json
import logging
import os
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from google.cloud import storage
from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel

app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class ClassificationResult(BaseModel):
    """Result of email classification."""
    category: str  # marketing, newsletter, unimportant_notification, other
    confidence: float
    reason: str


def get_storage_client():
    """Get Cloud Storage client."""
    project_id = os.getenv("GMAIL_AI_PROJECT_ID")
    return storage.Client(project=project_id)


def log_to_storage(
    trace_id: str,
    email_id: str,
    stage: str,
    result: str = "success",
    metadata: dict | None = None,
) -> None:
    """Log to Cloud Storage as JSONL."""
    bucket_name = os.getenv("GMAIL_AI_STORAGE_BUCKET", "gmail-ai-logs")

    log_entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "trace_id": trace_id,
        "email_id": email_id,
        "stage": stage,
        "result": result,
        "metadata": metadata or {},
    }

    try:
        client = get_storage_client()
        bucket = client.bucket(bucket_name)

        # Path: logs/YYYY/MM/DD/HH-trace_id.jsonl
        now = datetime.now(UTC)
        blob_path = f"logs/{now.strftime('%Y/%m/%d')}/{now.strftime('%H')}-{trace_id}.jsonl"
        blob = bucket.blob(blob_path)

        # Append to existing or create new
        existing = ""
        if blob.exists():
            existing = blob.download_as_text()

        blob.upload_from_string(existing + json.dumps(log_entry) + "\n")
        logger.info(f"Logged to {blob_path}: {stage}")
    except Exception as e:
        logger.error(f"Failed to log to storage: {e}")


def classify_email(subject: str, sender: str, body: str) -> ClassificationResult:
    """Classify email using Claude."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    llm = ChatAnthropic(
        model="claude-sonnet-4-20250514",
        api_key=api_key,
        max_tokens=500,
    )

    prompt = f"""Classify this email into one of these categories:
- marketing: Promotional emails, sales, ads, newsletters trying to sell something
- newsletter: Regular content updates, blogs, news digests, informational emails
- unimportant_notification: Low-value automated alerts, login notices, profile views
- other: Everything else (personal emails, important notifications, etc.)

Email:
From: {sender}
Subject: {subject}
Body: {body[:2000]}

Respond with JSON only:
{{"category": "...", "confidence": 0.0-1.0, "reason": "brief explanation"}}"""

    response = llm.invoke(prompt)
    content = response.content.strip()

    # Parse JSON from response
    try:
        # Handle markdown code blocks
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()

        data = json.loads(content)
        return ClassificationResult(
            category=data.get("category", "other"),
            confidence=float(data.get("confidence", 0.5)),
            reason=data.get("reason", ""),
        )
    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"Failed to parse classification response: {e}")
        return ClassificationResult(
            category="other",
            confidence=0.0,
            reason=f"Failed to parse: {content[:100]}",
        )


@app.get("/")
def health_check() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/process")
async def process_email(request: Request) -> JSONResponse:
    """Process an email: LOG → CLASSIFY → LOG."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    email_id = data.get("email_id", "unknown")
    trace_id = data.get("trace_id", f"trace-{email_id}")
    subject = data.get("subject", "")
    sender = data.get("from", "")
    body = data.get("body", "")

    # LOG: Request received
    log_to_storage(
        trace_id=trace_id,
        email_id=email_id,
        stage="request_received",
        metadata={"subject": subject, "from": sender},
    )

    # CLASSIFY
    try:
        result = classify_email(subject, sender, body)
        classification = result.model_dump()
    except Exception as e:
        logger.error(f"Classification failed: {e}")
        log_to_storage(
            trace_id=trace_id,
            email_id=email_id,
            stage="classification",
            result="failure",
            metadata={"error": str(e)},
        )
        return JSONResponse({"error": str(e)}, status_code=500)

    # LOG: Classification result
    log_to_storage(
        trace_id=trace_id,
        email_id=email_id,
        stage="classification",
        metadata=classification,
    )

    return JSONResponse({
        "status": "ok",
        "email_id": email_id,
        "classification": classification,
    })
