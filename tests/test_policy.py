"""Unit and contract tests for the root Policy class."""

from __future__ import annotations

import ast
import csv
import math
from pathlib import Path

import pytest

import policy as policy_mod
from policy import Policy

CREATIVES_CSV = Path(__file__).resolve().parent.parent / "data" / "creatives.csv"


def _load_creatives() -> list[dict[str, str]]:
    with CREATIVES_CSV.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_split_contract_min_dollar_and_idempotency() -> None:
    creatives = _load_creatives()
    assert len(creatives) == 50

    pol = Policy(creatives=creatives, daily_budget=2000.0, seed=7, options={"log_stderr": False})
    first = pol.allocate(day=61, history=[])

    assert len(first) == 50
    assert set(first.keys()) == {row["creative_id"] for row in creatives}
    assert all(math.isfinite(v) and v >= 0.0 for v in first.values())
    assert sum(first.values()) == pytest.approx(1.0, abs=1e-9)
    # No sub-$1 non-zero allocations
    for share in first.values():
        dollars = share * 2000.0
        assert dollars == 0.0 or dollars >= 1.0 - 1e-6

    repeated = pol.allocate(day=61, history=[])
    assert repeated == first
    expl = pol.explain(61)
    assert len(expl) == 50


def test_garbage_and_malformed_history_do_not_crash() -> None:
    creatives = _load_creatives()
    pol = Policy(creatives=creatives, daily_budget=2000.0, seed=11, options={"log_stderr": False})

    malformed_histories = [
        None,
        "not-a-list",
        [
            None,
            123,
            {"bad": "row"},
            {"day": "abc", "creative_id": "CR001", "spend": 10, "revenue": 20},
            {"day": 999, "creative_id": "CR001", "spend": 10, "revenue": 9999},  # future leak ignored
            {"day": 60, "creative_id": "UNKNOWN", "spend": 50, "revenue": 500},
            {"day": 60, "creative_id": "CR001", "spend": float("nan"), "revenue": -50},
            {"day": 60, "creative_id": "CR002", "spend": -10.0, "revenue": 100.0},
            # Duplicate row (should keep latest without crashing)
            {"day": 59, "creative_id": "CR003", "budget": 40, "spend": 40, "impressions": 1000, "clicks": 10, "purchases": 1, "revenue": 50},
            {"day": 59, "creative_id": "CR003", "budget": 40, "spend": 40, "impressions": 1000, "clicks": 10, "purchases": 2, "revenue": 110},
        ],
    ]
    for idx, hist in enumerate(malformed_histories):
        out = pol.allocate(day=61 + idx, history=hist)  # type: ignore[arg-type]
        assert len(out) == 50
        assert all(math.isfinite(v) and v >= 0.0 for v in out.values())
        assert sum(out.values()) == pytest.approx(1.0, abs=1e-9)


def test_lag_correction_raises_recent_under_reported_day() -> None:
    creatives = [
        {"creative_id": "C_RECENT", "format": "carousel"},
        {"creative_id": "C_MATURE", "format": "carousel"},
    ]
    # Both spent $100 and currently show $100 revenue, but C_RECENT is from day 60 (age=1 on day 61)
    # while C_MATURE is from day 45 (age=16, fully matured).
    history = [
        {"day": 60, "creative_id": "C_RECENT", "budget": 100.0, "spend": 100.0, "impressions": 4000, "clicks": 60, "purchases": 2, "revenue": 100.0},
        {"day": 45, "creative_id": "C_MATURE", "budget": 100.0, "spend": 100.0, "impressions": 4000, "clicks": 60, "purchases": 2, "revenue": 100.0},
    ]
    pol_lag = Policy(
        creatives,
        200.0,
        seed=1,
        options={"lag_correction": True, "exploration": False, "recency_decay": False, "max_share": 0.8, "log_stderr": False},
    )
    pol_nolag = Policy(
        creatives,
        200.0,
        seed=1,
        options={"lag_correction": False, "exploration": False, "recency_decay": False, "max_share": 0.8, "log_stderr": False},
    )

    s_lag = pol_lag.allocate(61, history)
    s_nolag = pol_nolag.allocate(61, history)

    e_lag = pol_lag.explain(61)
    e_nolag = pol_nolag.explain(61)

    assert e_lag["C_RECENT"]["lag_correction_applied"] is True
    assert e_lag["C_RECENT"]["estimated_roas"] > e_nolag["C_RECENT"]["estimated_roas"]
    assert s_lag["C_RECENT"] > s_nolag["C_RECENT"]


def test_cold_start_creative_uses_format_prior() -> None:
    creatives = [
        {"creative_id": "C_STORY", "format": "story"},
        {"creative_id": "C_STATIC", "format": "static_image"},
    ]
    pol = Policy(
        creatives,
        200.0,
        seed=1,
        options={"format_prior": True, "exploration": False, "max_share": 0.8, "log_stderr": False},
    )
    shares = pol.allocate(61, history=[])
    expl = pol.explain(61)

    assert expl["C_STORY"]["prior_source"] == "format"
    assert expl["C_STATIC"]["prior_source"] == "format"
    assert expl["C_STORY"]["prior_roas"] > expl["C_STATIC"]["prior_roas"]
    assert shares["C_STORY"] > shares["C_STATIC"]


def test_policy_never_reads_or_imports_sim(monkeypatch: pytest.MonkeyPatch) -> None:
    source = Path("policy.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("sim")
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("sim")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # Ignore module/function docstrings; forbid any code string referencing world_config or sim/
            if "Never imports from sim/" not in node.value:
                assert "world_config" not in node.value
                assert "sim/" not in node.value

    # Also verify fallback works if policy_priors.json is absent
    monkeypatch.setattr(policy_mod, "PRIORS_PATH", Path("/nonexistent/policy_priors.json"))
    pol = Policy([{"creative_id": "C1", "format": "story"}], 2000.0, seed=3, options={"log_stderr": False})
    out = pol.allocate(61, [])
    assert out == {"C1": pytest.approx(1.0)}
