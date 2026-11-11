# Pre-registered Checks & Ablations Summary

- **Command:** `uv run python -m sim.run_checks --seeds 15 --start-seed 1000`
- **Seeds:** `15` paired seeds (`1000..1014`), `2000` bootstrap replicates.
- **Runtime:** `831.3s` (policy unmodified; `options=None` for all world runs).

## 1. Default & Shifted Worlds (Default Policy vs B1, B2, Clairvoyant)

Criterion (PREREG Entry 5 #4): Policy must beat `B1` with a 95% CI strictly above `0` in every shifted world.

| World | Policy Rev ($ mean) | `(yours - B1)/(cl - B1)` [95% CI] | `(B2 - B1)/(cl - B1)` [95% CI] | Clairvoyant Gain | Check Result |
|---|---:|---:|---:|---:|---|
| `default` | $132,653 | +0.6296 [+0.5530, +0.7133] | -0.6140 [-0.6936, -0.5385] | +38.0% | **PASS** |
| `slow_lag` | $129,563 | +0.5511 [+0.5070, +0.5946] | -0.6327 [-0.7352, -0.5456] | +38.0% | **PASS** |
| `fast_fatigue` | $101,911 | +0.5769 [+0.4922, +0.6537] | -0.3915 [-0.4396, -0.3357] | +46.9% | **PASS** |
| `format_reshuffled` | $117,028 | +0.2447 [+0.1786, +0.3178] | -0.6824 [-0.7628, -0.6093] | +36.9% | **PASS** |
| `format_no_signal` | $102,490 | +0.1960 [+0.1290, +0.2589] | -0.4513 [-0.5064, -0.3907] | +45.1% | **PASS** |
| `noisy` | $130,688 | +0.7063 [+0.6418, +0.7717] | -0.6038 [-0.7549, -0.4710] | +39.4% | **PASS** |
| `other_saturation` | $111,007 | +0.3188 [+0.2092, +0.4211] | -2.8615 [-3.4076, -2.3676] | +17.9% | **PASS** |
| `big_outage` | $133,335 | +0.6480 [+0.5769, +0.7356] | -0.6034 [-0.6955, -0.5219] | +38.0% | **PASS** |

## 2. Ablations (Paired Default ON minus Ablated OFF)

Criteria (PREREG Entry 5 #1–#3): On `default`, turning a component off must measurably change/lower the headline (or else remove as unneeded complexity). On `format_no_signal`, `format_prior=true` must not be worse than `false` by more than the 95% interval.

| World | Ablated Flag (`off`) | Ablated `(yours-B1)/(cl-B1)` [95% CI] | Paired Delta `(ON - OFF)` [95% CI] | Pre-registered Verdict |
|---|---|---:|---:|---|
| `default` | `lag_correction=false` | +0.5625 [+0.4956, +0.6359] | +0.0671 [-0.0135, +0.1541] | **FAIL** (no reliable benefit; candidate to drop) |
| `default` | `format_prior=false` | +0.5664 [+0.4752, +0.6532] | +0.0633 [-0.0477, +0.1697] | **FAIL** (no reliable benefit; candidate to drop) |
| `default` | `recency_decay=false` | +0.8004 [+0.7446, +0.8504] | -0.1708 [-0.2703, -0.0578] | **PASS** (measurably changes headline; paired CI excludes 0) |
| `default` | `curvature=false` | +0.1902 [+0.0970, +0.2769] | +0.4394 [+0.3257, +0.5581] | **PASS** (measurably changes headline; paired CI excludes 0) |
| `default` | `exploration=false` | +0.8322 [+0.7945, +0.8773] | -0.2025 [-0.2971, -0.1176] | **PASS** (measurably changes headline; paired CI excludes 0) |
| `slow_lag` | `lag_correction=false` | +0.5368 [+0.4874, +0.5850] | +0.0143 [-0.0327, +0.0615] | **PASS (above B1)**, lag_correction delta CI includes 0 |
| `big_outage` | `lag_correction=false` | +0.6067 [+0.5486, +0.6625] | +0.0412 [-0.0375, +0.1295] | NEUTRAL (paired CI includes 0) |
| `format_no_signal` | `format_prior=false` | +0.2504 [+0.2004, +0.3012] | -0.0545 [-0.1425, +0.0301] | **PASS (borderline)** (slightly worse than off, within 95% CI) |
| `format_reshuffled` | `format_prior=false` | +0.2765 [+0.2232, +0.3312] | -0.0318 [-0.1222, +0.0535] | **PASS (borderline)** (slightly worse than off, within 95% CI) |
| `fast_fatigue` | `recency_decay=false` | +0.6494 [+0.5745, +0.7153] | -0.0726 [-0.1554, +0.0084] | NEUTRAL (paired CI includes 0) |
| `other_saturation` | `curvature=false` | -2.5247 [-3.0904, -2.0563] | +2.8435 [+2.3679, +3.3984] | **PASS** (ON beats OFF, CI > 0) |

## 3. Output Files
- Per-world and per-ablation raw evaluation printouts are saved in `evidence/world_*.txt` and `evidence/ablation_*.txt`.
