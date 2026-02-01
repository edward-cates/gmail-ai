#!/usr/bin/env python3
"""Set up Gmail Watch to send notifications to Pub/Sub topic."""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from gmail_ai_unsub.config import Config
from gmail_ai_unsub.gmail.client import GmailClient


def main():
    """Set up Gmail Watch."""
    try:
        config = Config()
        client = GmailClient(
            credentials_file=config.gmail_credentials_file,
            token_file=config.gmail_token_file,
            use_default_credentials=True,
        )
        
        # Get Pub/Sub topic from config
        topic = config.cloud_pubsub_topic
        if not topic:
            print("Error: cloud.pubsub_topic not set in config.toml")
            print("Add to config.toml:")
            print("  [cloud]")
            print("  pubsub_topic = \"projects/neat-simplicity-486023-a4/topics/gmail-watch\"")
            sys.exit(1)
        
        print(f"Setting up Gmail Watch to send notifications to: {topic}")
        print("This will watch for new emails and send notifications to Pub/Sub.")
        print("")
        
        # Call Gmail API watch() method
        # Watch expires after 7 days, so you'll need to renew it periodically
        watch_request = {
            "topicName": topic,
            "labelIds": ["INBOX"],  # Watch for emails in INBOX
        }
        
        result = (
            client.service.users()
            .watch(userId="me", body=watch_request)
            .execute()
        )
        
        expiration = result.get("expiration")
        history_id = result.get("historyId")
        
        print("✓ Gmail Watch set up successfully!")
        print(f"  History ID: {history_id}")
        print(f"  Expiration: {expiration} (Unix timestamp in milliseconds)")
        print("")
        print("Gmail will now send notifications to your Pub/Sub topic when new emails arrive.")
        print("Note: Watch expires after 7 days. You'll need to run this script again to renew it.")
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
