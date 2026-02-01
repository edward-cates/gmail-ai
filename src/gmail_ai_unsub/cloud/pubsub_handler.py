"""Pub/Sub handler for Gmail Watch notifications."""

import base64
import json
import logging
import os
import tempfile
import uuid
from typing import Any

from google.cloud import run_v2, storage, tasks_v2

from gmail_ai_unsub.cloud.email_fetcher import fetch_email_metadata
from gmail_ai_unsub.cloud.logging import CloudLogger
from gmail_ai_unsub.config import Config
from gmail_ai_unsub.gmail.client import GmailClient
from gmail_ai_unsub.gmail.labels import LabelManager

logger = logging.getLogger(__name__)


def _is_cloud_environment() -> bool:
    """Check if we're running in a cloud environment."""
    return bool(os.getenv("FUNCTION_TARGET") or os.getenv("K_SERVICE") or os.getenv("GAE_ENV"))


def _get_config_file() -> str | None:
    """Get config file path, loading from Cloud Storage if in cloud environment."""
    if _is_cloud_environment():
        bucket_name = os.getenv("GMAIL_AI_STORAGE_BUCKET", "gmail-ai-logs")
        project_id = os.getenv("GMAIL_AI_PROJECT_ID", "")

        if bucket_name and project_id:
            try:
                storage_client = storage.Client(project=project_id)
                bucket = storage_client.bucket(bucket_name)
                blob = bucket.blob("config.toml")

                if blob.exists():
                    temp_dir = tempfile.gettempdir()
                    temp_config_file = os.path.join(temp_dir, "gmail_config.toml")
                    blob.download_to_filename(temp_config_file)
                    logger.info(f"Loaded config from Cloud Storage: gs://{bucket_name}/config.toml")
                    return temp_config_file
            except Exception as e:
                logger.warning(f"Failed to load config from Cloud Storage: {e}")

    return None


def _get_token_file(config: Config) -> str:
    """Get token file path, loading from Cloud Storage if in cloud environment."""
    if _is_cloud_environment():
        bucket_name = config.cloud_storage_bucket
        if bucket_name:
            try:
                storage_client = storage.Client(project=config.cloud_project_id)
                bucket = storage_client.bucket(bucket_name)
                blob = bucket.blob("token.json")

                if blob.exists():
                    temp_dir = tempfile.gettempdir()
                    temp_token_file = os.path.join(temp_dir, "gmail_token.json")
                    blob.download_to_filename(temp_token_file)
                    logger.info(f"Loaded token from Cloud Storage: gs://{bucket_name}/token.json")
                    return temp_token_file
            except Exception as e:
                logger.warning(f"Failed to load token from Cloud Storage: {e}")

    return config.gmail_token_file


def _log(
    cloud_logger: CloudLogger | None,
    trace_id: str,
    email_id: str,
    stage: str,
    result: str = "success",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Helper to log to Cloud Storage, silently fails if logger is None or write fails."""
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
            pass  # Silently fail - logging shouldn't break processing


def handle_pubsub(event: dict[str, Any], context: Any) -> None:
    """Cloud Function entry point for Pub/Sub messages."""
    trace_id = str(uuid.uuid4())
    cloud_logger = None

    try:
        # Initialize config and logger
        config_path = os.getenv("GMAIL_AI_CONFIG_PATH") or _get_config_file()
        config = Config(config_path) if config_path else Config()
        cloud_logger = CloudLogger(
            bucket_name=config.cloud_storage_bucket, project_id=config.cloud_project_id
        )

        # Parse Pub/Sub message
        if "data" in event:
            message_data = base64.b64decode(event["data"]).decode("utf-8")
            pubsub_message = json.loads(message_data)
        else:
            pubsub_message = event

        history_id = str(pubsub_message.get("historyId", ""))
        assert history_id, "Pub/Sub message must contain historyId"

        # Initialize Gmail client
        token_file = _get_token_file(config)
        client = GmailClient(
            credentials_file=config.gmail_credentials_file,
            token_file=token_file,
            use_default_credentials=True,
        )

        # Query inbox for emails without 🤖 tag
        label_manager = LabelManager(client.service)
        processing_label = config.cloud_processing_label
        label_id = label_manager.get_or_create_label(processing_label)

        query = f"is:inbox -label:{processing_label}"
        result = client.list_messages(query=query, max_results=10, page_token=None)
        email_ids = [msg["id"] for msg in result.get("messages", [])]

        # Log entry and query
        _log(
            cloud_logger,
            trace_id,
            "unknown",
            "entry",
            "success",
            {"event_keys": list(event.keys())},
        )
        _log(
            cloud_logger,
            trace_id,
            "unknown",
            "query",
            "success",
            {"query": query, "count": len(email_ids)},
        )

        # Process each email
        for email_id in email_ids:
            _process_email_for_task(
                client=client,
                label_manager=label_manager,
                email_id=email_id,
                label_id=label_id,
                trace_id=trace_id,
                cloud_logger=cloud_logger,
                config=config,
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
    config: Config,
) -> None:
    """Process a single email: check if already processed, fetch metadata, mark, archive, create task."""
    try:
        # Check if already processed
        message = client.get_message_metadata(email_id)
        if label_id in message.get("labelIds", []):
            _log(
                cloud_logger,
                trace_id,
                email_id,
                "skip",
                "success",
                {"message": "Already processed"},
            )
            return

        # Fetch metadata
        metadata = fetch_email_metadata(client, email_id)
        assert metadata, f"Failed to fetch metadata for email {email_id}"
        subject = metadata.get("subject", "(No subject)")
        snippet = metadata.get("snippet", "")

        # Mark with 🤖 tag
        label_manager.apply_label(email_id, label_id)
        _log(
            cloud_logger,
            trace_id,
            email_id,
            "mark",
            "success",
            {
                "label": config.cloud_processing_label,
                "subject": subject,
                "snippet": snippet[:200] if snippet else "",
            },
        )

        # Create Cloud Task
        try:
            # Get Cloud Run service URL from API
            # This ensures we have the correct URL even if service name changes
            run_client = run_v2.ServicesClient()
            service_name = f"projects/{config.cloud_project_id}/locations/{config.cloud_tasks_location}/services/{config.cloud_run_service}"
            service = run_client.get_service(name=service_name)
            service_url = service.uri

            # Create Cloud Tasks client
            tasks_client = tasks_v2.CloudTasksClient()
            queue_path = tasks_client.queue_path(
                config.cloud_project_id, config.cloud_tasks_location, config.cloud_tasks_queue
            )

            # Task payload
            task_payload = {
                "email_id": email_id,
                "trace_id": trace_id,
            }
            payload = json.dumps(task_payload).encode()

            # Task name for deduplication (1-hour window)
            # Use email_id directly as task name (Cloud Tasks will handle the full path)
            task_name = f"email-{email_id}"

            # Create task with OIDC authentication for Cloud Run
            # Get service account email for OIDC token
            service_account_email = f"{config.cloud_project_id}-compute@developer.gserviceaccount.com"
            
            # Create task
            task = tasks_v2.Task(
                name=tasks_client.task_path(
                    config.cloud_project_id,
                    config.cloud_tasks_location,
                    config.cloud_tasks_queue,
                    task_name,
                ),
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

            # Create the task
            tasks_client.create_task(parent=queue_path, task=task)

            _log(
                cloud_logger,
                trace_id,
                email_id,
                "task_create",
                "success",
                {
                    "task_name": task_name,
                    "queue": config.cloud_tasks_queue,
                    "location": config.cloud_tasks_location,
                    "service": config.cloud_run_service,
                    "service_url": service_url,
                    "subject": subject,
                },
            )

        except Exception as e:
            logger.error(f"Failed to create Cloud Task for email {email_id}: {e}", exc_info=True)
            _log(
                cloud_logger,
                trace_id,
                email_id,
                "task_create",
                "failure",
                {
                    "error": str(e),
                    "queue": config.cloud_tasks_queue,
                    "location": config.cloud_tasks_location,
                    "service": config.cloud_run_service,
                    "subject": subject,
                },
            )
            # Don't raise - continue processing other emails

    except AssertionError as e:
        logger.error(f"Validation error for email {email_id}: {e}")
        _log(cloud_logger, trace_id, email_id, "process", "failure", {"error": str(e)})
    except Exception as e:
        logger.error(f"Failed to process email {email_id}: {e}", exc_info=True)
        _log(cloud_logger, trace_id, email_id, "process", "failure", {"error": str(e)})
