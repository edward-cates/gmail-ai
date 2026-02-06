#!/usr/bin/env bash
# Sync an env var from .env to Google Cloud Secret Manager.
#
# Usage:
#   ./scripts/sync_secret.sh ENV_VAR_NAME secret-name
#
# Examples:
#   ./scripts/sync_secret.sh ANTHROPIC_API_KEY anthropic-api-key
#   ./scripts/sync_secret.sh SLACK_BOT_TOKEN slack-bot-token
#   ./scripts/sync_secret.sh SLACK_SIGNING_SECRET slack-signing-secret
#   ./scripts/sync_secret.sh TRELLO_API_KEY trello-api-key
#   ./scripts/sync_secret.sh TRELLO_TOKEN trello-token

set -euo pipefail

PROJECT_ID="neat-simplicity-486023-a4"
ENV_FILE=".env"

if [ $# -ne 2 ]; then
    echo "Usage: $0 ENV_VAR_NAME secret-name"
    echo ""
    echo "Reads ENV_VAR_NAME from .env and creates/updates the secret in GCP Secret Manager."
    exit 1
fi

ENV_VAR="$1"
SECRET_NAME="$2"

# Read value from .env
VALUE=$(grep "^${ENV_VAR}=" "$ENV_FILE" | head -1 | cut -d'=' -f2-)

if [ -z "$VALUE" ]; then
    echo "Error: ${ENV_VAR} not found in ${ENV_FILE}"
    exit 1
fi

echo "Syncing ${ENV_VAR} → secret/${SECRET_NAME}"

# Check if secret exists
if gcloud secrets describe "$SECRET_NAME" --project="$PROJECT_ID" &>/dev/null; then
    # Add new version
    echo -n "$VALUE" | gcloud secrets versions add "$SECRET_NAME" \
        --data-file=- --project="$PROJECT_ID"
    echo "✓ Updated secret '${SECRET_NAME}' with new version"
else
    # Create secret
    echo -n "$VALUE" | gcloud secrets create "$SECRET_NAME" \
        --data-file=- --project="$PROJECT_ID" --replication-policy="automatic"
    echo "✓ Created secret '${SECRET_NAME}'"
fi
