#!/usr/bin/env bash
# Update the Slack batch processing interval.
#
# Usage:
#   ./scripts/set_batch_interval.sh MINUTES
#
# Examples:
#   ./scripts/set_batch_interval.sh 5
#   ./scripts/set_batch_interval.sh 15

set -euo pipefail

PROJECT_ID="neat-simplicity-486023-a4"
REGION="us-central1"
JOB_NAME="slack-batch-processor"

if [ -z "${1:-}" ]; then
    echo "Usage: $0 MINUTES"
    echo ""
    # Show current schedule
    CURRENT=$(gcloud scheduler jobs describe "$JOB_NAME" \
        --location="$REGION" --project="$PROJECT_ID" \
        --format="value(schedule)" 2>/dev/null || echo "unknown")
    echo "Current schedule: $CURRENT"
    exit 1
fi

MINUTES="$1"
SCHEDULE="*/${MINUTES} * * * *"

echo "Updating batch interval to every ${MINUTES} minutes..."
gcloud scheduler jobs update http "$JOB_NAME" \
    --location="$REGION" \
    --schedule="$SCHEDULE" \
    --project="$PROJECT_ID"

echo "Done. Schedule: $SCHEDULE"
