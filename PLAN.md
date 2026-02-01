# Cloud Email Processing

## Architecture

```
Gmail Watch → Pub/Sub → Cloud Function (gmail-processor)
                              ↓
                    Cloud Run Job (email-processor)
                              ↓
                    Classify with Claude
                              ↓
                    Log to Cloud Storage
```

## Services

| Service | Type | Purpose |
|---------|------|---------|
| `gmail-processor` | Cloud Function | Pub/Sub handler, triggers jobs |
| `gmail-watch-renewal` | Cloud Function | Renews Gmail Watch weekly |
| `email-processor` | Cloud Run Job | Classifies emails with Claude |
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
- [x] Cloud Function triggers Cloud Run Job
- [x] Job: classify email, log result
- [ ] Deploy and test

## File Structure

```
/
├── main.py                    # Cloud Function entry points
├── functions/                 # Cloud Function logic
│   ├── pubsub_handler.py      # Triggers job per email
│   ├── watch_renewal.py
│   ├── gmail_client.py
│   └── cloud_logger.py
├── cloud-run/
│   └── email-processor/       # Cloud Run Job (not a service!)
│       ├── main.py            # Script: classify and exit
│       └── requirements.txt
├── dashboard/
│   ├── app.py
│   └── templates/
├── Makefile
└── docs/cloud-setup.md
```

## Deploy

```bash
make deploy              # Deploy all
make deploy-email-processor  # Deploy job
make deploy-function     # Deploy Pub/Sub handler
make check-logs          # View logs
```
