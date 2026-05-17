"""Agent capability registry."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SearchModality(str, Enum):
    WEB = "web"
    NORMATIVE = "normative"
    CODE = "code"
    ANY = "any"


class AgentName(str, Enum):
    WEB_SEARCH = "WebSearchAgent"
    NORMATIVE = "NormativeAgent"
    CODE_SPATIAL = "CodeSpatialAgent"
    BLOCKS_NET = "BlocksNetAgent"
    MEDIATOR = "MediatorAgent"
    ORCHESTRATOR = "OrchestratorAgent"
    WIKI_CURATOR = "WikiCuratorAgent"
    CRITIC = "CriticAgent"


@dataclass(frozen=True)
class AgentCapability:
    name: str
    description: str
    handles: list[str]
    cannot_do: list[str]


WEB_CAPABILITY = AgentCapability(
    name=AgentName.WEB_SEARCH.value,
    description=(
        "General-purpose web research agent. It uses internet search and page fetching "
        "to find recent, public, text-based information from websites, reports, news, "
        "institutional pages, and open web documents."
    ),
    handles=[
        "factual questions answerable from public web pages",
        "current status, announcements, project descriptions, and news",
        "finding source URLs and extracting short textual evidence",
    ],
    cannot_do=[
        "execute code or compute spatial metrics",
        "reliably derive geometry, routing, counts, or network measures",
        "interpret regulatory requirements as precisely as the normative agent",
    ],
)

NORMATIVE_CAPABILITY = AgentCapability(
    name=AgentName.NORMATIVE.value,
    description=(
        "Regulatory and standards research agent. It searches a local normative vector "
        "database first and can fall back to targeted web search for laws, standards, "
        "planning rules, requirements, and exact citations."
    ),
    handles=[
        "questions about laws, regulations, standards, and mandatory requirements",
        "finding document names, sections, clauses, and exact quotes",
        "checking whether an urban planning decision is permitted or constrained",
    ],
    cannot_do=[
        "compute spatial indicators or run geospatial analysis",
        "answer broad empirical questions unless they require regulatory sources",
        "replace legal review for high-stakes final decisions",
    ],
)

CODE_SPATIAL_CAPABILITY = AgentCapability(
    name=AgentName.CODE_SPATIAL.value,
    description=(
        "Quantitative and spatial analysis agent. It writes and executes Python code, "
        "checks available local data, and can fetch geospatial/open data through libraries "
        "such as osmnx, geopandas, shapely, geopy, requests, and httpx."
    ),
    handles=[
        "spatial questions about streets, pedestrian areas, routes, distances, buffers, "
        "and accessibility",
        "calculations, counts, measurements, joins, geocoding, and data transformations",
        "questions best answered by OpenStreetMap, GeoJSON, public APIs, or local datasets",
    ],
    cannot_do=[
        "browse arbitrary pages as efficiently as the web agent",
        "produce authoritative legal interpretation",
        "guarantee completeness when public geodata is missing or outdated",
    ],
)


BLOCKS_NET_CAPABILITY = AgentCapability(
    name=AgentName.BLOCKS_NET.value,
    description=(
        "Urban analysis agent powered by the BlocksNet library. Operates on pre-loaded "
        "geospatial city data: urban blocks, travel-time accessibility matrix, and service "
        "capacities. Computes quantitative urban indicators: pedestrian accessibility, "
        "service provision ratios, block density (FSI/GSI/MXI), network centrality, "
        "Shannon diversity of services, and spatial clustering."
    ),
    handles=[
        "transport accessibility — mean/median/max travel time from blocks to amenities",
        "service provision ratios (schools, hospitals, parks, etc.) relative to population",
        "block-level density indicators: FSI, GSI, MXI, OSR",
        "network connectivity and centrality metrics for the loaded city",
        "Shannon diversity index and service collocation analysis",
        "identifying under-served or over-served zones in the city",
        "any urban planning metric computable with the BlocksNet library on the loaded city data",
    ],
    cannot_do=[
        "answer questions about a city whose data has not been loaded into data/",
        "general web search or retrieve recent news",
        "interpret regulatory requirements — use NormativeAgent for that",
        "execute arbitrary Python code unrelated to BlocksNet analysis",
    ],
)

AGENT_CAPABILITIES: dict[str, AgentCapability] = {
    WEB_CAPABILITY.name: WEB_CAPABILITY,
    NORMATIVE_CAPABILITY.name: NORMATIVE_CAPABILITY,
    CODE_SPATIAL_CAPABILITY.name: CODE_SPATIAL_CAPABILITY,
    BLOCKS_NET_CAPABILITY.name: BLOCKS_NET_CAPABILITY,
}


MODALITY_TO_AGENT: dict[SearchModality, AgentName] = {
    SearchModality.WEB: AgentName.WEB_SEARCH,
    SearchModality.NORMATIVE: AgentName.NORMATIVE,
    SearchModality.CODE: AgentName.CODE_SPATIAL,
    SearchModality.ANY: AgentName.CODE_SPATIAL,  # ambiguous spatial/open-data tasks need code first
}

AGENT_TO_NODE: dict[AgentName, str] = {
    AgentName.WEB_SEARCH: "web_search_agent",
    AgentName.NORMATIVE: "normative_agent",
    AgentName.CODE_SPATIAL: "code_spatial_agent",
    AgentName.BLOCKS_NET: "blocksnet_agent",
    AgentName.MEDIATOR: "mediator",
}
