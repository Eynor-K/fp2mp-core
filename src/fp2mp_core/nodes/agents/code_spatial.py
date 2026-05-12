"""
CodeSpatialAgent — ReAct agent for quantitative and spatial analysis.

Enforces mandatory chain-of-thought: HYPOTHESIS → LIBRARIES CHECK → PLAN → CODE → EXECUTE → INTERPRET.
Can fetch data from OpenStreetMap via osmnx, public APIs via requests/httpx,
and geocode addresses via geopy — no local data required.
"""

from __future__ import annotations

from typing import Any

from langchain_classic.agents import AgentExecutor, create_react_agent
from langchain_core.prompts import PromptTemplate

from fp2mp_core.llm import get_chat_model
from fp2mp_core.state import BlackBoard, RawEntry, board_message
from fp2mp_core.tools.code_exec import (
    check_available_data_tool,
    execute_python_tool,
    list_available_libraries_tool,
)

_SYSTEM_PROMPT = """\
You are the CodeSpatialAgent. You solve quantitative and spatial sub-queries by writing
and executing Python code. You have full network access to fetch data from external sources.

Available tools: execute_python_tool, check_available_data_tool, list_available_libraries_tool.

PRIMARY DATA SOURCES (use these before giving up):
- osmnx: download OSM data, street networks, building footprints, POIs, routing
- geopandas + shapely: geometric operations, buffer zones, spatial joins
- geopy: geocode addresses and place names to coordinates
- requests / httpx: public APIs, open government datasets, GeoJSON endpoints

MANDATORY chain-of-thought (follow this order):
1. HYPOTHESIS: state what you expect to find and compute
2. LIBRARIES CHECK: call list_available_libraries_tool to see what is installed
3. DATA CHECK: call check_available_data_tool to see if relevant local files exist
4. PLAN: write pseudocode of your approach, choosing local or network data source
5. CODE: write the Python snippet (use osmnx/geopy/requests for network data if needed)
6. EXECUTE: call execute_python_tool with the code
7. INTERPRET: explain what the output means for the original question
8. CONFIDENCE: score 0.0-1.0 based on data quality and result stability

DATA SOURCE DECISION RULES:
- Spatial / geographic question (distances, counts, routing, zones) → use osmnx
- Questions asking WHICH streets, buildings, zones, routes, or geographic features exist
  in a named area → ALWAYS start with osmnx.features_from_place(). Do NOT use requests
  for these unless osmnx fails and a real documented API endpoint is known.
- Address or place name → geocode first with geopy, then use coordinates with osmnx
- Statistical public data → use requests to call a public API or download a dataset
- Local file exists and is relevant → prefer local file
- If execution fails, diagnose the error and retry ONCE with a corrected approach

DATA CHECK interpretation:
- check_available_data_tool marks directories as "[dir]". A path with "[dir]" or without
  a file extension is a DIRECTORY, not a file. Do not read it with pandas/read_csv.
- To inspect a directory, call check_available_data_tool("directory_name/**").

NETWORK ERROR RECOVERY:
- If execute_python_tool raises ConnectionError, timeout, SSL, proxy, or similar network error,
  do NOT retry the same URL.
- Switch to osmnx immediately if you were using requests/httpx.
- If osmnx also fails, output a PLAN describing what the code would compute, set
  CONFIDENCE: 0.3, and include LIMITATIONS about unavailable network/geodata.

CODE EXECUTION FORMAT:
- Action Input for execute_python_tool must be raw Python code only.
- Do NOT wrap code in ```python fences or any other markdown.
- Print all results you need to inspect; a final bare expression may not be visible.

OSMNX USAGE PATTERNS:
```python
import osmnx as ox

# Find pedestrian streets/features in a named district
place = "Петроградский район, Санкт-Петербург, Россия"
tags = {{"highway": ["pedestrian", "footway", "living_street"]}}
gdf = ox.features_from_place(place, tags=tags)
cols = [c for c in ["name", "highway"] if c in gdf.columns]
print(f"Pedestrian features: {{len(gdf)}}")
named = gdf[cols].dropna(subset=["name"]).drop_duplicates().head(30)
print(named.to_string(index=False))

# Streets or areas with restricted car access
gdf_no_cars = ox.features_from_place(place, tags={{"access": "no"}})
print(f"No-access features: {{len(gdf_no_cars)}}")

# Street network for walking in a named place.
# graph_from_place does NOT accept dist; use only the place name and network_type.
G = ox.graph_from_place(place, network_type="walk")
print(f"Nodes: {{G.number_of_nodes()}}, edges: {{G.number_of_edges()}}")

# Get features (buildings, roads, POIs) near a point
point = (lat, lon)
tags = {{"building": True}}
gdf = ox.features_from_point(point, tags=tags, dist=500)

# Get street network around a coordinate and compute distance.
# graph_from_point accepts dist; graph_from_place does not.
G = ox.graph_from_point(point, dist=1000, network_type="walk")
nearest = ox.nearest_nodes(G, lon, lat)

# Geocode a place name or address
location = ox.geocode("Pulkovo Airport, Saint Petersburg")  # returns (lat, lon)
```

GEOPY USAGE PATTERNS:
```python
from geopy.geocoders import Nominatim
geolocator = Nominatim(user_agent="fp2mp_analysis")
loc = geolocator.geocode("Pulkovo Airport, Saint Petersburg, Russia")
lat, lon = loc.latitude, loc.longitude
```

You must use the ReAct format exactly. Your first action must be list_available_libraries_tool.

Use exactly this format:

Question: the input question
Thought: what spatial data or calculation you need
Action: one of [{tool_names}]
Action Input: the input to the action
Observation: the tool result
... repeat Thought/Action/Action Input/Observation as needed
Thought: I now know the final answer
Final Answer:
HYPOTHESIS: <your expectation>
PLAN: <pseudocode describing the approach>
RESULT: <interpretation of execution output>
CONFIDENCE: <0.0-1.0>
LIMITATIONS: <what this analysis cannot account for>

{tools}

Tool names: {tool_names}

Question: {input}

{agent_scratchpad}"""

_TOOLS = [execute_python_tool, check_available_data_tool, list_available_libraries_tool]


def _build_agent() -> AgentExecutor:
    llm = get_chat_model(temperature=0.0)
    prompt = PromptTemplate.from_template(_SYSTEM_PROMPT)
    agent = create_react_agent(
        llm,
        _TOOLS,
        prompt,
        stop_sequence=True,
    )
    return AgentExecutor(
        agent=agent,
        tools=_TOOLS,
        max_iterations=12,
        handle_parsing_errors="Use exactly one ReAct step: Thought, Action, Action Input. Do not write Observation yourself.",
        return_intermediate_steps=True,
        verbose=False,
    )


def _format_steps(steps: list[Any]) -> list[dict[str, str]]:
    formatted: list[dict[str, str]] = []
    for action, observation in steps:
        formatted.append(
            {
                "tool": getattr(action, "tool", ""),
                "tool_input": str(getattr(action, "tool_input", ""))[:500],
                "observation": str(observation)[:1000],
            }
        )
    return formatted


def _parse_output(text: str) -> tuple[str, float, list[str]]:
    """Extract result, confidence, and limitations from agent output."""
    result = ""
    confidence = 0.5
    limitations: list[str] = []

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("RESULT:"):
            result = line[len("RESULT:"):].strip()
        elif line.startswith("CONFIDENCE:"):
            try:
                confidence = float(line[len("CONFIDENCE:"):].strip())
            except ValueError:
                pass
        elif line.startswith("LIMITATIONS:"):
            limitations = [lim.strip() for lim in line[len("LIMITATIONS:"):].split(";") if lim.strip()]

    if not result:
        result = text[:500]

    return result, confidence, limitations


def _successful_execution_observation(steps: list[dict[str, str]]) -> str:
    """Return the last useful code execution observation after ReAct parser failures."""
    for step in reversed(steps):
        if step.get("tool") != "execute_python_tool":
            continue
        observation = step.get("observation", "").strip()
        if not observation or observation == "(no output)":
            continue
        failure_markers = (
            "Security check failed:",
            "Execution timed out",
            "Subprocess error:",
            "E2B error:",
            "Traceback",
        )
        if not observation.startswith(failure_markers):
            return observation
    return ""


def code_spatial_agent_node(state: BlackBoard) -> dict[str, Any]:
    """LangGraph node for CodeSpatialAgent."""
    directives = state.get("orchestrator_directives", [])
    iteration = state.get("iteration", 0)

    my_directives = [d for d in directives if d.get("target_agent") == "CodeSpatialAgent"]
    if not my_directives:
        return {"raw_data": [], "agent_trace": [{"node": "code_spatial_agent", "skipped": True}]}

    executor = _build_agent()
    new_entries: list[RawEntry] = []
    trace: list[dict[str, Any]] = []

    for directive in my_directives:
        question = directive.get("question", "")
        sq_id = directive.get("sub_query_id", "")

        try:
            result = executor.invoke({"input": question})
            output_text = result.get("output", "")
            intermediate_steps = _format_steps(result.get("intermediate_steps", []))
            answer, confidence, limitations = _parse_output(output_text)
            fallback_observation = _successful_execution_observation(intermediate_steps)
            if output_text.startswith("Agent stopped due to") and fallback_observation:
                answer = f"Code executed successfully. Observation: {fallback_observation}"
                confidence = max(confidence, 0.65)
                limitations = [
                    "The ReAct agent hit its iteration/parsing limit after a successful code execution; result is based on the last successful observation."
                ]

            entry = board_message(
                agent="CodeSpatialAgent",
                iteration=iteration,
                msg_type="code_result",
                content=answer + (f"\nLimitations: {'; '.join(limitations)}" if limitations else ""),
                sub_query_id=sq_id,
                confidence=confidence,
                tool_trace=[{
                    "directive": directive,
                    "raw_output": output_text[:400],
                    "intermediate_steps": intermediate_steps,
                }],
            )
            new_entries.append(entry)
            trace.append({"agent": "CodeSpatialAgent", "sq_id": sq_id, "confidence": confidence})
        except Exception as exc:
            err_entry = board_message(
                agent="CodeSpatialAgent",
                iteration=iteration,
                msg_type="code_result",
                content=f"Code execution failed: {exc}",
                sub_query_id=sq_id,
                confidence=0.1,
            )
            new_entries.append(err_entry)

    return {
        "raw_data": new_entries,
        "agent_trace": trace,
    }
