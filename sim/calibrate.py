"""Calibration check: replay days 1-60 real buyer budgets through the simulator.

Run from repository root:
    python -m sim.calibrate
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

import numpy as np

from sim.env import World, load_daily_final_csv


def _load_real_reported_by_day(data_dir: Path) -> dict[int, float]:
    by_day: dict[int, float] = defaultdict(float)
    with (data_dir / "daily_reported.csv").open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            by_day[int(r["day"])] += float(r["revenue"])
    return by_day


def main(n_seeds: int = 20, config_path: str = "sim/world_config.json", data_dir: str = "data") -> None:
    d_path = Path(data_dir)
    real_rows = load_daily_final_csv(d_path)
    real_rep_by_day = _load_real_reported_by_day(d_path)
    real_fin_by_day: dict[int, float] = defaultdict(float)
    for r in real_rows:
        real_fin_by_day[r["day"]] += r["revenue"]

    real_total_rev = sum(r["revenue"] for r in real_rows)
    real_total_spend = sum(r["spend"] for r in real_rows)
    real_rep_total_rev = sum(real_rep_by_day.values())
    real_zero_spend_share = sum(1 for r in real_rows if r["spend"] <= 0.0) / len(real_rows)
    real_mean_roas = real_total_rev / real_total_spend
    real_agg_rep_fin_ratio = real_rep_total_rev / real_total_rev

    # Group real budgets by day for exact days 1..60 replay
    budgets_by_day: dict[int, dict[str, float]] = defaultdict(dict)
    for r in real_rows:
        budgets_by_day[r["day"]][r["creative_id"]] = r["budget"]

    sim_revs, sim_spends, sim_zshares, sim_roass, sim_agg_ratios = [], [], [], [], []
    sim_age_ratios: dict[int, list[float]] = {age: [] for age in range(8)}
    sim_outage_ratios: list[float] = []

    for seed in range(100, 100 + n_seeds):
        world = World(config_path, seed=seed)
        for day in range(1, 61):
            day_budgets = budgets_by_day.get(day, {})
            for cid, b in sorted(day_budgets.items()):
                row = world._simulate_creative_day(day, cid, b, record_zero_unserved=True)
                if row is not None:
                    world.rows.append(row)

        sim_rev = sum(r.true_revenue for r in world.rows)
        sim_spend = sum(r.spend for r in world.rows)
        sim_zshare = sum(1 for r in world.rows if r.spend <= 0.0) / len(world.rows)
        sim_roas = sim_rev / sim_spend if sim_spend > 0 else 0.0

        sim_fin_by_day: dict[int, float] = defaultdict(float)
        for r in world.rows:
            sim_fin_by_day[r.day] += r.true_revenue

        # Day-60 reported snapshot corresponds to morning of day 61 (obs_cutoff_day = 60)
        rep_at_60 = world.reported_history(current_day=61)
        sim_rep_by_day: dict[int, float] = defaultdict(float)
        for r in rep_at_60:
            sim_rep_by_day[int(r["day"])] += float(r["revenue"])

        sim_rep_rev = sum(sim_rep_by_day.values())
        sim_agg_ratio = sim_rep_rev / sim_rev if sim_rev > 0 else 0.0

        for age in range(8):
            d = 60 - age
            fin_d = sim_fin_by_day[d]
            sim_age_ratios[age].append(sim_rep_by_day[d] / fin_d if fin_d > 0 else float("nan"))

        out_fin = sum(sim_fin_by_day[d] for d in (47, 48, 49))
        out_rep = sum(sim_rep_by_day[d] for d in (47, 48, 49))
        sim_outage_ratios.append(out_rep / out_fin if out_fin > 0 else float("nan"))

        sim_revs.append(sim_rev)
        sim_spends.append(sim_spend)
        sim_zshares.append(sim_zshare)
        sim_roass.append(sim_roas)
        sim_agg_ratios.append(sim_agg_ratio)

    def _fmt(vals: list[float], fmt: str) -> str:
        arr = np.array(vals, dtype=float)
        arr = arr[np.isfinite(arr)]
        m = float(np.mean(arr))
        s = float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0
        return f"{format(m, fmt)} +/- {format(s, fmt)}"

    print(f"=== Calibration Replay: Days 1-60 Real Buyer Budgets ({n_seeds} seeds) ===")
    print("Note: This check replays the same days 1-60 the world config was fitted from,")
    print("so it is an in-sample sanity check and not proof that the simulator is realistic.\n")
    print(f"{'Metric':<42} | {'Real (data/)':<14} | {'Simulated (mean +/- sd)':<26}")
    print("-" * 88)
    print(f"{'Total revenue ($)':<42} | {real_total_rev:<14,.2f} | {_fmt(sim_revs, ',.2f'):<26}")
    print(f"{'Total spend ($)':<42} | {real_total_spend:<14,.2f} | {_fmt(sim_spends, ',.2f'):<26}")
    print(f"{'Zero-spend row share':<42} | {real_zero_spend_share:<14.4f} | {_fmt(sim_zshares, '.4f'):<26}")
    print(f"{'Mean ROAS (total_rev/total_spend)':<42} | {real_mean_roas:<14.4f} | {_fmt(sim_roass, '.4f'):<26}")
    print(f"{'All-days reported / final rev ratio (1-60)':<42} | {real_agg_rep_fin_ratio:<14.4f} | {_fmt(sim_agg_ratios, '.4f'):<26}")

    print("\n=== Attribution Lag & Outage Check at Day-60 Snapshot (reported / final rev) ===")
    print(f"{'Slice at day 60':<42} | {'Real (data/)':<14} | {'Simulated (mean +/- sd)':<26}")
    print("-" * 88)
    for age in range(8):
        d = 60 - age
        real_r = real_rep_by_day[d] / real_fin_by_day[d] if real_fin_by_day[d] > 0 else float("nan")
        lbl = f"Age {age} (day {d})"
        print(f"{lbl:<42} | {real_r:<14.4f} | {_fmt(sim_age_ratios[age], '.4f'):<26}")

    real_out_fin = sum(real_fin_by_day[d] for d in (47, 48, 49))
    real_out_rep = sum(real_rep_by_day[d] for d in (47, 48, 49))
    real_out_r = real_out_rep / real_out_fin if real_out_fin > 0 else float("nan")
    print(f"{'Days 47-49 outage (ages 11-13)':<42} | {real_out_r:<14.4f} | {_fmt(sim_outage_ratios, '.4f'):<26}")


if __name__ == "__main__":
    main()
