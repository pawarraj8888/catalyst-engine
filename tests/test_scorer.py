"""Tests for the scoring engine."""

from __future__ import annotations

from pathlib import Path

from catalyst_engine.scoring.scorer import (
    ScoringConfig,
    ScoringRule,
    evaluate_rule,
    load_scoring_config,
    score_setup,
)

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def test_load_real_scoring_yaml() -> None:
    """The committed scoring.yaml loads cleanly."""
    config = load_scoring_config()
    assert config.version >= 1
    assert config.high_conviction_threshold > 0
    assert "earnings" in config.rules_by_catalyst
    assert len(config.rules_by_catalyst["earnings"]) >= 4


def test_load_custom_yaml(tmp_path: Path) -> None:
    yaml_path = tmp_path / "scoring.yaml"
    yaml_path.write_text(
        """
version: 2
high_conviction_threshold: 5.5
earnings:
  always_fires:
    description: Always True
    condition: True
    weight: 1.0
"""
    )
    cfg = load_scoring_config(yaml_path)
    assert cfg.version == 2
    assert cfg.high_conviction_threshold == 5.5
    assert len(cfg.rules_for("earnings")) == 1
    assert cfg.rules_for("earnings")[0].name == "always_fires"


def test_unknown_catalyst_returns_empty_rule_list() -> None:
    cfg = load_scoring_config()
    assert cfg.rules_for("nonexistent") == []


# ---------------------------------------------------------------------------
# Rule evaluation — basics
# ---------------------------------------------------------------------------


def _rule(condition: str, weight: float = 1.0) -> ScoringRule:
    return ScoringRule(name="r", description="", condition=condition, weight=weight)


def test_evaluate_rule_truthy() -> None:
    assert evaluate_rule(_rule("x > 5"), {"x": 10}) is True


def test_evaluate_rule_falsy() -> None:
    assert evaluate_rule(_rule("x > 5"), {"x": 1}) is False


def test_evaluate_rule_none_handling() -> None:
    """The 'is not None and' pattern from real scoring.yaml must work."""
    assert evaluate_rule(_rule("x is not None and x < 0.5"), {"x": 0.3}) is True
    assert evaluate_rule(_rule("x is not None and x < 0.5"), {"x": None}) is False


def test_evaluate_rule_missing_key_does_not_crash() -> None:
    """Missing feature => rule treated as not-fired, no exception."""
    assert evaluate_rule(_rule("missing_var > 5"), {}) is False


def test_evaluate_rule_uses_allowed_builtins() -> None:
    assert evaluate_rule(_rule("abs(x) > 2"), {"x": -3}) is True
    assert evaluate_rule(_rule("len(items) >= 3"), {"items": [1, 2, 3]}) is True


# ---------------------------------------------------------------------------
# Rule evaluation — SANDBOXING (security-critical)
# ---------------------------------------------------------------------------


def test_evaluate_rule_blocks_imports() -> None:
    """A malicious rule attempting import should not fire and not raise."""
    rule = _rule("__import__('os').system('echo PWNED')")
    assert evaluate_rule(rule, {}) is False


def test_evaluate_rule_blocks_file_access() -> None:
    rule = _rule("open('/etc/passwd').read()")
    assert evaluate_rule(rule, {}) is False


def test_evaluate_rule_blocks_eval() -> None:
    rule = _rule("eval('1+1')")
    assert evaluate_rule(rule, {}) is False


# ---------------------------------------------------------------------------
# Full setup scoring
# ---------------------------------------------------------------------------


def _config_with_rules(rules: list[ScoringRule]) -> ScoringConfig:
    return ScoringConfig(
        version=1,
        high_conviction_threshold=5.0,
        rules_by_catalyst={"earnings": rules},
    )


def test_score_sums_weights_of_fired_rules() -> None:
    rules = [
        _rule("x > 0", weight=2.0),
        _rule("y > 0", weight=3.0),
        _rule("z > 0", weight=1.0),
    ]
    rules[0] = ScoringRule(name="r1", description="", condition="x > 0", weight=2.0)
    rules[1] = ScoringRule(name="r2", description="", condition="y > 0", weight=3.0)
    rules[2] = ScoringRule(name="r3", description="", condition="z > 0", weight=1.0)

    cfg = _config_with_rules(rules)
    result = score_setup({"x": 1, "y": 1, "z": -1}, catalyst_type="earnings", config=cfg)
    assert result.score == 5.0
    assert set(result.rules_fired) == {"r1", "r2"}
    assert result.score_components == {"r1": 2.0, "r2": 3.0}


def test_score_capped_at_10() -> None:
    rules = [
        ScoringRule(name=f"r{i}", description="", condition="True", weight=5.0) for i in range(5)
    ]
    cfg = _config_with_rules(rules)
    result = score_setup({}, catalyst_type="earnings", config=cfg)
    assert result.score == 10.0  # capped, not 25


def test_score_zero_when_no_rules_fire() -> None:
    rules = [ScoringRule(name="r", description="", condition="False", weight=5.0)]
    cfg = _config_with_rules(rules)
    result = score_setup({}, catalyst_type="earnings", config=cfg)
    assert result.score == 0.0
    assert result.rules_fired == []


def test_score_unknown_catalyst_returns_zero() -> None:
    cfg = _config_with_rules([])
    result = score_setup({}, catalyst_type="fda", config=cfg)
    assert result.score == 0.0


def test_real_yaml_rules_evaluate_against_realistic_features() -> None:
    """Smoke test: the committed scoring.yaml rules don't crash on a typical
    feature dict from build_features_for_event."""
    cfg = load_scoring_config()
    features = {
        "ticker": "AAPL",
        "n_prior_events": 12,
        "trailing_3q_median_ratio": 0.5,  # should fire vol_compression_3q
        "trailing_3q_all_below_05": False,
        "last_ratio": 1.1,
        "trailing_median": 0.025,
        "median_prior_abs_move": 0.02,
    }
    result = score_setup(features, catalyst_type="earnings", config=cfg)
    assert result.score > 0
    assert "vol_compression_3q" in result.rules_fired
    # Also has deep history rules
    assert "has_history" in result.rules_fired
    assert "has_deep_history" in result.rules_fired
