"""
Phase-2 verification harness — run the system on a diverse, domain-NEUTRAL
question set and (optionally) score it with the external fp2mp-eval judge panel.

This script does NOT run automatically. Run it yourself when you have API keys:

    # 1. system key (this repo)
    #    .env must contain FP2MP_API_KEY (+ FP2MP_CHAT_URL)
    # 2. judge key (only if you also want eval scores)
    pip install -e path/to/fp2mp-eval        # git clone vasilstar97/fp2mp-eval
    export OPENAI_API_KEY=sk-...             # judges use gpt-4.1

    python examples/eval_regression.py --model <your-model-id>
    python examples/eval_regression.py --model <your-model-id> --no-eval

Results (solution traces + panel scores) are written to data/outputs/eval/.
Compare runs before vs. after the changes to track the 8 reasoning dimensions
(framing, decomposition, diversity, coherence, justification,
uncertainty_handling, knowledge_integration, metacognition).

The question set is deliberately mixed (urban, regulatory, factual, and a
non-urban engineering concept) to verify the reasoning layer stayed UNIVERSAL.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from fp2mp_core.config import DATA_DIR
from fp2mp_core.graph import run

# Domain-neutral on purpose — do not narrow this to urban/spatial only.
DEFAULT_QUESTIONS = [
    "Найди место для новой станции метро в Екатеринбурге",
    "Какие нормативные требования регулируют размещение АЗС вблизи жилой застройки в России?",
    "Как работает технология магнитной левитации в поездах maglev и каковы её ограничения?",
    "Предложи концепцию автономной системы накопления энергии для удалённой "
    "метеостанции в Арктике с учётом низких температур.",
]


def _serialize_log(log) -> list[dict]:
    out = []
    for m in log or []:
        out.append(
            {
                "type": m.__class__.__name__,
                "name": getattr(m, "name", None),
                "content": str(getattr(m, "content", ""))[:4000],
            }
        )
    return out


def _run_questions(questions: list[str], model: str, max_iter: int | None) -> list[dict]:
    results = []
    for i, q in enumerate(questions, 1):
        print(f"[{i}/{len(questions)}] running: {q[:70]}")
        t0 = time.time()
        try:
            state = run(q, model=model, max_iterations=max_iter)
            results.append(
                {
                    "question": q,
                    "answer": state.get("output", ""),
                    "log": _serialize_log(state.get("log", [])),
                    "elapsed_s": round(time.time() - t0, 1),
                    "error": None,
                }
            )
        except Exception as exc:  # keep going; record the failure
            results.append(
                {"question": q, "answer": "", "log": [], "elapsed_s": round(time.time() - t0, 1), "error": repr(exc)}
            )
            print(f"    ERROR: {exc!r}")
    return results


def _maybe_eval(results: list[dict]) -> dict | None:
    """Best-effort fp2mp-eval scoring. Returns per-question + mean dimensions."""
    try:
        from fp2mp_eval.core import FP2MPEval  # type: ignore[import-not-found]
    except Exception as exc:
        print(f"fp2mp-eval not available ({exc}); skipping judge scoring.")
        return None

    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY not set; skipping judge scoring.")
        return None

    judge = FP2MPEval()
    scored: dict = {"per_question": [], "mean": {}}
    dim_totals: dict[str, list[float]] = {}

    for r in results:
        if not r["answer"]:
            continue
        try:
            wide, _long = judge.evaluate_case((r["question"], r["answer"]))
            row = wide.mean(numeric_only=True).to_dict()
        except Exception as exc:
            print(f"    eval failed for question: {exc!r}")
            continue
        scored["per_question"].append({"question": r["question"], "scores": row})
        for dim, val in row.items():
            dim_totals.setdefault(dim, []).append(float(val))

    scored["mean"] = {d: round(sum(v) / len(v), 3) for d, v in dim_totals.items() if v}
    return scored


def main() -> None:
    parser = argparse.ArgumentParser(description="fp2mp_core Phase-2 verification harness")
    parser.add_argument("--model", default=os.getenv("FP2MP_MODEL", ""), help="model id for the system LLM")
    parser.add_argument("--max-iter", type=int, default=None)
    parser.add_argument("--no-eval", action="store_true", help="skip fp2mp-eval scoring")
    parser.add_argument("--questions-file", default=None, help="optional .txt, one question per line")
    args = parser.parse_args()

    if not args.model:
        parser.error("--model is required (or set FP2MP_MODEL)")

    questions = DEFAULT_QUESTIONS
    if args.questions_file:
        questions = [
            ln.strip()
            for ln in Path(args.questions_file).read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]

    out_dir = DATA_DIR / "outputs" / "eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    results = _run_questions(questions, args.model, args.max_iter)
    (out_dir / f"runs_{stamp}.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nSaved traces → {out_dir / f'runs_{stamp}.json'}")

    if not args.no_eval:
        scored = _maybe_eval(results)
        if scored:
            (out_dir / f"scores_{stamp}.json").write_text(
                json.dumps(scored, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"Saved scores → {out_dir / f'scores_{stamp}.json'}")
            print("\nMean dimension scores (1-5):")
            for dim, val in sorted(scored["mean"].items()):
                print(f"  {dim:24s} {val}")


if __name__ == "__main__":
    main()
