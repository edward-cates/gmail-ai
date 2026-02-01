"""Pub/Sub handler for Gmail Watch notifications - standalone."""

import base64
import json
import logging
import os
import uuid
from typing import Any

from google.cloud import tasks_v2

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
    """Fetch email metadata (subject and snippet)."""
    try:
        message = client.get_message_metadata(email_id)
        payload = message.get("payload", {})
        headers = payload.get("headers", [])
        subject = _extract_header(headers, "subject")
        snippet = message.get("snippet", "")
        return {"subject": subject or "(No subject)", "snippet": snippet}
    except Exception as e:
        logger.error(f"Failed to fetch metadata for {email_id}: {e}")
        return None


def _log(
    cloud_logger: CloudLogger | None,
    trace_id: str,
    email_id: str,
    stage: str,
    result: str = "success",
    metadata: dict | None = None,
) -> None:
    """Helper to log to Cloud Storage."""
    if cloud_logger:
        try:
            cloud_logger.log(
                trace_id=trace_id,
                email_id=email_id,
                stage=stage,
                result=result,
                metadata=metadata,
                service="orchestrator",
            )
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

        _log(cloud_logger, trace_id, "unknown", "entry", "success", {"event_keys": list(event.keys())})
        _log(cloud_logger, trace_id, "unknown", "query", "success", {"query": query, "count": len(email_ids)})

        # Process each email
        for email_id in email_ids:
            _process_email_for_task(
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


def _process_email_for_task(
    client: GmailClient,
    label_manager: LabelManager,
    email_id: str,
    label_id: str,
    trace_id: str,
    cloud_logger: CloudLogger | None,
    processing_label: str,
) -> None:
    """Process a single email: check, mark, create task."""
    try:
        # Check if already processed
        message = client.get_message_metadata(email_id)
        if label_id in message.get("labelIds", []):
            _log(cloud_logger, trace_id, email_id, "skip", "success", {"message": "Already processed"})
            return

        # Fetch metadata
        metadata = fetch_email_metadata(client, email_id)
        assert metadata, f"Failed to fetch metadata for {email_id}"
        subject = metadata.get("subject", "(No subject)")
        snippet = metadata.get("snippet", "")

        # Mark with processing label
        label_manager.apply_label(email_id, label_id)
        _log(cloud_logger, trace_id, email_id, "mark", "success", {
            "label": processing_label,
            "subject": subject,
            "snippet": snippet[:200] if snippet else "",
        })

        # Create Cloud Task
        try:
            project_id = os.getenv("GMAIL_AI_PROJECT_ID", "")
            project_number = os.getenv("GMAIL_AI_PROJECT_NUMBER", "")
            tasks_location = os.getenv("GMAIL_AI_TASKS_LOCATION", "us-central1")
            tasks_queue = os.getenv("GMAIL_AI_TASKS_QUEUE", "email-processing")
            run_service = os.getenv("GMAIL_AI_RUN_SERVICE", "email-processor")

            # Get service URL
            service_url = os.getenv("GMAIL_AI_RUN_SERVICE_URL")
            if not service_url:
                service_url = f"https://{run_service}-yktnhd6i3q-uc.a.run.app"

            tasks_client = tasks_v2.CloudTasksClient()
            queue_path = tasks_client.queue_path(project_id, tasks_location, tasks_queue)

            task_payload = {"email_id": email_id, "trace_id": trace_id}
            payload = json.dumps(task_payload).encode()

            service_account_email = f"{project_number}-compute@developer.gserviceaccount.com"

            task = tasks_v2.Task(
                http_request=tasks_v2.HttpRequest(
                    http_method=tasks_v2.HttpMethod.POST,
                    url=f"{service_url}/process",
                    headers={"Content-Type": "application/json"},
                    body=payload,
                    oidc_token=tasks_v2.OidcToken(
                        service_account_email=service_account_email,
                        audience=service_url,
                    ),
                ),
            )

            response = tasks_client.create_task(parent=queue_path, task=task)
            task_name = response.name if hasattr(response, 'name') else "unknown"

            _log(cloud_logger, trace_id, email_id, "task_create", "success", {
                "task_name": task_name,
                "queue": tasks_queue,
                "service_url": service_url,
                "subject": subject,
            })

        except Exception as e:
            logger.error(f"Failed to create task for {email_id}: {e}", exc_info=True)
            _log(cloud_logger, trace_id, email_id, "task_create", "failure", {"error": str(e)})

    except AssertionError as e:
        logger.error(f"Validation error for {email_id}: {e}")
        _log(cloud_logger, trace_id, email_id, "process", "failure", {"error": str(e)})
    except Exception as e:
        logger.error(f"Failed to process {email_id}: {e}", exc_info=True)
        _log(cloud_logger, trace_id, email_id, "process", "failure", {"error": str(e)})
