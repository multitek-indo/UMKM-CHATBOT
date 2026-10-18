# UMKM Suite v0.7.10 - WhatsApp NLU follow-up fixes

Release scope: WhatsApp bridge NLU plus the currently deployed core sources.
Telegram/core version on server: 0.7.6-boss-assistant (not silently renamed to v0.7.10).

## Changes represented

- Hybrid natural-language understanding and contextual finance date resolution.
- Follow-up PDF/Excel exports use explicit or verified conversation context.
- Export of all posted bookkeeping transactions uses the authorized tenant scope.
- Reminder-list inquiries are read-only, not converted into reminder creation.
- Draft and transaction confirmation guards remain active.
- Groq remains the configured primary AI; Gemini fallback is optional.

## Important

- This is a versioned, source-only snapshot, not an automatic production deploy.
- Functional validation of full WhatsApp flows must be performed separately.
- No credentials, customer messages, database dumps, or session data included.
