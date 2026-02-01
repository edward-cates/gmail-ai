"""Cloud Run entry point for email processor service."""

from src.gmail_ai_unsub.cloud.email_processor import app

# Expose app for Cloud Run
__all__ = ["app"]
