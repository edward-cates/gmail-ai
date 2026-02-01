"""Pub/Sub handler for Gmail Watch notifications - triggers Cloud Run Job."""

import base64
import json
import logging
import os
import uuid
from typing import Any

from google.cloud import run_v2

from functions.cloud_logger import CloudLogger
from functions.gmail_client import GmailClient, LabelManager

logger = logging.getLogger(__name__)


def _extract_header(headers: list[dict], name: str) -> str:
    """Extract header value by name (case-insensitive)."""
    for header in headers:
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def fetch_email_metadata(client: GmailClient, email_id: str) -> dict | None:
    """Fetch email metadata (subject, from, snippet)."""
    try:
        message = client.get_message_metadata(email_id)
        payload = message.get("payload", {})
        headers = payload.get("headers", [])
        return {
            "subject": _extract_header(headers, "subject") or "(No subject)",
            "from": _extract_header(headers, "from"),
            "snippet": message.get("snippet", ""),
        }
    except Exception as e:
        logger.error(f"Failed to fetch metadata for {email_id}: {e}")
        return None


def _log(cloud_logger: CloudLogger | None, trace_id: str, email_id: str, stage: str, result: str = "success", metadata: dict | None = None) -> None:
    """Log to Cloud Storage."""
    if cloud_logger:
        try:
            cloud_logger.log(trace_id=trace_id, email_id=email_id, stage=stage, result=result, metadata=metadata, service="orchestrator")
        except Exception:
            pass


def handle_pubsub(event: dict[str, Any], context: Any) -> None:
    """Cloud Function entry point for Pub/Sub messages."""
    trace_id = str(uuid.uuid4())
    cloud_logger = CloudLogger()

    try:
        # Parse Pub/Sub message
        if "data" in event:
            message_data = base64.b64decode(event["data"]).decode("utf-8")
            pubsub_message = json.loads(message_data)
        else:
            pubsub_message = event

        history_id = str(pubsub_message.get("historyId", ""))
        assert history_id, "Pub/Sub message must contain historyId"

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

        _log(cloud_logger, trace_id, "unknown", "entry", "success", {"history_id": history_id})
        _log(cloud_logger, trace_id, "unknown", "query", "success", {"query": query, "count": len(email_ids)})

        # Process each email
        for email_id in email_ids:
            _process_email(
                client=client,
                label_manager=label_manager,
                email_id=email_id,
                label_id=label_id,
                trace_id=trace_id,
                cloud_logger=cloud_logger,
                processing_label=processing_label,
            )

    except AssertionError as e:
        logger.error(f"Validation error: {e}")
        _log(cloud_logger, trace_id, "unknown", "entry", "failure", {"error": str(e)})
    except Exception as e:
        logger.error(f"Failed to process Pub/Sub message: {e}", exc_info=True)
        _log(cloud_logger, trace_id, "unknown", "entry", "failure", {"error": str(e)})


def _process_email(
    client: GmailClient,
    label_manager: LabelManager,
    email_id: str,
    label_id: str,
    trace_id: str,
    cloud_logger: CloudLogger | None,
    processing_label: str,
) -> None:
    """Process a single email: check, mark, trigger job."""
    try:
        # Check if already processed
        message = client.get_message_metadata(email_id)
        if label_id in message.get("labelIds", []):
            _log(cloud_logger, trace_id, email_id, "skip", "success", {"reason": "already_processed"})
            return

        # Fetch metadata
        metadata = fetch_email_metadata(client, email_id)
        if not metadata:
            _log(cloud_logger, trace_id, email_id, "fetch", "failure", {"error": "No metadata"})
            return

        # Mark with processing label
        label_manager.apply_label(email_id, label_id)
        _log(cloud_logger, trace_id, email_id, "mark", "success", {
            "label": processing_label,
            "subject": metadata["subject"],
        })

        # Trigger Cloud Run Job
        _trigger_job(email_id, trace_id, metadata, cloud_logger)

    except Exception as e:
        logger.error(f"Failed to process {email_id}: {e}", exc_info=True)
        _log(cloud_logger, trace_id, email_id, "process", "failure", {"error": str(e)})


def _trigger_job(email_id: str, trace_id: str, metadata: dict, cloud_logger: CloudLogger | None) -> None:
    """Trigger Cloud Run Job to process email."""
    project_id = os.getenv("GMAIL_AI_PROJECT_ID", "")
    location = os.getenv("GMAIL_AI_LOCATION", "us-central1")
    job_name = os.getenv("GMAIL_AI_JOB_NAME", "email-processor")

    try:
        jobs_client = run_v2.JobsClient()
        job_path = f"projects/{project_id}/locations/{location}/jobs/{job_name}"

        # Execute job with environment variable overrides
        request = run_v2.RunJobRequest(
            name=job_path,
            overrides=run_v2.RunJobRequest.Overrides(
                container_overrides=[
                    run_v2.RunJobRequest.Overrides.ContainerOverride(
                        env=[
                            run_v2.EnvVar(name="EMAIL_ID", value=email_id),
                            run_v2.EnvVar(name="TRACE_ID", value=trace_id),
                            run_v2.EnvVar(name="EMAIL_SUBJECT", value=metadata.get("subject", "")),
                            run_v2.EnvVar(name="EMAIL_FROM", value=metadata.get("from", "")),
                            run_v2.EnvVar(name="EMAIL_BODY", value=metadata.get("snippet", "")),
                        ],
                    ),
                ],
            ),
        )

        operation = jobs_client.run_job(request=request)
        execution_name = operation.metadata.name if hasattr(operation, "metadata") else "unknown"

        logger.info(f"Triggered job for {email_id}: {execution_name}")
        _log(cloud_logger, trace_id, email_id, "job_trigger", "success", {
            "job": job_name,
            "execution": execution_name,
            "subject": metadata.get("subject", ""),
        })

    except Exception as e:
        logger.error(f"Failed to trigger job for {email_id}: {e}", exc_info=True)
        _log(cloud_logger, trace_id, email_id, "job_trigger", "failure", {"error": str(e)})
