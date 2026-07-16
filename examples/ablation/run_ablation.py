"""Ablation smoke test: 5 runs of one question, varying AblationConfig.

Scenarios:
  1. baseline       — everything enabled (control)
  2. redi_off       — one sub-query, no ReDI decomposition
  3. blackboard_off — minimal fan-in, no wiki pages, no mediator dispatch
  4. critic_off     — deterministic critic (no LLM, no new_tasks)
  5. tools_off      — agents answer from parametric knowledge only

Output per run: JSON with question, ablation flags, final_answer, usage, raw_data summary.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from fp2mp_core.ablation import AblationConfig
from fp2mp_core.graph import run

QUESTION = "Можно ли строить жилой дом 9 этажей в 500м от аэропорта?"
MODEL = "ministral-3:8b"  # small, fast, no reasoning budget issues
MAX_ITER = 3

SCENARIOS = {
    "baseline":        AblationConfig(),
    "redi_off":        AblationConfig(redi=False),
    "blackboard_off":  AblationConfig(blackboard=False),
    "critic_off":      AblationConfig(critic=False),
    "tools_off":       AblationConfig(tools=False),
}


def summarize_state(state) -> dict:
    """Pull a compact summary from BaseState for JSON output."""
    log = state["log"] if isinstance(state, dict) else getattr(state, "log", [])
    raw_data = []
    for entry in log or []:
        if hasattr(entry, "content"):
            raw_data.append({
                "agent": getattr(entry, "name", "?"),
                "content": (entry.content or "")[:400],
            })
        elif isinstance(entry, dict):
            raw_data.append({"agent": entry.get("name", "?"), "content": str(entry)[:400]})
    inp = state["input"] if isinstance(state, dict) else getattr(state, "input", "")
    out = state["output"] if isinstance(state, dict) else getattr(state, "output", "")
    usage = state["usage"] if isinstance(state, dict) else getattr(state, "usage", {})
    return {
        "input": inp,
        "final_answer": (out or "")[:2000],
        "final_answer_len": len(out or ""),
        "log_entries": len(log or []),
        "usage": usage,
        "log_tail": raw_data[-6:],
    }


def main(target: str | None = None) -> None:
    out_dir = Path(__file__).parent / "runs"
    out_dir.mkdir(exist_ok=True)

    targets = {target: SCENARIOS[target]} if target else SCENARIOS

    for name, cfg in targets.items():
        run_path = out_dir / name
        run_path.mkdir(exist_ok=True)
        print(f"\n{'=' * 70}\n>> {name}  flags={cfg}\n{'=' * 70}", flush=True)
        t0 = time.time()
        try:
            state = run(
                input=QUESTION,
                model=MODEL,
                max_iterations=MAX_ITER,
                ablation=cfg,
            )
            elapsed = round(time.time() - t0, 1)
            summary = summarize_state(state)
            summary["scenario"] = name
            summary["flags"] = cfg.__dict__
            summary["elapsed_sec"] = elapsed
            summary["error"] = None
        except Exception as exc:  # noqa: BLE001
            elapsed = round(time.time() - t0, 1)
            summary = {
                "scenario": name,
                "flags": cfg.__dict__,
                "elapsed_sec": elapsed,
                "error": f"{type(exc).__name__}: {exc}",
            }
            print(f"!! {name} FAILED: {exc}", flush=True)

        out_file = run_path / "result.json"
        out_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"   -> {out_file}  ({elapsed}s)", flush=True)
        if "final_answer" in summary:
            print(f"   answer_len={summary['final_answer_len']}  "
                  f"usage={summary['usage']}", flush=True)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else None
    main(target)