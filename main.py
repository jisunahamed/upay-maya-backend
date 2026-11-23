"""Maya / upay demo backend bootstrap.

This intentionally exposes only public health endpoints. Financial/customer APIs
and Vapi webhooks should be added only after backend authentication, identity
verification, and database access controls are implemented.
"""

from fastapi import FastAPI

app = FastAPI(
    title="Maya Demo Backend",
    description="Initial Railway deployment check; no customer-data endpoints yet.",
    version="0.1.0",
)


@app.get("/")
def root():
    return {"service": "upay-maya-backend", "stage": "bootstrap", "status": "running"}


@app.get("/health")
def health():
    return {"status": "ok"}
