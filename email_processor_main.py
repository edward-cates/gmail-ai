"""Cloud Run entry point for email processor service."""

import sys
from pathlib import Path

# Add src to Python path
src_path = Path(__file__).parent / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

from gmail_ai_unsub.cloud.email_processor import app

__all__ = ["app"]
