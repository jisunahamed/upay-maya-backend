# upay Maya AI Demo — FastAPI Starter

This is a **demo bootstrap**, not a deployed financial-services backend. It only provides `GET /` and `GET /health`. It does not handle customer data, Vapi tool calls, payments, or automatic dialing yet.

## Run locally

```bash
python -m pip install -r requirements.txt
uvicorn main:app --reload
```

Open `http://127.0.0.1:8000/health` to confirm `{"status":"ok"}`.

## Deploy on Railway

1. Upload the **extracted files** (not this ZIP archive) to the root of your GitHub repository.
2. Railway service > Settings > Source > **Connect Repo** > select your repository.
3. Apply and deploy the change. Railway will use `railway.json` as the start configuration.
4. Once deployment is successful, Settings > Networking > **Generate Domain**.
5. Open `https://YOUR-RAILWAY-DOMAIN/health` and check for `{"status":"ok"}`.

## Security

- GitHub repository may be public, but **never commit** Twilio Auth Token, Vapi private key, Supabase service role/secret keys, `.env`, or real customer records.
- Add credentials only later in **Railway > Variables**, after endpoints that need them are implemented.
- Do not turn on real customer calling until identity verification, consent, privacy policy, and authenticated backend tools are implemented and tested.
