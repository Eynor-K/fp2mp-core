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
import importlib
import subprocess
import sys
import textwrap
from pathlib import Path

from langchain_core.tools import tool

from fp2mp_core.config import BASE_DIR, get_settings

_EXEC_TIMEOUT = 60  # seconds — increased to allow network calls
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
    error = _ast_safety_check(code)
    if error:
        return f"Security check failed: {error}"

    settings = get_settings()
    if settings.e2b_api_key:
        result = _execute_e2b(code)
        if not result.startswith("E2B error"):
            return result

    return _execute_subprocess(code)


@tool
def check_available_data_tool(pattern: str = "*") -> str:
    """
    List files available in the local DATA_DIR.
    Use to check if relevant local files exist before fetching from the network.
    """
    data_path = BASE_DIR / "data"
    try:
        files = list(data_path.rglob(pattern))
        if not files:
            return f"No files matching '{pattern}' in DATA_DIR ({data_path})."
        return "\n".join(str(f.relative_to(data_path)) for f in files[:50])
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
                importlib.import_module(lib)
                available.append(lib)
            except ImportError:
                available.append(f"{lib} [NOT INSTALLED]")
        lines.append(f"{category}: {', '.join(available)}")

    lines.append(
        "\nNote: DATA_DIR variable is always available in execute_python_tool."
    )
    return "\n".join(lines)
