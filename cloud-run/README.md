# Cloud Run Services

Each service is standalone with its own `main.py`, `Procfile`, and `requirements.txt`.

## Services

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
│   ├── main.py           # FastAPI app
│   ├── Procfile          # uvicorn main:app
│   └── requirements.txt
├── unsubscribe-service/
│   ├── main.py           # FastAPI app  
│   ├── Procfile          # uvicorn main:app
│   └── requirements.txt
└── README.md
```
