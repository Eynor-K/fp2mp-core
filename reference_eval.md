# Reference Evaluation

Use the benchmark harness in `examples/benchmark_fp2mp.ipynb` for fp2mp-vs-baseline checks.

Current judge API:

```python
from fp2mp_eval import FP2MPEval

judge = FP2MPEval(model="openai/gpt-4.1", n_judges=1)
scores = judge.evaluate_case((problem, solution))
```

Runtime configuration is via `FP2MP_*` environment variables for this package, plus provider keys such as `TAVILY_API_KEY` for web search. Do not rely on the old `OPENAI_API_KEY` + `examples/eval_regression.py` note as the current reference path.

The reference rubric has 8 dimensions. Track regressions across all dimensions, with special attention to justification, coherence, metacognition/uncertainty handling, diversity, and knowledge integration.
