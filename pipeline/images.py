"""Generate scene images with Cloudflare Workers AI, without local ML runtimes."""

import asyncio
import base64
import io
import logging
import os
import tempfile
from pathlib import Path

import httpx
from PIL import Image, ImageOps, ImageStat, UnidentifiedImageError


SIZE = (1280, 720)
logger = logging.getLogger(__name__)


def _validate_image(image: Image.Image) -> Image.Image:
    if image.width < 256 or image.height < 144:
        raise ValueError("The generated image is too small.")
    image = ImageOps.fit(image.convert("RGB"), SIZE, method=Image.Resampling.LANCZOS)
    if ImageStat.Stat(image.convert("L").resize((64, 36))).stddev[0] < 2:
        raise ValueError("The generated image appears blank.")
    return image


def _save_image(image: Image.Image, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.", suffix=".png", dir=destination.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            image.save(handle, format="PNG")
        if temporary.stat().st_size < 10_000:
            raise ValueError("The generated image file is too small.")
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def _generate_once(prompt: str) -> Image.Image:
    account_id = (os.getenv("CLOUDFLARE_ACCOUNT_ID") or "").strip()
    token = (os.getenv("CLOUDFLARE_API_TOKEN") or "").strip()
    if not account_id or not token:
        raise RuntimeError(
            "Automatic image generation needs CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN."
        )
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/"
        "@cf/black-forest-labs/flux-1-schnell"
    )
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                url, headers={"Authorization": f"Bearer {token}"},
                json={"prompt": prompt[:2048]},
            )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"Cloudflare image generation failed (HTTP {exc.response.status_code})."
        ) from exc
    except httpx.RequestError as exc:
        raise RuntimeError("Could not reach Cloudflare image generation.") from exc
    try:
        payload = response.json()
        if payload.get("success") is not True:
            raise ValueError("Cloudflare reported an unsuccessful image request.")
        image_bytes = base64.b64decode(payload["result"]["image"], validate=True)
        if not image_bytes or len(image_bytes) > 20 * 1024 * 1024:
            raise ValueError("Cloudflare returned an empty or oversized image.")
        with Image.open(io.BytesIO(image_bytes)) as source:
            return _validate_image(source)
    except (ValueError, KeyError, TypeError, UnidentifiedImageError, OSError) as exc:
        raise RuntimeError("Cloudflare returned an invalid image response.") from exc


async def generate_image(prompt: str, seed: int, destination: Path) -> Path:
    """Generate an image remotely with three attempts; no PyTorch is installed or required."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Image prompt must not be empty.")
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError("Seed must be an integer between 0 and 2**32 - 1.")
    destination = Path(destination)
    if destination.is_file() and destination.stat().st_size > 10_000:
        return destination
    last_error = None
    for attempt in range(3):
        try:
            image = await _generate_once(prompt)
            await asyncio.to_thread(_save_image, image, destination)
            return destination
        except (RuntimeError, ValueError, OSError) as exc:
            last_error = exc
            logger.warning("Cloud image generation attempt %d/3 failed: %s", attempt + 1, exc)
    raise RuntimeError("Cloud image generation failed after three attempts.") from last_error
