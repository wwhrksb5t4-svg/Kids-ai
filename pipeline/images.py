"""Generate nursery-rhyme images locally with FLUX.1-schnell."""

import asyncio
import logging
import os
import tempfile
import threading
from pathlib import Path

from PIL import Image, ImageStat

from pipeline.characters import NEGATIVE_PROMPT


MODEL_ID = "black-forest-labs/FLUX.1-schnell"
SIZE = (1280, 720)
logger = logging.getLogger(__name__)
_pipeline = None
_pipeline_lock = threading.Lock()


def _load_pipeline():
    global _pipeline
    if _pipeline is not None:
        return _pipeline
    try:
        import torch
        from diffusers import FluxPipeline
    except ImportError as exc:
        raise RuntimeError(
            "Local image generation requires torch, diffusers, transformers, accelerate, and sentencepiece."
        ) from exc

    cuda = torch.cuda.is_available()
    dtype = torch.bfloat16 if cuda and torch.cuda.is_bf16_supported() else (
        torch.float16 if cuda else torch.float32
    )
    pipe = FluxPipeline.from_pretrained(MODEL_ID, torch_dtype=dtype)
    if cuda:
        pipe.enable_model_cpu_offload()
    else:
        pipe.to("cpu")
    _pipeline = pipe
    return pipe


def _generate_once(prompt: str, seed: int) -> Image.Image:
    with _pipeline_lock:
        pipe = _load_pipeline()
        import torch

        result = pipe(
            prompt=prompt,
            negative_prompt=NEGATIVE_PROMPT,
            true_cfg_scale=1.5,
            guidance_scale=0.0,
            num_inference_steps=4,
            max_sequence_length=256,
            width=SIZE[0],
            height=SIZE[1],
            generator=torch.Generator("cpu").manual_seed(seed),
        )
        if not result.images:
            raise RuntimeError("The local model returned no image.")
        return result.images[0]


def _save_valid_image(image: Image.Image, destination: Path) -> None:
    if not isinstance(image, Image.Image) or image.size != SIZE:
        raise ValueError(f"Image must be {SIZE[0]}x{SIZE[1]} pixels.")
    image = image.convert("RGB")
    if ImageStat.Stat(image.convert("L").resize((64, 36))).stddev[0] < 2:
        raise ValueError("The generated image appears blank.")

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


async def generate_image(prompt: str, seed: int, destination: Path) -> Path:
    """Generate an image with three attempts and deterministic seed offsets."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Image prompt must not be empty.")
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError("Seed must be an integer between 0 and 2**32 - 1.")
    destination = Path(destination)
    last_error = None
    for attempt in range(3):
        try:
            image = await asyncio.to_thread(_generate_once, prompt, (seed + attempt) % 2**32)
            await asyncio.to_thread(_save_valid_image, image, destination)
            return destination
        except (RuntimeError, ValueError, OSError) as exc:
            last_error = exc
            logger.warning("Image generation attempt %d/3 failed: %s", attempt + 1, exc)
    raise RuntimeError("Local image generation failed after three attempts.") from last_error
