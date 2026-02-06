"""Slack Processor - Cloud Run Job (batch mode).

Reads all pending Slack events from Cloud Storage, classifies messages in a
single Opus call, updates Trello cards, and cleans up processed events.

Models:
- Opus 4.6: batch topic classification + matching (one call for all messages)
- Haiku: card description summaries and reaction interpretation (per card)
"""

import json
import logging
import os
import sys

import requests
from google.cloud import storage
from langchain_anthropic import ChatAnthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Model Config ---

MODEL_CLASSIFY = "claude-opus-4-6"  # batch topic matching — one call for N messages
MODEL_SUMMARIZE = "claude-3-5-haiku-20241022"  # description updates — cheap and fast


# --- Trello Client ---


class TrelloClient:
    """Simple Trello API client."""

    BASE_URL = "https://api.trello.com/1"

    def __init__(self):
        self.api_key = os.getenv("TRELLO_API_KEY")
        self.token = os.getenv("TRELLO_TOKEN")
        self.board_id = os.getenv("TRELLO_BOARD_ID")
        if not all([self.api_key, self.token, self.board_id]):
            raise ValueError("TRELLO_API_KEY, TRELLO_TOKEN, and TRELLO_BOARD_ID must be set")

    def _params(self, **extra):
        return {"key": self.api_key, "token": self.token, **extra}

    def get_lists(self):
        """Get all lists on the board."""
        r = requests.get(f"{self.BASE_URL}/boards/{self.board_id}/lists", params=self._params())
        r.raise_for_status()
        return r.json()

    def get_list_id(self, list_name):
        """Get list ID by name."""
        lists = self.get_lists()
        for lst in lists:
            if lst["name"].lower() == list_name.lower():
                return lst["id"]
        raise ValueError(f"List '{list_name}' not found on board")

    def get_cards(self):
        """Get all cards on the board."""
        r = requests.get(
            f"{self.BASE_URL}/boards/{self.board_id}/cards",
            params=self._params(),
        )
        r.raise_for_status()
        return r.json()

    def create_card(self, list_id, name, desc=""):
        """Create a new card."""
        params = self._params(idList=list_id, name=name, desc=desc, pos="top")
        r = requests.post(f"{self.BASE_URL}/cards", params=params)
        r.raise_for_status()
        return r.json()

    def update_card_desc(self, card_id, desc):
        """Update card description."""
        r = requests.put(f"{self.BASE_URL}/cards/{card_id}", params=self._params(desc=desc))
        r.raise_for_status()
        return r.json()

    def move_card(self, card_id, list_id):
        """Move a card to a different list."""
        r = requests.put(f"{self.BASE_URL}/cards/{card_id}", params=self._params(idList=list_id))
        r.raise_for_status()
        return r.json()

    def add_comment(self, card_id, text):
        """Add a comment to a card."""
        r = requests.post(
            f"{self.BASE_URL}/cards/{card_id}/actions/comments",
            params=self._params(text=text),
        )
        r.raise_for_status()
        return r.json()

    def get_checklists(self, card_id):
        """Get checklists on a card."""
        r = requests.get(
            f"{self.BASE_URL}/cards/{card_id}/checklists",
            params=self._params(),
        )
        r.raise_for_status()
        return r.json()

    def create_checklist(self, card_id, name="Action Items"):
        """Create a checklist on a card."""
        r = requests.post(
            f"{self.BASE_URL}/cards/{card_id}/checklists",
            params=self._params(name=name),
        )
        r.raise_for_status()
        return r.json()

    def add_checklist_item(self, checklist_id, name):
        """Add an item to a checklist."""
        r = requests.post(
            f"{self.BASE_URL}/checklists/{checklist_id}/checkItems",
            params=self._params(name=name),
        )
        r.raise_for_status()
        return r.json()


# --- Slack Client ---


class SlackClient:
    """Simple Slack API client for lookups."""

    BASE_URL = "https://slack.com/api"

    def __init__(self):
        self.token = os.getenv("SLACK_BOT_TOKEN")
        if not self.token:
            raise ValueError("SLACK_BOT_TOKEN must be set")
        self._authed_user_id = None
        self._workspace_url = None
        self._user_cache = {}
        self._channel_cache = {}

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def _ensure_auth(self):
        """Call auth.test once and cache user_id + workspace URL."""
        if self._authed_user_id and self._workspace_url:
            return
        r = requests.get(f"{self.BASE_URL}/auth.test", headers=self._headers())
        r.raise_for_status()
        data = r.json()
        if data.get("ok"):
            self._authed_user_id = data["user_id"]
            self._workspace_url = data.get("url", "").rstrip("/")
        else:
            raise ValueError(f"auth.test failed: {data}")

    def get_authed_user_id(self):
        """Get the authenticated user's Slack ID (cached)."""
        self._ensure_auth()
        return self._authed_user_id

    def get_workspace_url(self):
        """Get the Slack workspace URL (cached)."""
        self._ensure_auth()
        return self._workspace_url

    def message_link(self, channel_id, message_ts):
        """Build a deep link to a specific Slack message."""
        base = self.get_workspace_url()
        ts_clean = message_ts.replace(".", "")
        return f"{base}/archives/{channel_id}/p{ts_clean}"

    def channel_link(self, channel_id):
        """Build a deep link to a Slack channel."""
        base = self.get_workspace_url()
        return f"{base}/archives/{channel_id}"

    def get_user_name(self, user_id):
        """Get display name for a user (cached)."""
        if user_id in self._user_cache:
            return self._user_cache[user_id]
        r = requests.get(
            f"{self.BASE_URL}/users.info",
            headers=self._headers(),
            params={"user": user_id},
        )
        r.raise_for_status()
        data = r.json()
        name = user_id
        if data.get("ok"):
            user = data["user"]
            name = user.get("real_name") or user.get("name", user_id)
        self._user_cache[user_id] = name
        return name

    def get_channel_name(self, channel_id):
        """Get channel name (cached)."""
        if channel_id in self._channel_cache:
            return self._channel_cache[channel_id]
        r = requests.get(
            f"{self.BASE_URL}/conversations.info",
            headers=self._headers(),
            params={"channel": channel_id},
        )
        r.raise_for_status()
        data = r.json()
        name = channel_id
        if data.get("ok"):
            name = data["channel"].get("name", channel_id)
        self._channel_cache[channel_id] = name
        return name

    def get_message(self, channel_id, message_ts):
        """Fetch a single message by channel and timestamp."""
        r = requests.get(
            f"{self.BASE_URL}/conversations.history",
            headers=self._headers(),
            params={"channel": channel_id, "latest": message_ts, "inclusive": "true", "limit": 1},
        )
        r.raise_for_status()
        data = r.json()
        if data.get("ok") and data.get("messages"):
            return data["messages"][0]
        return None


# --- Cloud Storage ---


def read_pending_events():
    """Read all pending Slack events from Cloud Storage."""
    bucket_name = os.getenv("GMAIL_AI_STORAGE_BUCKET", "gmail-ai-logs")
    project_id = os.getenv("GMAIL_AI_PROJECT_ID", "")

    client = storage.Client(project=project_id)
    bucket = client.bucket(bucket_name)

    events = []
    for blob in bucket.list_blobs(prefix="slack-pending/"):
        try:
            data = json.loads(blob.download_as_text())
            data["_blob_name"] = blob.name
            events.append(data)
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"Failed to read {blob.name}: {e}")

    return events


def delete_pending_events(events):
    """Delete processed events from Cloud Storage."""
    bucket_name = os.getenv("GMAIL_AI_STORAGE_BUCKET", "gmail-ai-logs")
    project_id = os.getenv("GMAIL_AI_PROJECT_ID", "")

    client = storage.Client(project=project_id)
    bucket = client.bucket(bucket_name)

    for evt in events:
        blob_name = evt.get("_blob_name")
        if blob_name:
            try:
                bucket.blob(blob_name).delete()
            except Exception as e:
                logger.warning(f"Failed to delete {blob_name}: {e}")


# --- Claude: Batch Classification (Opus 4.6) ---

PRIORITY_TO_LIST = {
    "needs_response": "Needs Response",
    "action_required": "Action Required",
    "worth_reading": "Worth Reading",
}

PRIORITY_ORDER = ["Needs Response", "Action Required", "Worth Reading", "Noted"]


def batch_classify_messages(messages_with_context, existing_topics):
    """Classify all messages in a single Opus call.

    Args:
        messages_with_context: list of {idx, text, sender, channel}
        existing_topics: list of {id, name, list_name}

    Returns:
        list of classification dicts, one per message (in same order)
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    llm = ChatAnthropic(model=MODEL_CLASSIFY, api_key=api_key, max_tokens=4000)

    topics_list = (
        "\n".join(
            f'- [id:{t["id"]}] {t["name"]} (in: {t["list_name"]})'
            for t in existing_topics
        )
        if existing_topics
        else "(no existing topics yet)"
    )

    messages_block = "\n".join(
        f'{m["idx"]}. [#{m["channel"]}] {m["sender"]}: {m["text"][:1500]}'
        for m in messages_with_context
    )

    prompt = f"""You manage a Trello board that organizes Slack messages by topic.
Process this batch of {len(messages_with_context)} new messages.

Existing topics on the board:
{topics_list}

Messages to classify:
{messages_block}

For EACH message, determine:
1. Does it belong to an existing topic (give the card id) or is it new?
2. Priority: needs_response | action_required | worth_reading
3. Any action items directed at me
4. A 1-2 sentence summary
5. A short topic name (3-6 words) if new

IMPORTANT: Multiple messages may belong to the same topic. If two messages in this
batch are about the same thing, give them the same existing_topic_id or the same
new topic_name so they get grouped together.

Respond with a JSON array, one entry per message in the same order:
[
    {{
        "msg_idx": 1,
        "existing_topic_id": "card id or null",
        "topic_name": "short topic name",
        "priority": "needs_response|action_required|worth_reading",
        "action_items": [],
        "summary": "brief summary"
    }},
    ...
]"""

    response = llm.invoke(prompt)
    content = response.content.strip()

    try:
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        results = json.loads(content)
        if isinstance(results, list):
            return results
    except (json.JSONDecodeError, IndexError):
        logger.warning(f"Failed to parse batch classification: {content[:300]}")

    # Fallback: return generic classifications
    return [
        {
            "msg_idx": m["idx"],
            "existing_topic_id": None,
            "topic_name": f'#{m["channel"]} discussion',
            "priority": "worth_reading",
            "action_items": [],
            "summary": m["text"][:200],
        }
        for m in messages_with_context
    ]


# --- Claude: Description Updates (Haiku) ---


def update_description_summary(current_desc, channel_name, new_message, sender_name):
    """Use Haiku to update the Summary section of a card description."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    llm = ChatAnthropic(model=MODEL_SUMMARIZE, api_key=api_key, max_tokens=600)

    prompt = f"""You maintain a Trello card description that serves as a living brief for a Slack topic.
The description has two sections: **Summary** and **Reactions**.

Current card description:
---
{current_desc or "(empty — this is a new card)"}
---

A new message just arrived in #{channel_name}:
- From: {sender_name}
- Text: {new_message[:2000]}

Rewrite the **Summary** section to incorporate this new message. Keep it 1-3 sentences — dense,
direct, no fluff. Preserve the **Reactions** section exactly as-is (or include an empty placeholder
if there isn't one yet).

Output the full card description (both sections):

**Summary**
(your updated summary here)

**Reactions**
(preserve existing or write "No reactions yet.")"""

    response = llm.invoke(prompt)
    return response.content.strip()


def update_description_reactions(current_desc, reactor_name, reaction_emoji, original_text):
    """Use Haiku to update the Reactions section of a card description."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    llm = ChatAnthropic(model=MODEL_SUMMARIZE, api_key=api_key, max_tokens=600)

    prompt = f"""You maintain a Trello card description that serves as a living brief for a Slack topic.
The description has two sections: **Summary** and **Reactions**.

Current card description:
---
{current_desc or "(empty)"}
---

A new reaction just happened:
- {reactor_name} reacted with :{reaction_emoji}:
- On the message: "{original_text[:500]}"

Update the **Reactions** section to incorporate this new reaction. The Reactions section should
read as a brief, natural-language insight about how people are responding to what I'm saying —
what's landing well and what isn't. Don't just list emojis; interpret the sentiment.
Keep the **Summary** section exactly as-is.

Output the full card description (both sections):

**Summary**
(preserve existing summary exactly)

**Reactions**
(your updated reactions insight here)"""

    response = llm.invoke(prompt)
    return response.content.strip()


# --- Structured Logging ---


def log_structured(trace_id, stage, result="success", metadata=None):
    """Log structured JSON to Cloud Logging."""
    log_data = {
        "trace_id": trace_id,
        "stage": stage,
        "result": result,
        "service": "slack-processor",
    }
    if metadata:
        log_data["metadata"] = metadata
    print(json.dumps(log_data), flush=True)


# --- Process Messages (batch) ---


def process_messages(message_events, slack, trello, cards, list_map, batch_trace_id):
    """Batch-classify messages with one Opus call, then update Trello."""
    # Build context for each message
    messages_with_context = []
    for i, evt in enumerate(message_events):
        event = evt["event"]
        user_id = event.get("user", "")
        channel_id = event.get("channel", "")
        text = event.get("text", "")
        message_ts = event.get("ts", "")

        try:
            sender = slack.get_user_name(user_id)
            channel = slack.get_channel_name(channel_id)
        except Exception:
            sender = user_id
            channel = channel_id

        messages_with_context.append({
            "idx": i + 1,
            "text": text,
            "sender": sender,
            "channel": channel,
            "channel_id": channel_id,
            "user_id": user_id,
            "ts": message_ts,
            "trace_id": evt.get("trace_id", batch_trace_id),
        })

    # Get existing topics
    existing_topics = [
        {"id": c["id"], "name": c["name"], "list_name": list_map.get(c["idList"], "Unknown")}
        for c in cards
        if list_map.get(c["idList"]) != "Noted"
    ]

    log_structured(batch_trace_id, "batch_classify_start", metadata={
        "message_count": len(messages_with_context),
        "existing_topics": len(existing_topics),
    })

    # ONE Opus call for the entire batch
    try:
        classifications = batch_classify_messages(messages_with_context, existing_topics)
        log_structured(batch_trace_id, "batch_classify_done", "success", {
            "results": len(classifications),
        })
    except Exception as e:
        logger.error(f"Batch classification failed: {e}")
        log_structured(batch_trace_id, "batch_classify_done", "failure", {"error": str(e)})
        return

    # Process each classified message
    # Track new cards created in this batch so later messages can reference them
    new_cards_by_topic = {}  # topic_name -> card_id

    for classification in classifications:
        msg_idx = classification.get("msg_idx", 0) - 1  # back to 0-indexed
        if msg_idx < 0 or msg_idx >= len(messages_with_context):
            continue

        msg = messages_with_context[msg_idx]
        trace_id = msg["trace_id"]

        try:
            _apply_classification(
                classification, msg, trace_id,
                trello, slack, cards, list_map, new_cards_by_topic,
            )
        except Exception as e:
            logger.error(f"Failed to process message {msg_idx}: {e}")
            log_structured(trace_id, "trello_action", "failure", {"error": str(e)})


def _apply_classification(classification, msg, trace_id, trello, slack, cards, list_map, new_cards_by_topic):
    """Apply a single classification result to Trello."""
    priority = classification.get("priority", "worth_reading")
    target_list_name = PRIORITY_TO_LIST.get(priority, "Worth Reading")
    target_list_id = trello.get_list_id(target_list_name)

    existing_topic_id = classification.get("existing_topic_id")
    topic_name = classification.get("topic_name", f'#{msg["channel"]} discussion')
    action_items = classification.get("action_items", [])

    channel_link = slack.channel_link(msg["channel_id"])
    msg_link = slack.message_link(msg["channel_id"], msg["ts"]) if msg.get("ts") else ""

    comment = f'**{msg["sender"]}** in [#{msg["channel"]}]({channel_link}):\n{msg["text"]}'
    if msg_link:
        comment += f'\n\n[View message]({msg_link})'

    # Check if another message in this batch already created this topic
    if not existing_topic_id and topic_name in new_cards_by_topic:
        existing_topic_id = new_cards_by_topic[topic_name]

    if existing_topic_id:
        # --- Update existing card ---
        card_id = existing_topic_id
        trello.add_comment(card_id, comment)

        # Escalate if needed
        current_card = next((c for c in cards if c["id"] == card_id), None)
        if current_card:
            current_list = list_map.get(current_card["idList"], "")
            current_idx = PRIORITY_ORDER.index(current_list) if current_list in PRIORITY_ORDER else 99
            target_idx = PRIORITY_ORDER.index(target_list_name) if target_list_name in PRIORITY_ORDER else 99
            if target_idx < current_idx:
                trello.move_card(card_id, target_list_id)

        # Update description via Haiku
        current_desc = current_card.get("desc", "") if current_card else ""
        new_desc = update_description_summary(current_desc, msg["channel"], msg["text"], msg["sender"])
        trello.update_card_desc(card_id, new_desc)

        log_structured(trace_id, "trello_update", "success", {
            "card_id": card_id, "topic": topic_name, "priority": priority,
        })
    else:
        # --- Create new card ---
        initial_desc = update_description_summary("", msg["channel"], msg["text"], msg["sender"])
        card = trello.create_card(target_list_id, topic_name, desc=initial_desc)
        card_id = card["id"]
        trello.add_comment(card_id, comment)

        # Track for other messages in this batch
        new_cards_by_topic[topic_name] = card_id
        # Add to cards list so subsequent messages can find it
        cards.append({"id": card_id, "name": topic_name, "idList": target_list_id, "desc": initial_desc})

        log_structured(trace_id, "trello_create", "success", {
            "card_id": card_id, "topic": topic_name, "priority": priority,
        })

    # Add action items
    if action_items:
        checklists = trello.get_checklists(card_id)
        checklist_id = checklists[0]["id"] if checklists else trello.create_checklist(card_id)["id"]
        for item in action_items:
            trello.add_checklist_item(checklist_id, item)


# --- Process Reactions ---


def process_reactions(reaction_events, slack, trello, cards, list_map, batch_trace_id):
    """Process reaction events — update Reactions section on matching cards."""
    my_user_id = slack.get_authed_user_id()

    for evt in reaction_events:
        event = evt["event"]
        trace_id = evt.get("trace_id", batch_trace_id)
        user_id = event.get("user", "")
        reaction = event.get("reaction", "")
        item = event.get("item", {})
        channel_id = item.get("channel", "")
        message_ts = item.get("ts", "")

        # Fetch the reacted-to message
        try:
            msg = slack.get_message(channel_id, message_ts)
            if not msg:
                log_structured(trace_id, "skip", metadata={"reason": "message_not_found"})
                continue
            if msg.get("user") != my_user_id:
                log_structured(trace_id, "skip", metadata={"reason": "not_my_message"})
                continue
            original_text = msg.get("text", "")[:500]
        except Exception as e:
            logger.warning(f"Failed to fetch reacted message: {e}")
            continue

        # Resolve names
        try:
            reactor_name = slack.get_user_name(user_id)
            channel_name = slack.get_channel_name(channel_id)
        except Exception:
            reactor_name = user_id
            channel_name = channel_id

        # Find matching card
        matching_cards = [
            c for c in cards
            if f"#{channel_name}" in (c.get("desc") or "")
            and list_map.get(c["idList"]) != "Noted"
        ]

        if not matching_cards:
            log_structured(trace_id, "skip", metadata={"reason": "no_matching_card"})
            continue

        card = matching_cards[0]
        current_desc = card.get("desc", "")

        try:
            new_desc = update_description_reactions(current_desc, reactor_name, reaction, original_text)
            trello.update_card_desc(card["id"], new_desc)
            log_structured(trace_id, "trello_update", "success", {
                "card_id": card["id"], "reaction": reaction,
            })
        except Exception as e:
            logger.error(f"Failed to update reaction on card: {e}")
            log_structured(trace_id, "trello_action", "failure", {"error": str(e)})


# --- Main ---


def main():
    """Main entry point — batch process all pending Slack events."""
    batch_trace_id = os.getenv("TRACE_ID", "batch-unknown")

    # Read all pending events from Cloud Storage
    logger.info("Reading pending Slack events...")
    pending = read_pending_events()

    if not pending:
        logger.info("No pending events, exiting.")
        log_structured(batch_trace_id, "batch_empty")
        return

    logger.info(f"Found {len(pending)} pending events")
    log_structured(batch_trace_id, "batch_start", metadata={"total_events": len(pending)})

    # Separate messages from reactions
    messages = [e for e in pending if e["event"].get("type") != "reaction_added"]
    reactions = [e for e in pending if e["event"].get("type") == "reaction_added"]

    logger.info(f"Messages: {len(messages)}, Reactions: {len(reactions)}")

    # Initialize clients
    slack = SlackClient()
    trello = TrelloClient()

    # Fetch board state once
    try:
        lists = trello.get_lists()
        list_map = {lst["id"]: lst["name"] for lst in lists}
        cards = trello.get_cards()
    except Exception as e:
        logger.error(f"Failed to fetch Trello board: {e}")
        log_structured(batch_trace_id, "trello_fetch", "failure", {"error": str(e)})
        sys.exit(1)

    # Process messages (one Opus call for the batch)
    if messages:
        process_messages(messages, slack, trello, cards, list_map, batch_trace_id)

    # Process reactions (Haiku only, per reaction)
    if reactions:
        process_reactions(reactions, slack, trello, cards, list_map, batch_trace_id)

    # Clean up processed events
    delete_pending_events(pending)
    logger.info(f"Batch complete. Processed {len(pending)} events.")
    log_structured(batch_trace_id, "batch_complete", metadata={
        "messages": len(messages), "reactions": len(reactions),
    })


if __name__ == "__main__":
    main()
