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
