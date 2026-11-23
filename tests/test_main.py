import os
import json
import base64
import unittest
from unittest.mock import patch

import httpx

from main import app, database_health, _build_supabase_headers, _supabase_key_type


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, path: str) -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    async def test_root_and_health_routes(self):
        root = await self.request("/")
        health = await self.request("/health")

        self.assertEqual(root.status_code, 200)
        self.assertEqual(root.json()["service"], "upay-maya-backend")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})

    async def test_database_health_rejects_missing_configuration_safely(self):
        with patch.dict(os.environ, {}, clear=True):
            response = await self.request("/health/db")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"database": "not_configured"})
        self.assertNotIn("SUPABASE", response.text)

    async def test_database_health_rejects_whitespace_and_quotes(self):
        invalid_values = [
            (" https://project.supabase.co", "sb_secret_example"),
            ('"https://project.supabase.co"', "sb_secret_example"),
            ("https://project.supabase.co", "sb_secret_example "),
            ("https://project.supabase.co", "'sb_secret_example'"),
        ]
        for url, key in invalid_values:
            with self.subTest(url=url, key_shape=key[:2]):
                with patch.dict(
                    os.environ,
                    {"SUPABASE_URL": url, "SUPABASE_SECRET_KEY": key},
                    clear=True,
                ):
                    response = await self.request("/health/db")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), {"database": "invalid_configuration"})

    async def test_database_health_rejects_public_and_anon_keys(self):
        for key in ("sb_publishable_example", self.jwt_for_role("anon")):
            with self.subTest(key_type=_supabase_key_type(key)):
                with patch.dict(
                    os.environ,
                    {
                        "SUPABASE_URL": "https://project.supabase.co",
                        "SUPABASE_SECRET_KEY": key,
                    },
                    clear=True,
                ):
                    response = await self.request("/health/db")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), {"database": "invalid_configuration"})

    async def test_database_health_uses_read_only_query_and_returns_connected(self):
        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(200, json=[{"customer_id": "redacted"}])

        real_async_client = httpx.AsyncClient

        def client_factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_async_client(*args, **kwargs)

        with patch.dict(
            os.environ,
            {
                "SUPABASE_URL": "https://project.supabase.co",
                "SUPABASE_SECRET_KEY": "sb_secret_example",
            },
            clear=True,
        ), patch("main.httpx.AsyncClient", side_effect=client_factory):
            response = await database_health()

        request = captured["request"]
        self.assertEqual(response, {"database": "connected"})
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.path, "/rest/v1/customers")
        self.assertEqual(request.url.params["select"], "customer_id")
        self.assertEqual(request.url.params["limit"], "1")
        self.assertEqual(request.headers["apikey"], "sb_secret_example")
        self.assertNotIn("authorization", request.headers)

    async def test_database_health_sanitizes_upstream_auth_failure(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "sensitive upstream detail"})

        real_async_client = httpx.AsyncClient

        def client_factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_async_client(*args, **kwargs)

        with patch.dict(
            os.environ,
            {
                "SUPABASE_URL": "https://project.supabase.co",
                "SUPABASE_SECRET_KEY": "sb_secret_do_not_expose",
            },
            clear=True,
        ), patch("main.httpx.AsyncClient", side_effect=client_factory):
            response = await database_health()

        self.assertEqual(response.status_code, 503)
        body = json.loads(response.body)
        self.assertEqual(body, {"database": "connection_failed"})
        self.assertNotIn("sensitive", response.body.decode())
        self.assertNotIn("sb_secret", response.body.decode())

    def test_header_strategy_separates_opaque_and_legacy_keys(self):
        opaque = _build_supabase_headers("sb_secret_example")
        legacy_key = self.jwt_for_role("service_role")
        legacy = _build_supabase_headers(legacy_key)

        self.assertNotIn("Authorization", opaque)
        self.assertEqual(legacy["Authorization"], f"Bearer {legacy_key}")

    @staticmethod
    def jwt_for_role(role: str) -> str:
        def encode(value: bytes) -> str:
            return base64.urlsafe_b64encode(value).decode().rstrip("=")

        return f"{encode(b'{\"alg\":\"HS256\"}')}.{encode(json.dumps({'role': role}).encode())}.signature"


if __name__ == "__main__":
    unittest.main()
