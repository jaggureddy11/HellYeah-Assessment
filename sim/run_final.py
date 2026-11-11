"""Final fresh-seed (5000..5019) confirmation across all 10 worlds + runtime benchmark.

Run from repository root:
    uv run python -m sim.run_final
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from policy import Policy
from sim.baselines import B1Uniform, B2WindowedGreedy
from sim.clairvoyant import Clairvoyant
from sim.env import World, load_creatives_csv, load_daily_final_csv, run, _validate_and_normalize_shares
from sim.evaluate import bootstrap_mean_ci, format_evaluation_report
from sim.make_shifted_worlds import main as generate_shifted_worlds

EVIDENCE_DIR = Path("evidence")


def _fmt_ci(ci: tuple[float, float, float], digits: int = 4) -> str:
    m, lo, hi = ci
    return f"{m:+.{digits}f} [{lo:+.{digits}f}, {hi:+.{digits}f}]"


def _worker_run(task: tuple[str, str, str, int]) -> tuple[str, str, int, float, float]:
    w_name, cfg_path, pol_key, seed = task
    world_cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))

    if pol_key == "B1":
        pol_ctor: Any = B1Uniform
    elif pol_key == "B2":
        pol_ctor = B2WindowedGreedy
    elif pol_key == "CL":
        pol_ctor = lambda cr, db, sd, wc=world_cfg: Clairvoyant(cr, db, sd, world_config=wc, step_dollars=5.0)
    else:
        pol_ctor = Policy

    with contextlib.redirect_stderr(io.StringIO()):
        res = run(policy=pol_ctor, world_config=world_cfg, seed=seed, start_day=61, end_day=90)
    return w_name, pol_key, seed, float(res["total_revenue"]), float(res["total_spend"])


def benchmark_policy_runtime(config_path: str | Path, seeds: list[int]) -> dict[str, float]:
    """Measure sequential wall time of Policy.__init__ + 30 allocate() calls on default world."""
    world_cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    creatives = load_creatives_csv("data")
    hist_all = [r for r in load_daily_final_csv("data") if r["day"] < 61]

    call_times: list[float] = []
    run_times: list[float] = []

    with contextlib.redirect_stderr(io.StringIO()):
        for seed in seeds:
            world = World(world_config=world_cfg, seed=seed)
            world.ingest_historical_rows(hist_all)

            t_run0 = time.perf_counter()
            pol = Policy(creatives, world.daily_budget, seed)
            policy_elapsed = time.perf_counter() - t_run0

            for day in range(61, 91):
                hist = world.reported_history(current_day=day)
                t_call0 = time.perf_counter()
                raw_shares = pol.allocate(day, hist)
                dt_call = time.perf_counter() - t_call0
                call_times.append(dt_call)
                policy_elapsed += dt_call

                budgets = _validate_and_normalize_shares(raw_shares, world.creative_ids, world.daily_budget)
                world.step_day(day, budgets)

            run_times.append(policy_elapsed)

    return {
        "n_runs": float(len(seeds)),
        "n_calls": float(len(call_times)),
        "call_mean_ms": 1000.0 * float(np.mean(call_times)),
        "call_max_ms": 1000.0 * float(np.max(call_times)),
        "run_mean_s": float(np.mean(run_times)),
        "run_max_s": float(np.max(run_times)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Final confirmation on fresh seeds 5000..5019 across 10 worlds.")
    parser.add_argument("--seeds", type=int, default=20)
    parser.add_argument("--start-seed", type=int, default=5000)
    parser.add_argument("--n-boot", type=int, default=2000)
    parser.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 4))
    args = parser.parse_args()

    t0 = time.perf_counter()
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    generate_shifted_worlds()

    worlds: list[tuple[str, str]] = [
        ("default", "sim/world_config.json"),
        ("slow_lag", "sim/worlds/slow_lag.json"),
        ("fast_fatigue", "sim/worlds/fast_fatigue.json"),
        ("format_reshuffled", "sim/worlds/format_reshuffled.json"),
        ("format_no_signal", "sim/worlds/format_no_signal.json"),
        ("noisy", "sim/worlds/noisy.json"),
        ("other_saturation", "sim/worlds/other_saturation.json"),
        ("big_outage", "sim/worlds/big_outage.json"),
        ("strong_new_creatives", "sim/worlds/strong_new_creatives.json"),
        ("weak_new_creatives", "sim/worlds/weak_new_creatives.json"),
    ]
    seeds = list(range(args.start_seed, args.start_seed + args.seeds))

    jobs: list[tuple[str, str, str, int]] = []
    for w_name, cfg_path in worlds:
        for s in seeds:
            for pol_key in ("B1", "B2", "CL", "POLICY"):
                jobs.append((w_name, cfg_path, pol_key, s))

    results: dict[tuple[str, str, int], tuple[float, float]] = {}
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for w_name, pol_key, s, rev, sp in pool.map(_worker_run, jobs, chunksize=4):
            results[(w_name, pol_key, s)] = (rev, sp)

    # Sequential Policy-only runtime measurement on default world
    timing = benchmark_policy_runtime("sim/world_config.json", seeds)
    elapsed = time.perf_counter() - t0

    pol_defaults = Policy([{"creative_id": "CR001", "format": "story"}], 2000.0, 0, options={"log_stderr": False}).options

    lines = [
        "# Final Confirmation Summary (Fresh Seeds `5000..5019`)",
        "",
        f"- **Command:** `uv run python -m sim.run_final --seeds {args.seeds} --start-seed {args.start_seed}`",
        f"- **Fresh seeds:** `{args.seeds}` paired seeds (`{args.start_seed}..{args.start_seed + args.seeds - 1}`), `{args.n_boot}` bootstrap replicates.",
        (
            f"- **Policy defaults (`policy.py`):** `recency_decay={pol_defaults['recency_decay']}`, "
            f"`uniform_mix={pol_defaults['uniform_mix']}`, `cold_start_floor={pol_defaults['cold_start_floor']}`, "
            f"`thompson={pol_defaults['thompson']}`, `lag_correction={pol_defaults['lag_correction']}`, "
            f"`format_prior={pol_defaults['format_prior']}`, `curvature={pol_defaults['curvature']}`."
        ),
        f"- **Total suite wall time:** `{elapsed:.1f}s` (`{len(jobs)}` runs across `{args.workers}` workers).",
        "",
        "## 1. All 10 Worlds (Default Policy vs B1, B2, Clairvoyant)",
        "",
        "Criterion: `95% CI` of `(yours - B1) / (clairvoyant - B1)` strictly above `0` vs `B1`.",
        "",
        "| World | Policy Rev ($) | B1 Rev ($) | B2 Rev ($) | Clairvoyant ($) | `(yours - B1)/(cl - B1)` [95% CI] | `(B2 - B1)/(cl - B1)` [95% CI] | Result |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]

    n_pass = 0
    for w_name, cfg_path in worlds:
        b1_rev = [results[(w_name, "B1", s)][0] for s in seeds]
        b2_rev = [results[(w_name, "B2", s)][0] for s in seeds]
        cl_rev = [results[(w_name, "CL", s)][0] for s in seeds]
        yo_rev = [results[(w_name, "POLICY", s)][0] for s in seeds]

        b1_sp = [results[(w_name, "B1", s)][1] for s in seeds]
        b2_sp = [results[(w_name, "B2", s)][1] for s in seeds]
        cl_sp = [results[(w_name, "CL", s)][1] for s in seeds]
        yo_sp = [results[(w_name, "POLICY", s)][1] for s in seeds]

        b1_a, b2_a, cl_a, yo_a = map(np.asarray, (b1_rev, b2_rev, cl_rev, yo_rev))
        denom = cl_a - b1_a
        valid = np.abs(denom) > 1e-6
        y_norm = np.where(valid, (yo_a - b1_a) / denom, np.nan)
        b_norm = np.where(valid, (b2_a - b1_a) / denom, np.nan)
        cl_gain = 100.0 * (cl_a - b1_a) / np.where(b1_a > 0.0, b1_a, np.nan)

        y_ci = bootstrap_mean_ci(y_norm, n_boot=args.n_boot)
        b_ci = bootstrap_mean_ci(b_norm, n_boot=args.n_boot)
        passed = y_ci[1] > 0.0
        if passed:
            n_pass += 1
        status = "**PASS**" if passed else "**FAIL**"

        rep_obj = {
            "config_path": cfg_path,
            "n_seeds": args.seeds,
            "seeds": seeds,
            "policy_options": {},
            "rev_by_pol": {
                "B1 Uniform": b1_rev,
                "B2 Windowed Greedy": b2_rev,
                "Clairvoyant ($5 step)": cl_rev,
                "Policy (policy.py)": yo_rev,
            },
            "spend_by_pol": {
                "B1 Uniform": b1_sp,
                "B2 Windowed Greedy": b2_sp,
                "Clairvoyant ($5 step)": cl_sp,
                "Policy (policy.py)": yo_sp,
            },
            "yours_norm_ci": y_ci,
            "b2_norm_ci": b_ci,
            "cl_gain_pct_ci": bootstrap_mean_ci(cl_gain, n_boot=args.n_boot),
            "n_boot": args.n_boot,
        }
        (EVIDENCE_DIR / f"final_world_{w_name}.txt").write_text(
            format_evaluation_report(rep_obj) + "\n", encoding="utf-8"
        )

        lines.append(
            f"| `{w_name}` | ${float(np.mean(yo_a)):,.0f} | ${float(np.mean(b1_a)):,.0f} | "
            f"${float(np.mean(b2_a)):,.0f} | ${float(np.mean(cl_a)):,.0f} | "
            f"{_fmt_ci(y_ci)} | {_fmt_ci(b_ci)} | {status} |"
        )

    lines.extend(
        [
            "",
            f"**Summary:** `{n_pass}/{len(worlds)}` worlds passed (`95% CI > 0` vs `B1`).",
            "",
            "## 2. Policy Runtime Benchmark (`default` world, sequential, Policy alone)",
            "",
            f"- **Measured over:** `{int(timing['n_runs'])}` full 30-day runs (`{int(timing['n_calls'])}` `allocate()` calls on seeds `{args.start_seed}..{args.start_seed + args.seeds - 1}`).",
            f"- **Mean time per `allocate()` call:** `{timing['call_mean_ms']:.2f} ms` (`{timing['call_mean_ms']/1000.0:.4f} s`, limit `10.0 s`).",
            f"- **Max time per `allocate()` call:** `{timing['call_max_ms']:.2f} ms` (`{timing['call_max_ms']/1000.0:.4f} s`, limit `10.0 s`).",
            f"- **Mean total time per 30-day run (init + 30 calls):** `{timing['run_mean_s']:.3f} s` (limit `600.0 s`).",
            f"- **Max total time per 30-day run (init + 30 calls):** `{timing['run_max_s']:.3f} s` (limit `600.0 s`).",
            "",
            "## 3. Raw Printouts",
            "- Per-world evaluation files: `evidence/final_world_*.txt`.",
        ]
    )

    out_md = "\n".join(lines) + "\n"
    (EVIDENCE_DIR / "final_summary.md").write_text(out_md, encoding="utf-8")
    print(out_md)


if __name__ == "__main__":
    main()

