"""Publish automatic jobs with durable upload tracking and a daily limit."""

import asyncio
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from googleapiclient.http import MediaFileUpload


def _connection(db_path: Path):
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_uploads(db_path: Path) -> None:
    with _connection(db_path) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS auto_uploads (
            job_id TEXT PRIMARY KEY, upload_day TEXT, youtube_id TEXT,
            photo_id INTEGER, video_id INTEGER, state TEXT NOT NULL DEFAULT 'pending'
        )""")


def _row(db_path: Path, job_id: str):
    with _connection(db_path) as con:
        con.execute("INSERT OR IGNORE INTO auto_uploads(job_id) VALUES(?)", (job_id,))
        return con.execute("SELECT * FROM auto_uploads WHERE job_id=?", (job_id,)).fetchone()


def _reserve_upload(db_path: Path, job_id: str, max_uploads: int) -> None:
    day = datetime.now(timezone.utc).date().isoformat()
    with _connection(db_path) as con:
        con.execute("BEGIN IMMEDIATE")
        count = con.execute("SELECT COUNT(*) FROM auto_uploads WHERE upload_day=?", (day,)).fetchone()[0]
        if count >= max_uploads:
            raise RuntimeError("The daily automatic YouTube upload limit has been reached.")
        changed = con.execute(
            "UPDATE auto_uploads SET upload_day=?,state='uploading' WHERE job_id=? AND youtube_id IS NULL AND state='pending'",
            (day, job_id),
        ).rowcount
        if not changed:
            raise RuntimeError("Upload state is uncertain or already in progress; inspect it before retrying.")


async def publish(
    job_id: str,
    script: dict,
    video: Path,
    thumbnail: Path,
    *,
    bot,
    channel_id: str,
    youtube,
    db_path: Path,
    max_uploads: int,
    compress_for_telegram,
    telegram_limit: int = 49_000_000,
) -> str:
    if not channel_id or max_uploads < 1:
        raise ValueError("CHANNEL_ID and a positive MAX_UPLOADS_PER_DAY are required.")
    video, thumbnail = Path(video), Path(thumbnail)
    if not video.is_file() or not thumbnail.is_file():
        raise RuntimeError("Video or thumbnail is missing.")
    init_uploads(db_path)
    row = _row(db_path, job_id)
    if row["state"] not in ("pending", "complete"):
        raise RuntimeError(f"Publication has an uncertain state ({row['state']}); inspect it before retrying.")

    if row["photo_id"] is None:
        with _connection(db_path) as con:
            con.execute("UPDATE auto_uploads SET state='sending_photo' WHERE job_id=?", (job_id,))
        with thumbnail.open("rb") as source:
            photo = await bot.send_photo(chat_id=channel_id, photo=source, caption=script["title"][:1024])
        with _connection(db_path) as con:
            con.execute("UPDATE auto_uploads SET photo_id=?,state='pending' WHERE job_id=?", (photo.message_id, job_id))
        row = _row(db_path, job_id)
    if row["video_id"] is None:
        with tempfile.TemporaryDirectory(prefix="auto-telegram-") as directory:
            telegram_video = video
            if video.stat().st_size >= telegram_limit:
                telegram_video = await asyncio.to_thread(compress_for_telegram, video, Path(directory))
            with _connection(db_path) as con:
                con.execute("UPDATE auto_uploads SET state='sending_video' WHERE job_id=?", (job_id,))
            with telegram_video.open("rb") as source:
                message = await bot.send_video(
                    chat_id=channel_id, video=source, caption=script["title"][:1024],
                    supports_streaming=True, write_timeout=180,
                )
        with _connection(db_path) as con:
            con.execute("UPDATE auto_uploads SET video_id=?,state='pending' WHERE job_id=?", (message.message_id, job_id))
        row = _row(db_path, job_id)
    if row["youtube_id"] is None:
        _reserve_upload(db_path, job_id, max_uploads)
        body = {
            "snippet": {
                "title": script["title"][:100],
                "description": (script["description"] + "\n\nVisuals are AI-generated. "
                                + ("Music is AI-generated." if script.get("music_generated") else "Music is from a licensed library."))[:4900],
                "tags": script["tags"][:25], "categoryId": "1",
            },
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": True},
        }
        request = youtube.videos().insert(
            part="snippet,status", body=body,
            media_body=MediaFileUpload(str(video), mimetype="video/mp4", resumable=True, chunksize=8 * 1024 * 1024),
        )
        result = await asyncio.to_thread(request.execute)
        youtube_id = result["id"]
        with _connection(db_path) as con:
            con.execute("UPDATE auto_uploads SET youtube_id=?,state='pending' WHERE job_id=?", (youtube_id, job_id))
    else:
        youtube_id = row["youtube_id"]
    await asyncio.to_thread(
        youtube.thumbnails().set(
            videoId=youtube_id, media_body=MediaFileUpload(str(thumbnail), mimetype="image/jpeg")
        ).execute
    )
    with _connection(db_path) as con:
        con.execute("UPDATE auto_uploads SET state='complete' WHERE job_id=?", (job_id,))
    return f"https://www.youtube.com/watch?v={youtube_id}"
