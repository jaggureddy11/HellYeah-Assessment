"""Fresh-seed (4000..4019) robustness comparison of V0, V1, and V2 across 6 worlds.

Reuses B1, B2, and Clairvoyant per (world, seed). Does not modify policy defaults.

Run from repository root:
    uv run python -m sim.run_robustness
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
from sim.env import run
from sim.evaluate import bootstrap_mean_ci, format_evaluation_report
from sim.make_shifted_worlds import main as generate_shifted_worlds

EVIDENCE_DIR = Path("evidence")


def _fmt_ci(ci: tuple[float, float, float], digits: int = 4) -> str:
    m, lo, hi = ci
    return f"{m:+.{digits}f} [{lo:+.{digits}f}, {hi:+.{digits}f}]"


def _worker_run(task: tuple[str, str, str, dict[str, Any] | None, int]) -> tuple[str, str, int, float, float]:
    w_name, cfg_path, pol_key, pol_opts, seed = task
    world_cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))

    if pol_key == "B1":
        pol_ctor: Any = B1Uniform
    elif pol_key == "B2":
        pol_ctor = B2WindowedGreedy
    elif pol_key == "CL":
        pol_ctor = lambda cr, db, sd, wc=world_cfg: Clairvoyant(cr, db, sd, world_config=wc, step_dollars=5.0)
    else:
        pol_ctor = lambda cr, db, sd, o=pol_opts: Policy(cr, db, sd, options=o)

    with contextlib.redirect_stderr(io.StringIO()):
        res = run(policy=pol_ctor, world_config=world_cfg, seed=seed, start_day=61, end_day=90)
    return w_name, pol_key, seed, float(res["total_revenue"]), float(res["total_spend"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Run V0/V1/V2 robustness test on seeds 4000..4019.")
    parser.add_argument("--seeds", type=int, default=20)
    parser.add_argument("--start-seed", type=int, default=4000)
    parser.add_argument("--n-boot", type=int, default=2000)
    parser.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 4))
    args = parser.parse_args()

    t0 = time.perf_counter()
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    generate_shifted_worlds()

    worlds: list[tuple[str, str]] = [
        ("default", "sim/world_config.json"),
        ("format_no_signal", "sim/worlds/format_no_signal.json"),
        ("fast_fatigue", "sim/worlds/fast_fatigue.json"),
        ("format_reshuffled", "sim/worlds/format_reshuffled.json"),
        ("strong_new_creatives", "sim/worlds/strong_new_creatives.json"),
        ("weak_new_creatives", "sim/worlds/weak_new_creatives.json"),
    ]
    variants: list[tuple[str, dict[str, Any] | None, str]] = [
        ("V0", None, "current default (`recency_decay=False, uniform_mix=0.0`)"),
        ("V1", {"recency_decay": True}, "`recency_decay=True` (12d half-life)"),
        ("V2", {"recency_decay": False, "uniform_mix": 0.25}, "`recency_decay=False, uniform_mix=0.25`"),
    ]

    seeds = list(range(args.start_seed, args.start_seed + args.seeds))
    jobs: list[tuple[str, str, str, dict[str, Any] | None, int]] = []
    for w_name, cfg_path in worlds:
        for s in seeds:
            for base_k in ("B1", "B2", "CL"):
                jobs.append((w_name, cfg_path, base_k, None, s))
            for v_name, v_opts, _ in variants:
                jobs.append((w_name, cfg_path, v_name, v_opts, s))

    results: dict[tuple[str, str, int], tuple[float, float]] = {}
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for w_name, pol_key, s, rev, sp in pool.map(_worker_run, jobs, chunksize=4):
            results[(w_name, pol_key, s)] = (rev, sp)

    elapsed = time.perf_counter() - t0

    ci_table: dict[tuple[str, str], tuple[float, float, float]] = {}
    rev_table: dict[tuple[str, str], float] = {}
    b2_ci_by_world: dict[str, tuple[float, float, float]] = {}
    cl_gain_by_world: dict[str, float] = {}

    for w_name, cfg_path in worlds:
        b1_a = np.asarray([results[(w_name, "B1", s)][0] for s in seeds], dtype=float)
        b2_a = np.asarray([results[(w_name, "B2", s)][0] for s in seeds], dtype=float)
        cl_a = np.asarray([results[(w_name, "CL", s)][0] for s in seeds], dtype=float)
        b1_sp = [results[(w_name, "B1", s)][1] for s in seeds]
        b2_sp = [results[(w_name, "B2", s)][1] for s in seeds]
        cl_sp = [results[(w_name, "CL", s)][1] for s in seeds]

        denom = cl_a - b1_a
        valid = np.abs(denom) > 1e-6
        b2_norm = np.where(valid, (b2_a - b1_a) / denom, np.nan)
        cl_gain = 100.0 * (cl_a - b1_a) / np.where(b1_a > 0.0, b1_a, np.nan)
        b2_ci_by_world[w_name] = bootstrap_mean_ci(b2_norm, n_boot=args.n_boot)
        cl_gain_by_world[w_name] = float(np.mean(cl_gain))

        for v_name, v_opts, _ in variants:
            v_rev = [results[(w_name, v_name, s)][0] for s in seeds]
            v_sp = [results[(w_name, v_name, s)][1] for s in seeds]
            v_a = np.asarray(v_rev, dtype=float)
            v_norm = np.where(valid, (v_a - b1_a) / denom, np.nan)
            v_ci = bootstrap_mean_ci(v_norm, n_boot=args.n_boot)
            ci_table[(w_name, v_name)] = v_ci
            rev_table[(w_name, v_name)] = float(np.mean(v_a))

            rep_obj = {
                "config_path": cfg_path,
                "n_seeds": args.seeds,
                "seeds": seeds,
                "policy_options": dict(v_opts or {}),
                "rev_by_pol": {
                    "B1 Uniform": b1_a.tolist(),
                    "B2 Windowed Greedy": b2_a.tolist(),
                    "Clairvoyant ($5 step)": cl_a.tolist(),
                    "Policy (policy.py)": v_rev,
                },
                "spend_by_pol": {
                    "B1 Uniform": b1_sp,
                    "B2 Windowed Greedy": b2_sp,
                    "Clairvoyant ($5 step)": cl_sp,
                    "Policy (policy.py)": v_sp,
                },
                "yours_norm_ci": v_ci,
                "b2_norm_ci": b2_ci_by_world[w_name],
                "cl_gain_pct_ci": bootstrap_mean_ci(cl_gain, n_boot=args.n_boot),
                "n_boot": args.n_boot,
            }
            (EVIDENCE_DIR / f"robustness_{w_name}_{v_name}.txt").write_text(
                format_evaluation_report(rep_obj) + "\n", encoding="utf-8"
            )

    # Apply PREREG Entry 8 selection rule
    passing_variants: list[str] = []
    worst_world_mean: dict[str, float] = {}
    for v_name, _, _ in variants:
        all_pass = all(ci_table[(w, v_name)][1] > 0.0 for w, _ in worlds)
        if all_pass:
            passing_variants.append(v_name)
        worst_world_mean[v_name] = min(ci_table[(w, v_name)][0] for w, _ in worlds)

    if passing_variants:
        picked = max(passing_variants, key=lambda v: ci_table[("default", v)][0])
        pick_reason = (
            f"Variants passing all 6 worlds (`95% CI lo > 0`): {passing_variants}. "
            f"Picked `{picked}` by highest `default`-world headline ({ci_table[('default', picked)][0]:+.4f})."
        )
    else:
        picked = max([v for v, _, _ in variants], key=lambda v: worst_world_mean[v])
        pick_reason = (
            "No variant passed all 6 worlds (`95% CI lo > 0`). "
            f"Picked `{picked}` by highest worst-world mean headline ({worst_world_mean[picked]:+.4f}); "
            "remaining failure(s) will be documented as a limitation in README.md."
        )

    lines = [
        "# Robustness Check Summary (`V0` vs `V1` vs `V2`, seeds `4000..4019`)",
        "",
        f"- **Command:** `uv run python -m sim.run_robustness --seeds {args.seeds} --start-seed {args.start_seed}`",
        f"- **Fresh seeds:** `{args.seeds}` paired seeds (`{args.start_seed}..{args.start_seed + args.seeds - 1}`), `{args.n_boot}` bootstrap replicates.",
        f"- **Runtime:** `{elapsed:.1f}s` (`{len(jobs)}` runs across `{args.workers}` workers).",
        "",
        "## Variants",
    ]
    for v_name, _, desc in variants:
        lines.append(f"- `{v_name}`: {desc}")

    lines.extend(
        [
            "",
            "## Headline `(yours - B1) / (clairvoyant - B1)` [95% CI] across 6 worlds",
            "",
            "| World | `V0` (default) | `V1` (`recency=True`) | `V2` (`uniform_mix=0.25`) | `B2` | Cl Gain |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )

    for w_name, _ in worlds:
        c0 = _fmt_ci(ci_table[(w_name, "V0")]) + (" PASS" if ci_table[(w_name, "V0")][1] > 0 else " **FAIL**")
        c1 = _fmt_ci(ci_table[(w_name, "V1")]) + (" PASS" if ci_table[(w_name, "V1")][1] > 0 else " **FAIL**")
        c2 = _fmt_ci(ci_table[(w_name, "V2")]) + (" PASS" if ci_table[(w_name, "V2")][1] > 0 else " **FAIL**")
        cb2 = _fmt_ci(b2_ci_by_world[w_name])
        lines.append(f"| `{w_name}` | {c0} | {c1} | {c2} | {cb2} | {cl_gain_by_world[w_name]:+.1f}% |")

    lines.extend(
        [
            "",
            "## PREREG Entry 8 Selection Outcome",
            "",
            "| Variant | Passes all 6 worlds? | `default` mean | Worst-world mean |",
            "|---|---|---:|---:|",
        ]
    )
    for v_name, _, _ in variants:
        p_all = "YES" if v_name in passing_variants else "NO"
        lines.append(
            f"| `{v_name}` | {p_all} | {ci_table[('default', v_name)][0]:+.4f} | {worst_world_mean[v_name]:+.4f} |"
        )

    lines.extend(["", f"**Selected by PREREG Entry 8 rule:** **`{picked}`** — {pick_reason}", ""])

    out_md = "\n".join(lines)
    (EVIDENCE_DIR / "robustness_summary.md").write_text(out_md, encoding="utf-8")
    print(out_md)


if __name__ == "__main__":
    main()

