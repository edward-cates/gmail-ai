.PHONY: help validate-dashboard test-dashboard run-dashboard check-logs deploy-function watch-build setup-gmail-watch reset-gmail-watch test-pubsub-handler validate

help:
	@echo "Available commands:"
	@echo "  make validate           - Run all validation (dashboard + pubsub handler tests)"
	@echo "  make validate-dashboard  - Check dashboard for errors/warnings"
	@echo "  make test-dashboard     - Test dashboard API endpoints"
	@echo "  make test-pubsub-handler - Run Pub/Sub handler tests with mocking"
	@echo "  make run-dashboard      - Run dashboard locally"
	@echo "  make check-logs         - Check recent Cloud Storage logs"
	@echo "  make deploy-function     - Deploy Cloud Function (with verbose logs)"
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
	@PYTHONPATH=src uv run python -c "\
	import sys; \
	sys.path.insert(0, 'src'); \
	from gmail_ai_unsub.dashboard.app import get_logs_from_storage; \
	from gmail_ai_unsub.config import Config; \
	import json; \
	config = Config(); \
	logs = get_logs_from_storage(config.cloud_storage_bucket, config.cloud_project_id, hours=24); \
	print(f'Found {len(logs)} logs in last 24 hours'); \
	for i, log in enumerate(logs[:5]): \
		print(f\"\\n{i+1}. {log.get('timestamp', '')[:19]}: {log.get('stage')} - {log.get('result')}\"); \
		if log.get('metadata', {}).get('subject'): \
			print(f\"   Subject: {log['metadata']['subject']}\"); \
	"

deploy-function:
	@echo "Deploying Cloud Function (with verbose logs)..."
	@gcloud functions deploy gmail-processor \
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
		--verbosity=debug
	@echo ""
	@echo "Resetting Gmail Watch to ensure single subscription..."
	@$(MAKE) reset-gmail-watch

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

validate: validate-dashboard test-pubsub-handler
	@echo "✓ All validation passed"
