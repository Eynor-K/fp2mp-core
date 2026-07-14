# fp2mp-core — План реализации ablation-переключателей

> Цель: возможность независимо отключать 4 компонента системы:
> **ReDI** (полностью), **BlackBoard** (shared state), **Critic**, **Tools** (у всех агентов).
> Все флаги композируемы.

---

## Ключевые наблюдения

1. **Все 4 отключения — поведенческие.** Топологию графа (`build_graph`) менять не нужно:
   ни один узел не удаляется, всё переключается флагами внутри существующих нод.

2. **Управление циклом держится на `tasks` + `max_iterations` + `stagnation_count` + `critic`,
   а не на `output`.** Статус `"done"` нигде не выставляется, `_detect_and_reassign_failed`
   фактически не срабатывает. Значит слой `wiki`/`output` можно убрать, не разваливая цикл.

---

## Плумбинг конфига

Новый файл `src/fp2mp_core/ablation.py`:

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class AblationConfig:
    redi: bool = True        # ReDI: decomposer + enricher + fusion
    blackboard: bool = True  # shared state: wiki-страницы + confirmed facts (output) + курирование
    critic: bool = True      # LLM-критик + инъекция new_tasks
    tools: bool = True       # инструменты у web/normative/code агентов

def get_ablation(state) -> AblationConfig:
    return state.get("ablation") or AblationConfig()
```

Флаги — «enabled» (True = включено, ablation = False).

Проброс — **через state** (LangGraph-идиоматично, переживает `Send(node, dict(state))`):

- `state.py`: поле `ablation: NotRequired[AblationConfig]` в `BlackBoard`;
  `create_initial_state(question, max_iterations, ablation=AblationConfig())` кладёт его.
- `graph.py`: `run(input, model, max_iterations=None, ablation=AblationConfig())` →
  передаёт в `create_initial_state`. `build_graph()` не трогаем.
- Ни одна нода не возвращает `ablation`, поэтому значение из initial state живёт весь прогон.

---

## 1. ReDI OFF (полностью)

ReDI = decomposer + enricher + fusion.

| Файл | Изменение |
|---|---|
| `nodes/blackboard.py::redi_decompose_node` | если `not abl.redi`: без LLM вернуть один sub-query `{sub_query_id:"sq_001", text=question, search_modality:"any", enriched_variants:[], keywords:[], domain_hints:[]}`. Не вызывать `ReDIDecomposer`/`ReDIEnricher`. `_ensure_code_spatial_sub_query` при redi off тоже пропускать (иначе появляется «искусственная» вторая sq). |
| `nodes/curator.py::wiki_curator_node` | вызов `_fusion.fuse(...)` обернуть в `if abl.redi and processable:`. |

**Каскад:** `search_modality="any"` → оркестратор роутит через capability-LLM (штатно);
пустые `enriched_variants/domain_hints` → директива без хинтов (мягкая деградация).
Один sub-query = весь вопрос.

---

## 2. BlackBoard OFF (убрать shared state, оставить доску + итерации)

Трактовка по ТЗ: убираем **накопительный слой знаний** — wiki-страницы, `output`
(confirmed facts), курирование/промоушен/fusion/maintenance. Оставляем: `raw_data` как
доску, `tasks`, итерации, диспетчеризацию и ответы агентов друг за другом.

| Файл | Изменение |
|---|---|
| `nodes/curator.py::wiki_curator_node` | если `not abl.blackboard` → «минимальный fan-in»: только `iteration += 1`, `progress_delta`/`stagnation_count` (по числу новых agent-entries в `raw_data`), `curator_summary`-сообщение. Возврат **без** `wiki`/`output`. Пропустить построение страниц, fusion, `update_incoming_cross_refs`/`compute_relevance_scores`/`prune_wiki`/`merge_overlapping_pages`, промоушен фактов, index/log. |
| `nodes/curator.py::route_from_curator` | при blackboard off `output` всегда пуст → маршрут по `iteration>=max` (`finish_ready`), иначе `continue`. Роутинг к critic делает оркестратор при опустошении pending. |
| `nodes/blackboard.py::coverage_from_sub_queries` | fallback: при blackboard off считать sq «covered/partial» по наличию не-failed entries в `raw_data` для этого `sub_query_id`, а не по `output`. |
| `nodes/orchestrator.py::orchestrator_node` | при blackboard off **не диспатчить медиатора**: ветку `if agent_results >= 2 and "mediator_synthesis" not in result_counts: return _dispatch_mediator(...)` обернуть в `if abl.blackboard and ...`; иначе сразу `_route_to_critic`. Медиатор — часть shared-state слоя, отключается вместе с ним. |
| `nodes/synthesis.py::final_synthesis_node` | при пустом `output` (blackboard off) строить `facts_str` из `raw_data` (не-failed entries: content/confidence/agent/sub_query_id). `_fallback_answer` уже есть, но лучше явно кормить синтез доской. Ветку `wiki["synthesis"]` не трогаем — при blackboard off она пуста → «Not available». |

**Каскад:** `wiki_briefing` при blackboard off схлопывается до «recent raw tail» — это ок
(доска). Оркестратор/reassign работают на `tasks`+`raw_data` без изменений. `iteration`
по-прежнему инкрементится в curator → цикл бунден `max_iterations`. Узел `mediator` остаётся
в графе, но при blackboard off просто не получает `Send` (оркестратор его не выбирает).

---

## 3. Critic OFF

Заменить LLM-критика на детерминированный контроллер (без LLM, без инъекции `new_tasks`).

| Файл | Изменение |
|---|---|
| `nodes/critic.py::critic_node` | если `not abl.critic` → `_deterministic_critique(state)`: **STOP** если `iteration>=max_iterations` или `stagnation>=2` или **нет pending-задач**; иначе **CONTINUE** с `new_tasks=[]`. Не звать `call_with_thinking`. |
| `nodes/critic.py::route_from_critic` | без изменений (читает `stop_flag`). |

**Почему «нет pending → STOP», а не «всегда STOP»:** иначе цикл оборвётся после 1-го раунда,
не раздав все sub-queries (при >3 sq или раннем заходе в critic из curator). Так все sq
успевают отработать один полный проход + медиатор, дальше стоп. Инъекция гэпов и
quality-gating убраны — это и есть суть ablation критика. `max_iterations` — страховка.

---

## 4. Tools OFF (у всех агентов)

Три ReAct-агента (web/normative/code_spatial) отвечают из параметрических знаний LLM одним
вызовом, сохраняя тот же выходной контракт (`ANSWER/CONFIDENCE/SOURCES`, для code —
`RESULT/CONFIDENCE/LIMITATIONS`), чтобы downstream не сломался.

| Файл | Изменение |
|---|---|
| новый `nodes/agents/toolless.py` | `run_toolless(system_prompt, question, temperature) -> str`: один `get_chat_model().invoke(...)`, вернуть текст. |
| `agents/web_search.py` | если `not abl.tools`: вместо `_build_agent()`/`executor.invoke` — `run_toolless(_TOOLLESS_SYSTEM, question)`, затем существующий `_parse_output(text, steps=[])`, `tool_trace=[]`. Добавить `_TOOLLESS_SYSTEM` (тот же контракт, «инструментов нет, отвечай из знаний, снижай confidence при отсутствии источников»). |
| `agents/normative.py` | аналогично; отдельный toolless-промпт (текущий `_SYSTEM_PROMPT` содержит `{tools}/{tool_names}/{agent_scratchpad}` — убрать в toolless-варианте). |
| `agents/code_spatial.py` | аналогично; toolless-промпт без capability-cards и без `run_validated_python`. `retrieve_capabilities` не звать. |

**Каскад:** контракт `raw_data` неизменен → curator/mediator/synthesis работают штатно.
Ожидаемо падает confidence и растёт доля low-confidence фактов.

---

## Сводка по файлам

| Файл | Что делаем |
|---|---|
| `ablation.py` (новый) | `AblationConfig` + `get_ablation` |
| `nodes/agents/toolless.py` (новый) | общий toolless-хелпер |
| `state.py` | поле `ablation`, параметр в `create_initial_state` |
| `graph.py` | параметр `ablation` в `run` |
| `nodes/blackboard.py` | redi off (single-sq), coverage-fallback |
| `nodes/curator.py` | blackboard off (минимальный fan-in), fusion под флагом redi, `route_from_curator` |
| `nodes/critic.py` | critic off (детерминированный STOP) |
| `nodes/orchestrator.py` | blackboard off → не диспатчить медиатора |
| `nodes/synthesis.py` | blackboard off → факты из raw_data |
| `nodes/agents/{web_search,normative,code_spatial}.py` | tools off ветка + toolless-промпты |

---

## Порядок реализации

1. `ablation.py` + `state.py` + `graph.py` (каркас, дефолт = всё включено, поведение не меняется).
2. **Tools OFF** — самый локальный, независимый, легко проверить.
3. **ReDI OFF** — локальный (decompose + fusion-guard).
4. **Critic OFF** — детерминированный контроллер.
5. **BlackBoard OFF** — самый каскадный (curator/mediator/synthesis/coverage/routing), последним.
6. Проверить попарные комбинации и «всё выключено».

---

## Решения по открытым вопросам

1. **`_ensure_code_spatial_sub_query`** при ReDI OFF — **отключаем** (пропускаем, одна sq = весь вопрос).
2. **Медиатор при BlackBoard OFF** — **отключаем** (часть shared-state слоя; оркестратор его не диспатчит).
3. **Ноутбук** `ablation_explorer.ipynb` с тумблерами — **нужен, но отдельной задачей**
   (не входит в этот план; точка входа сейчас — `run(..., ablation=AblationConfig(...))`).
