"""Fresh-seed (3000..3019) confirmation suite and cold-start variant selection.

Reuses B1, B2, and Clairvoyant per (world, seed) across ablations and runs
independent (world, policy, seed) jobs in parallel without altering determinism.

Run from repository root:
    uv run python -m sim.run_confirm
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


def _fmt_ci(ci: tuple[float, float, float], digits: int = 4, signed: bool = True) -> str:
    m, lo, hi = ci
    if signed:
        return f"{m:+.{digits}f} [{lo:+.{digits}f}, {hi:+.{digits}f}]"
    return f"{m:.{digits}f} [{lo:.{digits}f}, {hi:.{digits}f}]"


def _worker_run(task: tuple[str, str, str, dict[str, Any] | None, int]) -> tuple[str, str, int, float, float]:
    """Execute one deterministic 30-day simulation run."""
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
        res = run(
            policy=pol_ctor,
            world_config=world_cfg,
            seed=seed,
            start_day=61,
            end_day=90,
        )
    return w_name, pol_key, seed, float(res["total_revenue"]), float(res["total_spend"])


def _verdict(
    w_name: str,
    tag: str,
    am: float,
    dm: float,
    dlo: float,
    dhi: float,
    def_lo: float,
    def_hi: float,
    world_lo: float,
) -> str:
    if w_name == "default":
        if tag in ("lag_correction_off", "format_prior_off"):
            outside = am < def_lo or am > def_hi
            if dlo > 0.0 and outside:
                return "**PASS** (helps & lowers outside default CI)"
            if dlo > 0.0:
                return "**FAIL** (paired lift > 0, but stays inside default 95% CI)"
            if dhi < 0.0:
                return "**FAIL (harmful)** (turning OFF significantly improves headline)"
            return "**FAIL** (no reliable benefit)"
        if dlo > 0.0:
            return "**PASS** (ON beats OFF; paired CI > 0)"
        if dhi < 0.0:
            return "**FAIL (harmful)** (turning OFF significantly improves headline)"
        return "**FAIL** (paired delta CI includes 0)"

    if w_name == "slow_lag" and tag == "lag_correction_off":
        if world_lo > 0.0 and dlo > 0.0:
            return "**PASS** (beats B1 & lag_correction helps vs off)"
        if world_lo > 0.0 and dhi < 0.0:
            return "**FAIL (harmful)** (lag_correction hurts in slow_lag)"
        if world_lo > 0.0:
            return "**PASS (above B1)**, lag_correction delta CI includes 0"
        return "**FAIL** (fails to stay above B1)"

    if dlo > 0.0:
        return "**PASS** (ON beats OFF, CI > 0)"
    if dhi < 0.0:
        return "**FAIL (harmful)** (turning OFF significantly improves headline)"
    return "NEUTRAL (paired CI includes 0)"


def main() -> None:
    parser = argparse.ArgumentParser(description="Fresh-seed confirmation run for Policy and ablations.")
    parser.add_argument("--seeds", type=int, default=20, help="Number of fresh paired seeds.")
    parser.add_argument("--start-seed", type=int, default=3000, help="Starting seed (3000+ fresh).")
    parser.add_argument("--n-boot", type=int, default=2000, help="Bootstrap replicates.")
    parser.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 4), help="Worker processes.")
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
    cfg_by_world = dict(worlds)
    seeds = list(range(args.start_seed, args.start_seed + args.seeds))

    ablation_specs: list[tuple[str, str, dict[str, Any]]] = [
        ("default", "thompson_off", {"thompson": False}),
        ("default", "cold_start_floor_off", {"cold_start_floor": False}),
        ("default", "both_off", {"cold_start_floor": False, "thompson": False}),
        ("default", "lag_correction_off", {"lag_correction": False}),
        ("default", "format_prior_off", {"format_prior": False}),
        ("strong_new_creatives", "thompson_off", {"thompson": False}),
        ("strong_new_creatives", "cold_start_floor_off", {"cold_start_floor": False}),
        ("strong_new_creatives", "both_off", {"cold_start_floor": False, "thompson": False}),
        ("strong_new_creatives", "lag_correction_off", {"lag_correction": False}),
        ("strong_new_creatives", "format_prior_off", {"format_prior": False}),
        ("slow_lag", "lag_correction_off", {"lag_correction": False}),
    ]

    # Build deduplicated job list
    jobs: list[tuple[str, str, str, dict[str, Any] | None, int]] = []
    for w_name, cfg_path in worlds:
        for seed in seeds:
            for pol_key in ("B1", "B2", "CL", "DEFAULT"):
                jobs.append((w_name, cfg_path, pol_key, None, seed))

    for w_name, tag, opts in ablation_specs:
        cfg_path = cfg_by_world[w_name]
        for seed in seeds:
            jobs.append((w_name, cfg_path, tag, opts, seed))

    results: dict[tuple[str, str, int], tuple[float, float]] = {}
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for w_name, pol_key, seed, rev, sp in pool.map(_worker_run, jobs, chunksize=4):
            results[(w_name, pol_key, seed)] = (rev, sp)

    # Assemble per-world evaluation reports
    world_summary: dict[str, dict[str, Any]] = {}
    for w_name, cfg_path in worlds:
        b1_rev = [results[(w_name, "B1", s)][0] for s in seeds]
        b2_rev = [results[(w_name, "B2", s)][0] for s in seeds]
        cl_rev = [results[(w_name, "CL", s)][0] for s in seeds]
        yo_rev = [results[(w_name, "DEFAULT", s)][0] for s in seeds]

        b1_sp = [results[(w_name, "B1", s)][1] for s in seeds]
        b2_sp = [results[(w_name, "B2", s)][1] for s in seeds]
        cl_sp = [results[(w_name, "CL", s)][1] for s in seeds]
        yo_sp = [results[(w_name, "DEFAULT", s)][1] for s in seeds]

        b1_a, b2_a, cl_a, yo_a = map(np.asarray, (b1_rev, b2_rev, cl_rev, yo_rev))
        denom = cl_a - b1_a
        valid = np.abs(denom) > 1e-6
        yours_norm = np.where(valid, (yo_a - b1_a) / denom, np.nan)
        b2_norm = np.where(valid, (b2_a - b1_a) / denom, np.nan)
        cl_gain = 100.0 * (cl_a - b1_a) / np.where(b1_a > 0.0, b1_a, np.nan)

        res_obj = {
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
            "yours_norm_ci": bootstrap_mean_ci(yours_norm, n_boot=args.n_boot),
            "b2_norm_ci": bootstrap_mean_ci(b2_norm, n_boot=args.n_boot),
            "cl_gain_pct_ci": bootstrap_mean_ci(cl_gain, n_boot=args.n_boot),
            "n_boot": args.n_boot,
            "yours_norm_arr": yours_norm,
        }
        world_summary[w_name] = res_obj
        (EVIDENCE_DIR / f"confirm_world_{w_name}.txt").write_text(
            format_evaluation_report(res_obj) + "\n", encoding="utf-8"
        )

    # Assemble per-ablation reports
    ablation_summary: list[tuple[str, str, dict[str, Any], tuple[float, float, float], np.ndarray]] = []
    abl_norm_map: dict[tuple[str, str], np.ndarray] = {}

    for w_name, tag, opts in ablation_specs:
        base = world_summary[w_name]
        b1_a = np.asarray(base["rev_by_pol"]["B1 Uniform"], dtype=float)
        cl_a = np.asarray(base["rev_by_pol"]["Clairvoyant ($5 step)"], dtype=float)
        ab_rev = [results[(w_name, tag, s)][0] for s in seeds]
        ab_sp = [results[(w_name, tag, s)][1] for s in seeds]
        ab_a = np.asarray(ab_rev, dtype=float)
        denom = cl_a - b1_a
        valid = np.abs(denom) > 1e-6
        ab_norm = np.where(valid, (ab_a - b1_a) / denom, np.nan)
        delta_on_off = base["yours_norm_arr"] - ab_norm
        delta_ci = bootstrap_mean_ci(delta_on_off, n_boot=args.n_boot)

        abl_obj = {
            "config_path": base["config_path"],
            "n_seeds": args.seeds,
            "seeds": seeds,
            "policy_options": opts,
            "rev_by_pol": {**base["rev_by_pol"], "Policy (policy.py)": ab_rev},
            "spend_by_pol": {**base["spend_by_pol"], "Policy (policy.py)": ab_sp},
            "yours_norm_ci": bootstrap_mean_ci(ab_norm, n_boot=args.n_boot),
            "b2_norm_ci": base["b2_norm_ci"],
            "cl_gain_pct_ci": base["cl_gain_pct_ci"],
            "n_boot": args.n_boot,
        }
        txt = (
            format_evaluation_report(abl_obj)
            + f"\n\nPaired delta (default ON - {tag}) in normalized score: {_fmt_ci(delta_ci)}\n"
        )
        (EVIDENCE_DIR / f"confirm_ablation_{w_name}_{tag}.txt").write_text(txt, encoding="utf-8")
        ablation_summary.append((w_name, tag, abl_obj, delta_ci, ab_norm))
        abl_norm_map[(w_name, tag)] = ab_norm

    elapsed = time.perf_counter() - t0

    # Build confirm_summary.md
    lines = [
        "# Fresh-Seed Confirmation Summary (`3000..3019`)",
        "",
        f"- **Command:** `uv run python -m sim.run_confirm --seeds {args.seeds} --start-seed {args.start_seed}`",
        f"- **Fresh seeds:** `{args.seeds}` paired seeds (`{args.start_seed}..{args.start_seed + args.seeds - 1}`), `{args.n_boot}` bootstrap replicates.",
        f"- **Policy defaults tested:** `recency_decay=False`, `cold_start_floor=True`, `thompson=True`, `lag_correction=True`, `format_prior=True`, `curvature=True`.",
        f"- **Runtime:** `{elapsed:.1f}s` (`{len(jobs)}` 30-day runs across `{args.workers}` worker processes).",
        "",
        "## 1. Default & Shifted Worlds (Current Default Policy vs B1, B2, Clairvoyant)",
        "",
        "| World | Policy Rev ($ mean) | `(yours - B1)/(cl - B1)` [95% CI] | `(B2 - B1)/(cl - B1)` [95% CI] | Clairvoyant Gain | Check Result |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for w_name, _ in worlds:
        r = world_summary[w_name]
        ym = float(np.mean(r["rev_by_pol"]["Policy (policy.py)"]))
        y_ci = r["yours_norm_ci"]
        b_ci = r["b2_norm_ci"]
        gm = float(r["cl_gain_pct_ci"][0])
        status = "**PASS**" if y_ci[1] > 0.0 else "**FAIL** (95% CI includes <= 0)"
        lines.append(
            f"| `{w_name}` | ${ym:,.0f} | {_fmt_ci(y_ci)} | {_fmt_ci(b_ci)} | {gm:+.1f}% | {status} |"
        )

    lines.extend(
        [
            "",
            "## 2. Ablations on `default`, `strong_new_creatives`, and `slow_lag`",
            "",
            "| World | Variant / Ablation | Variant `(yours-B1)/(cl-B1)` [95% CI] | Paired Delta `(DEFAULT - variant)` [95% CI] | Verdict |",
            "|---|---|---:|---:|---|",
        ]
    )
    def_lo, def_hi = world_summary["default"]["yours_norm_ci"][1], world_summary["default"]["yours_norm_ci"][2]
    for w_name, tag, abl_obj, delta_ci, _ in ablation_summary:
        a_ci = abl_obj["yours_norm_ci"]
        v = _verdict(
            w_name=w_name,
            tag=tag,
            am=a_ci[0],
            dm=delta_ci[0],
            dlo=delta_ci[1],
            dhi=delta_ci[2],
            def_lo=def_lo,
            def_hi=def_hi,
            world_lo=world_summary[w_name]["yours_norm_ci"][1],
        )
        lines.append(
            f"| `{w_name}` | `{tag}` | {_fmt_ci(a_ci)} | {_fmt_ci(delta_ci)} | {v} |"
        )

    # Apply PREREG Entry 6(c) selection rule for cold-start/exploration variants
    # Ordered lightest -> heaviest:
    variants = [
        ("both_off", "both_off", "`cold_start_floor=false, thompson=false`"),
        ("cold_start_floor_only", "thompson_off", "`cold_start_floor=true, thompson=false`"),
        ("thompson_only", "cold_start_floor_off", "`cold_start_floor=false, thompson=true`"),
        ("both_on", "DEFAULT", "`cold_start_floor=true, thompson=true`"),
    ]

    lines.extend(
        [
            "",
            "## 3. PREREG Entry 6(c) Cold-Start Variant Selection (Lightest -> Heaviest)",
            "",
            "Rule: Keep the **lightest** variant that is (1) not worse than `both_off` by more than its paired 95% CI in `default` (`delta_hi >= 0`) AND (2) not below `both_off` in `strong_new_creatives` (`mean_delta_strong >= 0`).",
            "",
            "| Order | Variant | Options | `default` score [95% CI] | `default` vs `both_off` [95% CI] | `strong_new` score [95% CI] | `strong_new` vs `both_off` [95% CI] | Passes Rule? |",
            "|---:|---|---|---:|---:|---:|---:|---|",
        ]
    )

    def _norm_arr(w: str, key: str) -> np.ndarray:
        if key == "DEFAULT":
            return world_summary[w]["yours_norm_arr"]
        return abl_norm_map[(w, key)]

    chosen_variant: str | None = None
    for idx, (v_name, key, opt_desc) in enumerate(variants, start=1):
        arr_def = _norm_arr("default", key)
        arr_str = _norm_arr("strong_new_creatives", key)
        ci_def_abs = bootstrap_mean_ci(arr_def, n_boot=args.n_boot)
        ci_str_abs = bootstrap_mean_ci(arr_str, n_boot=args.n_boot)
        d_def = arr_def - _norm_arr("default", "both_off")
        d_str = arr_str - _norm_arr("strong_new_creatives", "both_off")
        ci_def = bootstrap_mean_ci(d_def, n_boot=args.n_boot)
        ci_str = bootstrap_mean_ci(d_str, n_boot=args.n_boot)

        cond1 = ci_def[2] >= 0.0  # not worse than both_off by more than its paired 95% CI
        cond2 = ci_str[0] >= 0.0  # not below both_off in strong_new_creatives
        passes = cond1 and cond2
        if passes and chosen_variant is None:
            chosen_variant = v_name
        lines.append(
            f"| {idx} | `{v_name}` | {opt_desc} | {_fmt_ci(ci_def_abs)} | {_fmt_ci(ci_def)} | {_fmt_ci(ci_str_abs)} | {_fmt_ci(ci_str)} | {'**YES**' if passes else '**NO**'} |"
        )

    lines.extend(
        [
            "",
            f"**Selected by PREREG Entry 6(c) rule:** `{chosen_variant}` *(no defaults changed yet in `policy.py` pending confirmation)*.",
            "",
            "## 4. Output Files",
            "- Per-world and per-ablation raw printouts: `evidence/confirm_world_*.txt` and `evidence/confirm_ablation_*.txt`.",
        ]
    )

    out_md = "\n".join(lines) + "\n"
    (EVIDENCE_DIR / "confirm_summary.md").write_text(out_md, encoding="utf-8")
    print(f"Completed fresh confirmation in {elapsed:.1f}s. Wrote evidence/confirm_summary.md.\n")
    print(out_md)


if __name__ == "__main__":
    main()
