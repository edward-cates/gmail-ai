"""Cloud Function entry point for Pub/Sub handler.

This is the entry point for Google Cloud Functions.
The function is triggered by Pub/Sub messages from Gmail Watch.
"""

import sys
from pathlib import Path

# Add src directory to path so we can import gmail_ai_unsub
src_path = Path(__file__).parent / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

from gmail_ai_unsub.cloud.pubsub_handler import handle_pubsub


def handle_pubsub_event(event, context):
    """Cloud Function entry point for Pub/Sub trigger.

    Args:
        event: Pub/Sub event data (dict with 'data' key containing base64 message)
        context: Cloud Function context (metadata about the event)
    """
    handle_pubsub(event, context)
