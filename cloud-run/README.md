# Cloud Run Jobs

Each job is standalone with its own `main.py`, `Procfile`, and `requirements.txt`.

## Jobs

### email-processor (lightweight)
Simple classifier: LOG → CLASSIFY → LOG

- Uses Claude for classification
- Memory: 512Mi
- Deploy: `make deploy-email-processor`

### unsubscribe-service (heavy)
AI browser automation for complex unsubscribe pages

- Uses browser-use + playwright
- Memory: 2Gi
- Deploy: `make deploy-unsubscribe-service`

## Structure

```
cloud-run/
├── email-processor/
│   ├── main.py           # Script (reads env vars)
│   ├── Procfile          # web: python3 main.py
│   └── requirements.txt
├── unsubscribe-service/
│   ├── main.py           # Script (reads env vars)
│   ├── Procfile          # web: python3 main.py
│   └── requirements.txt
└── README.md
```

## How Jobs Work

Jobs are triggered by Cloud Functions via the Cloud Run Jobs API. Each execution:
1. Reads configuration from environment variables
2. Processes the task
3. Logs results to Cloud Storage
4. Exits with 0 (success) or 1 (failure)

No HTTP servers, no request handling—just run and exit.
