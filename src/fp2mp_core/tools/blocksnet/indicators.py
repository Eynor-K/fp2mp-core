from __future__ import annotations

import numpy as np
from langchain_core.tools import tool
from blocksnet.analysis.indicators import calculate_density_indicators, calculate_development_indicators
from blocksnet.relations import generate_adjacency_graph

from fp2mp_core.tools.blocksnet.data import ensure_blocks
from fp2mp_core.runtime import record_file
from fp2mp_core.tools.blocksnet.viz import save_metric_map


def make_indicators_tools(ctx: dict) -> list:
    state = ctx["state"]
    data_dir = ctx["data_dir"]
    output_dir = ctx["output_dir"]

    @tool
    def compute_density_indicators() -> str:
        """Вычисляет индикаторы плотности городской застройки для каждого квартала."""
        try:
            df = calculate_density_indicators(ensure_blocks(state, data_dir))
            state["density_indicators"] = df
            csv_path = output_dir / "density_indicators.csv"
            df.to_csv(csv_path)
            record_file(csv_path, "csv", meta={"tool": "compute_density_indicators"})
            save_metric_map(ensure_blocks(state, data_dir), df, "density_indicators", output_dir, "Индикаторы плотности")
            return f"Индикаторы плотности вычислены.\n{df.describe().to_string()}"
        except Exception as exc:
            return f"Ошибка: {exc}"

    @tool
    def compute_development_indicators() -> str:
        """Вычисляет индикаторы освоенности территории."""
        try:
            df = calculate_development_indicators(ensure_blocks(state, data_dir))
            state["development_indicators"] = df
            csv_path = output_dir / "development_indicators.csv"
            df.to_csv(csv_path)
            record_file(csv_path, "csv", meta={"tool": "compute_development_indicators"})
            save_metric_map(ensure_blocks(state, data_dir), df, "development_indicators", output_dir, "Индикаторы освоенности")
            return f"Индикаторы освоенности вычислены.\n{df.describe().to_string()}"
        except Exception as exc:
            return f"Ошибка: {exc}"

    @tool
    def build_adjacency_graph(buffer_size: int = 0) -> str:
        """Строит граф пространственной смежности городских кварталов."""
        try:
            graph = generate_adjacency_graph(ensure_blocks(state, data_dir), buffer_size=buffer_size)
            state["adjacency_graph"] = graph
            degrees = [degree for _, degree in graph.degree()]
            avg_degree = float(np.mean(degrees)) if degrees else 0.0
            return (
                f"Граф смежности построен: {graph.number_of_nodes()} узлов, {graph.number_of_edges()} ребер.\n"
                f"Средняя степень узла: {avg_degree:.2f}."
            )
        except Exception as exc:
            return f"Ошибка: {exc}"

    return [compute_density_indicators, compute_development_indicators, build_adjacency_graph]
