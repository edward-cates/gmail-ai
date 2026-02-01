"""Structured logging to Cloud Storage."""

import json
from datetime import datetime
from typing import Any

from google.cloud import storage


class CloudLogger:
    """Logger that writes structured JSON logs to Cloud Storage."""

    def __init__(self, bucket_name: str, project_id: str) -> None:
        """Initialize Cloud Logger.

        Args:
            bucket_name: Cloud Storage bucket name
            project_id: Google Cloud project ID
        """
        self.bucket_name = bucket_name
        self.project_id = project_id
        self._client: storage.Client | None = None

    def _get_client(self) -> storage.Client:
        """Get or create Cloud Storage client."""
        if self._client is None:
            self._client = storage.Client(project=self.project_id)
        return self._client

    def log(
        self,
        trace_id: str,
        email_id: str,
        stage: str,
        result: str = "success",
        duration_ms: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Write a log entry to Cloud Storage.

        Args:
            trace_id: Unique trace ID for correlation
            email_id: Gmail message ID
            stage: Stage of processing (entry, fetch, complete, etc.)
            result: Result status (success, failure, skipped)
            duration_ms: Duration in milliseconds (optional)
            metadata: Additional metadata (optional)
        """
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "trace_id": trace_id,
            "email_id": email_id,
            "stage": stage,
            "result": result,
        }

        if duration_ms is not None:
            log_entry["duration_ms"] = duration_ms

        if metadata:
            log_entry["metadata"] = metadata

        # Partition by date: YYYY/MM/DD
        date_str = datetime.utcnow().strftime("%Y/%m/%d")
        blob_path = f"logs/{date_str}/log.jsonl"

        try:
            client = self._get_client()
            bucket = client.bucket(self.bucket_name)
            blob = bucket.blob(blob_path)

            # Append to JSONL file (read existing, append new line, write back)
            existing_content = ""
            if blob.exists():
                existing_content = blob.download_as_text()

            new_line = json.dumps(log_entry, ensure_ascii=False)
            updated_content = existing_content + new_line + "\n"

            blob.upload_from_string(updated_content, content_type="application/jsonl")

        except Exception as e:
            # If Cloud Storage write fails, log to Cloud Logging (standard Python logging)
            # This ensures we don't lose error information
            import logging

            logger = logging.getLogger(__name__)
            logger.error(
                f"Failed to write log to Cloud Storage: {e}",
                extra={
                    "trace_id": trace_id,
                    "email_id": email_id,
                    "stage": stage,
                    "log_entry": log_entry,
                },
            )
            # Don't raise - just log and continue (per design: no retry/catch-up)
