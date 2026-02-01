"""Cloud Function entry point for Pub/Sub handler and Watch renewal.

This is the entry point for Google Cloud Functions.
- handle_pubsub_event: Triggered by Pub/Sub messages from Gmail Watch
- renew_watch: Triggered by Cloud Scheduler to renew Gmail Watch subscription
"""

import sys
from pathlib import Path

# Add src directory to path so we can import gmail_ai_unsub
src_path = Path(__file__).parent / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

from gmail_ai_unsub.cloud.pubsub_handler import handle_pubsub
from gmail_ai_unsub.cloud.watch_renewal import renew_watch


def handle_pubsub_event(event, context):
    """Cloud Function entry point for Pub/Sub trigger.

    Args:
        event: Pub/Sub event data (dict with 'data' key containing base64 message)
        context: Cloud Function context (metadata about the event)
    """
    handle_pubsub(event, context)


def renew_watch_http(request):
    """Cloud Function entry point for HTTP trigger to renew Gmail Watch.

    Args:
        request: HTTP request (Flask request object, not used)

    Returns:
        dict with status and details about the renewal (converted to JSON response)
    """
    from flask import jsonify
    result = renew_watch(None)
    return jsonify(result)
