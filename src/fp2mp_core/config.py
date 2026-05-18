"""Runtime configuration — loaded lazily from environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parents[2]   # fp2mp_core/
DATA_DIR = BASE_DIR / "data"
NORMATIVE_DIR = DATA_DIR / "normative"


class ConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    # --- API ---
    chat_url: str = "https://routerai.ru/api/v1"
    api_key: str = ""
    tavily_api_key: str = ""
    e2b_api_key: str = ""

    # --- Graph control ---
    max_iterations: int = 10
    max_dispatches_per_round: int = 3
    max_dispatches_ceiling: int = 6          # adaptive upper bound when many tasks are ready
    max_followup_tasks_per_round: int = 2    # agent-proposed follow-up tasks per round

    # --- Fact promotion thresholds (curator) ---
    promote_threshold: float = 0.65          # web / code / synthesis
    normative_promote_threshold: float = 0.70
    empirical_web_promote_threshold: float = 0.55  # web on empirical sub-query (partial)
    min_content_length: int = 50

    # --- Wiki maintenance ---
    prune_confidence_threshold: float = 0.35
    prune_min_iterations: int = 2
    merge_jaccard_threshold: float = 0.75
    conflict_jaccard_threshold: float = 0.25  # below this → flag CONFLICT

    # --- Paths ---
    normative_db_path: Path = field(default_factory=lambda: NORMATIVE_DIR)
    wiki_persist_dir: Path | None = None
    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    api_key = os.getenv("FP2MP_API_KEY", "")
    if not api_key:
        raise ConfigurationError(
            "FP2MP_API_KEY is not set. Copy .env.example to .env and fill in your key."
        )

    wiki_dir_str = os.getenv("WIKI_PERSIST_DIR", "")
    wiki_dir = Path(wiki_dir_str) if wiki_dir_str else None

    norm_path_str = os.getenv("NORMATIVE_DB_PATH", str(NORMATIVE_DIR))

    return Settings(
        chat_url=os.getenv("FP2MP_CHAT_URL", "https://routerai.ru/api/v1"),
        api_key=api_key,
        tavily_api_key=os.getenv("TAVILY_API_KEY", ""),
        e2b_api_key=os.getenv("E2B_API_KEY", ""),
        max_iterations=int(os.getenv("MAX_ITERATIONS", "10")),
        max_dispatches_per_round=int(os.getenv("MAX_DISPATCHES_PER_ROUND", "3")),
        max_dispatches_ceiling=int(os.getenv("MAX_DISPATCHES_CEILING", "6")),
        max_followup_tasks_per_round=int(os.getenv("MAX_FOLLOWUP_TASKS_PER_ROUND", "2")),
        promote_threshold=float(os.getenv("PROMOTE_THRESHOLD", "0.65")),
        normative_promote_threshold=float(os.getenv("NORMATIVE_PROMOTE_THRESHOLD", "0.70")),
        empirical_web_promote_threshold=float(os.getenv("EMPIRICAL_WEB_PROMOTE_THRESHOLD", "0.55")),
        min_content_length=int(os.getenv("MIN_CONTENT_LENGTH", "50")),
        prune_confidence_threshold=float(os.getenv("PRUNE_CONFIDENCE_THRESHOLD", "0.35")),
        prune_min_iterations=int(os.getenv("PRUNE_MIN_ITERATIONS", "2")),
        merge_jaccard_threshold=float(os.getenv("MERGE_JACCARD_THRESHOLD", "0.75")),
        conflict_jaccard_threshold=float(os.getenv("CONFLICT_JACCARD_THRESHOLD", "0.25")),
        normative_db_path=Path(norm_path_str),
        wiki_persist_dir=wiki_dir,
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
