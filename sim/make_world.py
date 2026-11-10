"""Generate sim/world_config.json from data/*.csv and analysis/out/summary.txt findings.

Uses only numpy and the Python standard library.
Run from repository root:
    python -m sim.make_world
"""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

SEED = 42
DATA_DIR = Path("data")
OUT_CONFIG = Path("sim/world_config.json")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def hill_saturation(spend: float, sat_floor: float, sat_half_spend: float, sat_power: float) -> float:
    """Smooth Hill-type saturation multiplier in (sat_floor, 1.0]."""
    if spend <= 0.0:
        return 1.0
    return float(sat_floor + (1.0 - sat_floor) / (1.0 + (spend / sat_half_spend) ** sat_power))


def build_world_config(seed: int = SEED) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    creatives_raw = _read_csv(DATA_DIR / "creatives.csv")
    final_raw = _read_csv(DATA_DIR / "daily_final.csv")
    reported_raw = _read_csv(DATA_DIR / "daily_reported.csv")

    # Group daily_final rows by creative_id
    by_creative: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in final_raw:
        by_creative[r["creative_id"]].append(
            {
                "day": int(r["day"]),
                "budget": float(r["budget"]),
                "spend": float(r["spend"]),
                "impressions": int(r["impressions"]),
                "clicks": int(r["clicks"]),
                "purchases": int(r["purchases"]),
                "revenue": float(r["revenue"]),
            }
        )

    # 1. Saturation curve parameters (Hill-type; non-linear vs policy linear/log assumptions)
    # Calibrated so age-adjusted within-creative decline matches ~ -0.35 ROAS per +$100 spend
    # around typical $50-$150 daily spend (analysis/out/summary.txt Q4).
    sat_floor = 0.25
    sat_half_spend = 230.0
    sat_power = 1.55

    # 2. Fatigue parameters (exponential decay exp(-rate * age))
    # Empirical ROAS slope is -0.123 / 10d on mean ROAS ~1.91 (~ -0.0064 / day),
    # and CTR slope is -0.082 pp / 10d on ~1.58% CTR (~ -0.0052 / day) (analysis/out/summary.txt Q5).
    base_fatigue_rate = 0.0065
    fatigue_rate_sd = 0.0020

    # 3. Global baseline funnel rates from daily_final.csv
    tot_spend = sum(float(r["spend"]) for r in final_raw)
    tot_impr = sum(int(r["impressions"]) for r in final_raw)
    tot_clicks = sum(int(r["clicks"]) for r in final_raw)
    tot_pur = sum(int(r["purchases"]) for r in final_raw)
    tot_rev = sum(float(r["revenue"]) for r in final_raw)
    global_cpm = 1000.0 * tot_spend / tot_impr
    global_ctr = tot_clicks / tot_impr
    global_aov = tot_rev / tot_pur

    # 4. Attribution lag completion curve from reported vs final by age at day 60 (days 54..60)
    rep_by_day: dict[int, float] = defaultdict(float)
    fin_by_day: dict[int, float] = defaultdict(float)
    for r in reported_raw:
        rep_by_day[int(r["day"])] += float(r["revenue"])
    for r in final_raw:
        fin_by_day[int(r["day"])] += float(r["revenue"])

    lag_curve = []
    for age in range(7):
        d = 60 - age
        ratio = rep_by_day[d] / fin_by_day[d] if fin_by_day[d] > 0 else 1.0
        lag_curve.append(round(float(min(1.0, max(0.05, ratio))), 4))

    # Days 47-49 outage ratio from analysis/out/summary.txt Q1
    outage_rep = sum(rep_by_day[d] for d in (47, 48, 49))
    outage_fin = sum(fin_by_day[d] for d in (47, 48, 49))
    outage_mult = round(float(outage_rep / outage_fin), 4)

    # 5. Compute served creative stats and format-level priors
    served_stats: dict[str, dict[str, float]] = {}
    by_format_roas: dict[str, list[float]] = defaultdict(list)

    for c in creatives_raw:
        cid = c["creative_id"]
        rows = by_creative.get(cid, [])
        if not rows:
            continue
        ld = int(float(c["launch_day"]))
        pos_rows = [r for r in rows if r["spend"] > 0.0]
        c_spend = sum(r["spend"] for r in pos_rows)
        c_rev = sum(r["revenue"] for r in pos_rows)
        c_impr = sum(r["impressions"] for r in pos_rows)
        c_clicks = sum(r["clicks"] for r in pos_rows)
        c_pur = sum(r["purchases"] for r in pos_rows)
        emp_roas = c_rev / c_spend if c_spend > 0 else 1.5
        c_cpm = 1000.0 * c_spend / c_impr if c_impr > 0 else global_cpm
        c_ctr = c_clicks / c_impr if c_impr > 0 else global_ctr
        c_aov = c_rev / c_pur if c_pur > 0 else global_aov

        # De-bias empirical ROAS back to (age=0, spend->0) using mean historical multiplier
        eff_mult_sum = 0.0
        for r in pos_rows:
            age = max(0, r["day"] - ld)
            sat = hill_saturation(r["spend"], sat_floor, sat_half_spend, sat_power)
            fat = math.exp(-base_fatigue_rate * age)
            eff_mult_sum += r["spend"] * sat * fat
        avg_mult = eff_mult_sum / c_spend if c_spend > 0 else 1.0
        base_roas = emp_roas / max(0.25, avg_mult)

        # Delivery cap from observed shortfalls (spend < budget)
        shortfall_spends = [r["spend"] for r in rows if r["spend"] < r["budget"] - 1e-6 and r["budget"] >= 1.0]
        if shortfall_spends:
            spend_cap = float(np.mean(shortfall_spends))
        else:
            # Uncapped in history: draw high cap above max observed budget so high-budget tests still cap realistically
            max_b = max(r["budget"] for r in rows)
            spend_cap = float(max(max_b * 1.15, rng.uniform(220.0, 420.0)))

        # Per-creative fatigue rate around baseline
        c_fatigue = float(np.clip(rng.normal(base_fatigue_rate, fatigue_rate_sd), 0.001, 0.015))

        served_stats[cid] = {
            "emp_roas": emp_roas,
            "base_roas": base_roas,
            "cpm": c_cpm,
            "ctr": c_ctr,
            "aov": c_aov,
            "spend_cap": spend_cap,
            "fatigue_rate": c_fatigue,
            "n_shortfalls": len(shortfall_spends),
        }
        by_format_roas[c["format"]].append(base_roas)

    all_base = [v["base_roas"] for v in served_stats.values()]
    global_base_mean = float(np.mean(all_base))
    global_base_sd = float(np.std(all_base, ddof=1))

    format_priors: dict[str, dict[str, Any]] = {}
    for fmt, vals in sorted(by_format_roas.items()):
        # Use residual spread wider than raw format SD because format explained ~23% of variance (Q7)
        within_sd = float(np.std(vals, ddof=1)) if len(vals) > 1 else global_base_sd
        wider_sd = max(within_sd * 1.25, global_base_sd * 0.95)
        format_priors[fmt] = {
            "n_served": len(vals),
            "mean_base_roas": round(float(np.mean(vals)), 4),
            "sample_sd_base_roas": round(within_sd, 4),
            "new_creative_draw_sd": round(wider_sd, 4),
        }

    # Build creative entries for all 50 creatives
    creatives_cfg: list[dict[str, Any]] = []
    for c in creatives_raw:
        cid = c["creative_id"]
        fmt = c["format"]
        ld_raw = c["launch_day"].strip()
        if ld_raw:
            st = served_stats[cid]
            creatives_cfg.append(
                {
                    "creative_id": cid,
                    "format": fmt,
                    "hook_type": c["hook_type"],
                    "launch_day": int(float(ld_raw)),
                    "true_base_roas": round(st["base_roas"], 4),
                    "fatigue_rate": round(st["fatigue_rate"], 5),
                    "base_cpm": round(st["cpm"], 3),
                    "base_ctr": round(st["ctr"], 5),
                    "base_aov": round(st["aov"], 2),
                    "daily_spend_cap": round(st["spend_cap"], 2),
                    "provenance": f"Fitted from daily_final.csv ({cid}, n_shortfalls={st['n_shortfalls']})",
                }
            )
        else:
            fp = format_priors.get(fmt, {"mean_base_roas": global_base_mean, "new_creative_draw_sd": global_base_sd})
            drawn_roas = float(np.clip(rng.normal(fp["mean_base_roas"], fp["new_creative_draw_sd"]), 0.35, 5.0))
            drawn_fatigue = float(np.clip(rng.normal(base_fatigue_rate, fatigue_rate_sd), 0.001, 0.015))
            drawn_cpm = float(np.clip(rng.normal(global_cpm, 1.8), 7.5, 22.0))
            drawn_ctr = float(np.clip(rng.normal(global_ctr, 0.004), 0.005, 0.035))
            drawn_aov = float(np.clip(rng.lognormal(math.log(65.0), 0.35), 35.0, 180.0))
            drawn_cap = float(rng.uniform(140.0, 380.0))
            creatives_cfg.append(
                {
                    "creative_id": cid,
                    "format": fmt,
                    "hook_type": c["hook_type"],
                    "launch_day": None,
                    "true_base_roas": round(drawn_roas, 4),
                    "fatigue_rate": round(drawn_fatigue, 5),
                    "base_cpm": round(drawn_cpm, 3),
                    "base_ctr": round(drawn_ctr, 5),
                    "base_aov": round(drawn_aov, 2),
                    "daily_spend_cap": round(drawn_cap, 2),
                    "provenance": (
                        f"Unserved cold-start draw from format='{fmt}' mean={fp['mean_base_roas']} "
                        f"with wider sd={fp['new_creative_draw_sd']} (Q7 format OOS R^2=0.23)"
                    ),
                }
            )

    config: dict[str, Any] = {
        "schema_version": 1,
        "seed": seed,
        "provenance": "Generated by sim/make_world.py from data/*.csv and analysis/out/summary.txt",
        "daily_budget": 2000.0,
        "min_allocation_dollars": {
            "value": 1.0,
            "provenance": "BRIEF.md Protocol + daily_final.csv Q2 (n=51 rows < $1 had $0 spend)",
        },
        "saturation": {
            "type": "hill",
            "sat_floor": sat_floor,
            "sat_half_spend": sat_half_spend,
            "sat_power": sat_power,
            "formula": "sat_floor + (1 - sat_floor) / (1 + (spend / sat_half_spend) ** sat_power)",
            "provenance": "Non-linear Hill saturation; calibrated to match Q4 age-adjusted slope (~ -0.35 ROAS per +$100 spend)",
        },
        "delivery_shortfall": {
            "global_soft_knee": 160.0,
            "global_soft_scale": 340.0,
            "shortfall_jitter_sd": 0.06,
            "provenance": "daily_final.csv Q2 delivery curve (96.2% in [100,200), 72.2% in [200+)) + per-creative caps",
        },
        "fatigue": {
            "type": "exponential_age_decay",
            "base_rate_per_day": base_fatigue_rate,
            "rate_sd": fatigue_rate_sd,
            "ctr_fatigue_share": 0.70,
            "provenance": "analysis/out/summary.txt Q5 (ROAS -0.123/10d, CTR -0.082pp/10d)",
        },
        "attribution_lag": {
            "completion_by_age": lag_curve,
            "provenance": "daily_reported.csv / daily_final.csv ratio at day 60 for ages 0..6 (Q1); >=7d is 1.0",
        },
        "outage_window": {
            "enabled": True,
            "start_day": 47,
            "end_day": 49,
            "resolved_after_day": 60,
            "reported_multiplier": outage_mult,
            "provenance": "analysis/out/summary.txt Q1 days 47-49 anomaly (rev_fin/rep=1.6924 at day 60, resolved by day 67)",
        },
        "noise": {
            "cpm_lognormal_sd": 0.06,
            "ctr_lognormal_sd": 0.14,
            "daily_cvr_lognormal_sd": 0.35,
            "aov_lognormal_sd": 0.35,
            "provenance": "Calibrated for Poisson purchase count + AOV variance (~34.7% zero-purchase days, Q3 within-creative SD ~1.96)",
        },
        "format_priors": format_priors,
        "creatives": creatives_cfg,
    }
    return config


def main() -> None:
    cfg = build_world_config(SEED)
    OUT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    OUT_CONFIG.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUT_CONFIG} with {len(cfg['creatives'])} creatives (seed={SEED}).")


if __name__ == "__main__":
    main()

