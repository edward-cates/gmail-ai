# Deployment Guide for Stage 1

This guide walks through deploying the minimal Pub/Sub integration.

## Prerequisites

- Google Cloud project: `neat-simplicity-486023-a4`
- Pub/Sub topic: `gmail-watch` (already exists)
- `gcloud` CLI installed and authenticated
- OAuth credentials set up (see OAuth Setup below)

## Step 1: Set Up OAuth Credentials

1. Create OAuth 2.0 credentials in Google Cloud Console:
   - Go to: https://console.cloud.google.com/apis/credentials?project=neat-simplicity-486023-a4
   - Click "Create Credentials" → "OAuth client ID"
   - Application type: "Desktop app"
   - Name it (e.g., "Gmail AI Unsub")
   - Copy the Client ID and Client Secret

2. Create `.env` file in project root:
   ```bash
   GMAIL_CLIENT_ID=your-client-id.apps.googleusercontent.com
   GMAIL_CLIENT_SECRET=your-client-secret
   ```

3. Generate OAuth token:
   ```bash
   export $(cat .env | grep -v '^#' | xargs)
   PYTHONPATH=src uv run python -c "
   from gmail_ai_unsub.config import Config
   from gmail_ai_unsub.gmail.auth import run_oauth_flow
   config = Config()
   run_oauth_flow(config.gmail_token_file)
   "
   ```

## Step 2: Set Up Application Default Credentials

For the dashboard to read from Cloud Storage:

```bash
gcloud auth application-default login --project=neat-simplicity-486023-a4
```

## Step 3: Create Cloud Storage Bucket

```bash
gcloud storage buckets create gs://gmail-ai-logs --location=us-central1 --project=neat-simplicity-486023-a4
```

## Step 4: Upload Files to Cloud Storage

Upload config and token:

```bash
# Upload config
gsutil cp "/Users/edward/Library/Application Support/gmail-ai-unsub/config.toml" gs://gmail-ai-logs/config.toml

# Upload token
gsutil cp "/Users/edward/Library/Application Support/gmail-ai-unsub/token.json" gs://gmail-ai-logs/token.json
```

## Step 5: Update Config

Ensure your `config.toml` has:

```toml
[cloud]
project_id = "neat-simplicity-486023-a4"
pubsub_topic = "projects/neat-simplicity-486023-a4/topics/gmail-watch"
storage_bucket = "gmail-ai-logs"
```

## Step 6: Enable Required APIs

```bash
gcloud services enable cloudfunctions.googleapis.com \
  cloudbuild.googleapis.com \
  eventarc.googleapis.com \
  --project=neat-simplicity-486023-a4
```

## Step 7: Deploy Cloud Function

```bash
gcloud functions deploy gmail-processor \
  --gen2 \
  --runtime=python312 \
  --region=us-central1 \
  --source=. \
  --entry-point=handle_pubsub_event \
  --trigger-topic=gmail-watch \
  --timeout=60s \
  --memory=256Mi \
  --project=neat-simplicity-486023-a4 \
  --set-env-vars="GMAIL_AI_STORAGE_BUCKET=gmail-ai-logs,GMAIL_AI_PROJECT_ID=neat-simplicity-486023-a4"
```

**Note**: The function automatically:
- Loads config from `gs://gmail-ai-logs/config.toml`
- Loads token from `gs://gmail-ai-logs/token.json`
- Uses embedded OAuth credentials from environment variables (if `.env` is set)

## Step 8: Set Up Gmail Watch

Gmail Watch needs to be activated to send notifications:

```bash
make setup-gmail-watch
```

Or manually:
```bash
export $(cat .env | grep -v '^#' | xargs)
PYTHONPATH=src uv run python -c "
from gmail_ai_unsub.config import Config
from gmail_ai_unsub.gmail.client import GmailClient
config = Config()
client = GmailClient(
    credentials_file=None,
    token_file=config.gmail_token_file,
    use_default_credentials=True,
)
topic = config.cloud_pubsub_topic
watch_request = {
    'topicName': topic,
    'labelIds': ['INBOX'],
}
result = client.service.users().watch(userId='me', body=watch_request).execute()
print(f'✓ Gmail Watch active! History ID: {result.get(\"historyId\")}')
"
```

**Note**: Gmail Watch expires after 7 days. Re-run this step to renew it.

## Step 9: Run Dashboard Locally

```bash
make run-dashboard
# Or:
PYTHONPATH=src uv run python -m uvicorn gmail_ai_unsub.dashboard.app:app --reload --port 8080 --host 127.0.0.1
```

Visit http://127.0.0.1:8080

## Testing

1. Send yourself a test email
2. Check Cloud Function logs: `gcloud functions logs read gmail-processor --gen2 --region=us-central1 --limit=20`
3. Check dashboard: http://127.0.0.1:8080
4. Verify logs in Cloud Storage: `gsutil cat gs://gmail-ai-logs/logs/2026/02/01/log.jsonl | tail -5`

## Validation

Run validation to check everything is working:

```bash
make validate-dashboard
```

## Troubleshooting

- **OAuth "deleted_client" error**: Token was created with wrong credentials. Regenerate token with correct credentials.
- **Dashboard can't read logs**: Run `gcloud auth application-default login`
- **Function not triggered**: Make sure Gmail Watch is set up (Step 8)
- **No email details in logs**: Check function logs for errors, verify token is uploaded to Cloud Storage
- **Permission errors**: Ensure the Cloud Function service account has permissions for:
  - Cloud Storage (to write logs)
  - Gmail API (to read emails)
  - Pub/Sub (to receive messages)
