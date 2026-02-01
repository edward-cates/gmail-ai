#!/usr/bin/env python3
"""Development utilities for gmail-ai-unsub.

Consolidates various dev scripts into a single CLI using Click.
"""

import sys
from pathlib import Path

import click

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from gmail_ai_unsub.config import Config
from gmail_ai_unsub.dashboard.app import app, get_logs_from_storage
from gmail_ai_unsub.gmail.client import GmailClient


@click.group()
def cli():
    """Development utilities for gmail-ai-unsub."""
    pass


@cli.command()
def validate_dashboard():
    """Validate dashboard functionality."""
    click.echo("Validating dashboard...")
    
    # Check config
    try:
        config = Config()
        click.echo(click.style("✓ Dashboard app loads successfully", fg="green"))
        click.echo(f"  Project: {config.cloud_project_id}")
        click.echo(f"  Bucket: {config.cloud_storage_bucket}")
    except Exception as e:
        click.echo(click.style(f"✗ Failed to load config: {e}", fg="red"))
        sys.exit(1)
    
    # Check Cloud Storage access
    try:
        logs = get_logs_from_storage(config.cloud_storage_bucket, config.cloud_project_id, hours=1)
        click.echo(click.style(f"✓ Can read from Cloud Storage: {len(logs)} logs in last hour", fg="green"))
        
        # Check for errors in recent logs
        errors = [log for log in logs if log.get("result") == "failure"]
        if errors:
            click.echo(click.style(f"⚠ Found {len(errors)} failed logs in last hour:", fg="yellow"))
            for err in errors[:3]:
                error_msg = err.get("metadata", {}).get("error", "Unknown error")
                click.echo(f"  - {err.get('timestamp', '')[:19]}: {error_msg[:100]}")
    except Exception as e:
        click.echo(click.style(f"✗ Cannot read from Cloud Storage: {e}", fg="red"))
        sys.exit(1)
    
    # Check FastAPI app
    try:
        routes = [route.path for route in app.routes]
        click.echo(click.style(f"✓ FastAPI app has {len(routes)} routes: {', '.join(routes)}", fg="green"))
    except Exception as e:
        click.echo(click.style(f"✗ Failed to inspect FastAPI app: {e}", fg="red"))
        sys.exit(1)
    
    click.echo(click.style("✓ Dashboard validation passed", fg="green"))


@cli.command()
def reset_gmail_watch():
    """Stop all Gmail Watch subscriptions and set up a fresh single one."""
    try:
        config = Config()
        client = GmailClient(
            credentials_file=config.gmail_credentials_file,
            token_file=config.gmail_token_file,
            use_default_credentials=True,
        )

        topic = config.cloud_pubsub_topic
        if not topic:
            click.echo(click.style("Error: cloud.pubsub_topic not set in config.toml", fg="red"))
            click.echo("Add to config.toml:")
            click.echo("  [cloud]")
            click.echo('  pubsub_topic = "projects/neat-simplicity-486023-a4/topics/gmail-watch"')
            sys.exit(1)

        click.echo("Resetting Gmail Watch...")
        click.echo("")

        # Step 1: Stop any existing watch
        click.echo("Step 1: Stopping any existing Gmail Watch...")
        try:
            client.service.users().stop(userId="me").execute()
            click.echo(click.style("✓ Stopped existing watch (if any)", fg="green"))
        except Exception as e:
            click.echo(f"  (No existing watch to stop, or error: {e})")
        click.echo("")

        # Step 2: Set up a fresh watch
        click.echo("Step 2: Setting up fresh Gmail Watch...")
        click.echo(f"  Topic: {topic}")
        click.echo("")

        watch_request = {
            "topicName": topic,
            "labelIds": ["INBOX"],
        }

        result = client.service.users().watch(userId="me", body=watch_request).execute()

        expiration = result.get("expiration")
        history_id = result.get("historyId")

        click.echo(click.style("✓ Gmail Watch reset successfully!", fg="green"))
        click.echo(f"  History ID: {history_id}")
        click.echo(f"  Expiration: {expiration} (Unix timestamp in milliseconds)")
        click.echo("")
        click.echo("You now have exactly ONE active Gmail Watch subscription.")
        click.echo("Note: Watch expires after 7 days. Re-run this command to renew it.")

    except Exception as e:
        click.echo(click.style(f"Error: {e}", fg="red"))
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    cli()
