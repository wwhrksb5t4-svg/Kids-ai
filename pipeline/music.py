"""Choose licensed library music or generate a local instrumental track."""

import asyncio
import logging
import os
import random
import wave
from pathlib import Path


logger = logging.getLogger(__name__)
MUSIC_PROMPT = "gentle playful toddler nursery rhyme, xylophone, ukulele, soft drums, 100 bpm, no vocals"
MUSIC_MODEL = "facebook/musicgen-small"
_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}


def choose_library_track(directory: Path) -> Path:
    directory = Path(directory)
    if not directory.is_dir():
        raise RuntimeError(f"Music library does not exist: {directory}")
    tracks = sorted(path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in _EXTENSIONS)
    if not tracks:
        raise RuntimeError(f"No licensed music tracks found in {directory}.")
    return random.choice(tracks)


def _generate_local(destination: Path) -> None:
    try:
        import torch
        from transformers import AutoProcessor, MusicgenForConditionalGeneration
    except ImportError as exc:
        raise RuntimeError("Local MusicGen requires torch, transformers and accelerate.") from exc
    device = "cuda" if torch.cuda.is_available() else "cpu"
    processor = AutoProcessor.from_pretrained(MUSIC_MODEL)
    model = MusicgenForConditionalGeneration.from_pretrained(MUSIC_MODEL).to(device)
    inputs = processor(text=[MUSIC_PROMPT], padding=True, return_tensors="pt").to(device)
    with torch.inference_mode():
        audio = model.generate(**inputs, do_sample=True, guidance_scale=3, max_new_tokens=1250)
    samples = audio[0, 0].detach().cpu().clamp(-1, 1)
    frames = (samples * 32767).to(torch.int16).numpy().astype("<i2").tobytes()
    if not frames:
        raise RuntimeError("MusicGen returned empty audio.")
    with wave.open(str(destination), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(model.config.audio_encoder.sampling_rate)
        output.writeframes(frames)


async def get_music(mode: str, library: Path, job_dir: Path) -> Path:
    if mode not in ("library", "generate", "auto"):
        raise ValueError("Music mode must be library, generate, or auto.")
    if mode == "library":
        return choose_library_track(library)
    if os.getenv("MUSICGEN_NONCOMMERCIAL_CONFIRMED") != "1":
        if mode == "auto":
            logger.warning("MusicGen is restricted to noncommercial use; using the music library.")
            return choose_library_track(library)
        raise RuntimeError(
            "MusicGen weights are CC-BY-NC-4.0. Confirm noncommercial use with "
            "MUSICGEN_NONCOMMERCIAL_CONFIRMED=1, or use licensed library music."
        )
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    output = job_dir / "generated-music.wav"
    if output.is_file() and output.stat().st_size > 44:
        return output
    last_error = None
    for attempt in range(1, 4):
        temporary = job_dir / "generated-music.tmp.wav"
        try:
            await asyncio.to_thread(_generate_local, temporary)
            temporary.replace(output)
            return output
        except Exception as exc:
            last_error = exc
            temporary.unlink(missing_ok=True)
            logger.warning("Music generation attempt %d/3 failed (%s).", attempt, type(exc).__name__)
    if mode == "auto":
        logger.warning("Local MusicGen failed; trying the licensed library.")
        return choose_library_track(library)
    raise RuntimeError("Local music generation failed after three attempts.") from last_error
