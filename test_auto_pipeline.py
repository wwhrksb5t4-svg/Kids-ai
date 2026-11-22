import asyncio
import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image, ImageDraw

from pipeline import animation, characters, guard, images, music, publish, script, video, voice
from pipeline.bot_commands import register_auto_handlers
from pipeline.scheduler import AutoStudio


class CharacterAndScriptTest(unittest.IsolatedAsyncioTestCase):
    async def test_gemini_failure_falls_back_to_groq(self):
        requests = []

        def respond(request):
            requests.append(request.url.host)
            if request.url.host == "generativelanguage.googleapis.com":
                return httpx.Response(429)
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test", "GROQ_API_KEY": "test"}), \
             patch.object(script.httpx, "AsyncClient", return_value=client), \
             patch.object(script.asyncio, "sleep", new_callable=AsyncMock):
            self.assertEqual(await script.generate_text("hello"), "ok")
        self.assertEqual(requests, ["generativelanguage.googleapis.com"] * 3 + ["api.groq.com"])

    async def test_characters_and_script_validation(self):
        source = characters.load_characters()
        self.assertEqual(len(source), 4)
        prompt = characters.build_image_prompt("Counting blocks.", ["mimo", "tavi"])
        self.assertIn(source[0]["fixed_visual_description"], prompt)
        self.assertEqual(characters.get_seed("mimo"), source[0]["seed"])
        response = {
            "title": "Counting together", "description": "Friends count blocks.", "tags": ["counting"],
            "scenes": [
                {"character_ids": ["mimo"], "lyric": f"Mimo counts {index}\nPlay all day",
                 "visual_description": f"Counting blocks in a garden {index}.",
                 "location": f"garden {index}", "camera_angle": f"angle {index}", "duration": 5}
                for index in range(6)
            ],
        }
        llm = AsyncMock(side_effect=['["mimo","tavi"]', "invalid", json.dumps(response)])
        result = await script.create_script("counting blocks", "en", llm=llm)
        self.assertEqual(len(result["scenes"]), 6)
        self.assertEqual(llm.await_count, 3)

    def test_originality_guard_and_invalid_scene(self):
        guard.check_originality("A turtle learns colors.")
        with self.assertRaises(ValueError):
            guard.check_originality("Baby Shark visits a garden")
        with self.assertRaises(ValueError):
            script._validate_script({"title": "x", "description": "x", "tags": ["x"], "scenes": []}, {"mimo"})

    async def test_cloud_image_retries_blank_frame(self):
        blank = Image.new("RGB", images.SIZE, "white")
        valid = Image.effect_noise(images.SIZE, 64).convert("RGB")
        with tempfile.TemporaryDirectory() as folder, patch.object(
            images, "_generate_once", new=AsyncMock(side_effect=[blank, valid])
        ):
            output = await images.generate_image("original garden", 99, Path(folder) / "image.png")
            with Image.open(output) as image:
                self.assertEqual(image.size, images.SIZE)

    async def test_cloud_image_uses_remote_workers_ai(self):
        requests = []
        artwork = Image.effect_noise((256, 144), 30).convert("RGB")
        encoded = io.BytesIO()
        artwork.save(encoded, format="PNG")
        encoded.seek(0)
        raw = encoded.read()
        encoded.close()
        client_class = httpx.AsyncClient

        def respond(request):
            requests.append(request)
            self.assertIn("/ai/run/@cf/black-forest-labs/flux-1-schnell", str(request.url))
            self.assertEqual(json.loads(request.content), {"prompt": "same character prompt"})
            return httpx.Response(200, json={
                "success": True, "result": {"image": base64.b64encode(raw).decode()}
            })

        client = client_class(transport=httpx.MockTransport(respond))
        with patch.object(images.httpx, "AsyncClient", return_value=client), \
             patch.dict(os.environ, {"CLOUDFLARE_ACCOUNT_ID": "a" * 32, "CLOUDFLARE_API_TOKEN": "token"}):
            result = await images._generate_once("same character prompt")
        self.assertEqual(result.size, images.SIZE)
        self.assertEqual(len(requests), 1)

    async def test_cloudflare_failure_falls_back_to_pollinations(self):
        image = Image.effect_noise(images.SIZE, 30).convert("RGB")
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(os.environ, {"CLOUDFLARE_ACCOUNT_ID": "a" * 32, "CLOUDFLARE_API_TOKEN": "token"}), \
             patch.object(images, "_cloudflare_once", new=AsyncMock(side_effect=RuntimeError("quota"))), \
             patch.object(images, "_pollinations_once", new=AsyncMock(return_value=image)) as pollinations:
            output = await images.generate_image("Mimo at a new place", 45, Path(folder) / "scene.png")
            self.assertTrue(output.is_file())
        pollinations.assert_awaited_once()

    async def test_pollinations_request_uses_unique_seed_and_requested_dimensions(self):
        requests = []
        artwork = Image.effect_noise((256, 144), 30).convert("RGB")
        encoded = io.BytesIO()
        artwork.save(encoded, format="PNG")
        raw = encoded.getvalue()
        client_class = httpx.AsyncClient

        def respond(request):
            requests.append(request)
            self.assertEqual(request.url.host, "image.pollinations.ai")
            self.assertEqual(request.url.params["width"], "1280")
            self.assertEqual(request.url.params["height"], "720")
            self.assertEqual(request.url.params["seed"], "321")
            return httpx.Response(200, content=raw)

        client = client_class(transport=httpx.MockTransport(respond))
        with patch.object(images.httpx, "AsyncClient", return_value=client), \
             patch.dict(os.environ, {"CLOUDFLARE_ACCOUNT_ID": "", "CLOUDFLARE_API_TOKEN": ""}):
            result = await images._generate_once("unique place, overhead angle", seed=321)
        self.assertEqual(result.size, images.SIZE)
        self.assertEqual(len(requests), 1)

    async def test_duplicate_ai_images_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            one, two = Path(folder) / "one.png", Path(folder) / "two.png"
            Image.new("RGB", (256, 144), "red").save(one)
            Image.new("RGB", (256, 144), "red").save(two)
            with self.assertRaisesRegex(RuntimeError, "duplicate image"):
                images.ensure_distinct_images([one, two])

    async def test_paid_animation_adapters_and_daily_guard(self):
        for provider, model in animation.FAL_MODELS.items():
            endpoint, body = animation._fal_body(provider, "move", "data:image/png;base64,abc", True)
            self.assertEqual(endpoint, model)
            self.assertIn("prompt", body)
            self.assertIn("image_url" if provider != "kling" else "start_image_url", body)
        self.assertEqual(animation._fal_body("hailuo", "move", "data:image/png;base64,abc", True)[1]["duration"], "6")
        self.assertEqual(animation._fal_body("pixverse", "move", "data:image/png;base64,abc", False)[1]["resolution"], "720p")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            artwork = root / "art.png"
            Image.effect_noise((128, 128), 30).save(artwork)
            destination = root / "motion.mp4"
            with patch.dict(os.environ, {"FAL_KEY": "test-key", "VIDEO_API_MAX_SCENES_PER_DAY": "1"}), \
                 patch.object(animation, "_fal_motion", new=AsyncMock(return_value=b"mp4")) as generate, \
                 patch.object(animation, "_finalize"):
                await animation.animate_scene(
                    {"visual_description": "wave"}, artwork, destination,
                    provider="kling", db_path=root / "db.sqlite3",
                )
                self.assertEqual(animation.daily_usage(root / "db.sqlite3"), (1, 1))
                self.assertEqual(
                    animation.daily_usage_details(root / "db.sqlite3"),
                    (1, 1, 0.75, 10.0),
                )
                with self.assertRaisesRegex(RuntimeError, "Daily AI video limit"):
                    await animation.animate_scene(
                        {"visual_description": "wave"}, artwork, root / "second.mp4",
                        provider="kling", db_path=root / "db.sqlite3",
                    )
            generate.assert_awaited_once()

    async def test_daily_estimated_cost_guard_blocks_before_paid_request(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            artwork = root / "art.png"
            Image.effect_noise((128, 128), 30).save(artwork)
            with patch.dict(os.environ, {
                "FAL_KEY": "test-key",
                "VIDEO_API_MAX_SCENES_PER_DAY": "8",
                "VIDEO_API_DAILY_BUDGET_USD": "1",
                "VIDEO_ESTIMATED_COST_VEO_USD": "1.70",
            }), patch.object(animation, "_fal_motion", new=AsyncMock()) as generate:
                with self.assertRaisesRegex(RuntimeError, "Estimated AI video budget reached"):
                    await animation.animate_scene(
                        {"visual_description": "wave"}, artwork, root / "motion.mp4",
                        provider="veo", db_path=root / "db.sqlite3",
                    )
            generate.assert_not_awaited()
            self.assertEqual(animation.daily_usage(root / "db.sqlite3"), (0, 8))

    async def test_fal_request_poll_and_download_use_documented_contract(self):
        requests = []
        client_class = httpx.AsyncClient

        def respond(request):
            requests.append(request)
            if request.method == "POST":
                self.assertEqual(request.url.path, "/fal-ai/minimax/hailuo-2.3-fast/standard/image-to-video")
                body = json.loads(request.content)
                self.assertEqual(body["duration"], "6")
                self.assertTrue(body["image_url"].startswith("data:image/png;base64,"))
                return httpx.Response(200, json={
                    "status_url": "https://queue.fal.run/status/1",
                    "response_url": "https://queue.fal.run/result/1",
                })
            if request.url.path == "/status/1":
                return httpx.Response(200, json={"status": "COMPLETED"})
            if request.url.path == "/result/1":
                return httpx.Response(200, json={"video": {"url": "https://cdn.fal.media/output.mp4"}})
            return httpx.Response(200, content=b"video-bytes")

        client = client_class(transport=httpx.MockTransport(respond))
        with tempfile.TemporaryDirectory() as folder:
            artwork = Path(folder) / "image.png"
            Image.effect_noise((128, 128), 30).save(artwork)
            with patch.object(animation.httpx, "AsyncClient", return_value=client), \
                 patch.dict(os.environ, {"FAL_KEY": "test-key"}):
                result = await animation._fal_motion(
                    "hailuo", "gentle movement", artwork, (1280, 720)
                )
        self.assertEqual(result, b"video-bytes")
        self.assertEqual(len(requests), 4)

    async def test_runway_uses_official_image_to_video_task_flow(self):
        requests = []
        client_class = httpx.AsyncClient

        def respond(request):
            requests.append(request)
            if request.method == "POST":
                self.assertEqual(request.url.host, "api.dev.runwayml.com")
                body = json.loads(request.content)
                self.assertEqual(body["model"], "gen4_turbo")
                self.assertTrue(body["promptImage"].startswith("data:image/png;base64,"))
                return httpx.Response(200, json={"id": "task-1"})
            if request.url.path == "/v1/tasks/task-1":
                return httpx.Response(200, json={
                    "status": "SUCCEEDED", "output": ["https://cdn.runwayml.com/output.mp4"]
                })
            return httpx.Response(200, content=b"runway-video")

        client = client_class(transport=httpx.MockTransport(respond))
        with tempfile.TemporaryDirectory() as folder:
            artwork = Path(folder) / "image.png"
            Image.effect_noise((128, 128), 30).save(artwork)
            with patch.object(animation.httpx, "AsyncClient", return_value=client), \
                 patch.dict(os.environ, {"RUNWAY_API_KEY": "test-key"}):
                result = await animation._runway_motion("gentle movement", artwork, (1280, 720))
        self.assertEqual(result, b"runway-video")
        self.assertEqual(len(requests), 3)

    async def test_music_library_and_automatic_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "music.mp3").write_bytes(b"track")
            with patch.dict(os.environ, {"MUSICGEN_NONCOMMERCIAL_CONFIRMED": "1"}), \
                 patch.object(music, "_generate_local", side_effect=RuntimeError("unavailable")):
                result = await music.get_music("auto", root, root / "work")
            self.assertEqual(result.name, "music.mp3")
            with self.assertRaises(RuntimeError):
                music.choose_library_track(root / "missing")

    async def test_bilingual_voice_uses_arabic_voice_and_joins_audio(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("FFmpeg is required")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tone = root / "tone.mp3"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.25",
                 "-c:a", "libmp3lame", "-y", str(tone)], check=True,
            )
            chosen = []

            class Voice:
                def __init__(self, text, voice, **settings):
                    chosen.append(voice)

                async def save(self, path):
                    shutil.copyfile(tone, path)

            with patch.object(voice.edge_tts, "Communicate", Voice), patch.dict(os.environ, {"VOICE_SONG_AR": "ar-SA-ZariyahNeural"}):
                output = await voice.generate_voice(
                    {"character_ids": ["mimo"], "lyric": "Hello friend\nمرحبا يا صديق"},
                    "both", root / "scene.mp3",
                )
            self.assertTrue(output.is_file())
            self.assertEqual(chosen, ["en-US-AnaNeural", "ar-SA-ZariyahNeural"])


class SchedulerAndPublishingTest(unittest.IsolatedAsyncioTestCase):
    def studio(self, root):
        return AutoStudio(
            db_path=root / "studio.sqlite3", data_dir=root, bot=SimpleNamespace(send_message=AsyncMock()),
            admin_ids=[123], channel_id="-1001", max_uploads=1,
            youtube_services=lambda: (None, None), compress_for_telegram=None,
            generation_lock=asyncio.Lock(), video_provider=lambda: "none",
        )

    async def test_pending_topics_and_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            studio = self.studio(Path(folder))
            studio.init()
            job_id = studio.enqueue("friendly counting", "auto", pending=True)
            studio.set_setting("auto_enabled", "1")
            await studio.schedule_topic()
            with studio.db() as con:
                self.assertEqual(con.execute("SELECT status FROM auto_jobs WHERE id=?", (job_id,)).fetchone()["status"], "queued")
                con.execute("UPDATE auto_jobs SET status='running' WHERE id=?", (job_id,))
            studio.init()
            with studio.db() as con:
                self.assertEqual(con.execute("SELECT status FROM auto_jobs WHERE id=?", (job_id,)).fetchone()["status"], "queued")
            studio.auto_off()
            with studio.db() as con:
                self.assertEqual(con.execute("SELECT status FROM auto_jobs WHERE id=?", (job_id,)).fetchone()["status"], "cancelled")

    async def test_admin_commands_preserve_pending_topics_and_reject_nonadmins(self):
        from telegram.ext import Application

        with tempfile.TemporaryDirectory() as folder:
            studio = self.studio(Path(folder))
            studio.init()
            app = Application.builder().token("123:abc").build()
            register_auto_handlers(app, studio, lambda update: update.effective_user.id == 123)
            commands = {
                next(iter(handler.commands)): handler.callback
                for handler in app.handlers[0]
            }
            message = SimpleNamespace(reply_text=AsyncMock())
            update = SimpleNamespace(effective_user=SimpleNamespace(id=456), message=message)
            await commands["addtopic"](update, SimpleNamespace(args=["counting", "colors"]))
            with studio.db() as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM auto_jobs").fetchone()[0], 0)
            update.effective_user.id = 123
            await commands["addtopic"](update, SimpleNamespace(args=["counting", "colors"]))
            with studio.db() as con:
                row = con.execute("SELECT topic,status FROM auto_jobs").fetchone()
            self.assertEqual(tuple(row), ("counting colors", "pending"))
            self.assertTrue({"create", "auto_on", "auto_off", "video_usage", "characters", "preview", "newchar", "delchar"} <= commands.keys())

    async def test_publication_is_not_duplicated_and_limit_is_enforced(self):
        class Request:
            def execute(self):
                return {"id": "youtube-id"}

        class YouTube:
            def __init__(self):
                self.uploads = 0

            def videos(self):
                return self

            def thumbnails(self):
                return self

            def insert(self, **kwargs):
                self.uploads += 1
                return Request()

            def set(self, **kwargs):
                return Request()

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, thumbnail = root / "video.mp4", root / "thumb.jpg"
            source.write_bytes(b"video")
            thumbnail.write_bytes(b"image")
            bot = SimpleNamespace(
                send_photo=AsyncMock(return_value=SimpleNamespace(message_id=1)),
                send_video=AsyncMock(return_value=SimpleNamespace(message_id=2)),
            )
            youtube = YouTube()
            kwargs = dict(
                bot=bot, channel_id="-1001", youtube=youtube, db_path=root / "db.sqlite",
                max_uploads=1, compress_for_telegram=None,
            )
            story = {"title": "title", "description": "description", "tags": ["kids"]}
            first = await publish.publish("job1", story, source, thumbnail, **kwargs)
            second = await publish.publish("job1", story, source, thumbnail, **kwargs)
            self.assertEqual(first, second)
            self.assertEqual(youtube.uploads, 1)
            bot.send_video.assert_awaited_once()
            with self.assertRaisesRegex(RuntimeError, "daily"):
                await publish.publish("job2", story, source, thumbnail, **kwargs)

    async def test_uncertain_upload_is_not_automatically_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db_path = root / "db.sqlite"
            publish.init_uploads(db_path)
            publish._row(db_path, "job")
            publish._reserve_upload(db_path, "job", 1)
            with self.assertRaisesRegex(RuntimeError, "uncertain"):
                publish._reserve_upload(db_path, "job", 2)

    async def test_job_progress_and_publication_end_to_end(self):
        class Request:
            def execute(self):
                return {"id": "youtube-id"}

        class YouTube:
            def videos(self):
                return self

            def thumbnails(self):
                return self

            def insert(self, **kwargs):
                return Request()

            def set(self, **kwargs):
                return Request()

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            studio = self.studio(root)
            studio.bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=1))
            studio.bot.send_video = AsyncMock(return_value=SimpleNamespace(message_id=2))
            studio.youtube_services = lambda: (YouTube(), None)
            studio.init()
            job_id = studio.enqueue("friendly counting")
            with studio.db() as con:
                job = dict(con.execute("SELECT * FROM auto_jobs WHERE id=?", (job_id,)).fetchone())
            story = {
                "title": "Count", "description": "Count together", "tags": ["counting"],
                "scenes": [
                    {"character_ids": ["mimo"], "lyric": f"Mimo count {index}", "visual_description": f"Counting {index}",
                     "location": f"garden {index}", "camera_angle": f"angle {index}", "duration": 3}
                    for index in range(6)
                ],
            }

            async def write_image(prompt, seed, destination):
                destination.write_bytes(b"image")

            async def write_voice(scene, language, destination, **kwargs):
                destination.write_bytes(b"voice")

            async def get_track(mode, library, directory):
                path = library / "track.wav"
                path.write_bytes(b"music")
                return path

            async def render(story, pictures, voices, track, directory, **kwargs):
                self.assertEqual(len(pictures), 6)
                self.assertEqual(kwargs["motions"], [None] * 6)
                output, thumbnail = directory / "final.mp4", directory / "thumbnail.jpg"
                output.write_bytes(b"video")
                thumbnail.write_bytes(b"image")
                return output, thumbnail

            with patch.object(script, "create_script", new=AsyncMock(return_value=story)), \
                 patch.object(images, "generate_image", side_effect=write_image), \
                 patch.object(voice, "generate_voice", side_effect=write_voice), \
                 patch.object(music, "get_music", side_effect=get_track), \
                 patch.object(video, "render_video", side_effect=render):
                await studio.process(job)
            with studio.db() as con:
                result = con.execute("SELECT status,youtube_url FROM auto_jobs WHERE id=?", (job_id,)).fetchone()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["youtube_url"], "https://www.youtube.com/watch?v=youtube-id")
            self.assertFalse((studio.work_dir / job_id).exists())
            studio.bot.send_video.assert_awaited_once()


class RenderingTest(unittest.IsolatedAsyncioTestCase):
    async def test_video_subtitle_has_no_box_and_sits_in_lower_third(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "caption.png"
            await asyncio.to_thread(video._text_image, "Mimo says hello", path)
            with Image.open(path) as caption:
                alpha = caption.getchannel("A")
                bounds = alpha.getbbox()
                self.assertEqual(alpha.getpixel((0, 0)), 0)
                self.assertGreater(bounds[1], 800)
                self.assertLess(bounds[3], 1080)

    async def test_rembg_character_cutout_produces_parallax_layers(self):
        source = Image.new("RGB", (256, 144), (60, 120, 180))
        source_bytes = io.BytesIO()
        source.save(source_bytes, format="PNG")
        cutout = Image.new("RGBA", source.size, (0, 0, 0, 0))
        ImageDraw.Draw(cutout).ellipse((80, 20, 180, 130), fill=(240, 80, 40, 255))
        cutout_bytes = io.BytesIO()
        cutout.save(cutout_bytes, format="PNG")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image, background, foreground = root / "scene.png", root / "bg.png", root / "fg.png"
            source.save(image)
            with patch.object(video, "_REMBG_SESSION", None), \
                 patch("rembg.new_session", return_value=object()) as new_session, \
                 patch("rembg.remove", return_value=cutout_bytes.getvalue()) as remove:
                await asyncio.to_thread(video._separate_character, image, background, foreground)
            new_session.assert_called_once_with("u2net")
            remove.assert_called_once()
            with Image.open(foreground) as layer:
                alpha = layer.getchannel("A")
                self.assertGreater(alpha.getbbox()[2], 0)
                self.assertLess(sum(alpha.getdata()) / (255 * 256 * 144), 0.5)
            self.assertTrue(background.is_file())

    async def test_real_render_is_1080p(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("FFmpeg is required")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            artwork, audio, track = root / "image.png", root / "voice.mp3", root / "music.wav"
            Image.effect_noise((1280, 720), 40).convert("RGB").save(artwork)
            for path, seconds in ((audio, 0.7), (track, 3)):
                subprocess.run(
                    ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                     "-i", f"sine=frequency=440:duration={seconds}", str(path)], check=True,
                )
            story = {"title": "ألوان جميلة", "scenes": [{"lyric": "هيا نلعب", "duration": 1}]}
            def separate(image, background, foreground):
                with Image.open(image) as source:
                    rgb = source.convert("RGB")
                    rgb.save(background)
                    layer = Image.new("RGBA", rgb.size, (0, 0, 0, 0))
                    layer.paste(rgb.crop((400, 180, 850, 600)), (400, 180))
                    mask = Image.new("L", rgb.size, 0)
                    ImageDraw.Draw(mask).ellipse((400, 180, 850, 600), fill=255)
                    layer.putalpha(mask)
                    layer.save(foreground)
            with patch.object(video, "_separate_character", side_effect=separate):
                output, thumb = await video.render_video(story, [artwork], [audio], track, root)
            dimensions = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=width,height", "-of", "csv=s=x:p=0", str(output)],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(dimensions, "1920x1080")
            self.assertLess(thumb.stat().st_size, 2_000_000)
            self.assertTrue((root / "parallax-v2-0.mp4").is_file())
            motion = root / "motion.mp4"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24",
                 "-t", "1", "-pix_fmt", "yuv420p", "-y", str(motion)],
                check=True,
            )
            moving_output, _ = await video.render_video(
                story, [artwork], [audio], track, root / "moving", motions=[motion]
            )
            moving_dimensions = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=width,height", "-of", "csv=s=x:p=0", str(moving_output)],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(moving_dimensions, "1920x1080")


if __name__ == "__main__":
    unittest.main()
