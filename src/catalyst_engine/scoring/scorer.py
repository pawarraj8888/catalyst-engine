"""YAML-driven scoring engine.

Design rules
------------
1. Inspectable. Every score traces back to a list of `(rule_name, weight)`
   pairs. A PM should be able to look at a 7.5 and see exactly which rules
   fired and why.
2. Deterministic. Same inputs => same score, every time.
3. Safe. Rule conditions in YAML are evaluated against a feature dict in a
   sandbox with no builtins. No filesystem, no network, no imports.
4. Decoupled from data layer. The scorer takes a *feature dict* as input.
   How features are computed lives in features/*.py and catalysts/*.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from catalyst_engine.config import get_settings
from catalyst_engine.utils.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class ScoringRule:
    """A single rule as parsed from scoring.yaml."""

    name: str
    description: str
    condition: str  # Python expression evaluated against features
    weight: float
    direction: str = "two_way"


@dataclass(frozen=True)
class ScoringConfig:
    """The full scoring configuration loaded from YAML."""

    version: int
    high_conviction_threshold: float
    rules_by_catalyst: dict[str, list[ScoringRule]]

    def rules_for(self, catalyst_type: str) -> list[ScoringRule]:
        return self.rules_by_catalyst.get(catalyst_type, [])


@dataclass
class ScoreResult:
    """Output of scoring a single setup."""

    score: float
    rules_fired: list[str] = field(default_factory=list)
    score_components: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_scoring_config(path: Path | None = None) -> ScoringConfig:
    """Load scoring.yaml into a typed config object."""
    if path is None:
        path = get_settings().project_root / "config" / "scoring.yaml"

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    rules_by_catalyst: dict[str, list[ScoringRule]] = {}
    for key, value in raw.items():
        if key in {"version", "last_updated", "high_conviction_threshold"}:
            continue
        if not isinstance(value, dict):
            continue
        rules: list[ScoringRule] = []
        for rule_name, body in value.items():
            if not isinstance(body, dict) or "condition" not in body:
                continue
            rules.append(
                ScoringRule(
                    name=rule_name,
                    description=body.get("description", ""),
                    condition=body["condition"],
                    weight=float(body.get("weight", 0.0)),
                    direction=body.get("direction", "two_way"),
                )
            )
        rules_by_catalyst[key] = rules

    config = ScoringConfig(
        version=raw.get("version", 1),
        high_conviction_threshold=float(raw.get("high_conviction_threshold", 7.0)),
        rules_by_catalyst=rules_by_catalyst,
    )
    log.info(
        "scoring_config_loaded",
        version=config.version,
        threshold=config.high_conviction_threshold,
        n_rules={k: len(v) for k, v in rules_by_catalyst.items()},
    )
    return config


# ---------------------------------------------------------------------------
# Rule evaluation
# ---------------------------------------------------------------------------


# Names allowed inside rule condition expressions. Anything not in this set
# (and not in the feature dict) is an undefined name and will raise NameError.
_ALLOWED_BUILTINS = {
    "True": True,
    "False": False,
    "None": None,
    "abs": abs,
    "min": min,
    "max": max,
    "len": len,
    "all": all,
    "any": any,
    "sum": sum,
}


def evaluate_rule(rule: ScoringRule, features: dict[str, Any]) -> bool:
    """Evaluate a rule's condition against a feature dict.

    Conditions are arbitrary Python expressions, restricted to:
    - The keys present in `features`
    - The whitelist in `_ALLOWED_BUILTINS`

    Returns True if the rule fires. Errors (missing names, type errors) are
    logged and treated as not-fired so a malformed rule never crashes a
    whole backtest.
    """
    # The eval globals dict is locked down: no __builtins__ access
    eval_globals: dict[str, Any] = {"__builtins__": {}}
    eval_globals.update(_ALLOWED_BUILTINS)

    try:
        result = eval(rule.condition, eval_globals, features)
    except Exception as exc:
        log.debug(
            "rule_evaluation_error",
            rule=rule.name,
            condition=rule.condition,
            error=str(exc),
        )
        return False

    return bool(result)


def score_setup(
    features: dict[str, Any],
    *,
    catalyst_type: str,
    config: ScoringConfig,
    score_cap: float = 10.0,
) -> ScoreResult:
    """Apply all rules for a catalyst type to a feature dict.

    Returns a ScoreResult with:
    - final score (sum of weights, capped at score_cap)
    - which rules fired
    - per-rule contribution map

    A score is always returned, even when zero rules fire (score=0).
    """
    rules = config.rules_for(catalyst_type)
    result = ScoreResult(score=0.0)

    for rule in rules:
        if evaluate_rule(rule, features):
            result.rules_fired.append(rule.name)
            result.score_components[rule.name] = rule.weight
            result.score += rule.weight

    if result.score > score_cap:
        result.score = score_cap

    return result
