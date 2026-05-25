from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from langchain_core.tools import tool

from blocksnet.enums import LandUse

from fp2mp_core.runtime import record_file
from fp2mp_core.tools.blocksnet.data import ensure_acc_mx, ensure_blocks
from fp2mp_core.tools.blocksnet.viz import save_metric_map


_SERVICE_PRESETS: dict[str, dict[str, float]] = {
    "basic": {
        "school": 1.0,
        "kindergarten": 1.0,
        "polyclinic": 0.9,
        "convenience": 0.7,
        "pharmacy": 0.7,
        "pitch": 0.5,
        "bus_stop": 0.5,
    },
    "advanced": {
        "school": 1.0,
        "kindergarten": 1.0,
        "polyclinic": 0.9,
        "convenience": 0.7,
        "pharmacy": 0.7,
        "pitch": 0.6,
        "sports_centre": 0.5,
        "extra_education": 0.5,
        "cafe": 0.4,
        "bus_stop": 0.5,
    },
    "comfort": {
        "school": 0.9,
        "kindergarten": 0.9,
        "polyclinic": 0.8,
        "convenience": 0.7,
        "pharmacy": 0.7,
        "pitch": 0.7,
        "sports_centre": 0.6,
        "extra_education": 0.6,
        "cafe": 0.5,
        "park": 0.5,
        "dog_park": 0.4,
    },
}


def make_optimize_tools(ctx: dict) -> list:
    state = ctx["state"]
    data_dir = ctx["data_dir"]
    output_dir = ctx["output_dir"]

    @tool
    def suggest_target_blocks(
        criterion: str = "low_provision",
        service_type: str = "",
        land_use: str = "",
        count: int = 10,
    ) -> str:
        """Suggest candidate block IDs for development optimization from cached metrics or model attributes."""
        try:
            blocks = ensure_blocks(state, data_dir)
            count = max(1, min(int(count), 30))
            ids: list[int]

            result_key = f"competitive_provision_{service_type}" if service_type else ""
            if criterion == "low_provision" and result_key and result_key in state:
                values = _numeric_metric(state[result_key])
                ids = [int(i) for i in values.nsmallest(count).index]
                return _format_target_suggestion(ids, f"lowest cached provision for {service_type}", values)

            filtered = blocks
            if land_use:
                lu = _parse_land_use(land_use)
                filtered = blocks[blocks.get("land_use").apply(lambda value: _land_use_name(value) == lu.name)]
            if filtered.empty:
                filtered = blocks

            if criterion in {"high_population", "low_provision"} and "population" in filtered.columns:
                values = pd.to_numeric(filtered["population"], errors="coerce").fillna(0)
                ids = [int(i) for i in values.nlargest(count).index]
                return _format_target_suggestion(ids, "highest population candidate blocks", values)

            ids = [int(i) for i in filtered.index[:count]]
            return _format_target_suggestion(ids, f"first {count} blocks after filters", None)
        except Exception as exc:
            return f"Ошибка подбора целевых кварталов: {exc}"

    @tool
    def propose_zone_development(
        block_ids: list[int],
        target_land_use: str = "RESIDENTIAL",
        service_set: str = "basic",
        max_time_sec: int = 300,
        max_runs: int = 1000,
    ) -> str:
        """Run area-based TPE optimization and return a development hypothesis for selected blocks."""
        try:
            from blocksnet.optimization.services import (
                AreaSolution,
                Facade,
                GradientChooser,
                RandomOrder,
                TPEOptimizer,
                WeightedConstraints,
                WeightedObjective,
            )
        except Exception as exc:
            return f"Ошибка: blocksnet.optimization.services недоступен: {exc}"

        try:
            blocks = ensure_blocks(state, data_dir).copy()
            acc_mx = ensure_acc_mx(state, data_dir)
            ids = _valid_block_ids(block_ids, blocks)
            if not ids:
                return "Ошибка: не найдено валидных block_ids для оптимизации."

            lu = _parse_land_use(target_land_use)
            blocks_lu = {bid: lu for bid in ids}
            weights = _available_service_weights(service_set, blocks, lu)
            if not weights:
                return (
                    f"Ошибка: для service_set='{service_set}' и land_use={lu.name} нет доступных capacity_* сервисов."
                )

            max_time_sec = max(10, min(int(max_time_sec), 300))
            max_runs = max(1, min(int(max_runs), 1000))

            facade = Facade(AreaSolution(blocks_lu), acc_mx, blocks, blocks_lu)
            added_services: list[str] = []
            for service_type, weight in weights.items():
                service_df = _capacity_df(blocks, service_type)
                before = facade.num_params
                facade.add_service_type(service_type, float(weight), service_df)
                if facade.num_params > before:
                    added_services.append(service_type)

            if facade.num_params <= 0 or not added_services:
                return (
                    "Ошибка: оптимизатор не создал переменные. Проверь target_land_use и выбранные сервисы."
                )

            objective = WeightedObjective(
                facade.num_params,
                facade,
                max_evals=max_runs,
                weights={service: weights[service] for service in added_services},
            )
            constraints = WeightedConstraints(facade, facade.num_params)
            optimizer = TPEOptimizer(
                objective,
                constraints,
                RandomOrder(),
                GradientChooser(facade, facade.num_params),
            )
            try:
                import optuna

                optuna.logging.set_verbosity(optuna.logging.WARNING)
            except Exception:
                pass
            best_x, best_val, success_rate, evals = optimizer.run(
                max_runs=max_runs,
                timeout=max_time_sec,
                initial_runs_num=1,
                verbose=False,
            )

            services_df = facade.solution_to_services_df(best_x)
            area_df = facade.get_solution_area_df(best_x)
            delta_df = _delta_demand_df(facade, best_x)
            provisions = _safe_provisions(facade, best_x)

            paths = _save_solution_outputs(
                output_dir,
                blocks,
                ids,
                services_df,
                area_df,
                delta_df,
            )
            state["zone_development_solution_services"] = services_df
            state["zone_development_solution_area"] = area_df
            state["zone_development_delta_demand"] = delta_df

            return _format_development_hypothesis(
                ids=ids,
                target_land_use=lu.name,
                service_set=service_set,
                added_services=added_services,
                services_df=services_df,
                area_df=area_df,
                delta_df=delta_df,
                best_val=float(best_val),
                success_rate=float(success_rate),
                evals=int(evals),
                max_time_sec=max_time_sec,
                provisions=provisions,
                paths=paths,
            )
        except Exception as exc:
            return f"Ошибка оптимизации зоны: {exc}"

    @tool
    def optimize_zone_services(
        block_ids: list[int],
        target_land_use: str = "RESIDENTIAL",
        service_set: str = "basic",
        max_time_sec: int = 300,
        max_runs: int = 1000,
    ) -> str:
        """Alias for propose_zone_development kept for area-based TPE service optimization wording."""
        return propose_zone_development.invoke({
            "block_ids": block_ids,
            "target_land_use": target_land_use,
            "service_set": service_set,
            "max_time_sec": max_time_sec,
            "max_runs": max_runs,
        })

    return [suggest_target_blocks, propose_zone_development, optimize_zone_services]


def _parse_land_use(value: str) -> LandUse:
    key = str(value or "RESIDENTIAL").split(".")[-1].upper()
    return LandUse[key]


def _land_use_name(value: Any) -> str:
    if isinstance(value, LandUse):
        return value.name
    return str(value).split(".")[-1].upper()


def _valid_block_ids(block_ids: list[int], blocks) -> list[int]:
    valid = []
    for raw in block_ids or []:
        try:
            bid = int(raw)
        except (TypeError, ValueError):
            continue
        if bid in blocks.index and bid not in valid:
            valid.append(bid)
    return valid[:30]


def _available_service_weights(service_set: str, blocks, land_use: LandUse) -> dict[str, float]:
    try:
        from blocksnet.config.service_types.config import SERVICE_TYPES
    except Exception:
        SERVICE_TYPES = pd.DataFrame()

    preset = _SERVICE_PRESETS.get(service_set, _SERVICE_PRESETS["basic"])
    available = {column.replace("capacity_", "") for column in blocks.columns if column.startswith("capacity_")}
    weights: dict[str, float] = {}
    for service_type, weight in preset.items():
        if service_type not in available:
            continue
        if not SERVICE_TYPES.empty and service_type in SERVICE_TYPES.index:
            allowed = {str(item).upper() for item in SERVICE_TYPES.loc[service_type, "land_use"]}
            if land_use.name.lower() not in {item.lower() for item in allowed}:
                continue
        weights[service_type] = weight
    return weights


def _capacity_df(blocks, service_type: str) -> pd.DataFrame:
    cap_col = f"capacity_{service_type}"
    df = blocks[[cap_col]].rename(columns={cap_col: "capacity"}).copy()
    df["capacity"] = pd.to_numeric(df["capacity"], errors="coerce").fillna(0).astype(int)
    return df


def _numeric_metric(metric) -> pd.Series:
    if isinstance(metric, pd.Series):
        return pd.to_numeric(metric, errors="coerce").dropna()
    if isinstance(metric, pd.DataFrame):
        preferred = ["provision", "provision_strong", "provision_weak"]
        for column in preferred:
            if column in metric.columns:
                return pd.to_numeric(metric[column], errors="coerce").dropna()
        numeric = metric.select_dtypes(include="number")
        if not numeric.empty:
            return pd.to_numeric(numeric.iloc[:, -1], errors="coerce").dropna()
    return pd.Series(dtype="float64")


def _solution_vector(facade, solution: dict[str, int]) -> np.ndarray:
    x = np.zeros(facade.num_params)
    for var_name, var_val in solution.items():
        x[int(str(var_name)[2:])] = var_val
    return x


def _delta_demand_df(facade, solution: dict[str, int]) -> pd.DataFrame:
    try:
        delta = facade.get_delta_demand(_solution_vector(facade, solution))
    except Exception:
        return pd.DataFrame(columns=["block_id", "service_type", "demand"])
    rows = [
        {"block_id": block_id, "service_type": service_type, "demand": demand}
        for block_id, services in delta.items()
        for service_type, demand in services.items()
    ]
    return pd.DataFrame(rows)


def _safe_provisions(facade, solution: dict[str, int]) -> dict[str, Any]:
    try:
        provisions, _changed = facade.get_all_provisions(_solution_vector(facade, solution))
        return {
            "start": dict(getattr(facade, "start_provisions", {}) or {}),
            "solution": dict(provisions or {}),
        }
    except Exception:
        return {"start": dict(getattr(facade, "start_provisions", {}) or {}), "solution": {}}


def _save_solution_outputs(output_dir, blocks, ids: list[int], services_df, area_df, delta_df) -> dict[str, str]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    files = {
        "solution_services": output_path / "solution_services.csv",
        "solution_area": output_path / "solution_area.csv",
        "delta_demand": output_path / "delta_demand.csv",
    }
    services_df.to_csv(files["solution_services"], index=False)
    area_df.to_csv(files["solution_area"])
    delta_df.to_csv(files["delta_demand"], index=False)
    for key, path in files.items():
        record_file(path, "csv", meta={"tool": "propose_zone_development", "artifact": key})
        paths[key] = str(path)

    service_counts = pd.Series(0.0, index=blocks.index, name="proposed_services_count")
    if not services_df.empty and "block_id" in services_df.columns:
        counts = services_df.groupby("block_id")["count"].sum()
        service_counts.loc[counts.index] = counts.astype(float)
    for bid in ids:
        if service_counts.loc[bid] == 0:
            service_counts.loc[bid] = 0.1
    map_path = save_metric_map(
        blocks,
        service_counts,
        "proposed_services_count",
        output_path,
        "Предложенные сервисы и зона оптимизации",
    )
    if map_path:
        paths["map"] = str(map_path)
    return paths


def _format_target_suggestion(ids: list[int], reason: str, values: pd.Series | None) -> str:
    lines = [f"Кандидаты для оптимизации ({reason}): {ids}"]
    if values is not None and ids:
        lines.append("Значения по выбранному критерию:")
        lines.extend(f"- block_id {bid}: {float(values.loc[bid]):.4f}" for bid in ids if bid in values.index)
    lines.append("Используй эти IDs в propose_zone_development(block_ids=[...]) при явном запросе гипотез развития.")
    return "\n".join(lines)


def _format_development_hypothesis(
    ids: list[int],
    target_land_use: str,
    service_set: str,
    added_services: list[str],
    services_df: pd.DataFrame,
    area_df: pd.DataFrame,
    delta_df: pd.DataFrame,
    best_val: float,
    success_rate: float,
    evals: int,
    max_time_sec: int,
    provisions: dict[str, Any],
    paths: dict[str, str],
) -> str:
    if services_df.empty:
        services_summary = "новые сервисы не выбраны в найденном решении"
    else:
        grouped = (
            services_df.groupby("service_type")
            .agg(count=("count", "sum"), capacity=("capacity", "sum"))
            .sort_values("count", ascending=False)
        )
        services_summary = "; ".join(
            f"{service}: {int(row['count'])} ед., capacity={int(row['capacity'])}"
            for service, row in grouped.iterrows()
        )
    block_summary = ""
    if not services_df.empty:
        per_block = services_df.groupby("block_id")["count"].sum().sort_values(ascending=False).head(10)
        block_summary = "; ".join(f"block_id {int(bid)}: {int(count)} ед." for bid, count in per_block.items())
    provision_summary = _format_provision_delta(provisions)
    artifact_summary = ", ".join(f"{key}={path}" for key, path in paths.items())
    return (
        "HYPOTHESES: Гипотеза развития зоны: для кварталов "
        f"{ids} рассмотреть целевое землепользование {target_land_use} и набор сервисов '{service_set}'. "
        f"Оптимизатор предложил: {services_summary}. "
        + (f"Распределение по кварталам: {block_summary}. " if block_summary else "")
        + (f"Оценка обеспеченности: {provision_summary}. " if provision_summary else "")
        + f"Objective={best_val:.4f}, successful_trials={success_rate:.2f}, evals={evals}, timeout={max_time_sec}s. "
        "Это эвристическая рекомендация TPE, а не нормативное решение.\n"
        "RESULT: Сохранены solution_services.csv, solution_area.csv, delta_demand.csv и карта предложений. "
        f"Артефакты: {artifact_summary}\n"
        "FOLLOW_UPS: NormativeAgent: проверить допустимость выбранного землепользования и размещения предложенных сервисов; "
        "WebSearchAgent: найти сопоставимые практики реализации такого набора сервисов; "
        "BlocksNetAgent: при необходимости пересчитать service provision после уточнения сценария.\n"
        "LIMITATIONS: Стохастический TPE; результат зависит от выбранных block_ids, весов сервисов, текущей матрицы доступности и ограничений BlocksNet."
    )


def _format_provision_delta(provisions: dict[str, Any]) -> str:
    start = provisions.get("start", {}) or {}
    solution = provisions.get("solution", {}) or {}
    parts = []
    for service, sol_value in solution.items():
        start_value = start.get(service)
        if start_value is None:
            parts.append(f"{service}={float(sol_value):.3f}")
        else:
            parts.append(f"{service}: {float(start_value):.3f}->{float(sol_value):.3f}")
    return "; ".join(parts)
