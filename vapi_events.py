"""Secure persistence of Vapi end-of-call reports."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from vapi_tools import (
    DatabaseUnavailable,
    _authenticate_vapi,
    _supabase_get,
    _supabase_write,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_SECRET_LABEL = re.compile(
    r"(?i)\b(pin|otp|cvv|cvc|password|passcode)\b\s*[:=\-]?\s*([^\s,;.!?]{3,})"
)
_LONG_NUMBER = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")


def _redact_text(value: str) -> str:
    value = _SECRET_LABEL.sub(lambda match: f"{match.group(1)}: [REDACTED]", value)
    return _LONG_NUMBER.sub("[REDACTED_NUMBER]", value)


def _redact_json(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("_", "")
            if normalized in {"pin", "otp", "cvv", "cvc", "password", "passcode", "cardnumber"}:
                result[key] = "[REDACTED]"
            else:
                result[key] = _redact_json(item)
        return result
    return value


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: Any) -> str | None:
    parsed = _parse_datetime(value)
    return parsed.isoformat() if parsed else None


def _call_direction(call: dict[str, Any]) -> str | None:
    call_type = call.get("type")
    if call_type == "inboundPhoneCall":
        return "inbound"
    if call_type == "outboundPhoneCall":
        return "outbound"
    if isinstance(call_type, str) and call_type:
        return call_type
    return None


def _duration_seconds(started_at: str | None, ended_at: str | None) -> int | None:
    start = _parse_datetime(started_at)
    end = _parse_datetime(ended_at)
    if not start or not end or end < start:
        return None
    return int((end - start).total_seconds())


def _storage_policy(call: dict[str, Any], trusted: dict[str, Any]) -> tuple[bool, int | None]:
    enabled = os.getenv("TRANSCRIPT_STORAGE_ENABLED", "").lower() == "true"
    retention_raw = os.getenv("TRANSCRIPT_RETENTION_DAYS", "")
    try:
        retention_days = int(retention_raw)
    except ValueError:
        retention_days = 0
    if not 1 <= retention_days <= 3650:
        retention_days = 0

    metadata = call.get("metadata") if isinstance(call.get("metadata"), dict) else {}
    permitted = metadata.get("transcriptStoragePermitted") is True
    if trusted.get("transcript_storage_permitted") is True:
        permitted = True
    return enabled and permitted and retention_days > 0, retention_days or None


def _recording_url(message: dict[str, Any], artifact: dict[str, Any]) -> str | None:
    recording = artifact.get("recording")
    candidates = [
        recording.get("url") if isinstance(recording, dict) else None,
        artifact.get("recordingUrl"),
        message.get("recordingUrl"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.startswith("https://"):
            return candidate
    return None


def _structured_analysis(message: dict[str, Any]) -> dict[str, Any]:
    analysis = message.get("analysis") if isinstance(message.get("analysis"), dict) else {}
    for candidate in (
        analysis.get("structuredData"),
        analysis.get("structuredOutput"),
        message.get("structuredOutput"),
    ):
        if isinstance(candidate, dict):
            return _redact_json(candidate)
    return {}


def _analysis_text(analysis: dict[str, Any], structured: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = analysis.get(key, structured.get(key))
        if isinstance(value, str) and value:
            return _redact_text(value)
    return None


def _message_rows(
    messages: Any,
    *,
    call_log_id: str,
    vapi_call_id: str,
) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        return []
    rows = []
    for index, item in enumerate(messages):
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            continue
        if item.get("transcriptType") == "partial":
            continue
        text = next(
            (
                item.get(key)
                for key in ("message", "text", "transcript", "content")
                if isinstance(item.get(key), str) and item.get(key)
            ),
            None,
        )
        if text is None:
            continue
        timestamp = next(
            (_iso(item.get(key)) for key in ("timestamp", "createdAt", "time") if _iso(item.get(key))),
            None,
        )
        external_id = item.get("id") if isinstance(item.get("id"), str) else None
        message_type = item.get("type") if isinstance(item.get("type"), str) else "conversation"
        rows.append(
            {
                "call_log_id": call_log_id,
                "vapi_call_id": vapi_call_id,
                "message_index": index,
                "speaker_role": item["role"],
                "message_type": message_type,
                "message_text": _redact_text(text),
                "message_timestamp": timestamp,
                "external_message_id": external_id,
            }
        )
    return rows


async def _persist_end_of_call(message: dict[str, Any]) -> None:
    call = message.get("call") if isinstance(message.get("call"), dict) else {}
    call_id = call.get("id")
    if not isinstance(call_id, str) or not call_id:
        raise HTTPException(status_code=400, detail="Missing Vapi call ID")

    existing = await _supabase_get(
        "call_logs",
        {
            "select": "id,customer_id,identity_verified,structured_output",
            "vapi_call_id": f"eq.{call_id}",
            "limit": "2",
        },
    )
    if len(existing) > 1:
        raise DatabaseUnavailable
    current = existing[0] if existing else {}
    current_structured = current.get("structured_output")
    if not isinstance(current_structured, dict):
        current_structured = {}

    analysis = message.get("analysis") if isinstance(message.get("analysis"), dict) else {}
    structured = _structured_analysis(message)
    started_at = _iso(message.get("startedAt") or call.get("startedAt"))
    ended_at = _iso(message.get("endedAt") or call.get("endedAt"))
    ended_reason = message.get("endedReason") or call.get("endedReason")
    combined_structured = dict(current_structured)
    if structured:
        combined_structured["analysis"] = structured
    call_payload: dict[str, Any] = {
        "vapi_call_id": call_id,
        "call_direction": _call_direction(call),
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": _duration_seconds(started_at, ended_at),
        "call_status": call.get("status") or "ended",
        "ended_reason": ended_reason if isinstance(ended_reason, str) else None,
        "summary": _analysis_text(analysis, structured, "summary"),
        "main_issue": _analysis_text(analysis, structured, "main_issue", "issue"),
        "customer_response": _analysis_text(analysis, structured, "customer_response"),
        "support_ticket_reference": _analysis_text(
            analysis, structured, "support_ticket_reference", "ticket"
        ),
        "next_callback_at": _iso(structured.get("next_callback_at")),
        "final_outcome": _analysis_text(analysis, structured, "final_outcome", "resolution"),
        "structured_output": combined_structured,
    }
    call_payload = {key: value for key, value in call_payload.items() if value is not None}
    if isinstance(structured.get("follow_up_required"), bool):
        call_payload["follow_up_required"] = structured["follow_up_required"]
    if current.get("customer_id"):
        call_payload["customer_id"] = current["customer_id"]
    else:
        metadata = call.get("metadata") if isinstance(call.get("metadata"), dict) else {}
        trusted_customer = metadata.get("customerId")
        if isinstance(trusted_customer, str) and trusted_customer:
            call_payload["customer_id"] = trusted_customer
    if os.getenv("STORE_CALLER_PHONE", "").lower() == "true":
        customer = call.get("customer") if isinstance(call.get("customer"), dict) else {}
        number = customer.get("number")
        if isinstance(number, str) and number:
            call_payload["phone_number"] = number

    if existing:
        written = await _supabase_write(
            "PATCH", "call_logs", params={"id": f"eq.{current['id']}"}, payload=call_payload
        )
    else:
        written = await _supabase_write("POST", "call_logs", params={}, payload=call_payload)
    if len(written) != 1 or not isinstance(written[0].get("id"), str):
        raise DatabaseUnavailable
    call_log_id = written[0]["id"]

    permitted, retention_days = _storage_policy(call, current_structured)
    if not permitted:
        logger.info("operation=vapi_end_of_call category=content_not_permitted")
        return

    artifact = message.get("artifact") if isinstance(message.get("artifact"), dict) else {}
    transcript = artifact.get("transcript", message.get("transcript"))
    transcript = _redact_text(transcript) if isinstance(transcript, str) and transcript else None
    compliance = message.get("compliance") if isinstance(message.get("compliance"), dict) else {}
    consent = (
        compliance.get("recordingConsent")
        if isinstance(compliance.get("recordingConsent"), dict)
        else {}
    )
    recording_consent = bool(consent.get("grantedAt"))
    store_recording = os.getenv("STORE_RECORDING_REFERENCES", "").lower() == "true"
    recording_url = _recording_url(message, artifact) if store_recording and recording_consent else None
    retention_until = (datetime.now(timezone.utc) + timedelta(days=retention_days)).isoformat()
    transcript_payload = {
        "call_log_id": call_log_id,
        "transcript": transcript,
        "recording_url": recording_url,
        "recording_consent": recording_consent,
        "transcript_storage_permitted": True,
        "retention_until": retention_until,
        "transcript_status": "stored" if transcript else "pending",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await _supabase_write(
        "POST",
        "call_transcripts",
        params={"on_conflict": "call_log_id"},
        payload=transcript_payload,
        prefer="resolution=merge-duplicates,return=representation",
    )

    rows = _message_rows(
        artifact.get("messages"), call_log_id=call_log_id, vapi_call_id=call_id
    )
    if rows:
        await _supabase_write(
            "POST",
            "call_messages",
            params={"on_conflict": "call_log_id,message_index"},
            payload=rows,
            prefer="resolution=merge-duplicates,return=representation",
        )
    logger.info(
        "operation=vapi_end_of_call category=stored transcript=%s messages=%d recording_reference=%s",
        bool(transcript),
        len(rows),
        bool(recording_url),
    )


@router.post("/vapi/events", status_code=204)
async def vapi_events(request: Request) -> Response:
    _authenticate_vapi(request)
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict) or message.get("type") != "end-of-call-report":
        raise HTTPException(status_code=400, detail="Unsupported Vapi event")
    await _persist_end_of_call(message)
    return Response(status_code=204)
