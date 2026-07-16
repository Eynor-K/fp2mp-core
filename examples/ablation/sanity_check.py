"""Sanity-check: imports + AblationConfig + BaseState fields."""
from fp2mp_core.ablation import AblationConfig
from fp2mp_core.graph import run
import inspect

print("AblationConfig fields:", list(AblationConfig.__dataclass_fields__.keys()))
print("run signature:", inspect.signature(run))
