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
                                        ↓ (every 5 min)
                              Cloud Scheduler → Cloud Function → Cloud Run Job (slack-processor)
                                                                      ↓
                                                            Classify + Describe (Opus 4.6) → Update Trello
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

### Model

Single Opus 4.6 (`claude-opus-4-6`) call per batch — classifies messages into topics,
names them, assigns priority, writes a 1-2 line description, flags action items, and
detects whether Edward is involved. No downstream model calls.

Uses `SLACK_AI_API_KEY` (dedicated key for Slack processing costs).

### Priorities

| Priority | Meaning | Action |
|----------|---------|--------|
| `needs_response` | Someone is waiting on **Edward** specifically | Card in "Needs Response" |
| `action_required` | **Edward** personally needs to do something | Card in "Action Required" |
| `worth_reading` | Relevant info, no action needed from Edward | Card in "Worth Reading" |
| `noted` | Low-priority but worth tracking | Card in "Noted" |
| `noise` | Zero informational value ("thanks!", "ok", emoji-only) | Silently dropped, never reaches Trello |

`needs_response` and `action_required` are ONLY for Edward Cates personally.
If someone else needs to act, it's `worth_reading` or `noted`.

Short messages (< 20 chars) that are NOT thread replies get 3 preceding channel messages
fetched as context, so Opus can distinguish noise ("ok!") from meaningful agreement.

### Batch Processing

1. Slack handler receives events via HTTP webhook, stores them in `gs://gmail-ai-logs/slack-pending/`
2. Cloud Scheduler triggers batch function (every 5 min)
3. Batch function checks for pending events, triggers slack-processor Cloud Run Job
4. Processor reads all pending events, **clears queue immediately**, then classifies
5. For each channel in the batch, fetches last 15 messages from Slack API as conversation context
6. One Opus call classifies all messages with full context, then updates Trello per message
7. Every 30 min (tracked via GCS timestamp), runs dedup pass to merge duplicate/overlapping cards

Queue is cleared before processing to prevent reprocessing on timeout/crash.
No fallback on parse failure — errors are logged and the batch is skipped.

### Thread Handling

Thread replies (messages with `thread_ts != ts`) skip classification entirely:
- Processor matches `thread_ts` against `ts:` markers stored in card descriptions
- If parent card found → add comment directly (no Opus call), auto-apply Mentioned label
- If parent not found → fall back to normal classification

### Trello Board

Four lists: `Needs Response` → `Action Required` → `Worth Reading` → `Noted`

Cards escalate to higher-priority lists automatically (never demote).

### Card Structure

Each card = a topic. Title is specific and concrete (real names, details), not abstract.

**Description** has two sections:
- Opus-generated 1-2 line description of the topic
- `**Threads**` — List of linked messages with `ts:` markers for thread matching

**Comments** — Individual messages with:
- Sender name
- Channel hyperlink (deep link to Slack channel)
- "View message" link (deep link to specific Slack message)
- Thread replies labeled as "(thread reply)"

**Checklist** — Per-message action items from Opus (most messages get none):
- Only created when a specific message creates a concrete task for Edward
- Short (under 10 words), e.g. "Review auth PR", "Respond to Alice re: deploy timeline"

**Labels**:
- Red "Mentioned" label — applied when Edward is involved in the conversation
  (directly mentioned, tagged, addressed, or message is directed at him).
  Thread replies auto-get this label. Created automatically if it doesn't exist.

### Board Description (Read-Only Context)

The Trello board description is user-maintained context about Edward, his projects,
and priorities. Opus reads it during classification to improve grouping and priority
decisions. The processor never writes to it.

### Dedup (every 30 min)

After message processing, if ≥30 min since last dedup (tracked in GCS `slack-dedup/last_run.txt`):
1. Fetch all open cards with comment previews
2. Opus identifies groups of duplicate/overlapping topics
3. For each group: create merged card, zipper comments chronologically, delete originals

### Logging & Debugging

All batch events share a single `batch_trace_id` (one collapsible group in dashboard).
Timing is logged for: channel context fetch, Opus call, total batch duration.
On parse failure, the first 500 chars of Opus's response are logged for debugging.

### User Mentions

Raw Slack markup (`<@U123>`, `<#C123>`) is resolved to readable names (`@Alice`, `#general`)
before classification and Trello display.

## Muscle Growth Coach (Trello)

### Architecture

```
Cloud Scheduler (7 AM CT) → Cloud Function → Cloud Run Job (coach)
                                                    ↓
                                          Read board desc (spec) + all cards/comments
                                                    ↓
                                          Opus 4.6 → Create exercise, nutrition, forum cards
                                                    ↓
                                          Execute board actions + Haiku spec update

Trello Webhook (comment on card) → Cloud Function → Cloud Run Job (coach)
                                                          ↓
                                                Read board desc + all cards + card comments
                                                          ↓
                                                Opus 4.6 → Reply comment + board actions
                                                          ↓
                                                Haiku → Apply spec update instruction
```

### Models

| Model | Role |
|-------|------|
| Opus 4.6 | Coaching responses (morning cards, replies, board actions) |
| Haiku 4.5 | Spec editing (applies brief instruction to full spec) |

Uses the `anthropic` SDK directly (not langchain) for faster container startup.

### Dual-Model Spec Update Strategy

The spec (board description) is the coach's only long-term memory. Updating it
requires two things: coaching judgment (what to change) and reliable text editing
(applying the change without breaking the rest of the document).

**Why not have Opus write the full spec?** Early versions had Opus return the
complete updated spec as a JSON field. The spec grew, and the full text inside
a JSON response hit output token limits — producing truncated JSON that broke
parsing. The spec was lost.

**The split:** Opus outputs a `spec_update_instruction` — a brief plain-English
description of what to change (e.g., "Update squat PR to 225, remove Tuesday's
grocery list, add vitamin D 5000 IU to supplements"). This is short and never
truncates. Haiku then receives the current spec + the instruction, applies the
edits, and returns the complete updated spec. Haiku's call is not JSON — it
returns raw text with plenty of output tokens.

**Why this works:** Opus has full context (board, conversation, spec) and makes
the judgment calls. Haiku is just a text editor — it follows precise instructions
and preserves everything it wasn't told to touch. The prompt explicitly tells it
to only make the described changes and keep everything else intact.

### State Management

- **Board description** — Living spec/manifesto. The coach's ONLY long-term memory. Updated via Haiku after each interaction.
- **Card comments** — Conversation history. Cards get archived by the user after responses.
- No GCS state files — all state lives in Trello

### Board Structure

- Board: `TRELLO_COACH_BOARD_ID`
- Three lists:
  - **Exercise** — one card per workout (description + exercise checklist + coach comment)
  - **Nutrition** — meal plans, grocery lists, supplements (description + checklist)
  - **Forum** — daily check-in card for open conversation throughout the day

### Morning Routine

1. Read board desc (spec) + all non-archived cards with comments and checklists
2. Opus generates: exercise card, nutrition card, forum check-in card, board actions, spec update instruction
3. Create cards with checklists and comments
4. Execute board actions (archive old cards, check items, etc.)
5. If spec update needed: Haiku applies the edit instruction to the full spec

### Reply Flow

1. Read board desc (spec) + full board context + specific card context
2. Opus generates: reply message, board actions, spec update instruction
3. Post reply comment (with `**[Coach]**` prefix to distinguish from user)
4. Execute board actions (can create new cards, archive, check items, etc.)
5. If spec update needed: Haiku applies the edit instruction to the full spec

### Board Actions

The coach can take these actions on the board:
- `archive_card` — archive a card
- `check_item` / `uncheck_item` — toggle checklist items
- `move_card` — move card between lists
- `comment` — comment on any card
- `update_card` — update card name/description
- `create_card` — create new cards on any list (with optional description, checklist, comment)
- `get_sleep_data` — fetch Oura Ring sleep score, readiness score, and contributor breakdowns for a date
- `get_calorie_data` — fetch Oura Ring total calories, active calories, and steps for a date

### Cloud Functions

- `coach_handler.py: handle_trello_webhook()` — Receives Trello webhook, filters for commentCard, skips own comments (checks `**[Coach]**` prefix), triggers job
- `coach_handler.py: trigger_coach_morning()` — Called by Cloud Scheduler, triggers morning card creation

### Environment Variables

Cloud Functions (Coach):
- `TRELLO_API_KEY` — Trello authentication (secret)
- `TRELLO_TOKEN` — Trello authentication (secret)
- `TRELLO_COACH_BOARD_ID` — Coach-specific Trello board
- `COACH_JOB_NAME` — Cloud Run Job name (default: `coach`)

Cloud Run (coach):
- `ANTHROPIC_API_KEY` — For Claude
- `TRELLO_API_KEY` — Trello authentication (secret)
- `TRELLO_TOKEN` — Trello authentication (secret)
- `TRELLO_COACH_BOARD_ID` — Coach-specific Trello board
- `OURA_ACCESS_TOKEN` — Oura Ring API personal access token (secret)
- `COACH_MODE` — "morning" or "reply" (set via container override)
- `COMMENT_TEXT` — User's comment text (reply mode only)
- `CARD_ID` — Card the comment was on (reply mode only)
- `ACTION_ID` — Trello action ID of the user's comment (reply mode, for reactions)

### Post-Deploy Setup

After deploying the coach components, these one-time steps are needed:

1. **Create Trello board** with three lists: Exercise, Nutrition, Forum
2. **Set board description** with initial client spec
3. **Register Trello webhook** (so card comments trigger the coach):
   ```bash
   CALLBACK_URL=$(gcloud functions describe coach-webhook-handler --gen2 --region=us-central1 --format="value(serviceConfig.uri)")
   curl -X POST "https://api.trello.com/1/tokens/${TRELLO_TOKEN}/webhooks/" \
     -d "callbackURL=${CALLBACK_URL}" \
     -d "idModel=${TRELLO_COACH_BOARD_ID}" \
     -d "description=Coach webhook" \
     -d "key=${TRELLO_API_KEY}"
   ```
4. **Set up Cloud Scheduler**: `make setup-coach-scheduler`

## Cloud Functions (`functions/`)

- `pubsub_handler.py` — Receives Gmail Watch, triggers email-processor Cloud Run Job
- `slack_handler.py` — Receives Slack events, verifies signature, stores to Cloud Storage
- `slack_handler.py: trigger_slack_batch()` — Called by Cloud Scheduler, triggers slack-processor
- `watch_renewal.py` — Renews Gmail Watch weekly
- `gmail_client.py` — Gmail API client

## Cloud Run Jobs (`cloud-run/`)

- `email-processor/` — Classify emails, summarize newsletters, send summaries
- `slack-processor/` — Batch classify Slack messages, manage Trello board
- `coach/` — Muscle growth coaching agent via Trello (Opus 4.6 + Haiku 4.5)
- `sms-coach/` — Muscle growth coaching agent via SMS (Twilio, deprecated)
- `unsubscribe-service/` — AI browser automation for unsubscribe pages

## CI/CD

GitHub Actions (`.github/workflows/deploy.yml`):
- **Validate** — runs `make validate` (syntax, imports, pytest, ruff) on all pushes/PRs
- **Selective deploy** — only deploys components whose files changed:
  - `main.py` or `functions/**` → all Cloud Functions
  - `cloud-run/email-processor/**` → email-processor
  - `cloud-run/slack-processor/**` → slack-processor
  - `cloud-run/coach/**` → coach
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
- `SLACK_AI_API_KEY` — Dedicated Anthropic API key for Slack processing
- `SLACK_BOT_TOKEN` — Slack API access (user token, `xoxp-`)
- `TRELLO_API_KEY` — Trello API key
- `TRELLO_TOKEN` — Trello auth token
- `TRELLO_BOARD_ID` — Target Trello board

## Deploy

```bash
make deploy                      # Deploy all
make deploy-email-processor      # Email classifier
make deploy-slack-processor      # Slack→Trello processor
make deploy-coach                # Trello coach (Cloud Run Job)
make deploy-coach-webhook        # Coach Trello webhook handler
make deploy-coach-morning-trigger # Coach morning trigger
make deploy-function             # Gmail Pub/Sub handler
make deploy-slack-function       # Slack event handler
make setup-coach-scheduler       # Set up 7 AM CT daily scheduler
make check-logs
```

## Batch Interval

Default: every 5 min (set in Makefile `setup-slack-scheduler`).

```bash
./scripts/set_batch_interval.sh 5    # every 5 min
./scripts/set_batch_interval.sh 15   # every 15 min
./scripts/set_batch_interval.sh      # show current
```

## GCP Project Setup

Prerequisites before first deploy:

```bash
# Enable required APIs
gcloud services enable \
  cloudfunctions.googleapis.com \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  logging.googleapis.com \
  gmail.googleapis.com \
  cloudbuild.googleapis.com

# Grant Cloud Build permission to deploy Cloud Run
PROJECT_NUMBER=$(gcloud projects describe $(gcloud config get-value project) --format="value(projectNumber)")
gcloud projects add-iam-policy-binding $(gcloud config get-value project) \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/run.admin"

# Grant default compute SA permission to access secrets
gcloud projects add-iam-policy-binding $(gcloud config get-value project) \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

## Secrets

Sync an env var from `.env` to GCP Secret Manager:
```bash
./scripts/sync_secret.sh ENV_VAR_NAME secret-name
```

| Env Var | Secret Name |
|---------|-------------|
| `ANTHROPIC_API_KEY` | `anthropic-api-key` |
| `SLACK_AI_API_KEY` | `slack-ai-api-key` |
| `SLACK_BOT_TOKEN` | `slack-bot-token` |
| `SLACK_SIGNING_SECRET` | `slack-signing-secret` |
| `TRELLO_API_KEY` | `trello-api-key` |
| `TRELLO_TOKEN` | `trello-token` |
| `OURA_ACCESS_TOKEN` | `oura-access-token` |

## OAuth Token

Token stored in `gs://gmail-ai-logs/token.json`. Refresh with:
```bash
export $(cat .env | grep -v '^#' | xargs)
uv run python scripts/refresh_token.py
gsutil cp token.json gs://gmail-ai-logs/token.json
```
