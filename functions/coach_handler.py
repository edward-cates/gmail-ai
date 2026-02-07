"""Coach handler - receives Trello webhooks and triggers coach job.

Trello webhook fires on board actions. We filter for commentCard events,
skip comments from the coach's own member, and trigger the Cloud Run Job
immediately for real-time conversation.
"""

import json
import logging
import os
import sys
import uuid

import requests
from google.cloud import run_v2

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

_coach_member_id = None
_resolved_board_id = None


def _log_structured(trace_id, stage, result="success", metadata=None):
    """Log structured JSON to Cloud Logging."""
    log_data = {
        "trace_id": trace_id,
        "stage": stage,
        "result": result,
        "service": "coach-handler",
    }
    if metadata:
        log_data["metadata"] = metadata
    print(json.dumps(log_data), flush=True)


def _resolve_board_id():
    """Resolve the short board ID to a full ID (cached).

    Trello webhooks send the full 24-char hex board ID, but the env var
    may contain the short URL slug (e.g. 'IrsYdZHV'). Resolve once.
    """
    global _resolved_board_id  # noqa: PLW0603
    if _resolved_board_id:
        return _resolved_board_id

    short_id = os.getenv("TRELLO_COACH_BOARD_ID", "")
    if not short_id:
        return ""

    # Already a full ID (24 hex chars)?
    if len(short_id) == 24:
        _resolved_board_id = short_id
        return _resolved_board_id

    api_key = os.getenv("TRELLO_API_KEY", "")
    token = os.getenv("TRELLO_TOKEN", "")
    if not api_key or not token:
        return short_id

    try:
        r = requests.get(
            f"https://api.trello.com/1/boards/{short_id}",
            params={"key": api_key, "token": token, "fields": "id"},
            timeout=10,
        )
        r.raise_for_status()
        _resolved_board_id = r.json()["id"]
        return _resolved_board_id
    except Exception as e:
        logger.error(f"Failed to resolve board ID: {e}")
        return short_id


def _get_coach_member_id():
    """Get the Trello member ID for the coach's API token (cached)."""
    global _coach_member_id  # noqa: PLW0603
    if _coach_member_id:
        return _coach_member_id

    api_key = os.getenv("TRELLO_API_KEY", "")
    token = os.getenv("TRELLO_TOKEN", "")
    if not api_key or not token:
        return None

    try:
        r = requests.get(
            "https://api.trello.com/1/members/me",
            params={"key": api_key, "token": token, "fields": "id"},
            timeout=10,
        )
        r.raise_for_status()
        _coach_member_id = r.json()["id"]
        return _coach_member_id
    except Exception as e:
        logger.error(f"Failed to get coach member ID: {e}")
        return None


def _trigger_coach_job(trace_id, comment_text="", card_id="", mode="reply"):
    """Trigger the coach Cloud Run Job."""
    project_id = os.getenv("GMAIL_AI_PROJECT_ID", "")
    location = os.getenv("GMAIL_AI_LOCATION", "us-central1")
    job_name = os.getenv("COACH_JOB_NAME", "coach")

    try:
        jobs_client = run_v2.JobsClient()
        job_path = f"projects/{project_id}/locations/{location}/jobs/{job_name}"

        env_vars = [
            run_v2.EnvVar(name="TRACE_ID", value=trace_id),
            run_v2.EnvVar(name="COACH_MODE", value=mode),
        ]
        if mode == "reply":
            env_vars.extend([
                run_v2.EnvVar(name="COMMENT_TEXT", value=comment_text),
                run_v2.EnvVar(name="CARD_ID", value=card_id),
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
        logger.error(f"Failed to trigger coach job: {e}", exc_info=True)
        _log_structured(trace_id, "job_trigger", "failure", {"error": str(e)})


def handle_trello_webhook(request):
    """Handle incoming Trello webhook — trigger coach job for user comments."""
    # Trello sends HEAD to verify callback URL exists
    if request.method == "HEAD":
        return ({"ok": True}, 200)

    trace_id = str(uuid.uuid4())

    try:
        body = request.get_json(silent=True) or {}
    except Exception:
        return ({"error": "invalid json"}, 400)

    action = body.get("action", {})
    action_type = action.get("type", "")

    # Only process card comments
    if action_type != "commentCard":
        return ({"ok": True, "skipped": action_type}, 200)

    # Skip comments from the coach itself (avoid infinite loop)
    commenter_id = action.get("memberCreator", {}).get("id", "")
    coach_id = _get_coach_member_id()
    if coach_id and commenter_id == coach_id:
        return ({"ok": True, "skipped": "own_comment"}, 200)

    # Verify the action is on our expected board
    board_id = action.get("data", {}).get("board", {}).get("id", "")
    expected_board = _resolve_board_id()
    if expected_board and board_id != expected_board:
        return ({"ok": True, "skipped": "wrong_board"}, 200)

    # Extract comment details
    card_id = action.get("data", {}).get("card", {}).get("id", "")
    comment_text = action.get("data", {}).get("text", "")
    commenter_name = action.get("memberCreator", {}).get("fullName", "Unknown")

    if not comment_text or not card_id:
        return ({"ok": True, "skipped": "empty"}, 200)

    _log_structured(trace_id, "comment_received", metadata={
        "commenter": commenter_name,
        "card_id": card_id,
        "text_preview": comment_text[:120],
    })

    _trigger_coach_job(trace_id, comment_text, card_id)

    return ({"ok": True, "trace_id": trace_id}, 200)


def trigger_coach_morning(request):
    """Trigger the daily morning coaching card.

    Called by Cloud Scheduler at 7 AM Central.
    """
    trace_id = str(uuid.uuid4())
    _log_structured(trace_id, "morning_trigger")

    _trigger_coach_job(trace_id, mode="morning")

    return {"ok": True, "trace_id": trace_id}
