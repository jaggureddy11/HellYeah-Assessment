"""Reproducible runner for all pre-registered shifted-world checks and ablations.

Run from repository root:
    uv run python -m sim.run_checks
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
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


def _ablation_verdict(
    w_name: str,
    opt_k: str,
    am: float,
    dm: float,
    dlo: float,
    dhi: float,
    def_lo: float,
    def_hi: float,
    world_lo: float,
) -> str:
    if w_name == "default":
        if opt_k in ("lag_correction", "format_prior"):
            outside_default = am < def_lo or am > def_hi
            if dlo > 0.0 and outside_default:
                return "**PASS** (helps & lowers outside default CI)"
            if dlo > 0.0:
                return "**FAIL** (paired lift > 0, but ablated mean stays inside default 95% CI)"
            if dhi < 0.0:
                return "**FAIL (harmful)** (turning OFF significantly improves headline)"
            return "**FAIL** (no reliable benefit; candidate to drop)"
        if dlo > 0.0:
            return "**PASS** (helps; paired CI > 0)"
        if dhi < 0.0:
            return "**FAIL (harmful)** (turning OFF significantly improves headline)"
        return "**FAIL** (paired delta CI includes 0; candidate to drop)"

    if w_name in ("format_no_signal", "format_reshuffled") and opt_k == "format_prior":
        if dhi < 0.0:
            return "**FAIL (harmful)** (format prior hurts significantly vs off)"
        if dm < 0.0:
            return "**PASS (borderline)** (slightly worse than off, within 95% CI)"
        return "**PASS** (does not hurt)"

    if w_name == "slow_lag" and opt_k == "lag_correction":
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


def _parse_ci(text: str) -> tuple[float, float, float]:
    m = re.search(r"([+-]?[\d,]+(?:\.\d+)?)\s*\[\s*(?:95% CI:\s*)?([+-]?[\d,]+(?:\.\d+)?),\s*([+-]?[\d,]+(?:\.\d+)?)\s*\]", text)
    if not m:
        raise ValueError(f"Could not parse CI from: {text!r}")
    return (
        float(m.group(1).replace(",", "")),
        float(m.group(2).replace(",", "")),
        float(m.group(3).replace(",", "")),
    )


def _parse_saved_evidence() -> tuple[
    list[tuple[str, float, tuple[float, float, float], tuple[float, float, float], float]],
    list[tuple[str, str, tuple[float, float, float], tuple[float, float, float]]],
    str,
]:
    world_names = [
        "default",
        "slow_lag",
        "fast_fatigue",
        "format_reshuffled",
        "format_no_signal",
        "noisy",
        "other_saturation",
        "big_outage",
    ]
    ablation_specs = [
        ("default", "lag_correction"),
        ("default", "format_prior"),
        ("default", "recency_decay"),
        ("default", "curvature"),
        ("default", "exploration"),
        ("slow_lag", "lag_correction"),
        ("big_outage", "lag_correction"),
        ("format_no_signal", "format_prior"),
        ("format_reshuffled", "format_prior"),
        ("fast_fatigue", "recency_decay"),
        ("other_saturation", "curvature"),
    ]

    meta_line = "- **Source:** Regenerated from saved `evidence/*.txt` files (15 paired seeds `1000..1014`, 2000 bootstrap replicates; original runtime `831.3s`)."
    world_rows = []
    for w in world_names:
        txt = (EVIDENCE_DIR / f"world_{w}.txt").read_text(encoding="utf-8")
        pol_line = [ln for ln in txt.splitlines() if ln.startswith("Policy (policy.py)")][0]
        y_rev_m = _parse_ci(pol_line.split("|")[1])[0]
        yours_line = [ln for ln in txt.splitlines() if "(yours - B1) / (clairvoyant - B1):" in ln][0]
        b2_line = [ln for ln in txt.splitlines() if "(B2    - B1) / (clairvoyant - B1):" in ln][0]
        cl_line = [ln for ln in txt.splitlines() if "Clairvoyant gain over B1 (%)" in ln][0]
        y_ci = _parse_ci(yours_line.split(":", 1)[1])
        b_ci = _parse_ci(b2_line.split(":", 1)[1])
        gm = float(re.search(r"([+-]?\d+\.\d+)%", cl_line).group(1))  # type: ignore[union-attr]
        world_rows.append((w, y_rev_m, y_ci, b_ci, gm))

    ablation_rows = []
    for w, opt_k in ablation_specs:
        txt = (EVIDENCE_DIR / f"ablation_{w}_{opt_k}_off.txt").read_text(encoding="utf-8")
        yours_line = [ln for ln in txt.splitlines() if "(yours - B1) / (clairvoyant - B1):" in ln][0]
        delta_line = [ln for ln in txt.splitlines() if "Paired delta (default ON - ablated OFF)" in ln][0]
        a_ci = _parse_ci(yours_line.split(":", 1)[1])
        d_ci = _parse_ci(delta_line.split(":", 1)[1])
        ablation_rows.append((w, opt_k, a_ci, d_ci))

    return world_rows, ablation_rows, meta_line


def _build_summary_markdown(
    world_rows: list[tuple[str, float, tuple[float, float, float], tuple[float, float, float], float]],
    ablation_rows: list[tuple[str, str, tuple[float, float, float], tuple[float, float, float]]],
    header_meta: list[str],
) -> str:
    summary_lines = [
        "# Pre-registered Checks & Ablations Summary",
        "",
        *header_meta,
        "",
        "## 1. Default & Shifted Worlds (Default Policy vs B1, B2, Clairvoyant)",
        "",
        "Criterion (PREREG Entry 5 #4): Policy must beat `B1` with a 95% CI strictly above `0` in every shifted world.",
        "",
        "| World | Policy Rev ($ mean) | `(yours - B1)/(cl - B1)` [95% CI] | `(B2 - B1)/(cl - B1)` [95% CI] | Clairvoyant Gain | Check Result |",
        "|---|---:|---:|---:|---:|---|",
    ]

    ci_by_world: dict[str, tuple[float, float, float]] = {}
    for w_name, y_rev_m, y_ci, b_ci, gm in world_rows:
        ci_by_world[w_name] = y_ci
        status = "**PASS**" if y_ci[1] > 0.0 else "**FAIL** (95% CI includes <= 0)"
        summary_lines.append(
            f"| `{w_name}` | ${y_rev_m:,.0f} | {_fmt_ci(y_ci)} | {_fmt_ci(b_ci)} | {gm:+.1f}% | {status} |"
        )

    summary_lines.extend(
        [
            "",
            "## 2. Ablations (Paired Default ON minus Ablated OFF)",
            "",
            "Criteria (PREREG Entry 5 #1–#3 & Entry 6 correction): On `default`, turning a component off must lower the headline (if turning OFF significantly improves it, label `FAIL (harmful)`). On `format_no_signal`, `format_prior=true` must not be worse than `false` by more than the 95% interval.",
            "",
            "| World | Ablated Flag (`off`) | Ablated `(yours-B1)/(cl-B1)` [95% CI] | Paired Delta `(ON - OFF)` [95% CI] | Pre-registered Verdict |",
            "|---|---|---:|---:|---|",
        ]
    )

    def_lo, def_hi = ci_by_world["default"][1], ci_by_world["default"][2]
    for w_name, opt_k, a_ci, d_ci in ablation_rows:
        verdict = _ablation_verdict(
            w_name=w_name,
            opt_k=opt_k,
            am=a_ci[0],
            dm=d_ci[0],
            dlo=d_ci[1],
            dhi=d_ci[2],
            def_lo=def_lo,
            def_hi=def_hi,
            world_lo=ci_by_world[w_name][1],
        )
        summary_lines.append(
            f"| `{w_name}` | `{opt_k}=false` | {_fmt_ci(a_ci)} | {_fmt_ci(d_ci)} | {verdict} |"
        )

    summary_lines.extend(
        [
            "",
            "## 3. Output Files",
            "- Per-world and per-ablation raw evaluation printouts are saved in `evidence/world_*.txt` and `evidence/ablation_*.txt`.",
        ]
    )
    return "\n".join(summary_lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run all pre-registered shifted-world checks and ablations.")
    parser.add_argument("--seeds", type=int, default=15, help="Paired seeds per world/ablation.")
    parser.add_argument("--start-seed", type=int, default=1000, help="Starting seed.")
    parser.add_argument("--n-boot", type=int, default=2000, help="Bootstrap replicates.")
    parser.add_argument(
        "--from-evidence",
        action="store_true",
        help="Regenerate evidence/summary.md from saved evidence/*.txt without rerunning simulations.",
    )
    args = parser.parse_args()

    if args.from_evidence:
        w_rows, a_rows, meta_line = _parse_saved_evidence()
        summary_text = _build_summary_markdown(w_rows, a_rows, [meta_line])
        (EVIDENCE_DIR / "summary.md").write_text(summary_text, encoding="utf-8")
        print(summary_text)
        return

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

        ablation_rows: list[tuple[str, str, tuple[float, float, float], tuple[float, float, float]]] = []
        for w_name, opt_k, opt_v in ablation_specs:
            abl_res, delta_ci = _run_ablation_on_cached_world(
                world_results[w_name], opt_k, opt_v, n_boot=args.n_boot
            )
            txt = (
                format_evaluation_report(abl_res)
                + f"\n\nPaired delta (default ON - ablated OFF) in normalized score: {_fmt_ci(delta_ci)}\n"
            )
            fname = f"ablation_{w_name}_{opt_k}_off.txt"
            (EVIDENCE_DIR / fname).write_text(txt, encoding="utf-8")
            ablation_rows.append((w_name, opt_k, abl_res["yours_norm_ci"], delta_ci))

    elapsed = time.perf_counter() - t0
    world_rows = [
        (
            w_name,
            float(np.mean(world_results[w_name]["rev_by_pol"]["Policy (policy.py)"])),
            world_results[w_name]["yours_norm_ci"],
            world_results[w_name]["b2_norm_ci"],
            float(world_results[w_name]["cl_gain_pct_ci"][0]),
        )
        for w_name, _ in worlds
    ]
    header_meta = [
        f"- **Command:** `uv run python -m sim.run_checks --seeds {args.seeds} --start-seed {args.start_seed}`",
        f"- **Seeds:** `{args.seeds}` paired seeds (`{args.start_seed}..{args.start_seed + args.seeds - 1}`), `{args.n_boot}` bootstrap replicates.",
        f"- **Runtime:** `{elapsed:.1f}s` (policy unmodified; `options=None` for all world runs).",
    ]
    summary_text = _build_summary_markdown(world_rows, ablation_rows, header_meta)
    (EVIDENCE_DIR / "summary.md").write_text(summary_text, encoding="utf-8")
    print(f"Completed all checks in {elapsed:.1f}s (n_seeds={args.seeds}). Summary written to evidence/summary.md.\n")
    print(summary_text)


if __name__ == "__main__":
    main()

