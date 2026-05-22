# Reference Evaluation

Use the benchmark harness in `examples/benchmark_fp2mp.ipynb` for fp2mp-vs-baseline checks.

Current judge API:

```python
from fp2mp_eval.core import FP2MPEval

judge = FP2MPEval(model="openai/gpt-4.1", temperature=0.5, n_judges=5)
scores = judge.evaluate_case(case=(problem, solution))
```

The benchmark notebook uses `model="openai/gpt-4.1"`, `temperature=0.5`, `n_judges=5`
(set `n_judges=1` for a quick single-judge check).

Runtime configuration is via `FP2MP_*` environment variables for this package, plus provider keys such as `TAVILY_API_KEY` for web search. Do not rely on the old `OPENAI_API_KEY` + `examples/eval_regression.py` note as the current reference path.

The reference rubric has 8 dimensions, grouped into three categories (5-point Likert scale each):

| Category | Dimension | What it measures |
|---|---|---|
| Problem structuring | Framing | Identification of key elements, goals, assumptions |
| Problem structuring | Decomposition | How well the problem is split into coherent subproblems |
| Problem structuring | Uncertainty handling | Addressing ambiguity, missing info, assumptions |
| Solution quality | Coherence | Logical consistency and clarity of reasoning |
| Solution quality | Justification | Strength of argumentation and trade-off analysis |
| Solution quality | Knowledge integration | Use of relevant domain knowledge / context |
| Reflective reasoning | Diversity | Exploration of alternative solutions / perspectives |
| Reflective reasoning | Metacognition | Reflection on the reasoning process and limitations |

Track regressions across all dimensions, with special attention to justification, coherence, metacognition/uncertainty handling, diversity, and knowledge integration.
