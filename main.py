import asyncio
import base64
import binascii
import io
import math
import json
import os
import re
import secrets
import sqlite3
import struct
import subprocess
import tempfile
import uuid
import wave
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import arabic_reshaper
import edge_tts
import httpx
import song_studio
from pipeline import animation, images as scene_images, video as scene_video
from pipeline.characters import DEFAULT_LIBRARY, build_image_prompt, get_seed, load_characters
from bidi.algorithm import get_display
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from openai import AsyncOpenAI
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat, UnidentifiedImageError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()
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
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
ADMIN_TELEGRAM_ID = (os.getenv("ADMIN_TELEGRAM_ID") or "").strip()
ADMIN_IDS = tuple(part.strip() for part in (os.getenv("ADMIN_IDS") or "").split(",") if part.strip())
if any(not value.isdigit() for value in ADMIN_IDS):
    raise ValueError("ADMIN_IDS must be comma-separated numeric Telegram user IDs.")
if not ADMIN_TELEGRAM_ID and ADMIN_IDS:
    ADMIN_TELEGRAM_ID = ADMIN_IDS[0]
CHANNEL_ID = (os.getenv("CHANNEL_ID") or "").strip()
MAX_UPLOADS_PER_DAY = int(os.getenv("MAX_UPLOADS_PER_DAY") or "1")
if MAX_UPLOADS_PER_DAY < 1:
    raise ValueError("MAX_UPLOADS_PER_DAY must be positive.")
APP_API_KEY = (os.getenv("APP_API_KEY") or "").strip()
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
GOOGLE_CLIENT_ID = (os.getenv("GOOGLE_CLIENT_ID") or "").strip()
GOOGLE_CLIENT_SECRET = (os.getenv("GOOGLE_CLIENT_SECRET") or "").strip()
YOUTUBE_CLIENT_SECRET = (os.getenv("YOUTUBE_CLIENT_SECRET") or "").strip()
GOOGLE_REDIRECT_URI = (os.getenv("GOOGLE_REDIRECT_URI") or (PUBLIC_BASE_URL + "/youtube/oauth/callback" if PUBLIC_BASE_URL else "")).strip()
DAILY_AR_TIME = (os.getenv("DAILY_AR_TIME") or "12:00").strip()
DAILY_EN_TIME = (os.getenv("DAILY_EN_TIME") or "14:00").strip()
DAILY_LANGUAGES = tuple(part.strip().lower() for part in (os.getenv("DAILY_LANGUAGES") or "ar,en").split(","))
if not DAILY_LANGUAGES or any(part not in ("ar", "en") for part in DAILY_LANGUAGES) or len(set(DAILY_LANGUAGES)) != len(DAILY_LANGUAGES):
    raise ValueError("DAILY_LANGUAGES must contain ar, en, or ar,en without duplicates.")
DAILY_TOPIC = (os.getenv("DAILY_TOPIC") or "").strip()
VOICE_AR = (os.getenv("VOICE_AR") or "ar-SA-HamedNeural").strip()
VOICE_EN = (os.getenv("VOICE_EN") or "en-US-GuyNeural").strip()
VOICE_RATE = (os.getenv("VOICE_RATE") or "-5%").strip()
VOICE_PITCH = (os.getenv("VOICE_PITCH") or "+0Hz").strip()
BACKGROUND_MUSIC_PATH = (os.getenv("BACKGROUND_MUSIC_PATH") or "").strip()
IMAGE_PROVIDER = (os.getenv("IMAGE_PROVIDER") or "cloudflare").strip().lower()
CLOUDFLARE_ACCOUNT_ID = (os.getenv("CLOUDFLARE_ACCOUNT_ID") or "").strip()
CLOUDFLARE_API_TOKEN = (os.getenv("CLOUDFLARE_API_TOKEN") or "").strip()
VIDEO_PROVIDER = {"huggingface": "hf", "fal.ai": "fal", "blender": "none", "": "none"}.get((os.getenv("VIDEO_PROVIDER") or "none").strip().strip("'\"").lower(), (os.getenv("VIDEO_PROVIDER") or "none").strip().strip("'\"").lower())
FAL_KEY = (os.getenv("FAL_KEY") or "").strip()
FAL_VIDEO_MODEL = (os.getenv("FAL_VIDEO_MODEL") or "fal-ai/kling-video/v2.1/standard/image-to-video").strip().strip("/")
HF_SPACE = (os.getenv("HF_SPACE") or "Lightricks/ltx-video-distilled").strip()
HF_API_NAME = (os.getenv("HF_API_NAME") or "/image_to_video").strip()
HF_TOKEN = (os.getenv("HF_TOKEN") or "").strip()
CONTENT_MODE = (os.getenv("CONTENT_MODE") or "song").strip().strip("'\"").lower()
SONG_PROVIDER = (os.getenv("SONG_PROVIDER") or "tts").strip().strip("'\"").lower()
FAL_MUSIC_MODEL = (os.getenv("FAL_MUSIC_MODEL") or "fal-ai/ace-step").strip().strip("/")
VOICE_SONG_EN = (os.getenv("VOICE_SONG_EN") or "en-US-AnaNeural").strip()
VOICE_SONG_AR = (os.getenv("VOICE_SONG_AR") or "ar-SA-ZariyahNeural").strip()
RENDER_RETRY_AFTER = timedelta(minutes=10)
TELEGRAM_VIDEO_LIMIT = 49_000_000
TELEGRAM_VIDEO_TARGET = 42_000_000

YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly", "https://www.googleapis.com/auth/yt-analytics.readonly"]
MODEL_PREFERENCES = ("gpt-5.6", "gpt-5.6-sol", "claude-sonnet-4-6", "gemini-3.8-flash", "deepseek-v4-flash", "qwen3.7-max", "kimi-k2.8")
client = AsyncOpenAI(api_key=FASTLLM_API_KEY, base_url=FASTLLM_BASE_URL) if FASTLLM_API_KEY else None
telegram_app: Optional[Application] = None
scheduler_task: Optional[asyncio.Task] = None
production_worker_task: Optional[asyncio.Task] = None
auto_studio = None


def utc_now():
    return datetime.now(timezone.utc)


def character_library_path():
    saved = DATA_DIR / "characters.json"
    return saved if saved.is_file() else DEFAULT_LIBRARY


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
            analytics TEXT, meta TEXT, source TEXT NOT NULL DEFAULT 'daily'
        )""")
        if "meta" not in [r[1] for r in con.execute("PRAGMA table_info(episodes)")]:
            con.execute("ALTER TABLE episodes ADD COLUMN meta TEXT")
        columns = [row[1] for row in con.execute("PRAGMA table_info(episodes)")]
        table_sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='episodes'").fetchone()[0]
        if "UNIQUE(episode_date,language)" in table_sql.replace(" ", ""):
            con.execute("""CREATE TABLE episodes_migrating (
                id TEXT PRIMARY KEY, episode_date TEXT NOT NULL, language TEXT NOT NULL,
                topic TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
                script TEXT NOT NULL, scenes TEXT NOT NULL, status TEXT NOT NULL,
                video_path TEXT, youtube_video_id TEXT, youtube_url TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, model TEXT,
                render_progress INTEGER NOT NULL DEFAULT 0, render_error TEXT,
                analytics TEXT, meta TEXT, source TEXT NOT NULL DEFAULT 'daily'
            )""")
            source = "source" if "source" in columns else "'daily'"
            con.execute(
                f"INSERT INTO episodes_migrating SELECT id,episode_date,language,topic,title,description,script,scenes,status,video_path,youtube_video_id,youtube_url,created_at,updated_at,model,render_progress,render_error,analytics,meta,{source} FROM episodes"
            )
            con.execute("DROP TABLE episodes")
            con.execute("ALTER TABLE episodes_migrating RENAME TO episodes")
        elif "source" not in columns:
            con.execute("ALTER TABLE episodes ADD COLUMN source TEXT NOT NULL DEFAULT 'daily'")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_episode_date_language ON episodes(episode_date,language) WHERE source='daily'")
        con.execute("""CREATE TABLE IF NOT EXISTS production_jobs (
            id TEXT PRIMARY KEY, episode_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT, cost_usd REAL, settings_json TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        job_columns = [row[1] for row in con.execute("PRAGMA table_info(production_jobs)")]
        if "cost_usd" not in job_columns:
            con.execute("ALTER TABLE production_jobs ADD COLUMN cost_usd REAL")
        if "settings_json" not in job_columns:
            con.execute("ALTER TABLE production_jobs ADD COLUMN settings_json TEXT")
        con.execute("CREATE TABLE IF NOT EXISTS studio_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")
        con.execute("""CREATE TABLE IF NOT EXISTS oauth_states (
            state TEXT PRIMARY KEY, created_at TEXT NOT NULL, code_verifier TEXT
        )""")
        if "code_verifier" not in [r[1] for r in con.execute("PRAGMA table_info(oauth_states)")]:
            con.execute("ALTER TABLE oauth_states ADD COLUMN code_verifier TEXT")
        con.execute("CREATE TABLE IF NOT EXISTS daily_runs (run_date TEXT NOT NULL, language TEXT NOT NULL, status TEXT NOT NULL, error TEXT, PRIMARY KEY(run_date,language))")


init_db()

def load_studio_settings():
    global IMAGE_PROVIDER, VIDEO_PROVIDER, SONG_PROVIDER, CONTENT_MODE
    with db() as con:
        settings = {row["key"]: row["value"] for row in con.execute("SELECT key,value FROM studio_settings")}
    IMAGE_PROVIDER = settings.get("image_provider", IMAGE_PROVIDER)
    if IMAGE_PROVIDER == "cards":
        IMAGE_PROVIDER = "cloudflare"
    VIDEO_PROVIDER = settings.get("video_provider", VIDEO_PROVIDER)
    if VIDEO_PROVIDER == "blender":
        VIDEO_PROVIDER = "none"
    SONG_PROVIDER = settings.get("song_provider", SONG_PROVIDER)
    CONTENT_MODE = settings.get("content_mode", CONTENT_MODE)


load_studio_settings()


def validate_studio_settings(values):
    if values["image_provider"] != "cloudflare":
        raise ValueError("صور المشاهد يجب أن تُولّد بالذكاء الاصطناعي عبر Cloudflare أو Pollinations.")
    if values["video_provider"] == "blender" or values["video_provider"] not in animation.VIDEO_PROVIDERS:
        raise ValueError("مزود الفيديو غير مدعوم.")
    if values["song_provider"] not in ("tts", "fal"):
        raise ValueError("مزود الموسيقى يجب أن يكون tts أو fal.")
    if values["content_mode"] not in ("story", "song"):
        raise ValueError("نوع المحتوى يجب أن يكون story أو song.")
    if values["video_provider"] != "none" and values["image_provider"] != "cloudflare":
        raise ValueError("تحريك المشاهد يحتاج IMAGE_PROVIDER=cloudflare.")
    if values["video_provider"] in animation.FAL_PROVIDERS and not FAL_KEY:
        raise ValueError("خدمات Kling وHailuo وPixVerse وVidu وDreamina وVeo عبر fal.ai تحتاج FAL_KEY في Railway.")
    if values["video_provider"] == "runway" and not (os.getenv("RUNWAY_API_KEY") or "").strip():
        raise ValueError("Runway يحتاج RUNWAY_API_KEY في Railway.")
    if values["song_provider"] == "fal" and not FAL_KEY:
        raise ValueError("مزود fal للموسيقى يحتاج FAL_KEY في Railway.")


def set_studio_settings(changes):
    global IMAGE_PROVIDER, VIDEO_PROVIDER, SONG_PROVIDER, CONTENT_MODE
    if generation_lock.locked():
        raise ValueError("لا تغيّر مزود الإنتاج أثناء عمل مهمة؛ جرّب بعد انتهائها أو إيقافها مؤقتاً.")
    values = {
        "image_provider": IMAGE_PROVIDER,
        "video_provider": VIDEO_PROVIDER,
        "song_provider": SONG_PROVIDER,
        "content_mode": CONTENT_MODE,
    }
    if any(key not in values for key in changes):
        raise ValueError("إعداد غير معروف.")
    values.update(changes)
    validate_studio_settings(values)
    timestamp = utc_now().isoformat()
    with db() as con:
        con.executemany(
            "INSERT INTO studio_settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            [(key, value, timestamp) for key, value in changes.items()],
        )
    IMAGE_PROVIDER = values["image_provider"]
    VIDEO_PROVIDER = values["video_provider"]
    SONG_PROVIDER = values["song_provider"]
    CONTENT_MODE = values["content_mode"]
    return values


def admin_only(update: Update) -> bool:
    user = update.effective_user
    return bool(user and str(user.id) in set(ADMIN_IDS + ((ADMIN_TELEGRAM_ID,) if ADMIN_TELEGRAM_ID else ())))


def check_admin_id(value: Optional[str]):
    if not APP_API_KEY:
        raise HTTPException(503, "Set APP_API_KEY in Railway before using web admin endpoints.")
    if not value or not secrets.compare_digest(value, APP_API_KEY):
        raise HTTPException(401, "Unauthorized")


def google_client_config():
    if YOUTUBE_CLIENT_SECRET:
        try:
            config = json.loads(Path(YOUTUBE_CLIENT_SECRET).read_text(encoding="utf-8"))
            web = config["web"]
            if not web["client_id"] or not web["client_secret"] or not GOOGLE_REDIRECT_URI:
                raise ValueError("Missing web OAuth client credentials or redirect URI.")
            web["redirect_uris"] = [GOOGLE_REDIRECT_URI]
            return {"web": web}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("YOUTUBE_CLIENT_SECRET must point to a valid Google web OAuth client JSON file.") from exc
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


STORY_WORLDS = (
    "a gentle sea adventure with sea animals, teaching kindness",
    "a cozy forest adventure with woodland animals, teaching sharing",
    "a friendly space trip to colorful planets, teaching curiosity and teamwork",
    "a sunny farm day with farm animals, teaching helping others",
    "a snowy mountain adventure with arctic animals, teaching patience",
    "a desert caravan journey with a baby camel, teaching courage and honesty",
    "a magical garden where flowers and insects live, teaching caring for nature",
    "a small town bakery with animal friends, teaching gratitude",
    "a jungle river trip with playful monkeys and birds, teaching listening",
    "a dinosaur valley with gentle baby dinosaurs, teaching friendship",
    "a bedtime journey through a dreamy night sky, teaching calm and love",
    "a school-day adventure with animal classmates, teaching saying sorry and forgiving",
)


def pick_topic(run_date: date) -> str:
    if DAILY_TOPIC:
        return DAILY_TOPIC
    return STORY_WORLDS[run_date.toordinal() % len(STORY_WORLDS)]


def recent_titles(limit=15):
    with db() as con:
        return [r["title"] for r in con.execute("SELECT title FROM episodes ORDER BY created_at DESC LIMIT ?", (limit,))]


async def write_story(
    topic: str, language: str, model: str, avoid_titles=(),
    min_words=300, max_words=360, scene_count=6, max_scenes=8,
    content_type="story",
):
    language_name = "Arabic" if language == "ar" else "English"
    type_guidance = {
        "story": "Tell an imaginative story with a clear beginning, middle, happy ending, and positive lesson.",
        "educational": "Teach one age-appropriate fact or skill through a story, and make the lesson clear without lecturing.",
        "adventure": "Tell a gentle, exciting adventure with a child-safe challenge and a happy resolution.",
    }.get(content_type, "Tell an imaginative story with a clear beginning, middle, happy ending, and positive lesson.")
    if not 1 <= scene_count <= 8 or not 1 <= min_words <= max_words:
        raise ValueError("Story length or scene count is outside supported limits.")
    duration_text = "about 45-60 seconds" if max_words <= 160 else "about 2-3 minutes"
    avoid = f" Do not reuse the plots or titles of these earlier episodes: {'; '.join(avoid_titles)}." if avoid_titles else ""
    character_bible = load_characters(character_library_path())
    bible_choices = [
        {
            "id": item["id"], "name": item["name"], "gender": item["gender"],
            "species": item["species"], "fixed_visual_description": item["fixed_visual_description"],
        }
        for item in character_bible
    ]
    prompt = (
        f"Write an original gentle cartoon episode for children ages 4-8 in {language_name}. Theme: {topic}.{avoid} "
        f"The combined spoken narration must contain {min_words}-{max_words} words so the finished narrated video lasts {duration_text}. "
        "Use original characters and never copy copyrighted franchises. No frightening scenes, violence, or unsafe instructions. "
        "Keep character names and events aligned between language versions. Use only characters from "
        "this character bible; appearance, clothing, colors and gender are fixed. Include the exact "
        "character name in each scene narration, with grammatically correct gendered wording. "
        f"Character bible: {json.dumps(bible_choices, ensure_ascii=False)}. "
        "Return JSON with: title, description, moral, art_style (one English sentence describing a single consistent visual style), "
        f"and scenes: exactly {scene_count} objects, each having character_ids (array of character-bible IDs), "
        "location (specific unique place), camera_angle (unique framing/angle), visual_description "
        "(action only, no appearance), motion (one English sentence describing movement), narration, sound_effect. "
        "Every scene must use a different location and a different camera_angle. "
        f"Write approximately {max(1, (min_words + max_words) // (2 * scene_count))} spoken words in each scene's narration; do not count other fields. "
        "Write each visual_description in English as an action-only description; the image prompt adds "
        "the exact character appearance from the bible, plus location and camera angle. "
        f"Narration must be in {language_name}. {type_guidance}"
    )
    system = "You are a children's story creator. Write original, age-appropriate stories. Never imitate a copyrighted franchise. Return valid JSON."
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    counts = []
    for attempt in range(3):
        try:
            response = await client.chat.completions.create(model=model, messages=messages, response_format={"type": "json_object"})
        except Exception as exc:
            if "response_format" not in str(exc).lower() and "json_object" not in str(exc).lower():
                raise
            response = await client.chat.completions.create(model=model, messages=messages)
        content = response.choices[0].message.content or "{}"
        story = parse_json(content)
        scenes = story.get("scenes")
        usable_scenes = [
            scene for scene in scenes
            if isinstance(scene, dict) and str(scene.get("narration", "")).strip()
        ][:max_scenes] if isinstance(scenes, list) else []
        narration = " ".join(str(scene.get("narration", "")) for scene in usable_scenes)
        word_count = len(re.findall(r"\S+", narration))
        counts.append(word_count)
        validation_error = validate_story_scenes(usable_scenes, character_bible, scene_count)
        if min_words <= word_count <= max_words and not validation_error:
            return story
        adjustment = "Expand" if word_count < min_words else "Shorten"
        if validation_error:
            adjustment = validation_error
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": content},
            {"role": "user", "content": (
                f"The usable scene narration contained {word_count} words. Correction required: "
                f"{adjustment}. Ensure the narration is {min_words}-{max_words} words total, aiming for about "
                f"{max(1, (min_words + max_words) // (2 * scene_count))} words in each of {scene_count} scenes. "
                "Keep the same characters and story, and return the complete corrected JSON."
            )},
        ]
    raise RuntimeError(
        f"AI could not create a story within {min_words}-{max_words} spoken words after 3 attempts "
        f"(actual counts: {', '.join(map(str, counts))})."
    )


def validate_story_scenes(scenes, character_bible, scene_count):
    if len(scenes) != scene_count:
        return f"Return exactly {scene_count} valid scenes."
    by_id = {item["id"]: item for item in character_bible}
    locations, angles = set(), set()
    for index, scene in enumerate(scenes, 1):
        ids = scene.get("character_ids")
        if (
            not isinstance(ids, list) or not ids
            or any(not isinstance(item, str) or item not in by_id for item in ids)
            or len(set(ids)) != len(ids)
        ):
            return f"Scene {index} must use one or more valid character-bible IDs."
        narration = str(scene.get("narration", "")).casefold()
        for character_id in ids:
            if by_id[character_id]["name"].casefold() not in narration:
                return f"Scene {index} narration must include the exact name {by_id[character_id]['name']}."
        for field, unique in (("location", locations), ("camera_angle", angles)):
            value = scene.get(field)
            if not isinstance(value, str) or not value.strip():
                return f"Scene {index} needs a {field.replace('_', ' ')}."
            normalized = " ".join(value.casefold().split())
            if normalized in unique:
                return f"Every scene needs a different {field.replace('_', ' ')}."
            unique.add(normalized)
        if not str(scene.get("visual_description", "")).strip():
            return f"Scene {index} needs an action-only visual description."
    return ""


def character_sheet(story) -> str:
    style = str(story.get("art_style") or "cute colorful 3D cartoon for toddlers").strip()
    ids = list(dict.fromkeys(
        character_id
        for scene in (story.get("scenes") or story.get("sections") or [])
        for character_id in scene.get("character_ids", [])
    ))
    library = {item["id"]: item for item in load_characters(character_library_path())}
    people = [
        f"{library[item]['name']} ({library[item]['gender']}): {library[item]['fixed_visual_description']}"
        for item in ids if item in library
    ]
    sheet = f"Art style: {style}."
    if people:
        sheet += " Recurring characters, drawn identically in every scene: " + "; ".join(people) + "."
    return sheet[:800]


def font(size, basic=False):
    for name in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size, layout_engine=ImageFont.Layout.BASIC if basic else None)
        except OSError:
            continue
    return ImageFont.load_default()


def check_image_provider():
    if VIDEO_PROVIDER not in animation.VIDEO_PROVIDERS:
        raise RuntimeError(f"Unsupported VIDEO_PROVIDER: {VIDEO_PROVIDER!r}.")
    if VIDEO_PROVIDER not in ("none",) and IMAGE_PROVIDER != "cloudflare":
        raise RuntimeError("Image-to-video providers require IMAGE_PROVIDER=cloudflare.")
    if VIDEO_PROVIDER in animation.FAL_PROVIDERS and not FAL_KEY:
        raise RuntimeError("This video provider requires FAL_KEY.")
    if VIDEO_PROVIDER == "runway" and not (os.getenv("RUNWAY_API_KEY") or "").strip():
        raise RuntimeError("VIDEO_PROVIDER=runway requires RUNWAY_API_KEY.")
    if IMAGE_PROVIDER != "cloudflare":
        raise RuntimeError("Scene images must use the Cloudflare/Pollinations AI image pipeline.")


def cloudflare_error(response):
    try:
        body = response.json()
    except ValueError:
        body = None
    errors = body.get("errors") if isinstance(body, dict) else None
    codes = []
    diagnostics = set()
    if isinstance(errors, list):
        for error in errors[:5]:
            code = error.get("code") if isinstance(error, dict) else None
            if type(code) is int and 0 <= code <= 99999999:
                codes.append(str(code))
            message = error.get("message", "") if isinstance(error, dict) else ""
            if code == 5006 and isinstance(message, str):
                message = message.lower()
                if "unevaluated propert" in message or "additional propert" in message:
                    diagnostics.add("unexpected field")
                if "required propert" in message:
                    diagnostics.add("missing required field")
                if "maxlength" in message or "max length" in message or "too long" in message:
                    diagnostics.add("input too long")
                if "must be string" in message or "must be integer" in message:
                    diagnostics.add("invalid field type")
    detail = f"; Cloudflare error code: {', '.join(codes)}" if codes else "; no Cloudflare error code returned"
    if diagnostics:
        detail += f"; validation: {', '.join(sorted(diagnostics))}"
    return f"Cloudflare image generation failed (HTTP {response.status_code}{detail})."


async def make_cloudflare_frame(scene, path, size=(720, 1280), style=""):
    description = scene.get("visual_description", "").strip()
    if not description:
        raise RuntimeError("A scene is missing its visual description for image generation.")
    ids = scene.get("character_ids")
    if not isinstance(ids, list) or not ids:
        raise RuntimeError("Every scene must identify characters from the character bible.")
    if not scene.get("location") or not scene.get("camera_angle"):
        raise RuntimeError("Every scene needs its own location and camera angle.")
    action = (
        f"Location: {scene.get('location') or 'a colorful child-friendly setting'}. "
        f"Camera angle: {scene.get('camera_angle') or 'eye-level wide shot'}. "
        f"Action: {description}"
    )
    library_path = character_library_path()
    prompt = build_image_prompt(
        action, ids, path=library_path,
        style=style or "Original colorful 3D cartoon illustration for young children, 9:16",
    )
    path = Path(path)

    def validate_generated_file():
        if not path.is_file() or path.stat().st_size < 10_000:
            raise RuntimeError("The generated image file is under 10 KB.")
        try:
            with Image.open(path) as image:
                deviation = ImageStat.Stat(image.convert("L")).stddev[0]
        except (OSError, UnidentifiedImageError) as exc:
            raise RuntimeError("The generated image is invalid.") from exc
        if deviation < 2:
            raise RuntimeError("The generated image is blank.")

    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
        await scene_images.generate_image(
            prompt, get_seed(ids[0], path=library_path), path, size=size
        )
        validate_generated_file()
        return

    last_error = None
    quota_error = False
    for attempt in range(3):
        try:
            await _cloudflare_frame(prompt, path, size)
            validate_generated_file()
            return
        except (RuntimeError, OSError, ValueError, Image.DecompressionBombError) as exc:
            last_error = exc
            path.unlink(missing_ok=True)
            quota_error = quota_error or "HTTP 429" in str(exc) or "4006" in str(exc)
            if attempt < 2:
                await asyncio.sleep(3)

    if quota_error:
        try:
            await make_hf_inference_frame(prompt[:900], path, size)
            validate_generated_file()
            return
        except (RuntimeError, OSError, ValueError, Image.DecompressionBombError) as exc:
            last_error = exc
            path.unlink(missing_ok=True)

    seed = get_seed(ids[0], path=library_path)
    for attempt in range(3):
        try:
            image = await scene_images._pollinations_once(prompt, (seed + attempt) % 2**32, size)
            await asyncio.to_thread(scene_images._save_image, image, path)
            validate_generated_file()
            return
        except (RuntimeError, ValueError, OSError, Image.DecompressionBombError) as exc:
            path.unlink(missing_ok=True)
            pollinations_error = exc
    raise RuntimeError(
        f"Cloudflare/Hugging Face and configured image fallbacks failed "
        f"({last_error}); Pollinations fallback failed: {pollinations_error}"
    ) from pollinations_error


async def make_hf_inference_frame(prompt, path, size):
    """Use Hugging Face Inference as a fallback when Cloudflare's image quota is exhausted."""
    if not HF_TOKEN:
        raise RuntimeError("Cloudflare image quota is exhausted and HF_TOKEN is not configured.")
    url = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"
    try:
        async with httpx.AsyncClient(timeout=180) as http:
            response = await http.post(
                url,
                headers={"Authorization": f"Bearer {HF_TOKEN}"},
                json={
                    "inputs": prompt,
                    "parameters": {
                        "width": 1024,
                        "height": 576 if size[0] > size[1] else 1024,
                    },
                },
            )
        response.raise_for_status()
        with Image.open(io.BytesIO(response.content)) as image:
            image = ImageOps.fit(image.convert("RGB"), size, method=Image.Resampling.LANCZOS)
    except (httpx.HTTPError, UnidentifiedImageError, OSError) as exc:
        raise RuntimeError(f"Hugging Face image fallback failed: {exc}") from exc
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG")


async def _cloudflare_frame(prompt, path, size=(720, 1280)):
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
        raise RuntimeError("Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN.")
    body = {"prompt": prompt[:2048], "steps": 8}
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/"
        "@cf/black-forest-labs/flux-1-schnell"
    )

    def rejects_steps(response):
        try:
            payload = response.json()
        except ValueError:
            return False
        errors = payload.get("errors", []) if isinstance(payload, dict) else []
        messages = [
            str(error.get("message", "")).lower()
            for error in errors if isinstance(error, dict)
        ]
        codes = [
            error.get("code") for error in errors
            if isinstance(error, dict)
        ]
        rejection_terms = (
            "unknown", "unexpected", "not allowed", "unsupported",
            "unrecognized", "additional propert", "unevaluated propert",
        )
        return any(
            "steps" in message and any(term in message for term in rejection_terms)
            for message in messages
        ) or any(
            code == 5006 and any(
                term in message for term in ("unevaluated propert", "additional propert")
            )
            for code, message in zip(codes, messages)
        )

    try:
        async with httpx.AsyncClient(timeout=120) as http:
            response = await http.post(
                url, headers={"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"},
                json=body,
            )
            if "steps" in body and rejects_steps(response):
                body = {"prompt": body["prompt"]}
                response = await http.post(
                    url, headers={"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"},
                    json=body,
                )
            response.raise_for_status()
            payload = response.json()
            if (
                isinstance(payload, dict) and payload.get("success") is not True
                and "steps" in body and rejects_steps(response)
            ):
                body = {"prompt": body["prompt"]}
                response = await http.post(
                    url, headers={"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"},
                    json=body,
                )
                response.raise_for_status()
                payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(cloudflare_error(exc.response)) from exc
    except httpx.RequestError as exc:
        raise RuntimeError("Could not reach Cloudflare image generation; try again.") from exc

    try:
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise RuntimeError(cloudflare_error(response))
        image_bytes = base64.b64decode(payload["result"]["image"], validate=True)
        if not image_bytes or len(image_bytes) > 20 * 1024 * 1024:
            raise ValueError("Cloudflare returned an empty or oversized image.")
        with Image.open(io.BytesIO(image_bytes)) as source:
            image = ImageOps.fit(source.convert("RGB"), size, method=Image.Resampling.LANCZOS)
    except (
        ValueError, KeyError, TypeError, binascii.Error, UnidentifiedImageError,
        OSError, Image.DecompressionBombError,
    ) as exc:
        raise RuntimeError("Cloudflare returned an invalid image response.") from exc
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        image.save(temporary, format="PNG")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


async def make_fal_motion(scene, image_path, path, size=(720, 1280)):
    """Animate a scene with the selected hosted API, then normalize it for FFmpeg."""
    await animation.animate_scene(
        scene, image_path, path, provider=VIDEO_PROVIDER, size=size, db_path=DB_PATH
    )


async def finalize_motion(raw_bytes, path, size=(720, 1280)):
    raw = path.with_suffix(".raw.mp4")
    raw.write_bytes(raw_bytes)
    # Play forward then backward so a 5s clip can loop under longer narration without a visible jump.
    result = await asyncio.to_thread(
        subprocess.run,
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw), "-filter_complex",
         f"[0:v]scale={size[0]}:{size[1]}:force_original_aspect_ratio=increase,crop={size[0]}:{size[1]},fps=24,split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1:a=0[v]",
         "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)],
        capture_output=True, text=True, timeout=240,
    )
    raw.unlink(missing_ok=True)
    if result.returncode:
        raise RuntimeError(f"Could not process fal.ai clip: {result.stderr[-300:]}")


async def make_hf_motion(scene, image_path, path, size=(720, 1280)):
    """Free image-to-video through a public Hugging Face Space (slow, queued, may fail)."""
    from gradio_client import Client, handle_file

    prompt = ("Gentle children's cartoon movement, preserving the character design. " + str(scene.get("motion") or scene.get("visual_description") or ""))[:500]

    def run():
        client = Client(HF_SPACE, hf_token=HF_TOKEN or None)
        return client.predict(
            prompt=prompt, negative_prompt="worst quality, distorted, text, watermark",
            input_image_filepath=handle_file(str(image_path)), input_video_filepath=None,
            height_ui=512 if size[0] > size[1] else 704, width_ui=704 if size[0] > size[1] else 512, mode="image-to-video", duration_ui=2,
            ui_frames_to_use=9, seed_ui=42, randomize_seed=True, ui_guidance_scale=1,
            improve_texture_flag=True, api_name=HF_API_NAME,
        )

    result = await asyncio.wait_for(asyncio.to_thread(run), timeout=900)
    found = None
    stack = [result]
    while stack and not found:
        item = stack.pop(0)
        if isinstance(item, (list, tuple)):
            stack.extend(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, str) and item.lower().endswith((".mp4", ".webm")) and os.path.isfile(item):
            found = item
    if not found:
        raise RuntimeError("Hugging Face Space returned no video.")
    await finalize_motion(Path(found).read_bytes(), path, size)


def audio_duration(path) -> float:
    result = subprocess.run(["ffmpeg", "-i", str(path)], capture_output=True, text=True)
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        raise RuntimeError("Could not read voice duration")
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def shape_text(text, language):
    if language != "ar":
        return text
    return get_display(arabic_reshaper.reshape(text))


def make_caption_images(narration, language, duration, folder, index):
    words = narration.split()
    groups = [words[i:i + 5] for i in range(0, len(words), 5)] or [[""]]
    total_chars = sum(len(" ".join(g)) for g in groups) or 1
    captions, cursor = [], 0.0
    caption_font = font(44, basic=True)
    for number, group in enumerate(groups):
        span = duration * len(" ".join(group)) / total_chars
        lines, line = [], ""
        for word in group:
            candidate = f"{line} {word}".strip()
            if line and caption_font.getlength(shape_text(candidate, language)) > 620:
                lines.append(line)
                line = word
            else:
                line = candidate
        lines.append(line)
        image = Image.new("RGBA", (720, 390), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        for row, text in enumerate(lines):
            draw.text(
                (360, 20 + 70 * row + 35), shape_text(text, language),
                font=caption_font, fill=(255, 255, 255, 255), anchor="mm",
                stroke_width=5, stroke_fill=(0, 0, 0, 255),
            )
        path = folder / f"caption-{index}-{number}.png"
        image.save(path)
        captions.append((path, cursor, cursor + span))
        cursor += span
    return captions


def write_caption_playlist(captions, path):
    entries = []
    for image, start, end in captions:
        entries.extend((f"file '{image.name}'", f"duration {end - start:.6f}"))
    entries.append(f"file '{captions[-1][0].name}'")
    path.write_text("\n".join(entries) + "\n", encoding="utf-8")


def ffmpeg_scene_error(result, index, total):
    if result.returncode < 0:
        reason = f"terminated by signal {-result.returncode} (check Railway logs and memory usage)"
    else:
        reason = f"exit code {result.returncode}"
    return f"FFmpeg failed on scene {index+1}/{total}: {reason}. {result.stderr[-700:]}"


def ensure_lullaby(path):
    if path.is_file():
        return path
    rate = 22050
    notes = [261.63, 293.66, 329.63, 392.0, 440.0, 392.0, 329.63, 293.66]
    melody = [0, 2, 4, 2, 3, 1, 4, 2, 5, 3, 4, 2, 1, 0, 2, 0]
    beat = 0.75
    frames = bytearray()
    for step in melody * 4:
        frequency = notes[step % len(notes)]
        for n in range(int(rate * beat)):
            t = n / rate
            envelope = math.exp(-3.0 * t / beat) * min(1.0, t * 80)
            value = 0.6 * math.sin(2 * math.pi * frequency * t) + 0.25 * math.sin(4 * math.pi * frequency * t) + 0.15 * math.sin(2 * math.pi * frequency * 0.5 * t)
            frames += struct.pack("<h", int(max(-1.0, min(1.0, value * envelope * 0.7)) * 32767))
    temporary = path.with_suffix(".tmp")
    with wave.open(str(temporary), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(bytes(frames))
    temporary.replace(path)
    return path


def make_sparkle(path):
    size = 48
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, size - 4, size - 4), fill=(255, 255, 255, 110), outline=(255, 255, 255, 200), width=2)
    draw.ellipse((12, 12, 22, 22), fill=(255, 255, 255, 220))
    image.save(path)


def animated_video_filter(index, sparkle_input, caption_input=None, captions=(), motion=False):
    # Alternate zoom-in / zoom-out per scene, with gentle sway, drifting bubbles and a fade-in.
    zoom = "min(1.10+0.0007*on,1.22)" if index % 2 == 0 else "max(1.22-0.0007*on,1.10)"
    base = "[0:v]format=yuv420p[base]" if motion else (
        "[0:v]scale=1080:1920,zoompan=z='%s':x='iw/2-(iw/zoom/2)+30*sin(on/20)':y='ih/2-(ih/zoom/2)+20*cos(on/27)':d=1:s=720x1280:fps=24,format=yuv420p[base]" % zoom)
    graph = [
        base,
        "[%d:v]format=rgba,split=6%s" % (sparkle_input, "".join(f"[s{i}]" for i in range(6))),
    ]
    previous = "base"
    for i in range(6):
        x = 40 + i * 115
        speed = 45 + (i % 3) * 20
        offset = i * 210
        graph.append(
            f"[{previous}][s{i}]overlay=x='{x}+25*sin(t*1.4+{i})':y='H-mod(t*{speed}+{offset},H+60)':format=auto[o{i}]"
        )
        previous = f"o{i}"
    if caption_input is not None:
        motion = "+".join(
            f"if(between(t,{start:.6f},{end:.6f}),max(0,1-(t-{start:.6f})/0.2),0)"
            for _, start, end in captions
        )
        graph.append(
            f"[{previous}][{caption_input}:v]overlay=x=0:y='900+24*({motion})':format=auto[captions]"
        )
        previous = "captions"
    graph.append(f"[{previous}]fade=t=in:st=0:d=0.3[vout]")
    return ";".join(graph)


async def prepare_scene(scene, index, total, language, voice, job_dir):
    frame = job_dir / f"artwork-v2-{index}.png"
    audio = job_dir / f"voice-{index}.mp3"
    motion = job_dir / f"motion-v2-{index}.mp4"

    async def prepare_image():
        if not frame.exists():
            await make_cloudflare_frame(scene, frame)
        if VIDEO_PROVIDER in animation.FAL_PROVIDERS or VIDEO_PROVIDER == "runway":
            if not motion.is_file():
                await make_fal_motion(scene, frame, motion)
        elif VIDEO_PROVIDER == "hf" and not motion.is_file():
            await make_hf_motion(scene, frame, motion)

    async def prepare_voice():
        if audio.is_file() and audio.stat().st_size:
            return
        try:
            await asyncio.wait_for(
                edge_tts.Communicate(scene["narration"], voice, rate=VOICE_RATE, pitch=VOICE_PITCH).save(str(audio)),
                timeout=120,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"Voice generation timed out on scene {index+1}/{total}") from exc

    tasks = [asyncio.create_task(prepare_image()), asyncio.create_task(prepare_voice())]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return frame, audio


async def pipeline_scenes(scenes, prepare, render):
    if not scenes:
        raise RuntimeError("Cannot render an episode without scenes")
    pending = asyncio.create_task(prepare(0, scenes[0]))
    clips = []
    try:
        for index, scene in enumerate(scenes):
            prepared = await pending
            pending = None
            if index + 1 < len(scenes):
                pending = asyncio.create_task(prepare(index + 1, scenes[index + 1]))
            clips.append(await render(index, scene, prepared))
    finally:
        if pending:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    return clips


async def render_song_episode(episode, scenes, meta, job_dir, control=None):
    if IMAGE_PROVIDER != "cloudflare":
        raise RuntimeError("Song videos need AI-generated scene images.")
    validation_error = validate_story_scenes(
        scenes, load_characters(character_library_path()), len(scenes)
    )
    if validation_error:
        raise RuntimeError(f"Saved song scene is incompatible with the character bible: {validation_error}")
    episode_id, language = episode["id"], episode["language"]

    def progress(value):
        with db() as con:
            con.execute("UPDATE episodes SET render_progress=?,updated_at=? WHERE id=?", (value, utc_now().isoformat(), episode_id))

    progress(2)
    if SONG_PROVIDER == "fal":
        if not FAL_KEY:
            raise RuntimeError("SONG_PROVIDER=fal requires FAL_KEY.")
        song_file, timings = await song_studio.make_fal_song(scenes, meta.get("music_prompt"), job_dir, FAL_KEY, FAL_MUSIC_MODEL, audio_duration)
    else:
        voice = VOICE_SONG_AR if language == "ar" else VOICE_SONG_EN
        song_file, timings = await song_studio.make_tts_song(scenes, language, job_dir, voice, audio_duration)
    progress(8)

    async def image(scene, path):
        await make_cloudflare_frame(scene, path, song_studio.SIZE, song_studio.STYLE_3D + ".")

    if VIDEO_PROVIDER in animation.FAL_PROVIDERS or VIDEO_PROVIDER == "runway":
        motion = lambda scene, frame, path, duration: make_fal_motion(scene, frame, path, song_studio.SIZE)
    elif VIDEO_PROVIDER == "hf":
        motion = lambda scene, frame, path, duration: make_hf_motion(scene, frame, path, song_studio.SIZE)
    else:
        motion = lambda scene, frame, path, duration: scene_video.make_parallax_clip(
            frame, path, duration, size=song_studio.SIZE, work_dir=job_dir
        )
    output = MEDIA_DIR / f"{episode_id}.mp4"
    await song_studio.render_song_video(
        scenes, timings, song_file, language, job_dir, output, image, motion,
        lambda size: font(size, basic=True), shape_text, progress,
        control=control,
    )
    create_video_thumbnail(output, episode.get("title", "Kids Haven"), language, MEDIA_DIR / f"{episode_id}-thumbnail.jpg")
    with db() as con:
        con.execute("UPDATE episodes SET video_path=?,status='ready_for_review',render_progress=100,updated_at=? WHERE id=?", (str(output), utc_now().isoformat(), episode_id))
    return output


def create_video_thumbnail(video_path, title, language, destination):
    source = destination.with_suffix(".source.png")
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "0.5", "-i", str(video_path),
         "-frames:v", "1", "-vf", "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720", str(source)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode or not source.is_file():
        raise RuntimeError(f"Could not create YouTube thumbnail frame: {result.stderr[-300:]}")
    try:
        with Image.open(source) as image:
            cover = image.convert("RGBA")
        overlay = Image.new("RGBA", cover.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        draw.rounded_rectangle((36, 470, 1244, 688), radius=36, fill=(0, 0, 0, 175))
        words = str(title or "Kids Haven").split()
        size = 68
        while size > 36:
            face = font(size, basic=language == "ar")
            lines, line = [], ""
            for word in words:
                candidate = f"{line} {word}".strip()
                if line and face.getlength(shape_text(candidate, language)) > 1080:
                    lines.append(line)
                    line = word
                else:
                    line = candidate
            if line:
                lines.append(line)
            if max((face.getlength(shape_text(item, language)) for item in lines), default=0) <= 1080:
                break
            size -= 4
        draw.multiline_text(
            (640, 579), "\n".join(shape_text(item, language) for item in lines),
            font=face, fill=(255, 255, 255, 255), anchor="mm", align="center",
            spacing=8, stroke_width=2, stroke_fill=(20, 20, 20, 220),
        )
        cover = Image.alpha_composite(cover, overlay).convert("RGB")
        temporary = destination.with_suffix(".tmp.jpg")
        for quality in (88, 75, 60, 45):
            cover.save(temporary, format="JPEG", quality=quality, optimize=True)
            if temporary.stat().st_size <= 2_000_000:
                break
        if temporary.stat().st_size > 2_000_000:
            raise RuntimeError("Generated YouTube thumbnail exceeds the 2 MB upload limit.")
        temporary.replace(destination)
        return destination
    finally:
        source.unlink(missing_ok=True)


async def render_video(episode_id: str):
    with db() as con:
        row = con.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not row:
        raise RuntimeError("Episode not found")
    episode = dict(row)
    episode_meta = json.loads(episode["meta"]) if episode.get("meta") else {}
    scenes = json.loads(episode["scenes"])
    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError("Episode has no renderable scene list.")
    validation_error = validate_story_scenes(
        scenes, load_characters(character_library_path()), len(scenes)
    )
    if validation_error:
        raise RuntimeError(f"Saved episode scene is incompatible with the character bible: {validation_error}")
    check_image_provider()
    if not subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode == 0:
        raise RuntimeError("FFmpeg is missing")
    job_dir = WORK_DIR / episode_id
    if episode.get("source") == "idea":
        job_dir /= f"{IMAGE_PROVIDER}-{VIDEO_PROVIDER}-{SONG_PROVIDER}-{CONTENT_MODE}"
    job_dir.mkdir(parents=True, exist_ok=True)
    async def control():
        if episode.get("source") == "idea":
            await wait_for_production_job(episode_id)
    if episode_meta.get("kind") == "song":
        return await render_song_episode(episode, scenes, episode_meta, job_dir, control=control)
    voice = episode_meta.get("voice") or (VOICE_AR if episode["language"] == "ar" else VOICE_EN)
    music = Path(BACKGROUND_MUSIC_PATH) if BACKGROUND_MUSIC_PATH else None
    if music and not music.is_file():
        music = None
    if music is None:
        music = ensure_lullaby(DATA_DIR / "lullaby.wav")
    async def prepare(index, scene):
        await control()
        return await prepare_scene(scene, index, len(scenes), episode["language"], voice, job_dir)

    async def render(index, scene, prepared):
        await control()
        frame, audio = prepared
        clip = job_dir / f"clip-{index}.mp4"
        if clip.is_file() and clip.stat().st_size:
            return clip
        temporary_clip = job_dir / f"clip-{index}.tmp.mp4"
        with db() as con:
            con.execute("UPDATE episodes SET render_progress=?,updated_at=? WHERE id=?", (int(index*90/len(scenes)), utc_now().isoformat(), episode_id))
        motion_path = job_dir / f"motion-v2-{index}.mp4"
        use_motion = VIDEO_PROVIDER in (*animation.FAL_PROVIDERS, "runway", "hf")
        if not use_motion:
            await scene_video.make_parallax_clip(
                frame, motion_path, audio_duration(audio), size=(720, 1280), work_dir=job_dir
            )
        if not motion_path.is_file() or motion_path.stat().st_size == 0:
            raise RuntimeError(f"No animated clip was produced for scene {index + 1}.")
        command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats", "-y", "-stream_loop", "-1", "-i", str(motion_path), "-i", str(audio)]
        if music:
            command += ["-stream_loop", "-1", "-i", str(music)]
        if IMAGE_PROVIDER == "cloudflare":
            sparkle = job_dir / "sparkle.png"
            if not sparkle.exists():
                make_sparkle(sparkle)
            command += ["-loop", "1", "-framerate", "24", "-i", str(sparkle)]
            sparkle_input = 3
            captions = make_caption_images(scene["narration"], episode["language"], audio_duration(audio), job_dir, index)
            playlist = job_dir / f"captions-{index}.txt"
            write_caption_playlist(captions, playlist)
            command += ["-f", "concat", "-safe", "1", "-i", str(playlist)]
            filters = [animated_video_filter(index, sparkle_input, 4, captions, motion=True)]
            video_map = "[vout]"
        if music:
            filters.append("[2:a]volume=0.12[bg];[1:a][bg]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]")
            audio_map = "[aout]"
        else:
            audio_map = "1:a"
        command += ["-filter_complex", ";".join(filters), "-map", video_map, "-map", audio_map,
                    "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(temporary_clip)]
        result = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=240)
        if result.returncode:
            temporary_clip.unlink(missing_ok=True)
            raise RuntimeError(ffmpeg_scene_error(result, index, len(scenes)))
        temporary_clip.replace(clip)
        with db() as con:
            con.execute("UPDATE episodes SET render_progress=?,updated_at=? WHERE id=?", (int((index+1)*90/len(scenes)), utc_now().isoformat(), episode_id))
        return clip

    clips = await pipeline_scenes(scenes, prepare, render)
    scene_images.ensure_distinct_images([
        job_dir / f"artwork-v2-{index}.png" for index in range(len(scenes))
    ])
    concat = job_dir / "concat.txt"
    concat.write_text("".join(f"file '{clip.as_posix()}'\n" for clip in clips), encoding="utf-8")
    output = MEDIA_DIR / f"{episode_id}.mp4"
    result = await asyncio.to_thread(subprocess.run, ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", "-movflags", "+faststart", str(output)], capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise RuntimeError(f"Video assembly failed: {result.stderr[-700:]}")
    create_video_thumbnail(output, episode["title"], episode["language"], MEDIA_DIR / f"{episode_id}-thumbnail.jpg")
    with db() as con:
        con.execute("UPDATE episodes SET video_path=?,status='ready_for_review',render_progress=100,updated_at=? WHERE id=?", (str(output), utc_now().isoformat(), episode_id))
    return output


def telegram_keyboard(episode_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ نشر الآن", callback_data=f"publish:{episode_id}"),
         InlineKeyboardButton("🗓 جدولة النشر", callback_data=f"schedule:{episode_id}")],
        [InlineKeyboardButton("🗑 رفض", callback_data=f"reject:{episode_id}")],
    ])


async def create_daily_episode(language: str, run_date: date):
    run_key = run_date.isoformat()
    episode_id = None
    with db() as con:
        existing = con.execute(
            "SELECT * FROM episodes WHERE episode_date=? AND language=? AND source='daily'",
            (run_key, language),
        ).fetchone()
        if existing:
            episode = dict(existing)
            updated_at = datetime.fromisoformat(episode["updated_at"])
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            retry = episode["status"] == "failed" or (
                episode["status"] == "rendering" and utc_now() - updated_at > RENDER_RETRY_AFTER
            )
            if not retry or not episode["scenes"]:
                return episode
            episode_id = episode["id"]
            con.execute(
                "UPDATE episodes SET status='rendering',render_progress=0,render_error=NULL,updated_at=? WHERE id=?",
                (utc_now().isoformat(), episode_id),
            )
            con.execute("INSERT OR REPLACE INTO daily_runs(run_date,language,status,error) VALUES(?,?,?,NULL)", (run_key, language, "rendering"))
        else:
            con.execute("INSERT OR REPLACE INTO daily_runs(run_date,language,status,error) VALUES(?,?,?,NULL)", (run_key, language, "generating"))
    try:
        if episode_id is None:
            try:
                model = await asyncio.wait_for(select_model(), timeout=90)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("Timed out while selecting a text model from FastLLM.") from exc
            if CONTENT_MODE == "song":
                topic = song_studio.SONG_TOPICS[(run_date.toordinal() + (1 if language == "en" else 0)) % len(song_studio.SONG_TOPICS)] if not DAILY_TOPIC else DAILY_TOPIC
                try:
                    story = await asyncio.wait_for(
                        song_studio.write_song(client, model, topic, language, recent_titles()),
                        timeout=300,
                    )
                except asyncio.TimeoutError as exc:
                    raise RuntimeError("Timed out while writing the song with FastLLM.") from exc
                sheet = character_sheet(story)
                scenes = song_studio.normalize_sections(story, sheet)
                meta = json.dumps({"kind": "song", "music_prompt": str(story.get("music_prompt") or ""), "tags": [str(t)[:30] for t in (story.get("tags") or []) if str(t).strip()][:15], "lyrics": song_studio.lyrics_text(scenes)}, ensure_ascii=False)
            else:
                meta = None
                topic = pick_topic(run_date)
                try:
                    story = await asyncio.wait_for(write_story(topic, language, model, recent_titles()), timeout=300)
                except asyncio.TimeoutError as exc:
                    raise RuntimeError("Timed out while generating the story from FastLLM.") from exc
                sheet = character_sheet(story)
                raw_scenes = story.get("scenes")
                if not isinstance(raw_scenes, list):
                    raise RuntimeError("The AI did not return scenes")
                scenes = [{
                    "visual_description": str(scene["visual_description"]).strip(),
                    "location": str(scene["location"]).strip(),
                    "camera_angle": str(scene["camera_angle"]).strip(),
                    "character_ids": list(scene["character_ids"]),
                    "narration": str(scene.get("narration", "")).strip(),
                    "sound_effect": str(scene.get("sound_effect", "gentle ambience")),
                    "motion": str(scene.get("motion", "")).strip(),
                    "character_sheet": sheet,
                } for scene in raw_scenes if isinstance(scene, dict) and str(scene.get("narration", "")).strip()]
            if not scenes:
                raise RuntimeError("The AI returned empty narration")
            episode_id = str(uuid.uuid4())
            timestamp = utc_now().isoformat()
            with db() as con:
                con.execute("INSERT INTO episodes(id,episode_date,language,topic,title,description,script,scenes,status,created_at,updated_at,model,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (episode_id, run_key, language, topic, str(story.get("title") or topic)[:180], str(story.get("description") or "An original Kids Haven adventure")[:3000], "\n\n".join(scene["narration"] for scene in scenes), json.dumps(scenes, ensure_ascii=False), "rendering", timestamp, timestamp, model, meta))
                con.execute("UPDATE daily_runs SET status='rendering' WHERE run_date=? AND language=?", (run_key, language))
        await render_video(episode_id)
        with db() as con:
            con.execute("UPDATE episodes SET status='pending_review' WHERE id=?", (episode_id,))
            con.execute("UPDATE daily_runs SET status='pending_review' WHERE run_date=? AND language=?", (run_key, language))
        return dict(con.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone())
    except Exception as exc:
        with db() as con:
            if episode_id:
                con.execute("UPDATE episodes SET status='failed',render_error=?,updated_at=? WHERE id=?", (str(exc)[:1000], utc_now().isoformat(), episode_id))
            con.execute("UPDATE daily_runs SET status='failed',error=? WHERE run_date=? AND language=?", (str(exc)[:1000], run_key, language))
        raise


def compress_for_telegram(source: Path, folder: Path) -> Path:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
        capture_output=True, text=True, timeout=30,
    )
    try:
        duration = float(probe.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("Could not determine video duration for Telegram compression.") from exc
    if probe.returncode or not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("Could not determine video duration for Telegram compression.")
    bitrate = int(TELEGRAM_VIDEO_TARGET * 8 / duration - 96_000)
    if bitrate < 100_000:
        raise RuntimeError("Video is too long to compress for Telegram without excessive quality loss.")
    output = folder / "review.mp4"
    common = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
        "-map", "0:v:0", "-c:v", "libx264", "-preset", "veryfast", "-threads", "2",
        "-pix_fmt", "yuv420p", "-b:v", str(bitrate), "-passlogfile", str(folder / "pass"),
    ]
    for pass_args in (
        ["-pass", "1", "-an", "-f", "null", os.devnull],
        ["-pass", "2", "-map", "0:a:0?", "-c:a", "aac", "-b:a", "96k",
         "-movflags", "+faststart", str(output)],
    ):
        result = subprocess.run(common + pass_args, capture_output=True, text=True, timeout=600)
        if result.returncode:
            raise RuntimeError(f"Telegram video compression failed (exit {result.returncode}): {result.stderr[-500:]}")
    if not output.is_file() or output.stat().st_size >= TELEGRAM_VIDEO_LIMIT:
        raise RuntimeError("Compressed video still exceeds the Telegram upload limit.")
    return output


async def send_episode_for_review(episode_id: str):
    if not telegram_app or not ADMIN_TELEGRAM_ID:
        return False
    with db() as con:
        row = con.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not row or not row["video_path"]:
        return False
    caption = f"مسودة جديدة للمراجعة\n{row['title']}\nاللغة: {'العربية' if row['language']=='ar' else 'English'}\n\nلن تُنشر إلا بعد ضغط زر الموافقة."
    try:
        source = Path(row["video_path"])
        with tempfile.TemporaryDirectory(prefix="telegram-review-", dir=WORK_DIR) as directory:
            video_path = source
            if source.stat().st_size >= TELEGRAM_VIDEO_LIMIT:
                await telegram_app.bot.send_message(chat_id=int(ADMIN_TELEGRAM_ID), text="الفيديو جاهز؛ جارٍ ضغط نسخة للمراجعة في Telegram.")
                video_path = await asyncio.to_thread(compress_for_telegram, source, Path(directory))
            with video_path.open("rb") as video:
                await telegram_app.bot.send_video(chat_id=int(ADMIN_TELEGRAM_ID), video=video, caption=caption[:1000], supports_streaming=True, reply_markup=telegram_keyboard(episode_id), write_timeout=180)
        return True
    except Exception as exc:
        await telegram_app.bot.send_message(chat_id=int(ADMIN_TELEGRAM_ID), text=f"جهزت الحلقة لكن تعذر إرسال الفيديو إلى Telegram: {str(exc)[:500]}\nلإعادة المحاولة: /review {episode_id}")
        return False


async def tg_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text("غير مصرح.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("استخدم /review متبوعاً بمعرّف الحلقة.")
        return
    episode_id = context.args[0]
    with db() as con:
        row = con.execute("SELECT status,video_path FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not row or row["status"] not in ("pending_review", "ready_for_review") or not row["video_path"]:
        await update.message.reply_text("لم أجد فيديو جاهزاً للمراجعة بهذا المعرّف.")
        return
    await send_episode_for_review(episode_id)


async def tg_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text("غير مصرح.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("استخدم /resume متبوعاً بمعرّف مهمة متوقفة أو فاشلة.")
        return
    if auto_studio:
        message = None
        with auto_studio.db() as con:
            job = con.execute("SELECT status FROM auto_jobs WHERE id=?", (context.args[0],)).fetchone()
            if job:
                upload = con.execute("SELECT state FROM auto_uploads WHERE job_id=?", (context.args[0],)).fetchone()
                if job["status"] != "failed":
                    message = "المهمة التلقائية ليست فاشلة."
                elif upload and upload["state"] not in ("pending", "complete"):
                    message = "حالة النشر غير مؤكدة؛ تحقق من Telegram وYouTube قبل إعادة المحاولة لتجنب التكرار."
                else:
                    con.execute("UPDATE auto_jobs SET status='queued',error=NULL WHERE id=?", (context.args[0],))
                    message = "تمت إعادة المهمة التلقائية إلى الطابور."
        if message:
            await update.message.reply_text(message)
            return
    await produce_idea_episode(context.args[0], update.message.reply_text)


async def tg_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text("غير مصرح.")
        return
    if len(context.args) != 3:
        await update.message.reply_text("استخدم: /schedule EPISODE_ID YYYY-MM-DD HH:MM — الوقت بتوقيت UTC.")
        return
    episode_id = context.args[0]
    with db() as con:
        row = con.execute("SELECT status FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not row or row["status"] not in ("pending_review", "ready_for_review"):
        await update.message.reply_text("الحلقة ليست بانتظار المراجعة.")
        return
    try:
        publish_at = datetime.strptime(
            f"{context.args[1]} {context.args[2]}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=timezone.utc)
        link = await publish_episode(episode_id, publish_at=publish_at)
        await update.message.reply_text(f"تمت جدولة الفيديو على YouTube:\n{link}")
    except (ValueError, RuntimeError) as exc:
        await update.message.reply_text(f"تعذر جدولة النشر: {str(exc)[:700]}")
    except Exception as exc:
        await update.message.reply_text(f"فشلت جدولة النشر: {str(exc)[:700]}")


async def tg_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text("غير مصرح.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("استخدم /pause متبوعاً بمعرّف المهمة.")
        return
    episode_id = context.args[0]
    with db() as con:
        changed = con.execute(
            "UPDATE production_jobs SET status='paused',updated_at=? WHERE episode_id=? AND status IN ('queued','running')",
            (utc_now().isoformat(), episode_id),
        ).rowcount
        if changed:
            con.execute("UPDATE episodes SET status='paused',updated_at=? WHERE id=?", (utc_now().isoformat(), episode_id))
    await update.message.reply_text(
        "تم إيقاف المهمة مؤقتاً؛ إذا كان طلب خارجي جارياً فسيكتمل قبل التوقف."
        if changed else "المهمة غير موجودة أو ليست قيد التشغيل."
    )


async def tg_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text("غير مصرح.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("استخدم /stop متبوعاً بمعرّف المهمة.")
        return
    episode_id = context.args[0]
    with db() as con:
        changed = con.execute(
            "UPDATE production_jobs SET status='cancelled',updated_at=? WHERE episode_id=? AND status IN ('queued','running','paused')",
            (utc_now().isoformat(), episode_id),
        ).rowcount
        if changed:
            con.execute("UPDATE episodes SET status='cancelled',updated_at=? WHERE id=?", (utc_now().isoformat(), episode_id))
    await update.message.reply_text(
        "تم إلغاء المهمة. قد يكتمل طلب المزود الحالي قبل إيقاف بقية الخطوات."
        if changed else "المهمة غير موجودة أو سبق اكتمالها."
    )


async def tg_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text("غير مصرح.")
        return
    with db() as con:
        rows = con.execute(
            "SELECT j.episode_id,j.status,j.attempts,j.error,j.cost_usd,e.title,e.render_progress "
            "FROM production_jobs j JOIN episodes e ON e.id=j.episode_id "
            "ORDER BY j.created_at DESC LIMIT 15"
        ).fetchall()
    labels = {
        "queued": "بالانتظار", "running": "قيد الإنتاج", "paused": "متوقفة",
        "completed": "جاهزة للمراجعة", "failed": "فشلت", "cancelled": "ملغاة",
    }
    lines = []
    for row in rows:
        line = f"{labels.get(row['status'], row['status'])} · {row['title']} ({row['render_progress']}%)\n{row['episode_id']}"
        if row["status"] == "running":
            line += f"\n/pause {row['episode_id']} · /stop {row['episode_id']}"
        elif row["status"] in ("paused", "failed"):
            line += f"\n/resume {row['episode_id']} · /stop {row['episode_id']}"
        if row["error"]:
            line += f"\nالخطأ: {row['error'][:180]}"
        if row["cost_usd"] is not None:
            line += f"\nالتكلفة المسجلة: ${row['cost_usd']:.2f}"
        else:
            line += f"\nلتسجيل تكلفة الفاتورة: /cost {row['episode_id']} USD"
        lines.append(line)
    if auto_studio:
        with auto_studio.db() as con:
            automatic = con.execute(
                "SELECT id,topic,status FROM auto_jobs WHERE status IN ('pending','queued','running') ORDER BY created_at LIMIT 15"
            ).fetchall()
        lines.extend(f"موضوع آلي · {row['status']} · {row['topic']}\n{row['id']}" for row in automatic)
    if not lines:
        await update.message.reply_text("طابور الإنتاج فارغ.")
        return
    await update.message.reply_text("\n\n".join(lines)[:3900])


async def tg_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text("غير مصرح.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("استخدم /cost EPISODE_ID USD_AMOUNT لتسجيل التكلفة الفعلية من فاتورة مزود الخدمة.")
        return
    try:
        amount = float(context.args[1])
    except ValueError:
        await update.message.reply_text("أدخل مبلغاً رقمياً بالدولار.")
        return
    if not math.isfinite(amount) or not 0 <= amount <= 10000:
        await update.message.reply_text("التكلفة يجب أن تكون بين 0 و10000 دولار.")
        return
    with db() as con:
        changed = con.execute(
            "UPDATE production_jobs SET cost_usd=?,updated_at=? WHERE episode_id=?",
            (amount, utc_now().isoformat(), context.args[0]),
        ).rowcount
    await update.message.reply_text("تم حفظ التكلفة اليدوية بالدولار." if changed else "المهمة غير موجودة.")


async def publish_episode(episode_id: str, publish_at: Optional[datetime] = None):
    youtube, _ = youtube_services()
    with db() as con:
        row = con.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not row or not row["video_path"] or not Path(row["video_path"]).is_file():
        raise RuntimeError("Episode video file is missing")
    meta = json.loads(row["meta"]) if row["meta"] else {}
    tags = ["Kids Haven", "nursery rhymes", "kids songs", "children songs", "cartoon for kids"] + list(meta.get("tags") or [])
    lyrics = ("\n\nLyrics:\n" + meta["lyrics"]) if meta.get("lyrics") else ""
    snippet = {"title": row["title"][:100], "description": (row["description"] + lyrics + "\n\n#KidsHaven #NurseryRhymes #KidsSongs").strip()[:4900], "tags": tags[:25], "categoryId": "1"}
    youtube_status = {
        "privacyStatus": "private" if publish_at else "public",
        "selfDeclaredMadeForKids": True,
    }
    if publish_at:
        if publish_at.tzinfo is None or publish_at <= utc_now() + timedelta(minutes=10):
            raise RuntimeError("Scheduled publish time must be at least 10 minutes in the future and include a timezone.")
        youtube_status["publishAt"] = publish_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    body = {"snippet": snippet, "status": youtube_status}
    thumbnail = MEDIA_DIR / f"{episode_id}-thumbnail.jpg"
    thumbnail_warning = ""
    if not thumbnail.is_file():
        try:
            await asyncio.to_thread(create_video_thumbnail, row["video_path"], row["title"], row["language"], thumbnail)
        except Exception as exc:
            thumbnail_warning = f"\nتعذر إنشاء الصورة المصغرة: {str(exc)[:200]}"
    media = MediaFileUpload(row["video_path"], mimetype="video/mp4", resumable=True, chunksize=8 * 1024 * 1024)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    result = await asyncio.to_thread(request.execute)
    video_id = result["id"]
    link = f"https://www.youtube.com/watch?v={video_id}"
    status = "scheduled" if publish_at else "published"
    with db() as con:
        con.execute("UPDATE episodes SET status=?,youtube_video_id=?,youtube_url=?,updated_at=? WHERE id=?", (status, video_id, link, utc_now().isoformat(), episode_id))
        con.execute("UPDATE daily_runs SET status=? WHERE run_date=? AND language=?", (status, row["episode_date"], row["language"]))
    if thumbnail.is_file():
        try:
            await asyncio.to_thread(
                youtube.thumbnails().set(
                    videoId=video_id,
                    media_body=MediaFileUpload(str(thumbnail), mimetype="image/jpeg"),
                ).execute
            )
        except Exception as exc:
            thumbnail_warning += f"\nالفيديو {'مجدول' if publish_at else 'نُشر'}، لكن تعذر رفع الصورة المصغرة: {str(exc)[:250]}"
    return link + thumbnail_warning


async def tg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text("غير مصرح. هذا البوت خاص بمدير القناة.")
        return
    await update.message.reply_text(
        "أهلًا بك في Kids Haven Studio.\n/new — إنشاء فيديو بمراجعة قبل النشر\n/connect — ربط قناة YouTube\n/today — إنشاء حلقات اليوم\n/create TOPIC — إنتاج ونشر تلقائي\n/auto_on HOURS — تفعيل الجدولة التلقائية\n/auto_off — إيقاف الجدولة\n/queue — الطابور\n/status — الحالة\n/video_usage — استهلاك تحريك الفيديو اليومي\n/stats — إحصاءات YouTube\n\nمسار /new و/today يحتاج موافقتك للنشر؛ /create و/auto_on ينشران تلقائياً.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✨ إنشاء فيديو من فكرة", callback_data="idea:new")],
            [InlineKeyboardButton("🎬 إنشاء حلقات اليوم", callback_data="generate:today")],
        ]),
    )


def settings_keyboard():
    video_options = [
        ("بدون تحريك", "none"), ("Kling عبر fal.ai", "kling"), ("Hailuo عبر fal.ai", "hailuo"),
        ("PixVerse عبر fal.ai", "pixverse"), ("Vidu عبر fal.ai", "vidu"),
        ("Seedance عبر fal.ai", "dreamina"), ("Runway API", "runway"),
        ("Google Veo عبر fal.ai", "veo"), ("fal.ai مخصص", "fal"),
        ("Hugging Face", "hf"),
    ]
    options = [
        ("نوع المحتوى", "content_mode", [("قصص", "story"), ("أغاني", "song")]),
        ("الصور", "image_provider", [("Cloudflare AI + Pollinations fallback", "cloudflare")]),
        ("الفيديو", "video_provider", [
            option for option in video_options if option[1] != "blender"
        ]),
        ("الموسيقى", "song_provider", [("TTS وإيقاع", "tts"), ("غناء fal.ai", "fal")]),
    ]
    rows = []
    for label, key, values in options:
        for start in range(0, len(values), 2):
            rows.append([
                InlineKeyboardButton(
                    f"{label}: {name}", callback_data=f"settings:{key}:{value}"
                )
                for name, value in values[start:start + 2]
            ])
    return InlineKeyboardMarkup(rows)


async def tg_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text("غير مصرح.")
        return
    await update.message.reply_text(
        f"الإعدادات الفعّالة حالياً:\nالمحتوى: {CONTENT_MODE}\nالصور: Cloudflare AI مع Pollinations كبديل تلقائي\nالفيديو: {VIDEO_PROVIDER}\nالموسيقى: {SONG_PROVIDER}\n\nأضف مفاتيح الخدمات المدفوعة إلى Railway قبل اختيارها.",
        reply_markup=settings_keyboard(),
    )


async def tg_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text("غير مصرح.")
        return
    context.user_data["idea_flow"] = {"step": "idea"}
    await update.message.reply_text(
        "أرسل فكرة الفيديو بجملة أو جملتين. بعدها تختار النوع والمدة واللغة والصوت، "
        "وتراجع السيناريو قبل بدء الإنتاج.\n/cancel لإلغاء."
    )


async def tg_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text("غير مصرح.")
        return
    context.user_data.pop("idea_flow", None)
    await update.message.reply_text("تم إلغاء إعداد الفكرة.")


async def tg_idea_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update) or not update.message:
        return
    flow = context.user_data.get("idea_flow")
    if not flow or flow.get("step") != "idea":
        return
    idea = (update.message.text or "").strip()
    if not 3 <= len(idea) <= 500:
        await update.message.reply_text("اكتب الفكرة بين 3 و500 حرف.")
        return
    flow.update(idea=idea, step="type")
    await update.message.reply_text(
        "اختر نوع الفيديو:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("قصة", callback_data="idea:type:story"),
            InlineKeyboardButton("تعليمي", callback_data="idea:type:educational"),
            InlineKeyboardButton("مغامرة", callback_data="idea:type:adventure"),
        ]]),
    )


def idea_selection_keyboard(step):
    choices = {
        "duration": [("قصير (نحو دقيقة)", "short"), ("قصة (نحو 2-3 دقائق)", "long")],
        "language": [("العربية", "ar"), ("English", "en")],
        "voice": [("الصوت الأساسي", "primary"), ("صوت بديل", "alternate")],
    }
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(label, callback_data=f"idea:{step}:{value}")
        for label, value in choices[step]
    ]])


async def create_idea_draft(flow):
    language = flow["language"]
    min_words, max_words, scene_count = (100, 140, 3) if flow["duration"] == "short" else (300, 360, 6)
    model = await asyncio.wait_for(select_model(), timeout=90)
    story = await asyncio.wait_for(
        write_story(
            flow["idea"], language, model, recent_titles(),
            min_words=min_words, max_words=max_words, scene_count=scene_count,
            max_scenes=scene_count, content_type=flow["type"],
        ),
        timeout=300,
    )
    sheet = character_sheet(story)
    raw_scenes = story.get("scenes")
    if not isinstance(raw_scenes, list):
        raise RuntimeError("النموذج لم يُرجع مشاهد للسيناريو.")
    scenes = [{
        "visual_description": str(scene.get("visual_description", "")).strip(),
        "location": str(scene.get("location", "")).strip(),
        "camera_angle": str(scene.get("camera_angle", "")).strip(),
        "character_ids": list(scene.get("character_ids") or []),
        "narration": str(scene.get("narration", "")).strip(),
        "sound_effect": str(scene.get("sound_effect", "gentle ambience")).strip(),
        "motion": str(scene.get("motion", "")).strip(),
        "character_sheet": sheet,
    } for scene in raw_scenes[:scene_count]
        if isinstance(scene, dict)
        and str(scene.get("narration", "")).strip()
        and str(scene.get("visual_description", "")).strip()]
    if len(scenes) != scene_count:
        raise RuntimeError(f"السيناريو يحتاج {scene_count} مشاهد صالحة؛ وصل {len(scenes)} فقط.")
    episode_id = str(uuid.uuid4())
    timestamp = utc_now().isoformat()
    meta = json.dumps({
        "kind": "story", "source": "idea", "idea": flow["idea"],
        "content_type": flow["type"], "duration": flow["duration"], "voice": flow["voice"],
    }, ensure_ascii=False)
    with db() as con:
        con.execute(
            "INSERT INTO episodes(id,episode_date,language,topic,title,description,script,scenes,status,created_at,updated_at,model,meta,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                episode_id, utc_now().date().isoformat(), language, flow["idea"],
                str(story.get("title") or flow["idea"])[:180],
                str(story.get("description") or "")[:3000],
                "\n\n".join(scene["narration"] for scene in scenes),
                json.dumps(scenes, ensure_ascii=False), "draft", timestamp, timestamp,
                model, meta, "idea",
            ),
        )
    return episode_id, story, scenes


async def send_idea_preview(message, episode_id, story, scenes):
    title = str(story.get("title") or "مسودة سيناريو").strip()
    text = f"معاينة السيناريو: {title}\n\n" + "\n\n".join(scene["narration"] for scene in scenes)
    await message.reply_text(text[:3900])
    outline = "\n".join(
        f"{index}. {scene['visual_description'][:240]}"
        for index, scene in enumerate(scenes, 1)
    )
    if outline:
        await message.reply_text(("وصف المشاهد:\n" + outline)[:3900])
    await message.reply_text(
        "راجع السيناريو. لن يبدأ الإنتاج إلا بعد موافقتك:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ ابدأ الإنتاج", callback_data=f"idea:produce:{episode_id}"),
            InlineKeyboardButton("🗑 احذف المسودة", callback_data=f"idea:discard:{episode_id}"),
        ]]),
    )


async def tg_idea_selection(query, context, parts):
    flow = context.user_data.get("idea_flow")
    if not flow or len(parts) != 3:
        await query.message.reply_text("انتهت جلسة إعداد الفكرة. أرسل /new للبدء من جديد.")
        return
    step, value = parts[1], parts[2]
    choices = {
        "type": {"story", "educational", "adventure"},
        "duration": {"short", "long"},
        "language": {"ar", "en"},
        "voice": {"primary", "alternate"},
    }
    if step not in choices or value not in choices[step] or flow.get("step") != step:
        await query.message.reply_text("هذا الخيار غير صالح أو انتهت صلاحيته. أرسل /new للبدء من جديد.")
        return
    flow[step] = value
    if step == "type":
        flow["step"] = "duration"
        await query.message.reply_text("اختر مدة تقريبية:", reply_markup=idea_selection_keyboard("duration"))
        return
    if step == "duration":
        flow["step"] = "language"
        await query.message.reply_text("اختر لغة السيناريو والتعليق:", reply_markup=idea_selection_keyboard("language"))
        return
    if step == "language":
        flow["step"] = "voice"
        await query.message.reply_text("اختر الصوت:", reply_markup=idea_selection_keyboard("voice"))
        return
    if generation_lock.locked():
        await query.message.reply_text("فيه إنتاج آخر شغال. أرسل /new بعد انتهائه.")
        context.user_data.pop("idea_flow", None)
        return
    flow["voice"] = {
        ("ar", "primary"): VOICE_AR,
        ("ar", "alternate"): VOICE_SONG_AR,
        ("en", "primary"): VOICE_EN,
        ("en", "alternate"): VOICE_SONG_EN,
    }[(flow["language"], value)]
    context.user_data.pop("idea_flow", None)
    await query.message.reply_text("عم أكتب السيناريو الآن. ما رح يبدأ إنتاج الفيديو قبل ما تراجعه وتوافق.")
    try:
        async with generation_lock:
            episode_id, story, scenes = await create_idea_draft(flow)
        await send_idea_preview(query.message, episode_id, story, scenes)
    except Exception as exc:
        await query.message.reply_text(f"تعذر إنشاء معاينة السيناريو: {str(exc)[:700]}")


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
    await create_today(update.message)


generation_lock = asyncio.Lock()


class ProductionCancelled(Exception):
    pass


async def wait_for_production_job(episode_id):
    while True:
        with db() as con:
            row = con.execute("SELECT status FROM production_jobs WHERE episode_id=?", (episode_id,)).fetchone()
        if not row:
            return
        if row["status"] == "paused":
            await asyncio.sleep(1)
            continue
        if row["status"] == "cancelled":
            raise ProductionCancelled("Production stopped by the admin.")
        if row["status"] != "running":
            raise RuntimeError(f"Production job left the running state ({row['status']}).")
        return


async def produce_idea_episode(episode_id, report):
    with db() as con:
        row = con.execute(
            "SELECT episodes.status,episodes.source,episodes.updated_at,production_jobs.status AS job_status "
            "FROM episodes LEFT JOIN production_jobs ON production_jobs.episode_id=episodes.id WHERE episodes.id=?",
            (episode_id,),
        ).fetchone()
    if not row or row["source"] != "idea":
        await report("هذه المسودة غير موجودة أو سبق بدء إنتاجها.")
        return
    retrying_stale = (
        row["status"] == "rendering"
        and row["updated_at"] < (utc_now() - RENDER_RETRY_AFTER).isoformat()
    )
    paused_retry = row["status"] == "paused" and row["job_status"] == "paused"
    if row["status"] not in ("draft", "failed") and not retrying_stale and not paused_retry:
        await report("هذه المسودة غير موجودة أو سبق بدء إنتاجها.")
        return
    timestamp = utc_now().isoformat()
    settings_json = json.dumps({
        "image_provider": IMAGE_PROVIDER,
        "video_provider": VIDEO_PROVIDER,
        "song_provider": SONG_PROVIDER,
        "content_mode": "story",
    })
    with db() as con:
        con.execute(
            "INSERT INTO production_jobs(id,episode_id,status,attempts,settings_json,created_at,updated_at) VALUES(?,?, 'queued',0,?,?,?) "
            "ON CONFLICT(episode_id) DO UPDATE SET status='queued',error=NULL,settings_json=excluded.settings_json,updated_at=excluded.updated_at "
            "WHERE production_jobs.status IN ('failed','paused')",
            (episode_id, episode_id, settings_json, timestamp, timestamp),
        )
        queued = con.execute("SELECT status FROM production_jobs WHERE episode_id=?", (episode_id,)).fetchone()
        if queued and queued["status"] == "queued":
            con.execute(
                "UPDATE episodes SET status='queued',render_error=NULL,updated_at=? WHERE id=? AND source='idea' AND status IN ('draft','failed','rendering','paused','queued')",
                (timestamp, episode_id),
            )
    if not queued or queued["status"] != "queued":
        await report("هذه المهمة قيد التنفيذ أو لا يمكن إعادة تشغيلها.")
        return
    await report("تم حفظ طلب الإنتاج في الطابور. يمكنك متابعة الحالة عبر /queue.")


async def process_production_job(episode_id):
    global IMAGE_PROVIDER, VIDEO_PROVIDER, SONG_PROVIDER, CONTENT_MODE
    async with generation_lock:
        with db() as con:
            job = con.execute(
                "SELECT settings_json FROM production_jobs WHERE episode_id=? AND status='queued'",
                (episode_id,),
            ).fetchone()
            if not job:
                return
            changed = con.execute(
                "UPDATE production_jobs SET status='running',attempts=attempts+1,error=NULL,updated_at=? WHERE episode_id=? AND status='queued'",
                (utc_now().isoformat(), episode_id),
            ).rowcount
            if changed:
                con.execute("UPDATE episodes SET status='rendering',render_error=NULL,updated_at=? WHERE id=?", (utc_now().isoformat(), episode_id))
        if not changed:
            return
        previous_settings = (IMAGE_PROVIDER, VIDEO_PROVIDER, SONG_PROVIDER, CONTENT_MODE)
        try:
            if job["settings_json"]:
                settings = json.loads(job["settings_json"])
                if settings.get("image_provider") == "cards":
                    settings["image_provider"] = "cloudflare"
                if settings.get("video_provider") == "blender":
                    settings["video_provider"] = "none"
                validate_studio_settings(settings)
                IMAGE_PROVIDER = settings["image_provider"]
                VIDEO_PROVIDER = settings["video_provider"]
                SONG_PROVIDER = settings["song_provider"]
                CONTENT_MODE = settings["content_mode"]
            await wait_for_production_job(episode_id)
            await render_video(episode_id)
            with db() as con:
                con.execute("UPDATE production_jobs SET status='completed',updated_at=? WHERE episode_id=?", (utc_now().isoformat(), episode_id))
            if await send_episode_for_review(episode_id):
                await notify_admin("اكتمل الفيديو وأرسلته للمراجعة. لن يُنشر على YouTube إلا بعد موافقتك على الفيديو النهائي.")
            else:
                await notify_admin(f"اكتمل الفيديو لكن تعذر إرساله تلقائياً. أعد الإرسال للمراجعة بالأمر /review {episode_id}")
        except ProductionCancelled:
            with db() as con:
                con.execute("UPDATE production_jobs SET status='cancelled',updated_at=? WHERE episode_id=?", (utc_now().isoformat(), episode_id))
                con.execute("UPDATE episodes SET status='cancelled',updated_at=? WHERE id=?", (utc_now().isoformat(), episode_id))
        except Exception as exc:
            with db() as con:
                con.execute("UPDATE production_jobs SET status='failed',error=?,updated_at=? WHERE episode_id=?", (str(exc)[:1000], utc_now().isoformat(), episode_id))
                con.execute(
                    "UPDATE episodes SET status='failed',render_error=?,updated_at=? WHERE id=?",
                    (str(exc)[:1000], utc_now().isoformat(), episode_id),
                )
            await notify_admin(f"فشل إنتاج الفيديو: {str(exc)[:700]}\nيمكن إيقافه أو إعادة المحاولة بالأمر /resume {episode_id}")
        finally:
            IMAGE_PROVIDER, VIDEO_PROVIDER, SONG_PROVIDER, CONTENT_MODE = previous_settings


async def production_worker_loop():
    with db() as con:
        con.execute("UPDATE production_jobs SET status='queued',updated_at=? WHERE status='running'", (utc_now().isoformat(),))
        con.execute(
            "UPDATE episodes SET status='queued',updated_at=? WHERE id IN (SELECT episode_id FROM production_jobs WHERE status='queued') AND status='rendering'",
            (utc_now().isoformat(),),
        )
    while True:
        try:
            with db() as con:
                row = con.execute("SELECT episode_id FROM production_jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row:
                await process_production_job(row["episode_id"])
            else:
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await notify_admin(f"توقف عامل طابور الإنتاج مؤقتاً: {str(exc)[:400]}")
            await asyncio.sleep(5)


async def run_today(report):
    if generation_lock.locked():
        await report("هناك عملية إنشاء جارية الآن. انتظر انتهاءها.")
        return False
    async with generation_lock:
        today = utc_now().date()
        for language in DAILY_LANGUAGES:
            try:
                row = await create_daily_episode(language, today)
                if row.get("status") in ("pending_review", "ready_for_review"):
                    await send_episode_for_review(row["id"])
            except Exception as exc:
                await report(f"فشل إنشاء {'العربية' if language=='ar' else 'English'}: {str(exc)[:700]}")
    return True


async def create_today(message):
    if generation_lock.locked():
        await message.reply_text("هناك عملية إنشاء جارية الآن. أرسل /status لمتابعتها.")
        return
    await message.reply_text("بدأ إنشاء حلقات اليوم. سأرسل كل فيديو هنا للمراجعة قبل النشر.")
    if await run_today(message.reply_text):
        await message.reply_text("انتهت محاولة إنشاء حلقات اليوم. أرسل /status لمعرفة النتيجة.")


async def tg_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text("غير مصرح.")
        return
    with db() as con:
        rows = con.execute("SELECT id,source,episode_date,language,status,title,youtube_url,render_progress,render_error FROM episodes ORDER BY created_at DESC LIMIT 10").fetchall()
        runs = con.execute(
            "SELECT language,status,error FROM daily_runs WHERE run_date=? ORDER BY language",
            (utc_now().date().isoformat(),),
        ).fetchall()
    lines = [
        f"{'🇸🇦' if r['language']=='ar' else '🇬🇧'} {r['episode_date']} · {r['status']}"
        + (f" ({r['render_progress']}%)" if r['status'] == "rendering" else "")
        + f" · {r['title']}"
        + (f"\nالخطأ: {r['render_error'][:250]}" if r['status'] == "failed" and r['render_error'] else "")
        + (f"\nإعادة المحاولة: /resume {r['id']}" if r["source"] == "idea" and r["status"] == "failed" else "")
        + (f"\n{r['youtube_url']}" if r['youtube_url'] else "")
        for r in rows
    ]
    if runs:
        run_lines = ["تشغيل اليوم (UTC):"]
        for run in runs:
            label = "العربية" if run["language"] == "ar" else "English"
            line = f"{label} · {run['status']}"
            if run["error"]:
                line += f"\nالخطأ: {run['error'][:250]}"
            run_lines.append(line)
        lines = run_lines + (["\nآخر الحلقات:"] + lines if lines else [])
    if auto_studio:
        with auto_studio.db() as con:
            last = con.execute("SELECT * FROM auto_jobs ORDER BY created_at DESC LIMIT 1").fetchone()
            count = con.execute(
                "SELECT COUNT(*) FROM auto_uploads WHERE upload_day=?", (utc_now().date().isoformat(),)
            ).fetchone()[0]
            queued = con.execute("SELECT COUNT(*) FROM auto_jobs WHERE status IN ('pending','queued','running')").fetchone()[0]
        lines.append(
            f"\nالإنتاج التلقائي: {'مفعّل' if auto_studio.setting('auto_enabled', '0') == '1' else 'متوقف'}"
            f" · المواضيع المنتظرة/الجارية {queued} · رفع اليوم {count}/{MAX_UPLOADS_PER_DAY}"
        )
        if last:
            lines.append(f"آخر مهمة: {last['topic']} · {last['status']}"
                         + (f"\nالخطأ: {last['error'][:250]}" if last["error"] else "")
                         + (f"\n{last['youtube_url']}" if last["youtube_url"] else ""))
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
    if query.data == "generate:today":
        await create_today(query.message)
        return
    if query.data == "idea:new":
        context.user_data["idea_flow"] = {"step": "idea"}
        await query.message.reply_text(
            "أرسل فكرة الفيديو بجملة أو جملتين. بعدها تختار النوع والمدة واللغة والصوت، "
            "وتراجع السيناريو قبل بدء الإنتاج.\n/cancel لإلغاء."
        )
        return
    if query.data.startswith("settings:"):
        parts = query.data.split(":")
        if len(parts) != 3:
            await query.message.reply_text("خيار الإعدادات غير صالح.")
            return
        try:
            values = set_studio_settings({parts[1]: parts[2]})
            await query.message.reply_text(
                f"تم حفظ الإعدادات: المحتوى {values['content_mode']} · الصور {values['image_provider']} · "
                f"الفيديو {values['video_provider']} · الموسيقى {values['song_provider']}",
                reply_markup=settings_keyboard(),
            )
        except ValueError as exc:
            await query.message.reply_text(str(exc), reply_markup=settings_keyboard())
        return
    if query.data.startswith("idea:type:") or query.data.startswith("idea:duration:") or query.data.startswith("idea:language:") or query.data.startswith("idea:voice:"):
        await tg_idea_selection(query, context, query.data.split(":"))
        return
    if query.data.startswith("idea:produce:"):
        await produce_idea_episode(query.data.split(":", 2)[2], query.message.reply_text)
        return
    if query.data.startswith("idea:discard:"):
        episode_id = query.data.split(":", 2)[2]
        with db() as con:
            changed = con.execute(
                "UPDATE episodes SET status='rejected',updated_at=? WHERE id=? AND source='idea' AND status='draft'",
                (utc_now().isoformat(), episode_id),
            ).rowcount
        await query.message.reply_text("تم حذف المسودة." if changed else "المسودة غير موجودة أو لم تعد قابلة للحذف.")
        return
    if query.data.startswith("schedule:"):
        episode_id = query.data.split(":", 1)[1]
        await query.message.reply_text(
            f"للموافقة على الجدولة، أرسل الأمر:\n/schedule {episode_id} YYYY-MM-DD HH:MM\nالوقت بتوقيت UTC، ويجب أن يكون بعد 10 دقائق على الأقل."
        )
        return
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
                if language not in DAILY_LANGUAGES:
                    continue
                try:
                    hour, minute = map(int, time_text.split(":"))
                except ValueError:
                    continue
                if now.hour == hour and now.minute == minute:
                    with db() as con:
                        done = con.execute("SELECT status FROM daily_runs WHERE run_date=? AND language=?", (now.date().isoformat(), language)).fetchone()
                    if not done or done["status"] == "failed":
                        try:
                            async with generation_lock:
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


def register_telegram_handlers(application):
    application.add_handler(CommandHandler("start", tg_start))
    application.add_handler(CommandHandler("settings", tg_settings))
    application.add_handler(CommandHandler("new", tg_new))
    application.add_handler(CommandHandler("cancel", tg_cancel))
    application.add_handler(CommandHandler("queue", tg_queue))
    application.add_handler(CommandHandler("cost", tg_cost))
    application.add_handler(CommandHandler("pause", tg_pause))
    application.add_handler(CommandHandler("stop", tg_stop))
    application.add_handler(CommandHandler("connect", tg_connect))
    application.add_handler(CommandHandler("today", tg_today, block=False))
    application.add_handler(CommandHandler("review", tg_review, block=False))
    application.add_handler(CommandHandler("resume", tg_resume, block=False))
    application.add_handler(CommandHandler("schedule", tg_schedule, block=False))
    application.add_handler(CommandHandler("status", tg_status))
    application.add_handler(CommandHandler("stats", tg_stats))
    application.add_handler(CallbackQueryHandler(tg_callback, pattern=r"^generate:today$", block=False))
    application.add_handler(CallbackQueryHandler(tg_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tg_idea_text, block=False))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app, scheduler_task, production_worker_task, auto_studio
    if TELEGRAM_BOT_TOKEN:
        from pipeline.bot_commands import register_auto_handlers
        from pipeline.scheduler import AutoStudio

        telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        register_telegram_handlers(telegram_app)
        admins = sorted({int(item) for item in ADMIN_IDS + ((ADMIN_TELEGRAM_ID,) if ADMIN_TELEGRAM_ID else ())})
        auto_studio = AutoStudio(
            db_path=DB_PATH, data_dir=DATA_DIR, bot=telegram_app.bot, admin_ids=admins,
            channel_id=CHANNEL_ID, max_uploads=MAX_UPLOADS_PER_DAY, youtube_services=youtube_services,
            compress_for_telegram=compress_for_telegram, generation_lock=generation_lock,
            video_provider=lambda: VIDEO_PROVIDER,
        )
        register_auto_handlers(telegram_app, auto_studio, admin_only)
        await telegram_app.initialize()
        await telegram_app.start()
        await auto_studio.start()
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        scheduler_task = asyncio.create_task(scheduler_loop())
        production_worker_task = asyncio.create_task(production_worker_loop())
    yield
    if auto_studio:
        await auto_studio.stop()
        auto_studio = None
    for task in (scheduler_task, production_worker_task):
        if not task:
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if telegram_app:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()


app = FastAPI(title="Kids Haven Telegram & YouTube Studio", version="2.0.0", lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def home():
    return HTMLResponse("<html lang='ar' dir='rtl'><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>Kids Haven Studio</title><body style='font:18px system-ui;max-width:720px;margin:50px auto;padding:20px'><h1>🌊 Kids Haven Studio</h1><p>بوت Telegram لإنشاء ومراجعة فيديوهات عربية وإنجليزية، مع ربط YouTube رسميًا.</p><p>مسار المراجعة يحتاج موافقة قبل النشر. أوامر الإنتاج التلقائي تنشر بعد تفعيلها صراحةً.</p><p><a href='/docs'>توثيق API</a> · <a href='/health'>حالة الخدمة</a></p></body></html>")


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


@app.get("/studio/queue")
async def web_queue(x_api_key: Optional[str] = Header(default=None)):
    check_admin_id(x_api_key)
    with db() as con:
        rows = con.execute(
            "SELECT j.episode_id,j.status,j.attempts,j.error,j.cost_usd,j.created_at,j.updated_at,e.title,e.render_progress "
            "FROM production_jobs j JOIN episodes e ON e.id=j.episode_id ORDER BY j.created_at DESC LIMIT 100"
        ).fetchall()
    return {"jobs": [dict(row) for row in rows]}


@app.post("/studio/settings")
async def web_settings(payload: dict, x_api_key: Optional[str] = Header(default=None)):
    check_admin_id(x_api_key)
    try:
        return {"settings": set_studio_settings(payload)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/studio/queue/{episode_id}/{action}")
async def web_job_control(episode_id: str, action: str, x_api_key: Optional[str] = Header(default=None)):
    check_admin_id(x_api_key)
    if action == "resume":
        messages = []
        async def report(text):
            messages.append(text)
        await produce_idea_episode(episode_id, report)
        return {"message": messages[-1] if messages else "لم يتم تغيير المهمة."}
    if action not in ("pause", "stop"):
        raise HTTPException(400, "Action must be pause, stop, or resume.")
    target = "paused" if action == "pause" else "cancelled"
    allowed = ("queued", "running") if action == "pause" else ("queued", "running", "paused")
    placeholders = ",".join("?" for _ in allowed)
    with db() as con:
        changed = con.execute(
            f"UPDATE production_jobs SET status=?,updated_at=? WHERE episode_id=? AND status IN ({placeholders})",
            (target, utc_now().isoformat(), episode_id, *allowed),
        ).rowcount
        if changed:
            con.execute("UPDATE episodes SET status=?,updated_at=? WHERE id=?", (target, utc_now().isoformat(), episode_id))
    if not changed:
        raise HTTPException(409, "Job does not exist or cannot be controlled in its current state.")
    return {"status": target}


@app.post("/studio/queue/{episode_id}/cost")
async def web_job_cost(episode_id: str, payload: dict, x_api_key: Optional[str] = Header(default=None)):
    check_admin_id(x_api_key)
    try:
        amount = float(payload.get("cost_usd"))
    except (TypeError, ValueError):
        raise HTTPException(400, "cost_usd must be a number in USD.")
    if not math.isfinite(amount) or not 0 <= amount <= 10000:
        raise HTTPException(400, "cost_usd must be between 0 and 10000.")
    with db() as con:
        changed = con.execute(
            "UPDATE production_jobs SET cost_usd=?,updated_at=? WHERE episode_id=?",
            (amount, utc_now().isoformat(), episode_id),
        ).rowcount
    if not changed:
        raise HTTPException(404, "Production job not found.")
    return {"episode_id": episode_id, "cost_usd": amount}


@app.get("/studio/status")
async def web_status(x_api_key: Optional[str] = Header(default=None)):
    check_admin_id(x_api_key)
    with db() as con:
        counts = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) AS n FROM episodes GROUP BY status")}
        runs = [dict(r) for r in con.execute("SELECT run_date,language,status,error FROM daily_runs ORDER BY run_date DESC LIMIT 10")]
    return {
        "now_utc": utc_now().isoformat(),
        "counts": counts,
        "daily_runs": runs,
        "settings": {
            "image_provider": IMAGE_PROVIDER,
            "video_provider": VIDEO_PROVIDER,
            "song_provider": SONG_PROVIDER,
            "content_mode": CONTENT_MODE,
            "daily_languages": list(DAILY_LANGUAGES),
            "daily_ar_time": DAILY_AR_TIME,
            "daily_en_time": DAILY_EN_TIME,
            "fastllm": bool(client),
            "telegram": bool(TELEGRAM_BOT_TOKEN),
            "admin_id": bool(ADMIN_TELEGRAM_ID),
            "youtube_connected": bool(load_google_credentials()),
            "cloudflare": bool(CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN),
            "pollinations_fallback": True,
            "scheduler_running": bool(scheduler_task and not scheduler_task.done()),
            "production_worker_running": bool(production_worker_task and not production_worker_task.done()),
        },
        "generating": generation_lock.locked(),
    }


async def notify_admin(text):
    if telegram_app and ADMIN_TELEGRAM_ID:
        await telegram_app.bot.send_message(chat_id=int(ADMIN_TELEGRAM_ID), text=text)


@app.post("/studio/generate")
async def web_generate(x_api_key: Optional[str] = Header(default=None)):
    check_admin_id(x_api_key)
    if generation_lock.locked():
        return {"started": False, "reason": "already running"}
    asyncio.create_task(run_today(notify_admin))
    return {"started": True}


DASHBOARD_HTML = """<!doctype html><html lang='ar' dir='rtl'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>لوحة التحكم</title>
<style>body{font:16px system-ui;max-width:900px;margin:20px auto;padding:0 14px;background:#0f172a;color:#e2e8f0}h1{font-size:22px}.card{background:#1e293b;border-radius:12px;padding:14px;margin:12px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}.ok{color:#4ade80}.bad{color:#f87171}input,button{font:inherit;padding:8px;border-radius:8px;border:0}
.bar{background:#334155;border-radius:6px;height:10px;overflow:hidden}.bar>div{background:#38bdf8;height:100%}table{width:100%;border-collapse:collapse}td,th{padding:6px;text-align:start;border-bottom:1px solid #334155;font-size:14px}</style>
<h1>🌊 لوحة تحكم Kids Haven</h1>
<div class='card' id='login'><input id='key' type='password' placeholder='APP_API_KEY'> <button onclick='save()'>دخول</button> <span id='err' class='bad'></span></div>
<div id='main' hidden><div class='card'><button id='gen' onclick='gen()'>🎬 توليد حلقات اليوم الآن</button> <span id='genmsg'></span></div><div class='card'><b>الإعدادات</b><div class='grid' id='settings'></div><div class='grid' id='providerControls'></div><button onclick='saveProviders()'>حفظ مزودي الإنتاج</button> <span id='providerMsg'></span></div>
<div class='card'><b>الحلقات</b><div class='grid' id='counts'></div></div>
<div class='card'><b>طابور الإنتاج</b><table id='queue'></table></div>
<div class='card'><b>آخر الحلقات</b><table id='eps'></table></div>
<div class='card'><b>التشغيل اليومي</b><table id='runs'></table></div></div>
<script>
const $=id=>document.getElementById(id);
async function gen(){const r=await fetch('/studio/generate',{method:'POST',headers:{'x-api-key':sessionStorage.getItem('k')||''}});const j=await r.json();$('genmsg').textContent=j.started?'بدأ التوليد، تابع التقدم أدناه':'هناك عملية جارية';load()}
function save(){sessionStorage.setItem('k',$('key').value);load()}
function el(t,text,cls){const e=document.createElement(t);e.textContent=text;if(cls)e.className=cls;return e}
async function api(p){const r=await fetch(p,{headers:{'x-api-key':sessionStorage.getItem('k')||''}});if(!r.ok)throw new Error(r.status);return r.json()}
async function post(p,body){const r=await fetch(p,{method:'POST',headers:{'x-api-key':sessionStorage.getItem('k')||'','Content-Type':'application/json'},body:JSON.stringify(body||{})});const j=await r.json();if(!r.ok)throw new Error(j.detail||r.status);return j}
function rows(t,head,data){t.replaceChildren();const h=document.createElement('tr');head.forEach(x=>h.append(el('th',x)));t.append(h);data.forEach(r=>{const tr=document.createElement('tr');r.forEach(c=>{const td=document.createElement('td');if(c instanceof Node)td.append(c);else td.textContent=c??'';tr.append(td)});t.append(tr)})}
const providerOptions={content_mode:[['story','قصص'],['song','أغاني']],image_provider:[['cloudflare','Cloudflare AI + Pollinations']],video_provider:[['none','2.5D Parallax'],['kling','Kling via fal.ai'],['hailuo','Hailuo via fal.ai'],['pixverse','PixVerse via fal.ai'],['vidu','Vidu via fal.ai'],['dreamina','Seedance via fal.ai'],['runway','Runway API'],['veo','Google Veo via fal.ai'],['fal','fal.ai custom'],['hf','Hugging Face']],song_provider:[['tts','TTS'],['fal','غناء fal.ai']]};
function drawProviders(settings){const c=$('providerControls');c.replaceChildren();for(const [key,label] of [['content_mode','نوع المحتوى'],['image_provider','الصور'],['video_provider','الفيديو'],['song_provider','الموسيقى']]){const wrap=document.createElement('label');wrap.append(el('div',label));const s=document.createElement('select');s.dataset.setting=key;for(const [value,name] of providerOptions[key]){const o=document.createElement('option');o.value=value;o.textContent=name;s.append(o)}s.value=settings[key];wrap.append(s);c.append(wrap)}}
async function saveProviders(){const payload={};document.querySelectorAll('[data-setting]').forEach(x=>payload[x.dataset.setting]=x.value);try{const r=await post('/studio/settings',payload);$('providerMsg').textContent='تم الحفظ';drawProviders(r.settings);load()}catch(e){$('providerMsg').textContent=e.message}}
async function jobAction(id,action){try{await post('/studio/queue/'+encodeURIComponent(id)+'/'+action,{});load()}catch(e){$('genmsg').textContent=e.message}}
async function saveCost(id,input){try{await post('/studio/queue/'+encodeURIComponent(id)+'/cost',{cost_usd:input.value});load()}catch(e){$('genmsg').textContent=e.message}}
async function load(){try{const [s,e,q]=await Promise.all([api('/studio/status'),api('/studio/episodes'),api('/studio/queue')]);$('login').hidden=true;$('main').hidden=false;$('gen').disabled=s.generating;if(!document.querySelector('[data-setting]:focus'))drawProviders(s.settings);
const L={fastllm:'نموذج النص',telegram:'Telegram',admin_id:'معرّف الأدمن',youtube_connected:'YouTube',cloudflare:'Cloudflare (اختياري)',pollinations_fallback:'Pollinations fallback',scheduler_running:'الجدولة'};
$('settings').replaceChildren(...Object.entries(L).map(([k,n])=>el('div',(s.settings[k]?'✅ ':'❌ ')+n,s.settings[k]?'ok':'bad')),el('div','الصور: '+s.settings.image_provider),el('div','عربي '+s.settings.daily_ar_time+' / EN '+s.settings.daily_en_time+' UTC'));
$('counts').replaceChildren(...Object.entries(s.counts).map(([k,v])=>el('div',k+': '+v)));
rows($('queue'),['المهمة','الحالة','التقدم','التكلفة USD','تحكم'],q.jobs.map(j=>{const controls=document.createElement('span');for(const [a,n] of (j.status==='running'?[['pause','إيقاف مؤقت'],['stop','إلغاء']]:j.status==='paused'?[['resume','استكمال'],['stop','إلغاء']]:j.status==='failed'?[['resume','إعادة محاولة']]:j.status==='queued'?[['stop','إلغاء']]:[])){const b=document.createElement('button');b.textContent=n;b.onclick=()=>jobAction(j.episode_id,a);controls.append(b)}const cost=document.createElement('span');const input=document.createElement('input');input.type='number';input.min='0';input.step='0.01';input.placeholder='سجّل الفاتورة';input.value=j.cost_usd??'';const save=document.createElement('button');save.textContent='حفظ';save.onclick=()=>saveCost(j.episode_id,input);cost.append(input,save);return[j.title+' · '+j.episode_id,j.status+' · محاولات '+j.attempts,j.render_progress+'%',cost,controls]}));
rows($('eps'),['التاريخ','اللغة','العنوان','الحالة','التقدم'],e.episodes.slice(0,20).map(x=>{const b=document.createElement('div');b.className='bar';const i=document.createElement('div');i.style.width=(x.render_progress||0)+'%';b.append(i);const st=document.createElement('div');st.append(el('span',x.status));if(x.render_error)st.append(el('div',x.render_error.slice(0,200),'bad'));return[x.episode_date,x.language,x.title,st,b]}));
rows($('runs'),['التاريخ','اللغة','الحالة','خطأ'],s.daily_runs.map(r=>[r.run_date,r.language,r.status,r.error]))}catch(x){$('login').hidden=false;$('main').hidden=true;$('err').textContent=sessionStorage.getItem('k')?'مفتاح خاطئ أو APP_API_KEY غير مضبوط':''}}
if(sessionStorage.getItem('k'))load();setInterval(()=>{if(!$('main').hidden)load()},10000);
</script></html>"""


@app.get("/dashboard", include_in_schema=False)
async def dashboard():
    return HTMLResponse(DASHBOARD_HTML)
