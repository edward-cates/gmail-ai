# Gmail AI

Cloud-based system that uses Claude to tame your inbox and Slack. Runs on Google Cloud, processes events in near real-time, costs pennies.

**Email:** Classifies Gmail messages and auto-labels, archives, or summarizes them.
**Slack:** Organizes messages into a prioritized Trello board by topic so you process a queue instead of a firehose.

## What It Does

### Email Classification

| Category | Criteria | Action |
|----------|----------|--------|
| **marketing** | Sales, promos, engagement bait | Label + archive |
| **newsletter** | Digests, essays, news roundups | Summarize → email you the summary → archive |
| **noti** | Social likes, shipping, receipts | Label + archive |
| **other** | Important or personal | No action |

Newsletter summaries are emailed with a `🤖` prefix (~1 min read). Emails starting with `🤖` are skipped to avoid loops.

### Slack → Trello

Messages are classified by Claude and organized into a Trello board:

| List | When |
|------|------|
| **Needs Response** | Someone asked you a direct question |
| **Action Required** | Tasks, reviews, decisions — not a direct question |
| **Worth Reading** | Relevant discussion, no action needed now |
| **Noted** | Done (you move cards here manually) |

Each card is a topic. New messages either create a card or get matched to an existing one. Action items become checklist items. Cards escalate to higher-priority lists automatically but never demote.

## Architecture

```
Gmail Watch → Pub/Sub → Cloud Function → Cloud Run Job (email-processor)
                             │                  ↓
                             │        Fetch → Classify → Label/Archive/Summarize
                             │
Slack Events API ──→ Cloud Function → Cloud Run Job (slack-processor)
                          (HTTP)              ↓
                                    Classify → Match Topic → Update Trello
```

**Cloud Functions** receive events (lightweight, always-on, instant cold start). **Cloud Run Jobs** do the heavy work (Claude API, Gmail/Slack/Trello API calls, then exit). Every log entry carries a `trace_id` generated at the function layer so you can follow a single event end-to-end.

## Project Structure

```
├── main.py                     # Cloud Function entry points (GCP requires this at root)
├── functions/                  # Cloud Function logic
│   ├── pubsub_handler.py      #   Gmail Watch → triggers email-processor job
│   ├── slack_handler.py       #   Slack events → triggers slack-processor job
│   ├── watch_renewal.py       #   Renews Gmail Watch weekly
│   └── gmail_client.py        #   Gmail API client
├── cloud-run/                  # Cloud Run Jobs (each standalone, no cross-imports)
│   ├── email-processor/       #   Classify emails, summarize newsletters
│   ├── slack-processor/       #   Classify Slack messages, update Trello
│   └── unsubscribe-service/   #   AI browser automation for unsubscribe pages
├── dashboard/                  # Local web dashboard (FastAPI, grouped by trace_id)
├── scripts/
│   ├── refresh_token.py       #   Gmail OAuth token refresh
│   └── sync_secret.sh         #   Sync .env var → GCP Secret Manager
└── Makefile                    # All commands
```

## Setup

For full GCP setup from scratch (project, APIs, OAuth, permissions), see **[docs/cloud-setup.md](docs/cloud-setup.md)**.

### `.env`

```bash
GMAIL_CLIENT_ID=your-client-id.apps.googleusercontent.com
GMAIL_CLIENT_SECRET=your-client-secret
ANTHROPIC_API_KEY=sk-ant-...

# Only needed for Slack pipeline
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
TRELLO_API_KEY=...
TRELLO_TOKEN=...
TRELLO_BOARD_ID=...
```

### Slack App (optional)

1. https://api.slack.com/apps → Create New App → From scratch
2. OAuth & Permissions → Bot scopes: `channels:history`, `channels:read`, `groups:history`, `groups:read`, `users:read`
3. Install to workspace, copy Bot Token
4. Event Subscriptions → Enable → set URL to the deployed Cloud Function URL (deploy first, then configure)
5. Subscribe to: `message.channels`, `message.groups`

### Trello Board (optional)

1. Create a board with four lists: **Needs Response**, **Action Required**, **Worth Reading**, **Noted**
2. Get API key + token from https://trello.com/power-ups/admin
3. Get board ID: `curl "https://api.trello.com/1/members/me/boards?key=KEY&token=TOKEN&fields=name,id"`

## Secrets

Sync `.env` values to GCP Secret Manager:

```bash
./scripts/sync_secret.sh ANTHROPIC_API_KEY anthropic-api-key
./scripts/sync_secret.sh SLACK_BOT_TOKEN slack-bot-token
./scripts/sync_secret.sh SLACK_SIGNING_SECRET slack-signing-secret
./scripts/sync_secret.sh TRELLO_API_KEY trello-api-key
./scripts/sync_secret.sh TRELLO_TOKEN trello-token
```

Creates the secret if new, adds a version if it exists.

## Deploy

```bash
make deploy                     # Everything

# Or individually:
make deploy-email-processor     # Email classifier (Cloud Run Job)
make deploy-slack-processor     # Slack→Trello processor (Cloud Run Job)
make deploy-function            # Gmail Pub/Sub handler (Cloud Function)
make deploy-slack-function      # Slack event handler (Cloud Function)
make deploy-watch-renewal       # Gmail Watch renewal (Cloud Function)
make setup-scheduler            # Weekly watch renewal via Cloud Scheduler
```

After deploying `slack-handler`, get the URL and configure it in Slack:

```bash
gcloud functions describe slack-handler --gen2 --region=us-central1 --format="value(serviceConfig.uri)"
```

## Logs & Dashboard

```bash
make check-job-logs             # email-processor logs
make check-function-logs        # gmail-processor logs
make check-slack-logs           # slack-processor logs
make check-slack-function-logs  # slack-handler logs
make run-dashboard              # Local web dashboard at http://127.0.0.1:8080
```

The dashboard groups log entries by `trace_id` in collapsible rows — one row per event, expandable to see the full pipeline.

## Refresh OAuth Token

When expired or after adding scopes:

```bash
export $(cat .env | grep -v '^#' | xargs)
uv run python scripts/refresh_token.py
gsutil cp token.json gs://gmail-ai-logs/token.json
```

## License

MIT
