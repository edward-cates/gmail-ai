"""FastAPI dashboard app for viewing email processing logs."""

import os
from datetime import datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from google.cloud import storage

from gmail_ai_unsub.config import Config

app = FastAPI(title="Gmail AI Unsub Dashboard")

# Get templates directory
templates_dir = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_dir)


def get_config() -> Config:
    """Get config from environment or default location."""
    config_path = os.getenv("GMAIL_AI_CONFIG_PATH")
    return Config(config_path) if config_path else Config()


def get_logs_from_storage(
    bucket_name: str, project_id: str, hours: int = 24
) -> list[dict[str, Any]]:
    """Read logs from Cloud Storage.

    Args:
        bucket_name: Cloud Storage bucket name
        project_id: Google Cloud project ID
        hours: Number of hours of logs to retrieve

    Returns:
        List of log entries, sorted by timestamp (newest first)
    """
    try:
        client = storage.Client(project=project_id)
        bucket = client.bucket(bucket_name)

        # Get logs from last N days (to cover the hours requested)
        logs: list[dict[str, Any]] = []
        cutoff_time = datetime.utcnow() - timedelta(hours=hours)

        # List blobs in logs/ directory
        blobs = bucket.list_blobs(prefix="logs/")

        for blob in blobs:
            # Parse date from path: logs/YYYY/MM/DD/log.jsonl
            parts = blob.name.split("/")
            if len(parts) >= 4 and parts[-1] == "log.jsonl":
                try:
                    year = int(parts[1])
                    month = int(parts[2])
                    day = int(parts[3])
                    blob_date = datetime(year, month, day).date()

                    if blob_date >= cutoff_time.date():
                        # Read JSONL file
                        content = blob.download_as_text()
                        for line in content.strip().split("\n"):
                            if line:
                                try:
                                    import json

                                    log_entry = json.loads(line)
                                    # Parse timestamp and filter
                                    timestamp_str = log_entry.get("timestamp", "")
                                    if timestamp_str:
                                        # Remove Z and parse
                                        timestamp_str = timestamp_str.rstrip("Z")
                                        log_timestamp = datetime.fromisoformat(timestamp_str)
                                        if log_timestamp >= cutoff_time:
                                            logs.append(log_entry)
                                except json.JSONDecodeError:
                                    continue  # Skip invalid JSON lines
                except (ValueError, IndexError):
                    continue  # Skip invalid paths

        # Sort by timestamp (newest first)
        logs.sort(
            key=lambda x: x.get("timestamp", ""),
            reverse=True,
        )

        return logs

    except Exception as e:
        # Return empty list on error (dashboard should still load)
        import logging

        logger = logging.getLogger(__name__)
        logger.error(f"Failed to read logs from Cloud Storage: {e}")
        return []


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Dashboard home page showing recent email logs."""
    try:
        config = get_config()
        bucket_name = config.cloud_storage_bucket
        project_id = config.cloud_project_id

        if not bucket_name or not project_id:
            return templates.TemplateResponse(
                "error.html",
                {
                    "request": request,
                    "error": "Cloud configuration missing. Please set cloud.storage_bucket and cloud.project_id in config.toml",
                },
            )

        # Get logs from last 24 hours
        logs = get_logs_from_storage(bucket_name, project_id, hours=24)

        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "logs": logs,
                "log_count": len(logs),
            },
        )

    except Exception as e:
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "error": f"Error loading dashboard: {e}",
            },
        )


@app.get("/api/logs")
async def api_logs(hours: int = 24) -> dict[str, Any]:
    """API endpoint for logs (JSON)."""
    try:
        config = get_config()
        bucket_name = config.cloud_storage_bucket
        project_id = config.cloud_project_id

        if not bucket_name or not project_id:
            return {"error": "Cloud configuration missing", "logs": []}

        logs = get_logs_from_storage(bucket_name, project_id, hours=hours)

        return {
            "logs": logs,
            "count": len(logs),
        }

    except Exception as e:
        return {"error": str(e), "logs": []}
