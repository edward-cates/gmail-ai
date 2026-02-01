# Cloud Email Processing

## Architecture

```
Gmail Watch → Pub/Sub → Cloud Function (gmail-processor)
                              ↓
                        Cloud Tasks
                              ↓
                    email-processor (Cloud Run)
                              ↓
                    Classify with Claude
                              ↓
                    Log to Cloud Storage
                              ↓
                    [Future] Take action
```

## Services

| Service | Type | Purpose |
|---------|------|---------|
| `gmail-processor` | Cloud Function | Pub/Sub handler, creates Cloud Tasks |
| `gmail-watch-renewal` | Cloud Function | Renews Gmail Watch weekly |
| `email-processor` | Cloud Run | Classifies emails with Claude |
| `unsubscribe-service` | Cloud Run | Browser automation (future) |
| Dashboard | Local | View logs |

## Classification Categories

| Category | Action |
|----------|--------|
| `marketing` | Unsubscribe |
| `newsletter` | Summarize |
| `unimportant_notification` | Archive |
| `other` | No action |

## Current Phase: Classification

- [x] Gmail Watch → Pub/Sub → Cloud Function
- [x] Cloud Function → Cloud Tasks → email-processor
- [x] email-processor: LOG → CLASSIFY → LOG
- [ ] Deploy and test

## File Structure

```
/
├── main.py                    # Cloud Function entry points
├── functions/                 # Cloud Function logic (standalone)
│   ├── pubsub_handler.py
│   ├── watch_renewal.py
│   ├── gmail_client.py
│   └── cloud_logger.py
├── cloud-run/
│   ├── email-processor/       # Lightweight classifier
│   │   ├── main.py
│   │   ├── Procfile
│   │   └── requirements.txt
│   └── unsubscribe-service/   # Heavy browser automation
│       ├── main.py
│       ├── Procfile
│       └── requirements.txt
├── dashboard/                 # Local dashboard
│   ├── app.py
│   └── templates/
├── Makefile                   # All commands
└── docs/cloud-setup.md        # Setup guide
```

## Deploy

```bash
make deploy              # Deploy all
make deploy-function     # Deploy Pub/Sub handler
make deploy-email-processor  # Deploy classifier
make check-logs          # View logs
make run-dashboard       # Local dashboard
```
