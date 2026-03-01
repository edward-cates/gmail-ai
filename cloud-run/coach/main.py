"""Muscle Growth Coach - Cloud Run Job.

Trello-based coaching agent using an agentic tool-use loop. Two modes:
- morning: Read board summary, create exercise + nutrition + forum cards via tools
- reply: Read card context, respond to user's comment via tools

Board has four lists: Exercise, Nutrition, Forum, Memories.

Reads COACH_MODE from environment:
- "morning": Create daily exercise card + nutrition card + forum card
- "reply": Process user comment on a card and respond
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import anthropic
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-6"
SPEC_MODEL = "claude-haiku-4-5-20251001"
CT = timezone(timedelta(hours=-6))


def log_structured(trace_id, stage, result="success", metadata=None):
    """Log structured JSON to Cloud Logging."""
    log_data = {
        "trace_id": trace_id,
        "stage": stage,
        "result": result,
        "service": "coach",
    }
    if metadata:
        log_data["metadata"] = metadata
    print(json.dumps(log_data), flush=True)


# --- Trello Client ---


class TrelloClient:
    """Trello API client for the coaching board."""

    BASE_URL = "https://api.trello.com/1"

    def __init__(self):
        self.api_key = os.getenv("TRELLO_API_KEY")
        self.token = os.getenv("TRELLO_TOKEN")
        self.board_id = os.getenv("TRELLO_COACH_BOARD_ID")
        if not all([self.api_key, self.token, self.board_id]):
            raise ValueError(
                "TRELLO_API_KEY, TRELLO_TOKEN, and TRELLO_COACH_BOARD_ID must be set"
            )
    def _auth_params(self):
        return {"key": self.api_key, "token": self.token}

    def _request(self, method, url, params=None, retries=3, **kwargs):
        """Make an authenticated request with retry and credential sanitization."""
        merged = {**self._auth_params(), **(params or {})}
        last_exc = None
        for attempt in range(retries):
            try:
                r = requests.request(method, url, params=merged, **kwargs)
                r.raise_for_status()
                return r
            except requests.HTTPError:
                if r.status_code < 500:
                    # Client errors (4xx) — not retryable, fail immediately
                    clean_url = re.sub(r"[?&](key|token)=[^&]*", "", str(r.url))
                    raise requests.HTTPError(
                        f"{r.status_code} {r.reason} for url: {clean_url}",
                        response=r,
                    ) from None
                last_exc = requests.HTTPError(
                    f"{r.status_code} {r.reason}", response=r,
                )
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
            except requests.ConnectionError as e:
                last_exc = e
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
        raise type(last_exc)(
            re.sub(r"[?&](key|token)=[^&]*", "", str(last_exc))
        ) from None

    def get_board_desc(self):
        """Get the board description (used as spec/manifesto)."""
        r = self._request(
            "GET", f"{self.BASE_URL}/boards/{self.board_id}",
            params={"fields": "desc"},
        )
        return r.json().get("desc", "")

    def update_board_desc(self, desc):
        """Update the board description."""
        r = self._request(
            "PUT", f"{self.BASE_URL}/boards/{self.board_id}",
            data={"desc": desc},
        )
        return r.json()

    def get_lists(self):
        """Get all lists on the board."""
        r = self._request("GET", f"{self.BASE_URL}/boards/{self.board_id}/lists")
        return r.json()

    def get_list_id(self, list_name):
        """Get list ID by name."""
        for lst in self.get_lists():
            if lst["name"].lower() == list_name.lower():
                return lst["id"]
        raise ValueError(f"List '{list_name}' not found on board")

    def get_cards(self):
        """Get all open cards on the board."""
        r = self._request("GET", f"{self.BASE_URL}/boards/{self.board_id}/cards")
        return r.json()

    def get_card(self, card_id):
        """Get a single card by ID."""
        r = self._request("GET", f"{self.BASE_URL}/cards/{card_id}")
        return r.json()

    def get_card_comments(self, card_id, limit=50):
        """Get comments on a card (newest first from API, reversed to oldest first)."""
        r = self._request(
            "GET", f"{self.BASE_URL}/cards/{card_id}/actions",
            params={"filter": "commentCard", "limit": limit},
        )
        comments = r.json()
        comments.reverse()  # oldest first
        return comments

    def create_card(self, list_id, name, desc=""):
        """Create a new card."""
        r = self._request(
            "POST", f"{self.BASE_URL}/cards",
            params={"idList": list_id, "pos": "top"},
            data={"name": name, "desc": desc},
        )
        return r.json()

    def move_card(self, card_id, list_id):
        """Move a card to a different list."""
        r = self._request(
            "PUT", f"{self.BASE_URL}/cards/{card_id}",
            params={"idList": list_id},
        )
        return r.json()

    def add_comment(self, card_id, text):
        """Add a comment to a card, prefixed with coach marker.

        The marker lets the webhook handler distinguish coach comments
        from user comments (both come from the same Trello account).
        Uses POST body instead of query params to avoid URL length limits.
        """
        # Strip any existing prefix to avoid duplication (Claude may echo it from context)
        if text.startswith(COACH_PREFIX):
            text = text[len(COACH_PREFIX):].lstrip()
        prefixed = f"{COACH_PREFIX} {text}"
        r = self._request(
            "POST", f"{self.BASE_URL}/cards/{card_id}/actions/comments",
            data={"text": prefixed},
        )
        return r.json()

    def create_checklist(self, card_id, name="Exercises"):
        """Create a checklist on a card."""
        r = self._request(
            "POST", f"{self.BASE_URL}/cards/{card_id}/checklists",
            params={"name": name},
        )
        return r.json()

    def add_checklist_item(self, checklist_id, name):
        """Add an item to a checklist."""
        r = self._request(
            "POST", f"{self.BASE_URL}/checklists/{checklist_id}/checkItems",
            params={"name": name},
        )
        return r.json()

    def archive_card(self, card_id):
        """Archive (close) a card."""
        r = self._request(
            "PUT", f"{self.BASE_URL}/cards/{card_id}",
            params={"closed": "true"},
        )
        return r.json()

    def update_card(self, card_id, **fields):
        """Update card fields (name, desc, etc.)."""
        r = self._request(
            "PUT", f"{self.BASE_URL}/cards/{card_id}",
            data=fields,
        )
        return r.json()

    def get_card_checklists(self, card_id):
        """Get all checklists on a card with items and states."""
        r = self._request("GET", f"{self.BASE_URL}/cards/{card_id}/checklists")
        return r.json()

    def add_reaction(self, action_id, emoji_short_name):
        """Add an emoji reaction to a comment (action)."""
        r = self._request(
            "POST", f"{self.BASE_URL}/actions/{action_id}/reactions",
            json={"shortName": emoji_short_name},
        )
        return r.json()

    def set_check_item_state(self, card_id, check_item_id, state="complete"):
        """Check or uncheck a checklist item. state: 'complete' or 'incomplete'."""
        r = self._request(
            "PUT", f"{self.BASE_URL}/cards/{card_id}/checkItem/{check_item_id}",
            params={"state": state},
        )
        return r.json()


# --- Context Helpers ---


COACH_PREFIX = "**[Coach]**"


def _utc_to_ct(iso_str):
    """Convert a UTC ISO timestamp to Central Time display string."""
    if not iso_str or len(iso_str) < 16:
        return iso_str
    try:
        from datetime import UTC
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        ct = dt.astimezone(CT)
        return ct.strftime("%b %-d %-I:%M %p").lower()
    except (ValueError, TypeError):
        return iso_str[:16]


def _comment_role(text):
    """Determine if a comment is from the coach or the client."""
    return "Coach" if text.startswith(COACH_PREFIX) else "Client"


def read_memories(trello):
    """Read all memory cards and their comments from the Memories list."""
    lists = trello.get_lists()
    memories_id = None
    for lst in lists:
        if lst["name"].lower() == "memories":
            memories_id = lst["id"]
            break
    if not memories_id:
        return ""

    cards = trello.get_cards()
    memory_cards = [c for c in cards if c.get("idList") == memories_id]

    lines = []
    for card in memory_cards:
        lines.append(f"### {card['name']}  (card_id: {card['id']})")
        if card.get("desc"):
            lines.append(card["desc"])
        comments = trello.get_card_comments(card["id"])
        for comment in comments:
            ts = _utc_to_ct(comment.get("date", ""))
            text = comment.get("data", {}).get("text", "")
            if text.startswith(COACH_PREFIX):
                text = text[len(COACH_PREFIX):].lstrip()
            lines.append(f"- [{ts}] {text}")
        lines.append("")
    return "\n".join(lines)


def read_card_context(trello, card_id):
    """Read all comments on a specific card.

    Returns a formatted string with the card name and all comments.
    """
    card = trello.get_card(card_id)
    comments = trello.get_card_comments(card_id)

    lines = [f"### Card: {card['name']}"]
    if card.get("desc"):
        lines.append(card["desc"])
    lines.append("")

    for comment in comments:
        ts = comment.get("date", "")[:16]
        text = comment.get("data", {}).get("text", "")
        role = _comment_role(text)
        lines.append(f"[{ts}] {role}: {text}")

    return "\n".join(lines)


def build_board_summary(trello):
    """Build lightweight board summary: card titles + IDs + list membership + last_activity.

    Excludes Memories list cards (read separately via read_memories).
    """
    cards = trello.get_cards()
    lists = trello.get_lists()
    list_names = {lst["id"]: lst["name"] for lst in lists}

    memories_id = None
    for lst in lists:
        if lst["name"].lower() == "memories":
            memories_id = lst["id"]
            break

    lines = []
    for card in cards:
        if card.get("idList") == memories_id:
            continue
        list_name = list_names.get(card.get("idList"), "Unknown")
        last_activity = _utc_to_ct(card.get("dateLastActivity", ""))
        lines.append(
            f"- [{list_name}] {card['name']}  "
            f"(card_id: {card['id']}, last_activity: {last_activity})"
        )

    return "\n".join(lines)


# --- Claude ---


def _get_client():
    """Create an Anthropic client."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=api_key)


def apply_spec_update(current_spec, instruction):
    """Apply a spec update instruction using Haiku (fast, cheap).

    The coach describes what to change in plain English; Haiku applies
    the edit and returns the full updated spec.
    """
    client = _get_client()

    response = client.messages.create(
        model=SPEC_MODEL,
        max_tokens=8000,
        messages=[{"role": "user", "content": f"""You are editing a client spec document. Apply the requested changes and return the COMPLETE updated document.

## Current Spec
{current_spec}

## Requested Changes
{instruction}

## Rules
- Return the COMPLETE updated spec (not a diff)
- Preserve all existing content that isn't being changed
- Keep the same markdown formatting style
- Only make the changes described above — nothing else
- Return ONLY the updated spec text, no commentary or markdown fences"""}],
    )
    return response.content[0].text.strip()


SPEC_CONSOLIDATION_THRESHOLD = 14_000
SPEC_CONSOLIDATION_TARGET = 10_000


def consolidate_spec(current_spec):
    """Compress a spec that has grown too large (sawtooth pattern).

    Called when spec exceeds SPEC_CONSOLIDATION_THRESHOLD after an update.
    Uses Haiku with an explicit size target to aggressively compress.
    """
    client = _get_client()
    response = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        messages=[{"role": "user", "content": f"""You are a fitness coach consolidating your client notebook. The spec below has grown too long and must be compressed.

## Current Spec ({len(current_spec)} characters)
{current_spec}

## Task
Rewrite this spec to fit within {SPEC_CONSOLIDATION_TARGET} characters. This is a hard limit.

## Priority (highest to lowest)
1. Active goals, current training program, and schedule
2. Recent PRs, progress trends, and body metrics
3. Preferences, constraints, and communication style
4. Supplement and nutrition plans
5. Historical context and resolved items

## Compression Rules
- Merge redundant sections
- Compress daily details into trends (e.g. "bench progressed 135→185 over 6 weeks")
- Drop completed one-off items (old grocery lists, past event prep)
- Replace verbose descriptions with concise bullet points
- Keep the same markdown formatting style
- Return ONLY the updated spec text, no commentary or markdown fences"""}],
    )
    return response.content[0].text.strip()


def _apply_spec_if_needed(trello, trace_id, spec, instruction):
    """Apply spec update instruction via Haiku if instruction is provided."""
    if not instruction:
        return
    try:
        updated_spec = apply_spec_update(spec, instruction)

        # Sawtooth: consolidate if spec has grown too large
        if len(updated_spec) > SPEC_CONSOLIDATION_THRESHOLD:
            pre_len = len(updated_spec)
            updated_spec = consolidate_spec(updated_spec)
            log_structured(trace_id, "spec_consolidated", metadata={
                "before": pre_len,
                "after": len(updated_spec),
            })

        trello.update_board_desc(updated_spec)
        log_structured(trace_id, "spec_updated", metadata={
            "instruction": instruction[:120],
            "spec_length": len(updated_spec),
        })
    except Exception as e:
        logger.error(f"[{trace_id}] Spec update failed: {e}", exc_info=True)
        log_structured(trace_id, "spec_updated", "failure", {"error": str(e)})


def _create_card_with_checklist(trello, trace_id, list_name, card_data):
    """Create a card with optional checklist and comment."""
    list_id = trello.get_list_id(list_name)
    card = trello.create_card(list_id, card_data["title"], card_data.get("description", ""))
    log_structured(trace_id, f"create_{list_name.lower()}_card", metadata={"card_id": card["id"]})

    checklist_items = card_data.get("checklist", [])
    if checklist_items:
        checklist = trello.create_checklist(card["id"])
        for item in checklist_items:
            trello.add_checklist_item(checklist["id"], item)
        log_structured(trace_id, f"create_{list_name.lower()}_checklist", metadata={
            "items": len(checklist_items),
        })

    comment = card_data.get("comment", "")
    if comment:
        trello.add_comment(card["id"], comment)
        log_structured(trace_id, f"add_{list_name.lower()}_comment", metadata={
            "length": len(comment),
        })

    return card


# --- Agent ---


COACH_TOOLS = [
    {
        "name": "read_card",
        "description": "Read a card's full details: name, description, checklist items with IDs and completion states, and all comments with timestamps and Coach/Client roles.",
        "input_schema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "Trello card ID"},
            },
            "required": ["card_id"],
        },
    },
    {
        "name": "archive_card",
        "description": "Archive (close) a card.",
        "input_schema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "Trello card ID"},
            },
            "required": ["card_id"],
        },
    },
    {
        "name": "check_item",
        "description": "Check off a checklist item as complete.",
        "input_schema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "Trello card ID"},
                "item_id": {"type": "string", "description": "Checklist item ID"},
            },
            "required": ["card_id", "item_id"],
        },
    },
    {
        "name": "uncheck_item",
        "description": "Uncheck a checklist item (mark incomplete).",
        "input_schema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "Trello card ID"},
                "item_id": {"type": "string", "description": "Checklist item ID"},
            },
            "required": ["card_id", "item_id"],
        },
    },
    {
        "name": "move_card",
        "description": "Move a card to a different list.",
        "input_schema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "Trello card ID"},
                "list_name": {
                    "type": "string",
                    "description": "Target list name (Exercise, Nutrition, Forum, or Memories)",
                },
            },
            "required": ["card_id", "list_name"],
        },
    },
    {
        "name": "comment_on_card",
        "description": "Add a comment to a card. Auto-prefixed with coach marker.",
        "input_schema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "Trello card ID"},
                "text": {"type": "string", "description": "Comment text"},
            },
            "required": ["card_id", "text"],
        },
    },
    {
        "name": "update_card",
        "description": "Update a card's name and/or description.",
        "input_schema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "Trello card ID"},
                "name": {"type": "string", "description": "New card name"},
                "desc": {"type": "string", "description": "New card description"},
            },
            "required": ["card_id"],
        },
    },
    {
        "name": "create_card",
        "description": "Create a new card on a list with optional description, checklist, and comment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "list_name": {
                    "type": "string",
                    "description": "List name (Exercise, Nutrition, Forum, or Memories)",
                },
                "title": {"type": "string", "description": "Card title"},
                "description": {"type": "string", "description": "Card description"},
                "checklist": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Checklist items",
                },
                "comment": {"type": "string", "description": "Initial comment on the card"},
            },
            "required": ["list_name", "title"],
        },
    },
    {
        "name": "update_spec",
        "description": "Update the client spec (board description). Provide a brief plain-English instruction describing what to change.",
        "input_schema": {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "Brief description of what to change in the spec",
                },
            },
            "required": ["instruction"],
        },
    },
    {
        "name": "add_reaction",
        "description": "React to a comment with an emoji.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action_id": {"type": "string", "description": "Trello action (comment) ID"},
                "emoji": {
                    "type": "string",
                    "description": "Emoji shortname (e.g. muscle, fire, thumbsup, heart)",
                },
            },
            "required": ["action_id", "emoji"],
        },
    },
]


def execute_tool(trello, trace_id, tool_name, tool_input):
    """Execute a tool call and return the result string. Errors returned as strings."""
    try:
        if tool_name == "read_card":
            card_id = tool_input["card_id"]
            card = trello.get_card(card_id)
            lines = [f"### Card: {card['name']}"]
            if card.get("desc"):
                lines.append(card["desc"])
            try:
                checklists = trello.get_card_checklists(card_id)
                for cl in checklists:
                    for item in cl.get("checkItems", []):
                        state = "x" if item.get("state") == "complete" else " "
                        lines.append(
                            f"- [{state}] {item['name']}  (item_id: {item['id']})"
                        )
            except Exception:
                pass
            lines.append("")
            comments = trello.get_card_comments(card_id)
            for comment in comments:
                ts = _utc_to_ct(comment.get("date", ""))
                text = comment.get("data", {}).get("text", "")
                role = _comment_role(text)
                lines.append(f"[{ts}] {role}: {text}")
            result = "\n".join(lines)
            log_structured(trace_id, "tool_read_card", metadata={
                "card_id": card_id, "length": len(result),
            })
            return result

        elif tool_name == "archive_card":
            trello.archive_card(tool_input["card_id"])
            log_structured(trace_id, "tool_archive_card", metadata={
                "card_id": tool_input["card_id"],
            })
            return "Card archived."

        elif tool_name == "check_item":
            trello.set_check_item_state(
                tool_input["card_id"], tool_input["item_id"], "complete",
            )
            log_structured(trace_id, "tool_check_item", metadata={
                "card_id": tool_input["card_id"], "item_id": tool_input["item_id"],
            })
            return "Item checked."

        elif tool_name == "uncheck_item":
            trello.set_check_item_state(
                tool_input["card_id"], tool_input["item_id"], "incomplete",
            )
            log_structured(trace_id, "tool_uncheck_item", metadata={
                "card_id": tool_input["card_id"], "item_id": tool_input["item_id"],
            })
            return "Item unchecked."

        elif tool_name == "move_card":
            list_id = trello.get_list_id(tool_input["list_name"])
            trello.move_card(tool_input["card_id"], list_id)
            log_structured(trace_id, "tool_move_card", metadata={
                "card_id": tool_input["card_id"], "list": tool_input["list_name"],
            })
            return f"Card moved to {tool_input['list_name']}."

        elif tool_name == "comment_on_card":
            trello.add_comment(tool_input["card_id"], tool_input["text"])
            log_structured(trace_id, "tool_comment", metadata={
                "card_id": tool_input["card_id"], "length": len(tool_input["text"]),
            })
            return "Comment posted."

        elif tool_name == "update_card":
            fields = {}
            if tool_input.get("name"):
                fields["name"] = tool_input["name"]
            if tool_input.get("desc"):
                fields["desc"] = tool_input["desc"]
            if fields:
                trello.update_card(tool_input["card_id"], **fields)
            log_structured(trace_id, "tool_update_card", metadata={
                "card_id": tool_input["card_id"], "fields": list(fields.keys()),
            })
            return "Card updated."

        elif tool_name == "create_card":
            card = _create_card_with_checklist(trello, trace_id, tool_input["list_name"], {
                "title": tool_input["title"],
                "description": tool_input.get("description", ""),
                "checklist": tool_input.get("checklist", []),
                "comment": tool_input.get("comment", ""),
            })
            return f"Card created (card_id: {card['id']})."

        elif tool_name == "update_spec":
            current_spec = trello.get_board_desc()
            _apply_spec_if_needed(trello, trace_id, current_spec, tool_input["instruction"])
            return "Spec updated."

        elif tool_name == "add_reaction":
            trello.add_reaction(tool_input["action_id"], tool_input["emoji"])
            log_structured(trace_id, "tool_add_reaction", metadata={
                "action_id": tool_input["action_id"], "emoji": tool_input["emoji"],
            })
            return "Reaction added."

        else:
            return f"Unknown tool: {tool_name}"

    except Exception as e:
        logger.error(f"[{trace_id}] Tool {tool_name} failed: {e}")
        return f"Error: {e}"


def run_agent(client, trello, trace_id, system_prompt, user_message, tools=None):
    """Run an agentic tool-use loop until the model stops or hits the iteration cap."""
    if tools is None:
        tools = COACH_TOOLS

    messages = [{"role": "user", "content": user_message}]

    for iteration in range(25):
        response = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            log_structured(trace_id, "agent_complete", metadata={
                "iterations": iteration + 1,
            })
            return

        if response.stop_reason != "tool_use":
            log_structured(trace_id, "agent_stop", metadata={
                "stop_reason": response.stop_reason,
                "iterations": iteration + 1,
            })
            return

        # Process tool calls
        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = execute_tool(trello, trace_id, block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "user", "content": tool_results})

    log_structured(trace_id, "agent_max_iterations")


# --- Handlers ---


def handle_morning(trace_id):
    """Generate and post daily exercise and nutrition cards via agent loop."""
    try:
        trello = TrelloClient()

        spec = trello.get_board_desc()
        log_structured(trace_id, "read_spec", metadata={"length": len(spec)})

        memories = read_memories(trello)
        log_structured(trace_id, "read_memories", metadata={"length": len(memories)})

        board_summary = build_board_summary(trello)
        log_structured(trace_id, "read_summary", metadata={"length": len(board_summary)})

        now_ct = datetime.now(tz=CT)
        day_of_week = now_ct.strftime("%A")
        date_str = now_ct.strftime("%B %d, %Y")
        time_str = now_ct.strftime("%-I:%M %p").lower()

        system_prompt = f"""You are a muscle growth coach managing a client's training via a Trello board.
Each morning you create cards for the day's training and nutrition.

Today is {day_of_week}, {date_str}. Current time: {time_str} Central.

## Board Structure
- **Exercise** — one card per workout (title, routine in description, checklist of exercises)
- **Nutrition** — cards for meals, grocery runs, supplements (title, description, checklist)
- **Forum** — daily check-in card for open conversation throughout the day
- **Memories** — long-term memory cards organized by category (one card per topic, comments are individual memories)

## Your Task
1. Use read_card to inspect any recent cards that look relevant
2. Archive old completed cards from previous days
3. Create today's cards using create_card:
   - Exercise card (skip on rest days): title like "{day_of_week}, {date_str} — [Focus Area]", description with full workout, checklist of exercises with weights/sets/reps, conversational comment
   - Nutrition card: title like "{day_of_week}, {date_str} — Nutrition", description with meal plan, checklist of actionable items
   - Forum check-in card: title like "{day_of_week}, {date_str} — Check In", conversational comment prompting the client
4. Update the spec or add memories as needed

## Rules
- Be specific to THEIR program — exercises, weights, sets, reps, rest periods
- Include warm-up guidance if relevant
- Exercise comments should be conversational and specific (not generic motivation)
- Nutrition checklist items should be actionable (meals to eat, items to buy)
- If the spec is empty, introduce yourself and ask what they're working on
- On rest days, skip the exercise card but still provide nutrition and forum
- The forum card is a daily check-in — open-ended prompt for the client

## Two-Tier Memory System
**Spec** (board description) — your working document:
- Current training program and schedule
- Active goals and preferences
- Plans and decisions that change over time
- Update via update_spec tool. Prune stale info, compress daily details into trends.

**Memories** (Memories list) — factual records that accumulate:
- Personal records (PRs, body weight milestones), injury history, recovery notes
- Key client info (schedule constraints, equipment access, dietary restrictions)
- Use comment_on_card on an existing memory card, or create_card on the Memories list for a new category
- Only store facts you'd actually reference later when coaching."""

        user_message = f"""## Client Spec
{spec or "(No spec yet — introduce yourself and ask about their goals)"}

## Memories
{memories or "(No memories yet)"}

## Board Summary (use read_card to see full details)
{board_summary or "(No cards on board — first interaction)"}"""

        client = _get_client()
        run_agent(client, trello, trace_id, system_prompt, user_message)

    except Exception as e:
        logger.error(f"[{trace_id}] Morning handler failed: {e}", exc_info=True)
        log_structured(trace_id, "handle_morning", "failure", {"error": str(e)})
        sys.exit(1)


def handle_reply(trace_id):
    """Process user comment on a card and respond via agent loop."""
    comment_text = os.getenv("COMMENT_TEXT", "")
    card_id = os.getenv("CARD_ID", "")
    action_id = os.getenv("ACTION_ID", "")

    if not comment_text or not card_id:
        log_structured(trace_id, "skip", metadata={"reason": "missing comment_text or card_id"})
        return

    try:
        trello = TrelloClient()

        spec = trello.get_board_desc()
        log_structured(trace_id, "read_spec", metadata={"length": len(spec)})

        memories = read_memories(trello)
        log_structured(trace_id, "read_memories", metadata={"length": len(memories)})

        board_summary = build_board_summary(trello)
        log_structured(trace_id, "read_summary", metadata={"length": len(board_summary)})

        # Pre-load the card being replied to (always relevant)
        card_context = read_card_context(trello, card_id)
        card = trello.get_card(card_id)
        card_name = card.get("name", "Unknown")
        log_structured(trace_id, "read_context", metadata={
            "card": card_name[:80],
            "summary_length": len(board_summary),
            "card_length": len(card_context),
        })

        now_ct = datetime.now(tz=CT)
        day_of_week = now_ct.strftime("%A")
        time_str = now_ct.strftime("%-I:%M %p").lower()

        reaction_instruction = ""
        if action_id:
            reaction_instruction = (
                f'\n4. React to their comment using '
                f'add_reaction(action_id="{action_id}", emoji="...") '
                f'with a fitting emoji (e.g. muscle, fire, thumbsup, heart, '
                f'tada, eyes, thinking_face, clap)'
            )

        system_prompt = f"""You are a muscle growth coach communicating with your client via Trello card comments.
Your client just commented on a card. Read their message and respond helpfully.

Today is {day_of_week}. Current time: {time_str} Central.

## Board Structure
- **Exercise** — workout cards (one per session)
- **Nutrition** — meal plans, grocery lists, supplement tracking
- **Forum** — ongoing discussion topics
- **Memories** — long-term memory cards organized by category

## Your Task
1. Understand what the client is telling you or asking
2. Reply using comment_on_card on the card they commented on (card_id: {card_id})
3. Take any other actions needed (check items, archive cards, create cards, update spec, add memories){reaction_instruction}

## Rules
- Be conversational but knowledgeable
- If they report a workout, acknowledge it specifically
- If they ask a question, give a direct answer then brief explanation
- If they share a concern (injury, plateau, motivation), address it with empathy and a plan
- Use read_card to inspect other cards if you need more context
- Use create_card when the client asks for a new workout plan, meal plan, grocery list, etc.

## Two-Tier Memory System
**Spec** (board description) — your working document:
- Current training program and schedule
- Active goals and preferences
- Plans and decisions that change over time
- Update via update_spec tool. Prune stale info, compress daily details into trends.

**Memories** (Memories list) — factual records that accumulate:
- Personal records (PRs, body weight milestones), injury history, recovery notes
- Key client info (schedule constraints, equipment access, dietary restrictions)
- Use comment_on_card on an existing memory card, or create_card on the Memories list for a new category
- Only store facts you'd actually reference later when coaching."""

        user_message = f"""## Client Spec
{spec or "(No spec yet)"}

## Memories
{memories or "(No memories yet)"}

## Board Summary (use read_card to see full details)
{board_summary or "(No cards on board)"}

## Card: {card_name} (card_id: {card_id})
{card_context}

## New Comment from Client
{comment_text}"""

        client = _get_client()
        run_agent(client, trello, trace_id, system_prompt, user_message)

        # React checkmark on user's comment as the very last step — signals job complete
        if action_id:
            try:
                trello.add_reaction(action_id, "white_check_mark")
            except Exception:
                pass  # Non-critical

    except Exception as e:
        logger.error(f"[{trace_id}] Reply handler failed: {e}", exc_info=True)
        log_structured(trace_id, "handle_reply", "failure", {"error": str(e)})
        sys.exit(1)


def main():
    """Main entry point."""
    trace_id = os.getenv("TRACE_ID", "coach-unknown")
    mode = os.getenv("COACH_MODE", "reply")

    log_structured(trace_id, "job_start", metadata={"mode": mode})

    if mode == "morning":
        handle_morning(trace_id)
    elif mode == "reply":
        handle_reply(trace_id)
    else:
        logger.error(f"[{trace_id}] Unknown COACH_MODE: {mode}")
        sys.exit(1)

    log_structured(trace_id, "job_complete")
    logger.info(f"[{trace_id}] Done ({mode})")


if __name__ == "__main__":
    main()
