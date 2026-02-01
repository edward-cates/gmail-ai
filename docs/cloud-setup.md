# Cloud Deployment Setup Guide

This guide walks you through setting up the complete cloud infrastructure for Gmail AI Unsubscribe Tool from scratch. This includes Cloud Functions, Cloud Run, Cloud Tasks, Pub/Sub, and all supporting services.

## Prerequisites

- **Google Cloud account** with billing enabled
- **gcloud CLI** installed and authenticated
- **Python 3.12+** and `uv` (or `pip`)
- **LLM API key** (Anthropic Claude recommended for classification)
- **OAuth credentials** for Gmail API (see [OAuth Setup](#step-2-oauth-credentials-setup) below)

## Step 1: Google Cloud Project Setup

### 1.1 Create a New Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Click "Select a project" → "New Project"
3. Enter a project name (e.g., "gmail-ai-unsub")
4. Note your **Project ID** (e.g., `gmail-ai-unsub-123456`)
5. Click "Create"

### 1.2 Get Your Project Number

You'll need your **Project Number** (different from Project ID) for OIDC authentication:

```bash
gcloud projects describe YOUR_PROJECT_ID --format="value(projectNumber)"
```

Save this number (e.g., `543519381062`) - you'll need it later.

### 1.3 Enable Required APIs

Enable all necessary APIs:

```bash
PROJECT_ID="your-project-id"

gcloud services enable \
  cloudfunctions.googleapis.com \
  cloudbuild.googleapis.com \
  eventarc.googleapis.com \
  run.googleapis.com \
  cloudtasks.googleapis.com \
  pubsub.googleapis.com \
  storage.googleapis.com \
  cloudscheduler.googleapis.com \
  gmail.googleapis.com \
  --project=$PROJECT_ID
```

### 1.4 Set Up Application Default Credentials

For local development and dashboard access:

```bash
gcloud auth application-default login --project=$PROJECT_ID
```

## Step 2: OAuth Credentials Setup

You need OAuth credentials to access Gmail API. Follow these steps:

### 2.1 Configure OAuth Consent Screen

1. Go to **APIs & Services** → **OAuth consent screen**
2. Choose **External** (unless you have Google Workspace)
3. Fill in:
   - **App name**: Gmail AI Unsubscribe Tool
   - **User support email**: Your email
   - **Developer contact**: Your email
4. Click **Save and Continue**
5. **Scopes**: Add these scopes:
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/gmail.modify`
   - `https://www.googleapis.com/auth/gmail.send`
6. Click **Save and Continue**
7. **Test users**: Add your Gmail address as a test user
8. Click **Save and Continue** → **Back to Dashboard**

### 2.2 Create OAuth 2.0 Credentials

1. Go to **APIs & Services** → **Credentials**
2. Click **Create Credentials** → **OAuth client ID**
3. Choose **Application type**: **Desktop app**
4. Name it (e.g., "Gmail AI Desktop Client")
5. Click **Create**
6. **Copy the Client ID and Client Secret** - you'll need these

### 2.3 Create .env File

Create a `.env` file in the project root:

```bash
# Gmail OAuth Credentials
GMAIL_CLIENT_ID=your-client-id.apps.googleusercontent.com
GMAIL_CLIENT_SECRET=your-client-secret

# Anthropic API Key (for email classification)
ANTHROPIC_API_KEY=your-anthropic-api-key

# Google Cloud Project
GMAIL_AI_PROJECT_ID=your-project-id
GMAIL_AI_PROJECT_NUMBER=your-project-number
GMAIL_AI_STORAGE_BUCKET=gmail-ai-logs
```

**Important**: Add `.env` to `.gitignore` (it should already be there).

## Step 3: Create Cloud Resources

### 3.1 Create Cloud Storage Bucket

```bash
PROJECT_ID="your-project-id"
BUCKET_NAME="gmail-ai-logs"

gcloud storage buckets create gs://$BUCKET_NAME \
  --location=us-central1 \
  --project=$PROJECT_ID
```

### 3.2 Create Pub/Sub Topic

```bash
PROJECT_ID="your-project-id"
TOPIC_NAME="gmail-watch"

gcloud pubsub topics create $TOPIC_NAME --project=$PROJECT_ID
```

### 3.3 Create Cloud Tasks Queue

```bash
PROJECT_ID="your-project-id"
QUEUE_NAME="email-processing"

gcloud tasks queues create $QUEUE_NAME \
  --location=us-central1 \
  --project=$PROJECT_ID
```

## Step 4: Local Configuration

### 4.1 Clone and Install

```bash
git clone https://github.com/yourusername/gmail-ai-unsub.git
cd gmail-ai-unsub
uv pip install -e .
```

### 4.2 Generate OAuth Token

Generate and save your Gmail OAuth token:

```bash
export $(cat .env | grep -v '^#' | xargs)
PYTHONPATH=src uv run python -c "
from gmail_ai_unsub.config import Config
from gmail_ai_unsub.gmail.auth import run_oauth_flow
config = Config()
run_oauth_flow(config.gmail_token_file)
"
```

This will open a browser for authentication. After completing, your token will be saved locally.

### 4.3 Create Config File

Create `config.toml` in your platform-specific config directory:

**macOS**: `~/Library/Application Support/gmail-ai-unsub/config.toml`  
**Linux**: `~/.config/gmail-ai-unsub/config.toml`  
**Windows**: `%LOCALAPPDATA%\gmail-ai-unsub\gmail-ai-unsub\config.toml`

Or create `./config.toml` in the project root:

```toml
[gmail]
# credentials_file = ""  # Leave empty to use .env variables

[llm]
provider = "anthropic"
model = "claude-4-5-opus"
api_key_env = "ANTHROPIC_API_KEY"

[cloud]
project_id = "your-project-id"
project_number = "your-project-number"  # e.g., "543519381062"
pubsub_topic = "projects/your-project-id/topics/gmail-watch"
storage_bucket = "gmail-ai-logs"
processing_label = "🤖"
tasks_queue = "email-processing"
tasks_location = "us-central1"
run_service = "email-processor"

[labels]
marketing = "Unsubscribe"
unsubscribed = "Unsubscribed"
failed = "Unsubscribe-Failed"
```

### 4.4 Upload Config and Token to Cloud Storage

```bash
PROJECT_ID="your-project-id"
BUCKET_NAME="gmail-ai-logs"

# Upload config (adjust path for your OS)
gsutil cp ~/Library/Application\ Support/gmail-ai-unsub/config.toml \
  gs://$BUCKET_NAME/config.toml

# Upload token
gsutil cp ~/Library/Application\ Support/gmail-ai-unsub/token.json \
  gs://$BUCKET_NAME/token.json
```

## Step 5: Deploy Cloud Function (Pub/Sub Handler)

The Cloud Function receives Gmail Watch notifications and creates Cloud Tasks.

### 5.1 Update Makefile (Optional)

If you want to use the Makefile, update the project ID in `Makefile`:

```makefile
PROJECT_ID := your-project-id
PROJECT_NUMBER := your-project-number
```

### 5.2 Deploy Using Makefile

```bash
export $(cat .env | grep -v '^#' | xargs)
make deploy-function
```

### 5.3 Or Deploy Manually

```bash
export $(cat .env | grep -v '^#' | xargs)

gcloud functions deploy gmail-processor \
  --gen2 \
  --runtime=python312 \
  --region=us-central1 \
  --source=. \
  --entry-point=handle_pubsub_event \
  --trigger-topic=gmail-watch \
  --timeout=60s \
  --memory=256Mi \
  --project=$PROJECT_ID \
  --set-env-vars="GMAIL_AI_STORAGE_BUCKET=$BUCKET_NAME,GMAIL_AI_PROJECT_ID=$PROJECT_ID,GMAIL_AI_PROJECT_NUMBER=$PROJECT_NUMBER"
```

**Important**: Make sure `GMAIL_AI_PROJECT_NUMBER` is set - this is required for OIDC authentication with Cloud Run.

## Step 6: Deploy Cloud Run Service (Email Processor)

The Cloud Run service processes individual emails and classifies them.

### 6.1 Deploy Using Makefile

```bash
export $(cat .env | grep -v '^#' | xargs)
make deploy-run-service
```

### 6.2 Or Deploy Manually

```bash
export $(cat .env | grep -v '^#' | xargs)

# Get the default compute service account
SERVICE_ACCOUNT=$(gcloud iam service-accounts list \
  --project=$PROJECT_ID \
  --filter="email:*-compute@developer.gserviceaccount.com" \
  --format="value(email)" | head -1)

gcloud run deploy email-processor \
  --source=. \
  --region=us-central1 \
  --platform=managed \
  --timeout=300s \
  --memory=1Gi \
  --cpu=1 \
  --no-allow-unauthenticated \
  --service-account="$SERVICE_ACCOUNT" \
  --set-env-vars="GMAIL_AI_STORAGE_BUCKET=$BUCKET_NAME,GMAIL_AI_PROJECT_ID=$PROJECT_ID,ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY,PYTHONPATH=/workspace/src" \
  --command="uvicorn" \
  --args="email_processor_main:app,--host,0.0.0.0,--port,8080" \
  --port=8080 \
  --project=$PROJECT_ID
```

**Note**: The `--command` and `--args` flags override the buildpack's auto-detection to use uvicorn instead of gunicorn.

## Step 7: Set Up Gmail Watch

Gmail Watch sends notifications to Pub/Sub when new emails arrive.

### 7.1 Set Up Watch

```bash
export $(cat .env | grep -v '^#' | xargs)
make setup-gmail-watch
```

### 7.2 Or Set Up Manually

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
print(f'  Expires: {result.get(\"expiration\")}')
"
```

**Important**: Gmail Watch expires after 7 days. Set up automatic renewal (Step 8).

## Step 8: Set Up Automatic Watch Renewal

### 8.1 Deploy Watch Renewal Function

```bash
export $(cat .env | grep -v '^#' | xargs)
make deploy-watch-renewal
```

### 8.2 Set Up Cloud Scheduler

```bash
export $(cat .env | grep -v '^#' | xargs)
make setup-scheduler
```

This creates a Cloud Scheduler job that runs every Sunday at 2 AM Pacific to renew the Gmail Watch.

### 8.3 Or Set Up Manually

```bash
PROJECT_ID="your-project-id"
SERVICE_URL=$(gcloud run services describe gmail-watch-renewal \
  --region=us-central1 \
  --project=$PROJECT_ID \
  --format="value(status.url)")

gcloud scheduler jobs create http gmail-watch-renewal \
  --location=us-central1 \
  --schedule="0 2 * * 0" \
  --time-zone="America/Los_Angeles" \
  --uri="$SERVICE_URL" \
  --http-method=GET \
  --oidc-service-account-email="$PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
  --project=$PROJECT_ID
```

## Step 9: Grant Required Permissions

Ensure the Cloud Function service account has necessary permissions:

```bash
PROJECT_ID="your-project-id"
PROJECT_NUMBER="your-project-number"

# Get the Cloud Function service account
FUNCTION_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

# Grant permissions
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${FUNCTION_SA}" \
  --role="roles/storage.objectAdmin" \
  --condition=None

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${FUNCTION_SA}" \
  --role="roles/cloudtasks.enqueuer" \
  --condition=None

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${FUNCTION_SA}" \
  --role="roles/run.invoker" \
  --condition=None
```

## Step 10: Verify Deployment

### 10.1 Check All Services

```bash
# Check Cloud Function
gcloud functions describe gmail-processor \
  --gen2 \
  --region=us-central1 \
  --project=$PROJECT_ID

# Check Cloud Run service
gcloud run services describe email-processor \
  --region=us-central1 \
  --project=$PROJECT_ID

# Check Cloud Tasks queue
gcloud tasks queues describe email-processing \
  --location=us-central1 \
  --project=$PROJECT_ID

# Check Pub/Sub topic
gcloud pubsub topics describe gmail-watch \
  --project=$PROJECT_ID
```

### 10.2 Test End-to-End

1. **Send yourself a test email** to your Gmail inbox
2. **Check Cloud Function logs**:
   ```bash
   gcloud functions logs read gmail-processor \
     --gen2 \
     --region=us-central1 \
     --limit=20 \
     --project=$PROJECT_ID
   ```
3. **Check Cloud Run logs**:
   ```bash
   gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=email-processor" \
     --limit=20 \
     --project=$PROJECT_ID
   ```
4. **Check Cloud Storage logs**:
   ```bash
   gsutil cat gs://$BUCKET_NAME/logs/$(date +%Y/%m/%d)/*.jsonl | tail -10
   ```

### 10.3 Run Dashboard Locally

View logs in a web interface:

```bash
make run-dashboard
# Or:
PYTHONPATH=src uv run python -m uvicorn gmail_ai_unsub.dashboard.app:app --reload --port 8080 --host 127.0.0.1
```

Visit http://127.0.0.1:8080

## Troubleshooting

### Cloud Function Not Triggered

- **Check Gmail Watch is active**: Run `make reset-gmail-watch` or check watch status
- **Verify Pub/Sub topic**: Ensure topic exists and Cloud Function is subscribed
- **Check function logs**: Look for errors in Cloud Function logs

### Cloud Run Service Returns 500

- **Check service logs**: Look for FastAPI/uvicorn errors
- **Verify environment variables**: Ensure `ANTHROPIC_API_KEY` is set
- **Check service account permissions**: Ensure it can read from Cloud Storage
- **Verify deployment**: Check that uvicorn command is being used (not gunicorn)

### Tasks Not Being Created

- **Check project number**: Ensure `GMAIL_AI_PROJECT_NUMBER` is set correctly
- **Verify OIDC authentication**: Check service account email format
- **Check Cloud Tasks permissions**: Ensure Cloud Function service account has `roles/cloudtasks.enqueuer`

### OAuth Token Issues

- **Token expired**: Re-run OAuth flow and upload new token to Cloud Storage
- **Wrong credentials**: Ensure `.env` file has correct `GMAIL_CLIENT_ID` and `GMAIL_CLIENT_SECRET`
- **Test user not added**: Add your email as a test user in OAuth consent screen

### Buildpack Auto-Detection Issues

If Cloud Run keeps trying to use gunicorn instead of uvicorn:

1. **Check Procfile exists**: Should contain `web: uvicorn email_processor_main:app --host 0.0.0.0 --port 8080`
2. **Use explicit command**: The Makefile uses `--command="uvicorn"` to override buildpack
3. **Remove Flask from requirements.txt**: If not needed, but it's required for Cloud Function in `main.py`

## Environment Variables Reference

### Cloud Function

- `GMAIL_AI_STORAGE_BUCKET` - Cloud Storage bucket name
- `GMAIL_AI_PROJECT_ID` - Google Cloud project ID
- `GMAIL_AI_PROJECT_NUMBER` - Google Cloud project number (for OIDC)

### Cloud Run Service

- `GMAIL_AI_STORAGE_BUCKET` - Cloud Storage bucket name
- `GMAIL_AI_PROJECT_ID` - Google Cloud project ID
- `ANTHROPIC_API_KEY` - Anthropic API key for classification
- `PYTHONPATH` - Set to `/workspace/src` for module imports

## Next Steps

- **Monitor logs**: Set up log-based alerts for errors
- **Set up monitoring**: Use Cloud Monitoring to track function invocations and errors
- **Optimize costs**: Review Cloud Functions and Cloud Run usage
- **Scale**: Adjust memory/CPU based on workload

## Cost Estimation

Approximate monthly costs (varies by usage):

- **Cloud Functions**: ~$0.40 per million invocations
- **Cloud Run**: ~$0.10 per million requests + compute time
- **Cloud Tasks**: Free tier: 1M operations/month
- **Cloud Storage**: ~$0.020 per GB/month
- **Pub/Sub**: Free tier: 10GB/month

For typical personal use (hundreds of emails/day), expect **<$5/month**.

## Security Best Practices

1. **Never commit `.env` file** - Already in `.gitignore`
2. **Rotate API keys regularly**
3. **Use least-privilege IAM roles**
4. **Enable audit logging** for Cloud Functions and Cloud Run
5. **Review OAuth token permissions** - Only grant necessary scopes
6. **Monitor for unauthorized access** - Set up alerts

## Support

For issues or questions:
- Check [ARCHITECTURE.md](../ARCHITECTURE.md) for system design
- Review [TROUBLESHOOTING.md](../TROUBLESHOOTING.md) (if exists)
- Open an issue on GitHub
