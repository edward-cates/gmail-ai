"""Cloud Function entry points.

Entry point for Google Cloud Functions:
- handle_pubsub_event: Triggered by Pub/Sub (Gmail Watch)
- renew_watch_http: Triggered by Cloud Scheduler
"""

from functions.pubsub_handler import handle_pubsub
from functions.watch_renewal import renew_watch


def handle_pubsub_event(event, context):
    """Cloud Function entry point for Pub/Sub trigger."""
    handle_pubsub(event, context)


def renew_watch_http(request):
    """Cloud Function entry point for HTTP trigger to renew Gmail Watch."""
    from flask import jsonify
    result = renew_watch(request)
    return jsonify(result)
