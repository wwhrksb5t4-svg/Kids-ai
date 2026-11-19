import base64
import asyncio
import io
import json
import os
import tempfile
import unittest
import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image, ImageFont
from telegram import Update, User
from telegram._bot import Bot
from telegram.ext import Application

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
            self.assertEqual(set(body), {"prompt"})
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

    def test_cloudflare_validation_details_are_sanitized(self):
        response = httpx.Response(400, json={"errors": [
            {"code": 5006, "message": "unevaluated properties: seed; private content"},
        ]})
        message = main.cloudflare_error(response)
        self.assertIn("validation: unexpected field", message)
        self.assertNotIn("private content", message)
        self.assertNotIn("seed", message)

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


class StudioHelpersTest(unittest.TestCase):
    def test_topics_rotate_and_can_be_overridden(self):
        with patch.object(main, "DAILY_TOPIC", ""):
            first = main.pick_topic(date(2026, 1, 1))
            self.assertNotEqual(first, main.pick_topic(date(2026, 1, 2)))
        with patch.object(main, "DAILY_TOPIC", "my topic"):
            self.assertEqual(main.pick_topic(date(2026, 1, 1)), "my topic")

    def test_character_sheet_is_added_to_image_prompt(self):
        sheet = main.character_sheet({"art_style": "watercolor", "characters": [{"name": "Lulu", "appearance": "blue fish with a red scarf"}]})
        self.assertIn("watercolor", sheet)
        self.assertIn("Lulu: blue fish with a red scarf", sheet)

    def test_captions_cover_the_whole_narration_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            captions = main.make_caption_images("one two three four five six seven eight", "en", 10, Path(directory), 0)
            self.assertEqual(len(captions), 2)
            self.assertAlmostEqual(captions[-1][2], 10)
            self.assertTrue(all(path.exists() for path, _, _ in captions))

    def test_caption_playlist_covers_different_line_heights(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            captions = main.make_caption_images(
                "one two three four five extraordinary " * 4, "en", 12, folder, 0
            )
            sizes = set()
            for path, _, _ in captions:
                with Image.open(path) as image:
                    sizes.add(image.size)
            self.assertEqual(sizes, {(720, 390)})
            playlist = folder / "captions.txt"
            main.write_caption_playlist(captions, playlist)
            lines = playlist.read_text().splitlines()
            self.assertEqual(len(lines), 2 * len(captions) + 1)
            self.assertEqual(lines[-1], f"file '{captions[-1][0].name}'")
            durations = [float(line.split()[1]) for line in lines if line.startswith("duration ")]
            self.assertAlmostEqual(sum(durations), 12, places=5)

    def test_ffmpeg_signal_diagnostic_does_not_claim_cause(self):
        result = SimpleNamespace(returncode=-9, stderr="some output")
        message = main.ffmpeg_scene_error(result, 0, 5)
        self.assertIn("signal 9", message)
        self.assertIn("scene 1/5", message)
        self.assertIn("check Railway logs and memory usage", message)


class StoryLengthTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def response(count, scenes=6):
        story = {"title": "A story", "scenes": [
            {"narration": " ".join(["word"] * count)}
            for _ in range(scenes)
        ]}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(story)))])

    async def test_short_story_is_revised_with_previous_response(self):
        create = AsyncMock(side_effect=[self.response(30), self.response(55)])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(main, "client", client):
            story = await main.write_story("friendship", "ar", "model")
        self.assertEqual(len(story["scenes"]), 6)
        messages = create.await_args_list[1].kwargs["messages"]
        self.assertEqual(messages[-2]["role"], "assistant")
        self.assertIn("180 words", messages[-1]["content"])
        self.assertIn("Expand", messages[-1]["content"])

    async def test_count_only_scenes_used_for_rendering_and_report_attempts(self):
        create = AsyncMock(side_effect=[self.response(35, 9), self.response(55)])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(main, "client", client):
            story = await main.write_story("friendship", "en", "model")
        self.assertEqual(len(story["scenes"]), 6)
        self.assertIn("280 words", create.await_args_list[1].kwargs["messages"][-1]["content"])

        create = AsyncMock(side_effect=[self.response(10), self.response(20), self.response(30)])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(main, "client", client):
            with self.assertRaisesRegex(RuntimeError, r"actual counts: 60, 120, 180"):
                await main.write_story("friendship", "ar", "model")


class ScenePipelineTest(unittest.IsolatedAsyncioTestCase):
    async def test_prepares_next_scene_while_rendering_in_order(self):
        started = asyncio.Event()
        events = []

        async def prepare(index, scene):
            events.append(f"prepare-{index}")
            if index == 1:
                started.set()
            return scene

        async def render(index, scene, prepared):
            if index == 0:
                await asyncio.wait_for(started.wait(), 1)
            events.append(f"render-{index}")
            return prepared

        clips = await main.pipeline_scenes(["first", "second"], prepare, render)
        self.assertEqual(clips, ["first", "second"])
        self.assertLess(events.index("prepare-1"), events.index("render-0"))
        self.assertLess(events.index("render-0"), events.index("render-1"))

    async def test_cancels_prefetch_when_render_fails(self):
        started, stopped = asyncio.Event(), asyncio.Event()

        async def prepare(index, scene):
            if index:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            return scene

        async def render(index, scene, prepared):
            await asyncio.wait_for(started.wait(), 1)
            raise RuntimeError("render failed")

        with self.assertRaisesRegex(RuntimeError, "render failed"):
            await main.pipeline_scenes(["first", "second"], prepare, render)
        self.assertTrue(stopped.is_set())

    async def test_preparation_failure_stops_before_rendering_that_scene(self):
        rendered = []

        async def prepare(index, scene):
            if index == 1:
                raise RuntimeError("image failed")
            return scene

        async def render(index, scene, prepared):
            rendered.append(index)
            return prepared

        with self.assertRaisesRegex(RuntimeError, "image failed"):
            await main.pipeline_scenes(["first", "second"], prepare, render)
        self.assertEqual(rendered, [0])

    async def test_image_and_voice_are_prepared_together(self):
        image_started, voice_started = asyncio.Event(), asyncio.Event()

        async def image(scene, path):
            image_started.set()
            await asyncio.wait_for(voice_started.wait(), 1)

        class Voice:
            def __init__(self, *args, **kwargs):
                pass

            async def save(self, path):
                voice_started.set()
                await asyncio.wait_for(image_started.wait(), 1)

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(main, "IMAGE_PROVIDER", "cloudflare"), \
             patch.object(main, "make_cloudflare_frame", side_effect=image), \
             patch.object(main.edge_tts, "Communicate", Voice):
            frame, audio = await main.prepare_scene(
                {"narration": "hello"}, 0, 1, "en", "voice", Path(directory)
            )
        self.assertEqual(frame.name, "artwork-0.png")
        self.assertEqual(audio.name, "voice-0.mp3")


class DashboardTest(unittest.IsolatedAsyncioTestCase):
    async def test_status_requires_key_and_reports_counts(self):
        with patch.object(main, "APP_API_KEY", "secret"):
            with self.assertRaises(main.HTTPException) as caught:
                await main.web_status("wrong")
            self.assertEqual(caught.exception.status_code, 401)
            result = await main.web_status("secret")
        self.assertIn("scheduler_running", result["settings"])
        self.assertIsInstance(result["counts"], dict)


class TelegramStatusTest(unittest.IsolatedAsyncioTestCase):
    async def test_status_responds_while_today_or_generate_button_is_running(self):
        for button in (False, True):
            with self.subTest(button=button):
                app = Application.builder().token("123:abc").build()
                app.bot._bot_user = User(id=123, first_name="Bot", is_bot=True, username="testbot")
                app._initialized = True
                app._running = True
                main.register_telegram_handlers(app)
                user = {"id": 123, "first_name": "Admin", "is_bot": False}
                chat = {"id": 123, "type": "private"}

                def command(update_id, text):
                    return Update.de_json({"update_id": update_id, "message": {
                        "message_id": update_id, "date": 1700000000, "chat": chat,
                        "from": user, "text": text,
                        "entities": [{"type": "bot_command", "offset": 0, "length": len(text)}],
                    }}, app.bot)

                if button:
                    first = Update.de_json({"update_id": 1, "callback_query": {
                        "id": "test", "from": user, "chat_instance": "chat",
                        "data": "generate:today", "message": {
                            "message_id": 1, "date": 1700000000, "chat": chat,
                            "from": {"id": 123, "first_name": "Bot", "is_bot": True},
                            "text": "start",
                        },
                    }}, app.bot)
                else:
                    first = command(1, "/today")
                entered, release = asyncio.Event(), asyncio.Event()

                async def wait_for_generation(report):
                    entered.set()
                    await release.wait()
                    return True

                with patch.object(main, "ADMIN_TELEGRAM_ID", "123"), \
                     patch.object(main, "run_today", side_effect=wait_for_generation), \
                     patch.object(Bot, "send_message", new_callable=AsyncMock) as send, \
                     patch.object(Bot, "answer_callback_query", new_callable=AsyncMock):
                    try:
                        await app.process_update(first)
                        await asyncio.wait_for(entered.wait(), 1)
                        await asyncio.wait_for(app.process_update(command(2, "/status")), 1)
                        messages = [call.kwargs["text"] for call in send.await_args_list]
                        self.assertTrue(any("لا توجد حلقات" in text or "تشغيل اليوم" in text for text in messages))
                    finally:
                        release.set()
                        await asyncio.sleep(0)
                        app._running = False

    async def test_status_shows_story_generation_before_episode_exists(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=123), message=message)
        today = main.utc_now().date().isoformat()
        with main.db() as con:
            con.execute("INSERT INTO daily_runs VALUES (?, ?, ?, ?)", (today, "ar", "generating", None))
        try:
            with patch.object(main, "ADMIN_TELEGRAM_ID", "123"):
                await main.tg_status(update, None)
            self.assertIn("العربية · generating", message.reply_text.await_args.args[0])
        finally:
            with main.db() as con:
                con.execute("DELETE FROM daily_runs WHERE run_date=? AND language='ar'", (today,))


class TelegramVideoTest(unittest.IsolatedAsyncioTestCase):
    async def test_send_failure_does_not_offer_publish_without_video(self):
        episode_id = str(uuid.uuid4())
        now = main.utc_now().isoformat()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "original.mp4"
            source.write_bytes(b"video")
            with main.db() as con:
                con.execute(
                    "INSERT INTO episodes(id,episode_date,language,topic,title,description,script,scenes,status,video_path,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (episode_id, "2024-01-04", "ar", "topic", "title", "", "", "[]",
                     "pending_review", str(source), now, now),
                )
            bot = SimpleNamespace(send_message=AsyncMock(), send_video=AsyncMock(side_effect=RuntimeError("upload failed")))
            try:
                with patch.object(main, "telegram_app", SimpleNamespace(bot=bot)), \
                     patch.object(main, "ADMIN_TELEGRAM_ID", "123"):
                    await main.send_episode_for_review(episode_id)
                self.assertIn(f"/review {episode_id}", bot.send_message.await_args.kwargs["text"])
                self.assertNotIn("reply_markup", bot.send_message.await_args.kwargs)
            finally:
                with main.db() as con:
                    con.execute("DELETE FROM episodes WHERE id=?", (episode_id,))

    async def test_large_video_sends_compressed_copy_and_keeps_original(self):
        episode_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "original.mp4"
            source.write_bytes(b"original video")
            now = main.utc_now().isoformat()
            with main.db() as con:
                con.execute(
                    "INSERT INTO episodes(id,episode_date,language,topic,title,description,script,scenes,status,video_path,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (episode_id, "2024-01-02", "ar", "topic", "title", "", "", "[]",
                     "pending_review", str(source), now, now),
                )
            bot = SimpleNamespace(send_message=AsyncMock(), send_video=AsyncMock())
            captured = []

            async def send_video(**kwargs):
                captured.append(kwargs["video"].read())

            bot.send_video.side_effect = send_video

            def compress(path, folder):
                self.assertEqual(path, source)
                result = folder / "review.mp4"
                result.write_bytes(b"compressed")
                return result

            try:
                with patch.object(main, "telegram_app", SimpleNamespace(bot=bot)), \
                     patch.object(main, "ADMIN_TELEGRAM_ID", "123"), \
                     patch.object(main, "TELEGRAM_VIDEO_LIMIT", 10), \
                     patch.object(main, "compress_for_telegram", side_effect=compress):
                    await main.send_episode_for_review(episode_id)
                self.assertEqual(captured, [b"compressed"])
                self.assertEqual(source.read_bytes(), b"original video")
                self.assertEqual(bot.send_video.await_args.kwargs["write_timeout"], 180)
            finally:
                with main.db() as con:
                    con.execute("DELETE FROM episodes WHERE id=?", (episode_id,))

    async def test_review_command_requires_admin_and_pending_episode(self):
        episode_id = str(uuid.uuid4())
        now = main.utc_now().isoformat()
        with main.db() as con:
            con.execute(
                "INSERT INTO episodes(id,episode_date,language,topic,title,description,script,scenes,status,video_path,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (episode_id, "2024-01-03", "en", "topic", "title", "", "", "[]",
                 "pending_review", "/tmp/example.mp4", now, now),
            )
        update = SimpleNamespace(effective_user=SimpleNamespace(id=123), message=SimpleNamespace(reply_text=AsyncMock()))
        context = SimpleNamespace(args=[episode_id])
        try:
            with patch.object(main, "ADMIN_TELEGRAM_ID", "123"), \
                 patch.object(main, "send_episode_for_review", new_callable=AsyncMock) as send:
                await main.tg_review(update, context)
                send.assert_awaited_once_with(episode_id)
                update.effective_user.id = 456
                await main.tg_review(update, context)
                send.assert_awaited_once()
        finally:
            with main.db() as con:
                con.execute("DELETE FROM episodes WHERE id=?", (episode_id,))


class BlenderModeTest(unittest.IsolatedAsyncioTestCase):
    async def test_old_animation_cache_is_replaced_and_new_cache_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "blender-child-v2-0.mp4").write_bytes(b"old")

            def render(command, **kwargs):
                if command[0] == "ffmpeg":
                    Path(command[-1]).write_bytes(b"new")
                return SimpleNamespace(returncode=0, stderr="")

            with patch.object(main, "DATA_DIR", path), \
                 patch.object(main, "WORK_DIR", path), \
                 patch.object(main.subprocess, "run", side_effect=render) as run:
                first = await main.blender_animation(0)
                second = await main.blender_animation(0)
            self.assertEqual(first.read_bytes(), b"new")
            self.assertEqual(second, first)
            self.assertEqual(run.call_count, 2)

    async def test_arabic_lyric_uses_basic_font_layout(self):
        used = []

        def capture_font(size):
            face = main.font(size, basic=True)
            used.append(face.layout_engine)
            return face

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lyric.png"
            main.song_studio.make_lyric_image("سلحفاة سميرة تساعد الأصدقاء", "ar", path, capture_font, main.shape_text)
            self.assertTrue(path.is_file())
            self.assertTrue(all(layout == ImageFont.Layout.BASIC for layout in used))

    async def test_daily_run_uses_only_configured_language(self):
        create = AsyncMock(return_value={"status": "pending_review", "id": "test"})
        with patch.object(main, "DAILY_LANGUAGES", ("ar",)), \
             patch.object(main, "create_daily_episode", create), \
             patch.object(main, "send_episode_for_review", new_callable=AsyncMock) as send:
            await main.run_today(AsyncMock())
        self.assertEqual(create.await_count, 1)
        self.assertEqual(create.await_args.args[0], "ar")
        send.assert_awaited_once_with("test")

    async def test_blender_song_does_not_request_cloudflare(self):
        episode = {"id": "test", "language": "ar"}
        scenes = [{"type": "verse", "lines": ["hello"]}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with patch.object(main, "VIDEO_PROVIDER", "blender"), \
                 patch.object(main, "IMAGE_PROVIDER", "cards"), \
                 patch.object(main, "SONG_PROVIDER", "tts"), \
                 patch.object(main, "db") as db, \
                 patch.object(main, "blender_animation", new_callable=AsyncMock, return_value=path / "loop.mp4"), \
                 patch.object(main.song_studio, "make_tts_song", new_callable=AsyncMock, return_value=(path / "song.m4a", [])), \
                 patch.object(main.song_studio, "render_song_video", new_callable=AsyncMock) as render, \
                 patch.object(main, "make_cloudflare_frame", new_callable=AsyncMock) as cloudflare:
                await main.render_song_episode(episode, scenes, {}, path)
                self.assertEqual(render.await_args.kwargs["prebuilt_motion"], [path / "loop.mp4"] * 3)
                cloudflare.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
