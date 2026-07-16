# fp2mp-core

Мультиагентная система для ответов на **ill-defined** (слабо формализованные) вопросы:
произвольный вопрос декомпозируется на подзапросы, параллельно запускаются
специализированные агенты (веб-поиск / нормативы / пространственный код), находки
накапливаются в общей wiki, и финальный синтез выдаёт структурированный ответ в формате
экспертного memo.

Подробная архитектура — [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Архитектура (high-level)

```
START → init → redi_decompose → init_blackboard → orchestrator
                                                │
        ┌────────────────────────────────────────┤
        │           [параллельный Send]          │
        ├──► web_search_agent ───────────────────┤
        ├──► normative_agent  ───────────────────┤ → wiki_curator
        ├──► code_spatial_agent ─────────────────┤
        └──► mediator ───────────────────────────┘

wiki_curator ──continue──► orchestrator   (цикл)
             ──critic────► critic
             ──finish_ready─► final_synthesis → END

critic ──continue──► orchestrator
       ──finish────► final_synthesis → END
```

### Агенты

| Агент | Роль |
|---|---|
| **OrchestratorAgent** | Читает очередь задач, выбирает оптимального агента через LLM-рассуждение над карточками способностей, обнаруживает провальные попытки и переназначает их другому агенту |
| **WebSearchAgent** | ReAct-агент — веб-поиск и загрузка страниц; эмпирические и описательные подзапросы |
| **NormativeAgent** | ReAct-агент — локальная нормативная векторная БД + целевой веб-поиск; законы, стандарты, строительные нормы |
| **CodeSpatialAgent** | ReAct-агент — пишет и выполняет Python-код; пространственные запросы (OSM через `osmnx`), подсчёты, геокодирование |
| **MediatorAgent** | Кросс-источниковый синтез — объединяет находки нескольких агентов, выявляет противоречия |
| **WikiCuratorAgent** | Узел fan-in — ведёт общую wiki, запускает ReDI Fusion, повышает факты с высокой уверенностью, обнаруживает стагнацию |
| **CriticAgent** | Оценщик — принимает решение STOP / CONTINUE, инжектирует новые задачи при пробелах в покрытии |

### Ключевые подсистемы

- **ReDI** (Retrieval-oriented Decomposition Index) — декомпозирует вопрос на 3–5 независимых подзапросов, назначает `intent_aspect` и подсказку модальности (`web | normative | code | any`), обогащает вариантами формулировок и ключевыми словами.
- **BlackBoard** — общее состояние LangGraph (`TypedDict`). Три уровня:
  - `raw_data` — append-only журнал всех выходных данных агентов
  - `wiki` — структурированные страницы знаний (upsert по `page_id`)
  - `output` — подтверждённые факты (upsert по `fact_id`, побеждает более высокая уверенность)
- **Маршрутизация по способностям** — оркестратор выбирает агентов через LLM-рассуждение над структурированными карточками способностей (`capabilities.py`). Провальные задачи (уверенность < 0.4 или лимит итераций) автоматически переназначаются другому агенту.
- **Финальный синтез** (`nodes/synthesis.py`) — собирает подтверждённые факты и wiki в итоговый ответ в режиме **fail-open**: всегда выдаёт конкретный ответ (выбор / место / число / рекомендацию), никогда не отказывается. Ответ оформлен как экспертное memo из 7 секций и генерируется трёхпроходной цепочкой LLM: draft → critique → refine.

## Установка

Требуется **Python 3.10+**.

```bash
# 1. Виртуальное окружение
python -m venv .venv
.venv\Scripts\activate              # Windows
# source .venv/bin/activate        # Linux / macOS

# 2. Установка пакета со всеми зависимостями
pip install -e ".[dev]"

# Опционально: тяжёлые пространственные и ML-зависимости
pip install -e ".[dev,spatial-extended]"

# 3. Переменные окружения
cp .env.example .env
# Заполнить API-ключи в .env
```

## Конфигурация

Все настройки читаются из `.env` (полный шаблон — [`.env.example`](.env.example)):

| Переменная | По умолчанию | Описание |
|---|---|---|
| `FP2MP_CHAT_URL` | `https://routerai.ru/api/v1` | OpenAI-compatible Chat Completions base URL |
| `FP2MP_API_KEY` | — | API-ключ для OpenAI-compatible backend |

Дополнительно для инструментов: `TAVILY_API_KEY` (веб-поиск), `E2B_API_KEY` (опциональный sandbox для `code_spatial`).

Минимальная конфигурация:
```
FP2MP_CHAT_URL=https://routerai.ru/api/v1
FP2MP_API_KEY=sk-...
```

Модель передаётся явно в `run(..., model="...")`, чтобы один backend можно было использовать с разными моделями и числом итераций.

## Запуск

```python
from fp2mp_core.graph import run

result = run(
    input="Какие улицы Петроградского района Санкт-Петербурга можно сделать пешеходными?",
    model="gpt-4o-mini",
)
print(result["output"])
```

С явной моделью и ограничением итераций:

```python
from fp2mp_core.graph import run

result = run(
    input="Где построить новую станцию метро в Екатеринбурге?",
    model="gpt-4o-mini",
    max_iterations=2,
)

assert {"input", "output", "log"} <= set(result.keys())
print(result["output"])
print(result["usage"])  # авторитетный итог по токенам за прогон
```

`run()` возвращает `BaseState`:

| Поле | Тип | Описание |
|---|---|---|
| `input` | `str` | Исходный вопрос |
| `output` | `str` | Финальный синтезированный ответ |
| `log` | `list[BaseMessage]` | Трасса в формате LangChain messages; каждый `AIMessage` несёт per-message оценку токенов (`usage_metadata`, tiktoken) |
| `usage` | `dict` | Авторитетный итог по токенам: `{input_tokens, output_tokens, total_tokens, source}`; `source` = `"real"` либо `"estimate"` |

Низкоуровневый запуск LangGraph доступен, если нужен полный внутренний `BlackBoard`:

```python
from fp2mp_core.graph import build_graph
from fp2mp_core.state import create_initial_state

graph = build_graph()
state = create_initial_state("Ваш вопрос", max_iterations=8)
result = graph.invoke(state)
```

## Ablation-переключатели

`AblationConfig` позволяет включать/выключать каждый компонент системы независимо.
По умолчанию все компоненты включены.

```python
from fp2mp_core.ablation import AblationConfig
from fp2mp_core.graph import run

cfg = AblationConfig(redi=False, tools=False)  # без ReDI и без tools
state = run(input="...", model="gpt-4o-mini", ablation=cfg)
```

Доступные флаги: `redi`, `blackboard`, `critic`, `tools`. Подробное описание каждого —
[`docs/ablation_components.md`](docs/ablation_components.md).

Готовый CLI-сценарий для воспроизводимого smoke-прогона — `examples/ablation/run_ablation.py`.
Ноутбук-зеркало — `examples/notebook/ablation/ablation.ipynb`.

## Тесты

```bash
pytest                     # все тесты
pytest -v --tb=short       # подробный вывод
pytest tests/test_state.py # один модуль
```

Покрытие:

| Файл | Что проверяет |
|---|---|
| `test_state.py` | Структура `BaseState`, фабрики, инициализация |
| `test_blackboard.py` | Узлы `redi_decompose_node`, `init_blackboard`, `coverage_from_sub_queries` |
| `test_redi.py` | Декомпозиция, обогащение, fusion |
| `test_wiki.py` | WikiPageBuilder, версионирование, обнаружение конфликтов |
| `test_curator.py` | WikiCuratorAgent, маршрутизация curator |
| `test_critic.py` | CriticAgent (STOP / CONTINUE) |
| `test_graph.py` | Полная сборка графа, fan-in/fan-out, финальный синтез |
| `test_ablation.py` | `AblationConfig`, проброс в state |
| `test_ablation_combinations.py` | Парные комбинации переключателей + «всё выключено» |
| `test_behavior_ablation.py` | Поведенческие инварианты для каждого флага |
| `test_tools_ablation.py` | Сценарии с `tools=False` (run_toolless) |
| `test_toolless.py` | Toolless-агенты (нет tool-вызовов, один LLM-call) |
| `test_usage.py` | Учёт токенов, `usage_metadata`, `state["usage"]` |
| `test_benchmark_smoke.py` | Smoke бенчмарк-ноутбука |
| `benchmark_smoke.py` | Preflight `preflight_answer()` для бенчмарка |

## Бенчмарк и оценка

Сравнение с бейзлайнами и оценка качества — в ноутбуке
[`examples/benchmark_fp2mp.ipynb`](examples/benchmark_fp2mp.ipynb).

- **Набор задач** — 60 ill-defined задач городского планирования (загружаются из репозитория `vasilstar97/fp2mp`).
- **Solver** — `run(..., model="gpt-4o-mini", max_iterations=3)`.
- **Судья** — внешний пакет `fp2mp-eval` (`FP2MPEval`, `model="openai/gpt-4.1"`, `temperature=0.5`, `n_judges=5`); рубрика из 8 измерений — [`reference_eval.md`](reference_eval.md).
- **Кэш** — решения в `data/benchmark/solutions.json`, оценки в `data/benchmark/evaluations_fp2mp.json`; авто-инвалидизация по SHA256-сигнатуре `synthesis.py`.
- Платные прогоны (`run()` и судья) запускает пользователь вручную.

## Примеры

| Файл / папка | Что делает |
|---|---|
| [`examples/benchmark_fp2mp.ipynb`](examples/benchmark_fp2mp.ipynb) | Бенчмарк: 60 задач × `fp2mp-core` + `fp2mp-eval` judge |
| [`examples/ablation/run_ablation.py`](examples/ablation/run_ablation.py) | CLI smoke-прогон 5 ablation-сценариев |
| [`examples/baseline_masterplan.py`](examples/baseline_masterplan.py) | Один masterplan-прогон (baseline, всё включено) |
| [`examples/notebook/ablation/ablation.ipynb`](examples/notebook/ablation/ablation.ipynb) | Ноутбук-зеркало CLI ablation-сценариев |

## Структура проекта

```
fp2mp-core/
├── src/fp2mp_core/
│   ├── config.py             # Настройки из .env
│   ├── llm.py                # Фабрика ChatOpenAI для OpenAI-compatible backend
│   ├── capabilities.py       # Карточки AgentCapability + реестр AGENT_CAPABILITIES
│   ├── state.py              # BlackBoard TypedDict, Task, RawEntry, WikiPage и др.
│   ├── usage.py              # Учёт токенов (per-message + агрегат state["usage"])
│   ├── ablation.py           # AblationConfig + проброс в state
│   ├── graph.py              # Сборка LangGraph StateGraph
│   ├── nodes/
│   │   ├── orchestrator.py   # Маршрутизация по способностям + переназначение при провале
│   │   ├── curator.py        # WikiCuratorAgent (fan-in, ReDI Fusion, продвижение фактов)
│   │   ├── critic.py         # CriticAgent (STOP / CONTINUE)
│   │   ├── mediator.py       # MediatorAgent (кросс-источниковый синтез)
│   │   ├── synthesis.py      # Финальный синтез (memo из 7 секций, draft→critique→refine)
│   │   ├── blackboard.py     # Инициализация + узел ReDI-декомпозиции
│   │   ├── entry_filters.py  # Фильтры входа в агенты
│   │   └── agents/
│   │       ├── web_search.py
│   │       ├── normative.py
│   │       ├── code_spatial.py
│   │       └── toolless.py   # Агенты-фолбэки для tools_off
│   ├── redi/
│   │   ├── decomposer.py     # Декомпозиция на подзапросы
│   │   ├── enricher.py       # Генерация вариантов + ключевые слова
│   │   └── fusion.py         # ReDI Fusion (дедупликация + аддитивная оценка)
│   ├── wiki/
│   │   ├── page.py           # WikiPageBuilder (обнаружение конфликтов, версионирование)
│   │   ├── index.py          # Генерация index.md
│   │   ├── log.py            # log.md (журнал изменений)
│   │   └── maintenance.py    # Прунинг, слияние, оценка релевантности
│   └── tools/
│       ├── web_search.py     # Tavily / DuckDuckGo
│       ├── vector_store.py   # Нормативный RAG на ChromaDB
│       ├── code_exec.py      # Выполнение Python (subprocess; e2b как опциональный fallback)
│       ├── tool_registry.py  # Реестр инструментов + find_capability_tool
│       └── wiki_io.py        # Сохранение вики на диск
├── tests/                    # 14 pytest-модулей + benchmark_smoke
├── docs/
│   ├── ARCHITECTURE.md       # Подробная архитектура
│   ├── ablation_components.md
│   └── ablation_plan.md
├── examples/                 # Ноутбук + CLI-сценарии
├── data/
│   ├── benchmark/            # Входные задачи + кэш решений/оценок
│   └── tool_kb/              # Карточки инструментов (справочник)
├── .env.example
├── pyproject.toml
└── README.md
```

## Документация

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — подробная архитектура и проектные решения
- [`docs/ablation_components.md`](docs/ablation_components.md) — что именно отключает каждый `AblationConfig` флаг
- [`docs/ablation_plan.md`](docs/ablation_plan.md) — дизайн ablation-исследования
- [`reference_eval.md`](reference_eval.md) — рубрика оценки `fp2mp-eval` (8 измерений, 3 категории)