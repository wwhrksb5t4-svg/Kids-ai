import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

_data_dir = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"DATA_DIR": _data_dir.name}):
    import main


class CloudflareImagesTest(unittest.IsolatedAsyncioTestCase):
    async def test_generate_scene_art(self):
        image_bytes = io.BytesIO()
        Image.new("RGB", (80, 80), "blue").save(image_bytes, format="PNG")

        def respond(request):
            self.assertEqual(request.headers["Authorization"], "Bearer test-token")
            body = json.loads(request.content)
            self.assertEqual(set(body), {"prompt", "steps"})
            self.assertIn("friendly turtle", body["prompt"])
            return httpx.Response(200, json={
                "success": True,
                "result": {"image": base64.b64encode(image_bytes.getvalue()).decode()},
            })

        transport = httpx.MockTransport(respond)
        real_client = httpx.AsyncClient
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scene.png"
            with patch.object(main, "CLOUDFLARE_API_TOKEN", "test-token"), \
                 patch.object(main, "CLOUDFLARE_ACCOUNT_ID", "a" * 32), \
                 patch.object(main.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=transport, **kwargs)):
                await main.make_cloudflare_frame({"visual_description": "friendly turtle"}, path)
            with Image.open(path) as image:
                self.assertEqual(image.size, (720, 1280))
            self.assertFalse(path.with_suffix(".tmp").exists())

    async def test_failed_or_invalid_responses_are_reported(self):
        real_client = httpx.AsyncClient
        for response, message in (
            (httpx.Response(429), "HTTP 429"),
            (httpx.Response(200, json={"success": True, "result": {"image": "not-base64"}}), "invalid image response"),
            (httpx.Response(200, json={"success": False}), "no Cloudflare error code"),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                transport = httpx.MockTransport(lambda request: response)
                path = Path(directory) / "scene.png"
                with patch.object(main.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=transport, **kwargs)):
                    with self.assertRaisesRegex(RuntimeError, message):
                        await main.make_cloudflare_frame({"visual_description": "turtle"}, path)
                self.assertFalse(path.exists())

    async def test_cloudflare_error_reports_code_not_provider_text(self):
        response = httpx.Response(400, json={"success": False, "errors": [
            {"code": 5007, "message": "secret provider detail"},
        ]})
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(lambda request: response)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(main.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=transport, **kwargs)):
            with self.assertRaises(RuntimeError) as caught:
                await main.make_cloudflare_frame({"visual_description": "turtle"}, Path(directory) / "scene.png")
        self.assertIn("HTTP 400; Cloudflare error code: 5007", str(caught.exception))
        self.assertNotIn("secret provider detail", str(caught.exception))

    def test_cloudflare_requires_credentials(self):
        with patch.object(main, "IMAGE_PROVIDER", "cloudflare"), \
             patch.object(main, "CLOUDFLARE_ACCOUNT_ID", ""), \
             patch.object(main, "CLOUDFLARE_API_TOKEN", ""):
            with self.assertRaisesRegex(RuntimeError, "CLOUDFLARE_ACCOUNT_ID"):
                main.check_image_provider()

    async def test_generate_button_requires_admin(self):
        query = SimpleNamespace(data="generate:today", message=object(), answer=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=123))
        with patch.object(main, "ADMIN_TELEGRAM_ID", "123"), \
             patch.object(main, "create_today", new_callable=AsyncMock) as create:
            await main.tg_callback(update, None)
            create.assert_awaited_once_with(query.message)
        query.answer.reset_mock()
        with patch.object(main, "ADMIN_TELEGRAM_ID", "456"), \
             patch.object(main, "create_today", new_callable=AsyncMock) as create:
            await main.tg_callback(update, None)
            create.assert_not_awaited()
            query.answer.assert_awaited_once_with("غير مصرح", show_alert=True)


class DashboardTest(unittest.IsolatedAsyncioTestCase):
    async def test_status_requires_key_and_reports_counts(self):
        with patch.object(main, "APP_API_KEY", "secret"):
            with self.assertRaises(main.HTTPException) as caught:
                await main.web_status("wrong")
            self.assertEqual(caught.exception.status_code, 401)
            result = await main.web_status("secret")
        self.assertIn("scheduler_running", result["settings"])
        self.assertIsInstance(result["counts"], dict)


if __name__ == "__main__":
    unittest.main()
