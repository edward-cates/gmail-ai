"""SMS handler - receives Twilio webhooks and triggers sms-coach job.

Unlike the Slack handler which batches events, SMS replies trigger the
Cloud Run Job immediately for real-time conversation.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import uuid

from google.cloud import run_v2

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)


def _log_structured(trace_id, stage, result="success", metadata=None):
    """Log structured JSON to Cloud Logging."""
    log_data = {
        "trace_id": trace_id,
        "stage": stage,
        "result": result,
        "service": "sms-handler",
    }
    if metadata:
        log_data["metadata"] = metadata
    print(json.dumps(log_data), flush=True)


def _verify_twilio_signature(request):
    """Verify the request is from Twilio using HMAC-SHA1 + base64.

    Twilio signs: URL + sorted POST params -> HMAC-SHA1 with auth token -> base64.
    Header: X-Twilio-Signature
    """
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    if not auth_token:
        logger.warning("TWILIO_AUTH_TOKEN not set, skipping verification")
        return True

    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        return False

    url = request.url
    form_data = request.form.to_dict()
    sorted_params = "".join(f"{k}{v}" for k, v in sorted(form_data.items()))

    data_to_sign = url + sorted_params
    expected = base64.b64encode(
        hmac.new(auth_token.encode(), data_to_sign.encode(), hashlib.sha1).digest()
    ).decode()

    return hmac.compare_digest(expected, signature)


def _trigger_sms_coach(trace_id, inbound_body="", from_number="",
                        message_sid="", mode="reply"):
    """Trigger the sms-coach Cloud Run Job."""
    project_id = os.getenv("GMAIL_AI_PROJECT_ID", "")
    location = os.getenv("GMAIL_AI_LOCATION", "us-central1")
    job_name = os.getenv("SMS_COACH_JOB_NAME", "sms-coach")

    try:
        jobs_client = run_v2.JobsClient()
        job_path = f"projects/{project_id}/locations/{location}/jobs/{job_name}"

        env_vars = [
            run_v2.EnvVar(name="TRACE_ID", value=trace_id),
            run_v2.EnvVar(name="SMS_MODE", value=mode),
        ]
        if mode == "reply":
            env_vars.extend([
                run_v2.EnvVar(name="SMS_BODY", value=inbound_body),
                run_v2.EnvVar(name="SMS_FROM", value=from_number),
                run_v2.EnvVar(name="SMS_MESSAGE_SID", value=message_sid),
            ])

        run_request = run_v2.RunJobRequest(
            name=job_path,
            overrides=run_v2.RunJobRequest.Overrides(
                container_overrides=[
                    run_v2.RunJobRequest.Overrides.ContainerOverride(env=env_vars),
                ],
            ),
        )

        operation = jobs_client.run_job(request=run_request)
        execution_name = (
            operation.metadata.name
            if hasattr(operation, "metadata") else "unknown"
        )

        _log_structured(trace_id, "job_trigger", "success", {
            "job": job_name, "execution": execution_name, "mode": mode,
        })

    except Exception as e:
        logger.error(f"Failed to trigger sms-coach: {e}", exc_info=True)
        _log_structured(trace_id, "job_trigger", "failure", {"error": str(e)})


def handle_sms(request):
    """Handle incoming Twilio SMS webhook — trigger Cloud Run Job immediately."""
    trace_id = str(uuid.uuid4())

    if not _verify_twilio_signature(request):
        logger.warning("Invalid Twilio signature - rejecting request")
        return ({"error": "invalid signature"}, 401)

    form = request.form
    from_number = form.get("From", "")
    body = form.get("Body", "")
    message_sid = form.get("MessageSid", "")

    if not body:
        return "<Response></Response>", 200, {"Content-Type": "text/xml"}

    _log_structured(trace_id, "sms_received", metadata={
        "from": from_number, "message_sid": message_sid,
        "text_preview": body[:120],
    })

    _trigger_sms_coach(trace_id, body, from_number, message_sid)

    # Return empty TwiML (no auto-reply — the job sends via API)
    return "<Response></Response>", 200, {"Content-Type": "text/xml"}


def trigger_sms_morning(request):
    """Trigger the daily morning coaching text.

    Called by Cloud Scheduler at 7 AM Central.
    """
    trace_id = str(uuid.uuid4())
    _log_structured(trace_id, "morning_trigger")

    _trigger_sms_coach(trace_id, mode="morning")

    return {"ok": True, "trace_id": trace_id}
