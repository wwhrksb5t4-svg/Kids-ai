import asyncio
import json
import os
import re
import sqlite3
import subprocess
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import edge_tts
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from openai import AsyncOpenAI
from PIL import Image, ImageDraw, ImageFont
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data"))).resolve()
MEDIA_DIR = DATA_DIR / "media"
WORK_DIR = DATA_DIR / "work"
for folder in (DATA_DIR, MEDIA_DIR, WORK_DIR):
    folder.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "kids_haven.sqlite3"
TOKEN_PATH = DATA_DIR / "google-token.json"

FASTLLM_API_KEY = (os.getenv("FASTLLM_API_KEY") or "").strip()
FASTLLM_BASE_URL = (os.getenv("FASTLLM_BASE_URL") or "https://fastllm.top/v1").strip().rstrip("/")
FASTLLM_MODEL = (os.getenv("FASTLLM_MODEL") or "auto").strip()
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
ADMIN_TELEGRAM_ID = (os.getenv("ADMIN_TELEGRAM_ID") or "").strip()
APP_API_KEY = (os.getenv("APP_API_KEY") or "").strip()
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
GOOGLE_CLIENT_ID = (os.getenv("GOOGLE_CLIENT_ID") or "").strip()
GOOGLE_CLIENT_SECRET = (os.getenv("GOOGLE_CLIENT_SECRET") or "").strip()
GOOGLE_REDIRECT_URI = (os.getenv("GOOGLE_REDIRECT_URI") or (PUBLIC_BASE_URL + "/youtube/oauth/callback" if PUBLIC_BASE_URL else "")).strip()
DAILY_AR_TIME = (os.getenv("DAILY_AR_TIME") or "12:00").strip()
DAILY_EN_TIME = (os.getenv("DAILY_EN_TIME") or "14:00").strip()
DAILY_TOPIC = (os.getenv("DAILY_TOPIC") or "A gentle, original sea adventure that teaches kindness, teamwork, or caring for nature").strip()
BACKGROUND_MUSIC_PATH = (os.getenv("BACKGROUND_MUSIC_PATH") or "").strip()

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly", "https://www.googleapis.com/auth/yt-analytics.readonly"]
MODEL_PREFERENCES = ("gpt-5.6", "gpt-5.6-sol", "claude-sonnet-4-6", "gemini-3.8-flash", "deepseek-v4-flash", "qwen3.7-max", "kimi-k2.8")
client = AsyncOpenAI(api_key=FASTLLM_API_KEY, base_url=FASTLLM_BASE_URL) if FASTLLM_API_KEY else None
telegram_app: Optional[Application] = None
scheduler_task: Optional[asyncio.Task] = None


def utc_now():
    return datetime.now(timezone.utc)


def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS episodes (
            id TEXT PRIMARY KEY, episode_date TEXT NOT NULL, language TEXT NOT NULL,
            topic TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
            script TEXT NOT NULL, scenes TEXT NOT NULL, status TEXT NOT NULL,
            video_path TEXT, youtube_video_id TEXT, youtube_url TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, model TEXT,
            render_progress INTEGER NOT NULL DEFAULT 0, render_error TEXT,
            analytics TEXT, UNIQUE(episode_date, language)
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS oauth_states (
            state TEXT PRIMARY KEY, created_at TEXT NOT NULL, code_verifier TEXT
        )""")
        if "code_verifier" not in [r[1] for r in con.execute("PRAGMA table_info(oauth_states)")]:
            con.execute("ALTER TABLE oauth_states ADD COLUMN code_verifier TEXT")
        con.execute("CREATE TABLE IF NOT EXISTS daily_runs (run_date TEXT NOT NULL, language TEXT NOT NULL, status TEXT NOT NULL, error TEXT, PRIMARY KEY(run_date,language))")


init_db()


def admin_only(update: Update) -> bool:
    user = update.effective_user
    return bool(user and ADMIN_TELEGRAM_ID and str(user.id) == ADMIN_TELEGRAM_ID)


def check_admin_id(value: Optional[str]):
    if not APP_API_KEY:
        raise HTTPException(503, "Set APP_API_KEY in Railway before using web admin endpoints.")
    if value != APP_API_KEY:
        raise HTTPException(401, "Unauthorized")


def google_client_config():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET or not GOOGLE_REDIRECT_URI:
        raise RuntimeError("Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and PUBLIC_BASE_URL in Railway.")
    return {"web": {"client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET, "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": [GOOGLE_REDIRECT_URI]}}


def load_google_credentials():
    if not TOKEN_PATH.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), YOUTUBE_SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        return creds if creds.valid else None
    except Exception:
        return None


def youtube_services():
    credentials = load_google_credentials()
    if not credentials:
        raise RuntimeError("YouTube is not connected. Use /connect in the authorized Telegram chat.")
    return build("youtube", "v3", credentials=credentials, cache_discovery=False), build("youtubeAnalytics", "v2", credentials=credentials, cache_discovery=False)


async def select_model():
    if not client:
        raise RuntimeError("FASTLLM_API_KEY is not configured.")
    if FASTLLM_MODEL.lower() not in ("auto", "all"):
        return FASTLLM_MODEL
    response = await client.models.list()
    model_ids = sorted({item.id for item in response.data})
    excluded = ("image", "video", "audio", "embedding", "rerank", "moderation", "seedance", "wan3", "hailuo")
    grok_models = [
        item for item in model_ids
        if "grok" in item.lower() and not any(marker in item.lower() for marker in excluded)
    ]
    if grok_models:
        return grok_models[0]
    for preferred in MODEL_PREFERENCES:
        if preferred in model_ids:
            return preferred
    for item in model_ids:
        if not any(marker in item.lower() for marker in excluded):
            return item
    raise RuntimeError("No chat/text model was returned by the provider.")


def parse_json(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise RuntimeError("AI did not return valid JSON.")
        return json.loads(match.group(0))


async def write_story(topic: str, language: str, model: str):
    language_name = "Arabic" if language == "ar" else "English"
    prompt = f"Write an original gentle sea-adventure cartoon episode for children ages 4-8 in {language_name}. Topic: {topic}. About 90 seconds of spoken narration. Use original characters and avoid copying copyrighted Sinbad adaptations or other franchises. No frightening scenes, violence, or unsafe instructions. Keep character names and events aligned between language versions. Return JSON with title, description, moral, and scenes: 5-7 objects, each having visual_description, narration, sound_effect. Narration must be in {language_name}, with a beginning, middle, happy ending, and positive lesson."
    try:
        response = await client.chat.completions.create(model=model, messages=[{"role": "system", "content": "You are a children's story creator. Write original, age-appropriate stories. Never imitate a copyrighted franchise. Return valid JSON."}, {"role": "user", "content": prompt}], response_format={"type": "json_object"})
        return parse_json(response.choices[0].message.content or "{}")
    except Exception as exc:
        if "response_format" not in str(exc).lower() and "json_object" not in str(exc).lower():
            raise
        response = await client.chat.completions.create(model=model, messages=[{"role": "system", "content": "Return valid JSON only. Create original safe children's stories."}, {"role": "user", "content": prompt}])
        return parse_json(response.choices[0].message.content or "{}")


def font(size):
    for name in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_frame(scene, path, index, total, language):
    width, height = 720, 1280
    image = Image.new("RGB", (width, height), (14, 35, 66))
    draw = ImageDraw.Draw(image)
    palette = [(14, 35, 66), (23, 72, 92), (58, 52, 86), (18, 74, 73), (78, 64, 37)]
    base = palette[index % len(palette)]
    for y in range(height):
        ratio = y / height
        draw.line((0, y, width, y), fill=tuple(int(base[k] * (1-ratio) + (7, 15, 33)[k] * ratio) for k in range(3)))
    for x, y, radius in ((70, 130, 5), (620, 200, 7), (150, 300, 4), (590, 860, 5), (100, 1050, 6)):
        draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=(200, 222, 243))
    draw.rounded_rectangle((34, 280, width-34, 1000), radius=34, fill=(8, 17, 38), outline=(126, 108, 255), width=3)
    draw.text((width//2, 345), "Kids Haven", font=font(34), fill=(207, 198, 255), anchor="mm")
    narration = scene.get("narration", "")
    lines, line = [], ""
    for word in narration.split():
        candidate = f"{line} {word}".strip()
        if draw.textbbox((0, 0), candidate, font=font(32))[2] > width-110 and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    y = 470
    for part in lines[:9]:
        try:
            draw.text((width//2, y), part, font=font(32), fill="white", anchor="mm", direction="rtl" if language == "ar" else "ltr")
        except (TypeError, ValueError):
            draw.text((width//2, y), part, font=font(32), fill="white", anchor="mm")
        y += 54
    draw.text((width//2, 940), f"{index+1} / {total}", font=font(24), fill=(205, 218, 237), anchor="mm")
    image.save(path)


async def render_video(episode_id: str):
    with db() as con:
        row = con.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not row:
        raise RuntimeError("Episode not found")
    episode = dict(row)
    scenes = json.loads(episode["scenes"])
    if not subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode == 0:
        raise RuntimeError("FFmpeg is missing")
    job_dir = WORK_DIR / episode_id
    job_dir.mkdir(parents=True, exist_ok=True)
    voice = "ar-SA-HamedNeural" if episode["language"] == "ar" else "en-US-GuyNeural"
    clips = []
    music = Path(BACKGROUND_MUSIC_PATH) if BACKGROUND_MUSIC_PATH else None
    if music and not music.is_file():
        music = None
    for index, scene in enumerate(scenes):
        frame = job_dir / f"scene-{index}.png"
        audio = job_dir / f"voice-{index}.mp3"
        clip = job_dir / f"clip-{index}.mp4"
        make_frame(scene, frame, index, len(scenes), episode["language"])
        await edge_tts.Communicate(scene["narration"], voice).save(str(audio))
        command = ["ffmpeg", "-y", "-loop", "1", "-framerate", "24", "-i", str(frame), "-i", str(audio)]
        if music:
            command += ["-stream_loop", "-1", "-i", str(music), "-filter_complex", "[2:a]volume=0.10[bg];[1:a][bg]amix=inputs=2:duration=first:dropout_transition=2[aout]", "-map", "0:v", "-map", "[aout]"]
        command += ["-shortest", "-vf", "scale=720:1280,format=yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(clip)]
        result = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=240)
        if result.returncode:
            raise RuntimeError(f"FFmpeg failed: {result.stderr[-700:]}")
        clips.append(clip)
        with db() as con:
            con.execute("UPDATE episodes SET render_progress=?,updated_at=? WHERE id=?", (int((index+1)*90/len(scenes)), utc_now().isoformat(), episode_id))
    concat = job_dir / "concat.txt"
    concat.write_text("".join(f"file '{clip.as_posix()}'\n" for clip in clips), encoding="utf-8")
    output = MEDIA_DIR / f"{episode_id}.mp4"
    result = await asyncio.to_thread(subprocess.run, ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", "-movflags", "+faststart", str(output)], capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise RuntimeError(f"Video assembly failed: {result.stderr[-700:]}")
    with db() as con:
        con.execute("UPDATE episodes SET video_path=?,status='ready_for_review',render_progress=100,updated_at=? WHERE id=?", (str(output), utc_now().isoformat(), episode_id))
    return output


def telegram_keyboard(episode_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve & publish to YouTube", callback_data=f"publish:{episode_id}"), InlineKeyboardButton("🗑 Reject", callback_data=f"reject:{episode_id}")]])


async def create_daily_episode(language: str, run_date: date):
    run_key = run_date.isoformat()
    with db() as con:
        existing = con.execute("SELECT id FROM episodes WHERE episode_date=? AND language=?", (run_key, language)).fetchone()
        if existing:
            return dict(con.execute("SELECT * FROM episodes WHERE id=?", (existing["id"],)).fetchone())
        con.execute("INSERT OR REPLACE INTO daily_runs(run_date,language,status,error) VALUES(?,?,?,NULL)", (run_key, language, "generating"))
    try:
        model = await select_model()
        story = await write_story(DAILY_TOPIC, language, model)
        raw_scenes = story.get("scenes")
        if not isinstance(raw_scenes, list):
            raise RuntimeError("The AI did not return scenes")
        scenes = [{"visual_description": str(scene.get("visual_description", "A friendly sea adventure")), "narration": str(scene.get("narration", "")).strip(), "sound_effect": str(scene.get("sound_effect", "gentle ocean waves"))} for scene in raw_scenes if isinstance(scene, dict) and str(scene.get("narration", "")).strip()][:8]
        if not scenes:
            raise RuntimeError("The AI returned empty narration")
        episode_id = str(uuid.uuid4())
        timestamp = utc_now().isoformat()
        with db() as con:
            con.execute("INSERT INTO episodes(id,episode_date,language,topic,title,description,script,scenes,status,created_at,updated_at,model) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (episode_id, run_key, language, DAILY_TOPIC, str(story.get("title") or DAILY_TOPIC)[:180], str(story.get("description") or "An original Kids Haven adventure")[:3000], "\n\n".join(scene["narration"] for scene in scenes), json.dumps(scenes, ensure_ascii=False), "rendering", timestamp, timestamp, model))
            con.execute("UPDATE daily_runs SET status='rendering' WHERE run_date=? AND language=?", (run_key, language))
        await render_video(episode_id)
        with db() as con:
            con.execute("UPDATE episodes SET status='pending_review' WHERE id=?", (episode_id,))
            con.execute("UPDATE daily_runs SET status='pending_review' WHERE run_date=? AND language=?", (run_key, language))
        if telegram_app and ADMIN_TELEGRAM_ID:
            await send_episode_for_review(episode_id)
        return dict(con.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone())
    except Exception as exc:
        with db() as con:
            con.execute("UPDATE daily_runs SET status='failed',error=? WHERE run_date=? AND language=?", (str(exc)[:1000], run_key, language))
        raise


async def send_episode_for_review(episode_id: str):
    if not telegram_app or not ADMIN_TELEGRAM_ID:
        return
    with db() as con:
        row = con.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not row or not row["video_path"]:
        return
    caption = f"مسودة جديدة للمراجعة\n{row['title']}\nاللغة: {'العربية' if row['language']=='ar' else 'English'}\n\nلن تُنشر إلا بعد ضغط زر الموافقة."
    try:
        with open(row["video_path"], "rb") as video:
            await telegram_app.bot.send_video(chat_id=int(ADMIN_TELEGRAM_ID), video=video, caption=caption[:1000], supports_streaming=True, reply_markup=telegram_keyboard(episode_id))
    except Exception as exc:
        await telegram_app.bot.send_message(chat_id=int(ADMIN_TELEGRAM_ID), text=f"جهزت الحلقة لكن تعذر إرسال الفيديو إلى Telegram: {str(exc)[:500]}\nمعرّف الحلقة: {episode_id}", reply_markup=telegram_keyboard(episode_id))


async def publish_episode(episode_id: str):
    youtube, _ = youtube_services()
    with db() as con:
        row = con.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not row or not row["video_path"] or not Path(row["video_path"]).is_file():
        raise RuntimeError("Episode video file is missing")
    snippet = {"title": row["title"][:100], "description": (row["description"] + "\n\n#KidsHaven #KidsStories").strip()[:5000], "tags": ["Kids Haven", "children stories", "sea adventure", "kids cartoon"], "categoryId": "1"}
    body = {"snippet": snippet, "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": True}}
    media = MediaFileUpload(row["video_path"], mimetype="video/mp4", resumable=True, chunksize=8 * 1024 * 1024)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    result = await asyncio.to_thread(request.execute)
    video_id = result["id"]
    link = f"https://www.youtube.com/watch?v={video_id}"
    with db() as con:
        con.execute("UPDATE episodes SET status='published',youtube_video_id=?,youtube_url=?,updated_at=? WHERE id=?", (video_id, link, utc_now().isoformat(), episode_id))
        con.execute("UPDATE daily_runs SET status='published' WHERE run_date=? AND language=?", (row["episode_date"], row["language"]))
    return link


async def tg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text("غير مصرح. هذا البوت خاص بمدير القناة.")
        return
    await update.message.reply_text("أهلًا بك في Kids Haven Studio.\n/connect — ربط قناة YouTube\n/today — إنشاء النسختين الآن\n/status — حالة الخدمة\n/stats — إحصاءات YouTube\n\nسيتم إنشاء فيديو عربي وآخر إنجليزي يوميًا. كل فيديو يحتاج موافقتك قبل النشر العام.")


async def tg_connect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text("غير مصرح.")
        return
    try:
        config = google_client_config()
        state = uuid.uuid4().hex + uuid.uuid4().hex
        flow = Flow.from_client_config(config, scopes=YOUTUBE_SCOPES, state=state, redirect_uri=GOOGLE_REDIRECT_URI)
        authorization_url, _ = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
        with db() as con:
            con.execute("INSERT INTO oauth_states(state,created_at,code_verifier) VALUES(?,?,?)", (state, utc_now().isoformat(), flow.code_verifier))
        await update.message.reply_text("افتح رابط Google التالي وسجّل الدخول بالحساب المالك للقناة. وافق على صلاحيات YouTube للرفع والتحليلات. لا ترسل رمز الدخول لأحد:\n" + authorization_url)
    except Exception as exc:
        await update.message.reply_text(f"تعذر بدء الربط: {exc}")


async def tg_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text("غير مصرح.")
        return
    await update.message.reply_text("بدأ إنشاء حلقتين. سأرسل كل فيديو هنا للمراجعة قبل النشر.")
    today = utc_now().date()
    for language in ("ar", "en"):
        try:
            row = await create_daily_episode(language, today)
            if row.get("status") in ("pending_review", "ready_for_review"):
                await send_episode_for_review(row["id"])
        except Exception as exc:
            await update.message.reply_text(f"فشل إنشاء {'العربية' if language=='ar' else 'English'}: {str(exc)[:700]}")


async def tg_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text("غير مصرح.")
        return
    with db() as con:
        rows = con.execute("SELECT episode_date,language,status,title,youtube_url FROM episodes ORDER BY created_at DESC LIMIT 10").fetchall()
    lines = [f"{'🇸🇦' if r['language']=='ar' else '🇬🇧'} {r['episode_date']} · {r['status']} · {r['title']}" + (f"\n{r['youtube_url']}" if r['youtube_url'] else "") for r in rows]
    await update.message.reply_text("\n".join(lines) if lines else "لا توجد حلقات بعد.")


async def tg_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text("غير مصرح.")
        return
    try:
        _, analytics = youtube_services()
        end = utc_now().date() - timedelta(days=1)
        start = end - timedelta(days=27)
        result = await asyncio.to_thread(lambda: analytics.reports().query(ids="channel==MINE", startDate=start.isoformat(), endDate=end.isoformat(), metrics="views,likes,comments,estimatedMinutesWatched", dimensions="video", sort="-views", maxResults=10).execute())
        entries = result.get("rows", [])
        if not entries:
            await update.message.reply_text("لا توجد بيانات متاحة للفترة الماضية حتى الآن.")
            return
        lines = ["أفضل الفيديوهات خلال آخر 28 يومًا:"]
        for item in entries:
            vid, views, likes, comments, minutes = item
            lines.append(f"https://youtu.be/{vid}\nمشاهدات {views} · إعجابات {likes} · تعليقات {comments} · وقت مشاهدة {minutes} دقيقة")
        await update.message.reply_text("\n\n".join(lines)[:4000])
    except Exception as exc:
        await update.message.reply_text(f"تعذر جلب Analytics: {exc}\nتحقق من ربط YouTube وAnalytics API.")


async def tg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if not admin_only(update):
        await query.answer("غير مصرح", show_alert=True)
        return
    await query.answer()
    action, episode_id = query.data.split(":", 1)
    if action == "reject":
        with db() as con:
            con.execute("UPDATE episodes SET status='rejected',updated_at=? WHERE id=? AND status IN ('pending_review','ready_for_review')", (utc_now().isoformat(), episode_id))
        await query.edit_message_caption(caption=(query.message.caption or "") + "\n\nتم رفض المسودة.", reply_markup=None)
        return
    if action == "publish":
        with db() as con:
            row = con.execute("SELECT status FROM episodes WHERE id=?", (episode_id,)).fetchone()
        if not row or row["status"] not in ("pending_review", "ready_for_review"):
            await query.message.reply_text("هذه الحلقة ليست بانتظار الموافقة أو سبق التعامل معها.")
            return
        await query.edit_message_caption(caption=(query.message.caption or "") + "\n\nتمت الموافقة، جارٍ الرفع إلى YouTube…", reply_markup=None)
        try:
            link = await publish_episode(episode_id)
            await query.message.reply_text("تم النشر على قناتك بعد موافقتك:\n" + link)
        except Exception as exc:
            await query.message.reply_text(f"فشل النشر: {str(exc)[:900]}")


async def scheduler_loop():
    while True:
        try:
            now = utc_now()
            for language, time_text in (("ar", DAILY_AR_TIME), ("en", DAILY_EN_TIME)):
                try:
                    hour, minute = map(int, time_text.split(":"))
                except ValueError:
                    continue
                if now.hour == hour and now.minute == minute:
                    with db() as con:
                        done = con.execute("SELECT status FROM daily_runs WHERE run_date=? AND language=?", (now.date().isoformat(), language)).fetchone()
                    if not done or done["status"] == "failed":
                        try:
                            row = await create_daily_episode(language, now.date())
                            if row.get("status") in ("pending_review", "ready_for_review"):
                                await send_episode_for_review(row["id"])
                        except Exception as exc:
                            if telegram_app and ADMIN_TELEGRAM_ID:
                                await telegram_app.bot.send_message(chat_id=int(ADMIN_TELEGRAM_ID), text=f"فشل الإنتاج الآلي ({language}): {str(exc)[:700]}")
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app, scheduler_task
    if TELEGRAM_BOT_TOKEN:
        telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        telegram_app.add_handler(CommandHandler("start", tg_start))
        telegram_app.add_handler(CommandHandler("connect", tg_connect))
        telegram_app.add_handler(CommandHandler("today", tg_today))
        telegram_app.add_handler(CommandHandler("status", tg_status))
        telegram_app.add_handler(CommandHandler("stats", tg_stats))
        telegram_app.add_handler(CallbackQueryHandler(tg_callback))
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        scheduler_task = asyncio.create_task(scheduler_loop())
    yield
    if scheduler_task:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
    if telegram_app:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()


app = FastAPI(title="Kids Haven Telegram & YouTube Studio", version="2.0.0", lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def home():
    return HTMLResponse("<html lang='ar' dir='rtl'><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>Kids Haven Studio</title><body style='font:18px system-ui;max-width:720px;margin:50px auto;padding:20px'><h1>🌊 Kids Haven Studio</h1><p>بوت Telegram لإنشاء ومراجعة فيديوهات عربية وإنجليزية، مع ربط YouTube رسميًا.</p><p>اضبط أسرار Railway ثم ابدأ البوت بأمر /start. لا يتم نشر الفيديو تلقائيًا: يجب ضغط موافقة النشر في Telegram.</p><p><a href='/docs'>توثيق API</a> · <a href='/health'>حالة الخدمة</a></p></body></html>")


@app.get("/health")
async def health():
    return {"status": "healthy", "fastllm_configured": bool(client), "telegram_configured": bool(TELEGRAM_BOT_TOKEN), "telegram_admin_configured": bool(ADMIN_TELEGRAM_ID), "youtube_connected": bool(load_google_credentials()), "model_mode": "auto" if FASTLLM_MODEL.lower() in ("auto", "all") else "fixed"}


@app.get("/youtube/oauth/callback")
async def youtube_oauth_callback(request: Request, state: str, code: str):
    with db() as con:
        saved = con.execute("SELECT created_at, code_verifier FROM oauth_states WHERE state=?", (state,)).fetchone()
        if saved:
            con.execute("DELETE FROM oauth_states WHERE state=?", (state,))
    if not saved:
        raise HTTPException(400, "Invalid or already-used OAuth state. Start again with Telegram /connect.")
    created = datetime.fromisoformat(saved["created_at"])
    if utc_now() - created > timedelta(minutes=15):
        raise HTTPException(400, "OAuth state expired. Start again with Telegram /connect.")
    try:
        flow = Flow.from_client_config(google_client_config(), scopes=YOUTUBE_SCOPES, state=state, redirect_uri=GOOGLE_REDIRECT_URI)
        flow.fetch_token(code=code, code_verifier=saved["code_verifier"])
        TOKEN_PATH.write_text(flow.credentials.to_json(), encoding="utf-8")
        TOKEN_PATH.chmod(0o600)
    except Exception as exc:
        raise HTTPException(400, f"Google authorization failed: {exc}")
    if telegram_app and ADMIN_TELEGRAM_ID:
        await telegram_app.bot.send_message(chat_id=int(ADMIN_TELEGRAM_ID), text="تم ربط حساب Google/YouTube بنجاح. يمكنك الآن استخدام /today أو انتظار الجدول اليومي.")
    return HTMLResponse("<meta charset='utf-8'><h2>تم ربط YouTube بنجاح. ارجع إلى Telegram.</h2>")


@app.get("/studio/stats")
async def web_stats(x_api_key: Optional[str] = Header(default=None)):
    check_admin_id(x_api_key)
    try:
        _, analytics = youtube_services()
        end = utc_now().date() - timedelta(days=1)
        start = end - timedelta(days=27)
        report = await asyncio.to_thread(lambda: analytics.reports().query(ids="channel==MINE", startDate=start.isoformat(), endDate=end.isoformat(), metrics="views,likes,comments,estimatedMinutesWatched", dimensions="video", sort="-views", maxResults=20).execute())
        return {"start_date": start.isoformat(), "end_date": end.isoformat(), "headers": report.get("columnHeaders", []), "rows": report.get("rows", [])}
    except Exception as exc:
        raise HTTPException(502, f"YouTube Analytics unavailable: {exc}")


@app.get("/studio/episodes")
async def web_episodes(x_api_key: Optional[str] = Header(default=None)):
    check_admin_id(x_api_key)
    with db() as con:
        rows = con.execute("SELECT id,episode_date,language,title,status,youtube_url,render_progress,render_error,created_at FROM episodes ORDER BY created_at DESC LIMIT 100").fetchall()
    return {"episodes": [dict(row) for row in rows]}
