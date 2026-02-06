"""Dashboard for viewing email and Slack processing logs from Cloud Logging."""

import json
import os
from collections import defaultdict
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
            )
            AND (textPayload : "trace_id" OR jsonPayload.trace_id != "")
        '''

        logs: list[dict[str, Any]] = []

        for entry in client.list_entries(filter_=filter_str, order_by=cloud_logging.DESCENDING, max_results=500):
            try:
                payload = entry.payload if hasattr(entry, "payload") else None
                if not payload:
                    continue

                # If payload is already a dict (jsonPayload), use it directly
                if isinstance(payload, dict):
                    log_data = payload
                else:
                    # Parse from text (textPayload with logging prefix)
                    text = str(payload)
                    if "{" in text:
                        text = text[text.index("{"):]
                    log_data = json.loads(text)

                # Add timestamp from entry if not in payload
                if "timestamp" not in log_data and hasattr(entry, "timestamp"):
                    log_data["timestamp"] = entry.timestamp.isoformat() if entry.timestamp else ""
                logs.append(log_data)
            except (json.JSONDecodeError, AttributeError, ValueError):
                continue

        return logs

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Failed to read logs: {e}")
        return []


def group_logs_by_trace(logs: list[dict]) -> list[dict]:
    """Group logs by trace_id and build summary for each trace."""
    traces: dict[str, list[dict]] = defaultdict(list)

    for log in logs:
        trace_id = log.get("trace_id", "unknown")
        traces[trace_id].append(log)

    groups = []
    for trace_id, entries in traces.items():
        # Sort entries within group chronologically (oldest first)
        entries.sort(key=lambda e: e.get("timestamp", ""))

        # Build summary from entries
        summary = _build_trace_summary(trace_id, entries)
        groups.append({
            "trace_id": trace_id,
            "summary": summary,
            "entries": entries,
        })

    # Sort groups by most recent first
    groups.sort(key=lambda g: g["summary"].get("latest_timestamp", ""), reverse=True)
    return groups


def _build_trace_summary(trace_id: str, entries: list[dict]) -> dict:
    """Build a summary object for a trace group."""
    services = set()
    stages = []
    has_failure = False
    subject = None
    email_id = None
    channel = None
    topic = None
    category = None
    priority = None
    text_preview = None
    batch_messages = None
    batch_reactions = None
    reaction = None
    earliest = None
    latest = None

    for entry in entries:
        service = entry.get("service", "")
        if service:
            services.add(service)

        stage = entry.get("stage", "")
        stages.append(stage)

        if entry.get("result") == "failure":
            has_failure = True

        ts = entry.get("timestamp", "")
        if ts:
            if earliest is None or ts < earliest:
                earliest = ts
            if latest is None or ts > latest:
                latest = ts

        eid = entry.get("email_id")
        if eid and eid != "unknown":
            email_id = eid

        meta = entry.get("metadata") or {}

        if not subject and meta.get("subject"):
            subject = meta["subject"]

        if not channel and meta.get("channel"):
            channel = meta["channel"]

        if not text_preview and meta.get("text_preview"):
            text_preview = meta["text_preview"]

        if not reaction and meta.get("reaction"):
            reaction = meta["reaction"]

        # Email classification
        if stage == "classification" and meta.get("category"):
            category = meta["category"]

        # Slack classification
        if stage == "classification" and meta.get("priority"):
            priority = meta["priority"]

        # Slack topic
        if meta.get("topic"):
            topic = meta["topic"]

        # Batch stats
        if stage == "batch_complete":
            batch_messages = meta.get("messages")
            batch_reactions = meta.get("reactions")

    # Determine pipeline type
    if "slack-handler" in services or "slack-processor" in services:
        pipeline = "slack"
    else:
        pipeline = "email"

    # Build display title
    if batch_messages is not None:
        title = f"\U0001f4e6 Batch: {batch_messages} messages, {batch_reactions or 0} reactions"
    elif pipeline == "slack" and topic:
        title = topic
    elif pipeline == "slack" and text_preview:
        title = text_preview
    elif pipeline == "slack" and reaction:
        title = f":{reaction}: in #{channel or '?'}"
    elif subject:
        title = subject
    elif pipeline == "slack" and channel:
        title = f"Message in #{channel}"
    else:
        title = f"Trace {trace_id[:8]}"

    return {
        "title": title,
        "pipeline": pipeline,
        "services": sorted(services),
        "stages": stages,
        "status": "failure" if has_failure else "success",
        "category": category,
        "priority": priority,
        "email_id": email_id,
        "channel": channel,
        "topic": topic,
        "text_preview": text_preview,
        "batch_messages": batch_messages,
        "batch_reactions": batch_reactions,
        "entry_count": len(entries),
        "earliest_timestamp": earliest or "",
        "latest_timestamp": latest or "",
    }


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
    groups = group_logs_by_trace(logs)

    # Stats
    email_count = sum(1 for g in groups if g["summary"]["pipeline"] == "email")
    slack_count = sum(1 for g in groups if g["summary"]["pipeline"] == "slack")
    failure_count = sum(1 for g in groups if g["summary"]["status"] == "failure")

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "groups": groups,
        "total_traces": len(groups),
        "total_logs": len(logs),
        "email_count": email_count,
        "slack_count": slack_count,
        "failure_count": failure_count,
    })


@app.get("/api/logs")
async def api_logs(hours: int = 24) -> dict:
    """API endpoint for logs (JSON)."""
    project_id = os.getenv("GMAIL_AI_PROJECT_ID", "")

    if not project_id:
        return {"error": "Missing config", "logs": []}

    logs = get_logs_from_cloud_logging(project_id, hours=hours)
    groups = group_logs_by_trace(logs)
    return {"groups": groups, "total_traces": len(groups), "total_logs": len(logs)}
