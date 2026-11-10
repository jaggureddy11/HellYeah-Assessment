"""Unit and contract tests for the simulated delivery environment."""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest

from policy import Policy
from sim.env import World, run

CONFIG_PATH = Path("sim/world_config.json")


def test_same_seed_gives_identical_results() -> None:
    r1 = run(Policy, CONFIG_PATH, seed=123, start_day=61, end_day=90)
    r2 = run(Policy, CONFIG_PATH, seed=123, start_day=61, end_day=90)
    assert r1["total_revenue"] == r2["total_revenue"]
    assert r1["per_day"] == r2["per_day"]

    r3 = run(Policy, CONFIG_PATH, seed=124, start_day=61, end_day=90)
    assert r3["per_day"] != r1["per_day"]


def test_allocation_under_one_dollar_delivers_nothing() -> None:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    world = World(cfg, seed=7)
    # Give CR011 (already launched) $0.99 and CR002 (unserved) $0.99
    rows = world.step_day(61, {"CR011": 0.99, "CR002": 0.99})
    cr002 = [r for r in rows if r.creative_id == "CR002"]
    cr011 = [r for r in rows if r.creative_id == "CR011"]
    assert len(cr002) == 0  # CR002 stays unserved (no row)
    assert len(cr011) == 1
    assert cr011[0].budget == 0.0
    assert cr011[0].spend == 0.0
    assert cr011[0].impressions == 0
    assert cr011[0].clicks == 0
    assert cr011[0].true_purchases == 0
    assert cr011[0].true_revenue == 0.0
    assert world.first_served_day["CR002"] is None


def test_history_non_revenue_columns_immutable_and_revisions_monotonic() -> None:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg["outage_window"]["enabled"] = False
    world = World(cfg, seed=99)

    # Simulate 5 days with positive budget on all creatives so recent days have lag
    budgets = {cid: 40.0 for cid in world.creative_ids}
    snapshots: dict[int, dict[tuple[int, str], dict]] = {}
    for d in range(61, 69):
        world.step_day(d, budgets)
        hist = world.reported_history(current_day=d + 1)
        snapshots[d] = {(r["day"], r["creative_id"]): r for r in hist}

    # Compare day 61 rows as reported at end of day 61 vs day 68
    snap_early = snapshots[61]
    snap_late = snapshots[68]
    for key, early_row in snap_early.items():
        late_row = snap_late[key]
        for col in ("budget", "spend", "impressions", "clicks"):
            assert late_row[col] == early_row[col]
        assert late_row["purchases"] >= early_row["purchases"]
        assert late_row["revenue"] >= early_row["revenue"]


def test_config_changes_affect_world_without_code_changes() -> None:
    cfg_base = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg_fast_fatigue = copy.deepcopy(cfg_base)
    for c in cfg_fast_fatigue["creatives"]:
        c["fatigue_rate"] = 0.08

    res_base = run(Policy, cfg_base, seed=55, start_day=61, end_day=90)
    res_fast = run(Policy, cfg_fast_fatigue, seed=55, start_day=61, end_day=90)
    assert res_fast["total_revenue"] < res_base["total_revenue"]


def test_policy_does_not_import_sim() -> None:
    source = Path("policy.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("sim")
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("sim")


def test_negative_shares_rejected_and_all_zero_is_uniform() -> None:
    class BadNegPolicy:
        def __init__(self, creatives, daily_budget, seed):
            self.ids = [c["creative_id"] for c in creatives]

        def allocate(self, day, history):
            return {self.ids[0]: -0.1, self.ids[1]: 1.1}

    with pytest.raises(ValueError):
        run(BadNegPolicy, CONFIG_PATH, seed=1, start_day=61, end_day=61)

    class ZeroPolicy:
        def __init__(self, creatives, daily_budget, seed):
            pass

        def allocate(self, day, history):
            return {"UNKNOWN_ID": 1.0}  # only unknown -> all valid shares 0 -> uniform

    res = run(ZeroPolicy, CONFIG_PATH, seed=1, start_day=61, end_day=61)
    assert res["per_day"][0]["allocated_budget"] == pytest.approx(2000.0, abs=1e-6)
