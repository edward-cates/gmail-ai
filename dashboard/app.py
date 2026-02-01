"""Dashboard for viewing email processing logs from Cloud Logging."""

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from google.cloud import logging as cloud_logging

app = FastAPI(title="Gmail AI Dashboard")

templates_dir = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_dir)


def get_logs_from_cloud_logging(project_id: str, hours: int = 24) -> list[dict]:
    """Read structured logs from Cloud Logging."""
    try:
        client = cloud_logging.Client(project=project_id)

        # Calculate time filter
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        timestamp_filter = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Query for our structured logs from all services
        filter_str = f'''
            timestamp >= "{timestamp_filter}"
            AND (
                resource.type = "cloud_run_revision"
                OR resource.type = "cloud_run_job"
                OR resource.type = "cloud_function"
            )
            AND textPayload : "trace_id"
        '''

        logs: list[dict[str, Any]] = []

        for entry in client.list_entries(filter_=filter_str, order_by=cloud_logging.DESCENDING, max_results=500):
            try:
                # Parse structured JSON from text payload
                if hasattr(entry, "text_payload") and entry.text_payload:
                    log_data = json.loads(entry.text_payload)
                    # Add timestamp from entry if not in payload
                    if "timestamp" not in log_data and hasattr(entry, "timestamp"):
                        log_data["timestamp"] = entry.timestamp.isoformat() if entry.timestamp else ""
                    logs.append(log_data)
            except (json.JSONDecodeError, AttributeError):
                continue

        return logs

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Failed to read logs: {e}")
        return []


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Dashboard home page."""
    project_id = os.getenv("GMAIL_AI_PROJECT_ID", "")

    if not project_id:
        return templates.TemplateResponse("error.html", {
            "request": request,
            "error": "Set GMAIL_AI_PROJECT_ID environment variable",
        })

    logs = get_logs_from_cloud_logging(project_id, hours=24)
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "logs": logs,
        "log_count": len(logs),
    })


@app.get("/api/logs")
async def api_logs(hours: int = 24) -> dict:
    """API endpoint for logs (JSON)."""
    project_id = os.getenv("GMAIL_AI_PROJECT_ID", "")

    if not project_id:
        return {"error": "Missing config", "logs": []}

    logs = get_logs_from_cloud_logging(project_id, hours=hours)
    return {"logs": logs, "count": len(logs)}
