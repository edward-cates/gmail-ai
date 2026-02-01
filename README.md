# Gmail AI

Cloud-based email processor that classifies Gmail messages with Claude and takes automated actions.

## What It Does

| Category | Criteria | Action |
|----------|----------|--------|
| **marketing** | Drives engagement (sales, promos, "we miss you") | Label + archive |
| **newsletter** | Informs (digests, essays, news) | Summarize → email you → archive |
| **noti** | Noise (social likes, shipping, receipts) | Label + archive |
| **other** | Important or personal | Nothing |

Newsletter summaries are emailed to you with 🤖 prefix (elevator-pitch length, ~1 min read).

## Architecture

```
Gmail Watch → Pub/Sub → Cloud Function → Cloud Run Job
                                              ↓
                                    Fetch → Classify → Act
```

## Setup

See [docs/cloud-setup.md](docs/cloud-setup.md) for full GCP setup.

**Quick version:**

1. Create GCP project, enable APIs
2. Set up OAuth consent screen with scopes: `gmail.readonly`, `gmail.modify`, `gmail.send`
3. Create `.env` with `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `ANTHROPIC_API_KEY`
4. Generate token: `uv run python scripts/refresh_token.py`
5. Upload: `gsutil cp token.json gs://gmail-ai-logs/token.json`
6. Deploy: `make deploy`

## Deploy

```bash
make deploy                  # Deploy all
make deploy-email-processor  # Just the classifier
make deploy-function         # Just the Pub/Sub handler
```

## Logs

```bash
make check-logs              # Cloud Storage logs
make check-job-logs          # Cloud Run Job logs
make check-function-logs     # Cloud Function logs
make run-dashboard           # Local web dashboard
```

## Refresh OAuth Token

When you need to add scopes or token expires:

```bash
export $(cat .env | grep -v '^#' | xargs)
uv run python scripts/refresh_token.py
gsutil cp token.json gs://gmail-ai-logs/token.json
```

## Structure

```
├── main.py              # Cloud Function entry points
├── functions/           # Cloud Function logic
├── cloud-run/
│   ├── email-processor/ # Classifier (Cloud Run Job)
│   └── unsubscribe-service/ # Browser automation
├── dashboard/           # Local log viewer
├── scripts/             # Utilities
└── Makefile
```

## License

MIT
