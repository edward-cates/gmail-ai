# Plan

## Current State

- [x] Gmail Watch → Pub/Sub → Cloud Function
- [x] Cloud Function triggers Cloud Run Job
- [x] Classification (marketing, newsletter, noti, other)
- [x] Marketing/noti: label + archive
- [x] Newsletter: summarize → email summary → archive
- [x] Skip app emails (🤖 prefix)
- [ ] Unsubscribe service integration

## Architecture

```
Gmail → Pub/Sub → Cloud Function → Cloud Run Job (email-processor)
                                         ↓
                              Fetch → Classify → Act
                                         ↓
                    ┌─────────────────┬─────────────────┐
                    ↓                 ↓                 ↓
              marketing/noti     newsletter          other
              (label+archive)  (summarize+email)   (nothing)
```

## Next

- Wire up unsubscribe-service for marketing emails
- Dashboard improvements
