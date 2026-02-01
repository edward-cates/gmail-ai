# Cloud Run Jobs

Standalone jobs, each with its own `main.py`, `Procfile`, `requirements.txt`.

## email-processor

Classifies emails and takes action.

| Category | Action |
|----------|--------|
| marketing | Label + archive |
| newsletter | Summarize → email you → archive |
| noti | Label + archive |
| other | Nothing |

- Memory: 512Mi
- Deploy: `make deploy-email-processor`

## unsubscribe-service

AI browser automation for complex unsubscribe pages.

- Memory: 2Gi
- Deploy: `make deploy-unsubscribe-service`

## How Jobs Work

Triggered by Cloud Functions via Cloud Run Jobs API:
1. Read env vars (EMAIL_ID, TRACE_ID)
2. Fetch email, classify, act
3. Exit 0 (success) or 1 (failure)
