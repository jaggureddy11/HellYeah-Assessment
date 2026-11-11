"""Creative Rotation Brain daily budget-allocation policy.

Uses only numpy and the Python standard library. Never imports from sim/ or
reads sim/world_config.json. Loads optional priors from policy_priors.json
with hard-coded fallbacks.
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

PRIORS_PATH = Path(__file__).resolve().parent / "policy_priors.json"

FALLBACK_PRIORS: dict[str, Any] = {
    "global_mean_roas": 1.91,
    "pooled_roas": 2.25,
    "format_mean_roas": {
        "carousel": 1.87,
        "static_image": 1.29,
        "story": 2.60,
        "video_15s": 2.33,
        "video_30s": 1.60,
    },
    "lag_completion_by_age": {
        "1": 0.3444,
        "2": 0.5441,
        "3": 0.6609,
        "4": 0.7639,
        "5": 0.8472,
        "6": 0.9158,
        "7": 0.9718,
    },
}

DEFAULT_OPTIONS: dict[str, Any] = {
    "lag_correction": True,
    "format_prior": True,
    "recency_decay": True,
    "curvature": True,
    "cold_start_floor": False,
    "thompson": False,
    "uniform_mix": 0.0,
    "half_life_days": 12.0,
    "prior_pseudo_spend": 180.0,
    "lag_completion_floor": 0.25,
    "lag_online_prior_weight": 2000.0,
    "max_share": 0.14,
    "default_s0": 260.0,
    "default_cap": 260.0,
    "cold_start_test_dollars": 22.0,
    "cold_start_test_days": 2,
    "explore_scale": 0.65,
    "obs_var_per_dollar": 200.0,
    "prior_roas_sd": 0.75,
    "step_dollars": 5.0,
    "log_stderr": True,
}


def _to_finite_nonneg(val: Any) -> float | None:
    try:
        x = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x) or x < 0.0:
        return None
    return x


def _to_int(val: Any) -> int | None:
    try:
        if isinstance(val, bool):
            return None
        x = int(val)
    except (TypeError, ValueError):
        return None
    return x


class Policy:
    """Empirical-Bayes + lag-aware + concave marginal allocation policy."""

    def __init__(
        self,
        creatives: list[dict[str, Any]],
        daily_budget: float,
        seed: int,
        options: dict[str, Any] | None = None,
    ) -> None:
        self.options: dict[str, Any] = dict(DEFAULT_OPTIONS)
        if isinstance(options, dict):
            self.options.update(options)

        self.creative_ids: list[str] = []
        self.creative_format: dict[str, str] = {}
        seen: set[str] = set()
        if isinstance(creatives, (list, tuple)):
            for row in creatives:
                if not isinstance(row, dict):
                    continue
                cid = row.get("creative_id")
                if cid is None:
                    continue
                cid_str = str(cid).strip()
                if cid_str and cid_str not in seen:
                    seen.add(cid_str)
                    self.creative_ids.append(cid_str)
                    self.creative_format[cid_str] = str(row.get("format") or "unknown").strip()
        self._creative_id_set: set[str] = set(self.creative_ids)

        budget_val = _to_finite_nonneg(daily_budget)
        self.daily_budget: float = budget_val if (budget_val is not None and budget_val > 0.0) else 2000.0

        seed_val = _to_int(seed)
        self.seed: int = seed_val if seed_val is not None else 0

        self.priors: dict[str, Any] = self._load_priors()
        self._day_cache: dict[int, dict[str, float]] = {}
        self._explanations: dict[int, dict[str, dict[str, Any]]] = {}

        # Online lag learning state:
        # _obs_by_key[(row_day, cid)][obs_age] = reported_revenue
        self._obs_by_key: dict[tuple[int, str], dict[int, float]] = {}
        self._matured_keys: set[tuple[int, str]] = set()
        self._online_lag_num: dict[int, float] = defaultdict(float)
        self._online_lag_den: dict[int, float] = defaultdict(float)

    def _load_priors(self) -> dict[str, Any]:
        try:
            if PRIORS_PATH.exists():
                loaded = json.loads(PRIORS_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    merged = dict(FALLBACK_PRIORS)
                    merged.update(loaded)
                    return merged
        except Exception:
            pass
        return dict(FALLBACK_PRIORS)

    def _clean_history(self, history: Any, current_day: int) -> list[dict[str, Any]]:
        """Validate, filter, and deduplicate (day, creative_id) keeping the latest entry."""
        if not isinstance(history, (list, tuple)):
            return []
        dedup: dict[tuple[int, str], dict[str, Any]] = {}
        for row in history:
            if not isinstance(row, dict):
                continue
            cid_raw = row.get("creative_id")
            if cid_raw is None:
                continue
            cid = str(cid_raw).strip()
            if cid not in self._creative_id_set:
                continue
            r_day = _to_int(row.get("day"))
            if r_day is None or r_day < 1 or r_day >= current_day:
                continue

            budget = _to_finite_nonneg(row.get("budget", 0.0))
            spend = _to_finite_nonneg(row.get("spend"))
            impr = _to_finite_nonneg(row.get("impressions", 0))
            clicks = _to_finite_nonneg(row.get("clicks", 0))
            pur = _to_finite_nonneg(row.get("purchases", 0))
            rev = _to_finite_nonneg(row.get("revenue"))
            if None in (budget, spend, impr, clicks, pur, rev):
                continue

            dedup[(r_day, cid)] = {
                "day": r_day,
                "age": current_day - r_day,
                "creative_id": cid,
                "budget": float(budget),  # type: ignore[arg-type]
                "spend": float(spend),  # type: ignore[arg-type]
                "impressions": float(impr),  # type: ignore[arg-type]
                "clicks": float(clicks),  # type: ignore[arg-type]
                "purchases": float(pur),  # type: ignore[arg-type]
                "revenue": float(rev),  # type: ignore[arg-type]
            }
        return [dedup[k] for k in sorted(dedup.keys())]

    def _update_and_get_completion(self, rows: list[dict[str, Any]]) -> dict[int, float]:
        """Learn completion ratios online from matured revisions and blend with priors."""
        prior_map = {
            int(k): float(v)
            for k, v in self.priors.get("lag_completion_by_age", FALLBACK_PRIORS["lag_completion_by_age"]).items()
        }
        if not bool(self.options.get("lag_correction", True)):
            return {a: 1.0 for a in range(1, 8)}

        for r in rows:
            key = (r["day"], r["creative_id"])
            age = r["age"]
            if 1 <= age <= 7:
                self._obs_by_key.setdefault(key, {})[age] = r["revenue"]
            elif age >= 8 and key not in self._matured_keys:
                self._matured_keys.add(key)
                rev_mature = r["revenue"]
                if rev_mature > 0.0 and key in self._obs_by_key:
                    for obs_age, obs_rev in self._obs_by_key[key].items():
                        self._online_lag_num[obs_age] += min(rev_mature, obs_rev)
                        self._online_lag_den[obs_age] += rev_mature

        floor = float(self.options.get("lag_completion_floor", 0.25))
        prior_w = float(self.options.get("lag_online_prior_weight", 2000.0))
        completion: dict[int, float] = {}
        for age in range(1, 8):
            c_prior = prior_map.get(age, 1.0)
            num = prior_w * c_prior + self._online_lag_num.get(age, 0.0)
            den = prior_w + self._online_lag_den.get(age, 0.0)
            blended = num / den if den > 0.0 else c_prior
            completion[age] = max(floor, min(1.0, blended))
        return completion

    def _estimate_s0(self, rows: list[dict[str, Any]], global_mean_roas: float) -> float:
        """Conservatively estimate curvature s0 from within-creative spend vs ROAS."""
        default_s0 = float(self.options.get("default_s0", 260.0))
        if not bool(self.options.get("curvature", True)):
            return 1e9

        by_c: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for r in rows:
            if r["spend"] > 0.0:
                by_c[r["creative_id"]].append((r["spend"], r["corr_revenue"] / r["spend"]))

        num = 0.0
        den = 0.0
        spends_all: list[float] = []
        for cid, pts in by_c.items():
            if len(pts) < 8:
                continue
            sp = np.array([p[0] for p in pts], dtype=float)
            ro = np.array([p[1] for p in pts], dtype=float)
            if float(np.std(sp)) < 15.0:
                continue
            sp_dm = sp - sp.mean()
            ro_dm = ro - ro.mean()
            num += float(np.dot(sp_dm, ro_dm))
            den += float(np.dot(sp_dm, sp_dm))
            spends_all.extend(sp.tolist())

        if den <= 1e-6 or not spends_all:
            return default_s0

        slope = num / den  # d(ROAS)/d($)
        if slope >= -1e-4:
            # Conservative shrinkage toward default_s0 when slope is flat/positive
            return default_s0 * 1.15

        rel_slope = -slope / max(0.5, global_mean_roas)
        s_bar = float(np.mean(spends_all))
        # For f(s) = 1 / (1 + s/s0), f'(s_bar) = -1 / (s0 * (1 + s_bar/s0)^2)
        est_s0 = max(110.0, min(650.0, 1.0 / (rel_slope * 1.7) - 0.5 * s_bar))
        est_s0 = max(110.0, min(650.0, est_s0))
        # Heavy conservative shrinkage toward default_s0 due to budget endogeneity
        return 0.70 * default_s0 + 0.30 * est_s0

    def _compute_caps(self, rows: list[dict[str, Any]]) -> dict[str, float]:
        """Estimate per-creative daily spend caps from observed delivery shortfalls and max_share."""
        max_share = float(self.options.get("max_share", 0.14))
        hard_max = max(25.0, max_share * self.daily_budget)
        default_cap = min(hard_max, float(self.options.get("default_cap", 260.0)))

        shortfalls: dict[str, list[float]] = defaultdict(list)
        max_obs_spend: dict[str, float] = defaultdict(float)
        for r in rows:
            cid = r["creative_id"]
            sp = r["spend"]
            b = r["budget"]
            if sp > max_obs_spend[cid]:
                max_obs_spend[cid] = sp
            if b >= 1.0 and sp < 0.98 * b - 0.5:
                shortfalls[cid].append(sp)

        caps: dict[str, float] = {}
        for cid in self.creative_ids:
            if shortfalls[cid]:
                obs_cap = max(25.0, float(np.median(shortfalls[cid])) * 1.05)
                caps[cid] = min(hard_max, obs_cap)
            elif max_obs_spend[cid] > 0.0:
                caps[cid] = min(hard_max, max(default_cap, 1.35 * max_obs_spend[cid]))
            else:
                caps[cid] = default_cap
        return caps

    def _normalize_shares(self, alloc_dollars: dict[str, float]) -> dict[str, float]:
        """Convert dollar allocations into finite, non-negative shares summing to 1.0."""
        if not self.creative_ids:
            return {}
        arr = np.array(
            [
                max(0.0, float(alloc_dollars.get(cid, 0.0)))
                if math.isfinite(float(alloc_dollars.get(cid, 0.0)))
                else 0.0
                for cid in self.creative_ids
            ],
            dtype=float,
        )
        total = float(arr.sum())
        if not math.isfinite(total) or total <= 0.0:
            arr = np.full(len(self.creative_ids), 1.0 / len(self.creative_ids), dtype=float)
        else:
            arr = arr / total
        return {cid: float(val) for cid, val in zip(self.creative_ids, arr)}

    def explain(self, day: int) -> dict[str, dict[str, Any]]:
        """Return stored per-creative decision reasons for the given day."""
        day_key = _to_int(day)
        if day_key is None or day_key not in self._explanations:
            return {}
        return {cid: dict(info) for cid, info in self._explanations[day_key].items()}

    def allocate(self, day: int, history: list[dict[str, Any]]) -> dict[str, float]:
        """Return a normalized {creative_id: share} split for the given day."""
        day_key = _to_int(day)
        if day_key is None:
            day_key = 0
        if day_key in self._day_cache:
            return dict(self._day_cache[day_key])

        if not self.creative_ids:
            return {}

        rows = self._clean_history(history, current_day=day_key)
        completion = self._update_and_get_completion(rows)

        recency_on = bool(self.options.get("recency_decay", True))
        half_life = max(1.0, float(self.options.get("half_life_days", 12.0)))

        # Apply lag correction and recency weights per row
        w_spend: dict[str, float] = defaultdict(float)
        w_rev: dict[str, float] = defaultdict(float)
        w_rev_raw: dict[str, float] = defaultdict(float)
        days_with_spend: dict[str, int] = defaultdict(int)
        lag_applied_flag: dict[str, bool] = defaultdict(bool)

        for r in rows:
            age = r["age"]
            comp = completion.get(age, 1.0) if 1 <= age <= 7 else 1.0
            corr_rev = r["revenue"] / comp
            r["corr_revenue"] = corr_rev
            if 1 <= age <= 7 and comp < 0.999 and r["revenue"] > 0.0:
                lag_applied_flag[r["creative_id"]] = True

            w = (0.5 ** ((age - 1) / half_life)) if recency_on else 1.0
            cid = r["creative_id"]
            if r["spend"] > 0.0:
                days_with_spend[cid] += 1
            w_spend[cid] += w * r["spend"]
            w_rev[cid] += w * corr_rev
            w_rev_raw[cid] += w * r["revenue"]

        # Compute online global and format means, shrunk lightly toward policy_priors.json
        prior_glob = float(self.priors.get("global_mean_roas", 1.91))
        prior_fmt_map = {
            str(k): float(v)
            for k, v in self.priors.get("format_mean_roas", FALLBACK_PRIORS["format_mean_roas"]).items()
        }

        c_roas_obs: list[float] = []
        fmt_roas_obs: dict[str, list[float]] = defaultdict(list)
        for cid in self.creative_ids:
            if w_spend[cid] > 0.0:
                est = w_rev[cid] / w_spend[cid]
                c_roas_obs.append(est)
                fmt_roas_obs[self.creative_format[cid]].append(est)

        pseudo_c = 3.0
        global_mean_roas = (
            (pseudo_c * prior_glob + sum(c_roas_obs)) / (pseudo_c + len(c_roas_obs))
            if c_roas_obs
            else prior_glob
        )

        format_mean_roas: dict[str, float] = {}
        all_formats = set(self.creative_format.values()) | set(prior_fmt_map.keys())
        for fmt in all_formats:
            p_f = prior_fmt_map.get(fmt, global_mean_roas)
            vals = fmt_roas_obs.get(fmt, [])
            format_mean_roas[fmt] = (pseudo_c * p_f + sum(vals)) / (pseudo_c + len(vals)) if vals else p_f

        # Empirical-Bayes posterior + Thompson sampling seeded strictly by (seed, day)
        pseudo_spend = float(self.options.get("prior_pseudo_spend", 180.0))
        format_on = bool(self.options.get("format_prior", True))
        thompson_on = bool(self.options.get("thompson", True))
        cold_floor_on = bool(self.options.get("cold_start_floor", True))
        explore_scale = float(self.options.get("explore_scale", 0.65))
        obs_var_per_dollar = float(self.options.get("obs_var_per_dollar", 200.0))
        prior_sd = float(self.options.get("prior_roas_sd", 0.75))

        day_seed = (self.seed * 1_000_003 + day_key) & 0xFFFFFFFF
        day_rng = np.random.default_rng(day_seed)

        mu_roas: dict[str, float] = {}
        sampled_roas: dict[str, float] = {}
        prior_used: dict[str, float] = {}
        prior_source: dict[str, str] = {}
        prior_weight_frac: dict[str, float] = {}

        for cid in sorted(self.creative_ids):
            ws = w_spend[cid]
            wr = w_rev[cid]
            little_history = ws < pseudo_spend or days_with_spend[cid] < 5
            if format_on and little_history:
                p_roas = format_mean_roas.get(self.creative_format[cid], global_mean_roas)
                p_src = "format"
            else:
                p_roas = global_mean_roas
                p_src = "global"

            post_mean = (wr + pseudo_spend * p_roas) / (ws + pseudo_spend)
            post_sd = math.sqrt(1.0 / (1.0 / (prior_sd * prior_sd) + ws / obs_var_per_dollar))
            z = float(day_rng.standard_normal())
            ts_roas = max(0.05, post_mean + explore_scale * post_sd * z) if thompson_on else max(0.05, post_mean)

            mu_roas[cid] = post_mean
            sampled_roas[cid] = ts_roas
            prior_used[cid] = p_roas
            prior_source[cid] = p_src
            prior_weight_frac[cid] = pseudo_spend / (ws + pseudo_spend)

        # Concave diminishing returns parameter s0 and per-creative spend caps
        s0 = self._estimate_s0(rows, global_mean_roas)
        caps = self._compute_caps(rows)

        # Cold-start test floor on first day or two for unserved creatives
        cold_days = int(self.options.get("cold_start_test_days", 2))
        cold_dollars = float(self.options.get("cold_start_test_dollars", 22.0))
        alloc: dict[str, float] = {cid: 0.0 for cid in self.creative_ids}
        is_cold_start: dict[str, bool] = {}

        if cold_floor_on and cold_days > 0 and cold_dollars >= 1.0:
            cold_cids = [cid for cid in sorted(self.creative_ids) if days_with_spend[cid] < cold_days]
            max_cold_pool = 0.35 * self.daily_budget
            per_cold = (
                min(cold_dollars, max_cold_pool / len(cold_cids))
                if cold_cids
                else 0.0
            )
            if per_cold >= 1.0:
                for cid in cold_cids:
                    alloc[cid] = min(per_cold, caps[cid])
                    is_cold_start[cid] = True
        for cid in self.creative_ids:
            is_cold_start.setdefault(cid, False)

        # Greedy marginal expected revenue allocation: E[rev(s)] = r * s / (1 + s / s0)
        def exp_rev(cid: str, s: float) -> float:
            return sampled_roas[cid] * s / (1.0 + s / s0)

        step = max(1.0, float(self.options.get("step_dollars", 5.0)))
        remaining = max(0.0, self.daily_budget - sum(alloc.values()))

        while remaining > 1e-6:
            inc = min(step, remaining)
            best_cid = None
            best_gain = -1.0
            for cid in sorted(self.creative_ids):
                if alloc[cid] + 1e-9 >= caps[cid]:
                    continue
                new_s = min(caps[cid], alloc[cid] + inc)
                actual_inc = new_s - alloc[cid]
                if actual_inc <= 1e-6:
                    continue
                gain = (exp_rev(cid, new_s) - exp_rev(cid, alloc[cid])) / actual_inc
                if gain > best_gain:
                    best_gain = gain
                    best_cid = cid

            if best_cid is None:
                # All caps saturated; relax toward hard max_share cap or uniform fallback
                hard_max = max(25.0, float(self.options.get("max_share", 0.14)) * self.daily_budget)
                expanded = False
                for cid in sorted(self.creative_ids, key=lambda c: (-sampled_roas[c], c)):
                    room = hard_max - alloc[cid]
                    if room > 1e-6:
                        add = min(inc, room)
                        alloc[cid] += add
                        remaining -= add
                        expanded = True
                        break
                if not expanded:
                    add_each = remaining / len(self.creative_ids)
                    for cid in self.creative_ids:
                        alloc[cid] += add_each
                    remaining = 0.0
            else:
                new_s = min(caps[best_cid], alloc[best_cid] + inc)
                used = new_s - alloc[best_cid]
                alloc[best_cid] = new_s
                remaining -= used

        # Optional uniform floor mix applied BEFORE $1 cleanup and max_share cap
        raw_mix = _to_finite_nonneg(self.options.get("uniform_mix", 0.0))
        u_mix = min(1.0, raw_mix) if raw_mix is not None else 0.0
        if u_mix > 0.0 and self.creative_ids:
            total_now = sum(alloc.values())
            uni_dollars = self.daily_budget / len(self.creative_ids)
            if total_now > 0.0:
                for cid in self.creative_ids:
                    pol_dollars = alloc[cid] * (self.daily_budget / total_now)
                    alloc[cid] = (1.0 - u_mix) * pol_dollars + u_mix * uni_dollars
            else:
                for cid in self.creative_ids:
                    alloc[cid] = uni_dollars

            # Enforce max_share cap if feasible across creative count
            hard_max = max(25.0, float(self.options.get("max_share", 0.14)) * self.daily_budget)
            if len(self.creative_ids) * hard_max >= self.daily_budget - 1e-6:
                for _ in range(3):
                    excess = 0.0
                    under_cap: list[str] = []
                    for cid in self.creative_ids:
                        if alloc[cid] > hard_max:
                            excess += alloc[cid] - hard_max
                            alloc[cid] = hard_max
                        elif alloc[cid] < hard_max - 1e-9:
                            under_cap.append(cid)
                    if excess <= 1e-9 or not under_cap:
                        break
                    room_sum = sum(hard_max - alloc[c] for c in under_cap)
                    if room_sum <= 1e-9:
                        break
                    for cid in under_cap:
                        alloc[cid] += excess * ((hard_max - alloc[cid]) / room_sum)

        # Enforce $1 minimum rule: drop any sub-$1 allocations and redistribute cleanly
        for _ in range(3):
            total_now = sum(alloc.values())
            if total_now <= 0.0:
                break
            sub_one = [cid for cid, v in alloc.items() if 0.0 < (v / total_now) * self.daily_budget < 1.0]
            if not sub_one:
                break
            reclaim = 0.0
            for cid in sub_one:
                reclaim += alloc[cid]
                alloc[cid] = 0.0
            # Give reclaimed dust to top funded creative below cap (or highest sampled ROAS)
            candidates = sorted(
                [cid for cid in self.creative_ids if alloc[cid] >= 1.0],
                key=lambda c: (-sampled_roas[c], c),
            )
            if candidates:
                alloc[candidates[0]] += reclaim

        shares = self._normalize_shares(alloc)

        # Build explainability records
        expl: dict[str, dict[str, Any]] = {}
        for cid in self.creative_ids:
            dollars = shares[cid] * self.daily_budget
            if dollars < 1.0:
                mode = "zero"
            elif is_cold_start[cid]:
                mode = "cold_start_test"
            elif thompson_on and sampled_roas[cid] > mu_roas[cid] * 1.05:
                mode = "explore"
            else:
                mode = "exploit"

            lag_ratio = (w_rev[cid] / w_rev_raw[cid]) if w_rev_raw[cid] > 0.0 else 1.0
            expl[cid] = {
                "creative_id": cid,
                "allocated_dollars": round(dollars, 2),
                "share": round(shares[cid], 6),
                "estimated_roas": round(mu_roas[cid], 4),
                "sampled_roas": round(sampled_roas[cid], 4),
                "prior_roas": round(prior_used[cid], 4),
                "prior_source": prior_source[cid],
                "prior_weight_frac": round(prior_weight_frac[cid], 3),
                "lag_correction_applied": bool(lag_applied_flag[cid]),
                "lag_uplift_factor": round(lag_ratio, 3),
                "mode": mode,
                "capped": bool(dollars >= caps[cid] - 1.0),
                "cap_dollars": round(caps[cid], 2),
                "s0": round(s0, 1),
            }

        self._day_cache[day_key] = dict(shares)
        self._explanations[day_key] = expl

        if bool(self.options.get("log_stderr", True)):
            top5 = sorted(expl.values(), key=lambda x: (-x["allocated_dollars"], x["creative_id"]))[:5]
            top_str = ", ".join(
                f"{r['creative_id']}:${r['allocated_dollars']:.0f}(roas={r['estimated_roas']:.2f},"
                f"{r['mode']},p={r['prior_weight_frac']:.2f}{'*cap' if r['capped'] else ''})"
                for r in top5
            )
            print(f"[day {day_key}] s0={s0:.0f} top5: {top_str}", file=sys.stderr)

        return shares
