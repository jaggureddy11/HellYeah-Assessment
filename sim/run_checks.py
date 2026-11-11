"""Reproducible runner for all pre-registered shifted-world checks and ablations.

Run from repository root:
    uv run python -m sim.run_checks
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import time
from pathlib import Path
from typing import Any

import numpy as np

from policy import Policy
from sim.env import run
from sim.evaluate import bootstrap_mean_ci, evaluate, format_evaluation_report
from sim.make_shifted_worlds import main as generate_shifted_worlds

EVIDENCE_DIR = Path("evidence")


def _fmt_ci(ci: tuple[float, float, float], digits: int = 4, signed: bool = True) -> str:
    m, lo, hi = ci
    if signed:
        return f"{m:+.{digits}f} [{lo:+.{digits}f}, {hi:+.{digits}f}]"
    return f"{m:.{digits}f} [{lo:.{digits}f}, {hi:.{digits}f}]"


def _run_ablation_on_cached_world(
    base_res: dict[str, Any],
    opt_key: str,
    opt_val: Any,
    n_boot: int = 2000,
) -> tuple[dict[str, Any], tuple[float, float, float]]:
    """Run Policy with a single option overridden, reusing paired B1/B2/Clairvoyant from base_res."""
    world_cfg = json.loads(Path(base_res["config_path"]).read_text(encoding="utf-8"))
    seeds = base_res["seeds"]
    opts = {opt_key: opt_val}

    yo_rev: list[float] = []
    yo_spend: list[float] = []
    for seed in seeds:
        res = run(
            policy=lambda cr, db, sd, o=opts: Policy(cr, db, sd, options=o),
            world_config=world_cfg,
            seed=seed,
            start_day=61,
            end_day=90,
        )
        yo_rev.append(float(res["total_revenue"]))
        yo_spend.append(float(res["total_spend"]))

    b1_arr = np.asarray(base_res["rev_by_pol"]["B1 Uniform"], dtype=float)
    cl_arr = np.asarray(base_res["rev_by_pol"]["Clairvoyant ($5 step)"], dtype=float)
    base_yo_arr = np.asarray(base_res["rev_by_pol"]["Policy (policy.py)"], dtype=float)
    abl_yo_arr = np.asarray(yo_rev, dtype=float)

    denom = cl_arr - b1_arr
    valid = np.abs(denom) > 1e-6
    base_norm = np.where(valid, (base_yo_arr - b1_arr) / denom, np.nan)
    abl_norm = np.where(valid, (abl_yo_arr - b1_arr) / denom, np.nan)
    paired_delta_norm = base_norm - abl_norm  # >0 means default policy (ON) beats ablated (OFF)

    abl_res = {
        "config_path": base_res["config_path"],
        "n_seeds": base_res["n_seeds"],
        "seeds": seeds,
        "policy_options": opts,
        "rev_by_pol": {
            **base_res["rev_by_pol"],
            "Policy (policy.py)": yo_rev,
        },
        "spend_by_pol": {
            **base_res["spend_by_pol"],
            "Policy (policy.py)": yo_spend,
        },
        "yours_norm_ci": bootstrap_mean_ci(abl_norm, n_boot=n_boot),
        "b2_norm_ci": base_res["b2_norm_ci"],
        "cl_gain_pct_ci": base_res["cl_gain_pct_ci"],
        "n_boot": n_boot,
    }
    delta_ci = bootstrap_mean_ci(paired_delta_norm, n_boot=n_boot)
    return abl_res, delta_ci


def main() -> None:
    parser = argparse.ArgumentParser(description="Run all pre-registered shifted-world checks and ablations.")
    parser.add_argument("--seeds", type=int, default=15, help="Paired seeds per world/ablation.")
    parser.add_argument("--start-seed", type=int, default=1000, help="Starting seed.")
    parser.add_argument("--n-boot", type=int, default=2000, help="Bootstrap replicates.")
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
    ]

    world_results: dict[str, dict[str, Any]] = {}
    stderr_buf = io.StringIO()

    with contextlib.redirect_stderr(stderr_buf):
        for w_name, cfg_path in worlds:
            res = evaluate(
                config_path=cfg_path,
                n_seeds=args.seeds,
                start_seed=args.start_seed,
                n_boot=args.n_boot,
                policy_options=None,  # Exact default policy
            )
            world_results[w_name] = res
            txt = format_evaluation_report(res) + "\n"
            (EVIDENCE_DIR / f"world_{w_name}.txt").write_text(txt, encoding="utf-8")

        ablation_specs = [
            ("default", "lag_correction", False),
            ("default", "format_prior", False),
            ("default", "recency_decay", False),
            ("default", "curvature", False),
            ("default", "exploration", False),
            ("slow_lag", "lag_correction", False),
            ("big_outage", "lag_correction", False),
            ("format_no_signal", "format_prior", False),
            ("format_reshuffled", "format_prior", False),
            ("fast_fatigue", "recency_decay", False),
            ("other_saturation", "curvature", False),
        ]

        ablation_rows: list[tuple[str, str, dict[str, Any], tuple[float, float, float]]] = []
        for w_name, opt_k, opt_v in ablation_specs:
            abl_res, delta_ci = _run_ablation_on_cached_world(
                world_results[w_name], opt_k, opt_v, n_boot=args.n_boot
            )
            label = f"{w_name}: {opt_k}={str(opt_v).lower()}"
            txt = (
                format_evaluation_report(abl_res)
                + f"\n\nPaired delta (default ON - ablated OFF) in normalized score: {_fmt_ci(delta_ci)}\n"
            )
            fname = f"ablation_{w_name}_{opt_k}_off.txt"
            (EVIDENCE_DIR / fname).write_text(txt, encoding="utf-8")
            ablation_rows.append((w_name, opt_k, abl_res, delta_ci))

    elapsed = time.perf_counter() - t0

    # Evaluate PREREG Entry 5 pass/fail checks strictly and plainly
    summary_lines = [
        "# Pre-registered Checks & Ablations Summary",
        "",
        f"- **Command:** `uv run python -m sim.run_checks --seeds {args.seeds} --start-seed {args.start_seed}`",
        f"- **Seeds:** `{args.seeds}` paired seeds (`{args.start_seed}..{args.start_seed + args.seeds - 1}`), `{args.n_boot}` bootstrap replicates.",
        f"- **Runtime:** `{elapsed:.1f}s` (policy unmodified; `options=None` for all world runs).",
        "",
        "## 1. Default & Shifted Worlds (Default Policy vs B1, B2, Clairvoyant)",
        "",
        "Criterion (PREREG Entry 5 #4): Policy must beat `B1` with a 95% CI strictly above `0` in every shifted world.",
        "",
        "| World | Policy Rev ($ mean) | `(yours - B1)/(cl - B1)` [95% CI] | `(B2 - B1)/(cl - B1)` [95% CI] | Clairvoyant Gain | Check Result |",
        "|---|---:|---:|---:|---:|---|",
    ]

    for w_name, _ in worlds:
        r = world_results[w_name]
        y_rev_m = float(np.mean(r["rev_by_pol"]["Policy (policy.py)"]))
        ym, ylo, yhi = r["yours_norm_ci"]
        bm, blo, bhi = r["b2_norm_ci"]
        gm, _, _ = r["cl_gain_pct_ci"]
        passed = ylo > 0.0
        status = "**PASS**" if passed else "**FAIL** (95% CI includes <= 0)"
        summary_lines.append(
            f"| `{w_name}` | ${y_rev_m:,.0f} | {_fmt_ci((ym, ylo, yhi))} | {_fmt_ci((bm, blo, bhi))} | {gm:+.1f}% | {status} |"
        )

    summary_lines.extend(
        [
            "",
            "## 2. Ablations (Paired Default ON minus Ablated OFF)",
            "",
            "Criteria (PREREG Entry 5 #1–#3): On `default`, turning a component off must measurably change/lower the headline (or else remove as unneeded complexity). On `format_no_signal`, `format_prior=true` must not be worse than `false` by more than the 95% interval.",
            "",
            "| World | Ablated Flag (`off`) | Ablated `(yours-B1)/(cl-B1)` [95% CI] | Paired Delta `(ON - OFF)` [95% CI] | Pre-registered Verdict |",
            "|---|---|---:|---:|---|",
        ]
    )

    def_res = world_results["default"]
    def_lo = def_res["yours_norm_ci"][1]
    def_hi = def_res["yours_norm_ci"][2]

    for w_name, opt_k, abl_res, delta_ci in ablation_rows:
        am, alo, ahi = abl_res["yours_norm_ci"]
        dm, dlo, dhi = delta_ci
        if w_name == "default":
            if opt_k in ("lag_correction", "format_prior"):
                outside_default = am < def_lo or am > def_hi
                paired_sig = dlo > 0.0 or dhi < 0.0
                if dlo > 0.0 and outside_default:
                    verdict = "**PASS** (helps & lowers outside default CI)"
                elif dlo > 0.0:
                    verdict = "**FAIL** (paired lift > 0, but ablated mean stays inside default 95% CI)"
                else:
                    verdict = "**FAIL** (no reliable benefit; candidate to drop)"
            else:
                if dlo > 0.0 or dhi < 0.0:
                    verdict = "**PASS** (measurably changes headline; paired CI excludes 0)"
                else:
                    verdict = "**FAIL** (paired delta CI includes 0; candidate to drop)"
        elif w_name in ("format_no_signal", "format_reshuffled") and opt_k == "format_prior":
            # Must not be worse than 'off' by more than the interval (i.e. ON - OFF not significantly negative)
            if dhi < 0.0:
                verdict = "**FAIL** (format prior hurts significantly vs off)"
            elif dm < 0.0:
                verdict = "**PASS (borderline)** (slightly worse than off, within 95% CI)"
            else:
                verdict = "**PASS** (does not hurt)"
        elif w_name == "slow_lag" and opt_k == "lag_correction":
            slow_lo = world_results["slow_lag"]["yours_norm_ci"][1]
            if slow_lo > 0.0 and dlo > 0.0:
                verdict = "**PASS** (beats B1 & lag_correction helps vs off)"
            elif slow_lo > 0.0:
                verdict = "**PASS (above B1)**, lag_correction delta CI includes 0"
            else:
                verdict = "**FAIL** (fails to stay above B1)"
        else:
            if dlo > 0.0:
                verdict = "**PASS** (ON beats OFF, CI > 0)"
            elif dhi < 0.0:
                verdict = "**FAIL** (ON worse than OFF)"
            else:
                verdict = "NEUTRAL (paired CI includes 0)"

        summary_lines.append(
            f"| `{w_name}` | `{opt_k}=false` | {_fmt_ci((am, alo, ahi))} | {_fmt_ci((dm, dlo, dhi))} | {verdict} |"
        )

    summary_lines.extend(
        [
            "",
            "## 3. Output Files",
            "- Per-world and per-ablation raw evaluation printouts are saved in `evidence/world_*.txt` and `evidence/ablation_*.txt`.",
        ]
    )

    summary_text = "\n".join(summary_lines) + "\n"
    (EVIDENCE_DIR / "summary.md").write_text(summary_text, encoding="utf-8")
    print(f"Completed all checks in {elapsed:.1f}s (n_seeds={args.seeds}). Summary written to evidence/summary.md.\n")
    print(summary_text)


if __name__ == "__main__":
    main()

