# upay Maya AI Demo — FastAPI Backend

This is a synthetic-data demonstration backend. It provides health checks and
an authenticated Vapi Function Tool endpoint for a verification-gated payment
status lookup. It does not initiate calls or perform financial transactions.

## Run locally

```bash
python -m pip install -r requirements.txt
uvicorn main:app --reload
```

Open `http://127.0.0.1:8000/health` to confirm `{"status":"ok"}`.

## Environment variables

- `SUPABASE_URL`: Supabase project URL.
- `SUPABASE_SECRET_KEY`: Backend-only `sb_secret_` key.
- `VAPI_WEBHOOK_SECRET`: Random value of at least 32 characters, configured as
  a Vapi Bearer credential for `POST /vapi/tools`.
- `ENABLE_DEMO_OPERATOR_VERIFICATION`: Set to `true` only while running an
  authorized synthetic-data verification test.
- `OPERATOR_API_SECRET`: Separate random value of at least 32 characters for
  the demo-only operator approval endpoint.
- `TRANSCRIPT_STORAGE_ENABLED`: Global transcript/message persistence switch;
  keep `false` until the retention and consent policy is approved.
- `TRANSCRIPT_RETENTION_DAYS`: Approved retention period from 1 to 3650 days.
  No default policy is assumed.
- `STORE_RECORDING_REFERENCES`: Stores an HTTPS recording reference only when
  enabled and Vapi reports explicit recording consent through `grantedAt`.
- `STORE_CALLER_PHONE`: Stores the caller number in `call_logs` only when the
  approved policy allows it.

Never reuse the Vapi or operator secrets for Supabase, Twilio, or another
service.

## Secure payment lookup flow

1. A trusted system records the Vapi call ID, customer ID, and payment
   reference in `call_logs`.
2. An authorized operator completes an approved synthetic demo challenge and
   invokes `POST /operator/demo/verify-call`.
3. The backend records verification in `call_identity_verifications`.
4. Vapi calls `POST /vapi/tools` with its saved Bearer credential.
5. The backend requires the call association and verification record before it
   reads the matching `payment_records` row.

The operator endpoint accepts only `DEMO-*` identifiers and is disabled by
default. Do not turn it into a model-callable Vapi tool.

## Call history webhook

`POST /vapi/events` accepts authenticated Vapi `end-of-call-report` events.
It upserts call metadata by Vapi Call ID and, when the explicit storage policy
allows it, stores one canonical transcript plus ordered customer/assistant
messages. System prompts, tool payloads, partial transcripts, and common secret
patterns are excluded or redacted before persistence.

Run `migrations/001_vapi_call_history.sql` in the Supabase SQL Editor before
enabling the event route. It adds the `call_messages` table and two additive
columns to `call_transcripts`; it does not drop or truncate data.

Per-call transcript permission must come from trusted call metadata:

```json
{"metadata":{"customerId":"DEMO-UPAY-001","transcriptStoragePermitted":true}}
```

Do not set `transcriptStoragePermitted` from model output. Recording consent is
checked separately and does not automatically authorize transcript storage.

Vapi does not retry server messages by default. Configure the saved assistant
server with an API-managed bounded `backoffPlan`, for example exponential
backoff with `maxRetries: 3` and non-retryable `400`, `401`, `403`, and `404`
status codes. The database writes are idempotent so duplicate deliveries do not
duplicate transcripts or messages.

## Deploy on Railway

Railway deploys `main` using `railway.json`. Verify `/`, `/health`, and
`/health/db` after every deployment. An unauthenticated request to
`POST /vapi/tools` must return HTTP 401 once `VAPI_WEBHOOK_SECRET` is set.

## Security

- GitHub repository may be public, but **never commit** Twilio Auth Token, Vapi private key, Supabase service role/secret keys, `.env`, or real customer records.
- Keep all credentials only in Railway/Vapi secret storage.
- Do not disclose balances before backend verification succeeds.
- Do not turn on real customer calling until identity verification, consent,
  privacy policy, and company procedures are approved and tested.
