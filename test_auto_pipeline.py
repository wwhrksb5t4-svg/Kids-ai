import asyncio
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
from PIL import Image

from pipeline import characters, guard, images, music, publish, script, video, voice
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
                {"character_ids": ["mimo"], "lyric": "Count and play\nAll the day",
                 "visual_description": "Counting blocks in a garden.", "duration": 5}
                for _ in range(6)
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

    async def test_local_image_retries_blank_frame(self):
        calls = []

        def generate(prompt, seed):
            calls.append(seed)
            if len(calls) == 1:
                return Image.new("RGB", images.SIZE, "white")
            return Image.effect_noise(images.SIZE, 64).convert("RGB")

        with tempfile.TemporaryDirectory() as folder, patch.object(images, "_generate_once", side_effect=generate):
            output = await images.generate_image("original garden", 99, Path(folder) / "image.png")
            self.assertEqual(calls, [99, 100])
            with Image.open(output) as image:
                self.assertEqual(image.size, images.SIZE)

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
            generation_lock=asyncio.Lock(),
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
            self.assertTrue({"create", "auto_on", "auto_off", "characters", "preview", "newchar", "delchar"} <= commands.keys())

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
                    {"character_ids": ["mimo"], "lyric": "Count and play", "visual_description": "Counting",
                     "duration": 3} for _ in range(6)
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

            async def render(story, pictures, voices, track, directory):
                self.assertEqual(len(pictures), 6)
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
            output, thumb = await video.render_video(story, [artwork], [audio], track, root)
            dimensions = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=width,height", "-of", "csv=s=x:p=0", str(output)],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(dimensions, "1920x1080")
            self.assertLess(thumb.stat().st_size, 2_000_000)


if __name__ == "__main__":
    unittest.main()
