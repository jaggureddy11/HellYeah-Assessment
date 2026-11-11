# Constants Provenance Audit (`policy.py`)

This document lists every hard-coded numerical constant in `policy.py`, its exact location, where it came from, whether it was chosen by hand before vs after seeing simulator runs, and an explicit flag wherever a value matches or closely mirrors `sim/world_config.json` (or `sim/`).

---

## 1. Fallback Priors (`FALLBACK_PRIORS`, lines 21–40)

| Constant | Value | Provenance | Chosen Before Sim Results? | Mirrors `sim/`? |
|---|---:|---|---|---|
| `global_mean_roas` | `1.91` | `data/daily_final.csv` via `analysis/make_priors.py` (`1.9103`, rounded to 2 decimals) | Yes (from `data/*.csv`) | Indirectly (both fit `daily_final.csv`) |
| `pooled_roas` | `2.25` | `data/daily_final.csv` via `analysis/make_priors.py` (`2.2490`, rounded) | Yes (from `data/*.csv`) | Indirectly (both fit `daily_final.csv`) |
| `format_mean_roas` | `carousel: 1.87, static_image: 1.29, story: 2.60, video_15s: 2.33, video_30s: 1.60` | `data/creatives.csv` + `data/daily_final.csv` via `analysis/make_priors.py` | Yes (from `data/*.csv`) | Indirectly (both fit `daily_final.csv` format means) |
| `lag_completion_by_age` | `"1": 0.3444, "2": 0.5441, "3": 0.6609, "4": 0.7639, "5": 0.8472, "6": 0.9158, "7": 0.9718` | `data/daily_reported.csv / data/daily_final.csv` via `analysis/make_priors.py` | Yes (from `data/*.csv`) | **FLAG — EXACT MIRROR:** Matches `sim/world_config.json["attribution_lag"]["completion_by_age"]` (`[0.3444, ..., 0.9718]`), because both were fitted from the exact same day-60 export ratios in `data/`. |

---

## 2. Tunable Policy Options (`DEFAULT_OPTIONS`, lines 42–62)

| Constant | Value | Provenance | Chosen Before Sim Results? | Mirrors `sim/`? |
|---|---:|---|---|---|
| `half_life_days` | `12.0` | Chosen by hand (~10–14 day half-life from Q5 fatigue decay) | Yes (before running `Policy` in sim) | No |
| `prior_pseudo_spend` | `180.0` | Chosen by hand (~3 days of typical `$60/day` spend) | Yes | No |
| `lag_completion_floor` | `0.25` | Chosen by hand (caps max lag multiplier at `4x`) | Yes | No |
| `lag_online_prior_weight` | `2000.0` | Chosen by hand (`~$2,000`, equal to 1 day of account budget) | Yes | No (`2000.0` is `daily_budget` from `BRIEF.md`) |
| `max_share` | `0.14` | Chosen by hand (`$280/day` at `$2,000` budget; motivated by Q2 `[200+)` 62.4% shortfall rate) | Yes | **FLAG — CLOSE:** Keeps daily creative budget (`$280`) near `world_config.json` uncapped range `[220, 420]`. |
| `default_s0` | `260.0` | Chosen by hand from Q4 age-adjusted slope (`-0.349` ROAS per `+$100`) | Yes | **FLAG — CLOSE:** Close in scale to `world_config.json["saturation"]["sat_half_spend"] = 230.0` (different functional form: `s/(1+s/s0)` vs Hill `sat_power=1.55`). |
| `default_cap` | `260.0` | Chosen by hand from Q2 shortfall region (`$200+`) | Yes | **FLAG — CLOSE:** Sits inside `world_config.json` uncapped `daily_spend_cap` draw range `[220, 420]`. |
| `cold_start_test_dollars` | `22.0` | Chosen by hand from `data/daily_final.csv` CPM (`$12.76` -> `$22` yields `~1,720` impr/day) | Yes | No |
| `cold_start_test_days` | `2` | Chosen by hand (`2` days -> `~3,440` impressions total) | Yes | No |
| `explore_scale` | `0.65` | Chosen by hand (moderate Thompson sampling multiplier) | Yes | No |
| `obs_var_per_dollar` | `200.0` | Chosen by hand from Q3 (`within_daily_SD = 1.96` at `~$65/day` -> `1.96^2 * 65 ~ 250`) | Yes | Indirectly reflects `data/` daily noise level |
| `prior_roas_sd` | `0.75` | Chosen by hand from Q3 (`creative_ROAS_sd = 0.797`) | Yes | Indirectly reflects `data/` creative ROAS spread |
| `step_dollars` | `5.0` | Chosen by hand (greedy marginal allocation increment) | Yes | **FLAG — EXACT MATCH:** Matches `sim/clairvoyant.py` `step_dollars = 5.0` (both use `$5` greedy discretization). |

---

## 3. Internal Hard-Coded Constants in `policy.py` Methods

| Location | Constant | Meaning | Provenance | Chosen Before Sim Results? | Mirrors `sim/`? |
|---|---|---|---:|---|---|
| `__init__` (L117) | `2000.0` | Fallback `daily_budget` if input invalid | `BRIEF.md` (`$2,000/day`) | Yes | Matches `world_config.json["daily_budget"]` (from `BRIEF.md`) |
| `_update_and_get_completion` (L197–199) | `1 <= age <= 7`, `age >= 8` | Online lag window & maturity threshold | `data/` Q1 (7-day lag ends at age 7) | Yes | **FLAG — CLOSE:** Matches default 7-day `completion_by_age` length in `world_config.json` (fails to adapt beyond 7 days if lag > 7d). |
| `_estimate_s0` (L222) | `1e9` | Effective infinity when `curvature=False` | Chosen by hand | Yes | No |
| `_estimate_s0` (L233, L237) | `8`, `15.0` | Min active days (`>=8`) and spend SD (`>=15`) per creative | Chosen by hand | Yes | No |
| `_estimate_s0` (L249, L251) | `-1e-4`, `1.15` | Flat/positive slope threshold and mild upward multiplier | Chosen by hand | Yes | No |
| `_estimate_s0` (L253–257) | `0.5`, `1.7`, `110.0`, `650.0` | Slope-to-`s0` conversion factor and clamp bounds | Chosen by hand | Yes | No |
| `_estimate_s0` (L259) | `0.70`, `0.30` | Shrinkage weights (`70%` default, `30%` empirical) | Chosen by hand | Yes | No |
| `_compute_caps` (L264, L281) | `25.0`, `1.05` | Floor on cap (`$25`) and `5%` buffer above median shortfall | Chosen by hand | Yes | No |
| `_compute_caps` (L275) | `1.0`, `0.98`, `0.5` | Shortfall detection (`spend < 0.98 * budget - 0.5` for `budget >= 1`) | `$1` from `BRIEF.md`; `0.98`/`0.5` by hand | Yes | No |
| `_compute_caps` (L284) | `1.35` | Headroom multiplier above max observed spend | Chosen by hand | Yes | No |
| `allocate` (L371) | `pseudo_c = 3.0` | Prior pseudo-creative count for online global/format mean | Chosen by hand | Yes | No |
| `allocate` (L393, L416) | `1_000_003`, `0.05` | Deterministic `(seed, day)` RNG hash and min ROAS floor | Chosen by hand | Yes | No |
| `allocate` (L405) | `5` | Min `days_with_spend` before switching prior from format to global | Chosen by hand | Yes | No |
| `allocate` (L436, L442) | `0.35`, `1.0` | Max cold-start pool (`35%` of budget) and `$1` minimum test floor | `0.35` by hand; `$1.0` from `BRIEF.md` | Yes | `$1.0` matches `BRIEF.md` |
| `allocate` (L474, L496, L500) | `25.0`, `3`, `1.0` | Hard max fallback floor (`$25`), reclaim passes (`3`), `$1` floor | `$1.0` from `BRIEF.md`; others by hand | Yes | `$1.0` matches `BRIEF.md` |
| `allocate` (L525, L543, L552) | `1.05`, `1.0`, `5` | Explainability `explore` tag threshold, cap tag tolerance, top-5 log | Chosen by hand | Yes | No |

---

## Summary of Potential Risks Flagged
1. **`lag_completion_by_age` + `age >= 8` maturity cutoff:** Both `policy.py` and `sim/world_config.json` fitted the 7-day lag curve from `data/`. In a world with a >7-day lag (`slow_lag`), `age >= 8` freezes learning at age 7 instead of extending to longer ages.
2. **`default_s0 = 260.0` vs `sat_half_spend = 230.0` & `default_cap = 260.0`:** Although chosen from `data/` EDA (`Q2`, `Q4`) before seeing `Policy` simulator scores (and using a different functional curve `s/(1+s/s0)` vs Hill), both reflect the same underlying `data/` spend scale.
3. **None tuned post-hoc:** All constants above were written in `policy.py` (`b34def7`) before running `sim/run_checks.py` and have not been tuned after seeing simulator results.
