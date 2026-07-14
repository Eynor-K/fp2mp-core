"""Runtime switches for component-level ablation experiments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fp2mp_core.state import BlackBoard


@dataclass(frozen=True)
class AblationConfig:
    """Enabled components for an fp2mp-core run.

    Every field defaults to ``True`` to preserve the production pipeline.
    """

    redi: bool = True
    blackboard: bool = True
    critic: bool = True
    tools: bool = True


def get_ablation(state: BlackBoard) -> AblationConfig:
    """Return the run configuration, preserving compatibility with legacy state."""
    return state.get("ablation") or AblationConfig()
