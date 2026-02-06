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

Slack Events API → Cloud Function → Cloud Storage (queue)
                                        ↓ (every 15 min, configurable)
                              Cloud Scheduler → Cloud Function → Cloud Run Job (slack-processor)
                                                                      ↓
                                                          Classify (Sonnet) → Update Trello (Haiku)
```

All code is standalone with no cross-imports between top-level directories:

```
/
├── main.py              # Cloud Function entry points
├── functions/           # Cloud Function logic
├── cloud-run/           # Cloud Run Jobs (each standalone)
├── dashboard/           # Local dashboard
├── scripts/             # Utility scripts
├── tests/               # Unit tests (pytest)
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

### Model Split

| Model | Role | When |
|-------|------|------|
| Sonnet 4.5 (`claude-sonnet-4-5`) | Batch topic classification + priority | One call per batch (every 15 min) |
| Haiku (`claude-3-5-haiku-20241022`) | Card description updates, action item revision, reaction interpretation | Per-card, cheap |

### Priorities

| Priority | Meaning | Action |
|----------|---------|--------|
| `needs_response` | Someone is waiting on me | Card in "Needs Response" |
| `action_required` | I need to do something | Card in "Action Required" |
| `worth_reading` | Relevant info, no action | Card in "Worth Reading" |
| `noise` | Zero informational value ("thanks!", "ok", emoji-only) | Silently dropped, never reaches Trello |

Short messages (< 20 chars) that are NOT thread replies get 3 preceding channel messages
fetched as context, so Sonnet can distinguish noise ("ok!") from meaningful agreement.

### Batch Processing

1. Slack handler receives events via HTTP webhook, stores them in `gs://gmail-ai-logs/slack-pending/`
2. Cloud Scheduler triggers batch function (default: every 15 min, change via `./scripts/set_batch_interval.sh`)
3. Batch function checks for pending events, triggers slack-processor Cloud Run Job
4. Processor reads all pending events, **clears queue immediately**, then classifies
5. For each channel in the batch, fetches last 15 messages from Slack API as conversation context
6. One Sonnet call classifies all messages with full context, then updates Trello per message

Queue is cleared before processing to prevent reprocessing on timeout/crash.
No fallback on parse failure — errors are logged and the batch is skipped.

### Thread Handling

Thread replies (messages with `thread_ts != ts`) skip classification entirely:
- Processor matches `thread_ts` against `ts:` markers stored in card descriptions
- If parent card found → add comment directly (no Sonnet call)
- If parent not found → fall back to normal classification

### Trello Board

Four lists: `Needs Response` → `Action Required` → `Worth Reading` → `Noted`

Cards escalate to higher-priority lists automatically (never demote).

### Card Structure

Each card = a topic.

**Description** has three sections:
- `**Summary**` — 1-3 sentence living brief, updated by Haiku on each new message
- `**Reactions**` — Natural-language sentiment from emoji reactions on user's own messages
- `**Threads**` — List of linked messages with `ts:` markers for thread matching

**Comments** — Individual messages with:
- Sender name
- Channel hyperlink (deep link to Slack channel)
- "View message" link (deep link to specific Slack message)
- Thread replies labeled as "(thread reply)"

**Checklist** — Action items, managed by Haiku during summary updates:
- New items added when messages create tasks
- Items marked complete when conversation resolves them
- All items unchecked when an existing card receives a new message
- Sonnet provides initial action items during classification; Haiku revises on each update

### Board Description (Memory)

The Trello board description acts as persistent user context / memory.
Haiku updates it after each batch with observations about projects, people, priorities.
Sonnet reads it during classification to improve grouping. User can view and edit it
directly in Trello (click board name → "About this board").

### Logging & Debugging

All batch events share a single `batch_trace_id` (one collapsible group in dashboard).
Timing is logged for: channel context fetch, Sonnet call, total batch duration.
On parse failure, the first 500 chars of Sonnet's response are logged for debugging.

### User Mentions

Raw Slack markup (`<@U123>`, `<#C123>`) is resolved to readable names (`@Alice`, `#general`)
before classification and Trello display.

## Cloud Functions (`functions/`)

- `pubsub_handler.py` — Receives Gmail Watch, triggers email-processor Cloud Run Job
- `slack_handler.py` — Receives Slack events, verifies signature, stores to Cloud Storage
- `slack_handler.py: trigger_slack_batch()` — Called by Cloud Scheduler, triggers slack-processor
- `watch_renewal.py` — Renews Gmail Watch weekly
- `gmail_client.py` — Gmail API client

## Cloud Run Jobs (`cloud-run/`)

- `email-processor/` — Classify emails, summarize newsletters, send summaries
- `slack-processor/` — Batch classify Slack messages, manage Trello board
- `unsubscribe-service/` — AI browser automation for unsubscribe pages

## CI/CD

GitHub Actions (`.github/workflows/deploy.yml`):
- **Validate** — runs `make validate` (syntax, imports, pytest, ruff) on all pushes/PRs
- **Selective deploy** — only deploys components whose files changed:
  - `main.py` or `functions/**` → all Cloud Functions
  - `cloud-run/email-processor/**` → email-processor
  - `cloud-run/slack-processor/**` → slack-processor
  - `cloud-run/unsubscribe-service/**` → unsubscribe-service

Deploy jobs run in parallel after validation passes.

## Environment Variables

Cloud Functions:
- `GMAIL_AI_STORAGE_BUCKET` — Cloud Storage bucket
- `GMAIL_AI_PROJECT_ID` — GCP project ID

Cloud Functions (Slack):
- `SLACK_SIGNING_SECRET` — Verify Slack event requests (secret)
- `SLACK_PROCESSOR_JOB_NAME` — Cloud Run Job name (default: `slack-processor`)

Cloud Run (email-processor):
- `ANTHROPIC_API_KEY` — For Claude

Cloud Run (slack-processor):
- `ANTHROPIC_API_KEY` — For Claude
- `SLACK_BOT_TOKEN` — Slack API access (user token, `xoxp-`)
- `TRELLO_API_KEY` — Trello API key
- `TRELLO_TOKEN` — Trello auth token
- `TRELLO_BOARD_ID` — Target Trello board

## Deploy

```bash
make deploy                  # Deploy all
make deploy-email-processor  # Email classifier
make deploy-slack-processor  # Slack→Trello processor
make deploy-function         # Gmail Pub/Sub handler
make deploy-slack-function   # Slack event handler
make check-logs
```

## Batch Interval

```bash
./scripts/set_batch_interval.sh 5    # every 5 min
./scripts/set_batch_interval.sh 15   # every 15 min
./scripts/set_batch_interval.sh      # show current
```

## Secrets

Sync an env var from `.env` to GCP Secret Manager:
```bash
./scripts/sync_secret.sh ENV_VAR_NAME secret-name
```

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
