import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from main import app
from vapi_tools import DatabaseUnavailable, normalize_bangladesh_phone, normalize_demo_last4


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
    def payload(
        arguments=None,
        *,
        tool_name="get_payment_status",
        tool_id="tool-call-1",
        call=None,
    ):
        return {
            "message": {
                "type": "tool-calls",
                "call": call or {"id": "vapi-call-1"},
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
    def outbound_call(number="+8801609043060", *, reason="payment_reminder"):
        return {
            "id": "vapi-call-1",
            "type": "outboundPhoneCall",
            "customer": {"number": number},
            "assistantOverrides": {"variableValues": {"call_reason": reason}},
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

    @staticmethod
    def demo_call_log(*, customer_id="DEMO-UPAY-001", attempts=0, verified=False):
        return {
            "id": "call-log-uuid",
            "customer_id": customer_id,
            "phone_number": "+8801609043060",
            "identity_verified": verified,
            "ended_at": None,
            "structured_output": {
                "payment_reference": "DEMO-PAY-001",
                "demo_identity_attempts": attempts,
            },
        }

    @staticmethod
    def demo_customer(*, account_status="demo"):
        return {
            "customer_id": "DEMO-UPAY-001",
            "registered_phone": "+8801609043060",
            "account_status": account_status,
        }

    @staticmethod
    def demo_verify_arguments(provided_last4="3060", **updates):
        arguments = {
            "customer_id": "DEMO-UPAY-001",
            "verification_method": "registered_phone_last4",
            "provided_last4": provided_last4,
        }
        arguments.update(updates)
        return arguments

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

    def test_bangladesh_phone_normalization_is_exact(self):
        expected = "+8801609043060"
        self.assertEqual(normalize_bangladesh_phone("01609043060"), expected)
        self.assertEqual(normalize_bangladesh_phone("+8801609043060"), expected)
        self.assertEqual(normalize_bangladesh_phone("8801609043060"), expected)
        self.assertEqual(normalize_bangladesh_phone("+880 1609-043060"), expected)
        self.assertIsNone(normalize_bangladesh_phone("0609043060"))
        self.assertIsNone(normalize_bangladesh_phone("0160904306"))
        self.assertIsNone(normalize_bangladesh_phone("016090430600"))

    def test_demo_last4_normalizes_bengali_digits(self):
        self.assertEqual(normalize_demo_last4("৩০৬০"), "3060")
        self.assertEqual(normalize_demo_last4("30-60"), "3060")
        self.assertIsNone(normalize_demo_last4("306"))
        self.assertIsNone(normalize_demo_last4("30৬x"))

    async def test_verify_demo_identity_accepts_correct_last4_and_returns_vapi_envelope(self):
        reads = AsyncMock(
            side_effect=[
                [self.demo_call_log()],
                [self.demo_customer()],
                [],
            ]
        )
        writes = AsyncMock(side_effect=[[{"id": "call-log-uuid"}], [{"id": "verification-uuid"}]])
        with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True), patch(
            "vapi_tools._supabase_get", reads
        ), patch("vapi_tools._supabase_write", writes):
            response = await self.post(
                self.payload(
                    self.demo_verify_arguments(),
                    tool_name="verify_demo_identity",
                    call=self.outbound_call(),
                )
            )

        self.assertEqual(response.status_code, 200)
        item = response.json()["results"][0]
        self.assertEqual(item["toolCallId"], "tool-call-1")
        self.assertIsInstance(item["result"], str)
        result = json.loads(item["result"])
        self.assertTrue(result["identity_verified"])
        self.assertEqual(result["verification_method"], "registered_phone_last4")
        self.assertEqual(writes.await_args_list[0].kwargs["payload"]["identity_verified"], True)
        self.assertEqual(
            writes.await_args_list[1].kwargs["payload"]["verification_method"],
            "registered_phone_last4",
        )

    async def test_verify_demo_identity_rejects_incorrect_bengali_last4(self):
        reads = AsyncMock(
            side_effect=[
                [self.demo_call_log()],
                [self.demo_customer()],
                [],
            ]
        )
        writes = AsyncMock(return_value=[{"id": "call-log-uuid"}])
        with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True), patch(
            "vapi_tools._supabase_get", reads
        ), patch("vapi_tools._supabase_write", writes):
            response = await self.post(
                self.payload(
                    self.demo_verify_arguments("৩০৬১"),
                    tool_name="verify_demo_identity",
                    call=self.outbound_call(),
                )
            )

        result = json.loads(response.json()["results"][0]["result"])
        self.assertFalse(result["identity_verified"])
        self.assertEqual(result["attempts_remaining"], 1)
        self.assertFalse(writes.await_args.kwargs["payload"].get("identity_verified", False))

    async def test_verify_demo_identity_rejects_wrong_call_customer_and_non_demo_customer(self):
        cases = [
            ([], self.demo_verify_arguments(), 1),
            ([self.demo_call_log(customer_id="DEMO-OTHER")], self.demo_verify_arguments(), 1),
            ([self.demo_call_log()], self.demo_verify_arguments(), 2),
        ]
        for call_rows, arguments, expected_reads in cases:
            with self.subTest(call_rows=call_rows):
                reads = AsyncMock(
                    side_effect=[call_rows, [self.demo_customer(account_status="production")]]
                )
                with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True), patch(
                    "vapi_tools._supabase_get", reads
                ):
                    response = await self.post(
                        self.payload(
                            arguments,
                            tool_name="verify_demo_identity",
                            call=self.outbound_call(),
                        )
                    )
                self.assertIn("error", response.json()["results"][0])
                self.assertEqual(reads.await_count, expected_reads)

    async def test_verify_demo_identity_locks_after_two_attempts(self):
        reads = AsyncMock(return_value=[self.demo_call_log(attempts=2)])
        writes = AsyncMock()
        with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True), patch(
            "vapi_tools._supabase_get", reads
        ), patch("vapi_tools._supabase_write", writes):
            response = await self.post(
                self.payload(
                    self.demo_verify_arguments(),
                    tool_name="verify_demo_identity",
                    call=self.outbound_call(),
                )
            )
        self.assertIn("error", response.json()["results"][0])
        writes.assert_not_awaited()

    async def test_verify_demo_identity_rejects_invalid_persisted_attempt_state(self):
        call_log = self.demo_call_log()
        call_log["structured_output"]["demo_identity_attempts"] = "0"
        reads = AsyncMock(
            side_effect=[
                [call_log],
                [self.demo_customer()],
                [],
            ]
        )
        writes = AsyncMock()
        with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True), patch(
            "vapi_tools._supabase_get", reads
        ), patch("vapi_tools._supabase_write", writes):
            response = await self.post(
                self.payload(
                    self.demo_verify_arguments(),
                    tool_name="verify_demo_identity",
                    call=self.outbound_call(),
                )
            )
        self.assertIn("error", response.json()["results"][0])
        writes.assert_not_awaited()

    async def test_customer_profile_uses_trusted_outbound_number_and_associates_call(self):
        reads = AsyncMock(
            side_effect=[
                [
                    {
                        "customer_id": "DEMO-UPAY-001",
                        "customer_name": "Jisun",
                        "registered_phone": "+8801609043060",
                        "account_status": "demo",
                        "preferred_language": "bn-BD",
                    }
                ],
                [],
            ]
        )
        writes = AsyncMock(return_value=[{"id": "call-log-uuid"}])
        payload = self.payload(
            {"lookup_value": "0609043060", "customer_id": "MODEL-INVENTED"},
            tool_name="get_customer_profile",
            call=self.outbound_call(),
        )
        with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True), patch(
            "vapi_tools._supabase_get", reads
        ), patch("vapi_tools._supabase_write", writes):
            response = await self.post(payload)

        item = response.json()["results"][0]
        self.assertEqual(response.status_code, 200)
        profile = json.loads(item["result"])
        self.assertEqual(profile["customer_id"], "DEMO-UPAY-001")
        self.assertEqual(profile["customer_name"], "Jisun")
        self.assertFalse(profile["identity_verified"])
        self.assertTrue(profile["verification_required"])
        self.assertNotIn("outstanding_amount", item["result"])
        customer_query = reads.await_args_list[0].args[1]
        self.assertEqual(customer_query["registered_phone"], "eq.+8801609043060")
        association = writes.await_args.kwargs["payload"]
        self.assertEqual(association["customer_id"], "DEMO-UPAY-001")
        self.assertEqual(association["call_direction"], "outbound")
        self.assertEqual(association["call_reason"], "payment_reminder")
        self.assertFalse(association["identity_verified"])

    async def test_customer_profile_rejects_incomplete_untrusted_transcription(self):
        database = AsyncMock()
        payload = self.payload(
            {"lookup_value": "0609043060"},
            tool_name="get_customer_profile",
            call={"id": "vapi-call-1", "type": "webCall"},
        )
        with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True), patch(
            "vapi_tools._supabase_get", database
        ):
            response = await self.post(payload)
        self.assertIn("error", response.json()["results"][0])
        database.assert_not_awaited()

    async def test_model_phone_requires_existing_backend_association(self):
        database = AsyncMock(return_value=[])
        payload = self.payload(
            {"lookup_value": "01609043060"},
            tool_name="get_customer_profile",
            call={"id": "vapi-call-1", "type": "webCall"},
        )
        with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True), patch(
            "vapi_tools._supabase_get", database
        ):
            response = await self.post(payload)
        self.assertIn("error", response.json()["results"][0])
        self.assertEqual(database.await_count, 1)

    async def test_customer_profile_unknown_number_returns_controlled_error(self):
        database = AsyncMock(return_value=[])
        payload = self.payload(
            tool_name="get_customer_profile",
            call=self.outbound_call("+8801711111111"),
        )
        with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True), patch(
            "vapi_tools._supabase_get", database
        ):
            response = await self.post(payload)
        item = response.json()["results"][0]
        self.assertIn("error", item)
        self.assertNotIn("customer_id", json.dumps(item))

    async def test_inbound_phone_creates_unverified_candidate_association(self):
        reads = AsyncMock(
            side_effect=[
                [
                    {
                        "customer_id": "DEMO-UPAY-001",
                        "customer_name": "Jisun",
                        "registered_phone": "+8801609043060",
                        "account_status": "demo",
                        "preferred_language": "bn-BD",
                    }
                ],
                [],
            ]
        )
        writes = AsyncMock(return_value=[{"id": "call-log-uuid"}])
        call = {
            "id": "vapi-call-1",
            "type": "inboundPhoneCall",
            "customer": {"number": "+8801609043060"},
        }
        with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True), patch(
            "vapi_tools._supabase_get", reads
        ), patch("vapi_tools._supabase_write", writes):
            response = await self.post(
                self.payload(tool_name="get_customer_profile", call=call)
            )
        profile = json.loads(response.json()["results"][0]["result"])
        self.assertFalse(profile["identity_verified"])
        association = writes.await_args.kwargs["payload"]
        self.assertEqual(association["call_direction"], "inbound")
        self.assertFalse(association["identity_verified"])

    async def test_customer_profile_database_failure_is_sanitized(self):
        payload = self.payload(
            tool_name="get_customer_profile",
            call=self.outbound_call(),
        )
        with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True), patch(
            "vapi_tools._supabase_get", AsyncMock(side_effect=DatabaseUnavailable)
        ):
            response = await self.post(payload)
        item = response.json()["results"][0]
        self.assertIn("error", item)
        self.assertNotIn("Supabase", item["error"])

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

    async def test_pending_transaction_is_not_reported_as_outstanding_bill(self):
        rows = self.verified_rows()
        rows[2][0]["payment_status"] = "pending"
        rows[2][0]["payment_type"] = "transaction"
        database = AsyncMock(side_effect=rows)
        with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True), patch(
            "vapi_tools._supabase_get", database
        ):
            response = await self.post(
                self.payload({"include_outstanding_balance": True})
            )
        result = json.loads(response.json()["results"][0]["result"])
        self.assertEqual(result["payment_status"], "pending")
        self.assertEqual(result["payment_type"], "transaction")
        self.assertNotIn("outstanding_amount", result)

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

    async def test_support_ticket_remains_inactive_without_persistence(self):
        with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True):
            response = await self.post(
                self.payload({"issue": "synthetic"}, tool_name="create_support_ticket")
            )
        item = response.json()["results"][0]
        self.assertIn("error", item)
        self.assertNotIn("success", json.dumps(item).lower())

    async def test_callback_rejects_past_date_before_inactive_workflow(self):
        with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True):
            response = await self.post(
                self.payload(
                    {"callback_at": "2020-01-01T10:00:00+06:00"},
                    tool_name="schedule_callback",
                )
            )
        item = response.json()["results"][0]
        self.assertIn("error", item)
        self.assertIn("অতীত", item["error"])

    async def test_future_callback_stays_inactive_without_worker(self):
        with patch.dict(os.environ, {"VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET}, clear=True):
            response = await self.post(
                self.payload(
                    {"callback_at": "2035-01-01T10:00:00+06:00"},
                    tool_name="schedule_callback",
                )
            )
        item = response.json()["results"][0]
        self.assertIn("error", item)
        self.assertIn("worker", item["error"])

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

    async def test_authenticated_operator_can_verify_existing_demo_association(self):
        reads = AsyncMock(
            side_effect=[
                [{"customer_id": "DEMO-CUSTOMER-A", "payment_reference": "DEMO-PAY-A"}],
                [
                    {
                        "id": "call-log-uuid",
                        "customer_id": "DEMO-CUSTOMER-A",
                        "identity_verified": False,
                        "ended_at": None,
                        "structured_output": {},
                    }
                ],
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

    async def test_operator_cannot_verify_call_without_trusted_association(self):
        reads = AsyncMock(
            side_effect=[
                [{"customer_id": "DEMO-CUSTOMER-A", "payment_reference": "DEMO-PAY-A"}],
                [],
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
                    "customer_id": "DEMO-CUSTOMER-A",
                    "payment_reference": "DEMO-PAY-A",
                }
            )
        self.assertEqual(response.status_code, 409)

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
