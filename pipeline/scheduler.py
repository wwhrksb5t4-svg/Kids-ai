"""Durable topic queue and opt-in automatic nursery-rhyme production."""

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from pipeline import animation, images, music, publish, script, voice, video
from pipeline.characters import DEFAULT_LIBRARY, build_image_prompt, get_seed
from pipeline.guard import check_originality


logger = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc).isoformat()


class AutoStudio:
    def __init__(self, *, db_path, data_dir, bot, admin_ids, channel_id, max_uploads,
                 youtube_services, compress_for_telegram, generation_lock, video_provider=None):
        self.db_path = Path(db_path)
        self.data_dir = Path(data_dir)
        self.bot = bot
        self.admin_ids = admin_ids
        self.channel_id = channel_id
        self.max_uploads = max_uploads
        self.youtube_services = youtube_services
        self.compress_for_telegram = compress_for_telegram
        self.generation_lock = generation_lock
        self.video_provider = video_provider or (lambda: os.getenv("VIDEO_PROVIDER", "none"))
        self.character_path = self.data_dir / "characters.json"
        self.work_dir = self.data_dir / "auto-work"
        self.music_dir = Path(os.getenv("MUSIC_LIBRARY_PATH") or self.data_dir / "music")
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self.worker = None

    def db(self):
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def init(self):
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.music_dir.mkdir(parents=True, exist_ok=True)
        if not self.character_path.exists():
            shutil.copyfile(DEFAULT_LIBRARY, self.character_path)
        with self.db() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS auto_jobs (
                id TEXT PRIMARY KEY, topic TEXT NOT NULL, origin TEXT NOT NULL,
                status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL, youtube_url TEXT,
                language TEXT, music_mode TEXT
            )""")
            columns = {row["name"] for row in con.execute("PRAGMA table_info(auto_jobs)")}
            for name in ("language", "music_mode"):
                if name not in columns:
                    con.execute(f"ALTER TABLE auto_jobs ADD COLUMN {name} TEXT")
            con.execute("CREATE TABLE IF NOT EXISTS auto_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            con.execute("UPDATE auto_jobs SET status='queued' WHERE status='running'")
        publish.init_uploads(self.db_path)

    def setting(self, key, default):
        with self.db() as con:
            row = con.execute("SELECT value FROM auto_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key, value):
        with self.db() as con:
            con.execute(
                "INSERT INTO auto_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    async def notify(self, message):
        for admin_id in self.admin_ids:
            try:
                await self.bot.send_message(chat_id=admin_id, text=message[:4000])
            except Exception:
                logger.exception("Could not send production progress to an admin.")

    def enqueue(self, topic, origin="manual", *, pending=False):
        topic = topic.strip()
        if not 3 <= len(topic) <= 500:
            raise ValueError("Topic must contain 3-500 characters.")
        check_originality(topic)
        job_id = str(uuid.uuid4())
        with self.db() as con:
            con.execute(
                "INSERT INTO auto_jobs(id,topic,origin,status,created_at,language,music_mode) VALUES(?,?,?,?,?,?,?)",
                (job_id, topic, origin, "pending" if pending else "queued", _now(),
                 self.setting("language", "ar"), self.setting("music", os.getenv("MUSIC_MODE", "library"))),
            )
        return job_id

    async def schedule_topic(self):
        if self.setting("auto_enabled", "0") != "1":
            return
        with self.db() as con:
            if con.execute("SELECT 1 FROM auto_jobs WHERE status IN ('queued','running') LIMIT 1").fetchone():
                return
            pending = con.execute("SELECT id FROM auto_jobs WHERE status='pending' ORDER BY created_at LIMIT 1").fetchone()
            if pending:
                con.execute(
                    "UPDATE auto_jobs SET status='queued',origin='auto',language=?,music_mode=? WHERE id=?",
                    (self.setting("language", "ar"), self.setting("music", os.getenv("MUSIC_MODE", "library")), pending["id"]),
                )
                return
            history = [row["topic"] for row in con.execute("SELECT topic FROM auto_jobs ORDER BY created_at DESC LIMIT 30")]
        try:
            text = await script.generate_text(
                "Invent one short, original and safe nursery-rhyme topic for toddlers about counting, colors, "
                "animals, manners or bedtime. Return only the topic, without quotation marks. "
                f"Do not repeat these topics: {json.dumps(history, ensure_ascii=False)}"
            )
            job_id = self.enqueue(text.strip().strip("\"'"), "auto")
            await self.notify(f"بدأ طلب تلقائي جديد في الطابور: {job_id}")
        except Exception as exc:
            logger.exception("Could not create an automatic topic.")
            await self.notify(f"تعذر إنشاء موضوع تلقائي: {str(exc)[:500]}")

    def configure_schedule(self, hours):
        self.scheduler.add_job(
            self.schedule_topic, "interval", hours=hours, id="automatic",
            replace_existing=True, next_run_time=datetime.now(timezone.utc) + timedelta(hours=hours),
            max_instances=1, coalesce=True,
        )

    def auto_on(self, hours):
        if not 1 <= hours <= 168:
            raise ValueError("Interval must be between 1 and 168 hours.")
        self.set_setting("auto_hours", hours)
        self.set_setting("auto_enabled", "1")
        self.configure_schedule(hours)

    def auto_off(self):
        self.set_setting("auto_enabled", "0")
        if self.scheduler.get_job("automatic"):
            self.scheduler.remove_job("automatic")
        with self.db() as con:
            con.execute("UPDATE auto_jobs SET status='cancelled' WHERE origin='auto' AND status='queued'")

    async def start(self):
        self.init()
        self.scheduler.start()
        if self.setting("auto_enabled", "0") == "1":
            self.configure_schedule(float(self.setting("auto_hours", "24")))
        self.worker = asyncio.create_task(self.worker_loop())

    async def stop(self):
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    async def worker_loop(self):
        while True:
            try:
                with self.db() as con:
                    row = con.execute("SELECT * FROM auto_jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
                if row:
                    await self.process(dict(row))
                else:
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Automatic production worker failed.")
                await asyncio.sleep(5)

    async def process(self, job):
        async with self.generation_lock:
            with self.db() as con:
                changed = con.execute(
                    "UPDATE auto_jobs SET status='running',error=NULL WHERE id=? AND status='queued'",
                    (job["id"],),
                ).rowcount
            if not changed:
                return
            directory = self.work_dir / job["id"]
            directory.mkdir(parents=True, exist_ok=True)
            try:
                await self.notify(f"المهمة {job['id']}: كتابة السيناريو.")
                script_path = directory / "script.json"
                if script_path.is_file():
                    story = json.loads(script_path.read_text(encoding="utf-8"))
                else:
                    story = await script.create_script(
                        job["topic"], job["language"] or "ar", character_path=self.character_path
                    )
                    temporary = directory / "script.tmp.json"
                    temporary.write_text(json.dumps(story, ensure_ascii=False), encoding="utf-8")
                    temporary.replace(script_path)
                language = job["language"] or "ar"
                pictures, voices, motions = [], [], []
                provider = self.video_provider()
                for index, scene in enumerate(story["scenes"]):
                    await self.notify(f"المهمة {job['id']}: الصورة والصوت للمشهد {index + 1}/{len(story['scenes'])}.")
                    picture = directory / f"picture-{index}.png"
                    audio = directory / f"voice-{index}.mp3"
                    motion = directory / f"motion-{index}.mp4"
                    if not picture.is_file():
                        prompt = build_image_prompt(scene["visual_description"], scene["character_ids"], path=self.character_path)
                        await images.generate_image(prompt, get_seed(scene["character_ids"][0], path=self.character_path), picture)
                    if not audio.is_file():
                        await voice.generate_voice(scene, language, audio, character_path=self.character_path)
                    if provider not in ("none", "hf", "blender") and not motion.is_file():
                        await self.notify(f"المهمة {job['id']}: تحريك المشهد {index + 1}/{len(story['scenes'])} عبر {provider}.")
                        await animation.animate_scene(
                            scene, picture, motion, provider=provider, db_path=self.db_path
                        )
                    elif provider == "hf" and not motion.is_file():
                        from main import make_hf_motion

                        await make_hf_motion(scene, picture, motion)
                    elif provider == "blender":
                        raise RuntimeError("Blender loops are not available in the automatic nursery pipeline.")
                    pictures.append(picture)
                    voices.append(audio)
                    motions.append(motion if motion.is_file() else None)
                await self.notify(f"المهمة {job['id']}: تجهيز الموسيقى والمونتاج.")
                mode = job["music_mode"] or "library"
                track = await music.get_music(mode, self.music_dir, directory)
                story["music_generated"] = track.parent == directory
                output, thumbnail = await video.render_video(
                    story, pictures, voices, track, directory, motions=motions
                )
                if job["origin"] == "auto" and self.setting("auto_enabled", "0") != "1":
                    raise RuntimeError("تم إيقاف التشغيل التلقائي قبل النشر.")
                used, limit, estimated, budget = animation.daily_usage_details(self.db_path)
                await self.notify(
                    f"استخدام تحريك المشاهد اليوم: {used}/{limit}، "
                    f"التكلفة المقدّرة ${estimated:.2f}/${budget:.2f} (UTC)."
                )
                await self.notify(f"المهمة {job['id']}: إرسال الفيديو ورفعه إلى YouTube.")
                youtube, _ = self.youtube_services()
                url = await publish.publish(
                    job["id"], story, output, thumbnail, bot=self.bot, channel_id=self.channel_id,
                    youtube=youtube, db_path=self.db_path, max_uploads=self.max_uploads,
                    compress_for_telegram=self.compress_for_telegram,
                )
                with self.db() as con:
                    con.execute("UPDATE auto_jobs SET status='completed',youtube_url=? WHERE id=?", (url, job["id"]))
                await self.notify(f"اكتمل النشر: {url}")
                try:
                    shutil.rmtree(directory)
                except OSError:
                    logger.exception("Could not clean automatic job files for %s.", job["id"])
                    await self.notify(f"اكتمل النشر، لكن تعذر حذف الملفات المؤقتة للمهمة {job['id']}.")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Automatic job %s failed.", job["id"])
                with self.db() as con:
                    con.execute("UPDATE auto_jobs SET status='failed',error=? WHERE id=?", (str(exc)[:1000], job["id"]))
                await self.notify(f"فشلت المهمة {job['id']}: {str(exc)[:700]}")
