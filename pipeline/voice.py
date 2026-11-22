"""Generate spoken scene lyrics with character-specific Edge TTS settings."""

import asyncio
import logging
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path

import edge_tts

from pipeline.characters import DEFAULT_LIBRARY, get_character


logger = logging.getLogger(__name__)
_ARABIC = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]")


def _segments(lyric: str, language: str) -> list[tuple[str, str]]:
    if language in ("en", "ar"):
        return [(language, lyric.strip())]
    segments: list[tuple[str, str]] = []
    for word in lyric.split():
        word_language = "ar" if _ARABIC.search(word) else "en"
        if segments and segments[-1][0] == word_language:
            previous_language, previous_text = segments[-1]
            segments[-1] = (previous_language, f"{previous_text} {word}")
        else:
            segments.append((word_language, word))
    return segments


def _check_audio(path: Path) -> None:
    if not path.is_file() or not path.stat().st_size:
        raise RuntimeError("The voice provider returned an empty audio file.")
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Could not run ffprobe to validate scene audio.") from exc
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("Could not read the generated audio duration.") from exc
    if result.returncode or not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("The generated audio is invalid.")


def _join_segments(paths: list[Path], destination: Path) -> None:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for path in paths:
        command.extend(["-i", str(path)])
    inputs = "".join(f"[{index}:a]" for index in range(len(paths)))
    command.extend([
        "-filter_complex", f"{inputs}concat=n={len(paths)}:v=0:a=1[a]",
        "-map", "[a]", "-c:a", "libmp3lame", "-b:a", "128k", str(destination),
    ])
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Could not run ffmpeg to join scene audio.") from exc
    if result.returncode:
        raise RuntimeError(f"Could not join scene audio: {result.stderr[-300:]}")


async def generate_voice(
    scene: dict,
    language: str,
    destination: Path,
    *,
    character_path: Path = DEFAULT_LIBRARY,
) -> Path:
    """Speak a scene's lyric using its speaker or first listed character."""
    if language not in ("en", "ar", "both"):
        raise ValueError("Language must be en, ar, or both.")
    lyric = scene.get("lyric")
    if not isinstance(lyric, str) or not lyric.strip():
        raise ValueError("Scene lyric must not be empty.")
    character_ids = scene.get("character_ids")
    if not isinstance(character_ids, list) or not character_ids or not all(
        isinstance(character_id, str) and character_id for character_id in character_ids
    ):
        raise ValueError("Scene needs at least one character ID.")
    speaker_id = scene.get("speaker_id") or character_ids[0]
    if speaker_id not in character_ids:
        raise ValueError("Scene speaker must be one of its characters.")
    character = get_character(speaker_id, path=character_path)
    segments = _segments(lyric, language)
    arabic_voice = character.get("voice_ar") or os.getenv("VOICE_SONG_AR", "ar-SA-ZariyahNeural").strip()
    if not arabic_voice:
        raise ValueError("Arabic voice is not configured.")

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error = None
    for attempt in range(1, 4):
        try:
            with tempfile.TemporaryDirectory(prefix=".voice-", dir=destination.parent) as directory:
                folder = Path(directory)
                parts = []
                for index, (segment_language, text) in enumerate(segments):
                    voice = arabic_voice if segment_language == "ar" else character["voice"]
                    path = folder / f"part-{index}.mp3"
                    await asyncio.wait_for(
                        edge_tts.Communicate(
                            text, voice, rate=character["rate"], pitch=character["pitch"]
                        ).save(str(path)),
                        timeout=120,
                    )
                    await asyncio.to_thread(_check_audio, path)
                    parts.append(path)
                finished = folder / "scene.mp3"
                if len(parts) == 1:
                    parts[0].replace(finished)
                else:
                    await asyncio.to_thread(_join_segments, parts, finished)
                    await asyncio.to_thread(_check_audio, finished)
                os.replace(finished, destination)
            return destination
        except Exception as exc:
            last_error = exc
            logger.warning("Voice generation attempt %d/3 failed (%s).", attempt, type(exc).__name__)
            if attempt < 3:
                await asyncio.sleep(attempt)
    raise RuntimeError("Scene voice generation failed after three attempts.") from last_error
