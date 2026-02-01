# Architecture: Current Implementation (Phase 1 + Phase 2)

This document describes the **currently implemented** architecture. For future plans, see `PLAN.md`.

## Overview

**Phase 1** implements:
- Pub/Sub handler for Gmail Watch notifications
- Inbox query and email marking
- Structured logging to Cloud Storage
- Dashboard for viewing logs
- Automatic Gmail Watch renewal

**Phase 2** implements:
- Cloud Run service for email classification
- Cloud Tasks integration with deduplication
- Email classification using Claude Opus 4.5
- Task start and classification logging

## Architecture

```
Gmail Watch → Pub/Sub Topic → Cloud Function (gmail-processor)
                                      ↓
                            ┌─────────────────┐
                            │  Receive Pub/Sub│ → 📝 Log: entry
                            │  Message        │
                            └─────────────────┘
                                      ↓
                            ┌─────────────────┐
                            │  Query Inbox    │ → Find emails without 🤖 tag
                            │  (is:inbox      │   📝 Log: query (with count)
                            │   -label:🤖)    │
                            └─────────────────┘
                                      ↓
                            ┌─────────────────┐
                            │  For each email:│
                            │  - Check if     │
                            │    already 🤖    │ → 📝 Log: skip (if already processed)
                            │  - Fetch metadata│ → 📝 Log: fetch
                            │  - Mark with 🤖 │ → 📝 Log: mark
                            │  - Create Task  │ → 📝 Log: task_create
                            └─────────────────┘
                                      ↓
                            ┌──────────────────────────────────┐
                            │   Cloud Tasks Queue             │
                            │   (email-processing)            │
                            │   Task name: email-{email_id}   │
                            │   (deduplication: 1-hour)       │
                            └──────────────────────────────────┘
                                      ↓
                            ┌──────────────────────────────────┐
                            │   Cloud Run Service             │
                            │   (email-processor)            │
                            │   POST /process                 │
                            └──────────────────────────────────┘
                                      ↓
                            ┌─────────────────┐
                            │  Task Start     │ → 📝 Log: task_start
                            │  Fetch Full     │
                            │  Email          │
                            │  Classify       │ → 📝 Log: classification
                            │  (Claude Opus)  │
                            └─────────────────┘
                                      ↓
                            ┌──────────────────────────────────┐
                            │   Cloud Storage (JSONL Logs)    │
                            │   gs://gmail-ai-logs/logs/     │
                            │   YYYY/MM/DD/*.jsonl           │
                            └──────────────────────────────────┘
                                      ↓
                            ┌──────────────────────────────────┐
                            │   Dashboard (FastAPI)           │
                            │   - Local: localhost:8080        │
                            │   - Reads logs from Storage      │
                            │   - Shows recent activity        │
                            └──────────────────────────────────┘
```

## Components

### 1. Gmail Watch

**Configuration:**
- Topic: `projects/neat-simplicity-486023-a4/topics/gmail-watch`
- Monitors: `INBOX` label
- Expiration: 7 days (Gmail API limitation)

**Auto-Renewal:**
- **Cloud Function**: `gmail-watch-renewal` (HTTP trigger)
  - File: `src/gmail_ai_unsub/cloud/watch_renewal.py`
  - Entry point: `renew_watch_http` in `main.py`
  - Reads OAuth token from Cloud Storage
  - Stops existing watch and creates fresh one
- **Cloud Scheduler**: `gmail-watch-renewal`
  - Schedule: Every Sunday at 2 AM Pacific (`0 2 * * 0`)
  - Triggers renewal function via HTTP GET
  - Ensures watch never expires

**Management:**
- `make reset-gmail-watch` - Manually reset watch
- `make setup-gmail-watch` - Set up initial watch

### 2. Cloud Function: Pub/Sub Handler

**Function**: `gmail-processor`
- **Trigger**: Pub/Sub topic `gmail-watch`
- **Entry point**: `handle_pubsub_event` in `main.py`
- **File**: `src/gmail_ai_unsub/cloud/pubsub_handler.py`
- **Runtime**: Python 3.12
- **Memory**: 256Mi
- **Timeout**: 60s

**Flow:**
1. Receive Pub/Sub message (contains `historyId`)
2. Query inbox for emails without 🤖 tag: `is:inbox -label:🤖`
3. For each email:
   - Check if already has 🤖 tag (skip if yes)
   - Fetch email metadata (subject, snippet)
   - Apply 🤖 tag (for tracking)
   - Create Cloud Task with deduplication

**Logging:**
- Every step logs to Cloud Storage as JSONL
- Stages: `entry`, `query`, `skip`, `mark`, `task_create`
- Results: `success`, `failure`
- Includes trace_id for correlation

**Cloud Tasks:**
- Queue: `email-processing` in `us-central1`
- Task name: `email-{email_id}` for deduplication (1-hour window)
- Payload: `{"email_id": "...", "trace_id": "..."}`
- Target: Cloud Run service `/process` endpoint

### 3. Cloud Storage: Logging

**Bucket**: `gmail-ai-logs`
**Path**: `logs/YYYY/MM/DD/*.jsonl`

**Log Format:**
```json
{
  "timestamp": "2026-02-01T12:34:56.789Z",
  "trace_id": "uuid",
  "email_id": "gmail-message-id",
  "stage": "mark|query|task_create|...",
  "result": "success|failure",
  "metadata": {
    "subject": "Email subject",
    "snippet": "Email snippet",
    "count": 5,
    "error": "..."
  }
}
```

**File**: `src/gmail_ai_unsub/cloud/logging.py`
- `CloudLogger` class writes structured logs
- Partitions by date for efficient queries
- Silent failure (logs to Cloud Logging if Storage fails)

### 4. Cloud Tasks Queue

**Queue**: `email-processing`
- **Location**: `us-central1`
- **Purpose**: Reliable delivery of email processing jobs
- **Deduplication**: Task name = `email-{email_id}` (1-hour window)
- **Retry**: Automatic retries on failure
- **Free tier**: 1M operations/month

**Task Creation:**
- Created by Pub/Sub handler for each email
- Task name ensures no duplicate processing even with simultaneous Pub/Sub events
- Payload includes `email_id` and `trace_id` for correlation

### 5. Cloud Run Service: Email Processor

**Service**: `email-processor`
- **Entry point**: `cloud_run_main.py` (Flask app)
- **File**: `src/gmail_ai_unsub/cloud/email_processor.py`
- **Runtime**: Python 3.12
- **Memory**: 1Gi
- **CPU**: 1
- **Timeout**: 300s
- **Authentication**: Cloud Tasks only (not public)

**Endpoints:**
- `POST /process` - Process email classification (called by Cloud Tasks)
- `GET /` - Health check

**Flow:**
1. Receive HTTP request from Cloud Task (contains `email_id`, `trace_id`)
2. Log `task_start` stage
3. Fetch full email from Gmail API (subject, from, body)
4. Create classifier with Claude Opus 4.5
5. Classify email using `EmailClassifier`
6. Log `classification` stage with result (category, confidence, reason, model)
7. Return JSON response

**Configuration:**
- `ANTHROPIC_API_KEY` environment variable (from `.env` at deploy time)
- Model: `claude-4-5-opus` (configurable via `config.toml`)

**Logging:**
- `task_start`: When task begins processing
- `classification`: Classification result with metadata

### 6. Dashboard

**File**: `src/gmail_ai_unsub/dashboard/app.py`
**Framework**: FastAPI
**Templates**: `src/gmail_ai_unsub/dashboard/templates/`

**Features:**
- View logs from last 24 hours
- Filter by stage, result, email ID
- Shows subject, snippet, timestamp
- Color-coded by stage and result

**Local Development:**
```bash
make run-dashboard
# Opens http://localhost:8080
```

**Deployment:**
- Not yet deployed to Cloud Run
- Reads from Cloud Storage using Application Default Credentials

### 7. Email Fetching

**File**: `src/gmail_ai_unsub/cloud/email_fetcher.py`
- `fetch_email_metadata()`: Gets subject and snippet (metadata format, quota-efficient)
- `fetch_email_full()`: Gets full email including body (for classification)
- Extracts subject from email headers
- Handles multipart messages
- Uses `parse_email_body()` to extract plain text body

### 8. Configuration

**File**: `src/gmail_ai_unsub/config.py`

**Cloud Settings:**
```python
cloud_project_id = "neat-simplicity-486023-a4"
cloud_pubsub_topic = "projects/neat-simplicity-486023-a4/topics/gmail-watch"
cloud_storage_bucket = "gmail-ai-logs"
cloud_processing_label = "🤖"
cloud_tasks_queue = "email-processing"  # For future use
cloud_tasks_location = "us-central1"    # For future use
cloud_run_service = "email-processor"   # For future use
```

**Config Loading:**
- Local: Reads from `config.toml`
- Cloud: Loads from Cloud Storage `gs://gmail-ai-logs/config.toml`

### 9. OAuth Token Management

**Storage:**
- Local: `~/Library/Application Support/gmail-ai-unsub/token.json`
- Cloud: `gs://gmail-ai-logs/token.json`

**Loading:**
- Cloud Functions automatically load from Cloud Storage
- File: `src/gmail_ai_unsub/cloud/pubsub_handler.py` → `_get_token_file()`

**Upload:**
```bash
scripts/upload-token.sh
```

## File Structure

```
src/gmail_ai_unsub/
├── cloud/
│   ├── __init__.py
│   ├── pubsub_handler.py      # Main Pub/Sub handler (creates Cloud Tasks)
│   ├── watch_renewal.py        # Watch renewal function
│   ├── email_processor.py      # Cloud Run service (classifies emails)
│   ├── email_fetcher.py        # Email metadata and body extraction
│   └── logging.py              # Structured logging
├── dashboard/
│   ├── __init__.py
│   ├── app.py                  # FastAPI dashboard
│   └── templates/
│       ├── dashboard.html      # Main dashboard UI
│       └── error.html          # Error page
└── [existing files...]

main.py                          # Cloud Functions entry points
cloud_run_main.py                # Cloud Run entry point (Flask app)
scripts/
├── dev.py                       # Dev utilities (validate, reset-watch)
└── upload-token.sh              # Upload token to Cloud Storage
```

## Deployment

### Cloud Functions

**All Components:**
```bash
make deploy                    # Deploys everything (function, watch renewal, run service, scheduler)
```

**Individual Components:**
```bash
make deploy-function          # Deploy Pub/Sub handler
make deploy-watch-renewal      # Deploy watch renewal function
make deploy-run-service        # Deploy email processor (Cloud Run)
make setup-queue              # Create Cloud Tasks queue
make setup-scheduler          # Set up watch renewal scheduler
```

### Cloud Scheduler

**Setup:**
```bash
make setup-scheduler
```

**Manual Test:**
```bash
gcloud scheduler jobs run gmail-watch-renewal --location=us-central1 --project=neat-simplicity-486023-a4
```

## Development

**Validation:**
```bash
make validate              # Run all tests and checks
make validate-dashboard    # Check dashboard
make test-pubsub-handler   # Run Pub/Sub handler tests
```

**Local Testing:**
```bash
make run-dashboard         # Run dashboard locally
make check-logs            # View recent logs from Storage
```

**Gmail Watch:**
```bash
make reset-gmail-watch     # Reset watch (stops all, creates fresh)
make setup-gmail-watch     # Set up initial watch
```

## Current Limitations

1. **Actions**: Archive, summarize, unsubscribe not yet implemented (classification only)
2. **Dashboard**: Not yet deployed to Cloud Run (runs locally only)
3. **Classification Categories**: Currently only marketing/other (newsletter/unimportant not yet implemented)

## Next Steps

See `PLAN.md` for future implementation phases.
