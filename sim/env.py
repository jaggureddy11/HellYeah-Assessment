"""Config-driven simulated delivery environment for Creative Rotation Brain.

Uses only numpy and the Python standard library.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


def _load_json_or_dict(world_config: dict[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(world_config, dict):
        return world_config
    path = Path(world_config)
    return json.loads(path.read_text(encoding="utf-8"))


def load_creatives_csv(data_dir: str | Path = "data") -> list[dict[str, str]]:
    path = Path(data_dir) / "creatives.csv"
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_daily_final_csv(data_dir: str | Path = "data") -> list[dict[str, Any]]:
    path = Path(data_dir) / "daily_final.csv"
    rows: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(
                {
                    "day": int(r["day"]),
                    "creative_id": r["creative_id"],
                    "budget": float(r["budget"]),
                    "spend": float(r["spend"]),
                    "impressions": int(r["impressions"]),
                    "clicks": int(r["clicks"]),
                    "purchases": int(r["purchases"]),
                    "revenue": float(r["revenue"]),
                }
            )
    return rows


def _lognormal_centered(rng: np.random.Generator, sigma: float) -> float:
    if sigma <= 0.0:
        return 1.0
    return float(rng.lognormal(mean=-0.5 * sigma * sigma, sigma=sigma))


def _hill_saturation(spend: float, sat_cfg: dict[str, Any]) -> float:
    if spend <= 0.0:
        return 1.0
    floor = float(sat_cfg["sat_floor"])
    half = float(sat_cfg["sat_half_spend"])
    power = float(sat_cfg["sat_power"])
    return float(floor + (1.0 - floor) / (1.0 + (spend / half) ** power))


def get_min_allocation(world_cfg: dict[str, Any]) -> float:
    min_alloc_cfg = world_cfg.get("min_allocation_dollars", 1.0)
    return float(min_alloc_cfg["value"]) if isinstance(min_alloc_cfg, dict) else float(min_alloc_cfg)


def effective_spend_cap(budget: float, creative_cfg: dict[str, Any], world_cfg: dict[str, Any]) -> float:
    """Deterministic delivery ceiling for a creative given daily budget."""
    deliv_cfg = world_cfg["delivery_shortfall"]
    soft_knee = float(deliv_cfg["global_soft_knee"])
    soft_scale = float(deliv_cfg["global_soft_scale"])
    creative_cap = float(creative_cfg["daily_spend_cap"])
    global_cap = (
        soft_knee + soft_scale * (1.0 - math.exp(-(budget - soft_knee) / soft_scale))
        if budget > soft_knee
        else budget
    )
    return float(min(creative_cap, global_cap))


def expected_spend(budget: float, creative_cfg: dict[str, Any], world_cfg: dict[str, Any]) -> float:
    """Deterministic expected delivered spend for a creative given allocated budget."""
    if budget < get_min_allocation(world_cfg):
        return 0.0
    return float(min(budget, effective_spend_cap(budget, creative_cfg, world_cfg)))


def expected_revenue(
    spend: float,
    age: int,
    day: int,
    creative_cfg: dict[str, Any],
    world_cfg: dict[str, Any],
) -> float:
    """Deterministic expected revenue for one creative given spend, age since launch, and day.

    Shared directly by both World._simulate_creative_day and Clairvoyant so they cannot drift.
    """
    _ = day  # Day-of-week effect is zero in world_config; signature supports future day effects.
    if spend <= 0.0 or age < 0:
        return 0.0
    fatigue_rate = float(creative_cfg["fatigue_rate"])
    fatigue_mult = math.exp(-fatigue_rate * age)
    sat_mult = _hill_saturation(spend, world_cfg["saturation"])
    expected_roas = float(creative_cfg["true_base_roas"]) * sat_mult * fatigue_mult
    return float(spend * expected_roas)


@dataclass
class _TrueRow:
    day: int
    creative_id: str
    budget: float
    spend: float
    impressions: int
    clicks: int
    true_purchases: int
    true_revenue: float
    purchase_delays: np.ndarray
    purchase_revenues: np.ndarray
    outage_held_mask: np.ndarray


class World:
    """Simulated ad-delivery environment driven entirely by world_config."""

    def __init__(self, world_config: dict[str, Any] | str | Path, seed: int) -> None:
        self.config: dict[str, Any] = _load_json_or_dict(world_config)
        self.seed = int(seed)
        self.rng = np.random.default_rng(self.seed)

        self.daily_budget = float(self.config.get("daily_budget", 2000.0))
        self.min_allocation = get_min_allocation(self.config)

        self.creatives_by_id: dict[str, dict[str, Any]] = {
            c["creative_id"]: dict(c) for c in self.config["creatives"]
        }
        self.creative_ids: list[str] = [c["creative_id"] for c in self.config["creatives"]]
        self.first_served_day: dict[str, int | None] = {
            c["creative_id"]: (int(c["launch_day"]) if c.get("launch_day") is not None else None)
            for c in self.config["creatives"]
        }
        self.rows: list[_TrueRow] = []

    def _draw_purchase_delays(self, n_purchases: int) -> tuple[np.ndarray, np.ndarray]:
        if n_purchases <= 0:
            return np.empty(0, dtype=int), np.empty(0, dtype=bool)
        lag_curve = [float(x) for x in self.config["attribution_lag"]["completion_by_age"]]
        # Convert cumulative completion curve [c_0, c_1, ...] into discrete delay probabilities
        probs: list[float] = []
        prev = 0.0
        for c in lag_curve:
            c_clamped = min(1.0, max(prev, c))
            probs.append(max(0.0, c_clamped - prev))
            prev = c_clamped
        rem = max(0.0, 1.0 - prev)
        probs.append(rem)
        prob_arr = np.array(probs, dtype=float)
        prob_arr = prob_arr / prob_arr.sum()
        delays = self.rng.choice(len(prob_arr), size=n_purchases, p=prob_arr)

        outage_cfg = self.config.get("outage_window", {})
        if outage_cfg.get("enabled", False):
            hold_prob = max(0.0, min(1.0, 1.0 - float(outage_cfg.get("reported_multiplier", 1.0))))
            held = self.rng.random(size=n_purchases) < hold_prob
        else:
            held = np.zeros(n_purchases, dtype=bool)
        return delays, held

    def ingest_historical_rows(self, hist_rows: list[dict[str, Any]]) -> None:
        """Load pre-existing days 1..60 true rows and assign deterministic lag realizations."""
        for r in hist_rows:
            pur = int(r["purchases"])
            rev = float(r["revenue"])
            delays, held = self._draw_purchase_delays(pur)
            if pur > 0 and rev > 0.0:
                weights = self.rng.dirichlet(np.ones(pur, dtype=float) * 4.0)
                p_revs = np.round(rev * weights, 2)
                # Adjust rounding residual onto first purchase so sum matches rev
                p_revs[0] = round(p_revs[0] + (rev - float(p_revs.sum())), 2)
            else:
                p_revs = np.empty(0, dtype=float)

            self.rows.append(
                _TrueRow(
                    day=int(r["day"]),
                    creative_id=str(r["creative_id"]),
                    budget=round(float(r["budget"]), 2),
                    spend=round(float(r["spend"]), 2),
                    impressions=int(r["impressions"]),
                    clicks=int(r["clicks"]),
                    true_purchases=pur,
                    true_revenue=round(rev, 2),
                    purchase_delays=delays,
                    purchase_revenues=p_revs,
                    outage_held_mask=held,
                )
            )

    def _simulate_creative_day(self, day: int, cid: str, raw_budget: float, record_zero_unserved: bool = False) -> _TrueRow | None:
        c = self.creatives_by_id[cid]
        if raw_budget < self.min_allocation:
            # Never served creatives have no rows; previously served creatives get a 0 row
            if self.first_served_day[cid] is None and not record_zero_unserved:
                return None
            return _TrueRow(
                day=day,
                creative_id=cid,
                budget=0.0,
                spend=0.0,
                impressions=0,
                clicks=0,
                true_purchases=0,
                true_revenue=0.0,
                purchase_delays=np.empty(0, dtype=int),
                purchase_revenues=np.empty(0, dtype=float),
                outage_held_mask=np.empty(0, dtype=bool),
            )

        if self.first_served_day[cid] is None:
            self.first_served_day[cid] = day
        age = max(0, day - int(self.first_served_day[cid]))

        budget = round(float(raw_budget), 2)
        jitter_sd = float(self.config["delivery_shortfall"]["shortfall_jitter_sd"])
        effective_cap = effective_spend_cap(budget, c, self.config)
        if budget <= 0.85 * effective_cap:
            spend = budget
        else:
            jitter = _lognormal_centered(self.rng, jitter_sd)
            spend = min(budget, effective_cap * jitter)
        spend = round(max(0.0, spend), 2)

        if spend <= 0.0:
            return _TrueRow(
                day=day,
                creative_id=cid,
                budget=budget,
                spend=0.0,
                impressions=0,
                clicks=0,
                true_purchases=0,
                true_revenue=0.0,
                purchase_delays=np.empty(0, dtype=int),
                purchase_revenues=np.empty(0, dtype=float),
                outage_held_mask=np.empty(0, dtype=bool),
            )

        noise_cfg = self.config["noise"]
        fat_cfg = self.config["fatigue"]
        fatigue_rate = float(c["fatigue_rate"])
        fatigue_mult = math.exp(-fatigue_rate * age)

        # Impressions & clicks
        cpm_spend_bump = 1.0 + 0.03 * max(0.0, spend - 100.0) / 100.0
        cpm = float(c["base_cpm"]) * cpm_spend_bump * _lognormal_centered(self.rng, float(noise_cfg["cpm_lognormal_sd"]))
        impressions = max(1, int(round(1000.0 * spend / max(0.5, cpm))))

        ctr_fat_share = float(fat_cfg.get("ctr_fatigue_share", 0.70))
        ctr_fatigue = fatigue_mult ** ctr_fat_share
        ctr_mean = min(0.35, float(c["base_ctr"]) * ctr_fatigue)
        ctr_drawn = min(0.35, ctr_mean * _lognormal_centered(self.rng, float(noise_cfg["ctr_lognormal_sd"])))
        clicks = int(self.rng.binomial(impressions, ctr_drawn))

        # Expected conversions derived from the shared deterministic expected_revenue()
        expected_rev = expected_revenue(spend, age, day, c, self.config)
        base_aov = float(c["base_aov"])
        expected_purchases = expected_rev / max(1.0, base_aov)
        expected_clicks = max(0.5, impressions * ctr_mean)
        base_cvr = expected_purchases / expected_clicks

        cvr_shock = _lognormal_centered(self.rng, float(noise_cfg["daily_cvr_lognormal_sd"]))
        lam_pur = max(0.0, clicks * base_cvr * cvr_shock)
        purchases = int(self.rng.poisson(lam_pur))

        if purchases > 0:
            aov_sd = float(noise_cfg["aov_lognormal_sd"])
            p_revs = np.round(
                base_aov * np.array([_lognormal_centered(self.rng, aov_sd) for _ in range(purchases)]),
                2,
            )
            p_revs = np.maximum(1.0, p_revs)
            revenue = round(float(p_revs.sum()), 2)
        else:
            p_revs = np.empty(0, dtype=float)
            revenue = 0.0

        delays, held = self._draw_purchase_delays(purchases)
        return _TrueRow(
            day=day,
            creative_id=cid,
            budget=budget,
            spend=spend,
            impressions=impressions,
            clicks=clicks,
            true_purchases=purchases,
            true_revenue=revenue,
            purchase_delays=delays,
            purchase_revenues=p_revs,
            outage_held_mask=held,
        )

    def step_day(self, day: int, budgets: dict[str, float], record_zero_unserved: bool = False) -> list[_TrueRow]:
        """Simulate one day across all creatives in deterministic order."""
        day_rows: list[_TrueRow] = []
        for cid in self.creative_ids:
            b = float(budgets.get(cid, 0.0))
            row = self._simulate_creative_day(day, cid, b, record_zero_unserved=record_zero_unserved)
            if row is not None:
                self.rows.append(row)
                day_rows.append(row)
        return day_rows

    def reported_history(self, current_day: int) -> list[dict[str, Any]]:
        """Return cumulative history for all days < current_day with reporting lag applied."""
        outage_cfg = self.config.get("outage_window", {})
        outage_enabled = bool(outage_cfg.get("enabled", False))
        o_start = int(outage_cfg.get("start_day", 0))
        o_end = int(outage_cfg.get("end_day", -1))
        o_resolved = int(outage_cfg.get("resolved_after_day", 9999))
        obs_cutoff_day = current_day - 1

        history: list[dict[str, Any]] = []
        for r in self.rows:
            if r.day >= current_day:
                continue
            lag_age = obs_cutoff_day - r.day
            in_outage = outage_enabled and (o_start <= r.day <= o_end) and (obs_cutoff_day <= o_resolved)
            if r.true_purchases > 0:
                visible = r.purchase_delays <= lag_age
                if in_outage:
                    visible = visible & (~r.outage_held_mask)
                rep_pur = int(visible.sum())
                rep_rev = round(float(r.purchase_revenues[visible].sum()), 2) if rep_pur > 0 else 0.0
            else:
                rep_pur = 0
                rep_rev = 0.0

            history.append(
                {
                    "day": r.day,
                    "creative_id": r.creative_id,
                    "budget": r.budget,
                    "spend": r.spend,
                    "impressions": r.impressions,
                    "clicks": r.clicks,
                    "purchases": rep_pur,
                    "revenue": rep_rev,
                }
            )
        return history


def _validate_and_normalize_shares(
    raw_shares: dict[str, float],
    creative_ids: list[str],
    daily_budget: float,
) -> dict[str, float]:
    """Enforce exact BRIEF.md / FAQ.md share validation and normalization."""
    if not isinstance(raw_shares, dict):
        raise TypeError("allocate() must return a dict[str, float]")

    cid_set = set(creative_ids)
    cleaned: dict[str, float] = {cid: 0.0 for cid in creative_ids}
    for k, v in raw_shares.items():
        if k not in cid_set:
            continue  # Unknown creative_ids are ignored
        val = float(v)
        if not math.isfinite(val) or val < 0.0:
            raise ValueError(f"Invalid share for {k}: {v!r}")
        cleaned[k] = val

    total = sum(cleaned.values())
    if total <= 0.0:
        # All zeros treated as uniform
        share = 1.0 / len(creative_ids)
        return {cid: share * daily_budget for cid in creative_ids}

    return {cid: (cleaned[cid] / total) * daily_budget for cid in creative_ids}


def run(
    policy: Any,
    world_config: dict[str, Any] | str | Path,
    seed: int,
    start_day: int = 61,
    end_day: int = 90,
    data_dir: str | Path = "data",
) -> dict[str, Any]:
    """Run a 30-day simulation loop mimicking the evaluation harness."""
    world = World(world_config=world_config, seed=seed)
    creatives = load_creatives_csv(data_dir=data_dir)
    if start_day > 1:
        hist_all = load_daily_final_csv(data_dir=data_dir)
        world.ingest_historical_rows([r for r in hist_all if r["day"] < start_day])

    if isinstance(policy, type) or (callable(policy) and not hasattr(policy, "allocate")):
        policy_inst = policy(creatives, world.daily_budget, seed)
    else:
        policy_inst = policy

    per_day: list[dict[str, Any]] = []
    total_revenue = 0.0
    total_spend = 0.0
    total_purchases = 0
    total_clicks = 0
    total_impressions = 0

    for day in range(start_day, end_day + 1):
        hist = world.reported_history(current_day=day)
        raw_shares = policy_inst.allocate(day, hist)
        budgets = _validate_and_normalize_shares(raw_shares, world.creative_ids, world.daily_budget)
        day_rows = world.step_day(day, budgets)

        d_spend = round(sum(r.spend for r in day_rows), 2)
        d_rev = round(sum(r.true_revenue for r in day_rows), 2)
        d_pur = sum(r.true_purchases for r in day_rows)
        d_clk = sum(r.clicks for r in day_rows)
        d_imp = sum(r.impressions for r in day_rows)

        total_spend += d_spend
        total_revenue += d_rev
        total_purchases += d_pur
        total_clicks += d_clk
        total_impressions += d_imp

        per_day.append(
            {
                "day": day,
                "allocated_budget": round(sum(budgets.values()), 2),
                "spend": d_spend,
                "impressions": d_imp,
                "clicks": d_clk,
                "purchases": d_pur,
                "revenue": d_rev,
            }
        )

    return {
        "seed": seed,
        "start_day": start_day,
        "end_day": end_day,
        "total_revenue": round(total_revenue, 2),
        "total_spend": round(total_spend, 2),
        "total_purchases": total_purchases,
        "total_clicks": total_clicks,
        "total_impressions": total_impressions,
        "roas": round(total_revenue / total_spend, 4) if total_spend > 0 else 0.0,
        "per_day": per_day,
    }
