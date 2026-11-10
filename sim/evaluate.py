"""Multi-seed evaluation harness comparing Policy vs B1, B2, and Clairvoyant.

Run from repository root:
    uv run python -m sim.evaluate
    uv run python -m sim.evaluate --config sim/world_config.json --seeds 20
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

from policy import Policy
from sim.baselines import B1Uniform, B2WindowedGreedy
from sim.clairvoyant import Clairvoyant
from sim.env import run


def bootstrap_mean_ci(
    values: list[float] | np.ndarray,
    n_boot: int = 2000,
    seed: int = 42,
) -> tuple[float, float, float]:
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) == 0:
        return float("nan"), float("nan"), float("nan")
    mean_val = float(np.mean(arr))
    if len(arr) == 1:
        return mean_val, mean_val, mean_val
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(arr), size=(n_boot, len(arr)))
    boot_means = arr[idx].mean(axis=1)
    lo, hi = np.quantile(boot_means, [0.025, 0.975])
    return mean_val, float(lo), float(hi)


def evaluate(
    config_path: str | Path = "sim/world_config.json",
    n_seeds: int = 20,
    start_seed: int = 1000,
    n_boot: int = 2000,
    data_dir: str | Path = "data",
) -> dict[str, Any]:
    world_cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    seeds = list(range(start_seed, start_seed + n_seeds))

    def make_clairvoyant(creatives: list[dict[str, Any]], daily_budget: float, seed: int) -> Clairvoyant:
        return Clairvoyant(
            creatives=creatives,
            daily_budget=daily_budget,
            seed=seed,
            world_config=world_cfg,
            step_dollars=5.0,
        )

    policies = {
        "B1 Uniform": B1Uniform,
        "B2 Windowed Greedy": B2WindowedGreedy,
        "Clairvoyant ($5 step)": make_clairvoyant,
        "Policy (policy.py)": Policy,
    }

    rev_by_pol: dict[str, list[float]] = {name: [] for name in policies}
    spend_by_pol: dict[str, list[float]] = {name: [] for name in policies}

    for seed in seeds:
        for name, pol_ctor in policies.items():
            res = run(
                policy=pol_ctor,
                world_config=world_cfg,
                seed=seed,
                start_day=61,
                end_day=90,
                data_dir=data_dir,
            )
            rev_by_pol[name].append(float(res["total_revenue"]))
            spend_by_pol[name].append(float(res["total_spend"]))

    b1_arr = np.asarray(rev_by_pol["B1 Uniform"], dtype=float)
    b2_arr = np.asarray(rev_by_pol["B2 Windowed Greedy"], dtype=float)
    cl_arr = np.asarray(rev_by_pol["Clairvoyant ($5 step)"], dtype=float)
    yo_arr = np.asarray(rev_by_pol["Policy (policy.py)"], dtype=float)

    denom = cl_arr - b1_arr
    valid_denom = np.abs(denom) > 1e-6
    yours_norm = np.where(valid_denom, (yo_arr - b1_arr) / denom, np.nan)
    b2_norm = np.where(valid_denom, (b2_arr - b1_arr) / denom, np.nan)
    cl_gain_pct = 100.0 * (cl_arr - b1_arr) / np.where(b1_arr > 0.0, b1_arr, np.nan)

    return {
        "config_path": str(config_path),
        "n_seeds": n_seeds,
        "seeds": seeds,
        "rev_by_pol": rev_by_pol,
        "spend_by_pol": spend_by_pol,
        "yours_norm_ci": bootstrap_mean_ci(yours_norm, n_boot=n_boot),
        "b2_norm_ci": bootstrap_mean_ci(b2_norm, n_boot=n_boot),
        "cl_gain_pct_ci": bootstrap_mean_ci(cl_gain_pct, n_boot=n_boot),
        "n_boot": n_boot,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Policy vs B1, B2, and Clairvoyant.")
    parser.add_argument("--config", default="sim/world_config.json", help="Path to world config JSON.")
    parser.add_argument("--seeds", type=int, default=20, help="Number of paired seeds to run.")
    parser.add_argument("--start-seed", type=int, default=1000, help="Starting integer seed.")
    parser.add_argument("--n-boot", type=int, default=2000, help="Bootstrap replicates for 95% CI.")
    args = parser.parse_args()

    res = evaluate(
        config_path=args.config,
        n_seeds=args.seeds,
        start_seed=args.start_seed,
        n_boot=args.n_boot,
    )

    print(f"=== Evaluation (days 61-90, n_seeds={res['n_seeds']}, config={res['config_path']}) ===")
    print(f"{'Policy':<24} | {'Revenue mean [95% CI] ($)':<34} | {'Spend mean [95% CI] ($)':<32}")
    print("-" * 96)
    for name in ["B1 Uniform", "B2 Windowed Greedy", "Clairvoyant ($5 step)", "Policy (policy.py)"]:
        rm, rlo, rhi = bootstrap_mean_ci(res["rev_by_pol"][name], n_boot=res["n_boot"])
        sm, slo, shi = bootstrap_mean_ci(res["spend_by_pol"][name], n_boot=res["n_boot"])
        r_str = f"{rm:,.2f} [{rlo:,.2f}, {rhi:,.2f}]"
        s_str = f"{sm:,.2f} [{slo:,.2f}, {shi:,.2f}]"
        print(f"{name:<24} | {r_str:<34} | {s_str:<32}")

    ym, ylo, yhi = res["yours_norm_ci"]
    bm, blo, bhi = res["b2_norm_ci"]
    gm, glo, ghi = res["cl_gain_pct_ci"]
    print("\nPaired per-seed summary (bootstrapped across seeds):")
    print(f"  (yours - B1) / (clairvoyant - B1): {ym:+.4f} [95% CI: {ylo:+.4f}, {yhi:+.4f}]")
    print(f"  (B2    - B1) / (clairvoyant - B1): {bm:+.4f} [95% CI: {blo:+.4f}, {bhi:+.4f}]")
    print(f"  Clairvoyant gain over B1 (%)     : {gm:+.2f}% [95% CI: {glo:+.2f}%, {ghi:+.2f}%]")

    if gm < 5.0:
        print(
            f"\nWARNING: Clairvoyant gain over B1 is only {gm:.2f}% (< 5%). "
            "This environment has very little headroom!"
        )


if __name__ == "__main__":
    main()

