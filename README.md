# fp2mp-core

Мультиагентная система для ответов на открытые вопросы. Принимает произвольный вопрос, декомпозирует его на подзапросы, параллельно запускает специализированных агентов, накапливает знания в общей вики и синтезирует итоговый структурированный ответ.

## Архитектура

```
START → redi_decompose → init_blackboard → orchestrator
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
| **MediatorAgent** | Синтез с расширенным мышлением — объединяет находки нескольких агентов, выявляет противоречия |
| **WikiCuratorAgent** | Узел fan-in — строит и ведёт общую вики, запускает ReDI Fusion, повышает факты с высокой уверенностью, обнаруживает стагнацию |
| **CriticAgent** | Оценщик с расширенным мышлением — принимает решение STOP / CONTINUE, инжектирует новые задачи при обнаружении пробелов в покрытии |

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
| `LLM_PROVIDER` | `anthropic` | `anthropic` или `openrouter` |
| `ANTHROPIC_API_KEY` | — | Обязателен при `LLM_PROVIDER=anthropic`; также включает расширенное мышление для Mediator/Critic при использовании OpenRouter |
| `OPENROUTER_API_KEY` | — | Обязателен при `LLM_PROVIDER=openrouter` |
| `TAVILY_API_KEY` | — | Веб-поиск; при отсутствии используется DuckDuckGo |
| `E2B_API_KEY` | — | Опциональная облачная песочница для CodeSpatialAgent; при отсутствии — subprocess |
| `MODEL_DEFAULT` | `claude-sonnet-4-6` | Модель для ReAct-агентов и оркестратора |
| `MODEL_THINKING` | `claude-sonnet-4-6` | Модель для Mediator и Critic (расширенное мышление) |
| `MAX_ITERATIONS` | `6` | Максимальное число итераций wiki-куратора до принудительной остановки |
| `WIKI_PERSIST_DIR` | _(пусто)_ | Если задан — страницы вики сохраняются на диск по этому пути |
| `NORMATIVE_DB_PATH` | `data/normative` | Путь к нормативной векторной базе данных |

**Рекомендуемая конфигурация** для баланса цены и качества:
```
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
ANTHROPIC_API_KEY=sk-ant-...        # включает расширенное мышление для синтеза
MODEL_DEFAULT=openai/gpt-4o-mini    # дёшево для поисковых агентов
MODEL_THINKING=anthropic/claude-3.5-sonnet
```

## Запуск

```python
from fp2mp_core.graph import run

result = run("Какие улицы Петроградского района Санкт-Петербурга можно сделать пешеходными?")
print(result["final_answer"])
```

С явным ограничением итераций и сохранением вики на диск:

```python
from fp2mp_core.graph import build_graph
from fp2mp_core.state import create_initial_state

graph = build_graph()
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
│   ├── llm.py                # Фабрика моделей (Anthropic / OpenRouter)
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
│       ├── code_exec.py      # Выполнение Python + проверка библиотек и данных
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
