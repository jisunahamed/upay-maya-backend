import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from main import app
from outbound_calls import (
    MAYA_ASSISTANT_ID,
    OUTBOUND_CALL_REASON,
    OUTBOUND_GREETING,
    build_vapi_outbound_payload,
)
from vapi_tools import DatabaseUnavailable


ADMIN_SECRET = "outbound-admin-secret-with-at-least-32-characters"
PHONE_NUMBER_ID = "11111111-1111-4111-8111-111111111111"


class OutboundCallTests(unittest.IsolatedAsyncioTestCase):
    async def post(self, *, authenticated=True):
        headers = {"Authorization": f"Bearer {ADMIN_SECRET}"} if authenticated else {}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/operator/demo/outbound-calls",
                json={"customer_id": "DEMO-UPAY-001"},
                headers=headers,
            )

    @staticmethod
    def environment():
        return {
            "ENABLE_OUTBOUND_CALLS": "true",
            "OUTBOUND_ADMIN_SECRET": ADMIN_SECRET,
            "VAPI_PRIVATE_API_KEY": "private-vapi-test-key-with-at-least-32-characters",
            "VAPI_ASSISTANT_ID": MAYA_ASSISTANT_ID,
            "VAPI_PHONE_NUMBER_ID": PHONE_NUMBER_ID,
        }

    @staticmethod
    def allowed_customer(**updates):
        row = {
            "customer_id": "DEMO-UPAY-001",
            "registered_phone": "+8801609043060",
            "account_status": "demo",
            "contact_consent": True,
            "voice_call_consent": True,
            "opted_out": False,
        }
        row.update(updates)
        return row

    def test_payload_preserves_maya_v6_and_exact_greeting(self):
        payload = build_vapi_outbound_payload(
            "+8801609043060", MAYA_ASSISTANT_ID, PHONE_NUMBER_ID
        )
        self.assertEqual(payload["assistantId"], MAYA_ASSISTANT_ID)
        self.assertEqual(payload["assistantVersion"], "v6")
        self.assertEqual(payload["phoneNumberId"], PHONE_NUMBER_ID)
        self.assertEqual(payload["customer"]["number"], "+8801609043060")
        overrides = payload["assistantOverrides"]
        self.assertEqual(overrides["firstMessageMode"], "assistant-speaks-first")
        self.assertEqual(overrides["firstMessage"], OUTBOUND_GREETING)
        self.assertEqual(overrides["variableValues"]["call_reason"], OUTBOUND_CALL_REASON)

    async def test_endpoint_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            response = await self.post()
        self.assertEqual(response.status_code, 404)

    async def test_unauthorized_request_is_rejected_before_database_access(self):
        database = AsyncMock()
        with patch.dict(os.environ, self.environment(), clear=True), patch(
            "outbound_calls._supabase_get", database
        ):
            response = await self.post(authenticated=False)
        self.assertEqual(response.status_code, 401)
        database.assert_not_awaited()

    async def test_all_consent_flags_are_required(self):
        cases = [
            {"contact_consent": False},
            {"voice_call_consent": False},
            {"voice_call_consent": None},
            {"opted_out": True},
            {"opted_out": None},
        ]
        for updates in cases:
            with self.subTest(updates=updates):
                provider = AsyncMock()
                reads = AsyncMock(return_value=[self.allowed_customer(**updates)])
                with patch.dict(os.environ, self.environment(), clear=True), patch(
                    "outbound_calls._supabase_get", reads
                ), patch("outbound_calls._create_vapi_call", provider):
                    response = await self.post()
                self.assertEqual(response.status_code, 409)
                provider.assert_not_awaited()

    async def test_authorized_request_builds_call_and_records_unverified_association(self):
        reads = AsyncMock(
            side_effect=[
                [self.allowed_customer()],
                [
                    {
                        "customer_id": "DEMO-UPAY-001",
                        "payment_reference": "DEMO-PAY-001",
                        "payment_status": "outstanding",
                    }
                ],
            ]
        )
        provider = AsyncMock(return_value={"id": "vapi-call-created", "status": "queued"})
        writes = AsyncMock(return_value=[{"id": "call-log-id"}])
        with patch.dict(os.environ, self.environment(), clear=True), patch(
            "outbound_calls._supabase_get", reads
        ), patch("outbound_calls._create_vapi_call", provider), patch(
            "outbound_calls._supabase_write", writes
        ):
            response = await self.post()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["association_status"], "recorded_unverified")
        payload = provider.await_args.args[0]
        self.assertEqual(payload["assistantOverrides"]["firstMessage"], OUTBOUND_GREETING)
        self.assertEqual(
            payload["assistantOverrides"]["firstMessageMode"], "assistant-speaks-first"
        )
        association = writes.await_args.kwargs["payload"]
        self.assertEqual(association["call_reason"], "payment_reminder")
        self.assertFalse(association["identity_verified"])
        self.assertEqual(
            association["structured_output"]["payment_reference"], "DEMO-PAY-001"
        )

    async def test_provider_failure_is_sanitized_and_not_recorded(self):
        reads = AsyncMock(
            side_effect=[
                [self.allowed_customer()],
                [
                    {
                        "customer_id": "DEMO-UPAY-001",
                        "payment_reference": "DEMO-PAY-001",
                        "payment_status": "outstanding",
                    }
                ],
            ]
        )
        writes = AsyncMock()
        provider = AsyncMock(
            side_effect=HTTPException(status_code=502, detail="Call provider rejected the request")
        )
        with patch.dict(os.environ, self.environment(), clear=True), patch(
            "outbound_calls._supabase_get", reads
        ), patch("outbound_calls._create_vapi_call", provider), patch(
            "outbound_calls._supabase_write", writes
        ):
            response = await self.post()
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("private", response.text.lower())
        writes.assert_not_awaited()

    async def test_post_provider_database_failure_warns_without_retry_signal(self):
        reads = AsyncMock(
            side_effect=[
                [self.allowed_customer()],
                [
                    {
                        "customer_id": "DEMO-UPAY-001",
                        "payment_reference": "DEMO-PAY-001",
                        "payment_status": "outstanding",
                    }
                ],
            ]
        )
        provider = AsyncMock(return_value={"id": "vapi-call-created", "status": "queued"})
        writes = AsyncMock(side_effect=DatabaseUnavailable)
        with patch.dict(os.environ, self.environment(), clear=True), patch(
            "outbound_calls._supabase_get", reads
        ), patch("outbound_calls._create_vapi_call", provider), patch(
            "outbound_calls._supabase_write", writes
        ):
            response = await self.post()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["association_status"], "failed_do_not_retry")
        provider.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
