"""Baseline masterplan run: no ablation, single model, single question.

Used to verify clean pipeline behavior (gating, routing, synthesis)
without component-level flags interfering.

Usage:
    python examples/baseline_masterplan.py                    # defaults
    python examples/baseline_masterplan.py --model kimi-k2.6  # override
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from fp2mp_core.ablation import AblationConfig
from fp2mp_core.graph import run

DEFAULT_MODEL = "gpt-oss:20b"
DEFAULT_QUESTION = (
    "Создай инвестиционный мастер-план территории города Гатчина "
    "(Ленинградская область): демография, экономика, землепользование, "
    "транспорт, инфраструктура, инвестиционный потенциал."
)
DEFAULT_OUT = Path(__file__).parent / "baseline" / "gatchina"


def summarize(state) -> dict:
    log = state["log"] if isinstance(state, dict) else getattr(state, "log", [])
    log_tail = []
    for entry in log or []:
        if hasattr(entry, "content"):
            log_tail.append({
                "agent": getattr(entry, "name", "?"),
                "content": (entry.content or "")[:400],
            })
        elif isinstance(entry, dict):
            log_tail.append({"agent": entry.get("name", "?"), "content": str(entry)[:400]})
    inp = state["input"] if isinstance(state, dict) else getattr(state, "input", "")
    out = state["output"] if isinstance(state, dict) else getattr(state, "output", "")
    usage = state["usage"] if isinstance(state, dict) else getattr(state, "usage", {})
    return {
        "input": inp,
        "final_answer": (out or "")[:3000],
        "final_answer_len": len(out or ""),
        "log_entries": len(log or []),
        "usage": usage,
        "log_tail": log_tail[-6:],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--max-iter", type=int, default=3)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    meta_path = args.out / "meta.json"
    meta = {
        "model": args.model,
        "max_iterations": args.max_iter,
        "question": args.question,
        "ablation": AblationConfig().__dict__,  # all enabled (baseline)
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

    print(f"\n{'=' * 70}\n>> BASELINE masterplan run\n   model={args.model}\n   max_iter={args.max_iter}\n"
          f"   question: {args.question[:120]}...\n   ablation: {meta['ablation']}\n{'=' * 70}", flush=True)

    t0 = time.time()
    try:
        state = run(
            input=args.question,
            model=args.model,
            max_iterations=args.max_iter,
            ablation=None,  # default: AblationConfig() — all enabled
        )
        elapsed = round(time.time() - t0, 1)
        summary = summarize(state)
        summary["scenario"] = "baseline"
        summary["flags"] = meta["ablation"]
        summary["elapsed_sec"] = elapsed
        summary["error"] = None
        print(f"   -> OK in {elapsed}s, answer_len={summary['final_answer_len']}, "
              f"usage={summary['usage']}", flush=True)
    except Exception as exc:  # noqa: BLE001
        elapsed = round(time.time() - t0, 1)
        summary = {
            "scenario": "baseline",
            "flags": meta["ablation"],
            "elapsed_sec": elapsed,
            "error": f"{type(exc).__name__}: {exc}",
        }
        print(f"!! FAILED in {elapsed}s: {exc}", flush=True)

    (args.out / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"   -> {args.out / 'result.json'}", flush=True)


if __name__ == "__main__":
    main()