# fp2mp-core: Ablation smoke-test report

**Дата:** 2026-07-14
**Вопрос:** `Можно ли строить жилой дом 9 этажей в 500м от аэропорта?`
**Модель:** `ministral-3:8b` (ollama-cloud, `FP2MP_CHAT_URL=https://ollama.com/v1`)
**MAX_ITER:** 3
**Скрипт:** [`examples/ablation/run_ablation.py`](run_ablation.py)
**Результаты:** [`examples/ablation/runs/`](runs/)

---

## 1. Сводка

| # | Сценарий | Флаги (выключено) | Статус | Время | Длина ответа | Токены (real) |
|---|---|---|---|---:|---:|---:|
| 1 | `tools_off` | tools | ✅ OK | 4:37 | 10 201 | 44 618 |
| 2 | `redi_off` | redi | ✅ OK | 9:00 | 17 932 | 59 528 |
| 3 | `blackboard_off` | blackboard | ⏱ TIMEOUT (20 мин) | >20:00 | — | — |
| 4 | `critic_off` | critic | ⏱ TIMEOUT (14 мин) | >14:00 | — | — |

baseline (всё включено) отдельно не запускался — план требовал 4 теста с выключением каждого элемента.

---

## 2. Результаты успешных прогонов

### 2.1 `tools_off` (агенты отвечают без инструментов)

| Метрика | Значение |
|---|---|
| `final_answer_len` | 10 201 символов |
| `log_entries` | 16 |
| `input_tokens` | 21 406 |
| `output_tokens` | 23 212 |
| `total_tokens` | 44 618 |
| Время | 277.8 с |

**Контракт соблюдён:** `run_toolless` отработал для трёх агентов, каждый сделал один LLM-вызов без ReAct-цикла. Цикл быстро дошёл до `final_synthesis` (1 итерация хватила).

**Качество:** ответ содержит конкретные нормативы (СанПиН 2.2.4.1191-03, СП 42.13330.2011, СП 53.13330.2011), шумовые нормы (60–75 дБА, лимиты 55/45 дБА), оценку реализуемости. Уверенный, структурированный.

### 2.2 `redi_off` (один sub-query, без ReDI Decomposer/Enricher)

| Метрика | Значение |
|---|---|
| `final_answer_len` | 17 932 символов (×1.76 от tools_off) |
| `log_entries` | 40 (×2.5 от tools_off) |
| `input_tokens` | 40 665 |
| `output_tokens` | 18 863 |
| `total_tokens` | 59 528 |
| Время | 539.0 с |

**Контракт соблюдён:** `redi_decompose_node` вернул `[{sub_query_id: "sq_001", text: question, ...}]` без LLM; `search_modality="any"`; fusion под флагом не вызван.

**Поведение:** 40 log-entries (CriticAgent → OrchestratorAgent → MediatorAgent → WikiCurator ×2 → FinalSynthesis). Critic-цикл сделал CONTINUE → Orchestrator диспатчил MediatorAgent → Mediator отдал `parse error: Invalid control character at line 2 column 27` — это известная мини-проблема парсинга JSON в mediator, не связанная с ablation. Wiki-curator успешно сформировал 6 страниц.

**Качество:** ответ длиннее, чем tools_off, **содержит markdown-обёртку ` ```markdown ... ``` `**, что снижает визуальную чистоту (вероятно, FinalSynthesis шаблон отдаёт fence, который не отрезается). Содержательно — отличается от tools_off: `redi_off` подчёркивает необходимость согласования с Росавиацией, `tools_off` — жёсткую невозможность. Оба ответа валидны, акценты разные.

---

## 3. Таймауты: `blackboard_off` и `critic_off`

Оба сценария с `tools=True` повисли на ReAct-цикле агентов. Прогресс застрял на стадии загрузки HuggingFaceEmbeddings (нормативная ретривальная база), и далее не двигался — что указывает на зависание внешнего HTTP (Tavily) или ReAct executor.

### Сравнение сроков (по факту):

| Сценарий | tools | Время | Поведение |
|---|---|---:|---|
| `tools_off` | False | 4:37 | OK |
| `redi_off` | True | 9:00 | OK |
| `blackboard_off` | True | >20:00 | TIMEOUT |
| `critic_off` | True | >14:00 | TIMEOUT |

**Закономерность:** единственная разница между успешными (`tools_off`, `redi_off`) и тайм-аутнувшими — `tools=True` приводит к ReAct-циклу. `redi_off` при `tools=True` прошёл за 9 минут, потому что orchestrator видел один sub-query и диспатчил меньше агентов. `blackboard_off` и `critic_off` при `tools=True` запускают полный ReDI → 3-5 sub-queries → все 3 агента в ReAct-цикле → зависание.

### Где именно висит (гипотеза)

Лог стадии загрузки — последнее, что видно:
```
Loading weights: 100%|██████████| 199/199 [00:00<00:00, 6862.17it/s]
Loading weights: 100%|██████████| 199/199 [00:00<00:00, 5515.39it/s]
```

Это `tools/vector_store.py:54` (`HuggingFaceEmbeddings`). После этого тишина — то есть либо:
1. **Tavily-поиск залипает** на внешнем HTTP-запросе (наиболее вероятно).
2. ReAct-цикл делает несколько итераций tool-вызовов без `print`, и общее время суммируется.

**План явно указывал**, что `blackboard_off` — самый каскадный сценарий и требует отдельной диагностики (`ablation_plan.md:143`).

---

## 4. Что подтверждает корректность ablation-логики

Несмотря на таймауты, **2/4 успешных сценария показывают, что ablation-флаги реально влияют на трассу**:

- `tools_off` → 16 log-entries, нет записей tool-вызовов, один LLM-call на агента. Длина ответа 10K символов — разумно для "answer from parametric knowledge".
- `redi_off` → 40 log-entries (×2.5), **несколько проходов critic+curator** (так как единственный sub-query с `search_modality=any` обрабатывается дольше), `Mediator` диспатчится и пытается синтезировать → наглядно видно, что **без ReDI система вынуждена компенсировать качество через большее количество итераций**, что согласуется с дизайном плана.

**Тест-сьют 23 ablation-тестов** (smoke + combinations) проходит зелёным — это покрывает структурные инварианты (state shape, routing, fallback paths). Реальные e2e-прогоны добавляют ценность только когда не упираются в инфраструктурный bottleneck (Tavily timeout).

---

## 5. Рекомендации для завершения тестирования

| Приоритет | Действие |
|---|---|
| **P0** | Добавить timeout в `tools/web_search.py` (Tavily), чтобы ReAct executor не залипал (например, `httpx.Timeout(15)`) |
| **P0** | Добавить progress-логирование в ReAct-цикл (`print(f"[agent] tool call {i}: {name}")` или `logging.info`) — без этого любая диагностика таймаутов вслепую |
| **P1** | Перезапустить `blackboard_off` и `critic_off` с фиксом из P0 — должна появиться возможность увидеть финальные ответы |
| **P1** | Добавить запуск `baseline` (всё включено) для контрольной точки |
| **P2** | Опционально: добавить в `run_ablation.py` параметр `--max-iter=1` для forced-termination сценариев (`critic_off` по плану требует `max_iter=1` — это его каскадное ограничение, см. `ablation_components.md:95`) |

---

## 6. Файлы артефактов

```
examples/ablation/
├── README.md                   # этот отчёт
├── run_ablation.py             # entry-point (CLI: `python run_ablation.py [scenario]`)
├── sanity_check.py             # smoke для импортов и AblationConfig
└── runs/
    ├── tools_off/result.json   # 4:37, 10K, OK
    ├── redi_off/result.json    # 9:00, 18K, OK
    ├── blackboard_off/result.json  # TIMEOUT >20 мин, есть investigation_hints
    └── critic_off/result.json  # TIMEOUT >14 мин, есть investigation_hints
```

Каждый `result.json` содержит: `scenario`, `flags`, `elapsed_sec`, `final_answer`, `final_answer_len`, `log_entries`, `usage {input_tokens, output_tokens, total_tokens, source}`, `log_tail` (последние 6 записей трассы с `agent` + `content[:400]`).
