"""Generate original toddler-friendly nursery-rhyme scripts."""

import asyncio
import json
import logging
import math
import os
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from pipeline.characters import DEFAULT_LIBRARY, load_characters, pick_characters_for_topic
from pipeline.guard import check_originality


logger = logging.getLogger(__name__)
TextGenerator = Callable[[str], Awaitable[str]]


async def generate_text(prompt: str) -> str:
    """Request text from Gemini, falling back to Groq if configured."""
    providers = []
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    if gemini_key:
        providers.append(("Gemini", gemini_key))
    if groq_key:
        providers.append(("Groq", groq_key))
    if not providers:
        raise RuntimeError("Set GEMINI_API_KEY or GROQ_API_KEY to generate scripts.")

    last_error = None
    async with httpx.AsyncClient(timeout=90) as client:
        for provider, key in providers:
            for attempt in range(1, 4):
                try:
                    if provider == "Gemini":
                        model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
                        response = await client.post(
                            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                            headers={"x-goog-api-key": key},
                            json={"contents": [{"parts": [{"text": prompt}]}]},
                        )
                    else:
                        model = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant").strip()
                        response = await client.post(
                            "https://api.groq.com/openai/v1/chat/completions",
                            headers={"Authorization": f"Bearer {key}"},
                            json={"model": model, "messages": [{"role": "user", "content": prompt}]},
                        )
                    response.raise_for_status()
                    body = response.json()
                    if provider == "Gemini":
                        parts = body["candidates"][0]["content"]["parts"]
                        text = "".join(part.get("text", "") for part in parts)
                    else:
                        text = body["choices"][0]["message"]["content"]
                    if not isinstance(text, str) or not text.strip():
                        raise ValueError("Provider returned empty text.")
                    return text.strip()
                except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
                    last_error = exc
                    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                    detail = f"HTTP {status}" if status is not None else type(exc).__name__
                    logger.warning("%s attempt %d/3 failed (%s).", provider, attempt, detail)
                    if attempt < 3:
                        await asyncio.sleep(attempt)
    raise RuntimeError("Configured text providers failed after three attempts each.") from last_error


def _parse_json(text: str) -> object:
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("The model did not return valid JSON.") from exc


def _validate_script(value: object, allowed_ids: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Script must be a JSON object.")
    for field in ("title", "description"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValueError(f"Script needs a non-empty {field}.")
    tags = value.get("tags")
    if not isinstance(tags, list) or not tags or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
        raise ValueError("Script needs a non-empty list of tags.")
    scenes = value.get("scenes")
    if not isinstance(scenes, list) or not 6 <= len(scenes) <= 8:
        raise ValueError("Script needs 6-8 scenes.")
    for index, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            raise ValueError(f"Scene {index} must be an object.")
        ids = scene.get("character_ids")
        if (
            not isinstance(ids, list)
            or not ids
            or any(not isinstance(item, str) or item not in allowed_ids for item in ids)
            or len(set(ids)) != len(ids)
        ):
            raise ValueError(f"Scene {index} has invalid character IDs.")
        lyric = scene.get("lyric")
        if not isinstance(lyric, str) or not 1 <= len(lyric.strip().splitlines()) <= 2:
            raise ValueError(f"Scene {index} needs a lyric of at most two lines.")
        if any(not line.strip() for line in lyric.strip().splitlines()):
            raise ValueError(f"Scene {index} has an empty lyric line.")
        visual = scene.get("visual_description")
        if not isinstance(visual, str) or not visual.strip():
            raise ValueError(f"Scene {index} needs a visual action.")
        duration = scene.get("duration")
        if type(duration) not in (int, float) or not math.isfinite(duration) or not 1 <= duration <= 60:
            raise ValueError(f"Scene {index} duration must be between 1 and 60 seconds.")
    return value


async def create_script(
    topic: str,
    language: str,
    *,
    llm: TextGenerator = generate_text,
    character_path: Path = DEFAULT_LIBRARY,
) -> dict:
    if not isinstance(topic, str) or not 3 <= len(topic.strip()) <= 500:
        raise ValueError("Topic must contain 3-500 characters.")
    if language not in ("en", "ar", "both"):
        raise ValueError("Language must be en, ar, or both.")
    check_originality(topic)
    available = load_characters(character_path)
    if not available:
        raise ValueError("Character library is empty.")
    characters = await pick_characters_for_topic(
        topic, min(2, len(available)), llm=llm, path=character_path
    )
    for character in characters:
        check_originality(json.dumps(character, ensure_ascii=False))
    allowed_ids = {character["id"] for character in characters}
    language_instruction = {
        "en": "Write lyrics, title, and description in English.",
        "ar": "Write lyrics, title, and description in Arabic.",
        "both": "Write bilingual English and Arabic lyrics, title, and description.",
    }[language]
    choices = [
        {key: character[key] for key in ("id", "name", "species", "personality")}
        for character in characters
    ]
    prompt = (
        "Write an original, safe nursery rhyme for toddlers. Never copy or imitate an existing "
        "brand, character, song, melody, or catchphrase. Use simple vocabulary, repetition, "
        "gentle rhymes and a positive theme. "
        f"{language_instruction}\n"
        f"Topic: {topic.strip()}\n"
        f"Available characters: {json.dumps(choices, ensure_ascii=False)}\n"
        "Return only a JSON object with title, description, tags (array of strings), and "
        "scenes (6-8 objects). Each scene must contain character_ids (array of available IDs), "
        "lyric (one or two short lines separated by a newline), visual_description "
        "(English setting and action only; do not describe the characters' appearance), "
        "and duration (seconds, number from 1 to 60)."
    )
    last_error = None
    for attempt in range(1, 4):
        try:
            script = _validate_script(_parse_json(await llm(prompt)), allowed_ids)
            check_originality(json.dumps(script, ensure_ascii=False))
            return script
        except ValueError as exc:
            last_error = exc
            logger.warning("Script validation attempt %d/3 failed: %s", attempt, exc)
    raise RuntimeError("The model could not produce a valid script after three attempts.") from last_error
