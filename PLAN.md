# Architecture Plan: Cloud-Based Email Processing Extension

This document outlines the plan to extend `gmail-ai-unsub` with new classification categories and cloud-based processing. It is organized into two sections:
1. **Final State**: The complete target architecture with cloud integration
2. **First State**: The initial implementation to get started

---

# Final State: Complete Cloud Architecture

## Overview

Final architecture includes:
- **Newsletter classification**: AI summarize and email summary to self
- **Unimportant notification classification**: Auto-archive
- **Cloud-based pub/sub processing**: Real-time email processing via Gmail Watch
- **Modular agent architecture**: Separate agents for each action type
- **Observability**: Local + cloud dashboard to view processing logs

## Architecture Diagram

```
Gmail Watch → Pub/Sub Topic → Cloud Function (Orchestrator)
                                      ↓
                            ┌─────────────────┐
                            │  Entry Handler   │ → 📝 Log: entry
                            └─────────────────┘
                                      ↓
                            ┌─────────────────┐
                            │  Query Inbox    │ → Find emails without 🤖 tag
                            │  (is:inbox      │
                            │   -label:🤖)    │
                            └─────────────────┘
                                      ↓
                            ┌─────────────────┐
                            │  Mark with 🤖    │ → Apply tag immediately
                            │  (even on error)│   (prevents reprocessing)
                            └─────────────────┘
                                      ↓
                            ┌─────────────────┐
                            │  Create Cloud   │ → One task per email
                            │  Tasks          │
                            └─────────────────┘
                                      ↓
                    ┌─────────────────────────────────┐
                    │    Cloud Tasks Queue            │
                    └─────────────────────────────────┘
                                      ↓
                    ┌─────────────────────────────────┐
                    │    Cloud Run Service            │
                    │    (email-processor)           │
                    └─────────────────────────────────┘
                                      ↓
                            ┌─────────────────┐
                            │  Fetch Email    │ → 📝 Log: fetch
                            └─────────────────┘
                                      ↓
                            ┌─────────────────┐
                            │  Classification │ → 📝 Log: classification
                            │     Agent       │
                            └─────────────────┘
                                      ↓
                    ┌─────────────────┼─────────────────┐
                    ↓                 ↓                 ↓
            ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
            │  Marketing   │  │ Newsletter   │  │ Unimportant   │
            │    Agent     │  │    Agent     │  │    Agent     │
            │  (Browser    │  │  (Summarize) │  │  (Archive)   │
            │   Automation)│  │              │  │              │
            └──────────────┘  └──────────────┘  └──────────────┘
                    ↓                 ↓                 ↓
            📝 Log: action    📝 Log: action    📝 Log: action
                    ↓                 ↓                 ↓
            ┌──────────────────────────────────────────────────┐
            │         Cloud Storage (JSONL Logs)              │
            │         gs://bucket/logs/YYYY/MM/DD/            │
            │    (logs written at every step above)            │
            └──────────────────────────────────────────────────┘
                                      ↓
                            Cloud Run Dashboard
                    (reads logs, shows activity)
                    Local: localhost:8080
                    Prod: https://dashboard.run.app
```

**Note**: 
- **Cloud Function (Orchestrator)**: Fast, lightweight - just queries inbox, creates tasks
- **Cloud Run (Processor)**: Handles heavy processing (classification, browser automation, etc.) with longer timeouts
- **Cloud Tasks Deduplication**: Task name = email ID, prevents duplicate processing even if Pub/Sub sends simultaneous notifications (1-hour deduplication window)
- **🤖 Tag**: Applied after task creation for tracking/visibility, not for deduplication (Cloud Tasks handles that)
- **Cloud Tasks**: Provides reliable delivery, retry, and built-in deduplication

## Components

### 1. Gmail Watch → Pub/Sub (Already Configured)
- Gmail Watch pushes notifications to Pub/Sub topic
- Message contains: `emailId`, `historyId`, `expiration`
- **Free tier**: 10GB/month

**Note**: Cloud Tasks free tier: 1M operations/month

### 2. Cloud Function (Orchestrator)
**File**: `src/gmail_ai_unsub/cloud/pubsub_handler.py`

**Flow:**
1. **Entry**: Receive Pub/Sub message → 📝 Log entry
2. **Query Inbox**: Find all emails in inbox (read or unread) without 🤖 tag → 📝 Log query result
3. **Create Tasks**: Create Cloud Task for each email (task name = email ID for deduplication) → 📝 Log task creation
4. **Mark Emails**: For each email, apply 🤖 tag (for tracking, not deduplication) → 📝 Log marking
5. **Complete**: Return success/failure → 📝 Log completion

**Deduplication Strategy:**
- **Cloud Tasks**: Task name = `email-{email_id}` prevents duplicate tasks even if Pub/Sub sends simultaneous notifications (1-hour deduplication window)
- **🤖 Tag**: Applied after task creation for visibility/tracking, not for deduplication

**Key Points:**
- Fast, lightweight function (no heavy processing)
- Cloud Tasks deduplication prevents race conditions (task name = email ID)
- 🤖 tag applied after task creation (for tracking, not deduplication)
- Cloud Tasks provide reliable delivery and retry
- Each email gets independent task (parallel processing)

### 2b. Cloud Run Service (Email Processor)
**File**: `src/gmail_ai_unsub/cloud/email_processor.py` (new)

**Flow (handles single email per invocation):**
1. **Entry**: Receive HTTP request from Cloud Task → 📝 Log entry
2. **Fetch**: Fetch email from Gmail API → 📝 Log fetch
3. **Classify**: Run Classification Agent → 📝 Log classification result
4. **Route & Execute**: Route to appropriate action agent, execute → 📝 Log action
5. **Complete**: Return success/failure → 📝 Log completion

**Key Points:**
- Handles one email per invocation
- Longer timeout (60+ seconds) for browser automation
- More memory/CPU for Playwright and AI models
- Independent processing (failures don't affect other emails)

### 3. Classification Agent
**File**: `src/gmail_ai_unsub/agents/classifier.py`

**Changes:**
- Extend `ClassificationResult`:
  ```python
  category: Literal["marketing", "newsletter", "unimportant_notification", "other"]
  is_marketing: bool  # Keep for backward compatibility
  ```
- Update prompts to distinguish:
  - **Newsletter**: Regular content updates (news, blogs, updates)
  - **Unimportant notification**: Low-value automated alerts
  - **Marketing**: Promotional emails (existing)

**Output**: Classification + routing decision

**Logging**: Logs classification result with confidence, category, and reasoning

### 4. Action Agents (Modular)

#### A. Marketing Agent
**File**: `src/gmail_ai_unsub/agents/marketing.py`
- Existing unsubscribe logic (refactored as agent)
- **Logging**: Logs unsubscribe attempt start, each method tried, success/failure

#### B. Newsletter Agent
**File**: `src/gmail_ai_unsub/agents/newsletter.py`
- **Summarization**: LLM summarizes email content → 📝 Log summary generation
- **Email Generation**: Create summary email → 📝 Log email creation
- **Send**: Gmail API sends summary to self → 📝 Log send status
- **Label**: Apply "Newsletter/Summarized" label → 📝 Log label applied

#### C. Unimportant Notification Agent
**File**: `src/gmail_ai_unsub/agents/unimportant.py`
- **Archive**: Remove from inbox → 📝 Log archive action
- **Label**: Apply "Unimportant Notification" label → 📝 Log label applied

#### D. Other/Pass-through Agent
- No action → 📝 Log pass-through (for observability)

### 5. Logging (At Every Step)

**Logging occurs at:**
1. Entry: Pub/Sub message received
2. Fetch: Email fetched from Gmail API
3. Classification: Classification result (with confidence, category, reasoning)
4. Action Start: Action agent invoked
5. Action Steps: Each sub-step (e.g., unsubscribe method tried, summary generated)
6. Action Complete: Action result (success/failure)
7. Overall: Function completion

**Structured JSON Logs:**
```json
{
  "timestamp": "2025-01-XX...",
  "trace_id": "abc123",
  "email_id": "gmail-msg-id",
  "stage": "entry|fetch|classification|action_start|action_step|action_complete|overall",
  "action": "receive|fetch|classify|unsubscribe|summarize|archive|complete",
  "result": "success|failure|skipped",
  "duration_ms": 1234,
  "metadata": {...}
}
```

**Storage:**
- Cloud Storage: `gs://bucket/logs/YYYY/MM/DD/*.jsonl`
- Partitioned by date for efficient queries
- Written immediately after each step
- **Free tier**: 5GB/month

**Trace ID**: Generated at entry point, passed through all steps for correlation

### 6. Dashboard (Observability)

**File**: `src/gmail_ai_unsub/dashboard/app.py`

**Features:**
- Recent activity feed (last 24 hours)
- Email processing timeline (trace view)
- Statistics (emails processed, success rates, by category)
- Search by email ID, trace ID, or sender
- Filter by category/action/result

**Tech:**
- FastAPI backend
- Simple HTML + vanilla JS frontend
- Reads JSONL from Cloud Storage

**Deployment:**
- **Local**: `gmail-ai-unsub dashboard --local` (syncs from Storage)
- **Cloud**: Deploy to Cloud Run (free tier: 2M requests/month)

## File Structure

```
src/gmail_ai_unsub/
├── agents/                    # New: Modular agents
│   ├── __init__.py
│   ├── classifier.py         # Extended classification
│   ├── marketing.py          # Marketing unsubscribe agent
│   ├── newsletter.py         # Newsletter summarization agent
│   └── unimportant.py        # Archive unimportant notifications
├── cloud/                     # New: Cloud deployment code
│   ├── __init__.py
│   ├── pubsub_handler.py     # Entry point for Pub/Sub
│   ├── functions.py           # Cloud Functions entry points
│   └── logging.py            # Structured logging setup
├── summarization/            # New: Newsletter summarization
│   ├── __init__.py
│   └── summarizer.py         # LLM-based summarization
├── dashboard/                # New: Observability dashboard
│   ├── __init__.py
│   ├── app.py                # FastAPI app
│   ├── templates/            # HTML templates
│   └── static/               # CSS/JS
└── [existing files...]
```

## Configuration Extensions

Add to `config.toml`:

```toml
[classification]
# Existing marketing classification
newsletter_criteria = "Regular content updates, news, blogs, informational emails"
unimportant_notification_criteria = "Low-value automated alerts, login notifications, profile views"

[newsletter]
enabled = true
summary_model = "gemini-2.5-flash"  # Can use faster model for summaries
summary_length = "medium"  # short, medium, long
send_to = "your-email@gmail.com"  # Where to send summaries
label = "Newsletter/Summarized"

[unimportant]
enabled = true
archive = true
label = "Unimportant Notification"

[cloud]
pubsub_topic = "projects/YOUR_PROJECT/topics/gmail-notifications"
project_id = "YOUR_PROJECT"
storage_bucket = "gmail-ai-logs"
tasks_queue = "email-processing"  # Cloud Tasks queue name
tasks_location = "us-central1"     # Cloud Tasks queue location
run_service = "email-processor"   # Cloud Run service name
processing_label = "🤖"            # Label to mark processed emails
```

## Google Cloud Setup

### Prerequisites
- Google Cloud account
- `gcloud` CLI installed
- Gmail Watch already configured (you have this)

### Setup Steps

1. **Create Project** (web console or CLI)
   ```bash
   gcloud projects create gmail-ai-unsub --name="Gmail AI Unsub"
   gcloud config set project gmail-ai-unsub
   ```

2. **Enable APIs**
   ```bash
   gcloud services enable gmail.googleapis.com
   gcloud services enable pubsub.googleapis.com
   gcloud services enable cloudfunctions.googleapis.com
   gcloud services enable cloudstorage.googleapis.com
   gcloud services enable run.googleapis.com
   ```

3. **Create Pub/Sub Topic** (if not exists)
   ```bash
   gcloud pubsub topics create gmail-notifications
   ```

4. **Create Cloud Storage Bucket**
   ```bash
   gcloud storage buckets create gs://gmail-ai-logs --location=us-central1
   ```

5. **Create Cloud Tasks Queue**
   ```bash
   gcloud tasks queues create email-processing \
     --location=us-central1
   ```

6. **Deploy Cloud Function (Orchestrator)**
   ```bash
   gcloud functions deploy gmail-orchestrator \
     --gen2 \
     --runtime=python312 \
     --region=us-central1 \
     --source=. \
     --entry-point=handle_pubsub \
     --trigger-topic=gmail-notifications \
     --timeout=60s \
     --memory=256Mi
   ```

7. **Deploy Cloud Run Service (Email Processor)**
   ```bash
   gcloud run deploy email-processor \
     --source=. \
     --region=us-central1 \
     --timeout=300s \
     --memory=1Gi \
     --cpu=1 \
     --no-allow-unauthenticated \
     --service-account=email-processor@YOUR_PROJECT.iam.gserviceaccount.com
   ```

8. **Deploy Dashboard**
   ```bash
   gcloud run deploy gmail-ai-dashboard \
     --source=. \
     --region=us-central1 \
     --allow-unauthenticated
   ```

**Time estimate**: 15-20 minutes first time, ~5 minutes after

## Implementation Phases

### Phase 1: Extend Classification
- [ ] Update `ClassificationResult` model with `category` field
- [ ] Extend prompts for newsletter/unimportant notification
- [ ] Test classification accuracy
- [ ] Keep backward compatibility with `is_marketing`

### Phase 2: Build Action Agents
- [ ] Refactor marketing unsubscribe into agent module
- [ ] Build newsletter summarization agent
- [ ] Build unimportant notification archiver
- [ ] Add agent routing logic

### Phase 3: Cloud Integration
- [ ] Implement Pub/Sub handler (orchestrator: query inbox, create tasks with deduplication, mark emails)
- [ ] Implement Cloud Run service (processor: fetch, classify, execute actions)
- [ ] Add Cloud Tasks integration (create tasks with email ID as task name for deduplication)
- [ ] Add logging at every step (entry, query, task creation, mark, fetch, classify, action, complete)
- [ ] Set up structured logging to Cloud Storage
- [ ] Deploy Cloud Function (orchestrator) and Cloud Run (processor)
- [ ] Test end-to-end flow with full logging and verify deduplication works

### Phase 4: Dashboard
- [ ] Build FastAPI dashboard app
- [ ] Implement log reading from Cloud Storage
- [ ] Create UI (activity feed, trace view, stats)
- [ ] Deploy to Cloud Run
- [ ] Add local mode (sync from Storage)

### Phase 5: Testing & Refinement
- [ ] Test with real emails
- [ ] Monitor logs and dashboard
- [ ] Tune classification prompts
- [ ] Optimize performance

## Cost Breakdown

**Google Cloud Free Tier:**
- Pub/Sub: 10GB/month free
- Cloud Functions: 2M invocations/month free
- Cloud Storage: 5GB/month free
- Cloud Run: 2M requests/month free
- Cloud Logging: 50GB/month free
- Gmail API: Free (quota limits apply)

**Total**: $0/month for personal use

## CLI Commands (New)

```bash
# View recent activity
gmail-ai-unsub logs --recent 24h

# View specific email processing
gmail-ai-unsub logs --email-id 19adade691d3f205

# View full trace
gmail-ai-unsub logs --trace-id abc123

# Open dashboard (local)
gmail-ai-unsub dashboard --local

# Sync logs from cloud
gmail-ai-unsub logs sync

# Deploy to cloud
gmail-ai-unsub cloud deploy
```

## Design Decisions

1. **Cloud Function + Cloud Run**: Orchestrator (fast, lightweight) creates tasks; Processor (heavy, long-running) handles email processing. This separation allows:
   - Fast Pub/Sub response (orchestrator completes quickly)
   - Longer timeouts for browser automation (Cloud Run supports 60+ seconds)
   - Better resource allocation (more memory/CPU for Playwright)
   - Independent processing (one email failure doesn't block others)
2. **Modular Agents**: Code organization as separate modules, not separate services
3. **Comprehensive Logging**: Log at every step (entry, fetch, classify, action start, action steps, action complete, overall) for full traceability
4. **Cloud Storage Logs**: JSONL format, partitioned by date for efficient queries
5. **Trace ID**: Generated at entry, passed through all steps for correlation
6. **Dashboard**: Reads directly from Cloud Storage (no database needed)
7. **Backward Compatibility**: Keep existing `is_marketing` field, add `category`
8. **Free Tier First**: All components use free tier services
9. **No Retry/Catch-up**: If service fails or is down, missed emails are just missed - no retry logic, no dead letter queue, no catch-up mechanism. Acceptable for personal use.
10. **Cloud Tasks Deduplication**: Task names use email ID to prevent duplicate processing from simultaneous Pub/Sub notifications. Deduplication window is ~1 hour, which is sufficient for preventing race conditions.

## Open Questions

1. **Newsletter Summarization**:
   - Daily digest vs. per-email?
   - Include images/links in summary?
   - How to handle very long newsletters?

2. **Unimportant Notifications**:
   - Delete vs. archive?
   - Exceptions (e.g., security alerts)?

3. **Error Handling**:
   - **Decision**: No retry logic, no dead letter queue, no catch-up mechanism
   - If service fails, log error and move on
   - If service is down, missed emails are just missed (acceptable for personal use)
   - Focus on making service reliable, not on perfect processing of every email

4. **Monitoring**:
   - Alerts for failures?
   - Email digest reports?

---

# First State: Minimal Pub/Sub Integration

## Overview

Minimal first step to get Pub/Sub integration working with basic observability:

- **Pub/Sub handler**: Receive Gmail Watch notifications
- **Email fetching**: Get email details (subject, snippet) from Gmail API
- **Logging**: Log subject and snippet to Cloud Storage
- **Dashboard**: View incoming emails in real-time

**No classification or actions yet** - just receiving and logging emails.

## Architecture Diagram

```
Gmail Watch → Pub/Sub Topic → Cloud Function
                                      ↓
                            ┌─────────────────┐
                            │  Receive Message │ → 📝 Log: entry
                            └─────────────────┘
                                      ↓
                            ┌─────────────────┐
                            │  Extract emailId│
                            │  from Pub/Sub   │
                            └─────────────────┘
                                      ↓
                            ┌─────────────────┐
                            │  Fetch Email     │ → 📝 Log: fetch
                            │  (metadata only) │
                            └─────────────────┘
                                      ↓
                            ┌─────────────────┐
                            │  Extract Subject│
                            │  & Snippet      │
                            └─────────────────┘
                                      ↓
                            ┌─────────────────┐
                            │  Log to Cloud   │ → 📝 Log: email details
                            │  Storage        │
                            └─────────────────┘
                                      ↓
                            ┌──────────────────────────────────┐
                            │   Cloud Storage (JSONL Logs)    │
                            │   gs://bucket/logs/YYYY/MM/DD/  │
                            └──────────────────────────────────┘
                                      ↓
                            ┌─────────────────┐
                            │   Dashboard     │
                            │   (Cloud Run)   │
                            └─────────────────┘
```

## Components

### 1. Pub/Sub Handler
**File**: `src/gmail_ai_unsub/cloud/pubsub_handler.py` (new)

**Flow:**
1. Receive Pub/Sub message
2. Extract `emailId` from notification payload
3. Generate trace ID
4. 📝 Log: entry received

### 2. Email Fetcher
**File**: `src/gmail_ai_unsub/cloud/email_fetcher.py` (new)

**Flow:**
1. Fetch email metadata from Gmail API (format: `metadata`)
2. Extract subject from headers
3. Extract snippet (Gmail provides this in metadata)
4. 📝 Log: email fetched

### 3. Logging
**File**: `src/gmail_ai_unsub/cloud/logging.py` (new)

**Structured JSON Logs:**
```json
{
  "timestamp": "2025-01-XX...",
  "trace_id": "abc123",
  "email_id": "gmail-msg-id",
  "stage": "entry|fetch|complete",
  "subject": "Email subject here",
  "snippet": "Email snippet preview...",
  "result": "success|failure"
}
```

**Storage:**
- Cloud Storage: `gs://bucket/logs/YYYY/MM/DD/*.jsonl`
- Partitioned by date
- Written immediately after fetching email

### 4. Dashboard
**File**: `src/gmail_ai_unsub/dashboard/app.py` (new)

**Features:**
- Recent emails feed (last 24 hours)
- Shows: timestamp, subject, snippet
- Auto-refresh (poll Cloud Storage every 5-10 seconds)
- Simple HTML + vanilla JS

**Deployment:**
- Cloud Run (free tier: 2M requests/month)
- Reads JSONL from Cloud Storage

## File Structure

```
src/gmail_ai_unsub/
├── cloud/                     # New: Cloud deployment code
│   ├── __init__.py
│   ├── pubsub_handler.py     # Entry point for Pub/Sub
│   ├── email_fetcher.py       # Fetch email metadata
│   └── logging.py            # Structured logging to Cloud Storage
├── dashboard/                # New: Observability dashboard
│   ├── __init__.py
│   ├── app.py                # FastAPI app
│   ├── templates/            # HTML templates
│   └── static/               # CSS/JS
└── [existing files...]
```

## Configuration

Add to `config.toml`:

```toml
[cloud]
pubsub_topic = "projects/YOUR_PROJECT/topics/gmail-notifications"
project_id = "YOUR_PROJECT"
storage_bucket = "gmail-ai-logs"
tasks_queue = "email-processing"  # Cloud Tasks queue name
tasks_location = "us-central1"     # Cloud Tasks queue location
run_service = "email-processor"   # Cloud Run service name
processing_label = "🤖"            # Label to mark processed emails
```

## Google Cloud Setup

### Prerequisites
- Google Cloud account
- `gcloud` CLI installed
- Gmail Watch already configured (you have this)

### Setup Steps

1. **Create Project** (if not exists)
   ```bash
   gcloud projects create gmail-ai-unsub --name="Gmail AI Unsub"
   gcloud config set project gmail-ai-unsub
   ```

2. **Enable APIs**
   ```bash
   gcloud services enable gmail.googleapis.com
   gcloud services enable pubsub.googleapis.com
   gcloud services enable cloudfunctions.googleapis.com
   gcloud services enable cloudstorage.googleapis.com
   gcloud services enable run.googleapis.com
   ```

3. **Create Pub/Sub Topic** (if not exists)
   ```bash
   gcloud pubsub topics create gmail-notifications
   ```

4. **Create Cloud Storage Bucket**
   ```bash
   gcloud storage buckets create gs://gmail-ai-logs --location=us-central1
   ```

5. **Deploy Cloud Function**
   ```bash
   gcloud functions deploy gmail-processor \
     --gen2 \
     --runtime=python312 \
     --region=us-central1 \
     --source=. \
     --entry-point=handle_pubsub \
     --trigger-topic=gmail-notifications \
     --timeout=60s \
     --memory=256Mi
   ```

6. **Deploy Dashboard**
   ```bash
   gcloud run deploy gmail-ai-dashboard \
     --source=. \
     --region=us-central1 \
     --allow-unauthenticated
   ```

**Time estimate**: 15-20 minutes first time

## Implementation Tasks

### Phase 1: Pub/Sub Handler
- [ ] Create Cloud Function entry point
- [ ] Parse Pub/Sub message
- [ ] Extract emailId from notification
- [ ] Generate trace ID
- [ ] Log entry
- [ ] **Error handling**: If message parsing fails, log error and return (don't retry)

### Phase 2: Email Fetcher
- [ ] Fetch email metadata from Gmail API
- [ ] Extract subject from headers
- [ ] Extract snippet (from Gmail metadata response)
- [ ] Log fetch result
- [ ] **Error handling**: If Gmail API fails, log error with emailId and return (don't retry)

### Phase 3: Logging
- [ ] Implement Cloud Storage writer
- [ ] Write JSONL logs (partitioned by date)
- [ ] **Error handling**: If Cloud Storage write fails, log to Cloud Logging and return (don't retry, don't block)

### Phase 4: Dashboard
- [ ] Build FastAPI app
- [ ] Read logs from Cloud Storage
- [ ] Create simple HTML UI (email feed)
- [ ] Add auto-refresh
- [ ] Deploy to Cloud Run

### Phase 5: Testing
- [ ] Test with real Gmail Watch notifications
- [ ] Verify logs are written correctly
- [ ] Verify dashboard shows emails
- [ ] Test error handling

## Design Decisions (First State)

1. **Minimal Scope**: Just receive, fetch, log - no processing yet
2. **Metadata Only**: Use Gmail API `format=metadata` for efficiency (includes snippet)
3. **Cloud Storage Logs**: JSONL format, partitioned by date
4. **Simple Dashboard**: Just show recent emails - no complex features yet
5. **Trace ID**: Generate at entry for future correlation (not used yet in dashboard)
6. **No Retry/Catch-up**: If processing fails, log error and return. If service is down, missed emails are just missed - acceptable for personal use.