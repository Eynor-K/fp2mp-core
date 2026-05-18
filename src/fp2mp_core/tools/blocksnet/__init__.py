from pathlib import Path

from langchain_core.tools import BaseTool

from fp2mp_core.tools.blocksnet.data import make_data_tools
from fp2mp_core.tools.blocksnet.indicators import make_indicators_tools
from fp2mp_core.tools.blocksnet.network import make_network_tools
from fp2mp_core.tools.blocksnet.provision import make_provision_tools
from fp2mp_core.tools.blocksnet.services import make_services_tools

# Tools always included regardless of task (data loading foundation)
_ALWAYS_INCLUDE = {"load_blocks", "load_accessibility_matrix", "list_cached_data", "list_service_types"}


def make_tools(state: dict, data_dir: Path, output_dir: Path) -> list[BaseTool]:
    ctx = {"state": state, "data_dir": data_dir, "output_dir": output_dir}
    return (
        make_data_tools(ctx)
        + make_network_tools(ctx)
        + make_provision_tools(ctx)
        + make_services_tools(ctx)
        + make_indicators_tools(ctx)
    )


def make_tools_for_task(
    state: dict,
    data_dir: Path,
    output_dir: Path,
    task_text: str,
    max_tools: int = 9,
) -> list[BaseTool]:
    """Select tools relevant to the task using BM25 scoring (falls back to all tools)."""
    all_tools = make_tools(state, data_dir, output_dir)

    try:
        from rank_bm25 import BM25Okapi  # type: ignore[import-untyped]

        descriptions = [t.description for t in all_tools]
        tokenized = [desc.lower().split() for desc in descriptions]
        bm25 = BM25Okapi(tokenized)
        scores = bm25.get_scores(task_text.lower().split())

        # Always keep foundation tools, then top scored tools up to max_tools
        always = [t for t in all_tools if t.name in _ALWAYS_INCLUDE]
        always_names = {t.name for t in always}

        ranked = sorted(
            ((score, t) for score, t in zip(scores, all_tools) if t.name not in always_names),
            key=lambda x: x[0],
            reverse=True,
        )
        selected = always + [t for _, t in ranked[: max(0, max_tools - len(always))]]
        return selected

    except ImportError:
        return all_tools


def tool_reference(tools: list[BaseTool]) -> str:
    """Auto-generate a signature reference from the actual tool objects.

    Introspects each tool's name, argument schema (name/type/default) and
    the first line of its docstring so the system prompt can never drift
    from the real callable signatures.
    """
    lines: list[str] = []
    for t in sorted(tools, key=lambda x: x.name):
        args = t.args or {}
        parts: list[str] = []
        for arg_name, spec in args.items():
            typ = spec.get("type", "any")
            if "default" in spec:
                parts.append(f"{arg_name}: {typ}={spec['default']!r}")
            else:
                parts.append(f"{arg_name}: {typ}")
        signature = ", ".join(parts)
        first_line = (t.description or "").strip().splitlines()[0] if t.description else ""
        lines.append(f"- {t.name}({signature}) — {first_line}")
    return "\n".join(lines)


__all__ = ["make_tools", "make_tools_for_task", "tool_reference"]
