"""
Code execution tool with two backends:
  1. E2B sandbox (if E2B_API_KEY configured) — isolated, networked
  2. Subprocess (fallback) — network allowed, AST safety check

Security model:
  - Blocks shell escape: subprocess, eval, exec, __import__, compile
  - Blocks dangerous os attributes: os.system, os.popen, os.execv, os.fork, os.kill
  - Allows: requests, httpx, urllib, osmnx, geopandas, geopy, pandas, numpy, etc.
"""

from __future__ import annotations

import ast
import difflib
import importlib
import inspect
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from langchain_core.tools import tool

from fp2mp_core.config import BASE_DIR, get_settings

_EXEC_TIMEOUT = 120  # seconds — allow slow OSM/geocoding network calls
_DATA_DIR = str(BASE_DIR / "data")

# Абсолютно запрещено: побег из процесса и динамическое выполнение
_FORBIDDEN_MODULES = {"subprocess", "eval", "exec", "__import__", "compile", "pty", "pexpect"}

# Атрибуты os, которые открывают shell / порождают процессы
_FORBIDDEN_OS_ATTRS = {"system", "popen", "execv", "execve", "execvp", "execvpe",
                       "fork", "forkpty", "kill", "killpg", "spawnl", "spawnle",
                       "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp"}

# Библиотеки явно разрешённые для пространственного анализа и загрузки данных
_ALLOWED_DATA_LIBRARIES = [
    "requests", "httpx", "urllib",
    "osmnx", "geopandas", "shapely", "pyproj", "fiona",
    "overpy", "geopy",
    "pandas", "numpy", "scipy", "sklearn",
    "json", "math", "statistics", "itertools", "collections",
    "pathlib", "re", "datetime",
    "networkx", "rtree",
]

_API_CHECK_MODULES = {
    "osmnx",
    "geopandas",
    "pandas",
    "networkx",
    "shapely",
    "numpy",
    "geopy",
    "requests",
    "httpx",
}


def _strip_markdown_code_fence(code: str) -> str:
    """Allow agents to pass fenced code without breaking AST parsing."""
    stripped = code.strip()
    if not stripped.startswith("```"):
        return code

    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


def _print_last_expression(code: str) -> str:
    """Mirror notebook behavior: print a final bare expression in agent snippets."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code

    if not tree.body or not isinstance(tree.body[-1], ast.Expr):
        return code

    last_expr = tree.body[-1]
    if (
        isinstance(last_expr.value, ast.Call)
        and isinstance(last_expr.value.func, ast.Name)
        and last_expr.value.func.id == "print"
    ):
        return code

    tree.body[-1] = ast.Expr(
        value=ast.Call(
            func=ast.Name(id="print", ctx=ast.Load()),
            args=[last_expr.value],
            keywords=[],
        )
    )
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def _ast_safety_check(code: str) -> str | None:
    """Return error message if code contains forbidden constructs, else None."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"Syntax error: {exc}"

    for node in ast.walk(tree):
        # Запрет запрещённых модулей верхнего уровня
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name.split(".")[0]
                if name in _FORBIDDEN_MODULES:
                    return f"Import of '{name}' is not allowed in the sandbox."

        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            if module in _FORBIDDEN_MODULES:
                return f"Import from '{module}' is not allowed in the sandbox."

        # Запрет вызовов запрещённых функций напрямую
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_MODULES:
                return f"Call to '{node.func.id}' is not allowed in the sandbox."

        # Запрет os.system / os.popen / os.execv и т.д.
        elif isinstance(node, ast.Attribute):
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "os"
                and node.attr in _FORBIDDEN_OS_ATTRS
            ):
                return f"Call to 'os.{node.attr}' is not allowed in the sandbox."

    return None


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _API_CHECK_MODULES:
                    aliases[alias.asname or root] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root in _API_CHECK_MODULES:
                for alias in node.names:
                    if alias.name != "*":
                        aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _resolve_dotted_name(dotted_name: str) -> object:
    parts = dotted_name.split(".")
    module = importlib.import_module(parts[0])
    obj: object = module
    for part in parts[1:]:
        obj = getattr(obj, part)
    return obj


def _api_existence_check(code: str) -> str | None:
    """Catch hallucinated module.attr calls before executing code."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"Syntax error: {exc}"

    aliases = _import_aliases(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
            continue
        alias = node.value.id
        if alias not in aliases:
            continue
        dotted_base = aliases[alias]
        root = dotted_base.split(".")[0]
        if root not in _API_CHECK_MODULES:
            continue
        try:
            obj = _resolve_dotted_name(dotted_base)
        except Exception:
            continue
        if hasattr(obj, node.attr):
            continue
        suggestions = difflib.get_close_matches(node.attr, dir(obj), n=3)
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        return f"API check failed: '{dotted_base}.{node.attr}' does not exist.{hint}"
    return None


def _ruff_pyflakes_check(code: str) -> str | None:
    """Run ruff F rules when available; skip if ruff is not installed."""
    wrapped = f"DATA_DIR = {_DATA_DIR!r}\n" + code
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "snippet.py"
            path.write_text(wrapped, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "ruff", "check", "--select", "F", str(path)],
                capture_output=True,
                text=True,
                timeout=20,
            )
    except Exception:
        return None

    combined = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        return None
    if "No module named ruff" in combined:
        return None
    return "Pyflakes check failed before execution:\n" + combined[:1200]


def _pre_execution_check(code: str) -> str | None:
    for check in (_ast_safety_check, _api_existence_check, _ruff_pyflakes_check):
        error = check(code)
        if error:
            return error
    return None


def _execute_subprocess(code: str) -> str:
    """Execute code in a subprocess with network access."""
    wrapped = textwrap.dedent(f"""\
DATA_DIR = {_DATA_DIR!r}

{code}
""")
    try:
        result = subprocess.run(
            [sys.executable, "-c", wrapped],
            capture_output=True,
            text=True,
            timeout=_EXEC_TIMEOUT,
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr[:800]}"
        return output[:4000] if output else "(no output)"
    except subprocess.TimeoutExpired:
        return f"Execution timed out after {_EXEC_TIMEOUT}s."
    except Exception as exc:
        return f"Subprocess error: {exc}"


def _execute_e2b(code: str) -> str:
    """Execute code in E2B sandbox (networked, isolated)."""
    try:
        from e2b_code_interpreter import Sandbox  # type: ignore[import-untyped]
        settings = get_settings()
        with Sandbox(api_key=settings.e2b_api_key) as sandbox:
            execution = sandbox.run_code(code)
            output = "\n".join(str(o) for o in execution.logs.stdout)
            if execution.logs.stderr:
                output += "\n[stderr]\n" + "\n".join(execution.logs.stderr)[:500]
            return output[:4000] if output else "(no output)"
    except Exception as exc:
        return f"E2B error: {exc}"


@tool
def execute_python_tool(code: str) -> str:
    """
    Execute a Python code snippet and return stdout + stderr.

    Network access is ALLOWED. You can use:
    - osmnx for OpenStreetMap spatial data and routing
    - geopandas, shapely for geometric operations
    - geopy for geocoding addresses to coordinates
    - requests, httpx for public APIs and open datasets
    - pandas, numpy for data analysis

    Restrictions (will be blocked):
    - subprocess, eval, exec, __import__, compile
    - os.system, os.popen, os.execv and other shell-escape calls

    DATA_DIR variable is set to the local data directory.
    Returns output as string (up to 4000 characters).
    """
    code = _print_last_expression(_strip_markdown_code_fence(code))
    error = _pre_execution_check(code)
    if error:
        return f"Validation failed: {error}"

    settings = get_settings()
    if settings.e2b_api_key:
        result = _execute_e2b(code)
        if not result.startswith("E2B error"):
            return result

    return _execute_subprocess(code)


@tool
def run_validated_python(code: str) -> str:
    """
    Validate a Python snippet with safety, API-existence, and pyflakes checks,
    then execute it. Prefer this over execute_python_tool for CodeSpatialAgent.
    """
    return execute_python_tool.invoke({"code": code})


@tool
def inspect_api_tool(dotted_name: str) -> str:
    """
    Inspect an installed Python object by dotted name, returning signature and docstring.
    Example: inspect_api_tool("osmnx.features_from_place").
    """
    try:
        obj = _resolve_dotted_name(dotted_name.strip())
    except Exception as exc:
        return f"Inspection failed: {exc}"

    try:
        signature = str(inspect.signature(obj))
    except Exception:
        signature = "(signature unavailable)"
    doc = inspect.getdoc(obj) or ""
    return f"{dotted_name}{signature}\n\n{doc[:1200]}"


@tool
def check_available_data_tool(pattern: str = "*") -> str:
    """
    List files available in the local DATA_DIR.
    Use to check if relevant local files exist before fetching from the network.
    """
    data_path = BASE_DIR / "data"
    try:
        if pattern in {None, "", "None"}:  # type: ignore[comparison-overlap]
            pattern = "*"
        paths = sorted(data_path.rglob(pattern), key=lambda p: str(p.relative_to(data_path)))
        if not paths:
            return f"No files matching '{pattern}' in DATA_DIR ({data_path})."
        result_lines = []
        for path in paths[:50]:
            rel = path.relative_to(data_path)
            suffix = " [dir]" if path.is_dir() else f" [{path.suffix or 'file'}]"
            result_lines.append(str(rel) + suffix)
        return "\n".join(result_lines)
    except Exception as exc:
        return f"Error listing data: {exc}"


@tool
def list_available_libraries_tool() -> str:
    """
    Check which Python libraries are installed and available for use in code.
    Call this before writing code to know what you can import.
    Returns a list of available libraries grouped by category.
    """
    categories = {
        "Spatial / OSM": ["osmnx", "geopandas", "shapely", "pyproj", "geopy", "networkx"],
        "HTTP / APIs": ["requests", "httpx"],
        "Data analysis": ["pandas", "numpy", "scipy"],
        "Standard library": ["json", "math", "statistics", "urllib", "pathlib", "re"],
    }

    lines = []
    for category, libs in categories.items():
        available = []
        for lib in libs:
            try:
                module = importlib.import_module(lib)
                version = getattr(module, "__version__", "version unknown")
                available.append(f"{lib} ({version})")
            except ImportError:
                available.append(f"{lib} [NOT INSTALLED]")
        lines.append(f"{category}: {', '.join(available)}")

    lines.append(
        "\nNote: DATA_DIR variable is always available in execute_python_tool."
    )
    return "\n".join(lines)
