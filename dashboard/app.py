"""Dashboard for viewing email processing logs - standalone."""

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from google.cloud import storage

app = FastAPI(title="Gmail AI Dashboard")

templates_dir = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_dir)


def get_logs_from_storage(bucket_name: str, project_id: str, hours: int = 24) -> list[dict]:
    """Read logs from Cloud Storage."""
    try:
        client = storage.Client(project=project_id)
        bucket = client.bucket(bucket_name)

        logs: list[dict[str, Any]] = []
        cutoff_time = datetime.now(UTC) - timedelta(hours=hours)

        blobs = bucket.list_blobs(prefix="logs/")

        for blob in blobs:
            parts = blob.name.split("/")
            if len(parts) >= 4 and parts[-1] == "log.jsonl":
                try:
                    year, month, day = int(parts[1]), int(parts[2]), int(parts[3])
                    blob_date = datetime(year, month, day).date()

                    if blob_date >= cutoff_time.date():
                        content = blob.download_as_text()
                        for line in content.strip().split("\n"):
                            if line:
                                try:
                                    entry = json.loads(line)
                                    ts = entry.get("timestamp", "").rstrip("Z")
                                    if ts:
                                        log_time = datetime.fromisoformat(ts)
                                        if log_time.replace(tzinfo=UTC) >= cutoff_time:
                                            logs.append(entry)
                                except json.JSONDecodeError:
                                    continue
                except (ValueError, IndexError):
                    continue

        logs.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return logs

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Failed to read logs: {e}")
        return []


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Dashboard home page."""
    bucket_name = os.getenv("GMAIL_AI_STORAGE_BUCKET", "")
    project_id = os.getenv("GMAIL_AI_PROJECT_ID", "")

    if not bucket_name or not project_id:
        return templates.TemplateResponse("error.html", {
            "request": request,
            "error": "Set GMAIL_AI_STORAGE_BUCKET and GMAIL_AI_PROJECT_ID environment variables",
        })

    logs = get_logs_from_storage(bucket_name, project_id, hours=24)
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "logs": logs,
        "log_count": len(logs),
    })


@app.get("/api/logs")
async def api_logs(hours: int = 24) -> dict:
    """API endpoint for logs (JSON)."""
    bucket_name = os.getenv("GMAIL_AI_STORAGE_BUCKET", "")
    project_id = os.getenv("GMAIL_AI_PROJECT_ID", "")

    if not bucket_name or not project_id:
        return {"error": "Missing config", "logs": []}

    logs = get_logs_from_storage(bucket_name, project_id, hours=hours)
    return {"logs": logs, "count": len(logs)}
