"""Admin-only, consent-gated outbound calling through Vapi's Create Call API."""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from vapi_tools import DatabaseUnavailable, _supabase_get, _supabase_write, normalize_bangladesh_phone

logger = logging.getLogger(__name__)
router = APIRouter()

MAYA_ASSISTANT_ID = "1f4fab2a-20f4-4fb5-93cd-88296270a89b"
MAYA_ASSISTANT_VERSION = "v7"
OUTBOUND_CALL_REASON = "payment_reminder"
OUTBOUND_GREETING = (
    "আসসালামু আলাইকুম। আমি মায়া, একটি AI ডেমো কলিং সিস্টেম থেকে বলছি। "
    "আপনার অ্যাকাউন্ট-সংক্রান্ত একটি আপডেট নিয়ে কথা বলতে কল করেছি। "
    "এখন কি একটু কথা বলা যাবে?"
)
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class OutboundCallRequest(BaseModel):
    customer_id: str = Field(min_length=1, max_length=64, pattern=r"^DEMO-[A-Za-z0-9-]+$")


def _authenticate_outbound_admin(request: Request) -> None:
    if os.getenv("ENABLE_OUTBOUND_CALLS", "").lower() != "true":
        raise HTTPException(status_code=404, detail="Not found")
    expected = os.getenv("OUTBOUND_ADMIN_SECRET", "")
    if not expected or expected != expected.strip() or len(expected) < 32:
        logger.error("operation=outbound_call category=authentication_not_configured")
        raise HTTPException(status_code=503, detail="Outbound authentication is not configured")
    authorization = request.headers.get("authorization", "")
    scheme, separator, supplied = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not hmac.compare_digest(supplied, expected):
        logger.warning("operation=outbound_call category=authentication_failed")
        raise HTTPException(status_code=401, detail="Unauthorized")


def _vapi_config() -> tuple[str, str, str]:
    key = os.getenv("VAPI_PRIVATE_API_KEY", "")
    assistant_id = os.getenv("VAPI_ASSISTANT_ID", MAYA_ASSISTANT_ID)
    phone_number_id = os.getenv("VAPI_PHONE_NUMBER_ID", "")
    if (
        not key
        or key != key.strip()
        or len(key) < 32
        or assistant_id != MAYA_ASSISTANT_ID
        or not _UUID.fullmatch(assistant_id)
        or not _UUID.fullmatch(phone_number_id)
    ):
        raise HTTPException(status_code=503, detail="Outbound calling is not configured")
    return key, assistant_id, phone_number_id


def build_vapi_outbound_payload(phone: str, assistant_id: str, phone_number_id: str) -> dict[str, Any]:
    """Build the exact per-call override on the published Maya v7."""
    return {
        "assistantId": assistant_id,
        "assistantVersion": MAYA_ASSISTANT_VERSION,
        "phoneNumberId": phone_number_id,
        "customer": {"number": phone},
        "assistantOverrides": {
            "firstMessageMode": "assistant-speaks-first",
            "firstMessage": OUTBOUND_GREETING,
            "variableValues": {"call_reason": OUTBOUND_CALL_REASON},
        },
    }


async def _create_vapi_call(payload: dict[str, Any], private_key: str) -> dict[str, Any]:
    timeout = httpx.Timeout(connect=5.0, read=20.0, write=8.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(
                "https://api.vapi.ai/call",
                json=payload,
                headers={
                    "Authorization": f"Bearer {private_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        logger.warning(
            "operation=outbound_call category=vapi_transport_error exception=%s",
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="Call provider is unavailable") from exc
    if response.status_code not in {200, 201}:
        logger.warning(
            "operation=outbound_call category=vapi_rejected status=%d",
            response.status_code,
        )
        raise HTTPException(status_code=502, detail="Call provider rejected the request")
    try:
        result = response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="Invalid call provider response") from exc
    if not isinstance(result, dict) or not isinstance(result.get("id"), str):
        raise HTTPException(status_code=502, detail="Invalid call provider response")
    return result


@router.post("/operator/demo/outbound-calls", status_code=201)
async def initiate_demo_outbound_call(request: Request) -> dict[str, str]:
    """Create one explicitly authorized demo call; never invoked by Vapi tools."""
    _authenticate_outbound_admin(request)
    try:
        body = OutboundCallRequest.model_validate(await request.json())
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail="Invalid outbound call request") from exc

    customers = await _supabase_get(
        "customers",
        {
            "select": "customer_id,registered_phone,account_status,contact_consent",
            "customer_id": f"eq.{body.customer_id}",
            "limit": "2",
        },
    )
    if len(customers) != 1 or customers[0].get("account_status") != "demo":
        raise HTTPException(status_code=404, detail="Eligible demo customer not found")
    customer = customers[0]
    # Voice consent is maintained independently from the customer profile.
    # Missing or duplicate preference rows must fail closed.
    preferences = await _supabase_get(
        "customer_contact_preferences",
        {
            "select": "customer_id,voice_call_consent,opted_out",
            "customer_id": f"eq.{body.customer_id}",
            "limit": "2",
        },
    )
    if len(preferences) != 1 or preferences[0].get("customer_id") != body.customer_id:
        raise HTTPException(status_code=409, detail="Voice call consent requirements are not satisfied")
    preference = preferences[0]
    if (
        customer.get("contact_consent") is not True
        or preference.get("voice_call_consent") is not True
        or preference.get("opted_out") is not False
    ):
        raise HTTPException(status_code=409, detail="Voice call consent requirements are not satisfied")
    phone = normalize_bangladesh_phone(customer.get("registered_phone"))
    if phone is None:
        raise HTTPException(status_code=409, detail="Customer phone number is invalid")

    payments = await _supabase_get(
        "payment_records",
        {
            "select": "customer_id,payment_reference,payment_status",
            "customer_id": f"eq.{body.customer_id}",
            "payment_status": "eq.outstanding",
            "order": "due_date.asc",
            "limit": "2",
        },
    )
    if len(payments) != 1 or payments[0].get("customer_id") != body.customer_id:
        raise HTTPException(status_code=409, detail="A unique outstanding demo payment was not found")

    private_key, assistant_id, phone_number_id = _vapi_config()
    payload = build_vapi_outbound_payload(phone, assistant_id, phone_number_id)
    created = await _create_vapi_call(payload, private_key)
    call_id = created["id"]
    status = created.get("status") if isinstance(created.get("status"), str) else "queued"
    try:
        written = await _supabase_write(
            "POST",
            "call_logs",
            params={"on_conflict": "vapi_call_id"},
            payload={
                "vapi_call_id": call_id,
                "customer_id": body.customer_id,
                "phone_number": phone,
                "call_direction": "outbound",
                "call_reason": OUTBOUND_CALL_REASON,
                "call_status": status,
                "identity_verified": False,
                "structured_output": {
                    "payment_reference": payments[0]["payment_reference"]
                },
            },
            prefer="resolution=ignore-duplicates,return=representation",
        )
    except DatabaseUnavailable:
        written = []
    if len(written) != 1:
        # The provider has already accepted the call. A retry here could create a
        # second real call, so acknowledge it and explicitly report the missing
        # association instead of returning a retryable server error.
        logger.error(
            "operation=outbound_call category=association_write_failed call_id=%s",
            call_id,
        )
        return {
            "status": status,
            "vapi_call_id": call_id,
            "customer_id": body.customer_id,
            "association_status": "failed_do_not_retry",
        }
    logger.info("operation=outbound_call category=created call_id=%s", call_id)
    return {
        "status": status,
        "vapi_call_id": call_id,
        "customer_id": body.customer_id,
        "association_status": "recorded_unverified",
    }
