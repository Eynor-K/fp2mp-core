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
    chat_url: str = "https://routerai.ru/api/v1"
    api_key: str = ""
    tavily_api_key: str = ""
    e2b_api_key: str = ""

    max_iterations: int = 6
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
        max_iterations=int(os.getenv("MAX_ITERATIONS", "6")),
        normative_db_path=Path(norm_path_str),
        wiki_persist_dir=wiki_dir,
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
