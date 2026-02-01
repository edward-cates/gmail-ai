.PHONY: help validate-dashboard test-dashboard run-dashboard check-logs deploy-function deploy-watch-renewal deploy-run-service setup-queue deploy setup-scheduler watch-build setup-gmail-watch reset-gmail-watch test-pubsub-handler test-email-processor validate

help:
	@echo "Available commands:"
	@echo "  make validate           - Run all validation (dashboard + pubsub handler tests)"
	@echo "  make validate-dashboard  - Check dashboard for errors/warnings"
	@echo "  make test-dashboard     - Test dashboard API endpoints"
	@echo "  make test-pubsub-handler - Run Pub/Sub handler tests with mocking"
	@echo "  make test-email-processor - Run email processor tests with mocking"
	@echo "  make run-dashboard      - Run dashboard locally"
	@echo "  make check-logs         - Check recent Cloud Storage logs"
	@echo "  make deploy              - Deploy all components (function, watch renewal, run service)"
	@echo "  make deploy-function     - Deploy Cloud Function (Pub/Sub handler)"
	@echo "  make deploy-watch-renewal - Deploy watch renewal Cloud Function"
	@echo "  make deploy-run-service  - Deploy Cloud Run service (email processor)"
	@echo "  make setup-queue         - Create Cloud Tasks queue"
	@echo "  make setup-scheduler     - Set up Cloud Scheduler to auto-renew watch (every 6 days)"
	@echo "  make watch-build        - Watch ongoing Cloud Build logs"
	@echo "  make setup-gmail-watch  - Set up Gmail Watch"
	@echo "  make reset-gmail-watch  - Stop all watches and set up fresh one (fixes duplicates)"

validate-dashboard:
	@echo "Validating dashboard code quality..."
	@uv run ruff check src/gmail_ai_unsub/dashboard/
	@echo "Validating dashboard functionality..."
	@PYTHONPATH=src uv run python scripts/dev.py validate-dashboard

test-dashboard:
	@echo "Testing dashboard endpoints..."
	@curl -s http://127.0.0.1:8080/api/logs > /dev/null && echo "✓ /api/logs endpoint works" || echo "✗ /api/logs endpoint failed"
	@curl -s http://127.0.0.1:8080/ > /dev/null && echo "✓ / endpoint works" || echo "✗ / endpoint failed"

run-dashboard:
	@echo "Starting dashboard on http://127.0.0.1:8080"
	@PYTHONPATH=src uv run python -m uvicorn gmail_ai_unsub.dashboard.app:app --reload --port 8080 --host 127.0.0.1

check-logs:
	@echo "Checking recent logs from Cloud Storage..."
	@PYTHONPATH=src uv run python scripts/check-logs.py

deploy-function:
	@echo "Checking if Cloud Function needs deployment..."
	@FUNCTION_EXISTS=$$(gcloud functions describe gmail-processor --gen2 --region=us-central1 --project=neat-simplicity-486023-a4 --format="value(name)" 2>/dev/null | wc -l); \
	FUNCTION_SOURCE_FILES="src/gmail_ai_unsub/cloud/pubsub_handler.py src/gmail_ai_unsub/cloud/email_fetcher.py src/gmail_ai_unsub/cloud/logging.py main.py"; \
	if [ "$$FUNCTION_EXISTS" -eq 0 ]; then \
		echo "Function doesn't exist, deploying..."; \
		SKIP=0; \
	else \
		echo "Function exists, checking for source changes..."; \
		LAST_DEPLOY=$$(gcloud functions describe gmail-processor --gen2 --region=us-central1 --project=neat-simplicity-486023-a4 --format="value(updateTime)" 2>/dev/null | head -1); \
		if [ -z "$$LAST_DEPLOY" ]; then \
			SKIP=0; \
		else \
			SKIP=1; \
			for file in $$FUNCTION_SOURCE_FILES; do \
				if [ -f "$$file" ]; then \
					FILE_TIME=$$(stat -f "%m" "$$file" 2>/dev/null || stat -c "%Y" "$$file" 2>/dev/null); \
					DEPLOY_TIME=$$(date -j -f "%Y-%m-%dT%H:%M:%S" "$$LAST_DEPLOY" "+%s" 2>/dev/null || date -d "$$LAST_DEPLOY" "+%s" 2>/dev/null); \
					if [ -n "$$FILE_TIME" ] && [ -n "$$DEPLOY_TIME" ] && [ "$$FILE_TIME" -gt "$$DEPLOY_TIME" ]; then \
						echo "  → $$file changed (newer than last deploy)"; \
						SKIP=0; \
					fi; \
				fi; \
			done; \
		fi; \
	fi; \
	if [ "$$SKIP" -eq 1 ]; then \
		echo "✓ No changes detected, skipping deployment"; \
	else \
		echo "Deploying Cloud Function (with verbose logs)..."; \
		gcloud functions deploy gmail-processor \
			--gen2 \
			--runtime=python312 \
			--region=us-central1 \
			--source=. \
			--entry-point=handle_pubsub_event \
			--trigger-topic=gmail-watch \
			--timeout=60s \
			--memory=256Mi \
			--project=neat-simplicity-486023-a4 \
			--set-env-vars="GMAIL_AI_STORAGE_BUCKET=gmail-ai-logs,GMAIL_AI_PROJECT_ID=neat-simplicity-486023-a4" \
			--verbosity=debug; \
		echo ""; \
		echo "Resetting Gmail Watch to ensure single subscription..."; \
		$(MAKE) reset-gmail-watch; \
	fi

deploy-watch-renewal:
	@echo "Checking if watch renewal function needs deployment..."
	@FUNCTION_EXISTS=$$(gcloud functions describe gmail-watch-renewal --gen2 --region=us-central1 --project=neat-simplicity-486023-a4 --format="value(name)" 2>/dev/null | wc -l); \
	FUNCTION_SOURCE_FILES="src/gmail_ai_unsub/cloud/watch_renewal.py main.py"; \
	if [ "$$FUNCTION_EXISTS" -eq 0 ]; then \
		echo "Function doesn't exist, deploying..."; \
		SKIP=0; \
	else \
		echo "Function exists, checking for source changes..."; \
		LAST_DEPLOY=$$(gcloud functions describe gmail-watch-renewal --gen2 --region=us-central1 --project=neat-simplicity-486023-a4 --format="value(updateTime)" 2>/dev/null | head -1); \
		if [ -z "$$LAST_DEPLOY" ]; then \
			SKIP=0; \
		else \
			SKIP=1; \
			for file in $$FUNCTION_SOURCE_FILES; do \
				if [ -f "$$file" ]; then \
					FILE_TIME=$$(stat -f "%m" "$$file" 2>/dev/null || stat -c "%Y" "$$file" 2>/dev/null); \
					DEPLOY_TIME=$$(date -j -f "%Y-%m-%dT%H:%M:%S" "$$LAST_DEPLOY" "+%s" 2>/dev/null || date -d "$$LAST_DEPLOY" "+%s" 2>/dev/null); \
					if [ -n "$$FILE_TIME" ] && [ -n "$$DEPLOY_TIME" ] && [ "$$FILE_TIME" -gt "$$DEPLOY_TIME" ]; then \
						echo "  → $$file changed (newer than last deploy)"; \
						SKIP=0; \
					fi; \
				fi; \
			done; \
		fi; \
	fi; \
	if [ "$$SKIP" -eq 1 ]; then \
		echo "✓ No changes detected, skipping deployment"; \
	else \
		echo "Deploying watch renewal Cloud Function..."; \
		gcloud functions deploy gmail-watch-renewal \
			--gen2 \
			--runtime=python312 \
			--region=us-central1 \
			--source=. \
			--entry-point=renew_watch_http \
			--trigger-http \
			--allow-unauthenticated \
			--timeout=60s \
			--memory=256Mi \
			--project=neat-simplicity-486023-a4 \
			--set-env-vars="GMAIL_AI_STORAGE_BUCKET=gmail-ai-logs,GMAIL_AI_PROJECT_ID=neat-simplicity-486023-a4" \
			--verbosity=debug; \
		echo ""; \
		echo "✓ Watch renewal function deployed!"; \
		echo "Run 'make setup-scheduler' to set up automatic renewal."; \
	fi

setup-queue:
	@echo "Checking Cloud Tasks queue..."
	@QUEUE_EXISTS=$$(gcloud tasks queues describe email-processing --location=us-central1 --project=neat-simplicity-486023-a4 --format="value(name)" 2>/dev/null | wc -l); \
	if [ "$$QUEUE_EXISTS" -eq 0 ]; then \
		echo "Creating Cloud Tasks queue..."; \
		gcloud tasks queues create email-processing \
			--location=us-central1 \
			--project=neat-simplicity-486023-a4 && echo "✓ Queue created"; \
	else \
		echo "✓ Queue already exists"; \
	fi

deploy-run-service:
	@echo "Checking if Cloud Run service needs deployment..."
	@if [ ! -f .env ]; then \
		echo "Error: .env file not found. Create it with ANTHROPIC_API_KEY=..."; \
		exit 1; \
	fi
	@export $$(cat .env | grep -v '^#' | xargs) && \
	if [ -z "$$ANTHROPIC_API_KEY" ]; then \
		echo "Error: ANTHROPIC_API_KEY not found in .env"; \
		exit 1; \
	fi && \
	SERVICE_EXISTS=$$(gcloud run services describe email-processor --region=us-central1 --project=neat-simplicity-486023-a4 --format="value(metadata.name)" 2>/dev/null | wc -l); \
	SERVICE_SOURCE_FILES="src/gmail_ai_unsub/cloud/email_processor.py src/gmail_ai_unsub/cloud/email_fetcher.py src/gmail_ai_unsub/classifier/email_classifier.py main.py"; \
	if [ "$$SERVICE_EXISTS" -eq 0 ]; then \
		echo "Service doesn't exist, deploying..."; \
		SKIP=0; \
	else \
		echo "Service exists, checking for source changes..."; \
		LAST_DEPLOY=$$(gcloud run services describe email-processor --region=us-central1 --project=neat-simplicity-486023-a4 --format="value(status.latestReadyRevisionName)" 2>/dev/null); \
		if [ -z "$$LAST_DEPLOY" ]; then \
			SKIP=0; \
		else \
			LAST_UPDATE=$$(gcloud run revisions describe "$$LAST_DEPLOY" --region=us-central1 --project=neat-simplicity-486023-a4 --format="value(metadata.creationTimestamp)" 2>/dev/null); \
			if [ -z "$$LAST_UPDATE" ]; then \
				SKIP=0; \
			else \
				SKIP=1; \
				for file in $$SERVICE_SOURCE_FILES; do \
					if [ -f "$$file" ]; then \
						FILE_TIME=$$(stat -f "%m" "$$file" 2>/dev/null || stat -c "%Y" "$$file" 2>/dev/null); \
						DEPLOY_TIME=$$(date -j -f "%Y-%m-%dT%H:%M:%S" "$$LAST_UPDATE" "+%s" 2>/dev/null || date -d "$$LAST_UPDATE" "+%s" 2>/dev/null); \
						if [ -n "$$FILE_TIME" ] && [ -n "$$DEPLOY_TIME" ] && [ "$$FILE_TIME" -gt "$$DEPLOY_TIME" ]; then \
							echo "  → $$file changed (newer than last deploy)"; \
							SKIP=0; \
						fi; \
					fi; \
				done; \
			fi; \
		fi; \
	fi; \
	if [ "$$SKIP" -eq 1 ]; then \
		echo "✓ No changes detected, skipping deployment"; \
	else \
		echo "Deploying Cloud Run service (email processor)..."; \
		SERVICE_ACCOUNT=$$(gcloud iam service-accounts list --project=neat-simplicity-486023-a4 --filter="email:*-compute@developer.gserviceaccount.com" --format="value(email)" | head -1) && \
		gcloud run deploy email-processor \
			--source=. \
			--region=us-central1 \
			--platform=managed \
			--timeout=300s \
			--memory=1Gi \
			--cpu=1 \
			--no-allow-unauthenticated \
			--service-account="$$SERVICE_ACCOUNT" \
			--set-env-vars="GMAIL_AI_STORAGE_BUCKET=gmail-ai-logs,GMAIL_AI_PROJECT_ID=neat-simplicity-486023-a4,ANTHROPIC_API_KEY=$$ANTHROPIC_API_KEY" \
			--project=neat-simplicity-486023-a4; \
		echo ""; \
		echo "✓ Cloud Run service deployed!"; \
	fi

deploy: setup-queue deploy-function deploy-watch-renewal deploy-run-service setup-scheduler
	@echo ""
	@echo "✓ All components deployed!"
	@echo ""
	@echo "Next steps:"
	@echo "  1. Ensure Gmail Watch is active: make reset-gmail-watch"
	@echo "  2. Check logs: make check-logs"

setup-scheduler:
	@echo "Checking Cloud Scheduler job..."
	@FUNCTION_URL=$$(gcloud functions describe gmail-watch-renewal --gen2 --region=us-central1 --project=neat-simplicity-486023-a4 --format="value(serviceConfig.uri)" 2>/dev/null); \
	if [ -z "$$FUNCTION_URL" ]; then \
		echo "Error: Watch renewal function not found. Run 'make deploy-watch-renewal' first."; \
		exit 1; \
	fi; \
	JOB_EXISTS=$$(gcloud scheduler jobs describe gmail-watch-renewal --location=us-central1 --project=neat-simplicity-486023-a4 --format="value(name)" 2>/dev/null | wc -l); \
	if [ "$$JOB_EXISTS" -eq 0 ]; then \
		echo "Creating Cloud Scheduler job..."; \
		gcloud scheduler jobs create http gmail-watch-renewal \
			--location=us-central1 \
			--schedule="0 2 * * 0" \
			--uri="$$FUNCTION_URL" \
			--http-method=GET \
			--time-zone="America/Los_Angeles" \
			--project=neat-simplicity-486023-a4; \
		echo ""; \
		echo "✓ Cloud Scheduler job created!"; \
	else \
		CURRENT_URI=$$(gcloud scheduler jobs describe gmail-watch-renewal --location=us-central1 --project=neat-simplicity-486023-a4 --format="value(httpTarget.uri)" 2>/dev/null); \
		if [ "$$CURRENT_URI" != "$$FUNCTION_URL" ]; then \
			echo "Updating Cloud Scheduler job (function URL changed)..."; \
			gcloud scheduler jobs update http gmail-watch-renewal \
				--location=us-central1 \
				--uri="$$FUNCTION_URL" \
				--project=neat-simplicity-486023-a4; \
			echo "✓ Cloud Scheduler job updated!"; \
		else \
			echo "✓ Cloud Scheduler job already configured correctly"; \
		fi; \
	fi; \
	echo ""; \
	echo "Watch will be renewed every Sunday at 2 AM Pacific Time (every 7 days)."; \
	echo ""; \
	echo "To test manually:"; \
	echo "  gcloud scheduler jobs run gmail-watch-renewal --location=us-central1 --project=neat-simplicity-486023-a4"

watch-build:
	@echo "Watching ongoing Cloud Build logs..."
	@BUILD_ID=$$(gcloud builds list --ongoing --limit=1 --format="value(id)" --project=neat-simplicity-486023-a4 2>/dev/null | head -1); \
	if [ -z "$$BUILD_ID" ]; then \
		echo "No ongoing builds found"; \
	else \
		echo "Streaming logs for build: $$BUILD_ID"; \
		gcloud builds log $$BUILD_ID --stream --project=neat-simplicity-486023-a4; \
	fi

setup-gmail-watch:
	@echo "Setting up Gmail Watch..."
	@export $$(cat .env | grep -v '^#' | xargs) && \
	PYTHONPATH=src uv run python -c "\
	import sys; \
	sys.path.insert(0, 'src'); \
	from gmail_ai_unsub.config import Config; \
	from gmail_ai_unsub.gmail.client import GmailClient; \
	config = Config(); \
	client = GmailClient( \
		credentials_file=None, \
		token_file=config.gmail_token_file, \
		use_default_credentials=True, \
	); \
	topic = config.cloud_pubsub_topic; \
	watch_request = { \
		'topicName': topic, \
		'labelIds': ['INBOX'], \
	}; \
	result = client.service.users().watch(userId='me', body=watch_request).execute(); \
	print(f'✓ Gmail Watch active!'); \
	print(f'  History ID: {result.get(\"historyId\")}'); \
	"

reset-gmail-watch:
	@echo "Resetting Gmail Watch (stops all existing, sets up fresh one)..."
	@export $$(cat .env | grep -v '^#' | xargs) && \
	PYTHONPATH=src uv run python scripts/dev.py reset-gmail-watch

test-pubsub-handler:
	@echo "Running Pub/Sub handler tests..."
	@PYTHONPATH=src uv run pytest tests/test_pubsub_handler.py -v

test-email-processor:
	@echo "Running email processor tests..."
	@PYTHONPATH=src uv run pytest tests/test_email_processor.py -v

validate: validate-dashboard test-pubsub-handler test-email-processor
	@echo "✓ All validation passed"
