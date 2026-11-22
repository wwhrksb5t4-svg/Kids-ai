"""Character library and consistent prompts for the nursery-rhyme pipeline."""

import json
import logging
from pathlib import Path
from typing import Awaitable, Callable, TypedDict


STYLE = (
    "cute colorful 3D cartoon for toddlers, soft studio lighting, big round eyes, "
    "smooth rounded shapes, pastel background, high quality, 16:9"
)
NEGATIVE_PROMPT = "text, watermark, logo, scary, realistic, extra limbs, deformed, blurry"
DEFAULT_LIBRARY = Path(__file__).resolve().parents[1] / "assets" / "characters" / "characters.json"
logger = logging.getLogger(__name__)


class Character(TypedDict):
    id: str
    name: str
    gender: str
    species: str
    age_role: str
    fixed_visual_description: str
    personality: str
    voice: str
    rate: str
    pitch: str
    seed: int


async_text_generator = Callable[[str], Awaitable[str]]
_TEXT_FIELDS = (
    "id", "name", "gender", "species", "age_role", "fixed_visual_description",
    "personality", "voice", "rate", "pitch",
)


def load_characters(path: Path = DEFAULT_LIBRARY) -> list[Character]:
    path = Path(path)
    try:
        with path.open(encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot load character library: {path}") from exc
    if not isinstance(data, list):
        raise ValueError("Character library must be a JSON array.")

    characters: list[Character] = []
    seen_ids: set[str] = set()
    legacy_genders = {
        character["id"]: character["gender"]
        for character in json.loads(DEFAULT_LIBRARY.read_text(encoding="utf-8"))
    }
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("Each character must be a JSON object.")
        if not item.get("gender") and item.get("id") in legacy_genders:
            item["gender"] = legacy_genders[item["id"]]
        if any(not isinstance(item.get(key), str) or not item[key].strip() for key in _TEXT_FIELDS):
            raise ValueError("A character has a missing or invalid text field.")
        if item["gender"] not in ("female", "male"):
            raise ValueError(f"Invalid gender for character {item['id']}.")
        if item["id"] != item["id"].strip() or item["id"] in seen_ids:
            raise ValueError("Character IDs must be unique and have no surrounding whitespace.")
        if type(item.get("seed")) is not int or not 0 <= item["seed"] <= 2**32 - 1:
            raise ValueError(f"Invalid seed for character {item['id']}.")
        seen_ids.add(item["id"])
        characters.append(item)
    return characters


def get_character(character_id: str, *, path: Path = DEFAULT_LIBRARY) -> Character:
    for character in load_characters(path):
        if character["id"] == character_id:
            return character
    raise ValueError(f"Unknown character ID: {character_id}")


async def pick_characters_for_topic(
    topic: str,
    n: int,
    *,
    llm: async_text_generator,
    path: Path = DEFAULT_LIBRARY,
) -> list[Character]:
    if not isinstance(topic, str) or not topic.strip():
        raise ValueError("Topic must not be empty.")
    characters = load_characters(path)
    if type(n) is not int or not 1 <= n <= len(characters):
        raise ValueError("Character count must be between 1 and the library size.")

    choices = [
        {key: character[key] for key in ("id", "name", "gender", "species", "personality")}
        for character in characters
    ]
    prompt = (
        f"Choose exactly {n} distinct characters for this toddler-friendly topic: {topic.strip()}\n"
        f"Characters: {json.dumps(choices, ensure_ascii=False)}\n"
        "Return only a JSON array of character IDs, with no other text."
    )
    by_id = {character["id"]: character for character in characters}
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            selected = json.loads(await llm(prompt))
            if (
                not isinstance(selected, list)
                or len(selected) != n
                or any(not isinstance(item, str) or item not in by_id for item in selected)
                or len(set(selected)) != n
            ):
                raise ValueError("Model returned invalid character IDs.")
            return [by_id[character_id] for character_id in selected]
        except Exception as exc:
            last_error = exc
            logger.warning("Character selection attempt %d/3 failed (%s).", attempt, type(exc).__name__)
    raise RuntimeError("Character selection failed after three attempts.") from last_error


def build_image_prompt(
    scene_action: str,
    character_ids: list[str],
    *,
    path: Path = DEFAULT_LIBRARY,
    style: str = STYLE,
) -> str:
    if not isinstance(scene_action, str) or not scene_action.strip():
        raise ValueError("Scene action must not be empty.")
    if not character_ids or len(set(character_ids)) != len(character_ids):
        raise ValueError("Provide at least one distinct character ID.")
    characters = {character["id"]: character for character in load_characters(path)}
    unknown = [character_id for character_id in character_ids if character_id not in characters]
    if unknown:
        raise ValueError(f"Unknown character ID: {unknown[0]}")
    descriptions = "\n".join(
        f"{characters[character_id]['name']} ({characters[character_id]['gender']}): "
        f"{characters[character_id]['fixed_visual_description']}"
        for character_id in character_ids
    )
    return (
        f"{style.strip()}.\nCharacters (copy each description verbatim):\n{descriptions}\n"
        f"Scene: {scene_action.strip()}\nNegative prompt: {NEGATIVE_PROMPT}"
    )


def get_seed(character_id: str, *, path: Path = DEFAULT_LIBRARY) -> int:
    return get_character(character_id, path=path)["seed"]
