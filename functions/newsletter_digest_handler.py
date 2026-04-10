"""Newsletter digest trigger - kicks off the newsletter-digest Cloud Run Job."""

import logging
import os
import sys
from typing import Any

from google.cloud import run_v2

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)


def trigger_newsletter_digest(request: Any = None) -> dict[str, Any]:
    """Trigger the newsletter-digest Cloud Run Job.

    Called by Cloud Scheduler at 7 AM CT daily.
    """
    project_id = os.getenv("GMAIL_AI_PROJECT_ID", "")
    location = os.getenv("GMAIL_AI_LOCATION", "us-central1")
    job_name = os.getenv("NEWSLETTER_DIGEST_JOB_NAME", "newsletter-digest")

    try:
        jobs_client = run_v2.JobsClient()
        job_path = f"projects/{project_id}/locations/{location}/jobs/{job_name}"

        request = run_v2.RunJobRequest(name=job_path)
        operation = jobs_client.run_job(request=request)
        execution_name = operation.metadata.name if hasattr(operation, "metadata") else "unknown"

        logger.info(f"Triggered newsletter-digest job: {execution_name}")
        return {"status": "triggered", "execution": execution_name}

    except Exception as e:
        logger.error(f"Failed to trigger newsletter-digest: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}, 500
