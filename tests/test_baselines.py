"""Unit and integration tests for B1, B2, Clairvoyant, and sim.env compatibility."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from policy import Policy
from sim.baselines import B1Uniform, B2WindowedGreedy
from sim.clairvoyant import Clairvoyant
from sim.env import run

CONFIG_PATH = Path("sim/world_config.json")


def test_b2_windowed_greedy_split_cold_start_and_ties() -> None:
    # 7 creatives: CR001..CR007
    creatives = [{"creative_id": f"CR00{i}"} for i in range(1, 8)]
    # Day = 20 -> trailing 7-day window is days 13..19 (inclusive).
    # Day 12 row should be ignored.
    history = [
        # Ignored old day 12
        {"day": 12, "creative_id": "CR007", "spend": 100.0, "impressions": 5000, "revenue": 9999.0},
        # CR001: 3000 impr, spend=100, rev=400 -> rate=4.0
        {"day": 15, "creative_id": "CR001", "spend": 100.0, "impressions": 3000, "revenue": 400.0},
        # CR002: 2500 impr, spend=100, rev=200 -> rate=2.0
        {"day": 16, "creative_id": "CR002", "spend": 100.0, "impressions": 2500, "revenue": 200.0},
        # CR003: < 2000 impr (1999) -> fallback to pop mean = (400+200+100+200+100)/(100+100+100+100+100) = 1000/500 = 2.0
        {"day": 17, "creative_id": "CR003", "spend": 100.0, "impressions": 1999, "revenue": 100.0},
        # CR004: 2000 impr, spend=100, rev=200 -> rate=2.0 (tie with CR002, CR003, CR005)
        {"day": 18, "creative_id": "CR004", "spend": 100.0, "impressions": 2000, "revenue": 200.0},
        # CR005: 0 spend -> fallback to pop mean = 2.0
        {"day": 19, "creative_id": "CR005", "spend": 0.0, "impressions": 5000, "revenue": 0.0},
        # CR006: 3000 impr, spend=100, rev=100 -> rate=1.0
        {"day": 19, "creative_id": "CR006", "spend": 100.0, "impressions": 3000, "revenue": 100.0},
        # CR007: no rows in trailing window -> 0 spend -> fallback to pop mean = 2.0
    ]
    # Rates: CR001=4.0, CR002=2.0, CR003=2.0, CR004=2.0, CR005=2.0, CR007=2.0, CR006=1.0
    # Tie-break ascending creative_id -> Top 5 are CR001, CR002, CR003, CR004, CR005;
    # Bottom 2 are CR007 and CR006.
    b2 = B2WindowedGreedy(creatives=creatives, daily_budget=2000.0, seed=1)
    shares = b2.allocate(day=20, history=history)

    assert sum(shares.values()) == pytest.approx(1.0, abs=1e-9)
    for cid in ["CR001", "CR002", "CR003", "CR004", "CR005"]:
        assert shares[cid] == pytest.approx(0.75 / 5.0, abs=1e-9)
    for cid in ["CR006", "CR007"]:
        assert shares[cid] == pytest.approx(0.25 / 2.0, abs=1e-9)


def test_clairvoyant_beats_or_equals_b1_and_all_run_through_env() -> None:
    world_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    for seed in (10, 11, 12):
        r_b1 = run(B1Uniform, world_cfg, seed=seed, start_day=61, end_day=90)
        r_b2 = run(B2WindowedGreedy, world_cfg, seed=seed, start_day=61, end_day=90)
        r_cl = run(Clairvoyant, world_cfg, seed=seed, start_day=61, end_day=90)
        r_po = run(Policy, world_cfg, seed=seed, start_day=61, end_day=90)

        assert r_cl["total_revenue"] >= r_b1["total_revenue"]
        assert r_b2["total_revenue"] > 0.0
        assert r_po["total_revenue"] == pytest.approx(r_b1["total_revenue"], abs=1e-6)

