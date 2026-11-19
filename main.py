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
from bidi.algorithm import get_display
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from openai import AsyncOpenAI
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
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
DAILY_TOPIC = (os.getenv("DAILY_TOPIC") or "").strip()
VOICE_AR = (os.getenv("VOICE_AR") or "ar-SA-HamedNeural").strip()
VOICE_EN = (os.getenv("VOICE_EN") or "en-US-GuyNeural").strip()
VOICE_RATE = (os.getenv("VOICE_RATE") or "-5%").strip()
VOICE_PITCH = (os.getenv("VOICE_PITCH") or "+0Hz").strip()
BACKGROUND_MUSIC_PATH = (os.getenv("BACKGROUND_MUSIC_PATH") or "").strip()
IMAGE_PROVIDER = (os.getenv("IMAGE_PROVIDER") or "cards").strip().lower()
CLOUDFLARE_ACCOUNT_ID = (os.getenv("CLOUDFLARE_ACCOUNT_ID") or "").strip()
CLOUDFLARE_API_TOKEN = (os.getenv("CLOUDFLARE_API_TOKEN") or "").strip()
VIDEO_PROVIDER = {"huggingface": "hf", "fal.ai": "fal", "": "none"}.get((os.getenv("VIDEO_PROVIDER") or "none").strip().strip("'\"").lower(), (os.getenv("VIDEO_PROVIDER") or "none").strip().strip("'\"").lower())
FAL_KEY = (os.getenv("FAL_KEY") or "").strip()
FAL_VIDEO_MODEL = (os.getenv("FAL_VIDEO_MODEL") or "fal-ai/kling-video/v2.1/standard/image-to-video").strip().strip("/")
HF_SPACE = (os.getenv("HF_SPACE") or "Lightricks/ltx-video-distilled").strip()
HF_API_NAME = (os.getenv("HF_API_NAME") or "/image_to_video").strip()
HF_TOKEN = (os.getenv("HF_TOKEN") or "").strip()
RENDER_RETRY_AFTER = timedelta(minutes=10)
TELEGRAM_VIDEO_LIMIT = 49_000_000
TELEGRAM_VIDEO_TARGET = 42_000_000

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
    if not value or not secrets.compare_digest(value, APP_API_KEY):
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


async def write_story(topic: str, language: str, model: str, avoid_titles=()):
    language_name = "Arabic" if language == "ar" else "English"
    min_words, max_words = 300, 360
    avoid = f" Do not reuse the plots or titles of these earlier episodes: {'; '.join(avoid_titles)}." if avoid_titles else ""
    prompt = (
        f"Write an original gentle cartoon episode for children ages 4-8 in {language_name}. Theme: {topic}.{avoid} "
        f"The combined spoken narration must contain {min_words}-{max_words} words so the finished narrated video lasts about 2-3 minutes. "
        "Use original characters and never copy copyrighted franchises. No frightening scenes, violence, or unsafe instructions. "
        "Keep character names and events aligned between language versions. "
        "Return JSON with: title, description, moral, art_style (one English sentence describing a single consistent visual style), "
        "characters (1-3 objects with name and appearance; appearance is a detailed English description of face, colors and clothing), "
        "and scenes: exactly 6 objects, each having visual_description, motion (one English sentence describing the characters' movement), narration, sound_effect. "
        "Write approximately 55 spoken words in each scene's narration; do not count other fields. "
        "Write each visual_description in English as a self-contained illustration prompt that names the characters present, the place and the action. "
        f"Narration must be in {language_name}, with a beginning, middle, happy ending, and positive lesson."
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
        ][:8] if isinstance(scenes, list) else []
        narration = " ".join(str(scene.get("narration", "")) for scene in usable_scenes)
        word_count = len(re.findall(r"\S+", narration))
        counts.append(word_count)
        if min_words <= word_count <= max_words:
            return story
        adjustment = "Expand" if word_count < min_words else "Shorten"
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": content},
            {"role": "user", "content": (
                f"The usable scene narration contained {word_count} words. {adjustment} the narration "
                f"to {min_words}-{max_words} words total, aiming for about 55 words in each of 6 scenes. "
                "Keep the same characters and story, and return the complete corrected JSON."
            )},
        ]
    raise RuntimeError(
        f"AI could not create a story within {min_words}-{max_words} spoken words after 3 attempts "
        f"(actual counts: {', '.join(map(str, counts))})."
    )


def character_sheet(story) -> str:
    style = str(story.get("art_style") or "soft colorful 3D cartoon style").strip()
    people = []
    for item in story.get("characters") or []:
        if isinstance(item, dict) and item.get("name") and item.get("appearance"):
            people.append(f"{str(item['name']).strip()}: {str(item['appearance']).strip()}")
    sheet = f"Art style: {style}."
    if people:
        sheet += " Recurring characters, drawn identically in every scene: " + "; ".join(people) + "."
    return sheet[:800]


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


def check_image_provider():
    if VIDEO_PROVIDER not in ("none", "fal", "hf"):
        raise RuntimeError(f"VIDEO_PROVIDER must be none, fal or hf (got {VIDEO_PROVIDER!r}).")
    if VIDEO_PROVIDER in ("fal", "hf") and IMAGE_PROVIDER != "cloudflare":
        raise RuntimeError("VIDEO_PROVIDER=fal/hf requires IMAGE_PROVIDER=cloudflare.")
    if VIDEO_PROVIDER == "fal" and not FAL_KEY:
        raise RuntimeError("VIDEO_PROVIDER=fal requires FAL_KEY.")
    if IMAGE_PROVIDER not in ("cards", "cloudflare"):
        raise RuntimeError("IMAGE_PROVIDER must be cards or cloudflare.")
    if IMAGE_PROVIDER == "cloudflare" and not (
        re.fullmatch(r"[a-fA-F0-9]{32}", CLOUDFLARE_ACCOUNT_ID) and CLOUDFLARE_API_TOKEN
    ):
        raise RuntimeError("Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN in Railway for Cloudflare images.")


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


async def make_cloudflare_frame(scene, path):
    description = scene.get("visual_description", "").strip()
    if not description:
        raise RuntimeError("A scene is missing its visual description for image generation.")
    prompt = (
        "Original child-friendly storybook illustration, bright colors, gentle and safe, "
        "no text, no logos, no recognizable copyrighted characters. "
        + str(scene.get("character_sheet", ""))[:800] + " Scene: " + description
    )[:2048]
    body = {"prompt": prompt}
    url = f"https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run/@cf/black-forest-labs/flux-1-schnell"
    try:
        async with httpx.AsyncClient(timeout=120) as http:
            response = await http.post(url, headers={"Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}"}, json=body)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(cloudflare_error(exc.response)) from exc
    except httpx.RequestError as exc:
        raise RuntimeError("Could not reach Cloudflare image generation; try again.") from exc
    try:
        payload = response.json()
        if payload.get("success") is not True:
            raise RuntimeError(cloudflare_error(response))
        image_bytes = base64.b64decode(payload["result"]["image"], validate=True)
        if len(image_bytes) > 20 * 1024 * 1024:
            raise RuntimeError("Cloudflare returned an image that is too large.")
        with Image.open(io.BytesIO(image_bytes)) as image:
            image = ImageOps.fit(image.convert("RGB"), (720, 1280), method=Image.Resampling.LANCZOS)
    except (ValueError, KeyError, TypeError, binascii.Error, UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise RuntimeError("Cloudflare returned an invalid image response.") from exc
    temporary = path.with_suffix(".tmp")
    try:
        image.save(temporary, format="PNG")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


async def make_fal_motion(scene, image_path, path):
    """Animate a scene image into a short looping clip via fal.ai image-to-video."""
    motion = str(scene.get("motion") or scene.get("visual_description") or "").strip()
    prompt = (
        "Smooth 3D animated children's cartoon, characters move gently and happily, subtle camera motion, "
        "no text, no logos. " + str(scene.get("character_sheet", ""))[:500] + " Action: " + motion
    )[:1500]
    data_uri = "data:image/png;base64," + base64.b64encode(image_path.read_bytes()).decode()
    headers = {"Authorization": f"Key {FAL_KEY}"}
    body = {"prompt": prompt, "image_url": data_uri, "duration": "5", "aspect_ratio": "9:16"}
    try:
        async with httpx.AsyncClient(timeout=120) as http:
            submit = await http.post(f"https://queue.fal.run/{FAL_VIDEO_MODEL}", headers=headers, json=body)
            submit.raise_for_status()
            job = submit.json()
            deadline = asyncio.get_running_loop().time() + 600
            while True:
                status = (await http.get(job["status_url"], headers=headers)).json()
                if status.get("status") == "COMPLETED":
                    break
                if status.get("status") not in ("IN_QUEUE", "IN_PROGRESS"):
                    raise RuntimeError(f"fal.ai video generation failed: {str(status)[:300]}")
                if asyncio.get_running_loop().time() > deadline:
                    raise RuntimeError("fal.ai video generation timed out.")
                await asyncio.sleep(5)
            result = await http.get(job["response_url"], headers=headers)
            result.raise_for_status()
            video = await http.get(result.json()["video"]["url"])
            video.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"fal.ai request failed (HTTP {exc.response.status_code}): {exc.response.text[:300]}") from exc
    except httpx.RequestError as exc:
        raise RuntimeError("Could not reach fal.ai; try again.") from exc
    except (KeyError, ValueError, TypeError) as exc:
        raise RuntimeError("fal.ai returned an unexpected response.") from exc
    await finalize_motion(video.content, path)


async def finalize_motion(raw_bytes, path):
    raw = path.with_suffix(".raw.mp4")
    raw.write_bytes(raw_bytes)
    # Play forward then backward so a 5s clip can loop under longer narration without a visible jump.
    result = await asyncio.to_thread(
        subprocess.run,
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw), "-filter_complex",
         "[0:v]scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,fps=24,split[a][b];[b]reverse[r];[a][r]concat=n=2:v=1:a=0[v]",
         "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)],
        capture_output=True, text=True, timeout=240,
    )
    raw.unlink(missing_ok=True)
    if result.returncode:
        raise RuntimeError(f"Could not process fal.ai clip: {result.stderr[-300:]}")


async def make_hf_motion(scene, image_path, path):
    """Free image-to-video through a public Hugging Face Space (slow, queued, may fail)."""
    from gradio_client import Client, handle_file

    prompt = ("Smooth 3D animated children's cartoon, gentle character movement. " + str(scene.get("motion") or scene.get("visual_description") or ""))[:500]

    def run():
        client = Client(HF_SPACE, hf_token=HF_TOKEN or None)
        return client.predict(
            prompt=prompt, negative_prompt="worst quality, distorted, text, watermark",
            input_image_filepath=handle_file(str(image_path)), input_video_filepath=None,
            height_ui=704, width_ui=512, mode="image-to-video", duration_ui=2,
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
    await finalize_motion(Path(found).read_bytes(), path)


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
    caption_font = font(44)
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
        draw.rounded_rectangle((20, 0, 700, 70 * len(lines) + 39), radius=28, fill=(0, 0, 0, 150))
        for row, text in enumerate(lines):
            draw.text((360, 20 + 70 * row + 35), shape_text(text, language), font=caption_font, fill=(255, 255, 255, 255), anchor="mm")
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
    frame = job_dir / (f"artwork-{index}.png" if IMAGE_PROVIDER == "cloudflare" else f"scene-{index}.png")
    audio = job_dir / f"voice-{index}.mp3"
    motion = job_dir / f"motion-{index}.mp4"

    async def prepare_image():
        if IMAGE_PROVIDER == "cloudflare":
            if not frame.exists():
                await make_cloudflare_frame(scene, frame)
            if VIDEO_PROVIDER == "fal" and not motion.exists():
                await make_fal_motion(scene, frame, motion)
            elif VIDEO_PROVIDER == "hf" and not motion.exists():
                try:
                    await make_hf_motion(scene, frame, motion)
                except Exception as exc:
                    # Free Spaces are unreliable: fall back to the zoom animation for this scene.
                    print(f"HF motion failed for scene {index+1}, using zoom: {exc}", flush=True)
        else:
            await asyncio.to_thread(make_frame, scene, frame, index, total, language)

    async def prepare_voice():
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


async def render_video(episode_id: str):
    with db() as con:
        row = con.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not row:
        raise RuntimeError("Episode not found")
    episode = dict(row)
    scenes = json.loads(episode["scenes"])
    check_image_provider()
    if not subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode == 0:
        raise RuntimeError("FFmpeg is missing")
    job_dir = WORK_DIR / episode_id
    job_dir.mkdir(parents=True, exist_ok=True)
    voice = VOICE_AR if episode["language"] == "ar" else VOICE_EN
    music = Path(BACKGROUND_MUSIC_PATH) if BACKGROUND_MUSIC_PATH else None
    if music and not music.is_file():
        music = None
    if music is None:
        music = ensure_lullaby(DATA_DIR / "lullaby.wav")
    async def prepare(index, scene):
        return await prepare_scene(scene, index, len(scenes), episode["language"], voice, job_dir)

    async def render(index, scene, prepared):
        frame, audio = prepared
        clip = job_dir / f"clip-{index}.mp4"
        with db() as con:
            con.execute("UPDATE episodes SET render_progress=?,updated_at=? WHERE id=?", (int(index*90/len(scenes)), utc_now().isoformat(), episode_id))
        use_motion = VIDEO_PROVIDER in ("fal", "hf") and (job_dir / f"motion-{index}.mp4").exists()
        if use_motion:
            command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats", "-y", "-stream_loop", "-1", "-i", str(job_dir / f"motion-{index}.mp4"), "-i", str(audio)]
        else:
            command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats", "-y", "-loop", "1", "-framerate", "24", "-i", str(frame), "-i", str(audio)]
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
            if use_motion:
                filters = [animated_video_filter(index, sparkle_input, 4, captions, motion=True)]
            else:
                filters = [animated_video_filter(index, sparkle_input, 4, captions)]
            video_map = "[vout]"
        else:
            filters = ["[0:v]scale=720:1280,format=yuv420p[vout]"]
            video_map = "[vout]"
        if music:
            filters.append("[2:a]volume=0.12[bg];[1:a][bg]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]")
            audio_map = "[aout]"
        else:
            audio_map = "1:a"
        command += ["-filter_complex", ";".join(filters), "-map", video_map, "-map", audio_map,
                    "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(clip)]
        result = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, timeout=240)
        if result.returncode:
            raise RuntimeError(ffmpeg_scene_error(result, index, len(scenes)))
        with db() as con:
            con.execute("UPDATE episodes SET render_progress=?,updated_at=? WHERE id=?", (int((index+1)*90/len(scenes)), utc_now().isoformat(), episode_id))
        return clip

    clips = await pipeline_scenes(scenes, prepare, render)
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
    episode_id = None
    with db() as con:
        existing = con.execute("SELECT * FROM episodes WHERE episode_date=? AND language=?", (run_key, language)).fetchone()
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
            topic = pick_topic(run_date)
            try:
                story = await asyncio.wait_for(write_story(topic, language, model, recent_titles()), timeout=300)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("Timed out while generating the story from FastLLM.") from exc
            sheet = character_sheet(story)
            raw_scenes = story.get("scenes")
            if not isinstance(raw_scenes, list):
                raise RuntimeError("The AI did not return scenes")
            scenes = [{"visual_description": str(scene.get("visual_description", "A friendly sea adventure")), "narration": str(scene.get("narration", "")).strip(), "sound_effect": str(scene.get("sound_effect", "gentle ocean waves")), "motion": str(scene.get("motion", "")).strip(), "character_sheet": sheet} for scene in raw_scenes if isinstance(scene, dict) and str(scene.get("narration", "")).strip()][:8]
            if not scenes:
                raise RuntimeError("The AI returned empty narration")
            episode_id = str(uuid.uuid4())
            timestamp = utc_now().isoformat()
            with db() as con:
                con.execute("INSERT INTO episodes(id,episode_date,language,topic,title,description,script,scenes,status,created_at,updated_at,model) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (episode_id, run_key, language, topic, str(story.get("title") or topic)[:180], str(story.get("description") or "An original Kids Haven adventure")[:3000], "\n\n".join(scene["narration"] for scene in scenes), json.dumps(scenes, ensure_ascii=False), "rendering", timestamp, timestamp, model))
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
        return
    with db() as con:
        row = con.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not row or not row["video_path"]:
        return
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
    except Exception as exc:
        await telegram_app.bot.send_message(chat_id=int(ADMIN_TELEGRAM_ID), text=f"جهزت الحلقة لكن تعذر إرسال الفيديو إلى Telegram: {str(exc)[:500]}\nلإعادة المحاولة: /review {episode_id}")


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


async def publish_episode(episode_id: str):
    youtube, _ = youtube_services()
    with db() as con:
        row = con.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not row or not row["video_path"] or not Path(row["video_path"]).is_file():
        raise RuntimeError("Episode video file is missing")
    snippet = {"title": row["title"][:100], "description": (row["description"] + "\n\n#KidsHaven #KidsStories").strip()[:5000], "tags": ["Kids Haven", "children stories", "kids cartoon", "bedtime stories"], "categoryId": "1"}
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
    await update.message.reply_text(
        "أهلًا بك في Kids Haven Studio.\n/connect — ربط قناة YouTube\n/today — إنشاء النسختين الآن\n/status — حالة الخدمة\n/stats — إحصاءات YouTube\n\nسيتم إنشاء فيديو عربي وآخر إنجليزي يوميًا. كل فيديو يحتاج موافقتك قبل النشر العام.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎬 إنشاء حلقتَي اليوم", callback_data="generate:today")]]),
    )


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


async def run_today(report):
    if generation_lock.locked():
        await report("هناك عملية إنشاء جارية الآن. انتظر انتهاءها.")
        return False
    async with generation_lock:
        today = utc_now().date()
        for language in ("ar", "en"):
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
    await message.reply_text("بدأ إنشاء حلقتين. سأرسل كل فيديو هنا للمراجعة قبل النشر.")
    if await run_today(message.reply_text):
        await message.reply_text("انتهت محاولة إنشاء حلقتَي اليوم. أرسل /status لمعرفة النتيجة.")


async def tg_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        await update.message.reply_text("غير مصرح.")
        return
    with db() as con:
        rows = con.execute("SELECT episode_date,language,status,title,youtube_url,render_progress,render_error FROM episodes ORDER BY created_at DESC LIMIT 10").fetchall()
        runs = con.execute(
            "SELECT language,status,error FROM daily_runs WHERE run_date=? ORDER BY language",
            (utc_now().date().isoformat(),),
        ).fetchall()
    lines = [
        f"{'🇸🇦' if r['language']=='ar' else '🇬🇧'} {r['episode_date']} · {r['status']}"
        + (f" ({r['render_progress']}%)" if r['status'] == "rendering" else "")
        + f" · {r['title']}"
        + (f"\nالخطأ: {r['render_error'][:250]}" if r['status'] == "failed" and r['render_error'] else "")
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


def register_telegram_handlers(application):
    application.add_handler(CommandHandler("start", tg_start))
    application.add_handler(CommandHandler("connect", tg_connect))
    application.add_handler(CommandHandler("today", tg_today, block=False))
    application.add_handler(CommandHandler("review", tg_review, block=False))
    application.add_handler(CommandHandler("status", tg_status))
    application.add_handler(CommandHandler("stats", tg_stats))
    application.add_handler(CallbackQueryHandler(tg_callback, pattern=r"^generate:today$", block=False))
    application.add_handler(CallbackQueryHandler(tg_callback))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app, scheduler_task
    if TELEGRAM_BOT_TOKEN:
        telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        register_telegram_handlers(telegram_app)
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
            "daily_ar_time": DAILY_AR_TIME,
            "daily_en_time": DAILY_EN_TIME,
            "fastllm": bool(client),
            "telegram": bool(TELEGRAM_BOT_TOKEN),
            "admin_id": bool(ADMIN_TELEGRAM_ID),
            "youtube_connected": bool(load_google_credentials()),
            "cloudflare": bool(CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN),
            "scheduler_running": bool(scheduler_task and not scheduler_task.done()),
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
<div id='main' hidden><div class='card'><button id='gen' onclick='gen()'>🎬 توليد حلقتَي اليوم الآن</button> <span id='genmsg'></span></div><div class='card'><b>الإعدادات</b><div class='grid' id='settings'></div></div>
<div class='card'><b>الحلقات</b><div class='grid' id='counts'></div></div>
<div class='card'><b>آخر الحلقات</b><table id='eps'></table></div>
<div class='card'><b>التشغيل اليومي</b><table id='runs'></table></div></div>
<script>
const $=id=>document.getElementById(id);
async function gen(){const r=await fetch('/studio/generate',{method:'POST',headers:{'x-api-key':sessionStorage.getItem('k')||''}});const j=await r.json();$('genmsg').textContent=j.started?'بدأ التوليد، تابع التقدم أدناه':'هناك عملية جارية';load()}
function save(){sessionStorage.setItem('k',$('key').value);load()}
function el(t,text,cls){const e=document.createElement(t);e.textContent=text;if(cls)e.className=cls;return e}
async function api(p){const r=await fetch(p,{headers:{'x-api-key':sessionStorage.getItem('k')||''}});if(!r.ok)throw new Error(r.status);return r.json()}
function rows(t,head,data){t.replaceChildren();const h=document.createElement('tr');head.forEach(x=>h.append(el('th',x)));t.append(h);data.forEach(r=>{const tr=document.createElement('tr');r.forEach(c=>{const td=document.createElement('td');if(c instanceof Node)td.append(c);else td.textContent=c??'';tr.append(td)});t.append(tr)})}
async function load(){try{const [s,e]=await Promise.all([api('/studio/status'),api('/studio/episodes')]);$('login').hidden=true;$('main').hidden=false;$('gen').disabled=s.generating;
const L={fastllm:'نموذج النص',telegram:'Telegram',admin_id:'معرّف الأدمن',youtube_connected:'YouTube',cloudflare:'Cloudflare',scheduler_running:'الجدولة'};
$('settings').replaceChildren(...Object.entries(L).map(([k,n])=>el('div',(s.settings[k]?'✅ ':'❌ ')+n,s.settings[k]?'ok':'bad')),el('div','الصور: '+s.settings.image_provider),el('div','عربي '+s.settings.daily_ar_time+' / EN '+s.settings.daily_en_time+' UTC'));
$('counts').replaceChildren(...Object.entries(s.counts).map(([k,v])=>el('div',k+': '+v)));
rows($('eps'),['التاريخ','اللغة','العنوان','الحالة','التقدم'],e.episodes.slice(0,20).map(x=>{const b=document.createElement('div');b.className='bar';const i=document.createElement('div');i.style.width=(x.render_progress||0)+'%';b.append(i);const st=document.createElement('div');st.append(el('span',x.status));if(x.render_error)st.append(el('div',x.render_error.slice(0,200),'bad'));return[x.episode_date,x.language,x.title,st,b]}));
rows($('runs'),['التاريخ','اللغة','الحالة','خطأ'],s.daily_runs.map(r=>[r.run_date,r.language,r.status,r.error]))}catch(x){$('login').hidden=false;$('main').hidden=true;$('err').textContent=sessionStorage.getItem('k')?'مفتاح خاطئ أو APP_API_KEY غير مضبوط':''}}
if(sessionStorage.getItem('k'))load();setInterval(()=>{if(!$('main').hidden)load()},10000);
</script></html>"""


@app.get("/dashboard", include_in_schema=False)
async def dashboard():
    return HTMLResponse(DASHBOARD_HTML)
