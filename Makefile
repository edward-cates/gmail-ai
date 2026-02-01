.PHONY: help run-dashboard check-logs check-run-logs check-run-logs-recent check-function-logs deploy-function deploy-function-force deploy-watch-renewal deploy-email-processor deploy-unsubscribe-service test-email-processor test-unsubscribe-service test-functions test-dashboard test setup-queue deploy setup-scheduler watch-build lint

PROJECT_ID = neat-simplicity-486023-a4
PROJECT_NUMBER = 543519381062
REGION = us-central1

help:
	@echo "Available commands:"
	@echo ""
	@echo "  DEPLOY:"
	@echo "    make deploy                  - Deploy all components"
	@echo "    make deploy-email-processor  - Deploy email classifier (Cloud Run)"
	@echo "    make deploy-unsubscribe-service - Deploy browser automation (Cloud Run)"
	@echo "    make deploy-function         - Deploy Pub/Sub handler (Cloud Function)"
	@echo "    make deploy-watch-renewal    - Deploy watch renewal (Cloud Function)"
	@echo ""
	@echo "  TEST:"
	@echo "    make test                    - Run all tests"
	@echo "    make lint                    - Run linter"
	@echo "    make validate                - Run tests + lint"
	@echo ""
	@echo "  LOGS:"
	@echo "    make check-logs              - Check Cloud Storage logs"
	@echo "    make check-run-logs          - Check Cloud Run logs"
	@echo "    make check-function-logs     - Check Cloud Function logs"
	@echo ""
	@echo "  SETUP:"
	@echo "    make setup-queue             - Create Cloud Tasks queue"
	@echo "    make setup-scheduler         - Set up watch renewal scheduler"
	@echo ""
	@echo "  LOCAL:"
	@echo "    make run-dashboard           - Run dashboard locally"

# ============================================================================
# LOCAL TESTING
# ============================================================================

test-functions:
	@echo "Testing Cloud Function imports..."
	@uv run python -c "from functions.pubsub_handler import handle_pubsub; from functions.watch_renewal import renew_watch; print('✓ functions imports OK')"

test-email-processor:
	@echo "Testing email-processor syntax..."
	@uv run python -m py_compile cloud-run/email-processor/main.py && echo "✓ email-processor syntax OK"

test-unsubscribe-service:
	@echo "Testing unsubscribe-service syntax..."
	@uv run python -m py_compile cloud-run/unsubscribe-service/main.py && echo "✓ unsubscribe-service syntax OK"

test-dashboard:
	@echo "Testing dashboard imports..."
	@uv run python -c "from dashboard.app import app; print('✓ dashboard imports OK')"

test: test-functions test-email-processor test-unsubscribe-service test-dashboard
	@echo ""
	@echo "✓ All tests passed!"

lint:
	@echo "Linting..."
	@uv run ruff check functions/ dashboard/ main.py cloud-run/
	@echo "✓ Lint passed!"

validate: test lint
	@echo ""
	@echo "✓ All validation passed!"

run-dashboard:
	@echo "Starting dashboard on http://127.0.0.1:8080"
	@echo "Set GMAIL_AI_STORAGE_BUCKET and GMAIL_AI_PROJECT_ID env vars first"
	@uv run uvicorn dashboard.app:app --reload --port 8080 --host 127.0.0.1

# ============================================================================
# LOGS
# ============================================================================

check-logs:
	@echo "Checking recent logs from Cloud Storage..."
	@uv run python -c "\
	import os; \
	from google.cloud import storage; \
	client = storage.Client(project='$(PROJECT_ID)'); \
	bucket = client.bucket('gmail-ai-logs'); \
	from datetime import datetime; \
	date_str = datetime.utcnow().strftime('%Y/%m/%d'); \
	blob = bucket.blob(f'logs/{date_str}/log.jsonl'); \
	if blob.exists(): \
	    print(blob.download_as_text()[-2000:]); \
	else: \
	    print('No logs for today'); \
	"

check-run-logs:
	@echo "Checking Cloud Run service logs (email-processor)..."
	@gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=email-processor" \
		--limit=20 \
		--project=$(PROJECT_ID) \
		--format="table(timestamp,textPayload)" \
		--freshness=30m \
		2>&1 | head -40

check-run-logs-recent:
	@echo "Checking most recent Cloud Run revision logs..."
	@REVISION=$$(gcloud run services describe email-processor --region=$(REGION) --project=$(PROJECT_ID) --format="value(status.latestReadyRevisionName)" 2>/dev/null | head -1); \
	if [ -z "$$REVISION" ]; then echo "No revision found"; else \
		echo "Latest revision: $$REVISION"; \
		gcloud logging read "resource.type=cloud_run_revision AND resource.labels.revision_name=$$REVISION" \
			--limit=50 --project=$(PROJECT_ID) --format="value(timestamp,textPayload)" --freshness=10m 2>&1 | head -50; \
	fi

check-function-logs:
	@echo "Checking Cloud Function logs..."
	@gcloud logging read "resource.type=cloud_function AND resource.labels.function_name=gmail-processor" \
		--limit=20 \
		--project=$(PROJECT_ID) \
		--format="table(timestamp,severity,textPayload)" \
		--freshness=30m \
		2>&1 | head -30

# ============================================================================
# DEPLOY CLOUD FUNCTIONS
# ============================================================================

deploy-function:
	@echo "Deploying Cloud Function (Pub/Sub handler)..."
	@gcloud functions deploy gmail-processor \
		--gen2 \
		--runtime=python312 \
		--region=$(REGION) \
		--source=. \
		--entry-point=handle_pubsub_event \
		--trigger-topic=gmail-watch \
		--timeout=60s \
		--memory=256Mi \
		--project=$(PROJECT_ID) \
		--set-env-vars="GMAIL_AI_STORAGE_BUCKET=gmail-ai-logs,GMAIL_AI_PROJECT_ID=$(PROJECT_ID),GMAIL_AI_PROJECT_NUMBER=$(PROJECT_NUMBER),GMAIL_AI_TASKS_QUEUE=email-processing,GMAIL_AI_RUN_SERVICE=email-processor"

deploy-function-force: deploy-function

deploy-watch-renewal:
	@echo "Deploying watch renewal Cloud Function..."
	@gcloud functions deploy gmail-watch-renewal \
		--gen2 \
		--runtime=python312 \
		--region=$(REGION) \
		--source=. \
		--entry-point=renew_watch_http \
		--trigger-http \
		--allow-unauthenticated \
		--timeout=60s \
		--memory=256Mi \
		--project=$(PROJECT_ID) \
		--set-env-vars="GMAIL_AI_STORAGE_BUCKET=gmail-ai-logs,GMAIL_AI_PROJECT_ID=$(PROJECT_ID),GMAIL_AI_PROJECT_NUMBER=$(PROJECT_NUMBER)"

# ============================================================================
# DEPLOY CLOUD RUN SERVICES
# ============================================================================

deploy-email-processor:
	@echo "Deploying email-processor..."
	@if [ ! -f .env ]; then echo "Error: .env file not found"; exit 1; fi
	@export $$(cat .env | grep -v '^#' | xargs) && \
	if [ -z "$$ANTHROPIC_API_KEY" ]; then echo "Error: ANTHROPIC_API_KEY not set"; exit 1; fi && \
	SERVICE_ACCOUNT=$$(gcloud iam service-accounts list --project=$(PROJECT_ID) --filter="email:*-compute@developer.gserviceaccount.com" --format="value(email)" | head -1) && \
	gcloud run deploy email-processor \
		--source=cloud-run/email-processor \
		--region=$(REGION) \
		--platform=managed \
		--timeout=60s \
		--memory=512Mi \
		--cpu=1 \
		--no-allow-unauthenticated \
		--service-account="$$SERVICE_ACCOUNT" \
		--set-env-vars="GMAIL_AI_STORAGE_BUCKET=gmail-ai-logs,GMAIL_AI_PROJECT_ID=$(PROJECT_ID),ANTHROPIC_API_KEY=$$ANTHROPIC_API_KEY" \
		--project=$(PROJECT_ID) && \
	echo "✓ email-processor deployed!"

deploy-unsubscribe-service:
	@echo "Deploying unsubscribe-service..."
	@if [ ! -f .env ]; then echo "Error: .env file not found"; exit 1; fi
	@export $$(cat .env | grep -v '^#' | xargs) && \
	if [ -z "$$ANTHROPIC_API_KEY" ]; then echo "Error: ANTHROPIC_API_KEY not set"; exit 1; fi && \
	SERVICE_ACCOUNT=$$(gcloud iam service-accounts list --project=$(PROJECT_ID) --filter="email:*-compute@developer.gserviceaccount.com" --format="value(email)" | head -1) && \
	gcloud run deploy unsubscribe-service \
		--source=cloud-run/unsubscribe-service \
		--region=$(REGION) \
		--platform=managed \
		--timeout=300s \
		--memory=2Gi \
		--cpu=2 \
		--no-allow-unauthenticated \
		--service-account="$$SERVICE_ACCOUNT" \
		--set-env-vars="GMAIL_AI_STORAGE_BUCKET=gmail-ai-logs,GMAIL_AI_PROJECT_ID=$(PROJECT_ID),ANTHROPIC_API_KEY=$$ANTHROPIC_API_KEY" \
		--project=$(PROJECT_ID) && \
	echo "✓ unsubscribe-service deployed!"

# ============================================================================
# SETUP
# ============================================================================

setup-queue:
	@echo "Checking Cloud Tasks queue..."
	@QUEUE_EXISTS=$$(gcloud tasks queues describe email-processing --location=$(REGION) --project=$(PROJECT_ID) --format="value(name)" 2>/dev/null | wc -l); \
	if [ "$$QUEUE_EXISTS" -eq 0 ]; then \
		echo "Creating queue..."; \
		gcloud tasks queues create email-processing --location=$(REGION) --project=$(PROJECT_ID); \
		echo "✓ Queue created"; \
	else \
		echo "✓ Queue already exists"; \
	fi

setup-scheduler:
	@echo "Checking Cloud Scheduler job..."
	@FUNCTION_URL=$$(gcloud functions describe gmail-watch-renewal --gen2 --region=$(REGION) --project=$(PROJECT_ID) --format="value(serviceConfig.uri)" 2>/dev/null); \
	if [ -z "$$FUNCTION_URL" ]; then \
		echo "Error: deploy-watch-renewal first"; exit 1; \
	fi; \
	JOB_EXISTS=$$(gcloud scheduler jobs describe gmail-watch-renewal --location=$(REGION) --project=$(PROJECT_ID) --format="value(name)" 2>/dev/null | wc -l); \
	if [ "$$JOB_EXISTS" -eq 0 ]; then \
		gcloud scheduler jobs create http gmail-watch-renewal \
			--location=$(REGION) --schedule="0 2 * * 0" --uri="$$FUNCTION_URL" \
			--http-method=GET --time-zone="America/Los_Angeles" --project=$(PROJECT_ID); \
		echo "✓ Scheduler created"; \
	else \
		echo "✓ Scheduler already exists"; \
	fi

watch-build:
	@echo "Watching ongoing Cloud Build..."
	@BUILD_ID=$$(gcloud builds list --ongoing --limit=1 --format="value(id)" --project=$(PROJECT_ID) 2>/dev/null | head -1); \
	if [ -z "$$BUILD_ID" ]; then echo "No ongoing builds"; else \
		gcloud builds log $$BUILD_ID --stream --project=$(PROJECT_ID); \
	fi

# ============================================================================
# DEPLOY ALL
# ============================================================================

deploy: setup-queue deploy-function deploy-watch-renewal deploy-email-processor setup-scheduler
	@echo ""
	@echo "✓ All deployed!"
	@echo "Check logs: make check-logs"
