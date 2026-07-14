"""Ablation configuration and state plumbing tests."""

from fp2mp_core.ablation import AblationConfig, get_ablation
from fp2mp_core.state import create_initial_state


def test_default_ablation_keeps_all_components_enabled():
    assert AblationConfig() == AblationConfig(
        redi=True,
        blackboard=True,
        critic=True,
        tools=True,
    )


def test_initial_state_preserves_explicit_ablation_config():
    config = AblationConfig(redi=False, tools=False)

    state = create_initial_state("question", ablation=config)

    assert state["ablation"] is config
    assert get_ablation(state) is config


def test_get_ablation_uses_all_enabled_default_for_legacy_state():
    assert get_ablation({}) == AblationConfig()
