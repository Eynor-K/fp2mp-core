"""Runtime helpers for the ablation notebook.

This module is imported by ``ablation.ipynb`` so the notebook itself stays thin:
only scenario names, flags, and the question live in user-editable cells.

What lives here:

- ``LLM_PROVIDER`` presets (``openai_compat`` / ``ollama``) and the
  ``apply_provider_preset()`` helper. Behaviour matches the benchmark notebook
  (``examples/benchmark_fp2mp.ipynb`` cell 1).
- ``BASELINE_SCENARIOS``: name → ``AblationConfig`` mapping. Reproduces the five
  scenarios from ``examples/ablation/run_ablation.py``.
- ``run_scenarios()``: execute one or more scenarios, write ``result.json`` per
  scenario, return a list of summary dicts ready for the comparison table.
- ``summarize_run()``: compact summary pulled from the graph ``BaseState``
  (``final_answer``, ``usage``, ``log_tail``).
- ``build_comparison_table()``: format a Markdown summary across scenarios.

All output is written under ``examples/ablation/runs/<scenario>/result.json`` to
stay compatible with the existing CLI runs and reports.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from fp2mp_core.ablation import AblationConfig

if TYPE_CHECKING:
    # Imported lazily inside ``run_scenarios`` so importing this module does not
    # require heavy graph dependencies (langchain_core, langgraph, ...). That way
    # the notebook can ``import notebook_lib`` even in a stripped-down env and
    # only fails when the user actually presses the "run" cell.
    from fp2mp_core.graph import run


# ---------------------------------------------------------------------------
# LLM provider presets (same logic as examples/benchmark_fp2mp.ipynb cell 1)
# ---------------------------------------------------------------------------

PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "openai_compat": {
        "FP2MP_CHAT_URL": "https://routerai.ru/api/v1",
    },
    "ollama": {
        "FP2MP_CHAT_URL": "https://ollama.com/v1",
        "_api_key_env_fallbacks": ["OLLAMA_API_KEY", "FP2MP_OLLAMA_KEY"],
    },
}


def apply_provider_preset(provider: str) -> dict[str, Any]:
    """Apply a provider preset as ENV defaults; resolve ``FP2MP_API_KEY``.

    Mirrors ``_apply_provider_preset`` in the benchmark notebook but lives here
    so the notebook can stay declarative.
    """
    import os

    if provider not in PROVIDER_PRESETS:
        raise ValueError(
            f"Неизвестный LLM_PROVIDER={provider!r}. "
            f"Допустимо: {sorted(PROVIDER_PRESETS)}"
        )

    preset = PROVIDER_PRESETS[provider]
    applied: dict[str, Any] = {}

    for key, value in preset.items():
        if key.startswith("_"):
            continue
        if key == "FP2MP_API_KEY":
            continue
        if not isinstance(value, str):
            continue
        os.environ.setdefault(key, value)
        applied[key] = os.environ[key]

    fallbacks = list(preset.get("_api_key_env_fallbacks", [])) + ["FP2MP_API_KEY"]
    for env_name in fallbacks:
        value = os.getenv(env_name)
        if value:
            os.environ["FP2MP_API_KEY"] = value
            applied["FP2MP_API_KEY_SOURCE"] = env_name
            break

    return {"provider": provider, "applied": applied}


# ---------------------------------------------------------------------------
# Ablation scenarios
# ---------------------------------------------------------------------------

BASELINE_SCENARIOS: dict[str, AblationConfig] = {
    "baseline":       AblationConfig(),                          # everything on
    "redi_off":       AblationConfig(redi=False),
    "blackboard_off": AblationConfig(blackboard=False),
    "critic_off":     AblationConfig(critic=False),
    "tools_off":      AblationConfig(tools=False),
}


def scenarios_to_run(names: Iterable[str] | None) -> dict[str, AblationConfig]:
    """Filter ``BASELINE_SCENARIOS`` by user-provided names; default = all."""
    if names is None:
        return dict(BASELINE_SCENARIOS)
    selected: dict[str, AblationConfig] = {}
    for name in names:
        if name not in BASELINE_SCENARIOS:
            raise ValueError(
                f"Неизвестный сценарий {name!r}. "
                f"Допустимо: {list(BASELINE_SCENARIOS)}"
            )
        selected[name] = BASELINE_SCENARIOS[name]
    if not selected:
        raise ValueError("Не выбран ни один сценарий.")
    return selected


# ---------------------------------------------------------------------------
# Run + summary
# ---------------------------------------------------------------------------

def _serialize_log(log: Iterable[Any]) -> list[dict[str, Any]]:
    """Dump a ``BaseState`` log to JSON-serializable dicts.

    Mirrors the benchmark notebook (``log_dump = [m.model_dump() for m in log]``)
    but falls back to a defensive shape if a message lacks ``model_dump`` (e.g.
    plain dicts from older code paths). Each entry is wrapped with a stable
    ``type`` field so downstream tooling can tell agent steps apart.
    """
    out: list[dict[str, Any]] = []
    for entry in log or []:
        if hasattr(entry, "model_dump"):
            try:
                payload = entry.model_dump()
            except Exception:  # noqa: BLE001 — never let logging break a run
                payload = {
                    "type": type(entry).__name__,
                    "name": getattr(entry, "name", "?"),
                    "content": getattr(entry, "content", ""),
                }
        elif isinstance(entry, dict):
            payload = dict(entry)
        else:
            payload = {
                "type": type(entry).__name__,
                "content": str(entry),
            }
        # Normalize: every entry gets ``type`` (class name) so it's discoverable
        # even when ``model_dump`` doesn't include it (e.g. raw dicts).
        payload.setdefault("type", type(entry).__name__)
        payload.setdefault("name", getattr(entry, "name", "?"))
        out.append(payload)
    return out


def summarize_run(state: Any) -> dict[str, Any]:
    """Compact summary from a ``BaseState`` returned by ``fp2mp_core.graph.run``.

    Pulls ``final_answer``, ``usage``, the last 6 log entries — same shape as
    ``examples/ablation/run_ablation.py`` so downstream tooling stays compatible.
    Also stores the full serialized log under ``log_dump``; callers can write it
    to disk via :func:`write_log_dump`.
    """
    log = state["log"] if isinstance(state, dict) else getattr(state, "log", [])
    inp = state["input"] if isinstance(state, dict) else getattr(state, "input", "")
    out = state["output"] if isinstance(state, dict) else getattr(state, "output", "")
    usage = state["usage"] if isinstance(state, dict) else getattr(state, "usage", {})

    raw_data: list[dict[str, str]] = []
    for entry in log or []:
        if hasattr(entry, "content"):
            raw_data.append({
                "agent": getattr(entry, "name", "?"),
                "content": (entry.content or "")[:400],
            })
        elif isinstance(entry, dict):
            raw_data.append({"agent": entry.get("name", "?"), "content": str(entry)[:400]})

    return {
        "input": inp,
        "final_answer": (out or "")[:2000],
        "final_answer_len": len(out or ""),
        "log_entries": len(log or []),
        "usage": usage,
        "log_tail": raw_data[-6:],
        "log_dump": _serialize_log(log),
    }


def write_log_dump(run_path: Path, summary: dict[str, Any]) -> Path:
    """Persist the full serialized log next to ``result.json``.

    Writes ``<run_path>/log.json`` with the same shape as the benchmark notebook
    (``solutions[solution_id]["log"]``) — list of ``model_dump()`` dicts, plus a
    ``usage`` block and a ``synthesis_sig`` field so cached logs can be
    auto-invalidated when ``synthesis.py`` changes.
    """
    log_file = run_path / "log.json"
    payload = {
        "input": summary.get("input", ""),
        "final_answer": summary.get("final_answer", ""),
        "usage": summary.get("usage", {}),
        "synthesis_sig": summary.get("synthesis_sig", "unknown"),
        "log": summary.get("log_dump", []),
    }
    log_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return log_file


def _synthesis_signature() -> str:
    """Hash of ``src/fp2mp_core/nodes/synthesis.py`` for cache invalidation.

    Mirrors the benchmark notebook's auto-invalidation logic: if the file is
    unreachable (e.g. notebook run from a stripped env), return ``"unknown"``.
    """
    # examples/notebook/ablation/notebook_lib.py -> repo root -> src/...
    repo_root = Path(__file__).resolve().parents[3]
    p = repo_root / "src" / "fp2mp_core" / "nodes" / "synthesis.py"
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:12]
    except OSError:
        return "unknown"


def _runs_root() -> Path:
    """Where per-scenario result.json files are written."""
    # examples/notebook/ablation/notebook_lib.py -> examples/ablation/runs
    return Path(__file__).resolve().parents[2] / "ablation" / "runs"


def run_scenarios(
    *,
    question: str,
    model: str,
    max_iterations: int,
    scenarios: dict[str, AblationConfig],
    runs_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Execute scenarios sequentially and persist each as ``runs/<name>/result.json``."""
    out_dir = runs_dir or _runs_root()
    out_dir.mkdir(parents=True, exist_ok=True)

    summaries: list[dict[str, Any]] = []
    # Lazy import: keep heavy graph deps out of the import path so that
    # ``import notebook_lib`` works in any environment.
    from fp2mp_core.graph import run as _run_graph  # noqa: PLC0415

    for name, cfg in scenarios.items():
        run_path = out_dir / name
        run_path.mkdir(parents=True, exist_ok=True)
        print(f"\n{'=' * 70}\n>> {name}  flags={asdict(cfg)}\n{'=' * 70}", flush=True)

        t0 = time.time()
        try:
            state = _run_graph(
                input=question,
                model=model,
                max_iterations=max_iterations,
                ablation=cfg,
            )
            elapsed = round(time.time() - t0, 1)
            summary = summarize_run(state)
            summary["scenario"] = name
            summary["flags"] = asdict(cfg)
            summary["elapsed_sec"] = elapsed
            summary["synthesis_sig"] = _synthesis_signature()
            summary["error"] = None
            # Persist the full serialized log next to result.json. The dump
            # itself is too large to embed in result.json — keep it in
            # ``log.json`` and reference the path from result.json.
            log_file = write_log_dump(run_path, summary)
            summary["log_path"] = str(log_file.relative_to(out_dir))
            summary.pop("log_dump", None)
        except Exception as exc:  # noqa: BLE001 — surface every failure into JSON
            elapsed = round(time.time() - t0, 1)
            summary = {
                "scenario": name,
                "flags": asdict(cfg),
                "elapsed_sec": elapsed,
                "error": f"{type(exc).__name__}: {exc}",
            }
            print(f"!! {name} FAILED: {exc}", flush=True)

        out_file = run_path / "result.json"
        out_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            f"   -> {out_file}  ({elapsed}s)"
            + (f"  answer_len={summary.get('final_answer_len')}  "
               f"usage={summary.get('usage')}" if "final_answer" in summary else ""),
            flush=True,
        )
        summaries.append(summary)

    return summaries


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def build_comparison_table(summaries: Iterable[dict[str, Any]]) -> str:
    """Render a Markdown table comparing scenarios (Markdown only, no IPython deps)."""
    rows = list(summaries)
    if not rows:
        return "_Нет результатов._"

    header = (
        "| scenario | flags (off) | status | elapsed | answer len | log entries | "
        "input tok | output tok | total tok | error |\n"
        "|---|---|---|---:|---:|---:|---:|---:|---:|---|"
    )
    lines = [header]
    for s in rows:
        flags_off = [k for k, v in (s.get("flags") or {}).items() if not v]
        flags_cell = ", ".join(flags_off) if flags_off else "—"
        usage = s.get("usage") or {}
        status = "✅" if not s.get("error") else "❌"
        error = (s.get("error") or "")
        if isinstance(error, str) and len(error) > 80:
            error = error[:77] + "…"
        lines.append(
            "| {scenario} | {flags} | {status} | {elapsed:.1f} | {alen} | {logs} | "
            "{in_tok} | {out_tok} | {tot_tok} | {err} |".format(
                scenario=s.get("scenario", "?"),
                flags=flags_cell,
                status=status,
                elapsed=s.get("elapsed_sec") or 0.0,
                alen=s.get("final_answer_len", 0),
                logs=s.get("log_entries", 0),
                in_tok=usage.get("input_tokens", "—"),
                out_tok=usage.get("output_tokens", "—"),
                tot_tok=usage.get("total_tokens", "—"),
                err=error,
            )
        )
    return "\n".join(lines)