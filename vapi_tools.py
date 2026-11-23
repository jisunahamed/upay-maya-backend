"""Authenticated Vapi Function Tool handling for the Maya demo.

Financial data is returned only when the Vapi call is associated with a
customer and a separate server-side verification record confirms identity.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)
router = APIRouter()


class ToolFailure(Exception):
    """A safe, user-facing tool-level failure."""


class DatabaseUnavailable(Exception):
    """Supabase could not complete a trusted lookup."""


class DemoVerificationRequest(BaseModel):
    vapi_call_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    customer_id: str = Field(min_length=1, max_length=64, pattern=r"^DEMO-[A-Za-z0-9-]+$")
    payment_reference: str = Field(min_length=1, max_length=64, pattern=r"^DEMO-[A-Za-z0-9-]+$")


def _authenticate_vapi(request: Request) -> None:
    expected = os.getenv("VAPI_WEBHOOK_SECRET", "")
    if not expected or expected != expected.strip() or len(expected) < 32:
        logger.error("operation=vapi_tools category=authentication_not_configured")
        raise HTTPException(status_code=503, detail="Webhook authentication is not configured")

    authorization = request.headers.get("authorization", "")
    scheme, separator, supplied = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not hmac.compare_digest(supplied, expected):
        logger.warning("operation=vapi_tools category=authentication_failed")
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _authenticate_operator(request: Request) -> None:
    if os.getenv("ENABLE_DEMO_OPERATOR_VERIFICATION", "").lower() != "true":
        raise HTTPException(status_code=404, detail="Not found")
    expected = os.getenv("OPERATOR_API_SECRET", "")
    if not expected or expected != expected.strip() or len(expected) < 32:
        logger.error("operation=demo_verification category=authentication_not_configured")
        raise HTTPException(status_code=503, detail="Operator authentication is not configured")
    authorization = request.headers.get("authorization", "")
    scheme, separator, supplied = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not hmac.compare_digest(supplied, expected):
        logger.warning("operation=demo_verification category=authentication_failed")
        raise HTTPException(status_code=401, detail="Unauthorized")


def _supabase_config() -> tuple[str, str]:
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_SECRET_KEY", "")
    if (
        not url
        or not key
        or url != url.strip()
        or key != key.strip()
        or not url.startswith("https://")
        or not url.rstrip("/").endswith(".supabase.co")
    ):
        raise DatabaseUnavailable
    return url.rstrip("/"), key


def _supabase_headers(key: str) -> dict[str, str]:
    headers = {"Accept": "application/json", "apikey": key}
    if key.count(".") == 2 and key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    return headers


async def _supabase_get(table: str, params: dict[str, str]) -> list[dict[str, Any]]:
    url, key = _supabase_config()
    timeout = httpx.Timeout(connect=5.0, read=8.0, write=5.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(
                f"{url}/rest/v1/{table}",
                params=params,
                headers=_supabase_headers(key),
            )
    except httpx.HTTPError as exc:
        logger.warning(
            "operation=vapi_database_lookup category=transport_error exception=%s",
            type(exc).__name__,
        )
        raise DatabaseUnavailable from exc

    if response.status_code != 200:
        logger.warning(
            "operation=vapi_database_lookup category=upstream_error table=%s status=%d",
            table,
            response.status_code,
        )
        raise DatabaseUnavailable

    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        logger.warning("operation=vapi_database_lookup category=invalid_json table=%s", table)
        raise DatabaseUnavailable from exc
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        logger.warning("operation=vapi_database_lookup category=invalid_shape table=%s", table)
        raise DatabaseUnavailable
    return payload


async def _supabase_write(
    method: str,
    table: str,
    *,
    params: dict[str, str],
    payload: dict[str, Any],
    prefer: str = "return=representation",
) -> list[dict[str, Any]]:
    url, key = _supabase_config()
    headers = _supabase_headers(key)
    headers.update({"Content-Type": "application/json", "Prefer": prefer})
    timeout = httpx.Timeout(connect=5.0, read=8.0, write=5.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.request(
                method,
                f"{url}/rest/v1/{table}",
                params=params,
                json=payload,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        logger.warning(
            "operation=demo_verification_database category=transport_error exception=%s",
            type(exc).__name__,
        )
        raise DatabaseUnavailable from exc
    if response.status_code not in {200, 201}:
        logger.warning(
            "operation=demo_verification_database category=upstream_error table=%s status=%d",
            table,
            response.status_code,
        )
        raise DatabaseUnavailable
    try:
        result = response.json()
    except json.JSONDecodeError as exc:
        raise DatabaseUnavailable from exc
    if not isinstance(result, list) or not all(isinstance(row, dict) for row in result):
        raise DatabaseUnavailable
    return result


def _tool_arguments(tool_call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    function = tool_call.get("function")
    if not isinstance(function, dict) or not isinstance(function.get("name"), str):
        raise ToolFailure("Tool request-টি সঠিকভাবে গঠিত নয়।")
    arguments = function.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ToolFailure("Tool arguments সঠিক JSON নয়।") from exc
    if not isinstance(arguments, dict):
        raise ToolFailure("Tool arguments সঠিকভাবে গঠিত নয়।")
    return function["name"], arguments


async def get_payment_status(call_id: str, arguments: dict[str, Any]) -> str:
    """Return a payment record only for the verified customer bound to a call."""
    call_rows = await _supabase_get(
        "call_logs",
        {
            "select": "id,customer_id,identity_verified,ended_at,structured_output",
            "vapi_call_id": f"eq.{call_id}",
            "limit": "2",
        },
    )
    if len(call_rows) != 1:
        raise ToolFailure("এই call-টি কোনো customer-এর সঙ্গে নিরাপদভাবে যুক্ত নয়।")

    call = call_rows[0]
    customer_id = call.get("customer_id")
    if not isinstance(customer_id, str) or not customer_id:
        raise ToolFailure("এই call-টি কোনো customer-এর সঙ্গে নিরাপদভাবে যুক্ত নয়।")
    if call.get("ended_at") is not None:
        raise ToolFailure("এই call আর active নেই।")

    requested_customer = arguments.get("customer_id")
    if requested_customer is not None and requested_customer != customer_id:
        raise ToolFailure("অনুরোধের customer active call-এর সঙ্গে মেলে না।")

    if call.get("identity_verified") is not True:
        raise ToolFailure("পরিচয় যাচাই সম্পন্ন না হওয়ায় আর্থিক তথ্য দেওয়া যাবে না।")

    verification_rows = await _supabase_get(
        "call_identity_verifications",
        {
            "select": "id",
            "call_log_id": f"eq.{call['id']}",
            "customer_id": f"eq.{customer_id}",
            "verified": "eq.true",
            "verified_at": "not.is.null",
            "order": "verified_at.desc",
            "limit": "1",
        },
    )
    if len(verification_rows) != 1:
        raise ToolFailure("পরিচয় যাচাই সম্পন্ন না হওয়ায় আর্থিক তথ্য দেওয়া যাবে না।")

    structured_output = call.get("structured_output")
    trusted_reference = (
        structured_output.get("payment_reference") if isinstance(structured_output, dict) else None
    )
    if not isinstance(trusted_reference, str) or not trusted_reference:
        raise ToolFailure("এই call-এর জন্য অনুমোদিত payment reference নির্ধারিত নেই।")

    requested_reference = arguments.get("payment_reference", arguments.get("payment_id"))
    if requested_reference is not None and requested_reference != trusted_reference:
        raise ToolFailure("অনুরোধের payment active call-এর সঙ্গে মেলে না।")

    payment_rows = await _supabase_get(
        "payment_records",
        {
            "select": (
                "customer_id,payment_reference,payment_type,payment_status,"
                "outstanding_amount,currency,due_date"
            ),
            "customer_id": f"eq.{customer_id}",
            "payment_reference": f"eq.{trusted_reference}",
            "limit": "2",
        },
    )
    if len(payment_rows) != 1:
        raise ToolFailure("Payment record যাচাই করা যায়নি।")

    payment = payment_rows[0]
    if payment.get("customer_id") != customer_id or payment.get("payment_reference") != trusted_reference:
        raise ToolFailure("Payment record যাচাই করা যায়নি।")

    result: dict[str, Any] = {
        "success": True,
        "record_type": "payment",
        "customer_id": customer_id,
        "payment_reference": trusted_reference,
        "payment_status": payment.get("payment_status"),
        "currency": payment.get("currency"),
        "due_date": payment.get("due_date"),
    }
    if arguments.get("include_outstanding_balance") is True:
        result["outstanding_amount"] = payment.get("outstanding_amount")
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)


async def _execute_tool(call_id: str, tool_call: dict[str, Any]) -> dict[str, str]:
    tool_call_id = tool_call["id"]
    try:
        name, arguments = _tool_arguments(tool_call)
        if name != "get_payment_status":
            raise ToolFailure("এই tool-টি বর্তমানে backend-এ সক্রিয় নয়।")
        result = await get_payment_status(call_id, arguments)
        logger.info("operation=vapi_tool tool=get_payment_status category=success")
        return {"toolCallId": tool_call_id, "result": result}
    except ToolFailure as exc:
        logger.info("operation=vapi_tool category=controlled_failure")
        return {"toolCallId": tool_call_id, "error": str(exc).replace("\n", " ")}
    except DatabaseUnavailable:
        logger.warning("operation=vapi_tool category=database_unavailable")
        return {
            "toolCallId": tool_call_id,
            "error": "Payment তথ্য এই মুহূর্তে নিরাপদভাবে যাচাই করা যাচ্ছে না।",
        }
    except Exception as exc:
        logger.warning(
            "operation=vapi_tool category=unexpected_error exception=%s",
            type(exc).__name__,
        )
        return {"toolCallId": tool_call_id, "error": "Tool request সম্পন্ন করা যায়নি।"}


@router.post("/vapi/tools")
async def vapi_tools(request: Request) -> dict[str, list[dict[str, str]]]:
    _authenticate_vapi(request)
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict) or message.get("type") != "tool-calls":
        raise HTTPException(status_code=400, detail="Invalid tool-calls message")
    call = message.get("call")
    call_id = call.get("id") if isinstance(call, dict) else None
    tool_calls = message.get("toolCallList")
    if not isinstance(call_id, str) or not call_id or not isinstance(tool_calls, list) or not tool_calls:
        raise HTTPException(status_code=400, detail="Invalid tool-calls message")
    if not all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in tool_calls):
        raise HTTPException(status_code=400, detail="Invalid tool call identifier")

    results = [await _execute_tool(call_id, tool_call) for tool_call in tool_calls]
    return {"results": results}


@router.post("/operator/demo/verify-call")
async def verify_demo_call(request: Request) -> dict[str, str]:
    """Explicit operator approval for synthetic demo calls only.

    This endpoint is intentionally not a Vapi tool. An operator must complete
    an approved, non-financial demo challenge before invoking it.
    """
    _authenticate_operator(request)
    try:
        verification = DemoVerificationRequest.model_validate(await request.json())
    except (json.JSONDecodeError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail="Invalid demo verification request") from exc

    payment_rows = await _supabase_get(
        "payment_records",
        {
            "select": "customer_id,payment_reference",
            "customer_id": f"eq.{verification.customer_id}",
            "payment_reference": f"eq.{verification.payment_reference}",
            "limit": "2",
        },
    )
    if len(payment_rows) != 1:
        raise HTTPException(status_code=409, detail="Demo association could not be verified")

    call_rows = await _supabase_get(
        "call_logs",
        {
            "select": "id,customer_id,ended_at,structured_output",
            "vapi_call_id": f"eq.{verification.vapi_call_id}",
            "limit": "2",
        },
    )
    if len(call_rows) > 1:
        raise DatabaseUnavailable

    if call_rows:
        call = call_rows[0]
        if call.get("customer_id") != verification.customer_id or call.get("ended_at") is not None:
            raise HTTPException(status_code=409, detail="Demo call association conflict")
        structured_output = call.get("structured_output")
        if not isinstance(structured_output, dict):
            structured_output = {}
        structured_output = {**structured_output, "payment_reference": verification.payment_reference}
        written_calls = await _supabase_write(
            "PATCH",
            "call_logs",
            params={"id": f"eq.{call['id']}"},
            payload={"identity_verified": True, "structured_output": structured_output},
        )
    else:
        written_calls = await _supabase_write(
            "POST",
            "call_logs",
            params={},
            payload={
                "vapi_call_id": verification.vapi_call_id,
                "customer_id": verification.customer_id,
                "call_direction": "demo",
                "call_reason": "payment_status_demo",
                "call_status": "active",
                "identity_verified": True,
                "structured_output": {"payment_reference": verification.payment_reference},
            },
        )
    if len(written_calls) != 1 or not isinstance(written_calls[0].get("id"), str):
        raise DatabaseUnavailable

    await _supabase_write(
        "POST",
        "call_identity_verifications",
        params={"on_conflict": "call_log_id,customer_id"},
        payload={
            "call_log_id": written_calls[0]["id"],
            "customer_id": verification.customer_id,
            "verification_method": "operator_approved_demo",
            "verified": True,
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "failure_reason": None,
        },
        prefer="resolution=merge-duplicates,return=representation",
    )
    logger.info("operation=demo_verification category=approved")
    return {
        "status": "verified",
        "vapi_call_id": verification.vapi_call_id,
        "customer_id": verification.customer_id,
    }
