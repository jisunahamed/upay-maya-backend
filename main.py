"""Maya/upay demo backend – bootstrap plus a read-only Supabase connection check.

No customer or payment details are exposed by the public HTTP endpoints.
Do not add financial-data endpoints without authentication and call-specific
identity verification enforced in backend code.
"""

import os
import logging

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

# Configure logging for diagnostics (server-side only)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Maya Demo Backend",
    description="Railway bootstrap with Supabase connectivity check only.",
    version="0.2.1",
)


@app.get("/")
def root():
    return {"service": "upay-maya-backend", "stage": "database-check", "status": "running"}


@app.get("/health")
def health():
    return {"status": "ok"}


def _build_supabase_headers(key: str) -> dict[str, str]:
    """Build correct Supabase headers for both legacy and new key formats.

    - Legacy service-role JWT keys: use 'apikey' header
    - New secret keys (sb_secret_*): use 'Authorization: Bearer' header
    Both formats also accept the 'apikey' header for backwards compatibility,
    but new keys work more reliably with Authorization header.
    """
    headers = {"Accept": "application/json"}
    # Support both key formats: use Authorization for sb_secret_ keys,
    # apikey for legacy JWT keys. Include both for maximum compatibility.
    if key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {key}"
    headers["apikey"] = key
    return headers


def _categorize_error(exc: Exception, response: httpx.Response | None = None) -> str:
    """Categorize error for safe diagnostic logging (no secrets)."""
    if response is not None:
        if response.status_code == 401:
            return "unauthorized"
        if response.status_code == 403:
            return "forbidden"
        if response.status_code == 404:
            return "not_found"
        if 500 <= response.status_code < 600:
            return "server_error"
        return f"http_{response.status_code}"
    if isinstance(exc, httpx.ConnectTimeout):
        return "connect_timeout"
    if isinstance(exc, httpx.ReadTimeout):
        return "read_timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connection_error"
    if isinstance(exc, httpx.DNSError):
        return "dns_error"
    if isinstance(exc, httpx.HTTPError):
        return "http_error"
    return type(exc).__name__


@app.get("/health/db")
async def database_health():
    """Verify Supabase credentials via a read-only, limited REST query.

    Intentionally returns no customer data, credentials, URLs or raw errors.
    This is a connectivity check, not an authorization or identity test.
    """
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_SECRET_KEY", "").strip()
    if not url or not key:
        logger.warning("Supabase health check: missing environment variables")
        return JSONResponse(status_code=503, content={"database": "not_configured"})
    if not url.startswith("https://") or not url.endswith(".supabase.co"):
        logger.warning("Supabase health check: invalid URL format")
        return JSONResponse(status_code=503, content={"database": "invalid_configuration"})

    headers = _build_supabase_headers(key)
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            response = await client.get(
                f"{url}/rest/v1/customers",
                params={"select": "customer_id", "limit": "1"},
                headers=headers,
            )
    except httpx.HTTPError as exc:
        error_category = _categorize_error(exc)
        logger.warning("Supabase health check failed: %s", error_category)
        return JSONResponse(status_code=503, content={"database": "unreachable"})

    if response.status_code == 200:
        logger.info("Supabase health check: connected")
        return {"database": "connected"}

    error_category = _categorize_error(None, response)
    logger.warning("Supabase health check failed: %s (status=%d)", error_category, response.status_code)
    return JSONResponse(status_code=503, content={"database": "connection_failed"})
