#!/bin/bash
# Upload Gmail OAuth token to Cloud Storage for Cloud Functions

set -e

PROJECT_ID="neat-simplicity-486023-a4"
BUCKET_NAME="gmail-ai-logs"
TOKEN_FILE="${1:-$HOME/Library/Application Support/gmail-ai-unsub/token.json}"

if [ ! -f "$TOKEN_FILE" ]; then
    echo "Error: Token file not found at $TOKEN_FILE"
    echo "Usage: $0 [path/to/token.json]"
    echo ""
    echo "Default location: ~/Library/Application Support/gmail-ai-unsub/token.json"
    exit 1
fi

echo "Uploading token file to Cloud Storage..."
echo "Project: $PROJECT_ID"
echo "Bucket: $BUCKET_NAME"
echo "Token file: $TOKEN_FILE"
echo ""

# Upload to Cloud Storage
gsutil cp "$TOKEN_FILE" "gs://$BUCKET_NAME/token.json"

echo ""
echo "✓ Token uploaded successfully!"
echo ""
echo "The Cloud Function will load the token from: gs://$BUCKET_NAME/token.json"
echo ""
echo "Note: Make sure the Cloud Function service account has read access to the bucket."
