# examples

Сценарии запуска и эксперименты поверх `fp2mp_core`.

| Файл | Назначение |
|---|---|
| [`benchmark_fp2mp.ipynb`](benchmark_fp2mp.ipynb) | Бенчмарк против baselines + оценка через `fp2mp-eval`. 60 ill-defined задач городского планирования. Тяжёлый: требует платных API-вызовов и кэширует результаты в `data/benchmark/`. |
| [`ablation/run_ablation.py`](ablation/run_ablation.py) | CLI smoke-прогон **5 ablation-сценариев** (`baseline`, `redi_off`, `blackboard_off`, `critic_off`, `tools_off`). Каждый сценарий пишет `result.json` под `examples/ablation/runs/<scenario>/`. См. [`../docs/ablation_components.md`](../docs/ablation_components.md). |
| [`ablation/sanity_check.py`](ablation/sanity_check.py) | Smoke-импорт: проверяет, что `AblationConfig` и `run()` импортируются без ошибок (полезно после рефакторинга сигнатур). |
| [`baseline_masterplan.py`](baseline_masterplan.py) | Один мастерплан-прогон (baseline: всё включено, без ablation-флагов). Используется для smoke-теста всей цепочки на конкретной задаче. Поддерживает `--model` и `--question` CLI-флаги. |
| [`notebook/ablation/ablation.ipynb`](notebook/ablation/ablation.ipynb) | Ноутбук-зеркало `ablation/run_ablation.py`. Полезен, если хочется прогнать сценарии интерактивно и видеть результаты в Markdown-таблице. |
| [`data/reference_solutions.json`](data/reference_solutions.json) | Опорные решения для sanity-check ноутбука бенчмарка. |

## Что куда писать (run artifacts)

Все артефакты прогонов автоматически складываются в поддиректории, которые
**не отслеживаются git** (см. `.gitignore`):

```
examples/ablation/runs/<scenario>/result.json   # ablation CLI
examples/baseline/<scenario>/result.json         # baseline_masterplan.py
examples/cache/                                  # HTTP/поисковые кэши ноутбуков
```

Эти директории регенерируются при запуске соответствующих скриптов. Не нужно
добавлять их в коммит.

## Воспроизводимость

- Все сценарии принимают `--model` (override LLM) и пишут `result.json` со
  схемой: `{scenario, flags, elapsed_sec, final_answer, final_answer_len,
  log_entries, usage, log_tail}`.
- `usage` — `{input_tokens, output_tokens, total_tokens, source}` (`source` =
  `"real"` для бэкендов с реальным usage, `"estimate"` для tiktoken-оценки).
- `log_tail` — последние 6 записей трассы (агент + content[:400]) для быстрой
  диагностики без чтения полного лога.