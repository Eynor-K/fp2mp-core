# fp2mp-core

Мультиагентная система для ответов на открытые вопросы. Принимает произвольный вопрос, декомпозирует его на подзапросы, параллельно запускает специализированных агентов, накапливает знания в общей вики и синтезирует итоговый структурированный ответ.

## Архитектура

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
| **OrchestratorAgent** | Читает очередь задач, выбирает оптимального агента для каждого подзапроса через LLM-рассуждение над карточками способностей, обнаруживает провальные попытки и переназначает их другому агенту |
| **WebSearchAgent** | ReAct-агент — веб-поиск и загрузка страниц; обрабатывает фактические, эмпирические и описательные подзапросы |
| **NormativeAgent** | ReAct-агент — локальная нормативная векторная БД + целевой веб-поиск; обрабатывает законы, стандарты, строительные нормы |
| **CodeSpatialAgent** | ReAct-агент — пишет и выполняет Python-код; обрабатывает пространственные запросы (OpenStreetMap через osmnx), подсчёты, измерения, геокодирование |
| **MediatorAgent** | Кросс-источниковый синтез — объединяет находки нескольких агентов, выявляет противоречия |
| **WikiCuratorAgent** | Узел fan-in — строит и ведёт общую вики, запускает ReDI Fusion, повышает факты с высокой уверенностью, обнаруживает стагнацию |
| **CriticAgent** | Оценщик — принимает решение STOP / CONTINUE, инжектирует новые задачи при обнаружении пробелов в покрытии |

### Ключевые подсистемы

**ReDI** (Retrieval-oriented Decomposition Index) — декомпозирует вопрос на 3–5 независимых подзапросов, назначает `intent_aspect` и подсказку модальности (`web | normative | code | any`), обогащает каждый подзапрос вариантами формулировок и ключевыми словами.

**BlackBoard** — общее состояние LangGraph (`TypedDict`). Три уровня:
- `raw_data` — append-only журнал всех выходных данных агентов
- `wiki` — структурированные страницы знаний (upsert по page_id)
- `output` — подтверждённые факты (upsert по fact_id, побеждает более высокая уверенность)

**Маршрутизация по способностям** — оркестратор выбирает агентов через LLM-рассуждение над структурированными карточками способностей, определёнными в `capabilities.py`. Подсказка модальности из ReDI передаётся как контекст, а не как обязательное ограничение. Провальные задачи (уверенность < 0.4 или агент упёрся в лимит итераций) автоматически переназначаются другому агенту.

**Финальный синтез** (`nodes/synthesis.py`) — собирает подтверждённые факты и вики в итоговый ответ. Работает в режиме **fail-open**: всегда коммитит конкретный ответ (выбор / место / число / рекомендацию), никогда не отказывается и не подменяет вывод формулировками «нужно проанализировать» / «требуются данные». Ответ оформлен как экспертное memo из 7 секций (`Answer`; `How I'm reading the question`; `What it hinges on`; `Options I weighed`; `The case for it`; `What's still uncertain`; `Where this reasoning could be wrong`) и генерируется трёхпроходной цепочкой LLM: draft → critique → refine. Промпты домен-нейтральны и работают для вопроса из любой области.

## Установка

Требуется **Python 3.10+**.

```bash
# 1. Создать и активировать виртуальное окружение
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux / macOS

# 2. Установить пакет со всеми зависимостями
pip install -e ".[dev]"

# Опционально: тяжёлые пространственные и ML-зависимости
pip install -e ".[dev,spatial-extended]"

# 3. Настроить переменные окружения
copy .env.example .env        # Windows
# cp .env.example .env        # Linux / macOS
# Заполнить API-ключи в .env
```

## Конфигурация

Все настройки читаются из `.env` (полный справочник — [`.env.example`](.env.example)).

| Переменная | По умолчанию | Описание |
|---|---|---|
| `FP2MP_CHAT_URL` | `https://routerai.ru/api/v1` | OpenAI-compatible Chat Completions base URL |
| `FP2MP_API_KEY` | — | API-ключ для OpenAI-compatible backend |

Минимальная конфигурация:
```
FP2MP_CHAT_URL=https://routerai.ru/api/v1
FP2MP_API_KEY=sk-...
```

Модель задаётся через `.env`. Она передаётся в `run(..., model="...")`, чтобы один и тот же backend можно было использовать в `fp2mp-baselines` и `fp2mp-eval` с разными моделями и числом итераций.

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
| `log` | `list[BaseMessage]` | Сообщения трассировки в формате LangChain messages; каждое `AIMessage` несёт **per-message** оценку токенов (`usage_metadata`, tiktoken) |
| `usage` | `dict` | **Авторитетный** итог по токенам за прогон: `{input_tokens, output_tokens, total_tokens, source}`, где `source` = `"real"` (реальные данные бэкенда) либо `"estimate"` (tiktoken-оценка). Считается отдельно от лога, чтобы суммирование per-message оценок не задваивало этот итог |

Низкоуровневый запуск LangGraph всё ещё доступен, если нужен полный внутренний `BlackBoard`:

```python
from fp2mp_core.graph import build_graph
from fp2mp_core.state import create_initial_state

graph = build_graph()
question = "Какие улицы Петроградского района Санкт-Петербурга можно сделать пешеходными?"
state = create_initial_state(question, max_iterations=8)
result = graph.invoke(state)
```

## Тесты

```bash
pytest                      # запустить все тесты
pytest tests/test_state.py  # один модуль
pytest -v --tb=short        # подробный вывод с короткими трейсбеками
```

68 тестов проходят (`pytest`). Примечание: `tests/benchmark_smoke.py` содержит вспомогательный preflight `preflight_answer()` со старым списком из 9 секций — он не участвует в рантайм-пути `run()` и его список секций отстаёт от текущего 7-секционного формата синтеза; сам тест при этом проходит (проверяется на собственных фикстурах).

## Бенчмарк и оценка

Сравнение с бейзлайнами и оценка качества — в ноутбуке [`examples/benchmark_fp2mp.ipynb`](examples/benchmark_fp2mp.ipynb).

- **Набор задач** — 60 ill-defined задач городского планирования (загружаются из репозитория `vasilstar97/fp2mp`).
- **Solver** — `run(..., model="gpt-4o-mini", max_iterations=3)` для бейзлайна `fp2mp`.
- **Судья** — внешний пакет `fp2mp-eval` (`FP2MPEval`, `model="openai/gpt-4.1"`, `temperature=0.5`, `n_judges=5`); рубрика из 8 измерений (см. [`reference_eval.md`](reference_eval.md)).
- **Кэш** — решения в `data/benchmark/solutions.json`, оценки в `data/benchmark/evaluations_fp2mp.json`; авто-инвалидизация по SHA256-сигнатуре `synthesis.py` (изменение синтеза помечает кэш устаревшим и перегенерирует).
- Платные прогоны (`run()` и судья) запускает пользователь вручную.

## Структура проекта

```
fp2mp_core/
├── src/fp2mp_core/
│   ├── config.py             # Настройки из .env
│   ├── llm.py                # Фабрика ChatOpenAI для OpenAI-compatible backend
│   ├── capabilities.py       # Карточки AgentCapability + реестр AGENT_CAPABILITIES
│   ├── state.py              # BlackBoard TypedDict, Task, RawEntry, WikiPage и др.
│   ├── graph.py              # Сборка LangGraph StateGraph
│   ├── nodes/
│   │   ├── orchestrator.py   # Маршрутизация по способностям + переназначение при провале
│   │   ├── curator.py        # WikiCuratorAgent (fan-in, ReDI Fusion, продвижение фактов)
│   │   ├── critic.py         # CriticAgent (STOP / CONTINUE)
│   │   ├── mediator.py       # MediatorAgent (кросс-источниковый синтез)
│   │   ├── synthesis.py      # Финальный синтез (memo из 7 секций, цепочка draft→critique→refine)
│   │   ├── blackboard.py     # Инициализация + узел ReDI-декомпозиции
│   │   └── agents/
│   │       ├── web_search.py
│   │       ├── normative.py
│   │       └── code_spatial.py
│   ├── redi/
│   │   ├── decomposer.py     # Декомпозиция на подзапросы
│   │   ├── enricher.py       # Генерация вариантов + извлечение ключевых слов
│   │   └── fusion.py         # ReDI Fusion (дедупликация + аддитивная оценка)
│   ├── wiki/
│   │   ├── page.py           # WikiPageBuilder (обнаружение конфликтов, версионирование)
│   │   ├── index.py          # Генерация index.md
│   │   ├── log.py            # log.md (журнал изменений)
│   │   └── maintenance.py    # Прунинг, слияние, оценка релевантности
│   └── tools/
│       ├── web_search.py     # Инструмент поиска Tavily / DuckDuckGo
│       ├── vector_store.py   # Нормативный RAG на ChromaDB
│       ├── code_exec.py      # Выполнение Python (subprocess; e2b как опциональный fallback) + проверка библиотек и данных
│       ├── tool_registry.py  # Реестр инструментов + карточки способностей (find_capability_tool)
│       └── wiki_io.py        # Вспомогательные функции сохранения вики на диск
├── tests/
│   ├── conftest.py
│   ├── test_state.py
│   ├── test_blackboard.py
│   ├── test_redi.py
│   ├── test_wiki.py
│   ├── test_curator.py
│   ├── test_critic.py
│   └── test_graph.py
├── .env.example
├── pyproject.toml
└── README.md
```
