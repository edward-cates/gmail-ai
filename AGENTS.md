# AGENTS.md

AI-readable project documentation.

## Project Purpose

Cloud-based system that uses Claude to:
1. Classify Gmail messages and take automated actions (label, archive, summarize)
2. Organize Slack messages into a Trello board by topic for prioritized reading

## Architecture

```
Gmail Watch → Pub/Sub → Cloud Function → Cloud Run Job (email-processor)
                                              ↓
                                    Classify → Act → Log

Slack Events API → Cloud Function (HTTP) → Cloud Run Job (slack-processor)
                                                ↓
                                    Classify → Match Topic → Update Trello
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

## Email Classification & Actions

| Category | Purpose | Action |
|----------|---------|--------|
| `marketing` | Drive engagement (sales, promos) | Label + archive |
| `newsletter` | Inform (content, digests) | Summarize → email summary → archive |
| `noti` | Unimportant notifications | Label + archive |
| `other` | Important or personal | No action |

Emails with subject starting with `🤖` are skipped (app's own emails).

## Slack → Trello Board

Slack messages are classified and organized into a Trello board with four lists:

| List | Purpose |
|------|---------|
| `Needs Response` | Someone asked you a direct question or is waiting on your input |
| `Action Required` | Tasks, reviews, decisions that need you (not a direct question) |
| `Worth Reading` | Relevant discussion, no action needed from you now |
| `Noted` | Processed/done (manual) |

Each card = a topic. Cards have:
- Description with channel + summary
- Comments with individual messages
- Checklist with action items
- Cards escalate to higher-priority lists automatically (never demote)

## Cloud Functions (`functions/`)

- `pubsub_handler.py` - Receives Gmail Watch, triggers email-processor Cloud Run Job
- `slack_handler.py` - Receives Slack events, verifies signature, triggers slack-processor Cloud Run Job
- `watch_renewal.py` - Renews Gmail Watch weekly
- `gmail_client.py` - Gmail API client

## Cloud Run Jobs (`cloud-run/`)

- `email-processor/` - Classify emails, summarize newsletters, send summaries
- `slack-processor/` - Classify Slack messages, match to topics, update Trello board
- `unsubscribe-service/` - AI browser automation for unsubscribe pages

## Environment Variables

Cloud Functions:
- `GMAIL_AI_STORAGE_BUCKET` - Cloud Storage bucket
- `GMAIL_AI_PROJECT_ID` - GCP project ID

Cloud Functions (Slack):
- `SLACK_SIGNING_SECRET` - Verify Slack event requests (secret)
- `SLACK_PROCESSOR_JOB_NAME` - Cloud Run Job name (default: `slack-processor`)

Cloud Run (email-processor):
- `ANTHROPIC_API_KEY` - For Claude

Cloud Run (slack-processor):
- `ANTHROPIC_API_KEY` - For Claude
- `SLACK_BOT_TOKEN` - Slack API access
- `TRELLO_API_KEY` - Trello API key
- `TRELLO_TOKEN` - Trello auth token
- `TRELLO_BOARD_ID` - Target Trello board

## Deploy

```bash
make deploy                  # Deploy all
make deploy-email-processor  # Email classifier
make deploy-slack-processor  # Slack→Trello processor
make deploy-function         # Gmail Pub/Sub handler
make deploy-slack-function   # Slack event handler
make check-logs
```

## Secrets

Sync an env var from `.env` to GCP Secret Manager:
```bash
./scripts/sync_secret.sh ENV_VAR_NAME secret-name
```

Creates the secret if it doesn't exist, or adds a new version if it does.

| Env Var | Secret Name |
|---------|-------------|
| `ANTHROPIC_API_KEY` | `anthropic-api-key` |
| `SLACK_BOT_TOKEN` | `slack-bot-token` |
| `SLACK_SIGNING_SECRET` | `slack-signing-secret` |
| `TRELLO_API_KEY` | `trello-api-key` |
| `TRELLO_TOKEN` | `trello-token` |

## OAuth Token

Token stored in `gs://gmail-ai-logs/token.json`. Refresh with:
```bash
export $(cat .env | grep -v '^#' | xargs)
uv run python scripts/refresh_token.py
gsutil cp token.json gs://gmail-ai-logs/token.json
```
