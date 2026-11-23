import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from main import app
from vapi_tools import DatabaseUnavailable


WEBHOOK_SECRET = "event-webhook-secret-with-at-least-32-characters"


class VapiEventTests(unittest.IsolatedAsyncioTestCase):
    async def post(self, payload, *, authenticated=True):
        headers = {"Authorization": f"Bearer {WEBHOOK_SECRET}"} if authenticated else {}
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/vapi/events", json=payload, headers=headers)

    @staticmethod
    def environment(**overrides):
        values = {
            "VAPI_WEBHOOK_SECRET": WEBHOOK_SECRET,
            "TRANSCRIPT_STORAGE_ENABLED": "true",
            "TRANSCRIPT_RETENTION_DAYS": "30",
            "STORE_RECORDING_REFERENCES": "false",
            "STORE_CALLER_PHONE": "false",
        }
        values.update(overrides)
        return values

    @staticmethod
    def payload(*, direction="outboundPhoneCall", permitted=True):
        return {
            "message": {
                "type": "end-of-call-report",
                "endedReason": "customer-ended-call",
                "startedAt": "2026-11-23T10:00:00Z",
                "endedAt": "2026-11-23T10:02:05Z",
                "call": {
                    "id": "vapi-call-history-1",
                    "type": direction,
                    "status": "ended",
                    "customer": {"number": "+8801000000000"},
                    "metadata": {
                        "customerId": "DEMO-CUSTOMER-A",
                        "transcriptStoragePermitted": permitted,
                    },
                },
                "analysis": {
                    "summary": "গ্রাহক সহায়তা চেয়েছেন।",
                    "structuredData": {
                        "issue": "ডেমো payment প্রশ্ন",
                        "resolution": "তথ্য ব্যাখ্যা করা হয়েছে",
                        "follow_up_required": True,
                    },
                },
                "artifact": {
                    "transcript": "AI: স্বাগতম। User: আমার বিল কত?",
                    "recording": {"url": "https://recordings.example/demo.wav"},
                    "messages": [
                        {
                            "id": "message-1",
                            "role": "assistant",
                            "message": "স্বাগতম।",
                            "timestamp": "2026-11-23T10:00:01Z",
                        },
                        {"id": "message-2", "role": "user", "message": "আমার বিল কত?"},
                        {"role": "system", "message": "private system prompt"},
                        {"role": "tool", "message": "sensitive tool payload"},
                        {
                            "role": "user",
                            "message": "অসম্পূর্ণ",
                            "transcriptType": "partial",
                        },
                    ],
                },
                "compliance": {"recordingConsent": {"type": "verbal"}},
            }
        }

    async def test_unauthorized_event_is_rejected(self):
        with patch.dict(os.environ, self.environment(), clear=True):
            response = await self.post(self.payload(), authenticated=False)
        self.assertEqual(response.status_code, 401)

    async def test_end_of_call_stores_metadata_transcript_and_ordered_messages(self):
        reads = AsyncMock(return_value=[])
        writes = AsyncMock(
            side_effect=[
                [{"id": "call-log-uuid"}],
                [{"id": "transcript-uuid"}],
                [{"id": "message-uuid-1"}, {"id": "message-uuid-2"}],
            ]
        )
        with patch.dict(os.environ, self.environment(), clear=True), patch(
            "vapi_events._supabase_get", reads
        ), patch("vapi_events._supabase_write", writes):
            response = await self.post(self.payload())

        self.assertEqual(response.status_code, 204)
        self.assertEqual(writes.await_count, 3)
        call_payload = writes.await_args_list[0].kwargs["payload"]
        self.assertEqual(call_payload["call_direction"], "outbound")
        self.assertEqual(call_payload["duration_seconds"], 125)
        self.assertEqual(call_payload["customer_id"], "DEMO-CUSTOMER-A")
        self.assertNotIn("phone_number", call_payload)

        transcript_payload = writes.await_args_list[1].kwargs["payload"]
        self.assertEqual(transcript_payload["transcript"], "AI: স্বাগতম। User: আমার বিল কত?")
        self.assertEqual(transcript_payload["transcript_status"], "stored")
        self.assertIsNone(transcript_payload["recording_url"])

        messages = writes.await_args_list[2].kwargs["payload"]
        self.assertEqual([row["speaker_role"] for row in messages], ["assistant", "user"])
        self.assertEqual([row["message_index"] for row in messages], [0, 1])
        self.assertIsNone(messages[1]["message_timestamp"])
        self.assertNotIn("system", json.dumps(messages))
        self.assertNotIn("tool payload", json.dumps(messages))

    async def test_inbound_direction_and_permitted_phone_storage(self):
        writes = AsyncMock(side_effect=[[{"id": "call-log-uuid"}]])
        payload = self.payload(direction="inboundPhoneCall", permitted=False)
        environment = self.environment(STORE_CALLER_PHONE="true")
        with patch.dict(os.environ, environment, clear=True), patch(
            "vapi_events._supabase_get", AsyncMock(return_value=[])
        ), patch("vapi_events._supabase_write", writes):
            response = await self.post(payload)
        self.assertEqual(response.status_code, 204)
        call_payload = writes.await_args_list[0].kwargs["payload"]
        self.assertEqual(call_payload["call_direction"], "inbound")
        self.assertEqual(call_payload["phone_number"], "+8801000000000")

    async def test_missing_customer_does_not_invent_association(self):
        payload = self.payload(permitted=False)
        payload["message"]["call"]["metadata"].pop("customerId")
        writes = AsyncMock(side_effect=[[{"id": "call-log-uuid"}]])
        with patch.dict(os.environ, self.environment(), clear=True), patch(
            "vapi_events._supabase_get", AsyncMock(return_value=[])
        ), patch("vapi_events._supabase_write", writes):
            response = await self.post(payload)
        self.assertEqual(response.status_code, 204)
        self.assertNotIn("customer_id", writes.await_args_list[0].kwargs["payload"])

    async def test_existing_customer_association_is_never_overwritten(self):
        payload = self.payload(permitted=False)
        payload["message"]["call"]["metadata"]["customerId"] = "DEMO-CUSTOMER-B"
        existing = [
            {
                "id": "call-log-uuid",
                "customer_id": "DEMO-CUSTOMER-A",
                "identity_verified": True,
                "structured_output": {"payment_reference": "DEMO-PAY-A"},
            }
        ]
        writes = AsyncMock(side_effect=[[{"id": "call-log-uuid"}]])
        with patch.dict(os.environ, self.environment(), clear=True), patch(
            "vapi_events._supabase_get", AsyncMock(return_value=existing)
        ), patch("vapi_events._supabase_write", writes):
            response = await self.post(payload)
        self.assertEqual(response.status_code, 204)
        call_payload = writes.await_args_list[0].kwargs["payload"]
        self.assertEqual(call_payload["customer_id"], "DEMO-CUSTOMER-A")
        self.assertEqual(call_payload["structured_output"]["payment_reference"], "DEMO-PAY-A")
        self.assertEqual(writes.await_args_list[0].args[0], "PATCH")

    async def test_missing_transcript_is_marked_pending(self):
        payload = self.payload()
        payload["message"]["artifact"].pop("transcript")
        writes = AsyncMock(side_effect=[[{"id": "call-log-uuid"}], [{"id": "transcript-uuid"}], []])
        with patch.dict(os.environ, self.environment(), clear=True), patch(
            "vapi_events._supabase_get", AsyncMock(return_value=[])
        ), patch("vapi_events._supabase_write", writes):
            response = await self.post(payload)
        self.assertEqual(response.status_code, 204)
        transcript_payload = writes.await_args_list[1].kwargs["payload"]
        self.assertIsNone(transcript_payload["transcript"])
        self.assertEqual(transcript_payload["transcript_status"], "pending")

    async def test_missing_structured_output_is_safe(self):
        payload = self.payload(permitted=False)
        payload["message"].pop("analysis")
        writes = AsyncMock(side_effect=[[{"id": "call-log-uuid"}]])
        with patch.dict(os.environ, self.environment(), clear=True), patch(
            "vapi_events._supabase_get", AsyncMock(return_value=[])
        ), patch("vapi_events._supabase_write", writes):
            response = await self.post(payload)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(writes.await_args_list[0].kwargs["payload"]["structured_output"], {})

    async def test_consent_or_policy_denied_stores_no_conversation_content(self):
        writes = AsyncMock(side_effect=[[{"id": "call-log-uuid"}]])
        with patch.dict(os.environ, self.environment(), clear=True), patch(
            "vapi_events._supabase_get", AsyncMock(return_value=[])
        ), patch("vapi_events._supabase_write", writes):
            response = await self.post(self.payload(permitted=False))
        self.assertEqual(response.status_code, 204)
        self.assertEqual(writes.await_count, 1)

    async def test_recording_reference_requires_separate_opt_in_and_granted_at(self):
        payload = self.payload()
        payload["message"]["compliance"]["recordingConsent"]["grantedAt"] = (
            "2026-11-23T10:00:00Z"
        )
        writes = AsyncMock(side_effect=[[{"id": "call-log-uuid"}], [{"id": "transcript-uuid"}], []])
        environment = self.environment(STORE_RECORDING_REFERENCES="true")
        with patch.dict(os.environ, environment, clear=True), patch(
            "vapi_events._supabase_get", AsyncMock(return_value=[])
        ), patch("vapi_events._supabase_write", writes):
            response = await self.post(payload)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(
            writes.await_args_list[1].kwargs["payload"]["recording_url"],
            "https://recordings.example/demo.wav",
        )

    async def test_sensitive_values_are_redacted_before_storage(self):
        payload = self.payload()
        payload["message"]["artifact"]["transcript"] = (
            "আমার OTP: 123456 এবং card 4111 1111 1111 1111"
        )
        payload["message"]["artifact"]["messages"] = [
            {"role": "user", "message": "password: secret123"},
            {"role": "assistant", "message": "আমি এটি সংরক্ষণ করব না"},
        ]
        writes = AsyncMock(side_effect=[[{"id": "call-log-uuid"}], [{"id": "transcript-uuid"}], []])
        with patch.dict(os.environ, self.environment(), clear=True), patch(
            "vapi_events._supabase_get", AsyncMock(return_value=[])
        ), patch("vapi_events._supabase_write", writes):
            response = await self.post(payload)
        self.assertEqual(response.status_code, 204)
        stored = json.dumps([call.kwargs["payload"] for call in writes.await_args_list], ensure_ascii=False)
        self.assertNotIn("123456", stored)
        self.assertNotIn("4111 1111 1111 1111", stored)
        self.assertNotIn("secret123", stored)

    async def test_duplicate_delivery_uses_database_upserts(self):
        writes = AsyncMock(side_effect=[[{"id": "call-log-uuid"}], [{"id": "transcript-uuid"}], []])
        with patch.dict(os.environ, self.environment(), clear=True), patch(
            "vapi_events._supabase_get", AsyncMock(return_value=[])
        ), patch("vapi_events._supabase_write", writes):
            response = await self.post(self.payload())
        self.assertEqual(response.status_code, 204)
        self.assertEqual(writes.await_args_list[1].kwargs["params"]["on_conflict"], "call_log_id")
        self.assertEqual(
            writes.await_args_list[2].kwargs["params"]["on_conflict"],
            "call_log_id,message_index",
        )

    async def test_database_failure_returns_retryable_503(self):
        with patch.dict(os.environ, self.environment(), clear=True), patch(
            "vapi_events._supabase_get", AsyncMock(side_effect=DatabaseUnavailable)
        ):
            response = await self.post(self.payload())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("Supabase", response.text)

    async def test_missing_call_id_is_rejected(self):
        payload = self.payload()
        payload["message"]["call"].pop("id")
        with patch.dict(os.environ, self.environment(), clear=True):
            response = await self.post(payload)
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
