"""SMS Coach - Cloud Run Job.

Muscle growth coaching agent. Two modes:
- morning: Read spec, generate daily coaching text, send via Twilio
- reply: Read spec + history, process inbound message, update spec, reply via Twilio

Reads SMS_MODE from environment:
- "morning": Send proactive daily coaching text
- "reply": Process inbound SMS and respond
"""

import json
import logging
import os
import re
import sys
from datetime import UTC, datetime, timedelta, timezone

import requests as http_requests
from google.cloud import storage
from langchain_anthropic import ChatAnthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-6"
SPEC_BLOB = "sms-coach/spec.md"
HISTORY_BLOB = "sms-coach/history.jsonl"
MAX_HISTORY_MESSAGES = 50

CT = timezone(timedelta(hours=-6))


def log_structured(trace_id, stage, result="success", metadata=None):
    """Log structured JSON to Cloud Logging."""
    log_data = {
        "trace_id": trace_id,
        "stage": stage,
        "result": result,
        "service": "sms-coach",
    }
    if metadata:
        log_data["metadata"] = metadata
    print(json.dumps(log_data), flush=True)


# --- GCS Helpers ---


def _get_bucket():
    bucket_name = os.getenv("GMAIL_AI_STORAGE_BUCKET", "gmail-ai-logs")
    project_id = os.getenv("GMAIL_AI_PROJECT_ID", "")
    client = storage.Client(project=project_id)
    return client.bucket(bucket_name)


def read_spec():
    """Read the coaching spec from GCS."""
    bucket = _get_bucket()
    blob = bucket.blob(SPEC_BLOB)
    if blob.exists():
        return blob.download_as_text()
    return ""


def write_spec(content):
    """Write the coaching spec back to GCS."""
    bucket = _get_bucket()
    blob = bucket.blob(SPEC_BLOB)
    blob.upload_from_string(content, content_type="text/markdown")


def read_history():
    """Read conversation history from GCS (JSONL format)."""
    bucket = _get_bucket()
    blob = bucket.blob(HISTORY_BLOB)
    if not blob.exists():
        return []
    lines = blob.download_as_text().strip().split("\n")
    messages = []
    for line in lines:
        if line.strip():
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return messages[-MAX_HISTORY_MESSAGES:]


def append_history(messages):
    """Append messages to conversation history, trimming old ones."""
    existing = read_history()
    existing.extend(messages)
    trimmed = existing[-MAX_HISTORY_MESSAGES:]
    content = "\n".join(json.dumps(m) for m in trimmed) + "\n"
    bucket = _get_bucket()
    blob = bucket.blob(HISTORY_BLOB)
    blob.upload_from_string(content, content_type="application/jsonl")


# --- Twilio ---


def send_sms(body):
    """Send an SMS via Twilio REST API."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_FROM_NUMBER")
    to_number = os.getenv("TWILIO_TO_NUMBER")

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    resp = http_requests.post(
        url,
        data={"From": from_number, "To": to_number, "Body": body},
        auth=(account_sid, auth_token),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("sid", "")


# --- Claude ---


def _parse_json_response(content):
    """Parse Claude's JSON response, handling markdown fences."""
    content = content.strip()
    if "```" in content:
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()
    if not content.startswith("{"):
        match = re.search(r"\{", content)
        if match:
            content = content[match.start():]
    result = json.loads(content)
    if not isinstance(result, dict) or "message" not in result:
        raise ValueError("Response missing 'message' field")
    return result


def _format_history(history, last_n=20):
    """Format conversation history for the prompt."""
    recent = history[-last_n:] if history else []
    if not recent:
        return ""
    lines = []
    for msg in recent:
        role = "Coach" if msg["role"] == "coach" else "Client"
        ts = msg.get("timestamp", "")[:16]
        msg_type = f" [{msg['type']}]" if msg.get("type") else ""
        lines.append(f"[{ts}] {role}{msg_type}: {msg['text']}")
    return "\n".join(lines)


def generate_morning_message(spec, history):
    """Generate the daily morning coaching text."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    llm = ChatAnthropic(model=MODEL, api_key=api_key, max_tokens=1000)

    history_block = _format_history(history, last_n=10)

    now_ct = datetime.now(tz=CT)
    day_of_week = now_ct.strftime("%A")
    date_str = now_ct.strftime("%B %d, %Y")

    prompt = f"""You are a muscle growth coach communicating via SMS.
You send one text each morning to keep your client on track.

Today is {day_of_week}, {date_str}.

## Client Spec (your living knowledge about this client)
{spec or "(No spec yet — introduce yourself and ask about their goals)"}

## Recent Conversation
{history_block or "(First interaction — no history yet)"}

## Your Task
Send a single morning text message. Consider:
- What day of the week it is (training day vs rest day per their schedule)
- What happened in recent conversations (did they mention soreness, a PR, skipping a meal?)
- Their current phase/goals from the spec
- Variety: don't repeat the same type of message. Rotate between:
  - Workout preview/motivation for training days
  - Recovery/nutrition tips for rest days
  - Check-in questions ("How did yesterday's session go?")
  - Quick knowledge drops (hypertrophy science, nutrition facts)
  - Encouragement based on their progress

## Rules
- Keep it under 300 characters (fits in 2 SMS segments max)
- Sound like a knowledgeable friend, not a corporate bot
- Be specific to THEIR program and goals, not generic
- Don't start every message with "Hey" or "Good morning"
- If the spec is empty, introduce yourself and ask what they're working on

## Output
Respond with JSON only:
{{
    "message": "the SMS text to send",
    "spec_updates": null or "full updated spec markdown if anything needs changing"
}}"""

    response = llm.invoke(prompt)
    return _parse_json_response(response.content)


def generate_reply(spec, history, inbound_message):
    """Process inbound message and generate reply + optional spec updates."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    llm = ChatAnthropic(model=MODEL, api_key=api_key, max_tokens=2000)

    history_block = _format_history(history, last_n=20)

    now_ct = datetime.now(tz=CT)
    day_of_week = now_ct.strftime("%A")

    prompt = f"""You are a muscle growth coach communicating via SMS.
Your client just texted you. Read their message and respond helpfully.

Today is {day_of_week}.

## Client Spec (your living knowledge about this client)
{spec or "(No spec yet)"}

## Recent Conversation
{history_block or "(No prior conversation)"}

## New Message from Client
{inbound_message}

## Your Task
1. Understand what they're telling you or asking
2. Respond with helpful, specific coaching advice
3. If they shared new information (a PR, a body weight update, a change in schedule,
   dietary info, an injury), update the spec to reflect it

## Rules
- Keep reply under 450 characters (3 SMS segments max) unless they asked
  a detailed question — then up to 900 characters is OK
- Be conversational but knowledgeable
- If they report a workout, acknowledge it specifically
- If they ask a question, give a direct answer then brief explanation
- If they share a concern (injury, plateau, motivation), address it with empathy and a plan
- Update the spec with ANY new information they share — this is your memory

## Output
Respond with JSON only:
{{
    "message": "your SMS reply text",
    "spec_updates": null or "the COMPLETE updated spec markdown (not a diff)"
}}"""

    response = llm.invoke(prompt)
    return _parse_json_response(response.content)


# --- Handlers ---


def handle_morning(trace_id, spec, history):
    """Generate and send the daily morning coaching text."""
    try:
        result = generate_morning_message(spec, history)
    except Exception as e:
        logger.error(f"[{trace_id}] Morning generation failed: {e}", exc_info=True)
        log_structured(trace_id, "generate_morning", "failure", {"error": str(e)})
        sys.exit(1)

    message_text = result["message"]
    log_structured(trace_id, "generate_morning", metadata={"length": len(message_text)})

    try:
        sid = send_sms(message_text)
        log_structured(trace_id, "sms_sent", metadata={"sid": sid, "length": len(message_text)})
    except Exception as e:
        logger.error(f"[{trace_id}] Failed to send SMS: {e}", exc_info=True)
        log_structured(trace_id, "sms_sent", "failure", {"error": str(e)})
        sys.exit(1)

    now = datetime.now(tz=UTC).isoformat()
    append_history([
        {"role": "coach", "text": message_text, "timestamp": now, "type": "morning"},
    ])

    if result.get("spec_updates"):
        write_spec(result["spec_updates"])
        log_structured(trace_id, "spec_updated")


def handle_reply(trace_id, spec, history):
    """Process inbound SMS and send a reply."""
    inbound = os.getenv("SMS_BODY", "")

    if not inbound:
        log_structured(trace_id, "skip", metadata={"reason": "empty_body"})
        return

    now = datetime.now(tz=UTC).isoformat()
    history.append({"role": "user", "text": inbound, "timestamp": now})

    try:
        result = generate_reply(spec, history, inbound)
    except Exception as e:
        logger.error(f"[{trace_id}] Reply generation failed: {e}", exc_info=True)
        log_structured(trace_id, "generate_reply", "failure", {"error": str(e)})
        sys.exit(1)

    reply_text = result["message"]
    log_structured(trace_id, "generate_reply", metadata={"length": len(reply_text)})

    try:
        sid = send_sms(reply_text)
        log_structured(trace_id, "sms_sent", metadata={"sid": sid, "length": len(reply_text)})
    except Exception as e:
        logger.error(f"[{trace_id}] Failed to send SMS: {e}", exc_info=True)
        log_structured(trace_id, "sms_sent", "failure", {"error": str(e)})
        sys.exit(1)

    append_history([
        {"role": "user", "text": inbound, "timestamp": now},
        {"role": "coach", "text": reply_text,
         "timestamp": datetime.now(tz=UTC).isoformat()},
    ])

    if result.get("spec_updates"):
        write_spec(result["spec_updates"])
        log_structured(trace_id, "spec_updated")


def main():
    """Main entry point."""
    trace_id = os.getenv("TRACE_ID", "sms-unknown")
    mode = os.getenv("SMS_MODE", "reply")

    log_structured(trace_id, "job_start", metadata={"mode": mode})

    try:
        spec = read_spec()
        history = read_history()
    except Exception as e:
        logger.error(f"[{trace_id}] Failed to read state: {e}", exc_info=True)
        log_structured(trace_id, "read_state", "failure", {"error": str(e)})
        sys.exit(1)

    if mode == "morning":
        handle_morning(trace_id, spec, history)
    elif mode == "reply":
        handle_reply(trace_id, spec, history)
    else:
        logger.error(f"[{trace_id}] Unknown SMS_MODE: {mode}")
        sys.exit(1)

    log_structured(trace_id, "job_complete")
    logger.info(f"[{trace_id}] Done ({mode})")


if __name__ == "__main__":
    main()
