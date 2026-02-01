#!/usr/bin/env python3
"""Check recent logs from Cloud Storage."""

import sys
from pathlib import Path

# Add src to path
src_path = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(src_path))

from gmail_ai_unsub.config import Config
from gmail_ai_unsub.dashboard.app import get_logs_from_storage

if __name__ == "__main__":
    config = Config()
    logs = get_logs_from_storage(config.cloud_storage_bucket, config.cloud_project_id, hours=24)
    
    print(f"Found {len(logs)} logs in last 24 hours")
    
    for i, log in enumerate(logs[:10], 1):
        timestamp = log.get("timestamp", "")[:19] if log.get("timestamp") else "unknown"
        stage = log.get("stage", "unknown")
        result = log.get("result", "unknown")
        service = log.get("service", "")
        
        service_str = f" [{service}]" if service else ""
        print(f"\n{i}. {timestamp}: {stage} - {result}{service_str}")
        
        metadata = log.get("metadata", {})
        if metadata.get("subject"):
            print(f"   Subject: {metadata['subject']}")
        if metadata.get("count"):
            print(f"   Found {metadata['count']} email(s)")
