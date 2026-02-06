"""Cloud Function entry points.

Entry point for Google Cloud Functions:
- handle_pubsub_event: Triggered by Pub/Sub (Gmail Watch)
- renew_watch_http: Triggered by Cloud Scheduler
- handle_slack_event: Triggered by Slack Events API (HTTP)
- trigger_slack_batch_http: Triggered by Cloud Scheduler (every 2 min)
"""

from functions.pubsub_handler import handle_pubsub
from functions.slack_handler import handle_slack, trigger_slack_batch
from functions.watch_renewal import renew_watch


def handle_pubsub_event(event, context):
    """Cloud Function entry point for Pub/Sub trigger."""
    handle_pubsub(event, context)


def renew_watch_http(request):
    """Cloud Function entry point for HTTP trigger to renew Gmail Watch."""
    from flask import jsonify
    result = renew_watch(request)
    return jsonify(result)


def handle_slack_event(request):
    """Cloud Function entry point for Slack Events API webhook."""
    from flask import jsonify
    result = handle_slack(request)
    if isinstance(result, tuple):
        body, status = result
        return jsonify(body), status
    return jsonify(result)


def trigger_slack_batch_http(request):
    """Cloud Function entry point for Slack batch processing (Cloud Scheduler)."""
    from flask import jsonify
    result = trigger_slack_batch(request)
    if isinstance(result, tuple):
        body, status = result
        return jsonify(body), status
    return jsonify(result)
