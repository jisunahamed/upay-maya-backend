"""Maya/upay demo backend – bootstrap plus a read-only Supabase connection check.

No customer or payment details are exposed by the public HTTP endpoints.
Do not add financial-data endpoints without authentication and call-specific
identity verification enforced in backend code.
"""

import os

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(
    title="Maya Demo Backend",
    description="Railway bootstrap with Supabase connectivity check only.",
    version="0.2.0",
)


@app.get("/")
def root():
    return {"service": "upay-maya-backend", "stage": "database-check", "status": "running"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/db")
async def database_health():
    """Verify Supabase credentials via a read-only, limited REST query.

    Intentionally returns no customer data, credentials, URLs or raw errors.
    This is a connectivity check, not an authorization or identity test.
    """
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_SECRET_KEY", "").strip()
    if not url or not key:
        return JSONResponse(status_code=503, content={"database": "not_configured"})
    if not url.startswith("https://") or not url.endswith(".supabase.co"):
        return JSONResponse(status_code=503, content={"database": "invalid_configuration"})

    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            response = await client.get(
                f"{url}/rest/v1/customers",
                params={"select": "customer_id", "limit": "1"},
                headers={"apikey": key, "Accept": "application/json"},
            )
    except httpx.HTTPError:
        return JSONResponse(status_code=503, content={"database": "unreachable"})

    if response.status_code == 200:
        return {"database": "connected"}
    return JSONResponse(status_code=503, content={"database": "connection_failed"})
