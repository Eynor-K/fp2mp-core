# fp2mp-core — Сводная таблица компонентов для ablation study

> Подготовлено для дизайна интерактивного ноутбука с переключателями
> компонентов. Описывает все отключаемые части системы, их узловую
> принадлежность, способ отключения и связи с другими компонентами.

---

## Сводная таблица (12 компонентов)

| # | Имя компонента | Описание | Узел LangGraph? | Как отключается | Связан с (зависит / читает) |
|---|---|---|---|---|---|
| **1** | **WebSearchAgent** | ReAct-агент: веб-поиск (Tavily/DDG) + загрузка страниц. Эмпирические, фактологические подзапросы. | ✅ `web_search_agent` | Пересборка графа: убрать `add_node` и входящее `Send`-ребро из orchestrator | зависит: capability cards; читает: `orchestrator_directives`; пишет в: `raw_data` |
| **2** | **NormativeAgent** | ReAct-агент: локальная нормативная ChromaDB + целевой веб-поиск. Законы, СНиП, ГОСТ. | ✅ `normative_agent` | Пересборка графа: убрать `add_node` + ребро | зависит: `domain_hints` от ReDI Enricher; пишет в: `raw_data` |
| **3** | **CodeSpatialAgent** | ReAct-агент: пишет и исполняет Python (osmnx, geopandas). Пространственные запросы, OSM. | ✅ `code_spatial_agent` | Пересборка графа: убрать `add_node` + ребро; **дополнительно** — `MODALITY_TO_AGENT[CODE]` должен стать невалидным | зависит: `MODALITY_TO_AGENT`; связан с: ReDI Decomposer (для spatial-routing rule) |
| **4** | **MediatorAgent** | Кросс-источниковый синтез: AGREEMENTS / CONTRADICTIONS / новая `synthesis` wiki-страница + новые ConfirmedFacts. | ✅ `mediator` | Пересборка графа: убрать `add_node` + входящее/исходящее ребро; **также** `_dispatch_mediator` в orchestrator не должен его диспатчить | **читает: `wiki`** (`mediator.py:67`); **пишет: `wiki["synthesis"]`**, `output`; требует: ≥2 результата других агентов |
| **5** | **WikiCuratorAgent** | Fan-in узел: строит/обновляет wiki-страницы, вызывает ReDI Fusion, продвигает факты в `output` (≥0.65 conf), обновляет `index.md`/`log.md`, детектит stagnation. | ✅ `wiki_curator` (содержит **3 подсистемы** внутри) | Пересборка графа: убрать узел; **но это сломает mediator (читает wiki) и synthesis (читает `wiki["synthesis"]`)**. Реалистично — патчить поведение внутри ноды, не удалять узел | читает: `raw_data`, `redi_decomposition`; пишет: `wiki`, `output`, `iteration`, `stagnation_count`; fan-in от: 1, 2, 3, 4 |
| **6** | **CriticAgent** | STOP/CONTINUE + инжект `new_tasks` при пробелах покрытия. Force-STOP при `iteration ≥ max_iter` или `stagnation ≥ 2`. | ✅ `critic` | Пересборка графа: убрать узел; **но тогда max_iter не работает**. Реалистично — заменить на force-STOP функцию | читает: `wiki` (брифинг), `output`, `redi_decomposition`, `raw_data`; пишет: `critique`, `stop_flag`, `tasks` |
| **7** | **final_synthesis** | Генерирует финальный ответ (7-секционное memo). 3-проходная цепочка: draft → critique → refine. Fail-open. | ✅ `final_synthesis` | Узел трогать нельзя (выход обязателен). Отключаемое — **поведение внутри**: пропустить critique/refine | читает: `output`, `wiki["synthesis"]`, `critique.contradictions`; пишет: `final_answer`, `stop_flag` |
| **8** | **ReDI Decomposer** | Разбивает вопрос на 3-5 sq, назначает `search_modality` (web/normative/code/any) + `intent_aspect`. | ❌ внутри `redi_decompose` | Флаг в `redi_decompose_node`: одна sq = весь вопрос | связан с: ReDI Enricher (9), Capability routing (10) |
| **9** | **ReDI Enricher** | LLM-обогащение каждой sq: 3 варианта формулировок + 5-10 keywords + domain_hints (СНиП, ГОСТ и т.п.). | ❌ внутри `initialize_blackboard` | Пропустить `enrich_all()` → пустые `enriched_variants/keywords/domain_hints` | читает: sq от (8); используется: WebSearchAgent (1), NormativeAgent (2) |
| **10** | **Capability-aware LLM routing** | LLM выбирает агента по карточкам `AgentCapability` (handles/cannot_do). Контекст — `wiki_briefing`. | ❌ внутри `orchestrator_node` | Fallback на `MODALITY_TO_AGENT[modality]` (без LLM-выбора) | зависит: `AGENT_CAPABILITIES`; читает: `wiki` (брифинг), `redi_decomposition`; использует результат: диспатчит 1/2/3/4 |
| **11** | **Reassign-on-failure** | Перекидывает провальную sq (conf < 0.4 или "Agent stopped due to…") другому агенту, который её ещё не пробовал. | ❌ внутри `orchestrator_node::_detect_and_reassign_failed` | Пропустить вызов → провалы идут в final_synthesis как есть | читает: `tasks`, `raw_data`; связан с: stagnation (детектится в 5) |
| **12** | **ReDI Fusion** | Дедуп raw_data по Jaccard (≥0.7), keyword overlap score, top-N summary per sq → пишет в `wiki["redi_fusion"]`. | ❌ внутри `wiki_curator_node` | Не вызывать `_fusion.fuse(...)` | читает: `raw_data`, `redi_decomposition`; пишет: `wiki["redi_fusion"]` |

---

## Группировка по типу отключения

### Топологические (4 компонента, чистое отключение узлом)
- **1** WebSearchAgent
- **2** NormativeAgent
- **3** CodeSpatialAgent
- **4** MediatorAgent

### Смешанные (3 компонента, узел есть, но внутри живут подсистемы)
- **5** WikiCurator — узел отключать опасно (ломает mediator, synthesis); реально — патчить поведение
- **6** Critic — узел отключать опасно (нет контроля итераций); реально — заменить на pass-through
- **7** final_synthesis — узел обязателен; отключаемое — поведение

### Поведенческие (5 компонентов, флаги внутри нод)
- **8** ReDI Decomposer
- **9** ReDI Enricher
- **10** Capability-aware LLM routing
- **11** Reassign-on-failure
- **12** ReDI Fusion

---

## Граф зависимостей

```
5 WikiCurator ←─── 4 Mediator (читает wiki)
      ↓ пишет wiki["synthesis"]
7 final_synthesis ←─── (читает wiki["synthesis"], contradictions из 6)

10 Capability routing ←─── читает wiki (wiki_briefing)
6 Critic ←─── читает wiki, output
11 Reassign ←─── читает tasks, raw_data
12 Fusion ←─── читает raw_data, sq

8 Decomposition → 9 Enrichment → 1/2/3 (directive text)
8 Decomposition → 10 routing (search_modality hint)
9 Enrichment → 1/2 (enriched_variants в directive, domain_hints)
```

---

## Каскадные риски (что ломается при отключении)

| Если отключили | Что ломается | Обход |
|---|---|---|
| **5** (wiki не строится) | 4 mediator читает пустую wiki, 7 synthesis читает пустую `wiki["synthesis"]` | 4: fallback на `raw_data`; 7: уже есть `_fallback_answer` |
| **4** (mediator) | 7 synthesis читает пустую `wiki["synthesis"]` | fallback в `_fallback_answer` уже есть |
| **6** (critic → pass-through) | 11 reassign теряет источник `new_tasks` | при `max_iter=1` не критично |
| **8** (decomposition) | 9 enricher нечего обогащать, 10 routing не имеет подсказки модальности | одна sq = весь вопрос, маршрут по `any` |
| **10** (LLM routing → modality fallback) | 11 reassign работает по-прежнему, но выбор менее точный | modality-hint уже нестрогий — деградация мягкая |
| **12** (fusion) | 5 wiki теряет `redi_fusion` page, но остальные страницы строятся | минимальный эффект |

---

## Рекомендация по реализации в ноутбуке

В UI разнести на **два визуальных блока**:

### «Топология графа» (4 тумблера + multi-select)
Какие агенты запускать. Меняется только `build_graph()`. Безопасно.

### «Поведение нод» (5-6 тумблеров + sliders для порогов)
Что внутри узлов делать. Меняется `AblationConfig`, прокидывается в ноды.

### Пары, которые **должны переключаться вместе** (из каскадных рисков)
- **Wiki OFF** ⟺ **Mediator fallback mode** (или Mediator OFF)
- **Critic OFF** ⟺ `max_iterations=1` обязательно

Это можно зашить в ноутбук как «связанные тумблеры» — при клике на один второй переключается/блокируется.

---

## Сводка по файлам, которые потребуется тронуть

| Файл | Что меняется |
|---|---|
| `src/fp2mp_core/graph.py` | `build_graph(ablation=...)` пропускает `add_node`/edges для отключённых агентов |
| `src/fp2mp_core/capabilities.py` | Централизация `AGENT_NODES` (сейчас дублируется в graph.py и orchestrator.py) |
| `src/fp2mp_core/nodes/orchestrator.py` | Учёт `disabled_agents`, `wiki_enabled`, `fusion_enabled`, `reassign_enabled` |
| `src/fp2mp_core/nodes/curator.py` | Если wiki OFF — `_builder` no-op; если fusion OFF — не вызывать `_fusion.fuse(...)` |
| `src/fp2mp_core/nodes/critic.py` | Если critic OFF — force-STOP функция вместо LLM-критика |
| `src/fp2mp_core/nodes/mediator.py` | Если wiki OFF — fallback на `raw_data` |
| `src/fp2mp_core/nodes/synthesis.py` | Если 3-pass OFF — один `llm.invoke`; уже есть `_fallback_answer` |
| `src/fp2mp_core/ablation.py` (новый) | `AblationConfig` dataclass + `apply_to_graph()` |
| `examples/ablation_explorer.ipynb` (новый) | Ноутбук с ipywidgets-переключателями |

Никаких изменений в `state.py` — `AblationConfig` передаётся как параметр, не через state.
