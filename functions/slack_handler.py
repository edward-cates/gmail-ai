"""Slack event handler - receives Slack Events API webhooks.

Instead of triggering a Cloud Run Job per event, writes events to Cloud Storage
for batch processing. A separate scheduler triggers the processor periodically.
"""

import hashlib
import hmac
import json
import logging
import os
import sys
import time
import uuid

from google.cloud import run_v2, storage

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)


def _log_structured(trace_id, stage, result="success", metadata=None):
    """Log structured JSON to Cloud Logging."""
    log_data = {
        "trace_id": trace_id,
        "stage": stage,
        "result": result,
        "service": "slack-handler",
    }
    if metadata:
        log_data["metadata"] = metadata
    print(json.dumps(log_data), flush=True)


def _verify_slack_signature(request):
    """Verify the request is actually from Slack using the signing secret."""
    signing_secret = os.getenv("SLACK_SIGNING_SECRET", "")
    if not signing_secret:
        logger.warning("SLACK_SIGNING_SECRET not set, skipping verification")
        return True

    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not timestamp or not signature:
        return False

    # Reject requests older than 5 minutes (replay protection)
    try:
        if abs(time.time() - float(timestamp)) > 300:
            logger.warning("Slack request timestamp too old")
            return False
    except (ValueError, TypeError):
        return False

    # Compute expected signature
    sig_basestring = f"v0:{timestamp}:{request.get_data(as_text=True)}"
    expected = "v0=" + hmac.new(
        signing_secret.encode(), sig_basestring.encode(), hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


def _store_pending_event(trace_id, event):
    """Write a Slack event to Cloud Storage for batch processing."""
    bucket_name = os.getenv("GMAIL_AI_STORAGE_BUCKET", "gmail-ai-logs")
    project_id = os.getenv("GMAIL_AI_PROJECT_ID", "")

    try:
        client = storage.Client(project=project_id)
        bucket = client.bucket(bucket_name)

        event_id = f"{event.get('event_ts', event.get('ts', ''))}-{uuid.uuid4().hex[:8]}"
        blob_name = f"slack-pending/{event_id}.json"
        blob = bucket.blob(blob_name)

        payload = {"event": event, "trace_id": trace_id}
        blob.upload_from_string(json.dumps(payload), content_type="application/json")

        logger.info(f"Stored pending event: {blob_name}")
        _log_structured(trace_id, "event_stored", metadata={"blob": blob_name})

    except Exception as e:
        logger.error(f"Failed to store event: {e}", exc_info=True)
        _log_structured(trace_id, "event_stored", "failure", {"error": str(e)})


def handle_slack(request):
    """Handle incoming Slack event — store for batch processing."""
    trace_id = str(uuid.uuid4())

    # Verify signature
    if not _verify_slack_signature(request):
        logger.warning("Invalid Slack signature - rejecting request")
        return ({"error": "invalid signature"}, 401)

    body = request.get_json(silent=True)
    if not body:
        return ({"error": "no body"}, 400)

    # --- URL verification challenge (sent during Slack app setup) ---
    if body.get("type") == "url_verification":
        logger.info("Handling Slack URL verification challenge")
        return {"challenge": body.get("challenge", "")}

    # --- Event callbacks ---
    if body.get("type") != "event_callback":
        return {"ok": True}

    event = body.get("event", {})
    event_type = event.get("type", "")

    # --- Reaction events → store for batch ---
    if event_type == "reaction_added":
        user = event.get("user", "")
        reaction = event.get("reaction", "")
        if user and reaction:
            _log_structured(trace_id, "event_received", metadata={
                "type": "reaction", "user": user, "reaction": reaction,
            })
            _store_pending_event(trace_id, event)
        return {"ok": True}

    # --- Message events → store for batch ---
    if event_type != "message":
        return {"ok": True}

    # Skip bot messages
    if event.get("bot_id"):
        _log_structured(trace_id, "skip", metadata={"reason": "bot_message"})
        return {"ok": True}

    # Skip message subtypes (edits, deletes, joins, etc.) except thread broadcasts
    subtype = event.get("subtype")
    if subtype and subtype != "thread_broadcast":
        _log_structured(trace_id, "skip", metadata={"reason": f"subtype_{subtype}"})
        return {"ok": True}

    channel = event.get("channel", "")
    user = event.get("user", "")
    text = event.get("text", "")

    if not text or not user:
        return {"ok": True}

    _log_structured(trace_id, "event_received", metadata={
        "type": "message", "channel": channel, "user": user, "ts": event.get("ts"),
        "text_preview": text[:120],
    })
    _store_pending_event(trace_id, event)

    return {"ok": True}


def trigger_slack_batch(request):
    """Trigger the slack-processor Cloud Run Job for batch processing.

    Called by Cloud Scheduler every N minutes.
    """
    trace_id = str(uuid.uuid4())
    project_id = os.getenv("GMAIL_AI_PROJECT_ID", "")
    location = os.getenv("GMAIL_AI_LOCATION", "us-central1")
    job_name = os.getenv("SLACK_PROCESSOR_JOB_NAME", "slack-processor")
    bucket_name = os.getenv("GMAIL_AI_STORAGE_BUCKET", "gmail-ai-logs")

    # Check if there are pending events before triggering
    try:
        client = storage.Client(project=project_id)
        bucket = client.bucket(bucket_name)
        blobs = list(bucket.list_blobs(prefix="slack-pending/", max_results=1))
        if not blobs:
            logger.info("No pending Slack events, skipping batch")
            return {"ok": True, "pending": 0}
    except Exception as e:
        logger.warning(f"Failed to check pending events: {e}")
        # Trigger anyway in case of transient storage error
        pass

    try:
        jobs_client = run_v2.JobsClient()
        job_path = f"projects/{project_id}/locations/{location}/jobs/{job_name}"

        run_request = run_v2.RunJobRequest(
            name=job_path,
            overrides=run_v2.RunJobRequest.Overrides(
                container_overrides=[
                    run_v2.RunJobRequest.Overrides.ContainerOverride(
                        env=[
                            run_v2.EnvVar(name="TRACE_ID", value=trace_id),
                        ],
                    ),
                ],
            ),
        )

        operation = jobs_client.run_job(request=run_request)
        execution_name = operation.metadata.name if hasattr(operation, "metadata") else "unknown"

        logger.info(f"Triggered slack-processor batch: {execution_name}")
        _log_structured(trace_id, "batch_trigger", "success", {
            "job": job_name, "execution": execution_name,
        })

        return {"ok": True, "execution": execution_name}

    except Exception as e:
        logger.error(f"Failed to trigger batch job: {e}", exc_info=True)
        _log_structured(trace_id, "batch_trigger", "failure", {"error": str(e)})
        return ({"error": str(e)}, 500)
