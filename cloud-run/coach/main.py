"""Muscle Growth Coach - Cloud Run Job.

Trello-based coaching agent. Two modes:
- morning: Read board (spec + card history), generate daily regimen card
- reply: Read board desc + card comments, respond to user's comment

Reads COACH_MODE from environment:
- "morning": Create daily regimen card with exercises + checklist
- "reply": Process user comment on a card and respond
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests
from langchain_anthropic import ChatAnthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-6"
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
        self._member_id = None

    def _params(self, **extra):
        return {"key": self.api_key, "token": self.token, **extra}

    def get_my_member_id(self):
        """Get the authenticated member's ID (cached)."""
        if self._member_id:
            return self._member_id
        r = requests.get(
            f"{self.BASE_URL}/members/me",
            params=self._params(fields="id"),
        )
        r.raise_for_status()
        self._member_id = r.json()["id"]
        return self._member_id

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
            params=self._params(desc=desc),
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
        params = self._params(idList=list_id, name=name, desc=desc, pos="top")
        r = requests.post(f"{self.BASE_URL}/cards", params=params)
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
        """Add a comment to a card."""
        r = requests.post(
            f"{self.BASE_URL}/cards/{card_id}/actions/comments",
            params=self._params(text=text),
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


# --- Context Helpers ---


def read_board_context(trello):
    """Read full board context: all non-archived cards with their comments.

    Returns a formatted string with all cards and conversations.
    """
    cards = trello.get_cards()
    coach_id = trello.get_my_member_id()

    lines = []
    for card in cards:
        lines.append(f"### Card: {card['name']}")
        if card.get("desc"):
            lines.append(card["desc"][:500])

        comments = trello.get_card_comments(card["id"])
        for comment in comments:
            author = comment.get("memberCreator", {}).get("fullName", "Unknown")
            role = "Coach" if comment["memberCreator"]["id"] == coach_id else "Client"
            ts = comment.get("date", "")[:16]
            text = comment.get("data", {}).get("text", "")
            lines.append(f"[{ts}] {role} ({author}): {text}")
        lines.append("")

    return "\n".join(lines)


def read_card_context(trello, card_id):
    """Read all comments on a specific card.

    Returns a formatted string with the card name and all comments.
    """
    card = trello.get_card(card_id)
    coach_id = trello.get_my_member_id()
    comments = trello.get_card_comments(card_id)

    lines = [f"### Card: {card['name']}"]
    if card.get("desc"):
        lines.append(card["desc"][:500])
    lines.append("")

    for comment in comments:
        author = comment.get("memberCreator", {}).get("fullName", "Unknown")
        role = "Coach" if comment["memberCreator"]["id"] == coach_id else "Client"
        ts = comment.get("date", "")[:16]
        text = comment.get("data", {}).get("text", "")
        lines.append(f"[{ts}] {role} ({author}): {text}")

    return "\n".join(lines)


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
    return json.loads(content)


def generate_morning_regimen(spec, board_context):
    """Generate the daily morning regimen card."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    llm = ChatAnthropic(model=MODEL, api_key=api_key, max_tokens=4000)

    now_ct = datetime.now(tz=CT)
    day_of_week = now_ct.strftime("%A")
    date_str = now_ct.strftime("%B %d, %Y")

    prompt = f"""You are a muscle growth coach managing a client's training via a Trello board.
Each morning you create a card with the day's regimen.

Today is {day_of_week}, {date_str}.

## Client Spec (board description — your living knowledge about this client)
{spec or "(No spec yet — introduce yourself and ask about their goals in the coach_message)"}

## Recent Board Activity (all cards and conversations)
{board_context or "(First interaction — no history yet)"}

## Your Task
Create today's training card. Consider:
- What day of the week it is (training day vs rest day per their schedule)
- What happened in recent conversations (soreness, PRs, skipped meals, injuries)
- Their current phase/goals from the spec
- Progressive overload: increment weights/volume based on recent performance
- If it's a rest day, create a recovery/nutrition-focused card instead

## Rules
- Be specific to THEIR program — exercises, weights, sets, reps, rest periods
- Include warm-up guidance if relevant
- The coach_message should be conversational and specific (not generic motivation)
- If the spec is empty, introduce yourself and ask what they're working on
- exercises array should be short labels for the checklist (e.g. "Bench Press 4x8 @ 185")

## Output
Respond with JSON only:
{{
    "card_title": "{day_of_week}, {date_str} — [Focus Area]",
    "regimen": "full markdown workout description for the card body",
    "exercises": ["exercise checklist items"],
    "coach_message": "motivational/contextual comment to post on the card",
    "spec_updates": null or "the COMPLETE updated spec markdown if anything needs changing"
}}"""

    response = llm.invoke(prompt)
    return _parse_json_response(response.content)


def generate_reply(spec, card_context, user_comment, card_name):
    """Process user comment and generate reply + optional spec updates."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    llm = ChatAnthropic(model=MODEL, api_key=api_key, max_tokens=4000)

    now_ct = datetime.now(tz=CT)
    day_of_week = now_ct.strftime("%A")

    prompt = f"""You are a muscle growth coach communicating with your client via Trello card comments.
Your client just commented on a card. Read their message and respond helpfully.

Today is {day_of_week}.

## Client Spec (board description)
{spec or "(No spec yet)"}

## Card Context (card: {card_name})
{card_context or "(No prior conversation on this card)"}

## New Comment from Client
{user_comment}

## Your Task
1. Understand what they're telling you or asking
2. Respond with helpful, specific coaching advice
3. If they shared new information (a PR, body weight update, schedule change,
   dietary info, injury), update the spec to reflect it

## Rules
- Be conversational but knowledgeable
- If they report a workout, acknowledge it specifically
- If they ask a question, give a direct answer then brief explanation
- If they share a concern (injury, plateau, motivation), address it with empathy and a plan
- Update the spec with ANY new information they share — this is your memory
- No length constraints — be as detailed as needed

## Output
Respond with JSON only:
{{
    "message": "your reply comment text",
    "spec_updates": null or "the COMPLETE updated spec markdown (not a diff)"
}}"""

    response = llm.invoke(prompt)
    return _parse_json_response(response.content)


# --- Handlers ---


def handle_morning(trace_id):
    """Generate and post the daily morning regimen card."""
    trello = TrelloClient()

    spec = trello.get_board_desc()
    log_structured(trace_id, "read_spec", metadata={"length": len(spec)})

    board_context = read_board_context(trello)
    log_structured(trace_id, "read_context", metadata={"length": len(board_context)})

    # Move any Active cards to Log
    try:
        active_list_id = trello.get_list_id("Active")
        log_list_id = trello.get_list_id("Log")
        cards = trello.get_cards()
        for card in cards:
            if card.get("idList") == active_list_id:
                trello.move_card(card["id"], log_list_id)
                log_structured(trace_id, "archive_card", metadata={"card": card["name"]})
    except ValueError:
        # Lists don't exist yet — will be handled by card creation
        active_list_id = None
        log_list_id = None

    # Generate regimen
    try:
        result = generate_morning_regimen(spec, board_context)
    except Exception as e:
        logger.error(f"[{trace_id}] Morning generation failed: {e}", exc_info=True)
        log_structured(trace_id, "generate_morning", "failure", {"error": str(e)})
        sys.exit(1)

    log_structured(trace_id, "generate_morning", metadata={
        "title": result.get("card_title", "")[:80],
    })

    # Create card
    if not active_list_id:
        active_list_id = trello.get_list_id("Active")

    card = trello.create_card(active_list_id, result["card_title"], result.get("regimen", ""))
    log_structured(trace_id, "create_card", metadata={"card_id": card["id"]})

    # Create exercise checklist
    exercises = result.get("exercises", [])
    if exercises:
        checklist = trello.create_checklist(card["id"])
        for exercise in exercises:
            trello.add_checklist_item(checklist["id"], exercise)
        log_structured(trace_id, "create_checklist", metadata={"items": len(exercises)})

    # Post coach comment
    coach_message = result.get("coach_message", "")
    if coach_message:
        trello.add_comment(card["id"], coach_message)
        log_structured(trace_id, "add_comment", metadata={"length": len(coach_message)})

    # Update spec if needed
    if result.get("spec_updates"):
        trello.update_board_desc(result["spec_updates"])
        log_structured(trace_id, "spec_updated")


def handle_reply(trace_id):
    """Process user comment on a card and respond."""
    comment_text = os.getenv("COMMENT_TEXT", "")
    card_id = os.getenv("CARD_ID", "")

    if not comment_text or not card_id:
        log_structured(trace_id, "skip", metadata={"reason": "missing comment_text or card_id"})
        return

    trello = TrelloClient()

    spec = trello.get_board_desc()
    log_structured(trace_id, "read_spec", metadata={"length": len(spec)})

    card_context = read_card_context(trello, card_id)
    card = trello.get_card(card_id)
    card_name = card.get("name", "Unknown")
    log_structured(trace_id, "read_context", metadata={
        "card": card_name[:80],
        "length": len(card_context),
    })

    # Generate reply
    try:
        result = generate_reply(spec, card_context, comment_text, card_name)
    except Exception as e:
        logger.error(f"[{trace_id}] Reply generation failed: {e}", exc_info=True)
        log_structured(trace_id, "generate_reply", "failure", {"error": str(e)})
        sys.exit(1)

    reply_text = result["message"]
    log_structured(trace_id, "generate_reply", metadata={"length": len(reply_text)})

    # Post reply
    trello.add_comment(card_id, reply_text)
    log_structured(trace_id, "add_comment", metadata={"length": len(reply_text)})

    # Update spec if needed
    if result.get("spec_updates"):
        trello.update_board_desc(result["spec_updates"])
        log_structured(trace_id, "spec_updated")


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
