# Final Confirmation Summary (Fresh Seeds `5000..5019`)

- **Command:** `uv run python -m sim.run_final --seeds 20 --start-seed 5000`
- **Fresh seeds:** `20` paired seeds (`5000..5019`), `2000` bootstrap replicates.
- **Policy defaults (`policy.py`):** `recency_decay=True`, `uniform_mix=0.0`, `cold_start_floor=False`, `thompson=False`, `lag_correction=True`, `format_prior=True`, `curvature=True`.
- **Total suite wall time:** `317.5s` (`800` runs across `6` workers).

## 1. All 10 Worlds (Default Policy vs B1, B2, Clairvoyant)

Criterion: `95% CI` of `(yours - B1) / (clairvoyant - B1)` strictly above `0` vs `B1`.

| World | Policy Rev ($) | B1 Rev ($) | B2 Rev ($) | Clairvoyant ($) | `(yours - B1)/(cl - B1)` [95% CI] | `(B2 - B1)/(cl - B1)` [95% CI] | Result |
|---|---:|---:|---:|---:|---:|---:|---|
| `default` | $138,953 | $106,840 | $84,033 | $147,217 | +0.8030 [+0.7342, +0.8709] | -0.5861 [-0.6773, -0.4929] | **PASS** |
| `slow_lag` | $135,920 | $106,840 | $81,712 | $147,217 | +0.7273 [+0.6745, +0.7779] | -0.6374 [-0.7088, -0.5632] | **PASS** |
| `fast_fatigue` | $102,898 | $80,086 | $66,114 | $119,413 | +0.5794 [+0.5313, +0.6358] | -0.3639 [-0.4271, -0.3075] | **PASS** |
| `format_reshuffled` | $118,446 | $105,978 | $81,403 | $145,292 | +0.3099 [+0.2490, +0.3701] | -0.6455 [-0.7245, -0.5651] | **PASS** |
| `format_no_signal` | $102,800 | $94,746 | $72,924 | $134,460 | +0.2003 [+0.1411, +0.2547] | -0.5807 [-0.6891, -0.4858] | **PASS** |
| `noisy` | $131,913 | $103,150 | $78,521 | $143,628 | +0.7241 [+0.6353, +0.8221] | -0.6304 [-0.7344, -0.5329] | **PASS** |
| `other_saturation` | $112,856 | $103,940 | $56,161 | $123,728 | +0.4472 [+0.3199, +0.5668] | -2.6218 [-3.0325, -2.2542] | **PASS** |
| `big_outage` | $136,982 | $106,840 | $81,743 | $147,217 | +0.7481 [+0.7094, +0.7842] | -0.6386 [-0.7243, -0.5477] | **PASS** |
| `strong_new_creatives` | $151,663 | $114,256 | $92,981 | $165,190 | +0.7375 [+0.6826, +0.7910] | -0.4209 [-0.4646, -0.3756] | **PASS** |
| `weak_new_creatives` | $129,828 | $98,194 | $76,801 | $137,097 | +0.8225 [+0.7518, +0.8966] | -0.5579 [-0.6258, -0.4832] | **PASS** |

**Summary:** `10/10` worlds passed (`95% CI > 0` vs `B1`).

## 2. Policy Runtime Benchmark (`default` world, sequential, Policy alone)

- **Measured over:** `20` full 30-day runs (`600` `allocate()` calls on seeds `5000..5019`).
- **Mean time per `allocate()` call:** `31.10 ms` (`0.0311 s`, limit `10.0 s`).
- **Max time per `allocate()` call:** `114.27 ms` (`0.1143 s`, limit `10.0 s`).
- **Mean total time per 30-day run (init + 30 calls):** `0.934 s` (limit `600.0 s`).
- **Max total time per 30-day run (init + 30 calls):** `1.003 s` (limit `600.0 s`).

## 3. Raw Printouts
- Per-world evaluation files: `evidence/final_world_*.txt`.

Reproduced identically across all 10 worlds and all 20 seeds (`5000..5019`) on the shipped policy at commit `644a4f2` after the operational fixes (verified `2026-11-11 06:35:44 IST (+0530)`).
