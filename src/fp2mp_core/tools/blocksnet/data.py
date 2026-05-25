from __future__ import annotations

import json
import numpy as np
import pandas as pd
import geopandas as gpd
from langchain_core.tools import tool
from blocksnet.enums import LandUse


def _shape(value) -> str:
    if hasattr(value, "shape"):
        return str(value.shape)
    if hasattr(value, "number_of_nodes") and hasattr(value, "number_of_edges"):
        return f"граф ({value.number_of_nodes()} узлов, {value.number_of_edges()} ребер)"
    return type(value).__name__


def _service_columns(blocks: pd.DataFrame) -> list[str]:
    return sorted(c.replace("capacity_", "", 1) for c in blocks.columns if c.startswith("capacity_"))


def _read_service_type_metadata(data_dir) -> list[dict]:
    path = data_dir / "service_type.json"
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    return data if isinstance(data, list) else []


def _parse_land_use(value):
    if isinstance(value, LandUse):
        return value
    if isinstance(value, str):
        key = value.split(".")[-1].upper()
        try:
            return LandUse[key]
        except KeyError:
            return value
    return value


def ensure_blocks(state: dict, data_dir):
    if "blocks" not in state:
        blocks = gpd.read_file(data_dir / "blocks_with_services.gpkg")
        if "land_use" in blocks.columns:
            blocks["land_use"] = blocks["land_use"].apply(_parse_land_use)
        blocks["site_area"] = blocks.geometry.area
        state["blocks"] = blocks
    return state["blocks"]


def ensure_acc_mx(state: dict, data_dir):
    if "acc_mx" not in state:
        acc_mx = pd.read_pickle(data_dir / "acc_mx.pickle")
        state["acc_mx"] = acc_mx.astype("float64")
    return state["acc_mx"]


def make_data_tools(ctx: dict) -> list:
    state = ctx["state"]
    data_dir = ctx["data_dir"]

    @tool
    def load_blocks() -> str:
        """Загружает GeoDataFrame кварталов с сервисами из data/blocks_with_services.gpkg."""
        try:
            blocks = ensure_blocks(state, data_dir)

            land_use_counts = blocks["land_use"].value_counts(dropna=False).to_dict() if "land_use" in blocks else {}
            services = _service_columns(blocks)
            lines = [
                f"Кварталы загружены: {blocks.shape[0]} строк, {blocks.shape[1]} столбцов.",
                f"CRS: {blocks.crs}",
                f"Землепользование: {{ {', '.join(f'{str(k)}: {v}' for k, v in land_use_counts.items())} }}",
            ]
            if "population" in blocks.columns:
                population = pd.to_numeric(blocks["population"], errors="coerce").fillna(0)
                lines.append(
                    f"Население - всего: {int(population.sum())}, среднее: {population.mean():.1f}, медиана: {population.median():.1f}"
                )
            lines.append(f"Типов сервисов: {len(services)}: {', '.join(services[:10])}{' ...' if len(services) > 10 else ''}")
            return "\n".join(lines)
        except Exception as exc:
            return f"Ошибка при загрузке кварталов: {exc}"

    @tool
    def load_accessibility_matrix() -> str:
        """Загружает предвычисленную матрицу доступности из data/acc_mx.pickle."""
        try:
            original = pd.read_pickle(data_dir / "acc_mx.pickle")
            original_dtype = original.dtypes.iloc[0]
            acc_mx = ensure_acc_mx(state, data_dir)
            values = acc_mx.to_numpy()
            flat = values[np.isfinite(values) & (values > 0)]
            return (
                f"Матрица доступности загружена: {acc_mx.shape[0]}x{acc_mx.shape[1]}, dtype={original_dtype}.\n"
                f"Время в пути (мин) - мин: {flat.min():.1f}, макс: {flat.max():.1f}, "
                f"среднее: {flat.mean():.1f}, медиана: {np.median(flat):.1f}."
            )
        except Exception as exc:
            return f"Ошибка при загрузке матрицы: {exc}"

    @tool
    def list_cached_data() -> str:
        """Возвращает список всех наборов данных в кэше агента с их размерами."""
        if not state:
            return "Кэш пуст. Загрузи данные с помощью load_blocks() и load_accessibility_matrix()."
        return "Данные в кэше:\n" + "\n".join(f"{key}: {_shape(value)}" for key, value in state.items())

    @tool
    def list_service_types() -> str:
        """Возвращает список типов сервисов из загруженных кварталов."""
        try:
            services = _service_columns(ensure_blocks(state, data_dir))
            return f"Доступные типы сервисов ({len(services)} шт.):\n" + ", ".join(services)
        except Exception as exc:
            return f"Ошибка: {exc}"

    @tool
    def list_key_services() -> str:
        """Возвращает доступные сервисы с нормативами demand и accessibility из service_type.json."""
        try:
            available = set(_service_columns(ensure_blocks(state, data_dir)))
            rows = []
            for item in _read_service_type_metadata(data_dir):
                name = str(item.get("name", "")).strip()
                if not name or name not in available:
                    continue
                demand = item.get("demand")
                accessibility = item.get("accessibility")
                name_ru = item.get("name_ru") or name
                rows.append((name, name_ru, demand, accessibility))
            rows.sort(key=lambda row: row[0])
            if not rows:
                return "Сервисы с нормативами не найдены. Проверь service_type.json и capacity_* в кварталах."
            lines = ["Ключевые сервисы из service_type.json (только доступные в модели):"]
            for name, name_ru, demand, accessibility in rows:
                lines.append(
                    f"- {name} ({name_ru}): demand={demand}, accessibility={accessibility} мин"
                )
            return "\n".join(lines)
        except Exception as exc:
            return f"Ошибка: {exc}"

    @tool
    def get_block_info(block_id: int) -> str:
        """Возвращает подробную информацию о квартале по индексу или колонке block_id."""
        try:
            blocks = ensure_blocks(state, data_dir)
            row = blocks[blocks.index == block_id]
            if row.empty and "block_id" in blocks.columns:
                row = blocks[blocks["block_id"] == block_id]
            if row.empty:
                return f"Квартал с ID={block_id} не найден. Диапазон индекса: {blocks.index.min()}-{blocks.index.max()}."
            series = row.iloc[0].drop(labels=["geometry"], errors="ignore")
            lines = [f"Квартал ID={block_id}:"]
            for column, value in series.items():
                if pd.notna(value) and value != 0:
                    lines.append(f"  {column}: {value}")
            return "\n".join(lines)
        except Exception as exc:
            return f"Ошибка: {exc}"

    @tool
    def get_analysis_results(result_key: str) -> str:
        """Извлекает из кэша краткую сводку ранее вычисленного результата."""
        try:
            if result_key not in state:
                return f"Ключ '{result_key}' не найден. Доступные: {list(state.keys())}"
            value = state[result_key]
            if isinstance(value, (pd.DataFrame, gpd.GeoDataFrame)):
                return (
                    f"Результат '{result_key}' ({value.shape}):\n"
                    f"Статистика:\n{value.describe().to_string()}\n\n"
                    f"Первые строки:\n{value.head(5).to_string()}"
                )
            if isinstance(value, pd.Series):
                return f"Результат '{result_key}' ({value.shape}):\n{value.describe().to_string()}\n\n{value.head(5).to_string()}"
            return f"'{result_key}': {str(value)[:500]}"
        except Exception as exc:
            return f"Ошибка: {exc}"

    return [
        load_blocks,
        load_accessibility_matrix,
        list_cached_data,
        list_service_types,
        list_key_services,
        get_block_info,
        get_analysis_results,
    ]
