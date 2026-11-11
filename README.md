# Creative Rotation Brain

## 1. What I built first and why

I started with append-only [PREREG.md](PREREG.md) from `data/*.csv`, then a config-driven simulator (`sim/env.py`, `sim/world_config.json`) with `B1`, `B2`, and a myopic `Clairvoyant`, then `Policy` (`policy.py`: 7-day lag correction, recency-weighted ROAS with a `format` prior for 10 unserved creatives, concave diminishing returns, and delivery caps). Of those parts, curvature was clearly worth it (ablation `+0.4394` on `default`, seeds `1000..1014`, `evidence/summary.md`), while lag correction and `format` prior were kept on weaker evidence (`+0.0200` and `+0.0301` on `default` with CIs crossing `0`; clearly positive only in `strong_new_creatives`: `+0.0663` and `+0.0765`, `evidence/confirm_summary.md`).

## 2. What I skipped and why

- **`hook_type`/`cta` priors:** Worsened leave-one-out ROAS error.
- **Competitor signals:** Absent on days 61–90; 235-row / 22-launch CPM spike had no placebo test.
- **Copy NLP & weekday effects:** Only 40 served creatives; weekday shifts were noise.
- **Media-buyer UI/override:** `Policy.explain(day)` exists; UI not attempted in the time.

## 3. What the evidence does and does not establish

On fresh seeds `5000..5019` (`evidence/final_summary.md`), `Policy` beats `B1` (`95% CI > 0`) in `10/10` worlds (`+0.2003 [+0.1411, +0.2547]` `format_no_signal` to `+0.8225 [+0.7518, +0.8966]` `weak_new_creatives`). On `default`, it scores `+0.8030 [+0.7342, +0.8709]` (`$138,953` vs `B1` `$106,840`, `Clairvoyant` `$147,217`), taking `31.10 ms` mean (`114.27 ms` max) per call and `1.003 s` max per 30-day run. About 20% of the clairvoyant gap remains (`+0.8030`) and I haven’t broken it down; `Clairvoyant` is myopic per day, so it isn’t a true ceiling. `B2` loses to `B1` (`-0.5861 [-0.6773, -0.4929]`, `$84,033`) likely from delivery caps (`B2` spend `$48,275.83` vs `B1` `$58,569.36` on `default`); I haven’t isolated the other cause, and the simulator may punish concentration more than a real account would.

**Limits:** Simulator and policy were built in one session, and several hand-chosen constants sit close to simulator values (`evidence/constants_provenance.md`). Intervals understate batch variation (`V0` `format_no_signal`: `-0.0564` on `3000..3019` vs `-0.2194` on `4000..4019`). `sim.calibrate` is in-sample only (~2.7% spend under-delivery). Earlier seeds (`1000..1014`, `3000..3019`, `4000..4019`) informed switches, leaving `5000..5019` clean. Online lag learning stops at 7 days.

## 4. With one more day

Extend lag learning past 7 days on longer-lag worlds; placebo-test competitor launches; build a buyer override/UI over `Policy.explain(day)`; test more seed batches.

## 5. Commands

```bash
uv sync && uv run pytest
uv run python -m sim.run_final --seeds 20 --start-seed 5000
```
