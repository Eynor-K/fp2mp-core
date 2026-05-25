"""Tests for BlocksNet development optimization tools."""

from __future__ import annotations

from pathlib import Path

from fp2mp_core.tools.blocksnet import make_blocksnet_tools
from fp2mp_core.tools.blocksnet.optimize import make_optimize_tools


def test_optimizer_tools_are_registered(tmp_path):
    names = {
        tool.name
        for tool in make_blocksnet_tools({}, Path("data/model"), tmp_path)
    }

    assert "suggest_target_blocks" in names
    assert "propose_zone_development" in names
    assert "optimize_zone_services" in names


def test_suggest_target_blocks_returns_model_ids(tmp_path):
    state = {}
    tools = {
        tool.name: tool
        for tool in make_optimize_tools({"state": state, "data_dir": Path("data/model"), "output_dir": tmp_path})
    }

    result = tools["suggest_target_blocks"].invoke({"criterion": "high_population", "count": 3})

    assert "Кандидаты для оптимизации" in result
    assert "block_id" in result


def test_propose_zone_development_writes_solution_artifacts(tmp_path):
    state = {}
    tools = {
        tool.name: tool
        for tool in make_optimize_tools({"state": state, "data_dir": Path("data/model"), "output_dir": tmp_path})
    }

    result = tools["propose_zone_development"].invoke({
        "block_ids": [0, 1, 2],
        "target_land_use": "RESIDENTIAL",
        "service_set": "basic",
        "max_time_sec": 10,
        "max_runs": 1,
    })

    assert "HYPOTHESES:" in result
    assert "solution_services.csv" in result
    assert (tmp_path / "solution_services.csv").exists()
    assert (tmp_path / "solution_area.csv").exists()
    assert (tmp_path / "delta_demand.csv").exists()
