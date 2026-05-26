"""Text normalization helpers shared by routing and retrieval code."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

_CYRILLIC_TO_LATIN = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "i",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


def transliterate_cyrillic_to_latin(text: str) -> str:
    """Return a generic Cyrillic-to-Latin transliteration without locality-specific rules."""
    result: list[str] = []
    for char in str(text or ""):
        lower = char.lower()
        replacement = _CYRILLIC_TO_LATIN.get(lower)
        if replacement is None:
            result.append(char)
        elif char.isupper():
            result.append(replacement.capitalize())
        else:
            result.append(replacement)
    return "".join(result)


def normalized_latin_tokens(text: str) -> list[str]:
    """Lowercase, transliterate Cyrillic, strip punctuation, and return alphanumeric tokens."""
    latin = transliterate_cyrillic_to_latin(text).lower()
    return re.findall(r"[a-z0-9]+", latin)


def text_matches_locality(text: str, locality_names: list[str], min_ratio: float = 0.85) -> bool:
    """Match query text to locality names across Cyrillic/Latin spelling variants."""
    query_tokens = normalized_latin_tokens(text)
    if not query_tokens:
        return False
    query_text = " ".join(query_tokens)
    query_token_set = set(query_tokens)

    locality_tokens: list[str] = []
    for name in locality_names:
        locality_tokens.extend(normalized_latin_tokens(name))

    for token in locality_tokens:
        if len(token) < 4:
            continue
        if token in query_token_set or token in query_text:
            return True
        for query_token in query_tokens:
            if len(query_token) < 4:
                continue
            if query_token in token or token in query_token:
                return True
            if SequenceMatcher(None, token, query_token).ratio() >= min_ratio:
                return True
    return False


# ---------------------------------------------------------------------------
# Shared routing intent detection (locality-agnostic).
# Used by both the orchestrator and blackboard initialization so that the
# decision "should this spatial sub-query go to BlocksNet?" is computed in one
# place. No locality names are hardcoded — the model locality always comes from
# data/model/boundaries.geojson via get_model_locality().
# ---------------------------------------------------------------------------

URBAN_METRIC_RE = re.compile(
    r"обеспеч|доступн|связност|плотност|разнообраз|центральност|сервис|населен|населени|"
    r"квартал|землепольз|освоенн|застрой|coverage|accessibility|density|diversity|centrality|service|block",
    re.IGNORECASE,
)
DEVELOPMENT_PROPOSAL_RE = re.compile(
    r"предлож|гипотез|развити|постро|размест|измен|смен|оптимиз|мастер-план|"
    r"proposal|hypothesis|develop|build|place|optimi[sz]e|land.?use",
    re.IGNORECASE,
)
_FRESH_GEODATA_TOKENS = (
    "osm", "openstreetmap", "улиц", "улицы", "улица", "дорог", "дороги",
    "здани", "маршрут", "route", "street", "building", "fresh geodata",
)


def needs_fresh_geodata(text: str) -> bool:
    """True if the task needs fresh OSM/web geodata (CodeSpatial territory)."""
    lowered = str(text or "").lower()
    return any(token in lowered for token in _FRESH_GEODATA_TOKENS)


def model_locality_names() -> list[str]:
    """Locality names of the local model, read from boundaries (no hardcoded city)."""
    try:
        from fp2mp_core.config import get_model_locality

        locality = get_model_locality()
    except Exception:
        return []
    if not locality:
        return []
    names = [str(locality.get("name", "")).strip()]
    names.extend(part.strip() for part in str(locality.get("display_name", "")).split(","))
    return [name for name in names if name]


def model_locality_matches(text: str) -> bool:
    """True if the text refers to the local model's locality (Cyrillic/Latin aware)."""
    return text_matches_locality(text, model_locality_names())


def prefer_blocksnet_routing(
    question: str,
    sub_query_text: str = "",
    modality: str = "any",
    directive_text: str = "",
) -> bool:
    """Decide whether a spatial sub-query should be handled primarily by BlocksNet.

    BlocksNet is primary by assignment for the local-model city when the intent is an
    urban-metric/development question that does not require fresh OSM/web geodata.
    CodeSpatial complements it (fresh geodata, custom scenarios, off-model places).
    Locality is matched against the original question, the sub-query, and the directive,
    because decomposed sub-queries often drop the city name.
    """
    if str(modality or "").lower() not in {"code", "any", ""}:
        return False
    names = model_locality_names()
    if not names:
        return False
    locality_hit = (
        text_matches_locality(question or "", names)
        or text_matches_locality(sub_query_text or "", names)
        or text_matches_locality(directive_text or "", names)
    )
    if not locality_hit:
        return False
    task_text = " ".join([sub_query_text or "", directive_text or ""]).strip()
    if needs_fresh_geodata(task_text):
        return False
    intent_text = " ".join([question or "", task_text]).strip()
    return bool(URBAN_METRIC_RE.search(intent_text) or DEVELOPMENT_PROPOSAL_RE.search(intent_text))
