"""Operational hardening, contract, isolation, and determinism tests for Policy."""

from __future__ import annotations

import ast
import csv
import importlib.util
import math
import shutil
from pathlib import Path

import pytest

from policy import Policy

CREATIVES_CSV = Path(__file__).resolve().parent.parent / "data" / "creatives.csv"


def _load_creatives() -> list[dict[str, str]]:
    with CREATIVES_CSV.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _assert_valid_shares(shares: dict[str, float], expected_ids: list[str], daily_budget: float = 2000.0) -> None:
    assert set(shares.keys()) == set(expected_ids)
    assert len(shares) == len(expected_ids)
    assert all(isinstance(v, float) and math.isfinite(v) and v >= 0.0 for v in shares.values())
    assert sum(shares.values()) == pytest.approx(1.0, abs=1e-9)
    for v in shares.values():
        dollars = v * daily_budget
        assert dollars == 0.0 or dollars >= 1.0 - 1e-6


def test_repeated_day_idempotency_and_no_lag_double_count() -> None:
    creatives = _load_creatives()
    expected_ids = [c["creative_id"] for c in creatives]
    pol = Policy(creatives, 2000.0, seed=42, options={"log_stderr": False})

    # Seed initial days 61..69 so day 60..69 snapshots mature and accumulate online lag state
    hist_base = [
        {"day": 61, "creative_id": "CR001", "budget": 50.0, "spend": 50.0, "impressions": 4000, "clicks": 60, "purchases": 1, "revenue": 40.0},
        {"day": 62, "creative_id": "CR001", "budget": 50.0, "spend": 50.0, "impressions": 4000, "clicks": 60, "purchases": 1, "revenue": 50.0},
    ]
    pol.allocate(63, hist_base)
    hist_70 = [
        {"day": 61, "creative_id": "CR001", "budget": 50.0, "spend": 50.0, "impressions": 4000, "clicks": 60, "purchases": 2, "revenue": 100.0},
        {"day": 62, "creative_id": "CR001", "budget": 50.0, "spend": 50.0, "impressions": 4000, "clicks": 60, "purchases": 2, "revenue": 100.0},
        {"day": 69, "creative_id": "CR001", "budget": 50.0, "spend": 50.0, "impressions": 4000, "clicks": 60, "purchases": 1, "revenue": 30.0},
    ]

    out1 = pol.allocate(70, hist_70)
    snap_keys_1 = dict(pol._obs_by_key)
    matured_1 = set(pol._matured_keys)
    lag_den_1 = dict(pol._online_lag_den)

    out2 = pol.allocate(70, hist_70)
    assert out2 == out1
    assert pol._matured_keys == matured_1
    assert pol._online_lag_den == lag_den_1

    # Third call on day 70 with revised higher revenue on recent row
    hist_70_revised = [
        {"day": 61, "creative_id": "CR001", "budget": 50.0, "spend": 50.0, "impressions": 4000, "clicks": 60, "purchases": 2, "revenue": 100.0},
        {"day": 62, "creative_id": "CR001", "budget": 50.0, "spend": 50.0, "impressions": 4000, "clicks": 60, "purchases": 2, "revenue": 100.0},
        {"day": 69, "creative_id": "CR001", "budget": 50.0, "spend": 50.0, "impressions": 4000, "clicks": 60, "purchases": 3, "revenue": 180.0},
    ]
    out3 = pol.allocate(70, hist_70_revised)
    _assert_valid_shares(out3, expected_ids)
    assert pol._matured_keys == matured_1
    assert pol._online_lag_den == lag_den_1
    assert len(pol._obs_by_key) == len(snap_keys_1)


def test_skipped_and_out_of_order_days() -> None:
    creatives = _load_creatives()
    expected_ids = [c["creative_id"] for c in creatives]

    # Jump from day 61 -> day 64
    pol1 = Policy(creatives, 2000.0, seed=7, options={"log_stderr": False})
    _assert_valid_shares(pol1.allocate(61, []), expected_ids)
    hist_63 = [
        {"day": 61, "creative_id": "CR001", "budget": 40.0, "spend": 40.0, "impressions": 3000, "clicks": 50, "purchases": 2, "revenue": 120.0},
        {"day": 63, "creative_id": "CR002", "budget": 40.0, "spend": 40.0, "impressions": 3000, "clicks": 50, "purchases": 1, "revenue": 70.0},
    ]
    _assert_valid_shares(pol1.allocate(64, hist_63), expected_ids)

    # First call starting straight at day 75
    pol2 = Policy(creatives, 2000.0, seed=7, options={"log_stderr": False})
    _assert_valid_shares(pol2.allocate(75, hist_63), expected_ids)

    # Out-of-order / backwards day: day 65 then day 63
    pol3 = Policy(creatives, 2000.0, seed=7, options={"log_stderr": False})
    _assert_valid_shares(pol3.allocate(65, hist_63), expected_ids)
    _assert_valid_shares(pol3.allocate(63, hist_63), expected_ids)


def test_bad_history_inputs_are_safe() -> None:
    creatives = _load_creatives()
    expected_ids = [c["creative_id"] for c in creatives]
    pol = Policy(creatives, 2000.0, seed=9, options={"log_stderr": False})

    cases = [
        None,
        [],
        [{"creative_id": "CR001"}],  # missing keys
        [{"day": 60, "creative_id": "CR001", "spend": None, "revenue": None}],  # None values
        [{"day": "sixty", "creative_id": "CR001", "spend": "abc", "revenue": "xyz"}],  # bad strings
        [{"day": float("inf"), "creative_id": "CR001", "spend": 10.0, "revenue": 20.0}],  # inf day
        [{"day": 60, "creative_id": "CR001", "spend": -10.0, "revenue": 50.0}],  # negative spend
        [{"day": 60, "creative_id": "CR001", "spend": float("nan"), "revenue": float("inf")}],  # NaN/inf
        [{"day": 60, "creative_id": "CR001", "spend": 50.0, "revenue": float("nan")}],  # valid spend + NaN rev
        [{"day": 60, "creative_id": "CR001", "spend": 50.0, "revenue": float("inf")}],  # valid spend + inf rev
        [  # duplicate rows
            {"day": 60, "creative_id": "CR001", "budget": 40.0, "spend": 40.0, "impressions": 1000, "clicks": 10, "purchases": 1, "revenue": 40.0},
            {"day": 60, "creative_id": "CR001", "budget": 40.0, "spend": 40.0, "impressions": 1000, "clicks": 10, "purchases": 2, "revenue": 90.0},
        ],
        [{"day": 60, "creative_id": "NO_SUCH_ID", "spend": 40.0, "revenue": 200.0}],  # unknown id
        [{"day": 999, "creative_id": "CR001", "spend": 40.0, "revenue": 9999.0}],  # future day
        [{"day": 60, "creative_id": "CR001", "budget": 40.0, "spend": 40.0, "impressions": 0, "clicks": 0, "purchases": 0, "revenue": 0.0}],  # spend with 0 impr
        [{"day": 60, "creative_id": "CR001", "budget": 40.0, "spend": 0.0, "impressions": 0, "clicks": 0, "purchases": 0, "revenue": 0.0}],  # 0 spend overall
    ]

    for idx, hist in enumerate(cases):
        day = 61 + idx
        out = pol.allocate(day, hist)  # type: ignore[arg-type]
        _assert_valid_shares(out, expected_ids)
        assert all(math.isfinite(info["estimated_roas"]) for info in pol.explain(day).values())


def test_bad_init_arguments_behave_sensibly() -> None:
    creatives = _load_creatives()
    expected_ids = [c["creative_id"] for c in creatives]

    # daily_budget of 0, negative, or None -> falls back safely to 2000.0
    for bad_budget in (0, -500.0, None, float("nan")):
        pol = Policy(creatives, bad_budget, seed=1, options={"log_stderr": False})  # type: ignore[arg-type]
        assert pol.daily_budget == 2000.0
        _assert_valid_shares(pol.allocate(61, []), expected_ids)

    # Duplicate ids & missing launch_day in creatives -> deduplicates cleanly
    dup_creatives = [
        {"creative_id": "CR001", "format": "story"},
        {"creative_id": "CR001", "format": "static_image"},
        {"creative_id": "CR002", "format": "carousel"},
    ]
    pol_dup = Policy(dup_creatives, 2000.0, seed=1, options={"log_stderr": False})
    assert pol_dup.creative_ids == ["CR001", "CR002"]
    out_dup = pol_dup.allocate(61, [])
    assert set(out_dup.keys()) == {"CR001", "CR002"}
    assert sum(out_dup.values()) == pytest.approx(1.0, abs=1e-9)

    # Empty creatives list -> returns {} without crashing
    pol_empty = Policy([], 2000.0, seed=1, options={"log_stderr": False})
    assert pol_empty.allocate(61, []) == {}


def test_max_share_and_one_dollar_floor_respected_even_with_dust() -> None:
    creatives = _load_creatives()
    expected_ids = [c["creative_id"] for c in creatives]
    max_share = 0.14
    pol = Policy(creatives, 2000.0, seed=3, options={"max_share": max_share, "step_dollars": 0.5, "log_stderr": False})

    # Make top 10 creatives hugely profitable and all others very weak so max_share binds
    # and some creatives could otherwise get < $1 dust.
    history = []
    for i, c in enumerate(creatives):
        if i == 0:
            rev = 2500.0
            sp = 500.0
        elif i < 7:
            rev = 800.0
            sp = 280.0
        elif i == 7:
            rev = 300.0
            sp = 100.0
        else:
            rev = 1.0
            sp = 100.0
        history.append(
            {
                "day": 50,
                "creative_id": c["creative_id"],
                "budget": sp,
                "spend": sp,
                "impressions": 5000,
                "clicks": 80,
                "purchases": 10 if i < 8 else 0,
                "revenue": rev,
            }
        )

    shares = pol.allocate(61, history)
    _assert_valid_shares(shares, expected_ids, 2000.0)
    assert max(shares.values()) <= max_share + 1e-9

    # Also test when uniform_mix=0.01 creates sub-$1 dust ($0.40 each) while top creatives hit max_share
    pol_dust = Policy(
        creatives,
        2000.0,
        seed=3,
        options={"max_share": max_share, "uniform_mix": 0.01, "log_stderr": False},
    )
    shares_dust = pol_dust.allocate(61, history)
    _assert_valid_shares(shares_dust, expected_ids, 2000.0)
    assert max(shares_dust.values()) <= max_share + 1e-9


def test_working_directory_independence_and_missing_priors_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creatives = _load_creatives()
    expected = Policy(creatives, 2000.0, seed=77, options={"log_stderr": False}).allocate(61, [])

    # Run from a completely different working directory -> still loads policy_priors.json
    monkeypatch.chdir(tmp_path)
    pol_chdir = Policy(creatives, 2000.0, seed=77, options={"log_stderr": False})
    assert "source" in pol_chdir.priors  # loaded from policy_priors.json
    assert pol_chdir.allocate(61, []) == expected

    # Copy policy.py alone into an empty temp directory with no policy_priors.json
    isolated_dir = tmp_path / "isolated"
    isolated_dir.mkdir()
    isolated_policy_path = isolated_dir / "policy.py"
    shutil.copy2(Path(__file__).resolve().parent.parent / "policy.py", isolated_policy_path)

    spec = importlib.util.spec_from_file_location("isolated_policy", isolated_policy_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    pol_fallback = mod.Policy(creatives, 2000.0, seed=77, options={"log_stderr": False})
    assert "source" not in pol_fallback.priors  # fell back cleanly to FALLBACK_PRIORS
    _assert_valid_shares(pol_fallback.allocate(61, []), [c["creative_id"] for c in creatives])


def test_isolation_no_sim_analysis_network_or_world_config() -> None:
    source = Path(__file__).resolve().parent.parent / "policy.py"
    text = source.read_text(encoding="utf-8")
    tree = ast.parse(text)

    forbidden_prefixes = ("sim", "analysis", "socket", "urllib", "requests", "http", "httplib2", "aiohttp")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(forbidden_prefixes)
        elif isinstance(node, ast.ImportFrom):
            mod_name = node.module or ""
            assert not mod_name.startswith(forbidden_prefixes)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # Ignore module docstring describing what the file avoids
            if "Never imports from sim/" not in node.value:
                assert "world_config" not in node.value


def test_determinism_same_seed_vs_different_seed() -> None:
    creatives = _load_creatives()
    history = [
        {"day": 59, "creative_id": "CR001", "budget": 50.0, "spend": 50.0, "impressions": 4000, "clicks": 60, "purchases": 2, "revenue": 140.0},
        {"day": 60, "creative_id": "CR003", "budget": 50.0, "spend": 50.0, "impressions": 4000, "clicks": 60, "purchases": 1, "revenue": 60.0},
    ]

    p1 = Policy(creatives, 2000.0, seed=123, options={"thompson": True, "log_stderr": False})
    p2 = Policy(creatives, 2000.0, seed=123, options={"thompson": True, "log_stderr": False})
    p3 = Policy(creatives, 2000.0, seed=456, options={"thompson": True, "log_stderr": False})

    s1 = p1.allocate(61, history)
    s2 = p2.allocate(61, history)
    s3 = p3.allocate(61, history)

    assert s1 == s2
    assert s1 != s3
