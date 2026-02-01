"""Cloud Run service for processing individual emails.

This service receives HTTP requests from Cloud Tasks, classifies emails,
and logs the results.
"""

import json
import logging
import os
import tempfile
from typing import Any

from flask import Flask, jsonify, request

app = Flask(__name__)
from google.cloud import storage

from gmail_ai_unsub.classifier.email_classifier import ClassificationResult, create_classifier
from gmail_ai_unsub.cloud.email_fetcher import fetch_email_full
from gmail_ai_unsub.cloud.logging import CloudLogger
from gmail_ai_unsub.config import Config
from gmail_ai_unsub.gmail.client import GmailClient

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


def _get_api_key(config: Config) -> str | None:
    """Get API key from environment variable."""
    api_key_env = config.llm_api_key_env
    api_key = os.getenv(api_key_env)
    if not api_key:
        logger.error(f"API key not found in environment variable: {api_key_env}")
    return api_key


def _log(
    cloud_logger: CloudLogger | None,
    trace_id: str,
    email_id: str,
    stage: str,
    result: str = "success",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Log to Cloud Storage, silently failing if logging fails."""
    if cloud_logger:
        try:
            cloud_logger.log(trace_id=trace_id, email_id=email_id, stage=stage, result=result, metadata=metadata)
        except Exception:
            pass  # Silently fail - logging shouldn't break processing


def process_email(request_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Cloud Run entry point for processing a single email.

    Args:
        request_data: Request data from Cloud Task (contains email_id, trace_id)

    Returns:
        dict with status and classification result
    """
    cloud_logger = None
    trace_id = "unknown"
    email_id = "unknown"

    try:
        # Parse request
        if request_data is None:
            if request.is_json:
                request_data = request.get_json()
            else:
                request_data = json.loads(request.get_data(as_text=True))

        email_id = request_data.get("email_id", "")
        trace_id = request_data.get("trace_id", "")

        assert email_id, "email_id is required"
        assert trace_id, "trace_id is required"

        # Initialize config and logger
        config_path = os.getenv("GMAIL_AI_CONFIG_PATH") or _get_config_file()
        config = Config(config_path) if config_path else Config()
        cloud_logger = CloudLogger(
            bucket_name=config.cloud_storage_bucket, project_id=config.cloud_project_id
        )

        # Log task start
        _log(cloud_logger, trace_id, email_id, "task_start", "success", {"message": "Task started"})

        # Initialize Gmail client
        token_file = _get_token_file(config)
        client = GmailClient(
            credentials_file=config.gmail_credentials_file,
            token_file=token_file,
            use_default_credentials=True,
        )

        # Fetch full email (including body)
        email_data = fetch_email_full(client, email_id)
        assert email_data, f"Failed to fetch email {email_id}"

        subject = email_data.get("subject", "(No subject)")
        from_address = email_data.get("from", "")
        snippet = email_data.get("snippet", "")
        body = email_data.get("body", "")

        # Get API key
        api_key = _get_api_key(config)
        assert api_key, f"API key not found in environment variable: {config.llm_api_key_env}"

        # Create classifier
        classifier = create_classifier(
            provider=config.llm_provider,  # type: ignore
            model=config.llm_model,
            api_key=api_key,
            system_prompt=config.prompt_system,
            marketing_criteria=config.prompt_marketing_criteria,
            exclusions=config.prompt_exclusions,
            temperature=config.llm_temperature,
            user_preferences=config.prompt_user_preferences,
        )

        # Classify email
        classification_result: ClassificationResult = classifier.classify_sync(
            subject=subject,
            from_address=from_address,
            body=body,
        )

        # Log classification result
        _log(
            cloud_logger,
            trace_id,
            email_id,
            "classification",
            "success",
            {
                "category": "marketing" if classification_result.is_marketing else "other",
                "is_marketing": classification_result.is_marketing,
                "confidence": classification_result.confidence,
                "reason": classification_result.reason,
                "model": config.llm_model,
                "provider": config.llm_provider,
                "subject": subject,
            },
        )

        return {
            "status": "success",
            "email_id": email_id,
            "trace_id": trace_id,
            "classification": {
                "is_marketing": classification_result.is_marketing,
                "confidence": classification_result.confidence,
                "reason": classification_result.reason,
            },
        }

    except AssertionError as e:
        error_msg = str(e)
        logger.error(f"Validation error: {error_msg}")
        _log(cloud_logger, trace_id, email_id, "task_start", "failure", {"error": error_msg})
        return {"status": "error", "error": error_msg}

    except Exception as e:
        error_msg = f"Error processing email {email_id}: {e}"
        logger.error(error_msg, exc_info=True)
        _log(cloud_logger, trace_id, email_id, "task_start", "failure", {"error": str(e)})
        return {"status": "error", "error": str(e)}


@app.route("/process", methods=["POST"])
def process_email_http() -> Any:
    """Cloud Run HTTP entry point for /process endpoint.

    Returns:
        JSON response
    """
    return jsonify(process_email(None))


@app.route("/", methods=["GET"])
def health_check() -> Any:
    """Health check endpoint."""
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
