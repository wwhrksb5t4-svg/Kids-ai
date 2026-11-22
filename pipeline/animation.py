"""Cloud image-to-video providers used by both production workflows."""

import asyncio
import base64
import os
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx


FAL_MODELS = {
    "kling": "fal-ai/kling-video/v3/pro/image-to-video",
    "hailuo": "fal-ai/minimax/hailuo-2.3-fast/standard/image-to-video",
    "pixverse": "fal-ai/pixverse/v6/image-to-video",
    "vidu": "fal-ai/vidu/q4/image-to-video",
    "dreamina": "bytedance/seedance-2.0/fast/image-to-video",
    "veo": "fal-ai/veo3.1/image-to-video",
}
FAL_PROVIDERS = frozenset((*FAL_MODELS, "fal"))
VIDEO_PROVIDERS = frozenset((*FAL_PROVIDERS, "runway", "hf", "none"))
_DEFAULT_COST_ESTIMATES = {
    "kling": 0.75,
    "hailuo": 0.25,
    "pixverse": 0.25,
    "vidu": 0.50,
    "dreamina": 1.25,
    "veo": 1.70,
    "fal": 1.50,
    "runway": 0.50,
}


def api_key_for(provider: str) -> str:
    if provider in FAL_PROVIDERS:
        return (os.getenv("FAL_KEY") or "").strip()
    if provider == "runway":
        return (os.getenv("RUNWAY_API_KEY") or "").strip()
    return ""


def _estimate_cost(provider: str) -> float:
    default = _DEFAULT_COST_ESTIMATES[provider]
    key = f"VIDEO_ESTIMATED_COST_{provider.upper()}_USD"
    try:
        value = float(os.getenv(key, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{key} must be a nonnegative number.") from exc
    if not 0 <= value < float("inf"):
        raise RuntimeError(f"{key} must be a finite nonnegative number.")
    return value


def _reserve_daily_slot(db_path: Path | str | None, provider: str) -> None:
    limit = int(os.getenv("VIDEO_API_MAX_SCENES_PER_DAY", "8"))
    if limit < 1:
        raise RuntimeError("VIDEO_API_MAX_SCENES_PER_DAY must be at least 1.")
    try:
        budget = float(os.getenv("VIDEO_API_DAILY_BUDGET_USD", "10"))
    except ValueError as exc:
        raise RuntimeError("VIDEO_API_DAILY_BUDGET_USD must be a positive number.") from exc
    if not 0 < budget < float("inf"):
        raise RuntimeError("VIDEO_API_DAILY_BUDGET_USD must be a finite positive number.")
    estimate = _estimate_cost(provider)
    if db_path is None:
        data_dir = Path(os.getenv("DATA_DIR", "data"))
        db_path = data_dir / "kids_haven.sqlite3"
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).date().isoformat()
    with sqlite3.connect(db_path, timeout=30) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS video_api_daily_usage "
            "(usage_date TEXT PRIMARY KEY, generations INTEGER NOT NULL, estimated_usd REAL NOT NULL DEFAULT 0)"
        )
        columns = {row[1] for row in con.execute("PRAGMA table_info(video_api_daily_usage)")}
        if "estimated_usd" not in columns:
            con.execute(
                "ALTER TABLE video_api_daily_usage ADD COLUMN estimated_usd REAL NOT NULL DEFAULT 0"
            )
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT generations,estimated_usd FROM video_api_daily_usage WHERE usage_date=?", (day,)
        ).fetchone()
        used, estimated = row if row else (0, 0.0)
        if used >= limit:
            raise RuntimeError(
                f"Daily AI video limit reached ({used}/{limit} scenes, UTC). "
                "Increase VIDEO_API_MAX_SCENES_PER_DAY only after checking provider costs."
            )
        if estimated + estimate > budget + 1e-9:
            raise RuntimeError(
                f"Estimated AI video budget reached (${estimated:.2f}/${budget:.2f} UTC today); "
                f"the next {provider} scene is estimated at ${estimate:.2f}. "
                "Check VIDEO_API_DAILY_BUDGET_USD and provider pricing before raising the limit."
            )
        con.execute(
            "INSERT INTO video_api_daily_usage(usage_date,generations,estimated_usd) VALUES(?,1,?) "
            "ON CONFLICT(usage_date) DO UPDATE SET generations=generations+1,"
            "estimated_usd=estimated_usd+excluded.estimated_usd",
            (day, estimate),
        )


def daily_usage(db_path: Path | str | None = None) -> tuple[int, int]:
    used, limit, _, _ = daily_usage_details(db_path)
    return used, limit


def daily_usage_details(db_path: Path | str | None = None) -> tuple[int, int, float, float]:
    limit = int(os.getenv("VIDEO_API_MAX_SCENES_PER_DAY", "8"))
    budget = float(os.getenv("VIDEO_API_DAILY_BUDGET_USD", "10"))
    if db_path is None:
        db_path = Path(os.getenv("DATA_DIR", "data")) / "kids_haven.sqlite3"
    path = Path(db_path)
    if not path.exists():
        return 0, limit, 0.0, budget
    with sqlite3.connect(path) as con:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='video_api_daily_usage'"
        ).fetchone()
        if not exists:
            return 0, limit, 0.0, budget
        day = datetime.now(timezone.utc).date().isoformat()
        columns = {row[1] for row in con.execute("PRAGMA table_info(video_api_daily_usage)")}
        if "estimated_usd" in columns:
            row = con.execute(
                "SELECT generations,estimated_usd FROM video_api_daily_usage WHERE usage_date=?", (day,)
            ).fetchone()
            used, estimated = row if row else (0, 0.0)
        else:
            row = con.execute(
                "SELECT generations FROM video_api_daily_usage WHERE usage_date=?", (day,)
            ).fetchone()
            used, estimated = (row[0], 0.0) if row else (0, 0.0)
    return used, limit, estimated, budget


def _fal_body(provider: str, prompt: str, image_url: str, landscape: bool) -> tuple[str, dict]:
    aspect = "16:9" if landscape else "9:16"
    if provider == "fal":
        model = (os.getenv("FAL_VIDEO_MODEL") or FAL_MODELS["kling"]).strip().strip("/")
        body = {"prompt": prompt, "image_url": image_url, "duration": "5", "aspect_ratio": aspect}
    elif provider == "kling":
        model = FAL_MODELS[provider]
        body = {"prompt": prompt, "start_image_url": image_url, "duration": "5", "aspect_ratio": aspect, "generate_audio": False}
    elif provider == "hailuo":
        model = FAL_MODELS[provider]
        body = {"prompt": prompt, "image_url": image_url, "duration": "6"}
    elif provider == "pixverse":
        model = FAL_MODELS[provider]
        body = {"prompt": prompt, "image_url": image_url, "duration": 5, "resolution": "720p", "aspect_ratio": aspect}
    elif provider == "vidu":
        model = FAL_MODELS[provider]
        body = {"prompt": prompt, "image_url": image_url, "duration": 5, "resolution": "720p"}
    elif provider == "dreamina":
        model = FAL_MODELS[provider]
        body = {"prompt": prompt, "image_url": image_url, "duration": "4", "resolution": "720p", "aspect_ratio": aspect, "generate_audio": False}
    elif provider == "veo":
        model = FAL_MODELS[provider]
        body = {"prompt": prompt, "image_url": image_url, "duration": "8s", "resolution": "720p", "aspect_ratio": aspect, "generate_audio": False}
    else:
        raise ValueError(f"Unsupported Fal provider: {provider}")
    return model, body


def _video_url(value):
    if isinstance(value, str):
        parsed = urlparse(value)
        if parsed.scheme == "https" and parsed.netloc:
            return value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in ("url", "video_url") and isinstance(item, str):
                parsed = urlparse(item)
                if parsed.scheme == "https" and parsed.netloc:
                    return item
            found = _video_url(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _video_url(item)
            if found:
                return found
    return None


async def _download_video(http: httpx.AsyncClient, url: str) -> bytes:
    response = await http.get(url)
    response.raise_for_status()
    if not response.content or len(response.content) > 500 * 1024 * 1024:
        raise RuntimeError("The video provider returned an empty or oversized clip.")
    return response.content


async def _fal_motion(provider: str, prompt: str, image_path: Path, size: tuple[int, int]) -> bytes:
    key = api_key_for(provider)
    if not key:
        raise RuntimeError("Set FAL_KEY in Railway to use this video provider.")
    image = "data:image/png;base64," + base64.b64encode(image_path.read_bytes()).decode("ascii")
    model, body = _fal_body(provider, prompt, image, size[0] > size[1])
    headers = {"Authorization": f"Key {key}"}
    async with httpx.AsyncClient(timeout=120) as http:
        try:
            submit = await http.post(f"https://queue.fal.run/{model}", headers=headers, json=body)
            submit.raise_for_status()
            task = submit.json()
            status_url, response_url = task["status_url"], task["response_url"]
            for url in (status_url, response_url):
                parsed = urlparse(url)
                if parsed.scheme != "https" or parsed.hostname not in ("queue.fal.run", "fal.run"):
                    raise RuntimeError("Fal returned an unexpected task URL.")
            deadline = asyncio.get_running_loop().time() + 900
            while True:
                status_response = await http.get(status_url, headers=headers)
                status_response.raise_for_status()
                status = status_response.json()
                if status.get("status") == "COMPLETED":
                    break
                if status.get("status") not in ("IN_QUEUE", "IN_PROGRESS"):
                    raise RuntimeError(f"fal.ai video generation failed: {str(status)[:300]}")
                if asyncio.get_running_loop().time() > deadline:
                    raise RuntimeError("fal.ai video generation timed out.")
                await asyncio.sleep(5)
            result = await http.get(response_url, headers=headers)
            result.raise_for_status()
            url = _video_url(result.json())
            if not url:
                raise RuntimeError("fal.ai completed without returning a video URL.")
            return await _download_video(http, url)
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"fal.ai request failed (HTTP {exc.response.status_code}): {exc.response.text[:300]}"
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError("Could not reach fal.ai; try again.") from exc
        except (KeyError, ValueError, TypeError) as exc:
            raise RuntimeError("fal.ai returned an unexpected response.") from exc


async def _runway_motion(prompt: str, image_path: Path, size: tuple[int, int]) -> bytes:
    key = api_key_for("runway")
    if not key:
        raise RuntimeError("Set RUNWAY_API_KEY in Railway to use Runway.")
    image_uri = "data:image/png;base64," + base64.b64encode(image_path.read_bytes()).decode("ascii")
    headers = {
        "Authorization": f"Bearer {key}",
        "X-Runway-Version": "2024-11-06",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=120) as http:
        try:
            submit = await http.post(
                "https://api.dev.runwayml.com/v1/image_to_video",
                headers=headers,
                json={
                    "model": os.getenv("RUNWAY_VIDEO_MODEL", "gen4_turbo"),
                    "promptImage": image_uri,
                    "promptText": prompt,
                    "ratio": "1280:720" if size[0] > size[1] else "720:1280",
                    "duration": 5,
                },
            )
            submit.raise_for_status()
            task_id = submit.json()["id"]
            deadline = asyncio.get_running_loop().time() + 900
            while True:
                response = await http.get(
                    f"https://api.dev.runwayml.com/v1/tasks/{task_id}", headers=headers
                )
                response.raise_for_status()
                task = response.json()
                status = task.get("status")
                if status == "SUCCEEDED":
                    url = _video_url(task.get("output"))
                    if not url:
                        raise RuntimeError("Runway completed without returning a video URL.")
                    return await _download_video(http, url)
                if status in ("FAILED", "CANCELED"):
                    raise RuntimeError(f"Runway video generation failed: {str(task)[:300]}")
                if asyncio.get_running_loop().time() > deadline:
                    raise RuntimeError("Runway video generation timed out.")
                await asyncio.sleep(5)
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"Runway request failed (HTTP {exc.response.status_code}): {exc.response.text[:300]}"
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeError("Could not reach Runway; try again.") from exc
        except (KeyError, ValueError, TypeError) as exc:
            raise RuntimeError("Runway returned an unexpected response.") from exc


def _finalize(raw: bytes, destination: Path, size: tuple[int, int]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.stem}-", suffix=".raw.mp4", dir=destination.parent, delete=False
    ) as handle:
        raw_path = Path(handle.name)
        handle.write(raw)
    output_path = destination.with_name(f".{destination.name}.tmp.mp4")
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw_path),
                "-filter_complex",
                f"[0:v]scale={size[0]}:{size[1]}:force_original_aspect_ratio=increase,"
                f"crop={size[0]}:{size[1]},fps=24,split[a][b];[b]reverse[r];"
                "[a][r]concat=n=2:v=1:a=0[v]",
                "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", str(output_path),
            ],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode:
            raise RuntimeError(f"Could not process generated video: {result.stderr[-300:]}")
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError("FFmpeg produced an empty animated clip.")
        os.replace(output_path, destination)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Could not run FFmpeg to process the generated video.") from exc
    finally:
        raw_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)


async def animate_scene(
    scene: dict, image_path: Path, destination: Path, *, provider: str,
    size: tuple[int, int] = (1920, 1080), db_path: Path | str | None = None,
) -> Path:
    """Animate a scene with one paid API request and cache the normalized result."""
    if provider not in VIDEO_PROVIDERS:
        raise ValueError(f"Unsupported video provider: {provider}")
    if provider in ("none", "hf"):
        raise ValueError(f"{provider} does not use this cloud animation adapter.")
    if destination.is_file() and destination.stat().st_size:
        return destination
    if not api_key_for(provider):
        key_name = "RUNWAY_API_KEY" if provider == "runway" else "FAL_KEY"
        raise RuntimeError(f"Set {key_name} in Railway to use {provider}.")
    prompt = (
        "Smooth colorful 3D cartoon for toddlers. Preserve the subject and outfit from the reference image. "
        "Animate only this action with gentle natural body movement and a cinematic camera move. "
        "No text, logos, or added characters. Action: "
        + str(scene.get("motion") or scene.get("visual_description") or "").strip()
    )[:1200]
    if not str(scene.get("motion") or scene.get("visual_description") or "").strip():
        raise ValueError("A scene needs a visual action before animation.")
    _reserve_daily_slot(db_path, provider)
    if provider == "runway":
        raw = await _runway_motion(prompt, Path(image_path), size)
    else:
        raw = await _fal_motion(provider, prompt, Path(image_path), size)
    await asyncio.to_thread(_finalize, raw, Path(destination), size)
    return Path(destination)
