# AGENTS.md

AI-readable project documentation.

## Project Purpose

Cloud-based email processing system that uses Claude to classify Gmail messages and take automated actions.

## Architecture

```
Gmail Watch → Pub/Sub → Cloud Function → Cloud Run Job
                                              ↓
                                    Classify → Act → Log
```

All code is standalone with no cross-imports between top-level directories:

```
/
├── main.py              # Cloud Function entry points
├── functions/           # Cloud Function logic
├── cloud-run/           # Cloud Run Jobs (each standalone)
├── dashboard/           # Local dashboard
├── scripts/             # Utility scripts
├── Makefile             # All commands
└── docs/                # Documentation
```

## Classification & Actions

| Category | Purpose | Action |
|----------|---------|--------|
| `marketing` | Drive engagement (sales, promos) | Label + archive |
| `newsletter` | Inform (content, digests) | Summarize → email summary → archive |
| `noti` | Unimportant notifications | Label + archive |
| `other` | Important or personal | No action |

Emails with subject starting with `🤖` are skipped (app's own emails).

## Cloud Functions (`functions/`)

- `pubsub_handler.py` - Receives Gmail Watch, triggers Cloud Run Job
- `watch_renewal.py` - Renews Gmail Watch weekly
- `gmail_client.py` - Gmail API client

## Cloud Run Jobs (`cloud-run/`)

- `email-processor/` - Classify emails, summarize newsletters, send summaries
- `unsubscribe-service/` - AI browser automation for unsubscribe pages

## Environment Variables

Cloud Functions:
- `GMAIL_AI_STORAGE_BUCKET` - Cloud Storage bucket
- `GMAIL_AI_PROJECT_ID` - GCP project ID

Cloud Run:
- `ANTHROPIC_API_KEY` - For Claude

## Deploy

```bash
make deploy              # Deploy all
make deploy-email-processor
make deploy-function
make check-logs
```

## OAuth Token

Token stored in `gs://gmail-ai-logs/token.json`. Refresh with:
```bash
export $(cat .env | grep -v '^#' | xargs)
uv run python scripts/refresh_token.py
gsutil cp token.json gs://gmail-ai-logs/token.json
```
