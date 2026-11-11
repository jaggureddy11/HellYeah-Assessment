# Robustness Check Summary (`V0` vs `V1` vs `V2`, seeds `4000..4019`)

- **Command:** `uv run python -m sim.run_robustness --seeds 20 --start-seed 4000`
- **Fresh seeds:** `20` paired seeds (`4000..4019`), `2000` bootstrap replicates.
- **Runtime:** `286.6s` (`720` runs across `6` workers).

## Variants
- `V0`: current default (`recency_decay=False, uniform_mix=0.0`)
- `V1`: `recency_decay=True` (12d half-life)
- `V2`: `recency_decay=False, uniform_mix=0.25`

## Headline `(yours - B1) / (clairvoyant - B1)` [95% CI] across 6 worlds

| World | `V0` (default) | `V1` (`recency=True`) | `V2` (`uniform_mix=0.25`) | `B2` | Cl Gain |
|---|---:|---:|---:|---:|---:|
| `default` | +0.8485 [+0.7936, +0.9045] PASS | +0.7962 [+0.7399, +0.8581] PASS | +0.5964 [+0.5327, +0.6612] PASS | -0.5505 [-0.6272, -0.4780] | +38.7% |
| `format_no_signal` | -0.2194 [-0.2925, -0.1519] **FAIL** | +0.1752 [+0.1298, +0.2182] PASS | -0.0316 [-0.0818, +0.0219] **FAIL** | -0.4821 [-0.5504, -0.4136] | +44.8% |
| `fast_fatigue` | +0.6239 [+0.5771, +0.6685] PASS | +0.5916 [+0.5465, +0.6361] PASS | +0.4646 [+0.4280, +0.4991] PASS | -0.4361 [-0.4884, -0.3834] | +46.3% |
| `format_reshuffled` | +0.0700 [+0.0168, +0.1195] PASS | +0.2345 [+0.1823, +0.2840] PASS | +0.1417 [+0.1109, +0.1715] PASS | -0.6360 [-0.7020, -0.5735] | +37.3% |
| `strong_new_creatives` | +0.7363 [+0.6918, +0.7815] PASS | +0.6997 [+0.6422, +0.7572] PASS | +0.5993 [+0.5581, +0.6409] PASS | -0.4438 [-0.5004, -0.3877] | +42.7% |
| `weak_new_creatives` | +1.0063 [+0.9213, +1.0917] PASS | +0.8647 [+0.7791, +0.9509] PASS | +0.7269 [+0.6451, +0.8152] PASS | -0.5924 [-0.6793, -0.5077] | +37.2% |

## PREREG Entry 8 Selection Outcome

| Variant | Passes all 6 worlds? | `default` mean | Worst-world mean |
|---|---|---:|---:|
| `V0` | NO | +0.8485 | -0.2194 |
| `V1` | YES | +0.7962 | +0.1752 |
| `V2` | NO | +0.5964 | -0.0316 |

**Selected by PREREG Entry 8 rule:** **`V1`** — Variants passing all 6 worlds (`95% CI lo > 0`): ['V1']. Picked `V1` by highest `default`-world headline (+0.7962).
