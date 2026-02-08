"""Muscle Growth Coach - Cloud Run Job.

Trello-based coaching agent. Two modes:
- morning: Read board (spec + card history), generate exercise + nutrition cards
- reply: Read board desc + card comments, respond to user's comment

Board has three lists: Exercise, Nutrition, Forum.

Reads COACH_MODE from environment:
- "morning": Create daily exercise card + nutrition card
- "reply": Process user comment on a card and respond
"""

import json
import logging
import os
import re
import sys
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
    def _params(self, **extra):
        return {"key": self.api_key, "token": self.token, **extra}

    def get_board_desc(self):
        """Get the board description (used as spec/manifesto)."""
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
            params=self._params(),
            data={"desc": desc},
        )
        r.raise_for_status()
        return r.json()

    def get_lists(self):
        """Get all lists on the board."""
        r = requests.get(
            f"{self.BASE_URL}/boards/{self.board_id}/lists",
            params=self._params(),
        )
        r.raise_for_status()
        return r.json()

    def get_list_id(self, list_name):
        """Get list ID by name."""
        for lst in self.get_lists():
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

    def get_card(self, card_id):
        """Get a single card by ID."""
        r = requests.get(
            f"{self.BASE_URL}/cards/{card_id}",
            params=self._params(),
        )
        r.raise_for_status()
        return r.json()

    def get_card_comments(self, card_id, limit=50):
        """Get comments on a card (newest first from API, reversed to oldest first)."""
        r = requests.get(
            f"{self.BASE_URL}/cards/{card_id}/actions",
            params=self._params(filter="commentCard", limit=limit),
        )
        r.raise_for_status()
        comments = r.json()
        comments.reverse()  # oldest first
        return comments

    def create_card(self, list_id, name, desc=""):
        """Create a new card."""
        r = requests.post(
            f"{self.BASE_URL}/cards",
            params=self._params(idList=list_id, pos="top"),
            data={"name": name, "desc": desc},
        )
        r.raise_for_status()
        return r.json()

    def move_card(self, card_id, list_id):
        """Move a card to a different list."""
        r = requests.put(
            f"{self.BASE_URL}/cards/{card_id}",
            params=self._params(idList=list_id),
        )
        r.raise_for_status()
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
        r = requests.post(
            f"{self.BASE_URL}/cards/{card_id}/actions/comments",
            params=self._params(),
            data={"text": prefixed},
        )
        r.raise_for_status()
        return r.json()

    def create_checklist(self, card_id, name="Exercises"):
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

    def archive_card(self, card_id):
        """Archive (close) a card."""
        r = requests.put(
            f"{self.BASE_URL}/cards/{card_id}",
            params=self._params(closed="true"),
        )
        r.raise_for_status()
        return r.json()

    def update_card(self, card_id, **fields):
        """Update card fields (name, desc, etc.)."""
        r = requests.put(
            f"{self.BASE_URL}/cards/{card_id}",
            params=self._params(),
            data=fields,
        )
        r.raise_for_status()
        return r.json()

    def get_card_checklists(self, card_id):
        """Get all checklists on a card with items and states."""
        r = requests.get(
            f"{self.BASE_URL}/cards/{card_id}/checklists",
            params=self._params(),
        )
        r.raise_for_status()
        return r.json()

    def add_reaction(self, action_id, emoji_short_name):
        """Add an emoji reaction to a comment (action)."""
        r = requests.post(
            f"{self.BASE_URL}/actions/{action_id}/reactions",
            params=self._params(),
            json={"shortName": emoji_short_name},
        )
        r.raise_for_status()
        return r.json()

    def set_check_item_state(self, card_id, check_item_id, state="complete"):
        """Check or uncheck a checklist item. state: 'complete' or 'incomplete'."""
        r = requests.put(
            f"{self.BASE_URL}/cards/{card_id}/checkItem/{check_item_id}",
            params=self._params(state=state),
        )
        r.raise_for_status()
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


def read_board_context(trello):
    """Read full board context: all non-archived cards with their comments.

    Returns a formatted string with all cards and conversations,
    including which list each card belongs to, card IDs, and checklist items.
    """
    cards = trello.get_cards()

    # Build list ID → name mapping
    lists = trello.get_lists()
    list_names = {lst["id"]: lst["name"] for lst in lists}

    lines = []
    for card in cards:
        list_name = list_names.get(card.get("idList"), "Unknown")
        lines.append(f"### [{list_name}] {card['name']}  (card_id: {card['id']})")
        if card.get("desc"):
            lines.append(card["desc"][:500])

        # Include checklist items with IDs and states
        try:
            checklists = trello.get_card_checklists(card["id"])
            for cl in checklists:
                for item in cl.get("checkItems", []):
                    state = "x" if item.get("state") == "complete" else " "
                    lines.append(
                        f"- [{state}] {item['name']}  (item_id: {item['id']})"
                    )
        except Exception:
            pass  # Skip if checklists fail

        comments = trello.get_card_comments(card["id"])
        for comment in comments:
            ts = _utc_to_ct(comment.get("date", ""))
            text = comment.get("data", {}).get("text", "")
            role = _comment_role(text)
            lines.append(f"[{ts}] {role}: {text}")
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
        lines.append(card["desc"][:500])
    lines.append("")

    for comment in comments:
        ts = comment.get("date", "")[:16]
        text = comment.get("data", {}).get("text", "")
        role = _comment_role(text)
        lines.append(f"[{ts}] {role}: {text}")

    return "\n".join(lines)


# --- Claude ---


def _get_client():
    """Create an Anthropic client."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=api_key)


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
    return json.loads(content)


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


def generate_morning_cards(spec, board_context):
    """Generate daily exercise and nutrition cards."""
    client = _get_client()

    now_ct = datetime.now(tz=CT)
    day_of_week = now_ct.strftime("%A")
    date_str = now_ct.strftime("%B %d, %Y")
    time_str = now_ct.strftime("%-I:%M %p").lower()

    prompt = f"""You are a muscle growth coach managing a client's training via a Trello board.
Each morning you create cards for the day's training and nutrition.

Today is {day_of_week}, {date_str}. Current time: {time_str} Central.

## Board Structure
The board has three lists:
- **Exercise** — one card per workout (title, full routine in description, checklist of exercises)
- **Nutrition** — cards for meals, grocery runs, supplements (title, description, checklist of items)
- **Forum** — daily check-in card for open conversation throughout the day

## Client Spec (board description — your living knowledge about this client)
{spec or "(No spec yet — introduce yourself and ask about their goals in the exercise comment)"}

## Recent Board Activity (all cards and conversations)
{board_context or "(First interaction — no history yet)"}

## Your Task
Create today's exercise card, nutrition card, and a Forum check-in card. Consider:
- What day of the week it is (training day vs rest day per their schedule)
- What happened in recent conversations (soreness, PRs, skipped meals, injuries)
- Their current phase/goals from the spec
- Progressive overload: increment weights/volume based on recent performance
- Nutrition to support their training (meals, macros, grocery needs)

## Rules
- Be specific to THEIR program — exercises, weights, sets, reps, rest periods
- Include warm-up guidance if relevant
- The exercise comment should be conversational and specific (not generic motivation)
- Nutrition checklist items should be actionable (meals to eat, items to buy)
- If the spec is empty, introduce yourself and ask what they're working on
- On rest days, skip the exercise card (set to null) but still provide nutrition
- The forum card is a daily check-in — open-ended prompt for the client to message throughout the day
- The forum comment should be conversational: ask how they're feeling, follow up on yesterday, etc.
- Exercise or nutrition can be null if not applicable; always create a forum card
- The spec is your ONLY long-term memory — cards get archived. Update the spec with any plans, recommendations, or decisions you make in today's cards. Also prune stale info, compress daily details into trends, and remove completed one-off tasks.

## Board Actions
You can take actions on existing cards. Available actions:
- {{"action": "archive_card", "card_id": "..."}} — archive a card
- {{"action": "check_item", "card_id": "...", "item_id": "..."}} — check off a checklist item
- {{"action": "uncheck_item", "card_id": "...", "item_id": "..."}} — uncheck a checklist item
- {{"action": "move_card", "card_id": "...", "list": "Exercise|Nutrition|Forum"}} — move a card
- {{"action": "comment", "card_id": "...", "text": "..."}} — comment on an existing card
- {{"action": "update_card", "card_id": "...", "name": "...", "desc": "..."}} — update card name/desc (both optional)
- {{"action": "create_card", "list": "Exercise|Nutrition|Forum", "title": "...", "description": "...", "checklist": ["item1", "item2"], "comment": "..."}} — create a new card (description, checklist, comment are optional)

## Output
Respond with JSON only:
{{
    "exercise": {{
        "title": "{day_of_week}, {date_str} — [Focus Area]",
        "description": "full markdown workout description",
        "checklist": ["exercise checklist items like 'Bench Press 4x8 @ 185'"],
        "comment": "conversational coach message"
    }},
    "nutrition": {{
        "title": "{day_of_week}, {date_str} — Nutrition",
        "description": "meal plan / nutrition notes in markdown",
        "checklist": ["actionable items like 'Meal 1: Oatmeal + whey (40g protein)'"]
    }},
    "forum": {{
        "title": "{day_of_week}, {date_str} — Check In",
        "description": "",
        "comment": "conversational check-in message"
    }},
    "actions": [],
    "spec_update_instruction": null or "brief description of what to change in the spec"
}}
Either "exercise" or "nutrition" can be null if not applicable for today. Always include "forum".
"actions" is an array of board actions to take (can be empty).
"spec_update_instruction" is a brief description — NOT the full spec."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_json_response(response.content[0].text)


def generate_reply(spec, board_context, card_context, user_comment, card_name):
    """Process user comment and generate reply + optional spec updates."""
    client = _get_client()

    now_ct = datetime.now(tz=CT)
    day_of_week = now_ct.strftime("%A")
    time_str = now_ct.strftime("%-I:%M %p").lower()

    prompt = f"""You are a muscle growth coach communicating with your client via Trello card comments.
Your client just commented on a card. Read their message and respond helpfully.

Today is {day_of_week}. Current time: {time_str} Central.

## Board Structure
The board has three lists:
- **Exercise** — workout cards (one per session)
- **Nutrition** — meal plans, grocery lists, supplement tracking
- **Forum** — ongoing discussion topics

## Client Spec (board description)
{spec or "(No spec yet)"}

## Full Board Activity (all cards and conversations)
{board_context or "(No board history yet)"}

## Current Card Context (card: {card_name})
{card_context or "(No prior conversation on this card)"}

## New Comment from Client
{user_comment}

## Your Task
1. Understand what they're telling you or asking
2. Respond with helpful, specific coaching advice
3. Update the spec with anything that should be remembered

## Rules
- Be conversational but knowledgeable
- If they report a workout, acknowledge it specifically
- If they ask a question, give a direct answer then brief explanation
- If they share a concern (injury, plateau, motivation), address it with empathy and a plan

## CRITICAL: Spec Updates
The spec is your ONLY long-term memory. Cards get archived after you respond.
If it's not in the spec, you will forget it. You are responsible for maintaining
the spec like a real coach maintains client notes — add, refine, and prune.

**Add** new information:
- Recommendations you make (supplements, dosages, timing, form cues)
- Plans and decisions (training split, diet approach, schedule changes)
- Info they share (PRs, injuries, body weight, preferences, schedule)

**Refine** over time:
- Compress daily details into trends ("bench progressed 135→185 over 6 weeks")
- Replace outdated info rather than appending (new PR replaces old PR)
- Consolidate scattered notes into organized sections

**Prune** what's no longer useful:
- Completed one-off tasks (old grocery lists, past event prep)
- Superseded plans (old training split after switching to a new one)
- Day-level details once they've been captured as trends

Keep the spec concise and current — like a coach's notebook, not a log file.
When in doubt, update the spec. It's cheap and prevents losing context.

## Board Actions
You can take actions on existing cards or create new ones. Available actions:
- {{"action": "archive_card", "card_id": "..."}} — archive a card
- {{"action": "check_item", "card_id": "...", "item_id": "..."}} — check off a checklist item
- {{"action": "uncheck_item", "card_id": "...", "item_id": "..."}} — uncheck a checklist item
- {{"action": "move_card", "card_id": "...", "list": "Exercise|Nutrition|Forum"}} — move a card
- {{"action": "comment", "card_id": "...", "text": "..."}} — comment on an existing card
- {{"action": "update_card", "card_id": "...", "name": "...", "desc": "..."}} — update card name/desc (both optional)
- {{"action": "create_card", "list": "Exercise|Nutrition|Forum", "title": "...", "description": "...", "checklist": ["item1", "item2"], "comment": "..."}} — create a new card (description, checklist, comment are optional)

Use create_card when the client asks for a new workout plan, meal plan, grocery list, or any other card-worthy content.
For example, if they say "can you make me a leg day card?", create one on the Exercise list.

## Output
Respond with JSON only:
{{
    "reaction": "emoji shortname reacting to the client's message (e.g. muscle, fire, eyes, tada, heart, thumbsup, thinking_face, saluting_face, clap)",
    "message": "your reply comment text",
    "actions": [],
    "spec_update_instruction": null or "brief description of what to change in the spec"
}}
"reaction" is an emoji shortname — pick one that fits your reaction to what they said.
"actions" is an array of board actions to take (can be empty).
"spec_update_instruction" is a brief description — NOT the full spec."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_json_response(response.content[0].text)


# --- Handlers ---


def _execute_actions(trello, trace_id, actions):
    """Execute board actions returned by the coach."""
    for action in actions:
        action_type = action.get("action", "")
        try:
            if action_type == "archive_card":
                trello.archive_card(action["card_id"])
            elif action_type == "check_item":
                trello.set_check_item_state(action["card_id"], action["item_id"], "complete")
            elif action_type == "uncheck_item":
                trello.set_check_item_state(action["card_id"], action["item_id"], "incomplete")
            elif action_type == "move_card":
                list_id = trello.get_list_id(action["list"])
                trello.move_card(action["card_id"], list_id)
            elif action_type == "comment":
                trello.add_comment(action["card_id"], action["text"])
            elif action_type == "create_card":
                _create_card_with_checklist(trello, trace_id, action["list"], {
                    "title": action["title"],
                    "description": action.get("description", ""),
                    "checklist": action.get("checklist", []),
                    "comment": action.get("comment", ""),
                })
            elif action_type == "update_card":
                fields = {}
                if action.get("name"):
                    fields["name"] = action["name"]
                if action.get("desc"):
                    fields["desc"] = action["desc"]
                if fields:
                    trello.update_card(action["card_id"], **fields)
            else:
                logger.warning(f"[{trace_id}] Unknown action: {action_type}")
                continue
            log_structured(trace_id, f"action_{action_type}", metadata={
                "card_id": action.get("card_id", ""),
            })
        except Exception as e:
            logger.error(f"[{trace_id}] Action {action_type} failed: {e}")
            log_structured(trace_id, f"action_{action_type}", "failure", {
                "error": str(e),
            })


def _apply_spec_if_needed(trello, trace_id, spec, instruction):
    """Apply spec update instruction via Haiku if instruction is provided."""
    if not instruction:
        return
    try:
        updated_spec = apply_spec_update(spec, instruction)
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


def handle_morning(trace_id):
    """Generate and post daily exercise and nutrition cards."""
    try:
        trello = TrelloClient()

        spec = trello.get_board_desc()
        log_structured(trace_id, "read_spec", metadata={"length": len(spec)})

        board_context = read_board_context(trello)
        log_structured(trace_id, "read_context", metadata={"length": len(board_context)})

        result = generate_morning_cards(spec, board_context)

        log_structured(trace_id, "generate_morning", metadata={
            "has_exercise": result.get("exercise") is not None,
            "has_nutrition": result.get("nutrition") is not None,
            "has_forum": result.get("forum") is not None,
        })

        # Create exercise card
        if result.get("exercise"):
            _create_card_with_checklist(trello, trace_id, "Exercise", result["exercise"])

        # Create nutrition card
        if result.get("nutrition"):
            _create_card_with_checklist(trello, trace_id, "Nutrition", result["nutrition"])

        # Create forum check-in card
        if result.get("forum"):
            _create_card_with_checklist(trello, trace_id, "Forum", result["forum"])

        # Execute board actions
        _execute_actions(trello, trace_id, result.get("actions", []))

        # Update spec if needed
        _apply_spec_if_needed(trello, trace_id, spec, result.get("spec_update_instruction"))

    except Exception as e:
        logger.error(f"[{trace_id}] Morning handler failed: {e}", exc_info=True)
        log_structured(trace_id, "handle_morning", "failure", {"error": str(e)})
        sys.exit(1)


def handle_reply(trace_id):
    """Process user comment on a card and respond."""
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

        board_context = read_board_context(trello)
        card_context = read_card_context(trello, card_id)
        card = trello.get_card(card_id)
        card_name = card.get("name", "Unknown")
        log_structured(trace_id, "read_context", metadata={
            "card": card_name[:80],
            "board_length": len(board_context),
            "card_length": len(card_context),
        })

        result = generate_reply(spec, board_context, card_context, comment_text, card_name)

        reply_text = result["message"]
        log_structured(trace_id, "generate_reply", metadata={"length": len(reply_text)})

        # React to the user's comment with the coach's chosen emoji
        reaction = result.get("reaction", "")
        if action_id and reaction:
            try:
                trello.add_reaction(action_id, reaction)
            except Exception:
                pass  # Non-critical

        # Post reply
        trello.add_comment(card_id, reply_text)
        log_structured(trace_id, "add_comment", metadata={"length": len(reply_text)})

        # Execute board actions (skip comment actions on the reply card to avoid duplicates)
        actions = [
            a for a in result.get("actions", [])
            if not (a.get("action") == "comment" and a.get("card_id") == card_id)
        ]
        _execute_actions(trello, trace_id, actions)

        # Update spec if needed
        _apply_spec_if_needed(trello, trace_id, spec, result.get("spec_update_instruction"))

        # React ✅ on user's comment as the very last step — signals job complete
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
