"""Generate shifted world configs into sim/worlds/ from sim/world_config.json.

Edits config values only (no changes to sim/env.py).
Run from repository root:
    uv run python -m sim.make_shifted_worlds
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import numpy as np

BASE_CONFIG = Path("sim/world_config.json")
WORLDS_DIR = Path("sim/worlds")


def build_shifted_worlds(base_cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    worlds: dict[str, dict[str, Any]] = {}

    # 1. slow_lag: completion curve delayed by ~3 days (10 days to mature instead of 7)
    w_lag = copy.deepcopy(base_cfg)
    w_lag["attribution_lag"]["completion_by_age"] = [
        0.12,
        0.20,
        0.28,
        0.38,
        0.52,
        0.66,
        0.78,
        0.88,
        0.94,
        0.98,
    ]
    w_lag["shift_note"] = (
        "slow_lag: delayed attribution_lag.completion_by_age by ~3 days "
        "(10-day tail starting at 0.12 vs default 7-day tail starting at 0.3444)."
    )
    worlds["slow_lag"] = w_lag

    # 2. fast_fatigue: fatigue_rate ~2x across all creatives and global config
    w_fat = copy.deepcopy(base_cfg)
    w_fat["fatigue"]["base_rate_per_day"] = round(float(w_fat["fatigue"]["base_rate_per_day"]) * 2.0, 5)
    w_fat["fatigue"]["rate_sd"] = round(float(w_fat["fatigue"]["rate_sd"]) * 2.0, 5)
    for c in w_fat["creatives"]:
        c["fatigue_rate"] = round(float(c["fatigue_rate"]) * 2.0, 5)
    w_fat["shift_note"] = "fast_fatigue: doubled fatigue_rate (~2x) for all creatives and global fatigue params."
    worlds["fast_fatigue"] = w_fat

    # 3. format_reshuffled: permute format quality across formats
    w_perm = copy.deepcopy(base_cfg)
    old_means = {fmt: float(info["mean_base_roas"]) for fmt, info in base_cfg["format_priors"].items()}
    perm_map = {
        "story": "static_image",
        "static_image": "video_15s",
        "video_15s": "video_30s",
        "video_30s": "carousel",
        "carousel": "story",
    }
    for c in w_perm["creatives"]:
        fmt = c["format"]
        target_fmt = perm_map.get(fmt, fmt)
        scale = old_means[target_fmt] / max(0.2, old_means[fmt])
        c["true_base_roas"] = round(float(np.clip(float(c["true_base_roas"]) * scale, 0.35, 5.5)), 4)
    for fmt, info in w_perm["format_priors"].items():
        target_fmt = perm_map.get(fmt, fmt)
        info["mean_base_roas"] = old_means[target_fmt]
    w_perm["shift_note"] = (
        "format_reshuffled: permuted format quality multipliers across formats "
        f"({perm_map}) while keeping creative format labels unchanged."
    )
    worlds["format_reshuffled"] = w_perm

    # 4. format_no_signal: draw all 50 creatives' true_base_roas independent of format
    w_nosig = copy.deepcopy(base_cfg)
    rng_ns = np.random.default_rng(777)
    all_roas = [float(c["true_base_roas"]) for c in base_cfg["creatives"]]
    g_mean = float(np.mean(all_roas))
    g_sd = float(np.std(all_roas, ddof=1))
    for c in w_nosig["creatives"]:
        c["true_base_roas"] = round(float(np.clip(rng_ns.normal(g_mean, g_sd), 0.35, 5.0)), 4)
    w_nosig["shift_note"] = (
        f"format_no_signal: redrew all 50 creatives' true_base_roas ~ N({g_mean:.3f}, {g_sd:.3f}^2) "
        "independent of format (seed=777)."
    )
    worlds["format_no_signal"] = w_nosig

    # 5. noisy: noise ~2x
    w_noise = copy.deepcopy(base_cfg)
    for k in ("cpm_lognormal_sd", "ctr_lognormal_sd", "daily_cvr_lognormal_sd", "aov_lognormal_sd"):
        w_noise["noise"][k] = round(float(w_noise["noise"][k]) * 2.0, 4)
    w_noise["delivery_shortfall"]["shortfall_jitter_sd"] = round(
        float(w_noise["delivery_shortfall"]["shortfall_jitter_sd"]) * 2.0, 4
    )
    w_noise["shift_note"] = "noisy: doubled all noise SDs (CPM, CTR, daily CVR, AOV, shortfall jitter ~2x)."
    worlds["noisy"] = w_noise

    # 6. other_saturation: steeper earlier saturation curve
    w_sat = copy.deepcopy(base_cfg)
    w_sat["saturation"]["sat_floor"] = 0.20
    w_sat["saturation"]["sat_half_spend"] = 115.0
    w_sat["saturation"]["sat_power"] = 2.2
    w_sat["shift_note"] = (
        "other_saturation: steeper Hill saturation (sat_floor=0.20, sat_half_spend=115.0, sat_power=2.2 "
        "vs default 0.25, 230.0, 1.55)."
    )
    worlds["other_saturation"] = w_sat

    # 7. big_outage: second tracking outage window during days 61-90 (days 64-70 suppressed to 45%, resolved after day 76)
    w_out = copy.deepcopy(base_cfg)
    w_out["outage_window"] = {
        "enabled": True,
        "start_day": 64,
        "end_day": 70,
        "resolved_after_day": 76,
        "reported_multiplier": 0.45,
        "provenance": "Shifted test outage during days 64-70 (45% reported until resolved after day 76)",
    }
    w_out["shift_note"] = (
        "big_outage: active tracking outage on days 64-70 (reported_multiplier=0.45, resolved after day 76)."
    )
    worlds["big_outage"] = w_out

    # 8. strong_new_creatives: 10 unserved creatives have ~30% higher true_base_roas
    w_strong_new = copy.deepcopy(base_cfg)
    for c in w_strong_new["creatives"]:
        if c.get("launch_day") is None:
            c["true_base_roas"] = round(float(np.clip(float(c["true_base_roas"]) * 1.30, 0.35, 6.5)), 4)
    w_strong_new["shift_note"] = (
        "strong_new_creatives: multiplied true_base_roas by 1.30 (+30%) for all 10 unserved creatives (launch_day is null)."
    )
    worlds["strong_new_creatives"] = w_strong_new

    # 9. weak_new_creatives: 10 unserved creatives have ~30% lower true_base_roas
    w_weak_new = copy.deepcopy(base_cfg)
    for c in w_weak_new["creatives"]:
        if c.get("launch_day") is None:
            c["true_base_roas"] = round(float(np.clip(float(c["true_base_roas"]) * 0.70, 0.25, 5.5)), 4)
    w_weak_new["shift_note"] = (
        "weak_new_creatives: multiplied true_base_roas by 0.70 (-30%) for all 10 unserved creatives (launch_day is null)."
    )
    worlds["weak_new_creatives"] = w_weak_new

    return worlds


def main() -> None:
    base_cfg = json.loads(BASE_CONFIG.read_text(encoding="utf-8"))
    worlds = build_shifted_worlds(base_cfg)
    WORLDS_DIR.mkdir(parents=True, exist_ok=True)
    for name, cfg in worlds.items():
        out_path = WORLDS_DIR / f"{name}.json"
        out_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {out_path}: {cfg['shift_note']}")


if __name__ == "__main__":
    main()

