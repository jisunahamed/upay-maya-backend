# upay Maya AI Demo — FastAPI Backend

This is a synthetic-data demonstration backend. It provides health checks and
an authenticated Vapi Function Tool endpoint for a verification-gated payment
status lookup and a disabled-by-default, admin-only outbound call endpoint. It
does not initiate calls automatically or perform financial transactions.

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
- `ENABLE_OUTBOUND_CALLS`: Fail-closed switch for the admin-only outbound
  endpoint. Keep `false` until an authorized synthetic test.
- `OUTBOUND_ADMIN_SECRET`: Separate Bearer secret for
  `POST /operator/demo/outbound-calls`; never reuse the webhook secret.
- `VAPI_PRIVATE_API_KEY`: Backend-only Vapi private API key. Never expose it
  to a browser, assistant prompt, tool argument, log, or repository.
- `VAPI_ASSISTANT_ID`: Must remain Maya's saved assistant ID.
- `VAPI_PHONE_NUMBER_ID`: Vapi ID for the existing imported Twilio number.
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

1. `get_customer_profile` prefers the authenticated Vapi webhook's
   `message.call.customer.number`, normalizes complete Bangladesh mobile
   numbers to E.164, and records an unverified call/customer candidate
   association. A conflicting model-generated phone is ignored.
2. A trusted system records the payment reference for that call. Phone
   possession and profile lookup alone never authorize financial disclosure.
3. An authorized operator completes an approved synthetic demo challenge and
   invokes `POST /operator/demo/verify-call`.
4. The backend records verification in `call_identity_verifications`.
5. Vapi calls `POST /vapi/tools` with its saved Bearer credential.
6. The backend requires the call association and verification record before it
   reads the matching `payment_records` row.

The operator endpoint accepts only `DEMO-*` identifiers and is disabled by
default. It refuses calls that do not already have a trusted customer
association. Do not turn it into a model-callable Vapi tool.

For an authorized future outbound-call API, pass a non-sensitive reason in
Vapi's supported per-call override without changing the saved assistant:

```json
{
  "assistantOverrides": {
    "variableValues": {"call_reason": "payment_reminder"}
  }
}
```

The backend accepts only an allowlisted reason from trusted call context. It
does not currently initiate calls, and no amount or due date belongs in the
initial greeting.

## Consent-gated outbound initiation

The active consent authority is one `customer_contact_preferences` row per
customer. It must contain `voice_call_consent=true` and `opted_out=false`;
missing or duplicate rows fail closed. Migration `002_outbound_call_consent.sql`
is additive legacy schema support only and does not modify existing consent.

`POST /operator/demo/outbound-calls` is not a Vapi tool. It accepts only a
`DEMO-*` customer ID, reads the registered phone from Supabase, requires
`contact_consent=true`, `voice_call_consent=true`, and `opted_out=false`, then
uses Vapi `POST /call` with Maya v6 and a per-call outbound greeting override.
It remains unavailable unless `ENABLE_OUTBOUND_CALLS=true` and all private
server configuration is valid.

The outbound endpoint must be invoked only after a separate explicit approval.
Do not call it from Composer, the assistant, a public client, or automated test.
If Vapi accepts a call but the subsequent audit association fails, the endpoint
returns `association_status=failed_do_not_retry`; never retry that response,
because the first call may already be queued.

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
