"""Maya/upay demo backend – bootstrap plus a read-only Supabase connection check.

No customer or payment details are exposed by the public HTTP endpoints.
Do not add financial-data endpoints without authentication and call-specific
identity verification enforced in backend code.
"""

import logging
import os
import socket
import ssl

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
    """Build Data API headers without treating opaque API keys as JWTs.

    Supabase's new ``sb_secret_`` keys belong only in ``apikey``. Legacy
    JWT-based service-role keys are also sent as bearer tokens so PostgREST can
    assume the embedded role.
    """
    headers = {"Accept": "application/json", "apikey": key}
    if key.count(".") == 2 and key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _categorize_error(exc: Exception | None = None, response: httpx.Response | None = None) -> str:
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
    cause = exc
    while cause is not None:
        if isinstance(cause, socket.gaierror):
            return "dns_error"
        if isinstance(cause, ssl.SSLError):
            return "tls_error"
        cause = cause.__cause__
    if isinstance(exc, httpx.ConnectError):
        return "connection_error"
    if isinstance(exc, httpx.RemoteProtocolError):
        return "invalid_http_response"
    if isinstance(exc, httpx.HTTPError):
        return "http_error"
    return type(exc).__name__ if exc is not None else "unknown_error"


def _has_wrapping_quotes(value: str) -> bool:
    return len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}


@app.get("/health/db")
async def database_health():
    """Verify Supabase credentials via a read-only, limited REST query.

    Intentionally returns no customer data, credentials, URLs or raw errors.
    This is a connectivity check, not an authorization or identity test.
    """
    raw_url = os.getenv("SUPABASE_URL", "")
    raw_key = os.getenv("SUPABASE_SECRET_KEY", "")
    url = raw_url.strip().rstrip("/")
    key = raw_key.strip()
    if not url or not key:
        logger.warning("operation=supabase_health category=missing_configuration")
        return JSONResponse(status_code=503, content={"database": "not_configured"})
    if (
        raw_url != raw_url.strip()
        or raw_key != raw_key.strip()
        or _has_wrapping_quotes(url)
        or _has_wrapping_quotes(key)
        or not url.startswith("https://")
        or not url.endswith(".supabase.co")
    ):
        logger.warning("operation=supabase_health category=invalid_configuration")
        return JSONResponse(status_code=503, content={"database": "invalid_configuration"})

    headers = _build_supabase_headers(key)
    try:
        timeout = httpx.Timeout(connect=5.0, read=8.0, write=5.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(
                f"{url}/rest/v1/customers",
                params={"select": "customer_id", "limit": "1"},
                headers=headers,
            )
    except httpx.HTTPError as exc:
        error_category = _categorize_error(exc)
        logger.warning(
            "operation=supabase_health category=%s exception=%s",
            error_category,
            type(exc).__name__,
        )
        return JSONResponse(status_code=503, content={"database": "unreachable"})
    except Exception as exc:
        logger.warning(
            "operation=supabase_health category=unexpected_error exception=%s",
            type(exc).__name__,
        )
        return JSONResponse(status_code=503, content={"database": "connection_failed"})

    if response.status_code == 200:
        logger.info("operation=supabase_health category=connected status=200")
        return {"database": "connected"}

    error_category = _categorize_error(response=response)
    logger.warning(
        "operation=supabase_health category=%s status=%d",
        error_category,
        response.status_code,
    )
    return JSONResponse(status_code=503, content={"database": "connection_failed"})
