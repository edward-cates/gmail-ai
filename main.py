"""Cloud Function entry points.

Entry point for Google Cloud Functions:
- handle_pubsub_event: Triggered by Pub/Sub (Gmail Watch)
- renew_watch_http: Triggered by Cloud Scheduler
- handle_slack_event: Triggered by Slack Events API (HTTP)
- trigger_slack_batch_http: Triggered by Cloud Scheduler (every 15 min)
- handle_sms_event: Triggered by Twilio SMS webhook (HTTP)
- trigger_sms_morning_http: Triggered by Cloud Scheduler (daily 7 AM CT)
- handle_coach_webhook: Triggered by Trello webhook (card comments)
- trigger_coach_morning_http: Triggered by Cloud Scheduler (daily 7 AM CT)
"""

from functions.coach_handler import handle_trello_webhook, trigger_coach_morning
from functions.pubsub_handler import handle_pubsub
from functions.slack_handler import handle_slack, trigger_slack_batch
from functions.sms_handler import handle_sms, trigger_sms_morning
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


def handle_sms_event(request):
    """Cloud Function entry point for Twilio SMS webhook."""
    result = handle_sms(request)
    if isinstance(result, tuple):
        body = result[0]
        status = result[1]
        headers = result[2] if len(result) > 2 else {}
        if isinstance(body, str):
            return body, status, headers
        from flask import jsonify
        return jsonify(body), status
    from flask import jsonify
    return jsonify(result)


def trigger_sms_morning_http(request):
    """Cloud Function entry point for daily morning SMS (Cloud Scheduler)."""
    from flask import jsonify
    result = trigger_sms_morning(request)
    if isinstance(result, tuple):
        body, status = result
        return jsonify(body), status
    return jsonify(result)


def handle_coach_webhook(request):
    """Cloud Function entry point for Trello webhook (coach)."""
    # Trello sends HEAD to verify callback URL exists
    if request.method == "HEAD":
        return "", 200
    from flask import jsonify
    result = handle_trello_webhook(request)
    if isinstance(result, tuple):
        body, status = result
        return jsonify(body), status
    return jsonify(result)


def trigger_coach_morning_http(request):
    """Cloud Function entry point for daily morning coach (Cloud Scheduler)."""
    from flask import jsonify
    result = trigger_coach_morning(request)
    if isinstance(result, tuple):
        body, status = result
        return jsonify(body), status
    return jsonify(result)
