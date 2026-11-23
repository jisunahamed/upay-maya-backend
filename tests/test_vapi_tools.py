import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from main import app
from vapi_tools import DatabaseUnavailable


WEBHOOK_SECRET = "test-webhook-secret-with-at-least-32-characters"


class VapiToolTests(unittest.IsolatedAsyncioTestCase):
    async def post(self, payload, *, authenticated=True):
        headers = {"Authorization": f"Bearer {WEBHOOK_SECRET}"} if authenticated else {}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/vapi/tools", json=payload, headers=headers)

    async def operator_post(self, payload, *, authenticated=True):
        headers = {"Authorization": f"Bearer {WEBHOOK_SECRET}"} if authenticated else {}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/operator/demo/verify-call", json=payload, headers=headers)

    @staticmethod
    def payload(arguments=None, *, tool_name="get_payment_status", tool_id="tool-call-1"):
        return {
            "message": {
                "type": "tool-calls",
                "call": {"id": "vapi-call-1"},
                "toolCallList": [
                    {
                        "id": tool_id,
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": arguments or {},
                        },
                    }
                ],
            }
        }

    @staticmethod
    def verified_rows():
        return [
            [
                {
                    "id": "call-log-uuid",
                    "customer_id": "DEMO-CUSTOMER-A",
                    "identity_verified": True,
                    "ended_at": None,
                    "structured_output": {"payment_reference": "DEMO-PAY-A"},
                }
            ],
            [{"id": "verification-uuid"}],
            [
                {
                    "customer_id": "DEMO-CUSTOMER-A",
                    "payment_reference": "DEMO-PAY-A",
                    "payment_type": "credit_card_bill",
                    "payment_status": "outstanding",
                    "outstanding_amount": 4250,
                    "currency": "BDT",
                    "due_date": "2026-12-01",
                }
            ],
        ]

    async def test_missing_webhook_configuration_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            response = await self.post(self.payload())
        self.assertEqual(response.status_code, 503)

    async def test_unauthenticated_and_wrong_secret_are_rejected(self):
        with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True):
            missing = await self.post(self.payload(), authenticated=False)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                wrong = await client.post(
                    "/vapi/tools",
                    json=self.payload(),
                    headers={"Authorization": "Bearer definitely-wrong-secret-value"},
                )
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)

    async def test_verified_customer_returns_vapi_string_result(self):
        database = AsyncMock(side_effect=self.verified_rows())
        arguments = {
            "customer_id": "DEMO-CUSTOMER-A",
            "payment_id": "DEMO-PAY-A",
            "include_outstanding_balance": True,
        }
        with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True), patch(
            "vapi_tools._supabase_get", database
        ):
            response = await self.post(self.payload(arguments))

        self.assertEqual(response.status_code, 200)
        item = response.json()["results"][0]
        self.assertEqual(item["toolCallId"], "tool-call-1")
        self.assertIsInstance(item["result"], str)
        self.assertNotIn("\n", item["result"])
        result = json.loads(item["result"])
        self.assertEqual(result["payment_reference"], "DEMO-PAY-A")
        self.assertEqual(result["outstanding_amount"], 4250)
        self.assertEqual(database.await_count, 3)

    async def test_unverified_customer_never_receives_financial_data(self):
        database = AsyncMock(
            return_value=[
                {
                    "id": "call-log-uuid",
                    "customer_id": "DEMO-CUSTOMER-A",
                    "identity_verified": False,
                    "ended_at": None,
                    "structured_output": {"payment_reference": "DEMO-PAY-A"},
                }
            ]
        )
        with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True), patch(
            "vapi_tools._supabase_get", database
        ):
            response = await self.post(
                self.payload({"customer_id": "DEMO-CUSTOMER-A", "payment_id": "DEMO-PAY-A"})
            )

        item = response.json()["results"][0]
        self.assertEqual(response.status_code, 200)
        self.assertIn("error", item)
        self.assertNotIn("4250", json.dumps(item))
        self.assertEqual(database.await_count, 1)

    async def test_unknown_call_cannot_retrieve_data(self):
        database = AsyncMock(return_value=[])
        with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True), patch(
            "vapi_tools._supabase_get", database
        ):
            response = await self.post(self.payload())
        self.assertIn("error", response.json()["results"][0])
        self.assertEqual(database.await_count, 1)

    async def test_cross_customer_access_is_rejected_before_payment_lookup(self):
        database = AsyncMock(return_value=self.verified_rows()[0])
        with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True), patch(
            "vapi_tools._supabase_get", database
        ):
            response = await self.post(
                self.payload({"customer_id": "DEMO-CUSTOMER-B", "payment_id": "DEMO-PAY-B"})
            )
        self.assertIn("error", response.json()["results"][0])
        self.assertEqual(database.await_count, 1)

    async def test_cross_payment_access_is_rejected(self):
        database = AsyncMock(side_effect=self.verified_rows()[:2])
        with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True), patch(
            "vapi_tools._supabase_get", database
        ):
            response = await self.post(
                self.payload({"customer_id": "DEMO-CUSTOMER-A", "payment_id": "DEMO-PAY-B"})
            )
        self.assertIn("error", response.json()["results"][0])
        self.assertEqual(database.await_count, 2)

    async def test_database_failure_returns_safe_tool_error(self):
        database = AsyncMock(side_effect=DatabaseUnavailable)
        with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True), patch(
            "vapi_tools._supabase_get", database
        ):
            response = await self.post(self.payload())
        item = response.json()["results"][0]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(item), {"toolCallId", "error"})
        self.assertNotIn("Supabase", item["error"])

    async def test_multiple_tool_calls_each_receive_matching_result(self):
        payload = self.payload(
            {
                "customer_id": "DEMO-CUSTOMER-A",
                "payment_reference": "DEMO-PAY-A",
                "include_outstanding_balance": False,
            }
        )
        payload["message"]["toolCallList"].append(
            {
                "id": "tool-call-2",
                "type": "function",
                "function": {"name": "unsupported_tool", "arguments": {}},
            }
        )
        database = AsyncMock(side_effect=self.verified_rows())
        with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True), patch(
            "vapi_tools._supabase_get", database
        ):
            response = await self.post(payload)

        results = response.json()["results"]
        self.assertEqual([item["toolCallId"] for item in results], ["tool-call-1", "tool-call-2"])
        self.assertIn("result", results[0])
        self.assertNotIn("outstanding_amount", json.loads(results[0]["result"]))
        self.assertIn("error", results[1])

    async def test_demo_operator_endpoint_is_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            response = await self.operator_post(
                {
                    "vapi_call_id": "demo-call-1",
                    "customer_id": "DEMO-CUSTOMER-A",
                    "payment_reference": "DEMO-PAY-A",
                }
            )
        self.assertEqual(response.status_code, 404)

    async def test_authenticated_operator_can_create_verified_demo_association(self):
        reads = AsyncMock(
            side_effect=[
                [{"customer_id": "DEMO-CUSTOMER-A", "payment_reference": "DEMO-PAY-A"}],
                [],
            ]
        )
        writes = AsyncMock(side_effect=[[{"id": "call-log-uuid"}], [{"id": "verification-uuid"}]])
        environment = {
            "ENABLE_DEMO_OPERATOR_VERIFICATION": "true",
            "OPERATOR_API_SECRET": WEBHOOK_SECRET,
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "vapi_tools._supabase_get", reads
        ), patch("vapi_tools._supabase_write", writes):
            response = await self.operator_post(
                {
                    "vapi_call_id": "demo-call-1",
                    "customer_id": "DEMO-CUSTOMER-A",
                    "payment_reference": "DEMO-PAY-A",
                }
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "verified")
        self.assertEqual(reads.await_count, 2)
        self.assertEqual(writes.await_count, 2)
        verification_write = writes.await_args_list[1].kwargs["payload"]
        self.assertEqual(verification_write["verification_method"], "operator_approved_demo")
        self.assertTrue(verification_write["verified"])

    async def test_operator_cannot_reassign_existing_call(self):
        reads = AsyncMock(
            side_effect=[
                [{"customer_id": "DEMO-CUSTOMER-B", "payment_reference": "DEMO-PAY-B"}],
                [
                    {
                        "id": "call-log-uuid",
                        "customer_id": "DEMO-CUSTOMER-A",
                        "ended_at": None,
                        "structured_output": {"payment_reference": "DEMO-PAY-A"},
                    }
                ],
            ]
        )
        environment = {
            "ENABLE_DEMO_OPERATOR_VERIFICATION": "true",
            "OPERATOR_API_SECRET": WEBHOOK_SECRET,
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "vapi_tools._supabase_get", reads
        ):
            response = await self.operator_post(
                {
                    "vapi_call_id": "demo-call-1",
                    "customer_id": "DEMO-CUSTOMER-B",
                    "payment_reference": "DEMO-PAY-B",
                }
            )
        self.assertEqual(response.status_code, 409)

    async def test_operator_database_failure_is_sanitized(self):
        environment = {
            "ENABLE_DEMO_OPERATOR_VERIFICATION": "true",
            "OPERATOR_API_SECRET": WEBHOOK_SECRET,
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "vapi_tools._supabase_get", AsyncMock(side_effect=DatabaseUnavailable)
        ):
            response = await self.operator_post(
                {
                    "vapi_call_id": "demo-call-1",
                    "customer_id": "DEMO-CUSTOMER-A",
                    "payment_reference": "DEMO-PAY-A",
                }
            )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("Supabase", response.text)


if __name__ == "__main__":
    unittest.main()
