# Plan: move the email classifier to OpenAI's Decisions API

**TL;DR:** Swap `classify_email` in `cloud-run/email-processor/main.py` from a Claude Opus 4.6
prompt that returns free-form JSON to a single OpenAI Decisions `choice` question. It's about 40
lines of code plus one new secret. Run both classifiers side by side for a few days before
switching, because a wrong `marketing`/`noti`/`newsletter` pick archives real mail. The newsletter
summarizer stays on Claude.

## Why

- **No parsing failures.** Today Claude writes JSON, and we strip code fences and `json.loads`
  it. Anything malformed silently becomes `other` with confidence 0. Decisions can only return
  one of the values we pass in.
- **Real confidence.** Claude's `"confidence"` is a number it writes about itself. Decisions
  returns a probability for every category, so a threshold actually means something.
- **Faster and cheaper.** Each call takes about 100–600 ms (measured from the budget app). Pricing
  is $0.10 per 1M input tokens and output tokens are free, versus an Opus call per email today.

## What changes

### 1. `classify_email` (cloud-run/email-processor/main.py)

The new version has the same signature and the same return shape (`category`, `confidence`,
`reason`), so `main()` doesn't change.

- Request: `POST https://api.openai.com/v1/decisions`, `model: "gpt-6-luna"`.
- `input`: `From: … / Subject: … / Body: …` as one string. Body can go to about 4000 characters
  (from 2000) since input is cheap.
- One question: `{"type": "choice", "name": "category", "instructions": "Classify this email by
  its PRIMARY PURPOSE.", "choices": [...]}`.
- `choices`: the five existing categories, with **today's prompt text moved over unchanged** as
  each `description`, including the Homeroom kindergarten exception and the "sneaky ToS changes"
  rule.
- **Safety threshold:** if the answer is a `refusal`, or `confidence < 0.6`, return `other`. Any
  other category archives or relabels the email, so "unsure" must mean "leave it in the inbox."
- `reason`: Decisions doesn't explain itself, so put the top-3 `probabilities` there instead. It
  still shows up in the structured logs.
- Use stdlib `urllib.request` (or `requests`) with retry on 429/503. No new dependency is needed.

### 2. Flag for rollout

Read `CLASSIFIER` from the environment: `claude` (default), `shadow`, or `decisions`.

- `shadow`: act on Claude's answer, and also call Decisions and log both, with
  `stage: "classifier_shadow"`, `claude`, `decisions`, `agree` and probabilities.
- `decisions`: act on the Decisions answer.

### 3. Secret and deploy

1. Add `OPENAI_API_KEY=sk-...` to `.env` (the same key the budget app uses works).
2. `./scripts/sync_secret.sh OPENAI_API_KEY openai-api-key`
3. Makefile `deploy-email-processor`: extend `--set-secrets` to
   `ANTHROPIC_API_KEY=anthropic-api-key:latest,OPENAI_API_KEY=openai-api-key:latest`, and add
   `CLASSIFIER=shadow` to `--set-env-vars`.
4. Add `requests` to `cloud-run/email-processor/requirements.txt` only if we use it.
5. Push to main. GitHub Actions runs `make deploy-email-processor` when that folder changes.

### 4. Tests

The email processor has only a syntax check (`make test-email-processor`). Add
`tests/test_email_classifier.py` with the HTTP call mocked:

- maps a `choice` answer to the category
- refusal → `other`
- low confidence → `other`
- HTTP error → raises (so the job exits non-zero, same as today)

Run `make validate`.

## Rollout

1. Deploy with `CLASSIFIER=shadow` and let it run 3–5 days.
2. Pull disagreements from Cloud Logging (`stage="classifier_shadow" AND agree=false`) and check
   them by hand. Watch the fine-grained cases: Homeroom, credit-monitoring alerts, recruiter vs.
   job-board emails, and ToS updates.
3. If Decisions is as good or better, flip to `CLASSIFIER=decisions`. If a category keeps losing,
   tune its `description` or the threshold first.
4. After a week of clean runs, remove the Claude classification path (keep Claude for
   `summarize_newsletter`).

## Risks

- **Fine-grained rules.** The model decides from category descriptions alone, with no
  step-by-step reasoning. The Homeroom and ToS exceptions are where it's most likely to slip,
  which is why the shadow period comes first.
- **Beta API.** Decisions is in public beta (GA "in the coming weeks"). If calls start failing,
  the `claude` flag is the instant rollback.
- **Cost of a mistake is asymmetric.** Archiving a real email is worse than leaving a promo in
  the inbox. The 0.6 threshold leans toward that.

Docs: https://platform.openai.com/docs/guides/decisions
