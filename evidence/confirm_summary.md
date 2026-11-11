# Fresh-Seed Confirmation Summary (`3000..3019`)

- **Command:** `uv run python -m sim.run_confirm --seeds 20 --start-seed 3000`
- **Fresh seeds:** `20` paired seeds (`3000..3019`), `2000` bootstrap replicates.
- **Policy defaults tested:** `recency_decay=False`, `cold_start_floor=True`, `thompson=True`, `lag_correction=True`, `format_prior=True`, `curvature=True`.
- **Runtime:** `364.7s` (`1020` 30-day runs across `6` worker processes).

## 1. Default & Shifted Worlds (Current Default Policy vs B1, B2, Clairvoyant)

| World | Policy Rev ($ mean) | `(yours - B1)/(cl - B1)` [95% CI] | `(B2 - B1)/(cl - B1)` [95% CI] | Clairvoyant Gain | Check Result |
|---|---:|---:|---:|---:|---|
| `default` | $138,202 | +0.7797 [+0.7349, +0.8268] | -0.5669 [-0.6317, -0.5027] | +39.0% | **PASS** |
| `slow_lag` | $137,290 | +0.7635 [+0.7094, +0.8127] | -0.6031 [-0.6559, -0.5507] | +39.0% | **PASS** |
| `fast_fatigue` | $104,930 | +0.6423 [+0.5864, +0.7059] | -0.4376 [-0.5070, -0.3730] | +47.5% | **PASS** |
| `format_reshuffled` | $114,125 | +0.1910 [+0.1280, +0.2578] | -0.6086 [-0.6934, -0.5247] | +38.6% | **PASS** |
| `format_no_signal` | $90,284 | -0.0564 [-0.1045, -0.0072] | -0.4459 [-0.4981, -0.3957] | +45.9% | **FAIL** (95% CI includes <= 0) |
| `noisy` | $132,220 | +0.7402 [+0.6482, +0.8370] | -0.5700 [-0.6602, -0.4755] | +39.1% | **PASS** |
| `other_saturation` | $113,342 | +0.4746 [+0.3656, +0.5734] | -2.5184 [-2.9200, -2.1801] | +19.3% | **PASS** |
| `big_outage` | $136,249 | +0.7381 [+0.6908, +0.7892] | -0.5604 [-0.6240, -0.4951] | +39.0% | **PASS** |
| `strong_new_creatives` | $150,554 | +0.7326 [+0.6888, +0.7817] | -0.3893 [-0.4344, -0.3401] | +44.5% | **PASS** |
| `weak_new_creatives` | $130,956 | +0.9056 [+0.8356, +0.9828] | -0.5881 [-0.6545, -0.5226] | +36.5% | **PASS** |

## 2. Ablations on `default`, `strong_new_creatives`, and `slow_lag`

| World | Variant / Ablation | Variant `(yours-B1)/(cl-B1)` [95% CI] | Paired Delta `(DEFAULT - variant)` [95% CI] | Verdict |
|---|---|---:|---:|---|
| `default` | `thompson_off` | +0.8427 [+0.7734, +0.9078] | -0.0631 [-0.1431, +0.0197] | **FAIL** (paired delta CI includes 0) |
| `default` | `cold_start_floor_off` | +0.8143 [+0.7672, +0.8653] | -0.0346 [-0.0907, +0.0257] | **FAIL** (paired delta CI includes 0) |
| `default` | `both_off` | +0.8366 [+0.7556, +0.9172] | -0.0569 [-0.1232, +0.0058] | **FAIL** (paired delta CI includes 0) |
| `default` | `lag_correction_off` | +0.7596 [+0.7063, +0.8129] | +0.0200 [-0.0257, +0.0644] | **FAIL** (no reliable benefit) |
| `default` | `format_prior_off` | +0.7496 [+0.6913, +0.8100] | +0.0301 [-0.0223, +0.0790] | **FAIL** (no reliable benefit) |
| `strong_new_creatives` | `thompson_off` | +0.7907 [+0.7192, +0.8562] | -0.0581 [-0.1203, +0.0087] | NEUTRAL (paired CI includes 0) |
| `strong_new_creatives` | `cold_start_floor_off` | +0.7383 [+0.6866, +0.7909] | -0.0057 [-0.0604, +0.0555] | NEUTRAL (paired CI includes 0) |
| `strong_new_creatives` | `both_off` | +0.7955 [+0.7362, +0.8515] | -0.0630 [-0.1139, -0.0068] | **FAIL (harmful)** (turning OFF significantly improves headline) |
| `strong_new_creatives` | `lag_correction_off` | +0.6662 [+0.6274, +0.7236] | +0.0663 [+0.0198, +0.1168] | **PASS** (ON beats OFF, CI > 0) |
| `strong_new_creatives` | `format_prior_off` | +0.6561 [+0.5883, +0.7241] | +0.0765 [+0.0070, +0.1488] | **PASS** (ON beats OFF, CI > 0) |
| `slow_lag` | `lag_correction_off` | +0.7063 [+0.6405, +0.7757] | +0.0571 [-0.0063, +0.1176] | **PASS (above B1)**, lag_correction delta CI includes 0 |

## 3. PREREG Entry 6(c) Cold-Start Variant Selection (Lightest -> Heaviest)

Rule: Keep the **lightest** variant that is (1) not worse than `both_off` by more than its paired 95% CI in `default` (`delta_hi >= 0`) AND (2) not below `both_off` in `strong_new_creatives` (`mean_delta_strong >= 0`).

| Order | Variant | Options | `default` score [95% CI] | `default` vs `both_off` [95% CI] | `strong_new` score [95% CI] | `strong_new` vs `both_off` [95% CI] | Passes Rule? |
|---:|---|---|---:|---:|---:|---:|---|
| 1 | `both_off` | `cold_start_floor=false, thompson=false` | +0.8366 [+0.7556, +0.9172] | +0.0000 [+0.0000, +0.0000] | +0.7955 [+0.7362, +0.8515] | +0.0000 [+0.0000, +0.0000] | **YES** |
| 2 | `cold_start_floor_only` | `cold_start_floor=true, thompson=false` | +0.8427 [+0.7734, +0.9078] | +0.0062 [-0.0725, +0.0885] | +0.7907 [+0.7192, +0.8562] | -0.0049 [-0.0769, +0.0661] | **NO** |
| 3 | `thompson_only` | `cold_start_floor=false, thompson=true` | +0.8143 [+0.7672, +0.8653] | -0.0223 [-0.1186, +0.0724] | +0.7383 [+0.6866, +0.7909] | -0.0572 [-0.1020, -0.0047] | **NO** |
| 4 | `both_on` | `cold_start_floor=true, thompson=true` | +0.7797 [+0.7349, +0.8268] | -0.0569 [-0.1232, +0.0058] | +0.7326 [+0.6888, +0.7817] | -0.0630 [-0.1139, -0.0068] | **NO** |

**Selected by PREREG Entry 6(c) rule:** `both_off` *(no defaults changed yet in `policy.py` pending confirmation)*.

## 4. Output Files
- Per-world and per-ablation raw printouts: `evidence/confirm_world_*.txt` and `evidence/confirm_ablation_*.txt`.
