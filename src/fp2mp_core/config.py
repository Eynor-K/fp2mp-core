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
    synthesis_refine: bool = True
    normative_db_path: Path = field(default_factory=lambda: NORMATIVE_DIR)
    model_data_dir: Path = field(default_factory=lambda: DATA_DIR / "model")
    model_output_dir: Path = field(default_factory=lambda: DATA_DIR / "model" / "output")
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
    model_data_dir = Path(os.getenv("MODEL_DATA_DIR", str(DATA_DIR / "model")))
    model_output_dir = Path(os.getenv("MODEL_OUTPUT_DIR", str(DATA_DIR / "model" / "output")))
    model_output_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        chat_url=os.getenv("FP2MP_CHAT_URL", "https://routerai.ru/api/v1"),
        api_key=api_key,
        tavily_api_key=os.getenv("TAVILY_API_KEY", ""),
        e2b_api_key=os.getenv("E2B_API_KEY", ""),
        max_iterations=int(os.getenv("MAX_ITERATIONS", "6")),
        synthesis_refine=os.getenv("SYNTHESIS_REFINE", "1").strip().lower()
        not in {"0", "false", "no", "off"},
        normative_db_path=Path(norm_path_str),
        model_data_dir=model_data_dir,
        model_output_dir=model_output_dir,
        wiki_persist_dir=wiki_dir,
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )


@lru_cache(maxsize=1)
def get_model_locality() -> dict | None:
    """Return the locality covered by the local data/model urban model."""
    settings = get_settings()
    boundary_file = os.getenv("MODEL_BOUNDARY_FILE", "boundaries.geojson")
    path = settings.model_data_dir / boundary_file
    if not path.exists():
        return None

    import geopandas as gpd

    gdf = gpd.read_file(path)
    if gdf.empty:
        return None

    row = gdf.iloc[0]
    if "addresstype" in gdf.columns:
        for candidate in ("city", "town", "village", "municipality"):
            selected = gdf[gdf["addresstype"] == candidate]
            if not selected.empty:
                row = selected.iloc[0]
                break

    name = str(row.get("name") or "").strip()
    display_name = str(row.get("display_name") or name).strip()
    return {"name": name, "display_name": display_name} if name else None
