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
import re
import sys
from datetime import datetime, timedelta, timezone

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

    def get_board_desc(self):
        """Get the board description (used as user context/memory)."""
        r = requests.get(
            f"{self.BASE_URL}/boards/{self.board_id}",
            params=self._params(fields="desc"),
        )
        r.raise_for_status()
        return r.json().get("desc", "")

    def update_board_desc(self, desc):
        """Update the board description."""
        r = requests.put(
            f"{self.BASE_URL}/boards/{self.board_id}",
            params=self._params(desc=desc),
        )
        r.raise_for_status()
        return r.json()

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
        """Get all open cards on the board."""
        r = requests.get(
            f"{self.BASE_URL}/boards/{self.board_id}/cards",
            params=self._params(),
        )
        r.raise_for_status()
        return r.json()

    def get_archived_cards(self, since_hours=24):
        """Get recently archived cards (closed within since_hours)."""
        r = requests.get(
            f"{self.BASE_URL}/boards/{self.board_id}/cards/closed",
            params=self._params(fields="id,name,desc,idList,dateLastActivity,closed"),
        )
        r.raise_for_status()
        all_closed = r.json()

        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=since_hours)  # noqa: UP017
        recent = []
        for card in all_closed:
            last_activity = card.get("dateLastActivity", "")
            try:
                dt = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
                if dt >= cutoff:
                    card["_archived"] = True
                    recent.append(card)
            except (ValueError, TypeError):
                pass
        return recent

    def unarchive_card(self, card_id):
        """Reopen an archived card."""
        r = requests.put(
            f"{self.BASE_URL}/cards/{card_id}",
            params=self._params(closed="false"),
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

    def update_checklist_item(self, card_id, check_item_id, completed=None, name=None):
        """Update a checklist item (name and/or completion state)."""
        params = self._params()
        if completed is not None:
            params["state"] = "complete" if completed else "incomplete"
        if name is not None:
            params["name"] = name
        r = requests.put(
            f"{self.BASE_URL}/cards/{card_id}/checkItem/{check_item_id}",
            params=params,
        )
        r.raise_for_status()
        return r.json()

    def delete_checklist_item(self, checklist_id, check_item_id):
        """Delete a checklist item."""
        r = requests.delete(
            f"{self.BASE_URL}/checklists/{checklist_id}/checkItems/{check_item_id}",
            params=self._params(),
        )
        r.raise_for_status()


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
        """Get display name for a user (cached).

        Prefers profile.display_name > profile.real_name > real_name > name.
        """
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
            profile = user.get("profile", {})
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or user.get("real_name")
                or user.get("name", user_id)
            )
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

    def resolve_mentions(self, text):
        """Replace <@U123> user mentions and <#C123> channel mentions with readable names."""
        def replace_user(match):
            user_id = match.group(1)
            try:
                return self.get_user_name(user_id)
            except Exception:
                return user_id

        def replace_channel(match):
            channel_id = match.group(1)
            # <#C123|name> format already has the name
            if match.group(2):
                return f"#{match.group(2)}"
            try:
                return f"#{self.get_channel_name(channel_id)}"
            except Exception:
                return f"#{channel_id}"

        text = re.sub(r"<@(\w+)>", replace_user, text)
        text = re.sub(r"<#(\w+)(?:\|([^>]+))?>", replace_channel, text)
        return text

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

    def get_channel_history(self, channel_id, limit=15):
        """Fetch recent messages from a channel for classification context.

        Returns list of {sender, text} dicts, oldest first.
        """
        r = requests.get(
            f"{self.BASE_URL}/conversations.history",
            headers=self._headers(),
            params={"channel": channel_id, "limit": limit},
        )
        r.raise_for_status()
        data = r.json()
        if data.get("ok") and data.get("messages"):
            msgs = data["messages"]  # newest first from API
            result = []
            for m in reversed(msgs):
                user_id = m.get("user", "")
                try:
                    name = self.get_user_name(user_id)
                except Exception:
                    name = user_id
                text = m.get("text", "")
                try:
                    text = self.resolve_mentions(text)
                except Exception:
                    pass
                result.append({"sender": name, "text": text})
            return result
        return []

    def get_preceding_messages(self, channel_id, before_ts, count=3):
        """Fetch preceding messages in a channel for context.

        Returns list of {user, text} dicts, oldest first.
        """
        r = requests.get(
            f"{self.BASE_URL}/conversations.history",
            headers=self._headers(),
            params={"channel": channel_id, "latest": before_ts, "limit": count},
        )
        r.raise_for_status()
        data = r.json()
        if data.get("ok") and data.get("messages"):
            msgs = data["messages"]  # newest first from API
            result = []
            for m in reversed(msgs):
                user_id = m.get("user", "")
                try:
                    name = self.get_user_name(user_id)
                except Exception:
                    name = user_id
                text = m.get("text", "")
                result.append({"sender": name, "text": text[:200]})
            return result
        return []


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


def batch_classify_messages(messages_with_context, existing_topics, user_context="", channel_context=None):
    """Classify all messages in a single Opus call.

    Args:
        messages_with_context: list of {idx, text, sender, channel}
        existing_topics: list of {id, name, list_name}
        user_context: board description with user's context/memory
        channel_context: dict of {channel_name: [{sender, text}, ...]} for conversation history

    Returns:
        list of classification dicts, one per message (in same order)
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    llm = ChatAnthropic(model=MODEL_CLASSIFY, api_key=api_key, max_tokens=4000)

    context_block = (
        f"About me (use this to prioritize and group):\n{user_context}\n\n"
        if user_context.strip()
        else ""
    )

    topics_list = (
        "\n".join(
            f'- [id:{t["id"]}] {t["name"]} (in: {t["list_name"]})'
            for t in existing_topics
        )
        if existing_topics
        else "(no existing topics yet)"
    )

    # Build channel context block
    channel_context = channel_context or {}
    channel_context_block = ""
    if channel_context:
        sections = []
        for ch_name, msgs in channel_context.items():
            lines = [f"  {m['sender']}: {m['text']}" for m in msgs]
            sections.append(f"#{ch_name} (recent history):\n" + "\n".join(lines))
        channel_context_block = "\n\n".join(sections)

    # Channel de-emphasized: trailing metadata, not leading identifier
    msg_lines = []
    for m in messages_with_context:
        line = f'{m["idx"]}. {m["sender"]}: {m["text"][:1500]}  (#{m["channel"]})'
        if m.get("preceding_context"):
            line += f'\n   Preceding conversation:\n{m["preceding_context"]}'
        msg_lines.append(line)
    messages_block = "\n".join(msg_lines)

    prompt = f"""You manage a Trello board that organizes my Slack messages by TOPIC for me.
{context_block}
## Recent channel conversations (for context — do NOT classify these, only the new messages below)

{channel_context_block if channel_context_block else "(no channel history available)"}

## Step 1: Identify distinct topics

Read ALL messages below. Identify the distinct SUBJECT MATTERS being discussed.
A topic is a specific project, decision, question, event, or ongoing thread — NOT a channel.

## Step 2: Assign each message to a topic

Match each message to an existing board topic OR one of the topics you just identified.

Existing topics on the board:
{topics_list}

Messages to classify:
{messages_block}

## Rules

CRITICAL: Topics must describe SUBJECT MATTER, never channels.
- WRONG: "backend discussion", "#design-reviews chat", "general updates"
- RIGHT: "Auth module refactor", "Q2 hiring plan", "Production outage Jan 15"

Messages from DIFFERENT channels often belong to the SAME topic (e.g. someone discusses
a deploy in #backend and #general — that's one topic). Messages in the SAME channel
are often about DIFFERENT topics.

Prefer matching to existing topics over creating new ones — group aggressively by
subject, not by channel or time.

## Priority

For each message, assign a priority:
- **needs_response**: someone is waiting on ME specifically to reply
- **action_required**: I need to do something (review, approve, decide) but nobody's blocked
- **worth_reading**: relevant info, no action needed from me — includes life updates,
  what people are up to, social/personal messages that I'd want to see
- **noise**: zero informational value — "thanks!", "ok", emoji-only, "got it", "+1", "lol"
  HOWEVER: if preceding context shows someone is agreeing/consenting to something
  actionable, that is NOT noise — classify based on what they're agreeing to.

## Examples

Imagine these messages arrive in a batch:

1. Alice: Can you review the auth PR when you get a chance?  (#backend)
2. Bob: Deployed the new caching layer to staging  (#backend)
3. Carol: Has anyone seen the Q2 planning doc?  (#general)
4. Dave: yeah I think that approach works 👍  (#backend)
   Preceding conversation:
     Alice: Should we add rate limiting to the API?
     Eve: I was thinking token bucket
5. Frank: Just got back from paternity leave! Baby is doing great 🎉  (#random)
6. Grace: ok  (#general)
7. Alice: The rate limiting PR is up btw  (#backend)

Correct grouping:
- Messages 1 → "Auth PR review" (needs_response)
- Messages 2 → "Staging cache deploy" (worth_reading)
- Messages 3 → "Q2 planning doc" (action_required)
- Messages 4, 7 → "API rate limiting" (worth_reading — Dave agrees with approach, Alice's PR is related)
- Message 5 → "Frank back from leave" (worth_reading — personal update I'd want to see)
- Message 6 → noise (bare "ok" with no meaningful context)

Note: messages 1, 2, 4, 7 are ALL from #backend but are THREE different topics.
Message 4 is NOT noise because context shows Dave is agreeing to a technical approach.

## Output

Respond with a JSON array, one entry per message in the same order:
[
    {{
        "msg_idx": 1,
        "existing_topic_id": "card id or null",
        "topic_name": "short descriptive topic name (3-6 words)",
        "priority": "needs_response|action_required|worth_reading|noise",
        "action_items": ["action items directed at me, if any"],
        "summary": "1-2 sentence summary of this message's contribution to the topic"
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
            "topic_name": f'{m["sender"]}: {m["text"][:40]}',
            "priority": "worth_reading",
            "action_items": [],
            "summary": m["text"][:200],
        }
        for m in messages_with_context
    ]


# --- Claude: Description Updates (Haiku) ---


def update_description_summary(current_desc, channel_name, new_message, sender_name, current_action_items=None):
    """Use Haiku to update the Summary section and revise action items.

    Args:
        current_desc: current card description
        channel_name: Slack channel name
        new_message: the new message text
        sender_name: who sent the message
        current_action_items: list of {"text": str, "completed": bool} or None

    Returns:
        {"description": str, "action_items": [{"text": str, "completed": bool}]}
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    llm = ChatAnthropic(model=MODEL_SUMMARIZE, api_key=api_key, max_tokens=1000)

    action_items_block = "(none yet)"
    if current_action_items:
        lines = []
        for item in current_action_items:
            status = "done" if item.get("completed") else "open"
            lines.append(f"- [{status}] {item['text']}")
        action_items_block = "\n".join(lines)

    prompt = f"""You maintain a Trello card that serves as a living brief for a Slack topic.
The card has a description (three sections: **Summary**, **Reactions**, **Threads**)
and a checklist of action items.

Current card description:
---
{current_desc or "(empty — this is a new card)"}
---

Current action items:
{action_items_block}

A new message just arrived in #{channel_name}:
- From: {sender_name}
- Text: {new_message[:2000]}

Do two things:
1. Rewrite the **Summary** to incorporate this message. Keep it 1-3 sentences — dense, direct.
   Preserve **Reactions** and **Threads** EXACTLY as-is (or add empty placeholders).
2. Revise the action items: add new ones if the message creates tasks, mark completed ones as done
   if the message resolves them, keep unchanged ones as-is. Action items should be concrete tasks
   directed at me. Remove items only if they're clearly obsolete.

Respond with JSON (no markdown fences):
{{
    "description": "**Summary**\\n...\\n\\n**Reactions**\\n...\\n\\n**Threads**\\n...",
    "action_items": [
        {{"text": "item text", "completed": false}},
        ...
    ]
}}"""

    response = llm.invoke(prompt)
    content = response.content.strip()

    try:
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        result = json.loads(content)
        if isinstance(result, dict) and "description" in result:
            return result
    except (json.JSONDecodeError, IndexError):
        logger.warning(f"Failed to parse Haiku JSON, falling back: {content[:200]}")

    # Fallback: treat entire response as description, keep action items unchanged
    return {
        "description": content,
        "action_items": current_action_items or [],
    }


def update_description_reactions(current_desc, reactor_name, reaction_emoji, original_text):
    """Use Haiku to update the Reactions section of a card description."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    llm = ChatAnthropic(model=MODEL_SUMMARIZE, api_key=api_key, max_tokens=600)

    prompt = f"""You maintain a Trello card description that serves as a living brief for a Slack topic.
The description has three sections: **Summary**, **Reactions**, and **Threads**.

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
Keep the **Summary** and **Threads** sections EXACTLY as-is.

Output the full card description (all three sections):

**Summary**
(preserve existing summary exactly)

**Reactions**
(your updated reactions insight here)

**Threads**
(preserve existing EXACTLY — do NOT modify this section)"""

    response = llm.invoke(prompt)
    return response.content.strip()


# --- Checklist Sync ---


def get_current_action_items(trello, card_id):
    """Read current checklist items from a Trello card.

    Returns list of {"id": str, "text": str, "completed": bool}.
    """
    checklists = trello.get_checklists(card_id)
    if not checklists:
        return []
    items = []
    for item in checklists[0].get("checkItems", []):
        items.append({
            "id": item["id"],
            "text": item["name"],
            "completed": item["state"] == "complete",
        })
    return items


def sync_action_items(trello, card_id, current_items, revised_items):
    """Sync revised action items back to the Trello checklist.

    Matches by text similarity. Adds new items, updates completion state,
    removes items not in the revised list.
    """
    checklists = trello.get_checklists(card_id)
    if not checklists and revised_items:
        checklist = trello.create_checklist(card_id)
        checklist_id = checklist["id"]
    elif checklists:
        checklist_id = checklists[0]["id"]
    else:
        return

    # Index current items by text for matching
    current_by_text = {item["text"].lower().strip(): item for item in current_items}

    revised_texts = set()
    for revised in revised_items:
        text = revised.get("text", "").strip()
        if not text:
            continue
        revised_texts.add(text.lower())
        match = current_by_text.get(text.lower())

        if match:
            # Update completion state if changed
            if match["completed"] != revised.get("completed", False):
                trello.update_checklist_item(card_id, match["id"], completed=revised["completed"])
        else:
            # New item
            trello.add_checklist_item(checklist_id, text)

    # Remove items no longer in the revised list
    for item in current_items:
        if item["text"].lower().strip() not in revised_texts:
            try:
                trello.delete_checklist_item(checklist_id, item["id"])
            except Exception:
                pass  # Non-critical if delete fails


# --- Thread Tracking ---


def find_card_by_thread_ts(cards, thread_ts):
    """Find a card whose description contains a thread_ts marker."""
    marker = f"`ts:{thread_ts}`"
    for card in cards:
        if marker in (card.get("desc") or ""):
            return card
    return None


def append_thread_entry(desc, sender, channel, text_preview, msg_link, message_ts):
    """Append a thread entry to the **Threads** section of a card description.

    Each entry looks like:
    - [Sender in #channel: "preview..."](msg_link) `ts:123.456`
    """
    preview = text_preview[:60].replace("\n", " ")
    entry = f'- [{sender} in #{channel}: "{preview}"]({msg_link}) `ts:{message_ts}`'

    if "**Threads**" in desc:
        # Append to existing section
        return desc.rstrip() + "\n" + entry
    else:
        # Add new section
        return desc.rstrip() + f"\n\n**Threads**\n{entry}"


# --- Board Context Memory (Haiku) ---


def update_board_context(current_context, batch_summary):
    """Use Haiku to update the board description with new observations.

    Args:
        current_context: current board description text
        batch_summary: short summary of what was just processed

    Returns:
        updated board description string
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return current_context

    llm = ChatAnthropic(model=MODEL_SUMMARIZE, api_key=api_key, max_tokens=800)

    prompt = f"""You maintain a "memory" document about a Slack user — stored as a Trello board description.
This helps an AI classify and prioritize their messages. The user can read and edit this anytime.

Current memory:
---
{current_context or "(empty — first time setup)"}
---

Here's what just happened in the latest batch of Slack messages:
{batch_summary}

Update the memory to reflect any new insights. The memory should contain:
- **Projects**: what the user is currently working on
- **People**: key collaborators, their manager, their team
- **Priorities**: what's urgent or important right now
- **Ignore**: topics/channels the user doesn't care about

Rules:
- Keep it SHORT — under 15 lines total
- Only add genuinely new information, don't repeat what's already there
- Remove stale info if the batch contradicts it
- Write in second person ("You are working on...", "Your team includes...")
- If nothing new to add, return the current memory unchanged

Output ONLY the updated memory text, nothing else."""

    try:
        response = llm.invoke(prompt)
        return response.content.strip()
    except Exception as e:
        logger.warning(f"Failed to update board context: {e}")
        return current_context


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


SHORT_MESSAGE_THRESHOLD = 20  # chars — fetch context for messages shorter than this


def _build_message_context(message_events, slack, batch_trace_id):
    """Build enriched context for each message event.

    Short non-thread messages get preceding channel messages fetched
    so Opus can understand context like "ok!" or "thanks".
    """
    messages = []
    for i, evt in enumerate(message_events):
        event = evt["event"]
        user_id = event.get("user", "")
        channel_id = event.get("channel", "")
        text = event.get("text", "")
        message_ts = event.get("ts", "")
        thread_ts = event.get("thread_ts", "")

        try:
            sender = slack.get_user_name(user_id)
            channel = slack.get_channel_name(channel_id)
            text = slack.resolve_mentions(text)
        except Exception:
            sender = user_id
            channel = channel_id

        is_thread = bool(thread_ts and thread_ts != message_ts)

        # Fetch preceding messages for short, non-thread messages
        preceding_context = ""
        if len(text.strip()) < SHORT_MESSAGE_THRESHOLD and not is_thread:
            try:
                preceding = slack.get_preceding_messages(channel_id, message_ts, count=3)
                if preceding:
                    lines = [f"    {m['sender']}: {m['text']}" for m in preceding]
                    preceding_context = "\n".join(lines)
            except Exception:
                pass

        messages.append({
            "idx": i + 1,
            "text": text,
            "sender": sender,
            "channel": channel,
            "channel_id": channel_id,
            "user_id": user_id,
            "ts": message_ts,
            "thread_ts": thread_ts,
            "is_thread_reply": is_thread,
            "preceding_context": preceding_context,
        })
    return messages


def _process_thread_reply(msg, slack, trello, cards, batch_trace_id):
    """Handle a thread reply — find parent card, add comment, skip Opus."""
    thread_ts = msg["thread_ts"]

    card = find_card_by_thread_ts(cards, thread_ts)
    if not card:
        return False  # parent not found, fall back to classification

    channel_link = slack.channel_link(msg["channel_id"])
    msg_link = slack.message_link(msg["channel_id"], msg["ts"]) if msg.get("ts") else ""

    comment = f'**{msg["sender"]}** in [#{msg["channel"]}]({channel_link}) (thread reply):\n{msg["text"]}'
    if msg_link:
        comment += f'\n\n[View message]({msg_link})'

    trello.add_comment(card["id"], comment)

    # Add this reply's ts to threads section too
    current_desc = card.get("desc", "")
    new_desc = append_thread_entry(
        current_desc, msg["sender"], msg["channel"],
        msg["text"], msg_link, msg["ts"],
    )
    trello.update_card_desc(card["id"], new_desc)
    card["desc"] = new_desc

    log_structured(batch_trace_id, "thread_reply", "success", {
        "card_id": card["id"], "parent_ts": thread_ts,
    })
    return True


def process_messages(message_events, slack, trello, cards, list_map, batch_trace_id, user_context=""):
    """Batch-classify messages with one Opus call, then update Trello.

    Thread replies skip classification entirely — they're matched to their
    parent card via thread_ts stored in the card description.
    """
    all_messages = _build_message_context(message_events, slack, batch_trace_id)

    # Process thread replies first (no Opus needed)
    top_level = []
    thread_replies_handled = 0
    thread_replies_unmatched = []

    for msg in all_messages:
        if msg["is_thread_reply"]:
            try:
                if _process_thread_reply(msg, slack, trello, cards, batch_trace_id):
                    thread_replies_handled += 1
                    continue
            except Exception as e:
                logger.error(f"Failed thread reply for ts={msg['ts']}: {e}")
            # Parent not found — include in classification batch
            thread_replies_unmatched.append(msg)
        else:
            top_level.append(msg)

    # Combine top-level + unmatched thread replies for classification
    to_classify = top_level + thread_replies_unmatched

    if thread_replies_handled:
        log_structured(batch_trace_id, "thread_replies", metadata={
            "handled": thread_replies_handled,
            "unmatched": len(thread_replies_unmatched),
        })

    if not to_classify:
        return

    # Re-index for the Opus prompt
    for i, msg in enumerate(to_classify):
        msg["idx"] = i + 1

    # Get existing topics (active cards only, excluding Noted)
    existing_topics = [
        {"id": c["id"], "name": c["name"], "list_name": list_map.get(c["idList"], "Unknown")}
        for c in cards
        if list_map.get(c["idList"]) != "Noted"
    ]

    # Fetch recent history for channels in this batch
    channel_ids_in_batch = {m["channel_id"]: m["channel"] for m in to_classify}
    channel_context = {}
    for channel_id, channel_name in channel_ids_in_batch.items():
        try:
            history = slack.get_channel_history(channel_id, limit=15)
            if history:
                channel_context[channel_name] = history
        except Exception as e:
            logger.warning(f"Failed to fetch history for #{channel_name}: {e}")

    log_structured(batch_trace_id, "batch_classify_start", metadata={
        "message_count": len(to_classify),
        "existing_topics": len(existing_topics),
        "channels_with_context": len(channel_context),
    })

    # ONE Opus call for the entire batch
    try:
        classifications = batch_classify_messages(
            to_classify, existing_topics, user_context,
            channel_context=channel_context,
        )
        log_structured(batch_trace_id, "batch_classify_done", "success", {
            "results": len(classifications),
        })
    except Exception as e:
        logger.error(f"Batch classification failed: {e}")
        log_structured(batch_trace_id, "batch_classify_done", "failure", {"error": str(e)})
        return

    # Process each classified message
    new_cards_by_topic = {}  # topic_name -> card_id

    for classification in classifications:
        msg_idx = classification.get("msg_idx", 0) - 1  # back to 0-indexed
        if msg_idx < 0 or msg_idx >= len(to_classify):
            continue

        msg = to_classify[msg_idx]

        try:
            _apply_classification(
                classification, msg, batch_trace_id,
                trello, slack, cards, list_map, new_cards_by_topic,
            )
        except Exception as e:
            logger.error(f"Failed to process message {msg_idx}: {e}")
            log_structured(batch_trace_id, "trello_action", "failure", {"error": str(e)})


def _apply_classification(classification, msg, trace_id, trello, slack, cards, list_map, new_cards_by_topic):
    """Apply a single classification result to Trello."""
    priority = classification.get("priority", "worth_reading")

    # Drop noise messages entirely — they never reach Trello
    if priority == "noise":
        logger.info(f"[{trace_id}] Dropping noise message from {msg['sender']} in #{msg['channel']}: {msg['text'][:60]}")
        return

    target_list_name = PRIORITY_TO_LIST.get(priority, "Worth Reading")
    target_list_id = trello.get_list_id(target_list_name)

    existing_topic_id = classification.get("existing_topic_id")
    topic_name = classification.get("topic_name", f'{msg["sender"]}: {msg["text"][:40]}')
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

        # Uncheck all completed checklist items (card is active again)
        try:
            for cl in trello.get_checklists(card_id):
                for item in cl.get("checkItems", []):
                    if item["state"] == "complete":
                        trello.update_checklist_item(card_id, item["id"], completed=False)
        except Exception as e:
            logger.warning(f"[{trace_id}] Failed to uncheck items on {card_id}: {e}")

        # Get current checklist items for Haiku to revise
        current_items = get_current_action_items(trello, card_id)

        # Merge Opus action items (from classification) into current items for Haiku
        existing_texts = {item["text"].lower().strip() for item in current_items}
        for item_text in action_items:
            if item_text.lower().strip() not in existing_texts:
                current_items.append({"text": item_text, "completed": False})

        # Update description + action items via Haiku
        current_desc = current_card.get("desc", "") if current_card else ""
        result = update_description_summary(
            current_desc, msg["channel"], msg["text"], msg["sender"],
            current_action_items=current_items,
        )
        new_desc = result["description"]
        revised_items = result.get("action_items", [])

        # Add thread tracking entry
        new_desc = append_thread_entry(
            new_desc, msg["sender"], msg["channel"],
            msg["text"], msg_link, msg["ts"],
        )
        trello.update_card_desc(card_id, new_desc)
        if current_card:
            current_card["desc"] = new_desc

        # Sync action items
        if current_items or revised_items:
            sync_action_items(trello, card_id, current_items, revised_items)

        log_structured(trace_id, "trello_update", "success", {
            "card_id": card_id, "topic": topic_name, "priority": priority,
        })
    else:
        # --- Create new card ---
        # Seed with Opus action items
        seed_items = [{"text": t, "completed": False} for t in action_items] if action_items else None

        result = update_description_summary(
            "", msg["channel"], msg["text"], msg["sender"],
            current_action_items=seed_items,
        )
        initial_desc = result["description"]
        revised_items = result.get("action_items", [])

        # Add thread tracking entry
        initial_desc = append_thread_entry(
            initial_desc, msg["sender"], msg["channel"],
            msg["text"], msg_link, msg["ts"],
        )
        card = trello.create_card(target_list_id, topic_name, desc=initial_desc)
        card_id = card["id"]
        trello.add_comment(card_id, comment)

        # Add action items to checklist
        if revised_items:
            checklist = trello.create_checklist(card_id)
            for item in revised_items:
                trello.add_checklist_item(checklist["id"], item.get("text", ""))

        # Track for other messages in this batch
        new_cards_by_topic[topic_name] = card_id
        cards.append({"id": card_id, "name": topic_name, "idList": target_list_id, "desc": initial_desc})

        log_structured(trace_id, "trello_create", "success", {
            "card_id": card_id, "topic": topic_name, "priority": priority,
        })


# --- Process Reactions ---


def process_reactions(reaction_events, slack, trello, cards, list_map, batch_trace_id):
    """Process reaction events — update Reactions section on matching cards."""
    my_user_id = slack.get_authed_user_id()

    for evt in reaction_events:
        event = evt["event"]
        user_id = event.get("user", "")
        reaction = event.get("reaction", "")
        item = event.get("item", {})
        channel_id = item.get("channel", "")
        message_ts = item.get("ts", "")

        # Fetch the reacted-to message
        try:
            msg = slack.get_message(channel_id, message_ts)
            if not msg:
                log_structured(batch_trace_id, "skip", metadata={"reason": "message_not_found"})
                continue
            if msg.get("user") != my_user_id:
                log_structured(batch_trace_id, "skip", metadata={"reason": "not_my_message"})
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
            log_structured(batch_trace_id, "skip", metadata={"reason": "no_matching_card"})
            continue

        card = matching_cards[0]
        current_desc = card.get("desc", "")

        try:
            new_desc = update_description_reactions(current_desc, reactor_name, reaction, original_text)
            trello.update_card_desc(card["id"], new_desc)
            log_structured(batch_trace_id, "trello_update", "success", {
                "card_id": card["id"], "reaction": reaction,
            })
        except Exception as e:
            logger.error(f"Failed to update reaction on card: {e}")
            log_structured(batch_trace_id, "trello_action", "failure", {"error": str(e)})


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

    # Clear queue immediately to prevent reprocessing on next batch
    delete_pending_events(pending)
    log_structured(batch_trace_id, "queue_cleared", metadata={"count": len(pending)})

    # Separate messages from reactions
    messages = [e for e in pending if e["event"].get("type") != "reaction_added"]
    reactions = [e for e in pending if e["event"].get("type") == "reaction_added"]

    log_structured(batch_trace_id, "batch_start", metadata={
        "total_events": len(pending),
        "messages": len(messages),
        "reactions": len(reactions),
    })

    # Initialize clients
    slack = SlackClient()
    trello = TrelloClient()

    # Fetch board state once
    try:
        user_context = trello.get_board_desc()
        lists = trello.get_lists()
        list_map = {lst["id"]: lst["name"] for lst in lists}
        cards = trello.get_cards()
        logger.info(f"Board: {len(cards)} open cards")
    except Exception as e:
        logger.error(f"Failed to fetch Trello board: {e}")
        log_structured(batch_trace_id, "trello_fetch", "failure", {"error": str(e)})
        sys.exit(1)

    # Process messages (one Opus call for the batch)
    if messages:
        process_messages(
            messages, slack, trello, cards, list_map,
            batch_trace_id, user_context=user_context,
        )

    # Process reactions (Haiku only, per reaction)
    if reactions:
        process_reactions(reactions, slack, trello, cards, list_map, batch_trace_id)

    # Update board context memory with observations from this batch
    if messages:
        try:
            batch_summary = _build_batch_summary(messages, slack)
            new_context = update_board_context(user_context, batch_summary)
            if new_context != user_context:
                trello.update_board_desc(new_context)
                log_structured(batch_trace_id, "context_updated")
        except Exception as e:
            logger.warning(f"Failed to update board context: {e}")

    logger.info(f"Batch complete. Processed {len(pending)} events.")
    log_structured(batch_trace_id, "batch_complete", metadata={
        "messages": len(messages), "reactions": len(reactions),
    })


def _build_batch_summary(message_events, slack):
    """Build a short summary of the batch for context updates."""
    lines = []
    channels_seen = set()
    people_seen = set()
    for evt in message_events[:20]:  # Cap to avoid huge summaries
        event = evt["event"]
        user_id = event.get("user", "")
        channel_id = event.get("channel", "")
        text = event.get("text", "")[:100]
        try:
            name = slack.get_user_name(user_id)
            channel = slack.get_channel_name(channel_id)
        except Exception:
            name = user_id
            channel = channel_id
        people_seen.add(name)
        channels_seen.add(channel)
        lines.append(f"- {name} in #{channel}: {text}")

    header = f"Channels: {', '.join(channels_seen)}. People: {', '.join(people_seen)}."
    return header + "\n" + "\n".join(lines)


if __name__ == "__main__":
    main()
