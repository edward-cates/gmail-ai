"""Gmail API client - standalone, no external deps from this repo."""

import os
import tempfile
import time
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.cloud import storage
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Gmail API scopes
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


def get_token_from_storage() -> str | None:
    """Download token.json from Cloud Storage."""
    bucket_name = os.getenv("GMAIL_AI_STORAGE_BUCKET")
    project_id = os.getenv("GMAIL_AI_PROJECT_ID")

    if not bucket_name or not project_id:
        return None

    try:
        client = storage.Client(project=project_id)
        bucket = client.bucket(bucket_name)
        blob = bucket.blob("token.json")

        if blob.exists():
            temp_dir = tempfile.gettempdir()
            temp_file = os.path.join(temp_dir, "gmail_token.json")
            blob.download_to_filename(temp_file)
            return temp_file
    except Exception:
        pass

    return None


def get_credentials(token_file: str | None = None) -> Credentials:
    """Get valid OAuth2 credentials for Gmail API."""
    # Try Cloud Storage first
    if token_file is None:
        token_file = get_token_from_storage()

    if not token_file:
        raise ValueError("No token file found. Upload token.json to Cloud Storage.")

    token_path = Path(token_file)
    if not token_path.exists():
        raise FileNotFoundError(f"Token file not found: {token_file}")

    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise ValueError("Credentials expired and cannot be refreshed")

    return creds


class GmailClient:
    """Gmail API client with retry logic."""

    def __init__(self, token_file: str | None = None, max_retries: int = 3):
        creds = get_credentials(token_file)
        self.service = build("gmail", "v1", credentials=creds)
        self.max_retries = max_retries

    def _execute_with_retry(self, request: Any, operation: str = "API call") -> Any:
        """Execute API request with exponential backoff."""
        for attempt in range(self.max_retries):
            try:
                return request.execute()
            except HttpError as error:
                if error.resp.status == 429:
                    if attempt < self.max_retries - 1:
                        wait_time = 2 ** attempt
                        time.sleep(wait_time)
                        continue
                raise
        raise RuntimeError(f"{operation} failed")

    def list_messages(self, query: str = "", max_results: int = 100, page_token: str | None = None) -> dict:
        """List messages matching query."""
        request = self.service.users().messages().list(
            userId="me", q=query, maxResults=max_results, pageToken=page_token
        )
        return self._execute_with_retry(request, "List messages")

    def get_message(self, message_id: str, format: str = "full") -> dict:
        """Get a message by ID."""
        request = self.service.users().messages().get(
            userId="me", id=message_id, format=format
        )
        return self._execute_with_retry(request, f"Get message {message_id}")

    def get_message_metadata(self, message_id: str) -> dict:
        """Get message metadata only (quota efficient)."""
        return self.get_message(message_id, format="metadata")


class LabelManager:
    """Gmail label management."""

    def __init__(self, service: Any):
        self.service = service

    def get_or_create_label(self, label_name: str) -> str:
        """Get existing label ID or create new label."""
        # Check if exists
        results = self.service.users().labels().list(userId="me").execute()
        for label in results.get("labels", []):
            if label.get("name") == label_name:
                return label["id"]

        # Create it
        try:
            label_obj = {
                "name": label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            }
            created = self.service.users().labels().create(userId="me", body=label_obj).execute()
            return created["id"]
        except HttpError as e:
            if e.resp.status == 409:
                # Race condition - try to find it again
                results = self.service.users().labels().list(userId="me").execute()
                for label in results.get("labels", []):
                    if label.get("name", "").lower() == label_name.lower():
                        return label["id"]
            raise

    def apply_label(self, message_id: str, label_id: str) -> None:
        """Apply label to a message."""
        self.service.users().messages().modify(
            userId="me", id=message_id, body={"addLabelIds": [label_id]}
        ).execute()
