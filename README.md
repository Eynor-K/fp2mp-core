# fp2mp-core

Мультиагентная система для ответов на открытые вопросы. Принимает произвольный вопрос, декомпозирует его на подзапросы, параллельно запускает специализированных агентов, накапливает знания в общей вики и синтезирует итоговый структурированный ответ.

## Архитектура

```
START → redi_decompose → init_blackboard → orchestrator
                                                │
        ┌────────────────────────────────────────┤
        │           [параллельный Send]          │
        ├──► web_search_agent ───────────────────┤
        ├──► normative_agent  ───────────────────┤
        ├──► code_spatial_agent ─────────────────┤ → wiki_curator
        ├──► blocksnet_agent  ───────────────────┤
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
| **BlocksNetAgent** | ReAct-агент — городской анализ на основе библиотеки blocksnet; 23 инструмента для расчёта транспортной доступности, обеспеченности сервисами, плотности застройки (FSI/GSI/MXI) и централности кварталов; работает с любыми геоданными, загруженными в `data/` (данные для конкретного города могут быть подготовлены другим агентом) |
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

Модель больше не задаётся через `.env`. Она передаётся в `run(..., model="...")`, чтобы один и тот же backend можно было использовать в `fp2mp-baselines` и `fp2mp-eval` с разными моделями и числом итераций.

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

assert set(result.keys()) == {"input", "output", "log"}
print(result["output"])
```

`run()` возвращает `BaseState`:

| Поле | Тип | Описание |
|---|---|---|
| `input` | `str` | Исходный вопрос |
| `output` | `str` | Финальный синтезированный ответ |
| `log` | `list[BaseMessage]` | Сообщения трассировки в формате LangChain messages |

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

52 теста проходят. 9 тестов имеют pre-existing падения в модулях curator, wiki и redi, не связанные с логикой маршрутизации.

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
│   │   ├── synthesis.py      # Генерация финального ответа
│   │   ├── blackboard.py     # Инициализация + узел ReDI-декомпозиции
│   │   └── agents/
│   │       ├── web_search.py
│   │       ├── normative.py
│   │       ├── code_spatial.py
│   │       └── blocksnet_agent.py  # BlocksNetAgent — городской анализ по данным из data/
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
│       ├── code_exec.py      # Выполнение Python + проверка библиотек и данных
│       ├── wiki_io.py        # Вспомогательные функции сохранения вики на диск
│       └── blocksnet/        # Инструменты BlocksNetAgent (23 шт.)
│           ├── data.py       #   Загрузка геоданных и кэш
│           ├── network.py    #   Транспортная доступность
│           ├── provision.py  #   Обеспеченность сервисами
│           ├── services.py   #   Анализ сервисов и централность
│           ├── indicators.py #   Плотность застройки (FSI/GSI/MXI)
│           └── prompts.py    #   Системный промпт агента
├── data/                     # Геоданные города (текущий пример — Екатеринбург)
│   ├── blocks_with_services.gpkg  # Кварталы + ёмкости сервисов (требуется для BlocksNetAgent)
│   ├── acc_mx.pickle              # Матрица доступности NxN (требуется для BlocksNetAgent)
│   ├── building.gpkg              # Контуры зданий (31 МБ)
│   ├── boundary.geojson           # Граница города
│   ├── roads.geojson              # Дорожная сеть
│   ├── railways.geojson           # Железные дороги
│   ├── water.geojson              # Водные объекты
│   ├── functional_zones.geojson   # Функциональные зоны (27 МБ)
│   ├── platform/                  # 68 GeoJSON с объектами сервисов
│   │   ├── school.geojson
│   │   ├── hospital.geojson
│   │   ├── kindergarten.geojson
│   │   └── ...
│   ├── normative/                 # Нормативные документы для NormativeAgent (RAG)
│   └── outputs/                   # CSV-результаты BlocksNetAgent (создаётся автоматически)
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
