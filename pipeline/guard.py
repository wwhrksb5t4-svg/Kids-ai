"""Screen text for references to a small set of existing children's properties."""

import difflib
import re
import unicodedata


_BLOCKED = (
    "cocomelon", "baby shark", "peppa pig", "paw patrol", "mickey mouse",
    "minnie mouse", "winnie the pooh", "sesame street", "elmo", "bluey",
    "barney the dinosaur", "little baby bum", "pinkfong", "تل تبيز",
    "بيبي شارك", "كوكوميلون", "بيبا بيغ", "ميكي ماوس",
)


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def check_originality(text: str) -> None:
    """Reject known property names and close spelling variants; not a legal guarantee."""
    normalized = _normalize(text)
    if not normalized:
        raise ValueError("Text to check must not be empty.")
    words = normalized.split()
    for name in _BLOCKED:
        target = _normalize(name)
        name_words = target.split()
        for index in range(len(words) - len(name_words) + 1):
            candidate = " ".join(words[index:index + len(name_words)])
            if candidate == target or (
                len(target) >= 7 and difflib.SequenceMatcher(None, candidate, target).ratio() >= 0.9
            ):
                raise ValueError("Content mentions or closely resembles a protected children's property.")
