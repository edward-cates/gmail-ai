"""Pub/Sub handler for Gmail Watch notifications - triggers Cloud Run Job."""

import base64
import json
import logging
import os
import sys
import uuid
from typing import Any

from google.cloud import run_v2

from functions.gmail_client import GmailClient, LabelManager

# Configure logging for Cloud Functions
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)


def _log_structured(trace_id: str, email_id: str, stage: str, result: str = "success", metadata: dict | None = None) -> None:
    """Log structured JSON to Cloud Logging."""
    log_data = {
        "trace_id": trace_id,
        "email_id": email_id,
        "stage": stage,
        "result": result,
        "service": "orchestrator",
    }
    if metadata:
        log_data["metadata"] = metadata

    # Print to stdout for Cloud Logging
    print(json.dumps(log_data), flush=True)


def handle_pubsub(event: dict[str, Any], context: Any) -> None:
    """Cloud Function entry point for Pub/Sub messages."""
    trace_id = str(uuid.uuid4())

    try:
        # Parse Pub/Sub message
        if "data" in event:
            message_data = base64.b64decode(event["data"]).decode("utf-8")
            pubsub_message = json.loads(message_data)
        else:
            pubsub_message = event

        history_id = str(pubsub_message.get("historyId", ""))
        assert history_id, "Pub/Sub message must contain historyId"

        _log_structured(trace_id, "unknown", "entry", "success", {"history_id": history_id})

        # Initialize Gmail client
        client = GmailClient()
        label_manager = LabelManager(client.service)

        # Get processing label
        processing_label = os.getenv("GMAIL_AI_PROCESSING_LABEL", "🤖")
        label_id = label_manager.get_or_create_label(processing_label)

        # Query inbox for unprocessed emails
        query = f"is:inbox -label:{processing_label}"
        result = client.list_messages(query=query, max_results=10)
        email_ids = [msg["id"] for msg in result.get("messages", [])]

        _log_structured(trace_id, "unknown", "query", "success", {"query": query, "count": len(email_ids)})

        # Process each email
        for email_id in email_ids:
            _process_email(
                client=client,
                label_manager=label_manager,
                email_id=email_id,
                label_id=label_id,
                trace_id=trace_id,
                processing_label=processing_label,
            )

    except AssertionError as e:
        logger.error(f"Validation error: {e}")
        _log_structured(trace_id, "unknown", "entry", "failure", {"error": str(e)})
    except Exception as e:
        logger.error(f"Failed to process Pub/Sub message: {e}", exc_info=True)
        _log_structured(trace_id, "unknown", "entry", "failure", {"error": str(e)})


def _get_header(headers: list[dict], name: str) -> str:
    """Extract header value by name (case-insensitive)."""
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _process_email(
    client: GmailClient,
    label_manager: LabelManager,
    email_id: str,
    label_id: str,
    trace_id: str,
    processing_label: str,
) -> None:
    """Process a single email: check, mark, trigger job."""
    try:
        # Fetch message metadata including headers
        message = client.get_message_metadata(email_id)
        headers = message.get("payload", {}).get("headers", [])
        subject = _get_header(headers, "Subject") or "(No Subject)"
        sender = _get_header(headers, "From")

        # Check if already processed
        if label_id in message.get("labelIds", []):
            _log_structured(trace_id, email_id, "skip", "success", {"reason": "already_processed", "subject": subject})
            return

        # Skip emails from this app (subject starts with 🤖)
        # Exception: "🤖 Axios" newsletters happen to use the same emoji
        if subject.startswith("🤖") and not subject.startswith("🤖 Axios"):
            _log_structured(trace_id, email_id, "skip", "success", {"reason": "app_email", "subject": subject})
            return

        # Mark with processing label
        label_manager.apply_label(email_id, label_id)
        _log_structured(trace_id, email_id, "mark", "success", {"label": processing_label, "subject": subject, "from": sender})

        # Trigger Cloud Run Job (job will fetch its own email data)
        _trigger_job(email_id, trace_id, subject)

    except Exception as e:
        logger.error(f"Failed to process {email_id}: {e}", exc_info=True)
        _log_structured(trace_id, email_id, "process", "failure", {"error": str(e)})


def _trigger_job(email_id: str, trace_id: str, subject: str) -> None:
    """Trigger Cloud Run Job to process email."""
    project_id = os.getenv("GMAIL_AI_PROJECT_ID", "")
    location = os.getenv("GMAIL_AI_LOCATION", "us-central1")
    job_name = os.getenv("GMAIL_AI_JOB_NAME", "email-processor")

    try:
        jobs_client = run_v2.JobsClient()
        job_path = f"projects/{project_id}/locations/{location}/jobs/{job_name}"

        # Execute job with just email_id and trace_id - job fetches its own data
        request = run_v2.RunJobRequest(
            name=job_path,
            overrides=run_v2.RunJobRequest.Overrides(
                container_overrides=[
                    run_v2.RunJobRequest.Overrides.ContainerOverride(
                        env=[
                            run_v2.EnvVar(name="EMAIL_ID", value=email_id),
                            run_v2.EnvVar(name="TRACE_ID", value=trace_id),
                        ],
                    ),
                ],
            ),
        )

        operation = jobs_client.run_job(request=request)
        execution_name = operation.metadata.name if hasattr(operation, "metadata") else "unknown"

        logger.info(f"Triggered job for {email_id}: {execution_name}")
        _log_structured(trace_id, email_id, "job_trigger", "success", {
            "job": job_name,
            "execution": execution_name,
            "subject": subject,
        })

    except Exception as e:
        logger.error(f"Failed to trigger job for {email_id}: {e}", exc_info=True)
        _log_structured(trace_id, email_id, "job_trigger", "failure", {"error": str(e)})
