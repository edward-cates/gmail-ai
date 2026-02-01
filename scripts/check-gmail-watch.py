#!/usr/bin/env python3
"""Check Gmail Watch status and Pub/Sub subscriptions."""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from gmail_ai_unsub.config import Config


def main():
    """Check Gmail Watch and Pub/Sub setup."""
    config = Config()
    project_id = config.cloud_project_id
    topic = config.cloud_pubsub_topic

    print("Checking Gmail Watch and Pub/Sub setup...")
    print("")

    # Check Pub/Sub topic
    print(f"Pub/Sub Topic: {topic}")
    print(f"Project ID: {project_id}")
    print("")

    # Note about Gmail Watch
    print("⚠️  Gmail API Limitation:")
    print("   Gmail API does not provide a way to list active watch() subscriptions.")
    print("   If you see duplicate Pub/Sub events, you may have multiple watch() calls active.")
    print("")
    print("To check for duplicate watches:")
    print("  1. Check Pub/Sub subscriptions (should only be one):")
    print(f"     gcloud pubsub subscriptions list --project={project_id}")
    print("")
    print("  2. If you see duplicate events, stop all watches and re-setup:")
    print("     - Gmail Watch expires after 7 days anyway")
    print("     - Or manually stop via Gmail API stop() method")
    print("")
    print("  3. Re-setup a single watch:")
    print("     make setup-gmail-watch")
    print("")

    # Check Pub/Sub subscriptions via gcloud
    import subprocess

    try:
        result = subprocess.run(
            ["gcloud", "pubsub", "subscriptions", "list", f"--project={project_id}"],
            capture_output=True,
            text=True,
            check=True,
        )
        subscriptions = [line for line in result.stdout.split("\n") if "gmail" in line.lower()]
        print(f"Found {len(subscriptions)} Pub/Sub subscription(s) related to Gmail:")
        for sub in subscriptions:
            print(f"  - {sub}")
    except subprocess.CalledProcessError:
        print("Could not check Pub/Sub subscriptions (gcloud not available or not authenticated)")
    except FileNotFoundError:
        print("Could not check Pub/Sub subscriptions (gcloud CLI not found)")


if __name__ == "__main__":
    main()
