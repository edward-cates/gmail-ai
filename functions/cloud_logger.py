"""Cloud Storage logging - standalone."""

import json
import os
from datetime import UTC, datetime
from typing import Any

from google.cloud import storage


class CloudLogger:
    """Logger that writes structured JSON logs to Cloud Storage."""

    def __init__(self, bucket_name: str | None = None, project_id: str | None = None):
        self.bucket_name = bucket_name or os.getenv("GMAIL_AI_STORAGE_BUCKET", "")
        self.project_id = project_id or os.getenv("GMAIL_AI_PROJECT_ID", "")
        self._client: storage.Client | None = None

    def _get_client(self) -> storage.Client:
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
        service: str | None = None,
    ) -> None:
        """Write a log entry to Cloud Storage."""
        log_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "trace_id": trace_id,
            "email_id": email_id,
            "stage": stage,
            "result": result,
        }

        if service:
            log_entry["service"] = service
        if duration_ms is not None:
            log_entry["duration_ms"] = duration_ms
        if metadata:
            log_entry["metadata"] = metadata

        date_str = datetime.now(UTC).strftime("%Y/%m/%d")
        blob_path = f"logs/{date_str}/log.jsonl"

        try:
            client = self._get_client()
            bucket = client.bucket(self.bucket_name)
            blob = bucket.blob(blob_path)

            existing = ""
            if blob.exists():
                existing = blob.download_as_text()

            new_line = json.dumps(log_entry, ensure_ascii=False)
            blob.upload_from_string(existing + new_line + "\n", content_type="application/jsonl")

        except Exception:
            pass  # Silently fail - logging shouldn't break processing
