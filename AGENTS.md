# AGENTS.md

AI-readable project documentation.

## Project Purpose

Cloud-based email processing system that uses LLMs to classify Gmail messages and take automated actions.

## Architecture

All code is standalone with no cross-imports between top-level directories:

```
/
├── main.py              # Cloud Function entry points
├── functions/           # Cloud Function logic
├── cloud-run/           # Cloud Run services (each standalone)
├── dashboard/           # Local dashboard
├── Makefile             # All commands
└── docs/                # Documentation
```

## Cloud Functions (`functions/`)

Deployed via `main.py` at root. Each file is standalone.

- `pubsub_handler.py` - Receives Gmail Watch Pub/Sub, creates Cloud Tasks
- `watch_renewal.py` - Renews Gmail Watch weekly
- `gmail_client.py` - Gmail API client
- `cloud_logger.py` - Cloud Storage logging

## Cloud Run Services (`cloud-run/`)

Each service has its own `main.py`, `Procfile`, `requirements.txt`.

- `email-processor/` - Lightweight: LOG → CLASSIFY → LOG
- `unsubscribe-service/` - Heavy: AI browser automation

## Key Design Decisions

1. **Standalone services** - No shared code between directories
2. **Simple classification** - Claude API, no fancy frameworks
3. **Cloud Tasks for dedup** - Task name = email ID
4. **Cloud Storage logs** - JSONL files, read by dashboard

## Environment Variables

Cloud Functions:
- `GMAIL_AI_STORAGE_BUCKET` - Cloud Storage bucket for logs
- `GMAIL_AI_PROJECT_ID` - GCP project ID
- `GMAIL_AI_PROJECT_NUMBER` - GCP project number (for OIDC)

Cloud Run:
- `ANTHROPIC_API_KEY` - For Claude classification

## Deploy

All via Makefile:
- `make deploy` - Deploy all
- `make deploy-function` - Deploy Pub/Sub handler
- `make deploy-email-processor` - Deploy classifier
- `make check-logs` - View logs

## Commit Message Standards

Use [Conventional Commits](https://www.conventionalcommits.org/):
- `feat(cloud): add email classification`
- `fix(functions): handle empty historyId`
- `docs: update deployment guide`
