"""Per-run artifact context and logging."""

from __future__ import annotations

import json
import mimetypes
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_current: "RunContext | None" = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


class RunLogger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.saved_files: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []

    def record_file(
        self,
        path: str | Path,
        kind: str,
        source: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        p = Path(path)
        entry: dict[str, Any] = {
            "path": str(p),
            "kind": kind,
            "size": _safe_size(p),
            "mime": mimetypes.guess_type(str(p))[0] or "",
            "meta": meta or {},
        }
        if source:
            entry["source"] = source
        with self._lock:
            if not any(item.get("path") == entry["path"] for item in self.saved_files):
                self.saved_files.append(entry)

    def record_tool_call(self, agent: str, tool: str, args: Any, observation: str) -> None:
        with self._lock:
            self.tool_calls.append({
                "agent": agent,
                "tool": tool,
                "args": args,
                "observation": str(observation)[:1000],
            })

    def to_json(self) -> dict[str, Any]:
        with self._lock:
            return {
                "saved_files": list(self.saved_files),
                "tool_calls": list(self.tool_calls),
            }


@dataclass(frozen=True)
class RunContext:
    run_id: str
    run_dir: Path
    maps_dir: Path
    images_dir: Path
    figures_dir: Path
    logger: RunLogger = field(default_factory=RunLogger)
    started_at: str = field(default_factory=_now_iso)


def start_run(base_output_dir: Path) -> RunContext:
    global _current
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    run_dir = base_output_dir / f"run_{run_id}"
    maps_dir = run_dir / "maps"
    images_dir = run_dir / "images"
    figures_dir = run_dir / "figures"
    for path in (maps_dir, images_dir, figures_dir):
        path.mkdir(parents=True, exist_ok=True)
    _current = RunContext(
        run_id=run_id,
        run_dir=run_dir,
        maps_dir=maps_dir,
        images_dir=images_dir,
        figures_dir=figures_dir,
    )
    return _current


def get_run_context() -> RunContext | None:
    return _current


def get_run_dir(fallback: str | Path) -> Path:
    ctx = get_run_context()
    if ctx:
        return ctx.run_dir
    path = Path(fallback)
    path.mkdir(parents=True, exist_ok=True)
    return path


def record_file(
    path: str | Path,
    kind: str,
    source: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    ctx = get_run_context()
    if ctx:
        ctx.logger.record_file(path, kind, source=source, meta=meta)


def collect_run_files(run_dir: Path, known: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = {item.get("path") for item in known}
    files = list(known)
    if not run_dir.exists():
        return files
    for path in sorted(p for p in run_dir.rglob("*") if p.is_file()):
        spath = str(path)
        if spath in seen:
            continue
        suffix = path.suffix.lower()
        if suffix == ".csv":
            kind = "csv"
        elif path.parent.name == "maps" and suffix in {".png", ".jpg", ".jpeg", ".svg"}:
            kind = "map"
        elif path.parent.name == "figures" and suffix in {".png", ".jpg", ".jpeg", ".svg"}:
            kind = "code_image"
        elif path.parent.name == "images":
            kind = "web_image"
        elif path.name.startswith("run_log"):
            kind = "log"
        else:
            kind = "file"
        files.append({
            "path": spath,
            "kind": kind,
            "size": _safe_size(path),
            "mime": mimetypes.guess_type(str(path))[0] or "",
            "meta": {},
        })
    return files


def write_logs(
    ctx: RunContext,
    question: str,
    model: str,
    final_answer: str,
    tool_calls: list[dict[str, Any]],
    agent_outputs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    logger_data = ctx.logger.to_json()
    saved_files = collect_run_files(ctx.run_dir, logger_data["saved_files"])
    payload = {
        "run_id": ctx.run_id,
        "question": question,
        "model": model,
        "started_at": ctx.started_at,
        "finished_at": _now_iso(),
        "tool_calls": tool_calls or logger_data["tool_calls"],
        "agent_outputs": agent_outputs or [],
        "saved_files": saved_files,
        "final_answer": final_answer,
    }
    json_path = ctx.run_dir / "run_log.json"
    md_path = ctx.run_dir / "run_log.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_to_markdown(payload), encoding="utf-8")
    ctx.logger.record_file(json_path, "log")
    ctx.logger.record_file(md_path, "log")
    payload["log_json"] = str(json_path)
    payload["log_md"] = str(md_path)
    payload["saved_files"] = collect_run_files(ctx.run_dir, ctx.logger.to_json()["saved_files"])
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_to_markdown(payload), encoding="utf-8")
    return payload


def _to_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Run Log",
        "",
        f"- Run ID: `{payload.get('run_id', '')}`",
        f"- Model: `{payload.get('model', '')}`",
        f"- Started: `{payload.get('started_at', '')}`",
        f"- Finished: `{payload.get('finished_at', '')}`",
        "",
        "## Question",
        str(payload.get("question", "")),
        "",
        "## Tool Calls",
        "| Agent | Tool | Args | Observation |",
        "|---|---|---|---|",
    ]
    for call in payload.get("tool_calls", []):
        lines.append(
            "| {agent} | `{tool}` | `{args}` | {obs} |".format(
                agent=_md_cell(call.get("agent", "")),
                tool=_md_cell(call.get("tool", "")),
                args=_md_cell(str(call.get("args", ""))[:200]),
                obs=_md_cell(str(call.get("observation", ""))[:240]),
            )
        )
    lines.extend(["", "## Agent Outputs"])
    for output in payload.get("agent_outputs", []):
        label = (
            f"{output.get('agent', '')} / {output.get('type', '')} / "
            f"{output.get('sub_query_id', '')}"
        )
        lines.extend([
            "",
            f"### {_md_cell(label)}",
            f"- Confidence: `{output.get('confidence', 0.0)}`",
            f"- Promotion: `{output.get('promotion', 'unknown')}`",
            "",
            "```text",
            str(output.get("content", "")),
            "```",
        ])
    lines.extend(["", "## Saved Files"])
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for item in payload.get("saved_files", []):
        by_kind.setdefault(item.get("kind", "file"), []).append(item)
    for kind, items in sorted(by_kind.items()):
        lines.extend(["", f"### {kind}", "| Path | Size | Source |", "|---|---:|---|"])
        for item in items:
            lines.append(
                f"| `{_md_cell(item.get('path', ''))}` | {item.get('size', 0)} | {_md_cell(item.get('source', ''))} |"
            )
    lines.extend(["", "## Final Answer", str(payload.get("final_answer", ""))])
    return "\n".join(lines)


def _md_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")
